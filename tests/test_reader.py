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
    find_textbox_by_value,
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


class TestDescribeReportV03Extensions:
    """Phase 12 commit 44 — backward-compatible new fields."""

    def test_existing_fields_unchanged(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        # Spot-check the v0.2 keys still resolve.
        assert "data_sources" in out
        assert "tablixes" in out
        assert "page" in out

    def test_parameter_layout_none_without_block(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert out["parameter_layout"] is None

    def test_parameter_layout_summarises_grid(self, rdl_path):
        from pbirb_mcp.ops.parameters import set_parameter_layout

        set_parameter_layout(
            path=str(rdl_path),
            rows=1,
            columns=2,
            parameter_order=["DateFrom", "DateTo"],
        )
        out = describe_report(path=str(rdl_path))
        layout = out["parameter_layout"]
        assert layout == {
            "rows": 1,
            "columns": 2,
            "cell_count": 2,
            "parameters_count": 2,
            "in_sync": True,
        }

    def test_embedded_images_lists_inventory(self, rdl_path, tmp_path):
        # Create a tiny PNG so add_embedded_image accepts it.
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        png_path = tmp_path / "logo.png"
        png_path.write_bytes(png_bytes)
        from pbirb_mcp.ops.embedded_images import add_embedded_image

        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        out = describe_report(path=str(rdl_path))
        assert len(out["embedded_images"]) == 1
        entry = out["embedded_images"][0]
        assert entry["name"] == "Logo"
        assert entry["mime_type"] == "image/png"
        assert entry["byte_size"] == len(png_bytes)

    def test_embedded_images_empty_when_no_block(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert out["embedded_images"] == []

    def test_dataset_query_parameters_lists_bindings(self, rdl_path):
        from pbirb_mcp.ops.dataset import add_query_parameter

        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFromParam",
            value_expression="=Parameters!DateFrom.Value",
        )
        out = describe_report(path=str(rdl_path))
        assert len(out["dataset_query_parameters"]) == 1
        qp = out["dataset_query_parameters"][0]
        assert qp == {
            "dataset": "MainDataset",
            "name": "DateFromParam",
            "value": "=Parameters!DateFrom.Value",
        }

    def test_designer_state_present_false_by_default(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert out["designer_state_present"] is False

    def test_designer_state_present_true_when_block_exists(self, rdl_path):
        # Inject a DesignerState block and confirm the flag flips.
        from lxml import etree

        from pbirb_mcp.core.document import RDLDocument
        from pbirb_mcp.core.xpath import RDL_NS, find_child, qrd

        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(f".//{{{RDL_NS}}}DataSet")
        query = find_child(ds, "Query")
        designer = etree.SubElement(query, qrd("DesignerState"))
        etree.SubElement(designer, qrd("Statement")).text = "EVALUATE 'Sales'"
        doc.save()
        out = describe_report(path=str(rdl_path))
        assert out["designer_state_present"] is True


# ---- find_textbox_by_value ---------------------------------------------


class TestFindTextboxByValue:
    def test_returns_empty_when_no_match(self, rdl_path):
        out = find_textbox_by_value(path=str(rdl_path), pattern="NoSuchString")
        assert out == []

    def test_finds_literal_match(self, rdl_path):
        out = find_textbox_by_value(path=str(rdl_path), pattern="Product ID")
        # Fixture's HeaderProductID textbox has "Product ID" as its
        # literal value.
        assert len(out) >= 1
        names = {entry["textbox"] for entry in out}
        assert "HeaderProductID" in names
        for entry in out:
            assert entry["region"] in ("Body", "PageHeader", "PageFooter")

    def test_regex_matches(self, rdl_path):
        out = find_textbox_by_value(path=str(rdl_path), pattern=r"^Product")
        names = {entry["textbox"] for entry in out}
        # All header textboxes start with "Product".
        assert "HeaderProductID" in names
        assert "HeaderProductName" in names

    def test_regex_for_parameter_reference(self, rdl_path):
        # Add a body textbox with a parameter ref, then find it.
        from pbirb_mcp.ops.body import add_body_textbox

        add_body_textbox(
            path=str(rdl_path),
            name="ParamEcho",
            text="=Parameters!DateFrom.Value",
            top="3in",
            left="0.5in",
            width="2in",
            height="0.3in",
        )
        out = find_textbox_by_value(
            path=str(rdl_path), pattern=r"Parameters!\w+\.Value"
        )
        names = {entry["textbox"] for entry in out}
        assert "ParamEcho" in names

    def test_invalid_regex_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="invalid regex"):
            find_textbox_by_value(path=str(rdl_path), pattern="(unclosed")


class TestFindTextboxByValueRegistration:
    def test_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "find_textbox_by_value" in names


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
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
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
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        describe = next(t for t in listing if t["name"] == "describe_report")
        assert describe["inputSchema"]["required"] == ["path"]
        assert describe["inputSchema"]["properties"]["path"]["type"] == "string"
