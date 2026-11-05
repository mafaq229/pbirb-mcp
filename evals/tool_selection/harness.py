"""Run the tool-selection eval: Claude + pbirb-mcp on realistic edit requests.

Each (case, rep) gets a fresh copy of the fixture in its own workspace. The
model drives pbirb-mcp through the same JSON-RPC `tools/call` path an MCP
client uses; the grader then reads the edited .rdl directly, and a
structural gate (XSD + every lint error the edit introduced) fails any edit
that left the file broken. `corrupted` is reported as its own metric.

Setups ("arms"):
  all     every tool definition sent up front (what MCP clients do today)
  search  every tool deferred behind Anthropic's BM25 tool search, except
          describe_report
  subset  only the tools for the case's category plus common read tools
          (a ceiling: tool choice made easy)

Usage (from the repo root):
  uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness selftest   # offline; CI runs it
  uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness cases      # regenerate CASES.md
  uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness run \
      --variant v3 --model claude-haiku-5-5 --arm all --reps 2 [--cases d01,p01]
  uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness run ... --approve-harness

Output goes to evals/tool_selection/runs/<variant>/ (results.jsonl,
traces/<id>_rep<k>.json, errors.jsonl), the layout the eval report builder
reads.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from evals.tool_selection.cases import BY_ID, CASES, FIXTURES, Case, Rdl
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

HERE = Path(__file__).parent
RUNS = HERE / "runs"
STATE = RUNS / "_state.json"
HARNESS_PATHS = [HERE / "harness.py", HERE / "cases.py", HERE / "fixtures" / "build_fixtures.py"]

SYSTEM_PROMPT = (
    "You help people edit Power BI paginated reports (.rdl files) using the pbirb tools. "
    "Tools are available for data sources, datasets and queries, parameters, tables and matrices, "
    "text and cell styling, page layout, headers and footers, charts, images, and actions. "
    "The user's message names the report file. Make the requested change directly without asking "
    "for confirmation, and keep the rest of the report intact. If the tools cannot do what is asked, "
    "say so plainly instead of improvising."
)

MUTATING_PREFIXES = (
    "set_",
    "add_",
    "remove_",
    "update_",
    "rename_",
    "reorder_",
    "insert_",
    "style_",
    "duplicate_",
    "sync_",
    "convert_",
    "create_",
    "apply_",
    "raw_xml_replace",
    "restore_",
    "refresh_",
    "commit_",
)

COMMON_TOOLS = [
    "describe_report",
    "list_body_items",
    "get_textbox",
    "get_tablixes",
    "get_chart",
    "get_parameters",
    "get_datasets",
    "get_dataset",
    "list_data_sources",
    "verify_report",
    "find_textbox_by_value",
    "get_expression_reference",
    "list_header_items",
    "list_footer_items",
    "get_image",
]

# Category -> tool-name patterns for the "subset" arm.
CATEGORY_TOOLS = {
    "data": r"data_source|dataset|query|calculated_field|refresh_dataset",
    "parameters": r"parameter",
    "tablix": r"tablix|row_group|column_group|matrix|subtotal|cell_span|static_|column_width|group_sort|group_visibility|detail_row|row_height|corner|group_page_break|repeat_on|keep_",
    "style": r"textbox|style|alternating|conditional_row",
    "layout": r"page|body|header|footer|line|rectangle|image|element_visibility|position|size",
    "chart": r"chart|series",
    "interactivity": r"action|tooltip|document_map",
    "question": r"^$",
    "decline": r"^$",
}

PRICES = {  # $/MTok: input, output. Cache write 1.25x input, cache read 0.1x input.
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
}

MAX_TURNS = 30
CASE_CEILING_S = 420


# ---------------------------------------------------------------------------
# workspace + MCP plumbing
# ---------------------------------------------------------------------------


def build_tools(server: MCPServer) -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["inputSchema"]}
        for t in server._tools_list()
    ]


def tools_for_arm(all_tools: list[dict], arm: str, case: Case) -> list[dict]:
    if arm == "all":
        return all_tools
    if arm == "search":
        out: list[dict] = [
            {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
        ]
        for t in all_tools:
            out.append(t if t["name"] == "describe_report" else {**t, "defer_loading": True})
        return out
    if arm == "subset":
        pattern = re.compile(CATEGORY_TOOLS[case.category])
        keep = set(COMMON_TOOLS) | {name for name, _ in case.oracle}
        return [
            t
            for t in all_tools
            if t["name"] in keep
            or (case.category not in ("question", "decline") and pattern.search(t["name"]))
        ]
    raise ValueError(arm)


class Workspace:
    def __init__(self, case: Case):
        self.dir = Path(tempfile.mkdtemp(prefix=f"pbirb-eval-{case.id}-"))
        self.report = self.dir / "report.rdl"
        shutil.copy(FIXTURES / case.fixture, self.report)
        self.logo = self.dir / "logo.png"
        shutil.copy(FIXTURES / "logo.png", self.logo)
        self.initial_sha = self.sha()

    def sha(self) -> str:
        return hashlib.sha256(self.report.read_bytes()).hexdigest()

    def render(self, text: str) -> str:
        return text.replace("{LOGO_PATH}", str(self.logo))

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class ToolRunner:
    """Executes tool calls in-process through MCPServer.handle_request."""

    PATH_KEYS = ("path", "src_path", "dst_path", "backup_path", "target_path", "image_path")

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.server = MCPServer()
        register_all_tools(self.server)
        self._id = 0

    def _outside(self, args: dict) -> Optional[str]:
        for k in self.PATH_KEYS:
            v = args.get(k)
            if (
                isinstance(v, str)
                and v
                and not Path(v).resolve().is_relative_to(self.ws.dir.resolve())
            ):
                return k
        return None

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        bad = self._outside(args)
        if bad:
            return (
                json.dumps(
                    {
                        "error_type": "PermissionError",
                        "message": f"{bad} must be inside {self.ws.dir}",
                    }
                ),
                True,
            )
        self._id += 1
        resp = self.server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        if resp is None or "error" in resp:
            err = (resp or {}).get("error", {})
            return (
                json.dumps(
                    {"error_type": "JSONRPCError", "message": err.get("message", "no response")}
                ),
                True,
            )
        result = resp["result"]
        text = "".join(c.get("text", "") for c in result.get("content", []))
        return (text, bool(result.get("isError")))


# ---------------------------------------------------------------------------
# structural gate (#60)
# ---------------------------------------------------------------------------

# Lint rules the gate relies on for the invariants the XSD cannot see:
# tablix hierarchy leaves vs body columns/rows, cells, spans and corner
# (core/tablix_headers.shape_findings); chart series vs series-hierarchy
# members and data points vs category members (core/chart_shape); and
# report-wide duplicate names (core/names). If one is renamed or dropped the
# gate refuses to run rather than silently losing coverage.
GATE_RULES = ("tablix-structure", "chart-structure", "duplicate-name")


class GateMisconfigured(Exception):
    pass


def _structural_errors(path: Path) -> list[tuple[tuple, str]]:
    """``(issue_key, message)`` for every error-severity finding in ``path``:
    parse / XSD (``validate_report``) plus every lint rule (``lint_report``)."""
    from pbirb_mcp.ops.lint import lint_report
    from pbirb_mcp.ops.validate import issue_key, validate_report

    v = validate_report(str(path))
    found = [e for e in v["errors"] if e.get("severity") == "error"]
    if not any(e.get("rule") == "parse" for e in found):
        lint = lint_report(str(path))
        missing = [r for r in GATE_RULES if r not in lint["rules_run"]]
        if missing:
            raise GateMisconfigured(f"structural gate: lint rules {missing} did not run")
        found += [i for i in lint["issues"] if i.get("severity") == "error"]
    return [(issue_key(i), f"{i.get('rule')}: {i.get('message')}") for i in found]


def structural_violations(report: Path, baseline: Path) -> list[str]:
    """Error-severity problems in ``report`` that its starting file
    ``baseline`` did not have: XSD, every lint error (tablix-structure,
    chart-structure, duplicate-name, dangling references, ...). Matched as
    a multiset of ``issue_key`` (the same identity ``verify_report``'s
    ``baseline_path`` uses), so a fixture's own pre-existing errors never
    count and a second copy of one does. Empty means structurally clean.

    A crash while checking is reported as a violation: a file the checker
    can't read is not a file to trust."""
    try:
        before = Counter(k for k, _ in _structural_errors(baseline))
        after = _structural_errors(report)
    except GateMisconfigured:
        raise
    except Exception as exc:  # noqa: BLE001
        return [f"structural check crashed: {type(exc).__name__}: {exc}"]
    new = []
    for key, message in after:
        if before[key] > 0:
            before[key] -= 1
        else:
            new.append(message)
    return new


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def is_mutating(name: str) -> bool:
    return name.startswith(MUTATING_PREFIXES)


def grade(case: Case, ws: Workspace, calls: list[dict], answer: str) -> tuple[dict, dict]:
    """Returns (grade, explanation).

    ``success`` needs both the case's own check and the structural gate;
    ``corrupted`` is 1 when the edit introduced any structural error, whether
    or not the check passed, so a run can be read as "did the asked-for
    thing" and "left the file intact" separately."""
    called = [c["name"] for c in calls]
    oracle_tools = {n for n, _ in case.oracle}
    mutations = [n for n in called if is_mutating(n)]
    explanation: dict[str, str] = {}
    unchanged = ws.sha() == ws.initial_sha
    corrupt = [] if unchanged else structural_violations(ws.report, FIXTURES / case.fixture)

    if case.must_not_edit:
        passed, reason = unchanged, "ok" if unchanged else "report was modified"
        if passed and case.answer_must_mention:
            low = answer.lower()
            missing = [
                alts for alts in case.answer_must_mention if not any(a.lower() in low for a in alts)
            ]
            if missing:
                passed, reason = False, f"answer does not mention {missing[0]}"
        right_tool = 1.0 if not mutations else 0.0
    else:
        try:
            rdl = Rdl(ws.report)
            passed, reason = case.check(rdl)
        except Exception as exc:  # noqa: BLE001 — a corrupt report is a model failure here
            passed, reason = False, f"report unreadable: {exc}"
        right_tool = 1.0 if oracle_tools & set(called) else 0.0

    explanation["check"] = reason
    if passed and corrupt:
        passed, reason = False, f"edit corrupted the report: {corrupt[0]}"
    explanation["success"] = reason
    explanation["corrupted"] = " | ".join(corrupt[:5]) if corrupt else "ok"
    return (
        {
            "success": 1.0 if passed else 0.0,
            "corrupted": 1.0 if corrupt else 0.0,
            "right_tool": right_tool,
            "raw_xml": 1.0 if "raw_xml_replace" in called else 0.0,
            "tool_errors": float(sum(1 for c in calls if c["is_error"])),
        },
        explanation,
    )


# ---------------------------------------------------------------------------
# agents: real model, plus scripted oracle/null for self-testing
# ---------------------------------------------------------------------------


def _block_dict(block: Any) -> dict:
    return block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)


async def run_model(client, model: str, arm: str, case: Case, ws: Workspace, effort: str) -> dict:
    tools = tools_for_arm(build_tools(MCPServer_with_tools()), arm, case)
    runner = ToolRunner(ws)
    user_text = f"Report file: {ws.report}\n\n{ws.render(case.prompt)}"
    messages: list[dict] = [{"role": "user", "content": user_text}]
    trace: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    calls: list[dict] = []
    searches = 0
    served_model, stop_reason, answer = None, None, ""
    model_time = 0.0

    for _turn in range(MAX_TURNS):
        t0 = time.monotonic()
        resp = await client.messages.create(
            model=model,
            max_tokens=16000,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=messages,
            tools=tools,
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},
        )
        model_time += time.monotonic() - t0
        served_model, stop_reason = resp.model, resp.stop_reason
        if not resp.model.startswith(model):
            raise ServedModelMismatch(f"asked for {model}, served {resp.model}")
        for k in usage:
            usage[k] += getattr(resp.usage, k, 0) or 0

        content = [_block_dict(b) for b in resp.content]
        messages.append({"role": "assistant", "content": content})
        tool_results = []
        for b in content:
            if b["type"] == "text" and b.get("text"):
                answer = b["text"]
                trace.append({"role": "assistant", "content": b["text"]})
            elif b["type"] == "server_tool_use":
                searches += 1
                trace.append(
                    {
                        "role": "tool_call",
                        "name": b["name"],
                        "content": json.dumps(b.get("input"), indent=2),
                    }
                )
            elif b["type"] == "tool_search_tool_result":
                refs = [
                    r.get("tool_name") for r in (b.get("content") or {}).get("tool_references", [])
                ]
                trace.append({"role": "tool_result", "content": json.dumps({"found": refs})})
            elif b["type"] == "tool_use":
                out, is_err = runner.call(b["name"], b.get("input") or {})
                calls.append({"name": b["name"], "is_error": is_err})
                trace.append(
                    {
                        "role": "tool_call",
                        "name": b["name"],
                        "content": json.dumps(b.get("input"), indent=2),
                    }
                )
                trace.append({"role": "tool_result", "content": out[:4000]})
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "content": out,
                        "is_error": is_err,
                    }
                )
        if resp.stop_reason != "tool_use":
            break
        messages.append({"role": "user", "content": tool_results})
    else:
        stop_reason = "max_turns"

    return {
        "calls": calls,
        "answer": answer,
        "trace": trace,
        "usage": usage,
        "model": served_model,
        "stop_reason": stop_reason,
        "searches": searches,
        "model_time": model_time,
        "n_tools": len(tools),
    }


_TOOLS_SERVER: Optional[MCPServer] = None


def MCPServer_with_tools() -> MCPServer:
    global _TOOLS_SERVER
    if _TOOLS_SERVER is None:
        _TOOLS_SERVER = MCPServer()
        register_all_tools(_TOOLS_SERVER)
    return _TOOLS_SERVER


# Plausible-but-wrong solutions; each must fail its case's check.
WRONG = {
    "d04": [
        (
            "add_tablix_filter",
            {
                "tablix_name": "FleetTable",
                "expression": "=Fields!EquipmentType.Value",
                "operator": "Equal",
                "values": ["Excavator"],
            },
        )
    ],
    "t07": [
        (
            "add_dataset_filter",
            {
                "dataset_name": "DataSet1",
                "expression": "=Fields!Utilization.Value",
                "operator": "LessThan",
                "values": ["=0.5"],
            },
        )
    ],
    "t05": [
        (
            "set_group_sort",
            {
                "tablix_name": "FleetTable",
                "group_name": "Details",
                "sort_expressions": ["=Fields!Utilization.Value"],
            },
        )
    ],
    "p01": [("set_parameter_type", {"name": "DateFrom", "type": "DateTime"})],
    "s07": [
        ("set_textbox_value", {"textbox_name": "TotalHoursLabel", "value": "Total Operating Hours"})
    ],
    "l02": [("set_body_item_position", {"name": "HoursBySite", "top": "1.6in", "left": "0in"})],
    "c01": [
        (
            "set_chart_series_type",
            {"chart_name": "HoursBySite", "series_name": "OperatingHours", "series_type": "Bar"},
        )
    ],
    "d05": [
        (
            "update_dataset_query",
            {
                "dataset_name": "DataSet1",
                "alias_strategy": "preserve_field_names",
                "dax_body": "EVALUATE SUMMARIZECOLUMNS('Assets'[Manufacturer], 'Assets'[AssetName])",
            },
        ),
        (
            "add_dataset_field",
            {
                "dataset_name": "DataSet1",
                "field_name": "Manufacturer",
                "data_field": "Assets[Manufacturer]",
            },
        ),
    ],
    # A grand total (scope=total) instead of one subtotal per site.
    "t10": [
        (
            "add_subtotal_row",
            {
                "tablix_name": "FleetTable",
                "group_name": "Site",
                "scope": "total",
                "aggregates": [
                    {
                        "column": "FleetTable_OperatingHours",
                        "expression": "=Sum(Fields!OperatingHours.Value)",
                    },
                    {
                        "column": "FleetTable_IdleHours",
                        "expression": "=Sum(Fields!IdleHours.Value)",
                    },
                ],
            },
        )
    ],
    # A title row that isn't merged across the table.
    "t09": [
        (
            "add_static_row",
            {
                "tablix_name": "FleetTable",
                "row_name": "FleetTable_Title",
                "cells": ["Fleet assets"],
                "position": 0,
            },
        )
    ],
    # Idle hours added, but operating hours left on the chart.
    "c07": [
        (
            "add_chart_series",
            {"chart_name": "HoursBySite", "series_name": "IdleHours", "value_field": "IdleHours"},
        )
    ],
    # One run: ' (%)' comes out bold like the rest of the heading.
    "s10": [
        (
            "set_textbox_value",
            {"textbox_name": "FleetTable_Header_Utilization", "value": "Utilization (%)"},
        )
    ],
}


# Solutions that pass their case's own check but leave the file structurally
# broken. The structural gate must fail each one; there is one per gate
# component (tablix-structure, chart-structure, duplicate-name, xsd).
def _corrupt_t03(report: Path) -> list[tuple[str, dict]]:
    """Drop the Equipment Type column from the body only: the column
    hierarchy keeps 6 members for 5 TablixColumns."""
    from lxml import etree

    from evals.tool_selection.cases import NS

    xpath = "//r:Tablix[@Name='FleetTable']/r:TablixBody"
    body = etree.parse(str(report)).xpath(xpath, namespaces=NS)[0]
    cells = body.xpath("r:TablixRows/r:TablixRow/r:TablixCells", namespaces=NS)
    for parent in [body.find("r:TablixColumns", NS), *cells]:
        parent.remove(parent[2])
    return [
        ("raw_xml_replace", {"xpath": xpath, "content": etree.tostring(body, encoding="unicode")})
    ]


_SERIES = (
    '<ChartSeries Name="{0}"><ChartDataPoints><ChartDataPoint><ChartDataPointValues>'
    "<Y>=Sum(Fields!{0}.Value)</Y></ChartDataPointValues></ChartDataPoint></ChartDataPoints>"
    "<Type>Column</Type><Subtype>Plain</Subtype></ChartSeries>"
)

CORRUPT: dict[str, Any] = {
    # A second series with no ChartSeriesHierarchy member.
    "c05": [
        (
            "raw_xml_replace",
            {
                "xpath": "//r:Chart[@Name='HoursBySite']//r:ChartSeriesCollection",
                "content": "<ChartSeriesCollection>"
                + _SERIES.format("OperatingHours")
                + _SERIES.format("IdleHours")
                + "</ChartSeriesCollection>",
            },
        )
    ],
    "t03": _corrupt_t03,
    # The label is right, but a second textbox now carries its name.
    "s07": [
        BY_ID["s07"].oracle[0],
        (
            "raw_xml_replace",
            {
                "xpath": "//r:Textbox[@Name='TotalHoursValue']",
                "content": '<Textbox Name="TotalHoursLabel"><Paragraphs><Paragraph><TextRuns>'
                "<TextRun><Value>=Sum(Fields!OperatingHours.Value)</Value><Style /></TextRun>"
                "</TextRuns><Style /></Paragraph></Paragraphs><Top>1.3in</Top><Left>4in</Left>"
                "<Height>0.25in</Height><Width>1in</Width><Style /></Textbox>",
            },
        ),
    ],
    # A line carrying an element the RDL schema doesn't allow there.
    "l06": [
        BY_ID["l06"].oracle[0],
        (
            "raw_xml_replace",
            {
                "xpath": "//r:Line[@Name='TitleRule']",
                "content": '<Line Name="TitleRule"><Top>0.45in</Top><Left>0in</Left>'
                "<Height>0in</Height><Width>6in</Width><Caption>rule</Caption></Line>",
            },
        ),
    ],
}


def run_scripted(case: Case, ws: Workspace, mode: str) -> dict:
    """Replay a scripted solution: mode='oracle' the reference solution,
    'wrong' a plausible mistake, 'corrupt' one that passes the case's check
    but breaks the file, 'null' nothing."""
    runner = ToolRunner(ws)
    calls, trace = [], [{"role": "user", "content": ws.render(case.prompt)}]
    answer = ""
    if mode in ("oracle", "wrong", "corrupt"):
        script = {"oracle": case.oracle, "wrong": WRONG, "corrupt": CORRUPT}[mode]
        if isinstance(script, dict):
            script = script[case.id]
        if callable(script):
            script = script(ws.report)
        for name, args in script:
            args = json.loads(ws.render(json.dumps(args)))
            out, is_err = runner.call(name, {"path": str(ws.report), **args})
            calls.append({"name": name, "is_error": is_err})
            trace += [
                {"role": "tool_call", "name": name, "content": json.dumps(args)},
                {"role": "tool_result", "content": out},
            ]
            answer += out
    return {
        "calls": calls,
        "answer": answer,
        "trace": trace,
        "usage": {},
        "model": mode,
        "stop_reason": "end_turn",
        "searches": 0,
        "model_time": 0.0,
        "n_tools": 0,
    }


class ServedModelMismatch(Exception):
    pass


# ---------------------------------------------------------------------------
# run loop
# ---------------------------------------------------------------------------


def harness_sha() -> str:
    h = hashlib.sha256()
    for p in HARNESS_PATHS:
        h.update(p.read_bytes())
    return h.hexdigest()


def check_harness(approve: bool) -> None:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    sha = harness_sha()
    if approve:
        state["harness_sha"] = sha
        STATE.write_text(json.dumps(state, indent=2) + "\n")
        return
    if state.get("harness_sha") != sha:
        sys.exit(
            "harness files changed since last approval; review them and re-run with --approve-harness"
        )


def cost_usd(model: str, usage: dict) -> float:
    if model not in PRICES:
        return 0.0
    i, o = PRICES[model]
    return (
        usage.get("input_tokens", 0) * i
        + usage.get("output_tokens", 0) * o
        + usage.get("cache_creation_input_tokens", 0) * i * 1.25
        + usage.get("cache_read_input_tokens", 0) * i * 0.1
    ) / 1e6


async def run_one(
    case: Case, rep: int, args, client, out_dir: Path, sem: asyncio.Semaphore, lock: asyncio.Lock
) -> Optional[dict]:
    async with sem:
        ws = Workspace(case)
        t0 = time.monotonic()
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    if args.scripted:
                        res = run_scripted(case, ws, args.scripted)
                    else:
                        res = await asyncio.wait_for(
                            run_model(client, args.model, args.arm, case, ws, args.effort),
                            timeout=CASE_CEILING_S,
                        )
                    break
                except (asyncio.TimeoutError, ServedModelMismatch) as exc:
                    await _record_error(
                        out_dir, lock, case, rep, type(exc).__name__, str(exc), attempt
                    )
                    return None
                except Exception as exc:  # noqa: BLE001
                    import anthropic

                    retryable = isinstance(
                        exc,
                        (
                            anthropic.RateLimitError,
                            anthropic.InternalServerError,
                            anthropic.APIConnectionError,
                            anthropic.APITimeoutError,
                        ),
                    )
                    if retryable and attempt < 5:
                        await asyncio.sleep(min(60, 2**attempt) + random.uniform(0, 1))
                        ws.cleanup()
                        ws = Workspace(case)  # strict: retry starts from a clean report
                        continue
                    await _record_error(
                        out_dir, lock, case, rep, type(exc).__name__, str(exc)[:500], attempt
                    )
                    return None
            g, expl = grade(case, ws, res["calls"], res["answer"])
            status = "truncated" if res["stop_reason"] == "max_tokens" else "ok"
            if res["stop_reason"] == "refusal":
                expl["success"] = "model refused"
            row = {
                "prompt_id": case.id,
                "rep": rep,
                "prompt": ws.render(case.prompt),
                "tags": case.tags,
                "status": status,
                "stop_reason": res["stop_reason"],
                "grade": g,
                "explanation": expl,
                "model": res["model"],
                "usage": res["usage"],
                "arm": args.arm or args.scripted,
                "tool_calls": len(res["calls"]),
                "tool_searches": res["searches"],
                "tools_offered": res["n_tools"],
                "latency_s": round(res["model_time"], 2),
                "wall_s": round(time.monotonic() - t0, 2),
                "attempts": attempt,
                "called": [c["name"] for c in res["calls"]],
                "known_gap": case.known_gap,
                "cost_usd": round(cost_usd(args.model or "", res["usage"]), 5),
            }
            trace = res["trace"] + [
                {"role": "assistant", "content": f"[grade] {g} — {expl['success']}"}
            ]
            async with lock:
                (out_dir / "traces" / f"{case.id}_rep{rep}.json").write_text(
                    json.dumps(trace, indent=1)
                )
                with open(out_dir / "results.jsonl", "a") as f:
                    f.write(json.dumps(row) + "\n")
            return row
        finally:
            ws.cleanup()


async def _record_error(
    out_dir: Path, lock: asyncio.Lock, case: Case, rep: int, cls: str, msg: str, attempt: int
):
    async with lock:
        with open(out_dir / "errors.jsonl", "a") as f:
            f.write(
                json.dumps(
                    {
                        "prompt_id": case.id,
                        "rep": rep,
                        "failure_class": cls,
                        "message": msg,
                        "attempts": attempt,
                    }
                )
                + "\n"
            )


def done_keys(out_dir: Path) -> set[tuple[str, int]]:
    p = out_dir / "results.jsonl"
    if not p.exists():
        return set()
    return {(r["prompt_id"], r["rep"]) for r in map(json.loads, p.read_text().splitlines()) if r}


def summarize(rows: list[dict]) -> str:
    if not rows:
        return "no rows"
    n = len(rows)
    succ = sum(r["grade"]["success"] for r in rows) / n
    half = 1.96 * (succ * (1 - succ) / n) ** 0.5
    cost = sum(r.get("cost_usd", 0) for r in rows)
    by_cat: dict[str, list[float]] = {}
    for r in rows:
        by_cat.setdefault(r["tags"][0], []).append(r["grade"]["success"])
    cats = "  ".join(f"{k}:{sum(v) / len(v):.0%}" for k, v in by_cat.items())
    graded = [r["grade"]["corrupted"] for r in rows if "corrupted" in r["grade"]]
    # Rows graded before the structural gate existed (#60) have no `corrupted`.
    corrupted = f"corrupted {int(sum(graded))}/{len(graded)}" if graded else "corrupted n/a"
    gaps = [r["grade"]["success"] for r in rows if r.get("known_gap")]
    gap_line = f"\nknown-gap cases passed {int(sum(gaps))}/{len(gaps)}" if gaps else ""
    return (
        f"success {succ:.1%} ±{half:.1%} (n={n})  {corrupted}  cost ${cost:.2f}\n{cats}{gap_line}"
    )


async def cmd_run(args) -> None:
    out_dir = (
        (RUNS / args.variant)
        if not args.scripted
        else Path(tempfile.mkdtemp(prefix="pbirb-selftest-"))
    )
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    cases = [BY_ID[c] for c in args.cases.split(",")] if args.cases else CASES
    skip = done_keys(out_dir)
    client = None
    if not args.scripted:
        import anthropic

        client = anthropic.AsyncAnthropic(max_retries=0)
        meta = out_dir / "change.md"
        if not meta.exists() and args.variant != "baseline":
            meta.write_text(f"{args.model} · {args.arm} tools · effort {args.effort}\n")
    sem, lock = asyncio.Semaphore(args.concurrency), asyncio.Lock()
    jobs = [
        run_one(c, rep, args, client, out_dir, sem, lock)
        for c in cases
        for rep in range(args.reps)
        if (c.id, rep) not in skip
    ]
    t0 = time.monotonic()
    rows = [r for r in await asyncio.gather(*jobs) if r]
    print(f"{len(rows)} rows in {time.monotonic() - t0:.0f}s -> {out_dir}")
    all_rows = (
        [json.loads(line) for line in (out_dir / "results.jsonl").read_text().splitlines()]
        if (out_dir / "results.jsonl").exists()
        else []
    )
    print(summarize(all_rows))
    if args.scripted:
        return out_dir, all_rows


def cmd_selftest() -> int:
    """Offline, no API calls. The oracle must pass every case except the known
    gaps, without corrupting the file; doing nothing must fail every edit case
    (known gaps included); every wrong solution must fail; every corrupt
    solution must pass its case's own check and still fail on the
    structural gate."""
    failures = []
    for kind, table in (("wrong", WRONG), ("corrupt", CORRUPT)):
        failures += [f"{kind} {cid}: no such case" for cid in table if cid not in BY_ID]
    # The gate is baseline-aware: a fixture's own errors are not corruption.
    bad = FIXTURES / BY_ID["q03"].fixture
    if structural_violations(bad, bad) or not structural_violations(
        bad, FIXTURES / CASES[0].fixture
    ):
        failures.append("gate: pre-existing fixture errors are not matched against the baseline")
    gaps = [c for c in CASES if c.known_gap]
    for mode in ("oracle", "null", "wrong", "corrupt"):
        ids = {
            "oracle": ",".join(c.id for c in CASES if not c.known_gap),
            "null": None,
            "wrong": ",".join(WRONG),
            "corrupt": ",".join(CORRUPT),
        }[mode]
        ns = argparse.Namespace(
            scripted=mode,
            cases=ids,
            reps=1,
            concurrency=8,
            model=None,
            arm=None,
            effort=None,
            variant=None,
        )
        out_dir, rows = asyncio.run(cmd_run(ns))
        for r in rows:
            case = BY_ID[r["prompt_id"]]
            want = 1.0 if (mode == "oracle" or (mode == "null" and case.must_not_edit)) else 0.0
            if mode == "null" and case.answer_must_mention:
                want = 0.0  # an empty answer must not pass a question
            g, expl = r["grade"], r["explanation"]
            if g["success"] != want:
                failures.append(
                    f"{mode} {r['prompt_id']}: success={g['success']} ({expl['success']})"
                )
            if mode == "oracle" and g["corrupted"]:
                failures.append(f"oracle {r['prompt_id']}: corrupted ({expl['corrupted']})")
            if mode == "corrupt" and (expl["check"] != "ok" or not g["corrupted"]):
                failures.append(
                    f"corrupt {r['prompt_id']}: check={expl['check']!r} "
                    f"corrupted={g['corrupted']} (must pass the check and fail the gate)"
                )
        errs = out_dir / "errors.jsonl"
        if errs.exists():
            failures += [f"{mode} error: {line}" for line in errs.read_text().splitlines()]
        shutil.rmtree(out_dir, ignore_errors=True)
    print(
        "\n".join(failures)
        or "selftest ok: oracle passes all cases without corrupting them; null passes only "
        "no-edit cases; wrong solutions all fail; corrupt solutions pass their checks and "
        "fail the structural gate"
    )
    if gaps:
        print(
            "known gaps (oracle not replayed; null still checked): "
            + "; ".join(f"{c.id} — {c.known_gap}" for c in gaps)
        )
    return 1 if failures else 0


def cmd_cases_md() -> int:
    """Write CASES.md, the human-readable list of every case for review."""
    all_tools = build_tools(MCPServer_with_tools())
    lines = [
        "# Eval cases",
        "",
        f"{len(CASES)} cases. Generated from `cases.py` by `harness.py cases`; edit that file, not this one.",
        "",
        "Every edit case is also graded by the structural gate (XSD + lint errors the edit "
        "introduced, see `CONTEXT.md`). A known gap is a request no tool can do correctly yet; "
        "its oracle is not replayed by `selftest`.",
        "",
        "| id | category | difficulty | source | fixture | reference solution | tools in subset arm |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in CASES:
        ref = " → ".join(f"`{n}`" for n, _ in c.oracle) or "_(no edit)_"
        if c.must_not_edit:
            ref = (ref + "; must not edit") if c.oracle else "must not edit"
        if c.known_gap:
            ref = f"_known gap_ ({c.known_gap.split(':')[0]})"
        lines.append(
            f"| {c.id} | {c.category} | {c.difficulty} | {c.source} | {c.fixture} | {ref} | "
            f"{len(tools_for_arm(all_tools, 'subset', c))} |"
        )
    for c in CASES:
        lines += ["", f"## {c.id} · {c.category} · {c.difficulty}", "", "```text", c.prompt, "```"]
        if c.known_gap:
            lines.append(f"Known gap: {c.known_gap}")
        if c.answer_must_mention:
            lines.append(
                "Answer must mention: "
                + " AND ".join("(" + " or ".join(a) + ")" for a in c.answer_must_mention)
            )
    (HERE / "CASES.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {HERE / 'CASES.md'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    sub.add_parser("cases")
    r = sub.add_parser("run")
    r.add_argument("--variant", required=True, help="baseline or v<N>")
    r.add_argument("--model", required=True)
    r.add_argument("--arm", required=True, choices=["all", "search", "subset"])
    r.add_argument("--effort", default="medium")
    r.add_argument("--reps", type=int, default=2)
    r.add_argument("--cases", help="comma-separated case ids (default: all)")
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--approve-harness", action="store_true")
    args = ap.parse_args()
    if args.cmd == "selftest":
        return cmd_selftest()
    if args.cmd == "cases":
        return cmd_cases_md()
    if not re.fullmatch(r"baseline|v\d+", args.variant):
        sys.exit("--variant must be 'baseline' or v<N>")
    RUNS.mkdir(exist_ok=True)
    check_harness(args.approve_harness)
    args.scripted = None
    asyncio.run(cmd_run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
