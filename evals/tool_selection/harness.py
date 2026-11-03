"""Run the tool-selection eval: Claude + pbirb-mcp on realistic edit requests.

Each (case, rep) gets a fresh copy of the fixture in its own workspace. The
model drives pbirb-mcp through the same JSON-RPC `tools/call` path an MCP
client uses; the grader then reads the edited .rdl directly.

Setups ("arms"):
  all     every tool definition sent up front (what MCP clients do today)
  search  every tool deferred behind Anthropic's BM25 tool search, except
          describe_report
  subset  only the tools for the case's category plus common read tools
          (a ceiling: tool choice made easy)

Usage (from the repo root):
  uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness selftest
  uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness run \
      --variant baseline --model claude-sonnet-5-5 --arm all --reps 2 [--cases d01,p01]
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
# grading
# ---------------------------------------------------------------------------


def is_mutating(name: str) -> bool:
    return name.startswith(MUTATING_PREFIXES)


def grade(case: Case, ws: Workspace, calls: list[dict], answer: str) -> tuple[dict, dict]:
    """Returns (grade, explanation)."""
    called = [c["name"] for c in calls]
    oracle_tools = {n for n, _ in case.oracle}
    mutations = [n for n in called if is_mutating(n)]
    explanation: dict[str, str] = {}
    unchanged = ws.sha() == ws.initial_sha

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
        if passed:
            verdict = json.loads(ToolRunner(ws).call("verify_report", {"path": str(ws.report)})[0])
            if not verdict.get("valid", False):
                passed = False
                errs = [i for i in verdict.get("issues", []) if i.get("severity") == "error"]
                reason = f"edit left the report invalid: {errs[0]['message'] if errs else verdict}"
        right_tool = 1.0 if oracle_tools & set(called) else 0.0

    explanation["success"] = reason
    return (
        {
            "success": 1.0 if passed else 0.0,
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
}


def run_scripted(case: Case, ws: Workspace, mode: str) -> dict:
    """mode='oracle' replays the reference solution, 'wrong' a plausible mistake, 'null' does nothing."""
    runner = ToolRunner(ws)
    calls, trace = [], [{"role": "user", "content": ws.render(case.prompt)}]
    answer = ""
    if mode in ("oracle", "wrong"):
        for name, args in case.oracle if mode == "oracle" else WRONG[case.id]:
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
    return f"success {succ:.1%} ±{half:.1%} (n={n})  cost ${cost:.2f}\n{cats}"


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
    """Oracle must pass every case; doing nothing must fail every edit case."""
    failures = []
    for mode in ("oracle", "null", "wrong"):
        ids = ",".join(WRONG) if mode == "wrong" else None
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
            if r["grade"]["success"] != want:
                failures.append(
                    f"{mode} {r['prompt_id']}: success={r['grade']['success']} ({r['explanation']['success']})"
                )
        errs = out_dir / "errors.jsonl"
        if errs.exists():
            failures += [f"{mode} error: {line}" for line in errs.read_text().splitlines()]
        shutil.rmtree(out_dir, ignore_errors=True)
    print(
        "\n".join(failures)
        or "selftest ok: oracle passes all cases; null passes only no-edit cases; wrong solutions all fail"
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
        "| id | category | difficulty | source | reference solution | tools in subset arm |",
        "|---|---|---|---|---|---|",
    ]
    for c in CASES:
        ref = " → ".join(f"`{n}`" for n, _ in c.oracle) or "_(no edit)_"
        if c.must_not_edit:
            ref = (ref + "; must not edit") if c.oracle else "must not edit"
        lines.append(
            f"| {c.id} | {c.category} | {c.difficulty} | {c.source} | {ref} | "
            f"{len(tools_for_arm(all_tools, 'subset', c))} |"
        )
    for c in CASES:
        lines += ["", f"## {c.id} · {c.category} · {c.difficulty}", "", "```text", c.prompt, "```"]
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
