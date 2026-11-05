"""Address tablix columns / cells by header text or field (#59)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from lxml import etree

from evals.tool_selection import cases as eval_cases
from pbirb_mcp.core import tablix_headers
from pbirb_mcp.core.columns import (
    AmbiguousColumnError,
    ColumnNotFoundError,
    describe_columns,
    resolve_cell,
    resolve_column,
)
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
from pbirb_mcp.core.tablix_shape import column_leaves
from pbirb_mcp.core.xpath import q
from pbirb_mcp.ops.reader import describe_report
from pbirb_mcp.ops.tablix import add_row_group, convert_to_matrix
from pbirb_mcp.ops.tablix_cells import set_cell_span
from pbirb_mcp.ops.tablix_columns import add_column_group
from pbirb_mcp.ops.templates import insert_tablix_from_template
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "pbi_paginated_minimal.rdl"
EVAL_FIXTURES = Path(eval_cases.__file__).parent / "fixtures"

_SERVER = MCPServer()
register_all_tools(_SERVER)


@pytest.fixture
def rdl(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(MINIMAL, dest)
    return dest


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    dest = tmp_path / "fleet.rdl"
    shutil.copy(EVAL_FIXTURES / eval_cases.FLEET, dest)
    return dest


def _call(tool, **arguments):
    resp = _SERVER.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    result = resp["result"]
    return json.loads(result["content"][0]["text"]), bool(result.get("isError"))


def _ok(tool, **arguments):
    payload, is_error = _call(tool, **arguments)
    assert not is_error, payload
    return payload


def _tablix(path: Path, name: str = "MainTable"):
    return resolve_tablix(RDLDocument.open(str(path)), name)


def _textbox(path: Path, name: str):
    root = etree.parse(str(path)).getroot()
    found = root.findall(f".//{q('Textbox')}[@Name='{name}']")
    assert len(found) == 1
    return found[0]


def _text_align(path: Path, name: str):
    node = _textbox(path, name).find(f".//{q('Paragraph')}/{q('Style')}/{q('TextAlign')}")
    return node.text if node is not None else None


def _width(path: Path, index: int) -> str:
    cols = _tablix(path).find(f"{q('TablixBody')}/{q('TablixColumns')}")
    return cols[index].find(q("Width")).text


# ---- describe_report / describe_columns -------------------------------------


class TestColumnDetails:
    def test_issue_repro_describe_report_lists_columns(self, rdl):
        t = describe_report(str(rdl))["tablixes"][0]
        assert t["column_details"] == [
            {
                "index": 0,
                "header_text": "Product ID",
                "header_cell": "HeaderProductID",
                "data_cell": "ProductID",
                "bound_fields": ["ProductID"],
            },
            {
                "index": 1,
                "header_text": "Product Name",
                "header_cell": "HeaderProductName",
                "data_cell": "ProductName",
                "bound_fields": ["ProductName"],
            },
            {
                "index": 2,
                "header_text": "Amount",
                "header_cell": "HeaderAmount",
                "data_cell": "Amount",
                "bound_fields": ["Amount"],
            },
        ]

    def test_eval_fixture(self, fleet):
        cols = describe_report(str(fleet))["tablixes"][0]["column_details"]
        assert [c["header_text"] for c in cols] == [
            "AssetName",
            "Site",
            "EquipmentType",
            "OperatingHours",
            "IdleHours",
            "Utilization",
        ]
        assert cols[0]["data_cell"] == "FleetTable_AssetName"
        assert cols[0]["header_cell"] == "FleetTable_Header_AssetName"

    def test_row_group_header_row_is_not_the_column_header(self, rdl):
        add_row_group(str(rdl), "MainTable", "ByName", "=Fields!ProductName.Value")
        cols = describe_columns(_tablix(rdl))
        assert [c.header_text for c in cols] == ["Product ID", "Product Name", "Amount"]
        assert [c.data_cell for c in cols] == ["ProductID", "ProductName", "Amount"]
        # The group-header row's =Fields!ProductName.Value binds column 0
        # too, but the column showing only ProductName wins.
        assert cols[0].bound_fields == ["ProductID", "ProductName"]
        assert resolve_column(_tablix(rdl), {"field": "ProductName"}) == 1

    def test_column_group_header_does_not_shadow_body_headers(self, rdl):
        add_column_group(str(rdl), "MainTable", "ByID", "=Fields!ProductID.Value")
        cols = describe_columns(_tablix(rdl))
        assert [c.header_text for c in cols] == ["Product ID", "Product Name", "Amount"]
        assert [c.bound_fields for c in cols] == [["ProductID"], ["ProductName"], ["Amount"]]

    def test_matrix_dynamic_column_uses_group_header(self, rdl):
        add_row_group(str(rdl), "MainTable", "ByName", "=Fields!ProductName.Value")
        add_column_group(str(rdl), "MainTable", "ByID", "=Fields!ProductID.Value")
        convert_to_matrix(str(rdl), "MainTable", "ByName", "ByID")
        (col,) = describe_report(str(rdl))["tablixes"][0]["column_details"]
        assert col == {
            "index": 0,
            "header_text": "=Fields!ProductID.Value",
            "header_cell": "ByID_Header",
            "data_cell": "Amount",
            "bound_fields": ["Amount", "ProductID"],
        }
        assert resolve_column(_tablix(rdl), {"field": "ProductID"}) == 0

    def test_column_hierarchy_tablix_header_wins(self, rdl):
        doc = RDLDocument.open(str(rdl))
        tablix = resolve_tablix(doc, "MainTable")
        for i, leaf in enumerate(column_leaves(tablix)):
            tablix_headers.insert_member_header(
                leaf, tablix_headers.new_header("0.25in", f"ColHdr{i}", f"Caption {i}")
            )
        cols = describe_columns(tablix)
        assert [c.header_text for c in cols] == ["Caption 0", "Caption 1", "Caption 2"]
        assert [c.header_cell for c in cols] == ["ColHdr0", "ColHdr1", "ColHdr2"]
        assert resolve_column(tablix, {"header": "caption 1"}) == 1

    def test_literal_string_expression_header_is_decoded(self, rdl):
        doc = RDLDocument.open(str(rdl))
        tablix = resolve_tablix(doc, "MainTable")
        _value = tablix.find(f".//{q('Textbox')}[@Name='HeaderAmount']//{q('Value')}")
        _value.text = '="Total Amount"'
        assert describe_columns(tablix)[2].header_text == "Total Amount"

    def test_merged_header_heads_both_columns(self, rdl):
        set_cell_span(str(rdl), "MainTable", 0, "HeaderProductID", col_span=2, force=True)
        cols = describe_columns(_tablix(rdl))
        assert cols[0].header_cell == cols[1].header_cell == "HeaderProductID"
        with pytest.raises(AmbiguousColumnError) as exc:
            resolve_column(_tablix(rdl), {"header": "Product ID"})
        assert [c["index"] for c in exc.value.payload["candidates"]] == [0, 1]


# ---- resolve_column -----------------------------------------------------------


class TestResolveColumn:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            (0, 0),
            ("2", 2),
            ("ProductName", 1),
            ("HeaderAmount", 2),
            ({"index": 1}, 1),
            ({"textbox": "Amount"}, 2),
            ({"header": "Product Name"}, 1),
            ({"header": "  product   name "}, 1),
            ({"header": "ProductName"}, 1),  # letters/digits only
            ({"header": "amount"}, 2),
            ({"header": "Name"}, 1),  # containing header, unique
            ({"field": "ProductID"}, 0),
            ({"field": "amount"}, 2),
            ({"field": "Fields!ProductName.Value"}, 1),
            ({"field": "=Fields!Amount.Value"}, 2),
        ],
    )
    def test_resolves(self, rdl, spec, expected):
        assert resolve_column(_tablix(rdl), spec) == expected

    def test_ambiguous_header_lists_candidates(self, rdl):
        with pytest.raises(AmbiguousColumnError) as exc:
            resolve_column(_tablix(rdl), {"header": "Product"})
        assert "Product ID" in str(exc.value) and "Product Name" in str(exc.value)
        assert [c["index"] for c in exc.value.payload["candidates"]] == [0, 1]

    def test_missing_header_lists_columns(self, rdl):
        with pytest.raises(ColumnNotFoundError) as exc:
            resolve_column(_tablix(rdl), {"header": "Asset Name"})
        assert isinstance(exc.value, ElementNotFoundError)
        assert [c["header_text"] for c in exc.value.payload["columns"]] == [
            "Product ID",
            "Product Name",
            "Amount",
        ]

    def test_missing_field(self, rdl):
        with pytest.raises(ColumnNotFoundError, match="AssetID"):
            resolve_column(_tablix(rdl), {"field": "AssetID"})

    def test_ambiguous_field(self, rdl):
        doc = RDLDocument.open(str(rdl))
        tablix = resolve_tablix(doc, "MainTable")
        value = tablix.find(f".//{q('Textbox')}[@Name='Amount']//{q('Value')}")
        value.text = "=Fields!ProductID.Value & Fields!Amount.Value"
        # The column showing only ProductID wins over one that also uses it.
        assert resolve_column(tablix, {"field": "ProductID"}) == 0
        other = tablix.find(f".//{q('Textbox')}[@Name='ProductID']//{q('Value')}")
        other.text = "=Fields!ProductID.Value & Fields!ProductName.Value"
        with pytest.raises(AmbiguousColumnError) as exc:
            resolve_column(tablix, {"field": "ProductID"})
        assert [c["index"] for c in exc.value.payload["candidates"]] == [0, 2]

    def test_out_of_range_index(self, rdl):
        with pytest.raises(IndexError, match="out of range"):
            resolve_column(_tablix(rdl), {"index": 3})

    def test_unknown_textbox(self, rdl):
        with pytest.raises(ElementNotFoundError, match="Nope"):
            resolve_column(_tablix(rdl), "Nope")

    @pytest.mark.parametrize(
        "spec",
        [{}, {"header": "Amount", "field": "Amount"}, {"heading": "Amount"}],
    )
    def test_malformed_spec(self, rdl, spec):
        with pytest.raises(ValueError):
            resolve_column(_tablix(rdl), spec)


class TestResolveCell:
    def test_rows(self, rdl):
        root = RDLDocument.open(str(rdl)).root
        assert resolve_cell(root, {"header": "Amount"}) == "Amount"
        assert resolve_cell(root, {"header": "Amount", "row": "header"}) == "HeaderAmount"
        assert resolve_cell(root, {"field": "ProductID", "row_index": 0}) == "HeaderProductID"
        assert resolve_cell(root, {"tablix": "MainTable", "index": 1}) == "ProductName"

    def test_tablix_required_with_several(self, rdl):
        insert_tablix_from_template(
            str(rdl), "Second", "MainDataset", ["Amount"], "3in", "0in", "1in", "0.5in"
        )
        root = RDLDocument.open(str(rdl)).root
        with pytest.raises(ValueError, match="cell.tablix is required"):
            resolve_cell(root, {"header": "Amount"})
        assert resolve_cell(root, {"tablix": "Second", "field": "Amount"}) == "Second_Amount"
        with pytest.raises(ElementNotFoundError, match="Third"):
            resolve_cell(root, {"tablix": "Third", "field": "Amount"})


# ---- column tools ---------------------------------------------------------------


class TestColumnTools:
    def test_set_column_width_by_header(self, rdl):
        out = _ok(
            "set_column_width",
            path=str(rdl),
            tablix_name="MainTable",
            column_ref={"header": "Product Name"},
            width="2.75in",
        )
        assert out["column_index"] == 1 and out["changed"] is True
        assert _width(rdl, 1) == "2.75in"
        assert validate_report(str(rdl))["valid"]

    def test_set_column_width_legacy_column_still_works(self, rdl):
        _ok("set_column_width", path=str(rdl), tablix_name="MainTable", column=2, width="2in")
        _ok(
            "set_column_width",
            path=str(rdl),
            tablix_name="MainTable",
            column="ProductID",
            width="3in",
        )
        assert _width(rdl, 2) == "2in" and _width(rdl, 0) == "3in"

    def test_set_column_width_needs_one_handle(self, rdl):
        before = rdl.read_bytes()
        payload, is_error = _call(
            "set_column_width", path=str(rdl), tablix_name="MainTable", width="2in"
        )
        assert is_error and "column_ref" in payload["message"]
        payload, is_error = _call(
            "set_column_width",
            path=str(rdl),
            tablix_name="MainTable",
            column=0,
            column_ref={"field": "Amount"},
            width="2in",
        )
        assert is_error and "not both" in payload["message"]
        assert rdl.read_bytes() == before

    def test_ambiguous_error_payload_reaches_the_client(self, rdl):
        payload, is_error = _call(
            "set_column_width",
            path=str(rdl),
            tablix_name="MainTable",
            column_ref={"header": "Product"},
            width="2in",
        )
        assert is_error and payload["error_type"] == "AmbiguousColumnError"
        assert [c["index"] for c in payload["candidates"]] == [0, 1]

    def test_column_ref_schema_is_strict(self, rdl):
        payload, is_error = _call(
            "set_column_width",
            path=str(rdl),
            tablix_name="MainTable",
            column_ref={"heading": "Amount"},
            width="2in",
        )
        assert is_error and payload["error_type"] == "InvalidArguments"

    def test_remove_tablix_column_by_field(self, rdl):
        out = _ok(
            "remove_tablix_column",
            path=str(rdl),
            tablix_name="MainTable",
            column_ref={"field": "ProductName"},
        )
        assert out["position"] == 1 and out["removed_column"] == "ProductName"
        assert set(out["removed_textboxes"]) == {"HeaderProductName", "ProductName"}
        assert validate_report(str(rdl))["valid"]

    def test_remove_tablix_column_needs_one_handle(self, rdl):
        payload, is_error = _call("remove_tablix_column", path=str(rdl), tablix_name="MainTable")
        assert is_error and "column_ref" in payload["message"]

    def test_set_cell_span_by_header(self, rdl):
        out = _ok(
            "set_cell_span",
            path=str(rdl),
            tablix_name="MainTable",
            row_index=0,
            column_ref={"header": "Product ID"},
            col_span=2,
            force=True,
        )
        assert out["column_name"] == "HeaderProductID"
        assert validate_report(str(rdl))["valid"]

    def test_add_subtotal_row_by_header(self, rdl):
        add_row_group(str(rdl), "MainTable", "ByName", "=Fields!ProductName.Value")
        out = _ok(
            "add_subtotal_row",
            path=str(rdl),
            tablix_name="MainTable",
            group_name="ByName",
            aggregates=[
                {"column_ref": {"header": "Amount"}, "expression": "=Sum(Fields!Amount.Value)"}
            ],
        )
        values = [
            "".join(v.text or "" for v in _textbox(rdl, name).iter(q("Value")))
            for name in out["textboxes"]
        ]
        assert values == ["", "", "=Sum(Fields!Amount.Value)"]
        assert validate_report(str(rdl))["valid"]

    def test_add_subtotal_row_entry_needs_one_handle(self, rdl):
        add_row_group(str(rdl), "MainTable", "ByName", "=Fields!ProductName.Value")
        payload, is_error = _call(
            "add_subtotal_row",
            path=str(rdl),
            tablix_name="MainTable",
            group_name="ByName",
            aggregates=[{"expression": "=Sum(Fields!Amount.Value)"}],
        )
        assert is_error and "exactly one of column" in payload["message"]


# ---- textbox tools via `cell` -------------------------------------------------


class TestCellTools:
    def test_set_textbox_style_by_header(self, rdl):
        out = _ok(
            "set_textbox_style",
            path=str(rdl),
            cell={"tablix": "MainTable", "header": "Amount"},
            text_align="Right",
        )
        assert out["textbox"] == "Amount"
        assert _text_align(rdl, "Amount") == "Right"
        assert _text_align(rdl, "HeaderAmount") is None

    def test_set_textbox_style_header_row(self, rdl):
        _ok(
            "set_textbox_style",
            path=str(rdl),
            cell={"header": "Amount", "row": "header"},
            text_align="Right",
        )
        assert _text_align(rdl, "HeaderAmount") == "Right"

    def test_textbox_name_still_works(self, rdl):
        _ok("set_textbox_style", path=str(rdl), textbox_name="ProductID", text_align="Left")
        assert _text_align(rdl, "ProductID") == "Left"

    def test_needs_textbox_name_or_cell(self, rdl):
        payload, is_error = _call("set_textbox_style", path=str(rdl), text_align="Left")
        assert is_error and "textbox_name or cell" in payload["message"]
        payload, is_error = _call(
            "set_textbox_style",
            path=str(rdl),
            textbox_name="ProductID",
            cell={"header": "Amount"},
            text_align="Left",
        )
        assert is_error and "not both" in payload["message"]

    def test_tooltip_value_runs_action(self, rdl):
        _ok(
            "set_textbox_tooltip",
            path=str(rdl),
            cell={"field": "ProductName"},
            text_or_expression="=Fields!ProductID.Value",
        )
        assert _textbox(rdl, "ProductName").find(q("ToolTip")).text == "=Fields!ProductID.Value"
        _ok(
            "set_textbox_value",
            path=str(rdl),
            cell={"header": "Amount", "row": "header"},
            value="Total",
        )
        assert _textbox(rdl, "HeaderAmount").find(f".//{q('Value')}").text == "Total"
        _ok(
            "set_textbox_runs",
            path=str(rdl),
            cell={"header": "Product ID", "row": "header"},
            runs=[{"text": "ID", "font_weight": "Bold"}],
        )
        assert _textbox(rdl, "HeaderProductID").find(f".//{q('Value')}").text == "ID"
        _ok(
            "set_textbox_action",
            path=str(rdl),
            cell={"header": "Product Name"},
            action_type="Hyperlink",
            target_expression="https://example.com",
        )
        assert _textbox(rdl, "ProductName").find(f"{q('ActionInfo')}") is not None
        assert validate_report(str(rdl))["valid"]

    def test_style_bulk_cells(self, rdl):
        out = _ok(
            "set_textbox_style_bulk",
            path=str(rdl),
            cells=[{"header": "Product ID"}, {"field": "Amount"}],
            textbox_names=["HeaderAmount"],
            text_align="Center",
        )
        assert set(out["textboxes"]) == {"HeaderAmount", "ProductID", "Amount"}
        assert all(_text_align(rdl, n) == "Center" for n in ("HeaderAmount", "ProductID", "Amount"))

    def test_style_bulk_needs_something(self, rdl):
        payload, is_error = _call("set_textbox_style_bulk", path=str(rdl), text_align="Center")
        assert is_error

    def test_unmatched_cell_leaves_file_untouched(self, rdl):
        before = rdl.read_bytes()
        payload, is_error = _call(
            "set_textbox_tooltip",
            path=str(rdl),
            cell={"header": "Asset Name"},
            text_or_expression="x",
        )
        assert is_error and payload["error_type"] == "ColumnNotFoundError"
        assert "columns" in payload
        assert rdl.read_bytes() == before

    def test_cell_inside_a_transaction(self, rdl):
        tx = _ok("start_editing_transaction", path=str(rdl))["transaction_id"]
        _ok(
            "insert_tablix_from_template",
            path=str(rdl),
            transaction_id=tx,
            name="Second",
            dataset_name="MainDataset",
            columns=[{"field": "Amount", "header": "Total Amount"}],
            top="3in",
            left="0in",
            width="1in",
            height="0.5in",
        )
        _ok(
            "set_textbox_style",
            path=str(rdl),
            transaction_id=tx,
            cell={"tablix": "Second", "header": "Total Amount"},
            text_align="Right",
        )
        _ok("commit_editing_transaction", transaction_id=tx)
        assert _text_align(rdl, "Second_Amount") == "Right"


# ---- eval cases t04 / s09 / i01 / i02 solved by header / field -------------------


_EVAL_BY_ID = {c.id: c for c in eval_cases.CASES}

_HEADER_SOLUTIONS = {
    "t04": [
        (
            "set_column_width",
            {"tablix_name": "FleetTable", "column_ref": {"header": "Asset Name"}, "width": "2in"},
        )
    ],
    "s09": [
        (
            "set_textbox_style_bulk",
            {
                "cells": [
                    {"tablix": "FleetTable", "header": "Operating Hours"},
                    {"tablix": "FleetTable", "header": "Idle Hours"},
                    {"tablix": "FleetTable", "field": "Utilization"},
                ],
                "text_align": "Center",
            },
        )
    ],
    "i01": [
        (
            "set_textbox_action",
            {
                "cell": {"tablix": "FleetTable", "header": "Asset Name"},
                "action_type": "Drillthrough",
                "target_expression": "AssetDetail",
                "drillthrough_parameters": [{"name": "AssetID", "value": "=Fields!AssetID.Value"}],
            },
        )
    ],
    "i02": [
        (
            "set_textbox_tooltip",
            {
                "cell": {"field": "AssetName"},
                "text_or_expression": "=Fields!AssetID.Value",
            },
        )
    ],
}


@pytest.mark.parametrize("case_id", sorted(_HEADER_SOLUTIONS))
@pytest.mark.parametrize("solution", ["oracle", "by_header"])
def test_eval_case_solvable(fleet, case_id, solution):
    case = _EVAL_BY_ID[case_id]
    calls = case.oracle if solution == "oracle" else _HEADER_SOLUTIONS[case_id]
    for tool, args in calls:
        _ok(tool, path=str(fleet), **args)
    passed, reason = case.check(eval_cases.Rdl(fleet))
    assert passed, reason
    assert validate_report(str(fleet))["valid"]
