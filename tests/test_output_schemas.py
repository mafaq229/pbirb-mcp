"""Issue #55: outputSchema + structuredContent, response_format, size caps."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import jsonschema
import pytest
from lxml import etree

from pbirb_mcp.core.xpath import XPATH_NS
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
CHART = FIXTURES / "pbi_chart_rich.rdl"


@pytest.fixture(scope="module")
def server():
    srv = MCPServer()
    register_all_tools(srv)
    return srv


@pytest.fixture
def rdl(tmp_path):
    dst = tmp_path / "report.rdl"
    shutil.copy(CHART, dst)
    return dst


def _call(server, tool, version_meta=None, **arguments):
    params = {"name": tool, "arguments": arguments}
    if version_meta:
        params["_meta"] = version_meta
    resp = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    assert "result" in resp, resp
    return resp["result"]


def _schemas(server):
    listing = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {t["name"]: t.get("outputSchema") for t in listing["result"]["tools"]}


def _check(server, name, result):
    """structuredContent matches the text block and the published outputSchema."""
    assert not result.get("isError"), result
    text_value = json.loads(result["content"][0]["text"])
    structured = result["structuredContent"]
    expected = text_value if isinstance(text_value, dict) else {"result": text_value}
    assert structured == expected
    jsonschema.validate(structured, _schemas(server)[name])
    return text_value


class TestEveryToolDeclaresOutputSchema:
    def test_every_tool_has_an_object_output_schema(self, server):
        schemas = _schemas(server)
        assert len(schemas) >= 140
        for name, schema in schemas.items():
            assert schema is not None, name
            assert schema.get("type") == "object", name
            jsonschema.Draft202012Validator.check_schema(schema)


# (tool, arguments) — a representative call from every result family.
REPRESENTATIVE = [
    ("describe_report", {}),
    ("describe_report", {"response_format": "concise"}),
    ("get_datasets", {}),
    ("get_datasets", {"response_format": "concise"}),
    ("get_parameters", {}),
    ("get_parameters", {"response_format": "concise"}),
    ("get_tablixes", {}),
    ("get_tablixes", {"response_format": "concise"}),
    ("get_dataset", {"name": "MainDataset"}),
    ("get_dataset", {"name": "MainDataset", "response_format": "concise"}),
    ("get_data_source", {"name": "PowerBIDataset"}),
    ("get_textbox", {"name": "Amount"}),
    ("get_textbox", {"name": "Amount", "response_format": "concise"}),
    ("get_chart", {"name": "SalesByProduct"}),
    ("get_chart", {"name": "SalesByProduct", "response_format": "concise"}),
    ("list_body_items", {}),
    ("list_data_sources", {}),
    ("list_embedded_images", {}),
    ("list_dataset_filters", {"dataset_name": "MainDataset"}),
    ("find_textbox_by_value", {"pattern": "Amount"}),
    ("raw_xml_view", {"xpath": ".//r:Textbox"}),
    ("raw_xml_view", {"xpath": ".//r:Tablix", "max_bytes": 300}),
    ("validate_report", {}),
    ("verify_report", {}),
    ("lint_report", {}),
    (
        "dry_run_edit",
        {"ops": [{"tool": "set_textbox_value", "args": {"textbox_name": "Amount", "value": "x"}}]},
    ),
    ("dry_run_edit", {"ops": [], "summary": True}),
    ("set_textbox_style", {"textbox_name": "Amount", "font_weight": "Bold"}),
    (
        "add_body_textbox",
        {
            "name": "Note",
            "text": "hi",
            "top": "0in",
            "left": "0in",
            "width": "1in",
            "height": "0.25in",
        },
    ),
    ("set_page_orientation", {"orientation": "Landscape"}),
    ("backup_report", {}),
    (
        "apply_edits",
        {"ops": [{"tool": "set_textbox_value", "args": {"textbox_name": "Amount", "value": "y"}}]},
    ),
    ("start_editing_transaction", {}),
]


@pytest.mark.parametrize(
    "name,args", REPRESENTATIVE, ids=[f"{n}-{i}" for i, (n, _) in enumerate(REPRESENTATIVE)]
)
def test_structured_content_validates_against_output_schema(server, rdl, name, args):
    result = _call(server, name, path=str(rdl), **args)
    _check(server, name, result)


@pytest.mark.parametrize(
    "name,args",
    [("count_where", {"condition": "Fields!X.Value > 0"}), ("get_expression_reference", {})],
)
def test_pathless_tools(server, name, args):
    _check(server, name, _call(server, name, **args))


def test_auto_verify_wrapped_result_validates(server, rdl, monkeypatch):
    monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")
    result = _call(server, "set_textbox_style", path=str(rdl), textbox_name="Amount", color="Red")
    value = _check(server, "set_textbox_style", result)
    assert set(value) == {"result", "verify"}


def test_errors_carry_no_structured_content(server, rdl):
    result = _call(server, "get_textbox", path=str(rdl), name="NoSuchBox")
    assert result["isError"] is True
    assert "structuredContent" not in result


class TestPerRevision:
    MODERN = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    def test_2026_07_28_carries_structured_content(self, server):
        result = _call(server, "describe_report", self.MODERN, path=str(CHART))
        assert result["structuredContent"]["datasets"] == ["MainDataset"]

    def test_2024_11_05_client_still_gets_the_text_block(self):
        # The SDK serves the older handshake revisions with the 2025-11-25
        # result surface; a client that predates structuredContent reads
        # the unchanged JSON text block.
        srv = MCPServer()
        register_all_tools(srv)
        init = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        }
        srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
        result = _call(srv, "describe_report", path=str(CHART))
        assert json.loads(result["content"][0]["text"])["datasets"] == ["MainDataset"]


# ---- response_format ---------------------------------------------------------


class TestResponseFormat:
    def test_default_is_detailed_and_unchanged(self, server):
        from pbirb_mcp.ops import reader

        result = _call(server, "describe_report", path=str(CHART))
        assert json.loads(result["content"][0]["text"]) == json.loads(
            json.dumps(reader.describe_report(str(CHART)))
        )

    def test_concise_describe_report_keeps_inventory_drops_item_lists(self, server):
        full = json.loads(_call(server, "describe_report", path=str(CHART))["content"][0]["text"])
        concise = json.loads(
            _call(server, "describe_report", path=str(CHART), response_format="concise")["content"][
                0
            ]["text"]
        )
        for key in ("datasets", "parameters", "data_sources", "charts"):
            assert concise[key] == full[key]
        # tablixes stay whole (shape hints / column details locate cells).
        assert concise["tablixes"] == full["tablixes"]
        assert "body_items" not in concise
        assert concise["omitted_counts"]["body_items"] == len(full["body_items"])
        assert len(json.dumps(concise)) < len(json.dumps(full))

    def test_concise_get_datasets_drops_query_text(self, server):
        text = _call(server, "get_datasets", path=str(CHART), response_format="concise")
        (ds,) = json.loads(text["content"][0]["text"])
        assert ds == {
            "name": "MainDataset",
            "data_source": "PowerBIDataset",
            "fields": ["ProductID", "ProductName", "Amount"],
            "query_parameters": [],
            "filter_count": 0,
            "designer_state_present": False,
        }

    def test_concise_get_tablixes(self, server):
        text = _call(server, "get_tablixes", path=str(CHART), response_format="concise")
        (tx,) = json.loads(text["content"][0]["text"])
        assert tx["name"] == "MainTable"
        assert tx["column_count"] == 3
        assert tx["row_groups"] == ["Details"]
        assert "cells" not in tx

    def test_bad_response_format_is_invalid_arguments(self, server):
        result = _call(server, "get_parameters", path=str(CHART), response_format="tiny")
        assert result["isError"] is True
        assert json.loads(result["content"][0]["text"])["error_type"] == "InvalidArguments"


# ---- size caps -----------------------------------------------------------------


def _big_report(tmp_path, copies=80):
    """The chart fixture with its tablix cloned ``copies`` times (~2 KB each)."""
    tree = etree.parse(str(CHART))
    items = tree.getroot().find(".//r:Body/r:ReportItems", XPATH_NS)
    tablix = items.find("r:Tablix", XPATH_NS)
    for i in range(copies):
        clone = copy.deepcopy(tablix)
        clone.set("Name", f"Copy{i}")
        for tb in clone.iterfind(".//r:Textbox", XPATH_NS):
            tb.set("Name", f"{tb.get('Name')}_{i}")
        items.append(clone)
    out = tmp_path / "big.rdl"
    tree.write(str(out), xml_declaration=True, encoding="utf-8")
    return out


class TestRawXmlViewCap:
    def test_default_cap_is_explicit_and_bounded(self, server, tmp_path):
        big = _big_report(tmp_path)
        result = _call(server, "raw_xml_view", path=str(big), xpath=".//r:Tablix")
        text = result["content"][0]["text"]
        # ~25k tokens is ~100 KB; the default lands well under.
        assert len(text) < 60_000
        items = json.loads(text)
        assert len(items) < 81
        assert "<!-- pbirb-mcp: truncated" in items[-1]
        assert "later match(es) omitted" in items[-1]
        assert sum(len(i.encode()) for i in items[:-1]) <= 40_000

    def test_max_bytes_zero_disables_cap(self, server, tmp_path):
        big = _big_report(tmp_path, copies=30)
        items = json.loads(
            _call(server, "raw_xml_view", path=str(big), xpath=".//r:Tablix", max_bytes=0)[
                "content"
            ][0]["text"]
        )
        assert len(items) == 31
        assert not any("pbirb-mcp: truncated" in i for i in items)
        for item in items:
            etree.fromstring(item)  # every element whole

    def test_under_the_cap_is_untouched(self, server):
        from pbirb_mcp.ops.escape import raw_xml_view

        full = raw_xml_view(str(CHART), ".//r:Textbox", max_bytes=0)
        assert raw_xml_view(str(CHART), ".//r:Textbox") == full

    def test_cut_marker_reports_sizes(self):
        from pbirb_mcp.ops.escape import raw_xml_view

        out = raw_xml_view(str(CHART), ".//r:Textbox", max_bytes=100)
        assert len(out) == 1
        assert out[0].endswith("-->")
        assert "showing 100 of" in out[0]

    def test_negative_max_bytes_rejected(self, server):
        result = _call(server, "raw_xml_view", path=str(CHART), xpath=".", max_bytes=-1)
        assert result["isError"] is True


class TestDryRunSummary:
    OPS = [{"tool": "set_textbox_value", "args": {"textbox_name": "Amount", "value": "changed"}}]

    def test_default_has_diff_and_stats(self, server, rdl):
        out = json.loads(
            _call(server, "dry_run_edit", path=str(rdl), ops=self.OPS)["content"][0]["text"]
        )
        assert out["diff"]
        assert out["diff_truncated"] is False
        assert out["diff_stats"]["lines_added"] >= 1
        assert out["diff_stats"]["hunks"] >= 1

    def test_summary_omits_diff_and_result_text(self, server, rdl):
        out = json.loads(
            _call(server, "dry_run_edit", path=str(rdl), ops=self.OPS, summary=True)["content"][0][
                "text"
            ]
        )
        assert "diff" not in out
        assert out["applied"] == [{"tool": "set_textbox_value", "ok": True}]
        assert out["diff_stats"]["changed"] is True
        assert set(out["verify"]) == {
            "valid",
            "xsd_used",
            "error_count",
            "issue_count",
            "issues_by_rule",
        }

    def test_diff_cap_is_explicit(self, server, rdl):
        out = json.loads(
            _call(server, "dry_run_edit", path=str(rdl), ops=self.OPS, max_diff_bytes=120)[
                "content"
            ][0]["text"]
        )
        assert out["diff_truncated"] is True
        assert "pbirb-mcp: diff truncated" in out["diff"]
        body, marker = out["diff"].rstrip("\n").rsplit("\n", 1)
        assert len(body.encode()) <= 120
        assert marker.startswith("\\ pbirb-mcp: diff truncated")
