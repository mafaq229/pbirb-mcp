"""Reader tool tests.

The reader surface is the LLM's situational awareness — every multi-step
edit starts with one of these. Output schemas are stable: callers chain on
field names like ``datasets[0].command_text`` and ``tablixes[*].row_groups``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.ops.reader import (
    describe_report,
    get_datasets,
    get_parameters,
    get_tablixes,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


# ---- describe_report -------------------------------------------------------


class TestDescribeReport:
    def test_returns_top_level_inventory(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert out["path"] == str(rdl_path)
        assert out["data_sources"] == ["PowerBIDataset"]
        assert out["datasets"] == ["MainDataset"]
        assert out["parameters"] == ["DateFrom", "DateTo"]
        assert out["tablixes"] == ["MainTable"]
        assert out["page"]["height"] == "11in"
        assert out["page"]["width"] == "8.5in"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            describe_report(path=str(tmp_path / "missing.rdl"))


# ---- get_datasets ----------------------------------------------------------


class TestGetDatasets:
    def test_exposes_full_dax_body(self, rdl_path):
        out = get_datasets(path=str(rdl_path))
        assert len(out) == 1
        ds = out[0]
        assert ds["name"] == "MainDataset"
        assert ds["data_source"] == "PowerBIDataset"
        # The full DAX body — not just a sproc name (the upstream rdl-mcp gap).
        assert ds["command_text"] == "EVALUATE 'Sales'"

    def test_lists_fields_with_data_field_and_type(self, rdl_path):
        out = get_datasets(path=str(rdl_path))
        fields = out[0]["fields"]
        assert {f["name"] for f in fields} == {"ProductID", "ProductName", "Amount"}
        amount = next(f for f in fields if f["name"] == "Amount")
        assert amount["data_field"] == "Sales[Amount]"
        assert amount["type_name"] == "System.Decimal"

    def test_query_parameters_default_empty(self, rdl_path):
        out = get_datasets(path=str(rdl_path))
        assert out[0]["query_parameters"] == []

    def test_filters_default_empty(self, rdl_path):
        out = get_datasets(path=str(rdl_path))
        assert out[0]["filters"] == []


# ---- get_parameters --------------------------------------------------------


class TestGetParameters:
    def test_lists_report_parameters(self, rdl_path):
        out = get_parameters(path=str(rdl_path))
        assert len(out) == 2
        names = [p["name"] for p in out]
        assert names == ["DateFrom", "DateTo"]

    def test_each_parameter_has_data_type_and_prompt(self, rdl_path):
        out = get_parameters(path=str(rdl_path))
        date_from = next(p for p in out if p["name"] == "DateFrom")
        assert date_from["data_type"] == "DateTime"
        assert date_from["prompt"] == "Date From"


# ---- get_tablixes ----------------------------------------------------------


class TestGetTablixes:
    def test_returns_tablix_with_layout(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        assert len(out) == 1
        t = out[0]
        assert t["name"] == "MainTable"
        assert t["dataset"] == "MainDataset"
        assert t["top"] == "0.5in"
        assert t["left"] == "0.5in"
        assert t["width"] == "4in"
        assert t["height"] == "0.5in"

    def test_lists_columns_with_widths(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        cols = out[0]["columns"]
        assert [c["width"] for c in cols] == ["1in", "2in", "1in"]

    def test_lists_row_groups_keyed_by_name(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        groups = out[0]["row_groups"]
        # Fixture has the conventional `Details` group plus a header member.
        assert "Details" in [g["name"] for g in groups if g["name"]]

    def test_filters_default_empty(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        assert out[0]["filters"] == []

    def test_visibility_default_none(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        # No visibility expression in fixture.
        assert out[0]["visibility"] is None


# ---- registration / JSON-RPC integration ----------------------------------


class TestToolRegistration:
    def test_all_four_readers_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {"describe_report", "get_datasets", "get_parameters", "get_tablixes"} <= names

    def test_describe_report_callable_via_jsonrpc(self, rdl_path):
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "describe_report",
                    "arguments": {"path": str(rdl_path)},
                },
            }
        )
        # Result is wrapped as MCP content; the JSON body is in content[0].text.
        text = resp["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["datasets"] == ["MainDataset"]

    def test_input_schema_requires_path(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        describe = next(t for t in listing if t["name"] == "describe_report")
        assert describe["inputSchema"]["required"] == ["path"]
        assert describe["inputSchema"]["properties"]["path"]["type"] == "string"
