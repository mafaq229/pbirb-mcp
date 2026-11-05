"""Tool titles and annotations (issue #54).

Every tool is published with a ``title`` and the four MCP hints
(``readOnlyHint``, ``destructiveHint``, ``idempotentHint``,
``openWorldHint``), derived from the registry's own classifications:
``mutating``, :data:`~pbirb_mcp.server.DESTRUCTIVE_TOOLS` (#57) and
:data:`~pbirb_mcp.server.IDEMPOTENT_TOOLS` (kept equal to the "noop" cases
of ``tests/test_idempotency.py``, #61). The hints must be true: every
``readOnlyHint`` tool is called on a fixture and must leave the file
untouched.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import mcp_types
import pytest

from pbirb_mcp.server import (
    DESTRUCTIVE_TOOLS,
    IDEMPOTENT_TOOLS,
    MCPServer,
    tool_annotations,
    tool_title,
)
from pbirb_mcp.tools import register_all_tools
from tests import test_idempotency

FIXTURES = Path(__file__).parent / "fixtures"
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


@pytest.fixture(scope="module")
def server():
    srv = MCPServer()
    register_all_tools(srv)
    return srv


def _listed(server, version="2025-11-25"):
    server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        }
    )
    resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {t["name"]: t for t in resp["result"]["tools"]}


def _call(server, name, arguments):
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    return resp["result"]


class TestPublished:
    @pytest.mark.parametrize("version", ["2025-03-26", "2025-06-18", "2025-11-25"])
    def test_every_tool_has_title_and_all_four_hints(self, server, version):
        tools = _listed(server, version)
        assert len(tools) == len(server._tools)
        for name, tool in tools.items():
            assert isinstance(tool.get("title"), str) and tool["title"], name
            ann = tool.get("annotations")
            assert isinstance(ann, dict), name
            for hint in HINTS:
                assert isinstance(ann.get(hint), bool), (name, hint)

    def test_tools_list_entries_are_valid_sdk_tools(self, server):
        for tool in _listed(server).values():
            sdk = mcp_types.Tool.model_validate(tool)
            assert sdk.title == tool["title"]
            assert sdk.annotations is not None
            assert sdk.annotations.read_only_hint == tool["annotations"]["readOnlyHint"]

    def test_modern_envelope_carries_annotations(self, server):
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
                    }
                },
            }
        )
        tool = next(t for t in resp["result"]["tools"] if t["name"] == "remove_parameter")
        assert tool["annotations"]["destructiveHint"] is True

    def test_a_newly_registered_tool_gets_them_too(self):
        srv = MCPServer()
        srv.register_tool("frob_widget", "Frob.", {"type": "object"}, lambda: {})
        tool = _listed(srv)["frob_widget"]
        assert tool["title"] == "Frob widget"
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        srv.register_tool(
            "frob_more", "Frob.", {"type": "object"}, lambda: {}, mutating=True, title="Frob!"
        )
        tool = _listed(srv)["frob_more"]
        assert tool["title"] == "Frob!"
        assert tool["annotations"]["readOnlyHint"] is False
        assert tool["annotations"]["idempotentHint"] is False


class TestHintsAreTrue:
    def test_open_world_is_false_everywhere(self, server):
        assert not any(t.annotations["openWorldHint"] for t in server._tools.values())

    def test_mutating_tools_are_not_read_only(self, server):
        for tool in server._tools.values():
            if tool.mutating:
                assert tool.annotations["readOnlyHint"] is False, tool.name

    def test_stateful_non_mutating_tools_are_not_read_only(self, server):
        for name in (
            "start_editing_transaction",
            "commit_editing_transaction",
            "cancel_editing_transaction",
            "backup_report",
        ):
            assert server._tools[name].annotations["readOnlyHint"] is False, name

    def test_destructive_hint_is_exactly_destructive_tools(self, server):
        flagged = {n for n, t in server._tools.items() if t.annotations["destructiveHint"]}
        assert flagged == set(DESTRUCTIVE_TOOLS)
        # The tools the issue names; every remover is checked in test_security.
        assert {"raw_xml_replace", "restore_from_backup", "convert_to_matrix"} <= flagged

    def test_idempotent_tools_match_the_idempotency_suite(self):
        noop = {n for n, c in test_idempotency.CASES.items() if c.second == "noop"}
        assert set(IDEMPOTENT_TOOLS) == noop

    def test_idempotent_hint(self, server):
        for name, tool in server._tools.items():
            expected = tool.annotations["readOnlyHint"] or name in IDEMPOTENT_TOOLS
            assert tool.annotations["idempotentHint"] is expected, name
        # A creator (refuses the repeat) and an appender are not idempotent.
        assert server._tools["add_parameter"].annotations["idempotentHint"] is False
        assert server._tools["add_tablix_filter"].annotations["idempotentHint"] is False


def test_tool_title():
    assert tool_title("set_page_setup") == "Set page setup"
    assert tool_title("raw_xml_view") == "View raw XML"
    assert tool_annotations("get_textbox", mutating=False)["readOnlyHint"] is True


# ---- every readOnlyHint tool leaves the report alone -------------------------

_TINY_PNG = test_idempotency._TINY_PNG

# Arguments for each read-only tool against the prepared report (`{path}`
# is filled in). A new read-only tool must be added here.
READ_ONLY_CALLS: dict[str, dict] = {
    "describe_report": {},
    "get_datasets": {},
    "get_parameters": {},
    "get_tablixes": {},
    "list_data_sources": {},
    "get_data_source": {"name": "PowerBIDataset"},
    "list_dataset_filters": {"dataset_name": "MainDataset"},
    "get_dataset": {"name": "MainDataset"},
    "get_embedded_image_data": {"name": "LogoData"},
    "validate_report": {},
    "verify_report": {},
    "lint_report": {},
    "dry_run_edit": {
        "ops": [{"tool": "set_textbox_value", "args": {"textbox_name": "Note", "value": "X"}}]
    },
    "get_expression_reference": None,
    "count_where": None,
    "sum_where": None,
    "iif_format": None,
    "raw_xml_view": {"xpath": ".//r:Tablix[@Name='MainTable']"},
    "list_tablix_filters": {"tablix_name": "MainTable"},
    "list_body_items": {"recursive": True},
    "list_header_items": {},
    "list_footer_items": {},
    "get_textbox": {"name": "Note"},
    "get_image": {"name": "Logo"},
    "get_rectangle": {"name": "Box"},
    "get_chart": {"name": "Chart2"},
    "find_textboxes_by_style": {"font_weight": "Bold"},
    "find_textbox_by_value": {"pattern": "Note"},
    "list_embedded_images": {},
}

# Path-less helpers: no file to touch; called with their own arguments.
_PATHLESS = {
    "get_expression_reference": {},
    "count_where": {"condition": "Fields!Amount.Value > 0"},
    "sum_where": {"field_expression": "Fields!Amount.Value", "condition": "True"},
    "iif_format": {"condition": "True", "true_value": '"a"', "false_value": '"b"'},
}


def test_every_read_only_tool_is_exercised(server):
    read_only = {n for n, t in server._tools.items() if t.annotations["readOnlyHint"]}
    assert read_only == set(READ_ONLY_CALLS)


@pytest.fixture(scope="module")
def prepared_report(tmp_path_factory, server):
    """The minimal fixture plus an embedded image, body image, rectangle,
    textbox and chart, so every reader has something to read."""
    tmp = tmp_path_factory.mktemp("annotations")
    rdl = tmp / "report.rdl"
    shutil.copy(FIXTURES / "pbi_paginated_minimal.rdl", rdl)
    png = tmp / "logo.png"
    png.write_bytes(_TINY_PNG)
    setup = [
        (
            "add_embedded_image",
            {"name": "LogoData", "mime_type": "image/png", "image_path": str(png)},
        ),
        (
            "add_body_image",
            {
                "name": "Logo",
                "image_source": "Embedded",
                "value": "LogoData",
                "top": "3in",
                "left": "0in",
                "width": "1in",
                "height": "1in",
            },
        ),
        (
            "add_rectangle",
            {"name": "Box", "top": "4in", "left": "0in", "width": "1in", "height": "1in"},
        ),
        (
            "add_body_textbox",
            {
                "name": "Note",
                "text": "Note",
                "top": "5in",
                "left": "0in",
                "width": "2in",
                "height": "0.25in",
            },
        ),
        (
            "insert_chart_from_template",
            {
                "name": "Chart2",
                "dataset_name": "MainDataset",
                "category_field": "ProductName",
                "value_field": "Amount",
                "top": "6in",
                "left": "0in",
                "width": "4in",
                "height": "3in",
            },
        ),
    ]
    for tool, args in setup:
        result = _call(server, tool, {"path": str(rdl), **args})
        assert not result.get("isError"), (tool, result)
    return rdl


@pytest.mark.parametrize("name", sorted(READ_ONLY_CALLS))
def test_read_only_tool_leaves_the_file_untouched(server, prepared_report, name):
    rdl = prepared_report
    before = (rdl.read_bytes(), os.stat(rdl).st_mtime_ns)
    siblings = sorted(p.name for p in rdl.parent.iterdir())
    args = _PATHLESS[name] if name in _PATHLESS else {"path": str(rdl), **READ_ONLY_CALLS[name]}
    result = _call(server, name, args)
    assert not result.get("isError"), json.loads(result["content"][0]["text"])
    assert (rdl.read_bytes(), os.stat(rdl).st_mtime_ns) == before
    assert sorted(p.name for p in rdl.parent.iterdir()) == siblings
