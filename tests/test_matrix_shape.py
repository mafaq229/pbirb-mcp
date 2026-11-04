"""convert_to_matrix / set_tablix_corner build a real matrix (issue #31).

A Report Builder matrix keeps its group captions in ``<TablixHeader>``
cells of the hierarchy members, aggregates its data cells at group scope,
and has a ``<TablixCorner>`` exactly column-header depth x row-header
depth. The documented flow — ``insert_tablix_from_template`` →
``add_row_group`` → ``add_column_group`` → ``convert_to_matrix`` →
``set_tablix_corner`` — must produce that shape, checked with the #29
shape checker (:func:`pbirb_mcp.core.tablix_headers.shape_problems`).
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.tablix_headers import (
    COLUMN,
    ROW,
    corner_shape,
    header_depth,
    shape_problems,
)
from pbirb_mcp.core.tablix_shape import column_leaves, detail_member, row_leaves
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.page import set_body_size
from pbirb_mcp.ops.styling import set_textbox_style
from pbirb_mcp.ops.tablix import add_row_group, convert_to_matrix, set_tablix_corner
from pbirb_mcp.ops.tablix_columns import add_column_group
from pbirb_mcp.ops.tablix_static import add_static_row
from pbirb_mcp.ops.tablix_subtotals import add_subtotal_column
from pbirb_mcp.ops.templates import insert_tablix_from_template
from pbirb_mcp.ops.validate import validate_report, verify_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _patched(tmp_path: Path, old: str, new: str) -> Path:
    text = FIXTURE.read_text(encoding="utf-8")
    assert text.count(old) == 1
    dest = tmp_path / "report.rdl"
    dest.write_text(text.replace(old, new), encoding="utf-8")
    return dest


def _tablix(path: Path, name: str = "MainTable"):
    return RDLDocument.open(path).root.find(f".//{{{RDL_NS}}}Tablix[@Name='{name}']")


def _rows(tablix):
    return find_children(find_child(find_child(tablix, "TablixBody"), "TablixRows"), "TablixRow")


def _columns(tablix):
    body = find_child(tablix, "TablixBody")
    return find_children(find_child(body, "TablixColumns"), "TablixColumn")


def _body_values(tablix) -> list[list[str]]:
    return [[v.text or "" for v in row.iter(q("Value"))] for row in _rows(tablix)]


def _textbox_names(el) -> list[str]:
    return [tb.get("Name") for tb in el.iter(q("Textbox"))]


def _header(member):
    return find_child(member, "TablixHeader")


def _group_member(tablix, name):
    return next(
        m
        for m in tablix.iter(q("TablixMember"))
        if find_child(m, "Group") is not None and find_child(m, "Group").get("Name") == name
    )


def _assert_clean_matrix(path: Path, name: str = "MainTable"):
    tablix = _tablix(path, name)
    assert shape_problems(tablix) == []
    assert len(row_leaves(tablix)) == len(_rows(tablix))
    assert len(column_leaves(tablix)) == len(_columns(tablix))
    assert validate_report(str(path))["valid"]
    assert verify_report(str(path))["valid"]
    return tablix


def _documented_flow(path: Path) -> None:
    add_row_group(str(path), "MainTable", "Region", "=Fields!ProductName.Value")
    add_column_group(str(path), "MainTable", "Date", "=Fields!ProductID.Value")


def _call(server: MCPServer, tool: str, **arguments):
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    result = resp["result"]
    return json.loads(result["content"][0]["text"]), bool(result.get("isError"))


def _corner_cells(tablix):
    corner = find_child(tablix, "TablixCorner")
    rows = find_children(find_child(corner, "TablixCornerRows"), "TablixCornerRow")
    return [find_children(r, "TablixCornerCell") for r in rows]


# ---- the documented flow --------------------------------------------------


class TestDocumentedFlow:
    def test_template_flow_yields_report_builder_matrix(self, rdl_path):
        insert_tablix_from_template(
            str(rdl_path),
            "Matrix",
            "MainDataset",
            ["ProductID", "ProductName", "Amount"],
            "2in",
            "0.5in",
            "4in",
            "0.5in",
        )
        add_row_group(str(rdl_path), "Matrix", "Region", "=Fields!ProductName.Value")
        add_column_group(str(rdl_path), "Matrix", "Date", "=Fields!ProductID.Value")

        result = convert_to_matrix(str(rdl_path), "Matrix", "Region", "Date")

        tablix = _assert_clean_matrix(rdl_path, "Matrix")
        # One body cell, the aggregated value.
        assert _body_values(tablix) == [["=Sum(Fields!Amount.Value)"]]
        # Row and column TablixHeaders carry the group captions.
        region, date = _group_member(tablix, "Region"), _group_member(tablix, "Date")
        assert [v.text for v in _header(region).iter(q("Value"))] == ["=Fields!ProductName.Value"]
        assert [v.text for v in _header(date).iter(q("Value"))] == ["=Fields!ProductID.Value"]
        # Region is the row leaf (Report Builder's shape), no detail group.
        assert detail_member(tablix) is None
        assert row_leaves(tablix) == [region]
        # Corner: 1 column-header row x 1 row-header column.
        assert header_depth(tablix, COLUMN) == 1
        assert header_depth(tablix, ROW) == 1
        assert corner_shape(tablix) == (1, [1])
        assert result["corner_textboxes"] == ["Matrix_Corner"]
        assert result["aggregated"] == ["Matrix_Amount"]
        assert result["warnings"] == []

    def test_fixture_flow_result(self, rdl_path):
        _documented_flow(rdl_path)
        result = convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")

        tablix = _assert_clean_matrix(rdl_path)
        assert _body_values(tablix) == [["=Sum(Fields!Amount.Value)"]]
        assert result["changed"] == [
            "caption_columns_removed",
            "caption_rows_removed",
            "details_member_removed",
            "cells_aggregated",
            "row_headers_added",
            "corner_created",
        ]
        # The add_row_group caption textbox moved into the row header
        # (name kept); the ProductName column's width became its Size.
        region = _group_member(tablix, "Region")
        assert result["row_headers"] == ["Region_Header_0"]
        assert _textbox_names(_header(region)) == ["Region_Header_0"]
        assert find_child(_header(region), "Size").text == "2in"
        assert result["removed_rows"] == [0, 1]
        assert result["removed_columns"] == [0, 1]
        assert "HeaderAmount" in result["removed_textboxes"]
        # The data cell kept its name and formatting.
        amount = next(tb for tb in tablix.iter(q("Textbox")) if tb.get("Name") == "Amount")
        assert amount.find(f".//{{{RDL_NS}}}Format").text == "#,0.00"

    def test_second_call_refuses(self, rdl_path):
        _documented_flow(rdl_path)
        convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="already a matrix"):
            convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")
        assert rdl_path.read_bytes() == before

    def test_via_server_with_aggregate(self, rdl_path):
        _documented_flow(rdl_path)
        srv = MCPServer()
        register_all_tools(srv)
        payload, is_error = _call(
            srv,
            "convert_to_matrix",
            path=str(rdl_path),
            tablix_name="MainTable",
            row_group="Region",
            column_group="Date",
            aggregate="Avg",
        )
        assert not is_error, payload
        assert payload["aggregated"] == ["Amount"]
        assert _body_values(_tablix(rdl_path)) == [["=Avg(Fields!Amount.Value)"]]


# ---- the issue's repro: total row, Details --------------------------------


class TestRepro:
    def test_total_row_survives(self, rdl_path):
        add_static_row(str(rdl_path), "MainTable", "TotalRow", ["Total"])
        add_row_group(str(rdl_path), "MainTable", "RG", "=Fields!ProductName.Value")
        add_column_group(str(rdl_path), "MainTable", "CG", "=Fields!ProductID.Value")

        result = convert_to_matrix(str(rdl_path), "MainTable", "RG", "CG")

        tablix = _assert_clean_matrix(rdl_path)
        rows = _rows(tablix)
        # Data row (aggregated) then the total row, both under RG.
        assert len(rows) == 2
        assert "TotalRow" in _textbox_names(rows[1])
        assert "=Sum(Fields!Amount.Value)" in _body_values(tablix)[0]
        # Column 0 (=Fields!ProductID.Value, CG's expression) holds the
        # "Total" caption in the kept total row, so it stays, unaggregated.
        assert _body_values(tablix)[0][0] == "=Fields!ProductID.Value"
        assert result["removed_columns"] == [1]
        assert any("body column 0" in w for w in result["warnings"])
        # Two value columns left: the column titles became column headers.
        assert result["column_headers"] == ["HeaderProductID", "HeaderAmount"]
        assert header_depth(tablix, COLUMN) == 2
        assert corner_shape(tablix) == (2, [1, 1])

    @pytest.mark.parametrize("detail", ["Details", "Details1", "DriverList_Details"])
    def test_detail_group_as_row_group_refused(self, tmp_path, detail):
        path = _patched(tmp_path, '<Group Name="Details" />', f'<Group Name="{detail}" />')
        add_column_group(str(path), "MainTable", "CG", "=1")
        before = path.read_bytes()
        with pytest.raises(ValueError, match="is the detail group"):
            convert_to_matrix(str(path), "MainTable", detail, "CG")
        assert path.read_bytes() == before


# ---- preconditions ---------------------------------------------------------


class TestRefusals:
    def test_row_group_not_enclosing_detail(self, tmp_path):
        # A sibling dynamic group next to the detail group's member.
        path = _patched(
            tmp_path,
            '<TablixMember>\n                  <Group Name="Details" />',
            '<TablixMember><Group Name="Other"><GroupExpressions><GroupExpression>'
            "=Fields!ProductID.Value</GroupExpression></GroupExpressions></Group>"
            "</TablixMember>\n"
            '<TablixMember>\n                  <Group Name="Details" />',
        )
        # Keep rows == leaves: duplicate the detail row for "Other".
        text = path.read_text(encoding="utf-8")
        start = text.index("<TablixRow>", text.index("</TablixRow>"))
        end = text.index("</TablixRow>", start) + len("</TablixRow>")
        row = (
            text[start:end]
            .replace('Name="Product', 'Name="O_Product')
            .replace('Name="Amount"', 'Name="O_Amount"')
        )
        path.write_text(text[:start] + row + text[start:], encoding="utf-8")
        add_column_group(str(path), "MainTable", "CG", "=1")
        before = path.read_bytes()
        with pytest.raises(ValueError, match="does not enclose the detail rows"):
            convert_to_matrix(str(path), "MainTable", "Other", "CG")
        assert path.read_bytes() == before

    def test_unknown_aggregate(self, rdl_path):
        _documented_flow(rdl_path)
        with pytest.raises(ValueError, match="aggregate must be one of"):
            convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date", aggregate="Median")

    def test_column_group_on_row_axis_refused(self, rdl_path):
        _documented_flow(rdl_path)
        with pytest.raises(ValueError, match="column group 'Region' not found"):
            convert_to_matrix(str(rdl_path), "MainTable", "Region", "Region")

    def test_no_value_column(self, tmp_path):
        # Every column shows a group expression: nothing to aggregate.
        path = _patched(
            tmp_path,
            "<Value>=Fields!Amount.Value</Value>",
            "<Value>=Fields!ProductID.Value</Value>",
        )
        _documented_flow(path)
        before = path.read_bytes()
        with pytest.raises(ValueError, match="no value to aggregate"):
            convert_to_matrix(str(path), "MainTable", "Region", "Date")
        assert path.read_bytes() == before

    def test_inconsistent_tablix(self, rdl_path):
        _documented_flow(rdl_path)
        text = rdl_path.read_text(encoding="utf-8")
        rdl_path.write_text(text.replace("<TablixMember />", "", 1), encoding="utf-8")
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="inconsistent tablix"):
            convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")
        assert rdl_path.read_bytes() == before


# ---- aggregation of the data cells ----------------------------------------


class TestAggregation:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("=Fields!Amount.Value", "=Sum(Fields!Amount.Value)"),
            ("= Fields!Amount.Value * 2 ", "=Sum(Fields!Amount.Value * 2)"),
            (
                "=(Fields!Amount.Value - Fields!ProductID.Value) / 100",
                "=Sum((Fields!Amount.Value - Fields!ProductID.Value) / 100)",
            ),
            # Already aggregated: never wrapped twice.
            ("=Sum(Fields!Amount.Value)", "=Sum(Fields!Amount.Value)"),
            ("=Fields!Amount.Value / Sum(Fields!Amount.Value)", None),
            ("=Max(Fields!Amount.Value) + 1", None),
            # No field reference / a literal: unchanged.
            ("=Parameters!DateFrom.Value", None),  # a declared parameter (#47)
            ("n/a", None),
        ],
    )
    def test_rewrites(self, tmp_path, value, expected):
        path = _patched(tmp_path, "<Value>=Fields!Amount.Value</Value>", f"<Value>{value}</Value>")
        _documented_flow(path)
        result = convert_to_matrix(str(path), "MainTable", "Region", "Date")
        assert _body_values(_tablix(path)) == [[expected or value]]
        assert result["warnings"] == []
        _assert_clean_matrix(path)

    @pytest.mark.parametrize(
        "value",
        [
            '=Format(Fields!Amount.Value, "C")',
            "=Fields!Amount.IsMissing",
            "=IIf(Fields!Amount.Value > 0, Fields!Amount.Value, 0)",
        ],
    )
    def test_complex_expression_left_with_warning(self, tmp_path, value):
        path = _patched(
            tmp_path,
            "<Value>=Fields!Amount.Value</Value>",
            f"<Value>{value.replace('&', '&amp;').replace('<', '&lt;')}</Value>",
        )
        _documented_flow(path)
        result = convert_to_matrix(str(path), "MainTable", "Region", "Date")
        assert _body_values(_tablix(path)) == [[value]]
        assert result["aggregated"] == []
        assert len(result["warnings"]) == 1 and "aggregate it by hand" in result["warnings"][0]

    def test_string_field_warns(self, rdl_path):
        # Group rows by ProductID: ProductName (System.String) is a value.
        add_row_group(str(rdl_path), "MainTable", "ByID", "=Fields!ProductID.Value")
        add_column_group(str(rdl_path), "MainTable", "All", "=1")
        result = convert_to_matrix(str(rdl_path), "MainTable", "ByID", "All")
        assert result["aggregated"] == ["ProductName", "Amount"]
        assert len(result["warnings"]) == 1
        assert "string field 'ProductName'" in result["warnings"][0]
        _assert_clean_matrix(rdl_path)

    def test_aggregate_is_case_insensitive(self, rdl_path):
        _documented_flow(rdl_path)
        convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date", aggregate="countdistinct")
        assert _body_values(_tablix(rdl_path)) == [["=CountDistinct(Fields!Amount.Value)"]]


# ---- other shapes ----------------------------------------------------------


class TestShapes:
    def test_two_value_columns_get_column_headers(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_column_group(str(rdl_path), "MainTable", "All", "=1")
        result = convert_to_matrix(str(rdl_path), "MainTable", "Region", "All")

        tablix = _assert_clean_matrix(rdl_path)
        assert _body_values(tablix) == [
            ["=Sum(Fields!ProductID.Value)", "=Sum(Fields!Amount.Value)"]
        ]
        # The column titles moved into headers of the static column
        # members under the group: a second column-header level.
        leaves = column_leaves(tablix)
        assert [_textbox_names(_header(leaf)) for leaf in leaves] == [
            ["HeaderProductID"],
            ["HeaderAmount"],
        ]
        assert result["column_headers"] == ["HeaderProductID", "HeaderAmount"]
        assert corner_shape(tablix) == (2, [1, 1])

    def test_nested_row_groups_each_get_a_header(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "Inner", "=Fields!ProductName.Value")
        add_row_group(str(rdl_path), "MainTable", "Outer", "=Fields!ProductID.Value")
        add_column_group(str(rdl_path), "MainTable", "All", "=1")
        result = convert_to_matrix(str(rdl_path), "MainTable", "Inner", "All")

        tablix = _assert_clean_matrix(rdl_path)
        assert result["row_headers"] == ["Outer_Header_0", "Inner_Header_0"]
        assert header_depth(tablix, ROW) == 2
        assert corner_shape(tablix) == (1, [2])
        assert _body_values(tablix) == [["=Sum(Fields!Amount.Value)"]]
        assert row_leaves(tablix) == [_group_member(tablix, "Inner")]

    def test_outer_group_may_be_named(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "Inner", "=Fields!ProductName.Value")
        add_row_group(str(rdl_path), "MainTable", "Outer", "=Fields!ProductID.Value")
        add_column_group(str(rdl_path), "MainTable", "All", "=1")
        convert_to_matrix(str(rdl_path), "MainTable", "Outer", "All")
        assert header_depth(_assert_clean_matrix(rdl_path), ROW) == 2

    def test_report_builder_group_without_caption_row(self, tmp_path):
        # Report Builder's native shape: Product{Details}, no caption row;
        # the ProductID column shows the group expression. Its textbox
        # moves into the new row header.
        path = _patched(
            tmp_path,
            '<Group Name="Details" />',
            '<Group Name="Product"><GroupExpressions>'
            "<GroupExpression>=Fields!ProductID.Value</GroupExpression>"
            "</GroupExpressions></Group>"
            '<TablixMembers><TablixMember><Group Name="Details" />'
            "</TablixMember></TablixMembers>",
        )
        add_column_group(str(path), "MainTable", "All", "=1")
        result = convert_to_matrix(str(path), "MainTable", "Product", "All")

        tablix = _assert_clean_matrix(path)
        product = _group_member(tablix, "Product")
        assert result["row_headers"] == ["ProductID"]
        assert _textbox_names(_header(product)) == ["ProductID"]
        assert find_child(_header(product), "Size").text == "1in"
        assert row_leaves(tablix) == [product]

    def test_collapsed_detail_visibility_warns(self, tmp_path):
        path = _patched(
            tmp_path,
            '<Group Name="Details" />',
            '<Group Name="Product"><GroupExpressions>'
            "<GroupExpression>=Fields!ProductID.Value</GroupExpression>"
            "</GroupExpressions></Group>"
            '<TablixMembers><TablixMember><Group Name="Details" />'
            "<Visibility><Hidden>true</Hidden></Visibility>"
            "</TablixMember></TablixMembers>",
        )
        add_column_group(str(path), "MainTable", "All", "=1")
        result = convert_to_matrix(str(path), "MainTable", "Product", "All")
        assert any("Visibility" in w for w in result["warnings"])
        _assert_clean_matrix(path)

    def test_round_trip_through_report_builder_order(self, rdl_path):
        _documented_flow(rdl_path)
        convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")
        tablix = _tablix(rdl_path)
        region = _group_member(tablix, "Region")
        children = [c.tag.split("}")[1] for c in region]
        assert children == ["Group", "TablixHeader"]
        tablix_children = [c.tag.split("}")[1] for c in tablix]
        assert tablix_children.index("TablixCorner") < tablix_children.index("TablixBody")


# ---- set_tablix_corner -----------------------------------------------------


class TestSetTablixCorner:
    @pytest.fixture
    def matrix(self, rdl_path):
        _documented_flow(rdl_path)
        convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")
        return rdl_path

    def test_styled_corner_keeps_name_and_style(self, matrix):
        # The issue's repro: an author-styled, renamed corner textbox.
        set_textbox_style(str(matrix), "MainTable_Corner", background_color="Navy")
        matrix.write_text(
            matrix.read_text(encoding="utf-8").replace("MainTable_Corner", "CornerCell"),
            encoding="utf-8",
        )
        result = set_tablix_corner(str(matrix), "MainTable", text="Product")
        assert result["name"] == "CornerCell"
        assert result["changed"] == ["corner_written"]
        tablix = _tablix(matrix)
        corner = find_child(tablix, "TablixCorner")
        assert _textbox_names(corner) == ["CornerCell"]
        assert corner.find(f".//{{{RDL_NS}}}BackgroundColor").text == "Navy"
        assert [v.text for v in corner.iter(q("Value"))] == ["Product"]

        before = matrix.read_bytes()
        again = set_tablix_corner(str(matrix), "MainTable", text="Product")
        assert again["changed"] == []
        assert again["name"] == "CornerCell"
        assert matrix.read_bytes() == before

    def test_expression_then_text_edit_the_same_cell(self, matrix):
        set_tablix_corner(str(matrix), "MainTable", expression="=Parameters!P.Value")
        result = set_tablix_corner(str(matrix), "MainTable", text="Type")
        assert result["changed"] == ["corner_written"]
        corner = find_child(_tablix(matrix), "TablixCorner")
        assert [v.text for v in corner.iter(q("Value"))] == ["Type"]
        _assert_clean_matrix(matrix)

    def test_extra_runs_and_paragraphs_collapse(self, matrix):
        doc = RDLDocument.open(matrix)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        textbox = find_child(find_child(_corner_cells(tablix)[0][0], "CellContents"), "Textbox")
        paragraphs = find_child(textbox, "Paragraphs")
        paragraph = find_child(paragraphs, "Paragraph")
        runs = find_child(paragraph, "TextRuns")
        runs.append(copy.deepcopy(find_child(runs, "TextRun")))
        paragraphs.append(copy.deepcopy(paragraph))
        doc.save()

        result = set_tablix_corner(str(matrix), "MainTable", text="Type")
        assert result["changed"] == ["corner_written"]
        corner = find_child(_tablix(matrix), "TablixCorner")
        assert [v.text for v in corner.iter(q("Value"))] == ["Type"]
        assert len(list(corner.iter(q("Paragraph")))) == 1
        _assert_clean_matrix(matrix)

    def test_refuses_without_row_headers(self, rdl_path):
        # The old flow (no convert_to_matrix): column headers, no row
        # headers — a corner there is a shape_problems defect.
        _documented_flow(rdl_path)
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="1 column-header row.*0 row-header column"):
            set_tablix_corner(str(rdl_path), "MainTable", text="Type")
        assert rdl_path.read_bytes() == before

    def test_refuses_without_column_headers(self, tmp_path):
        # Row headers (a Report Builder table with a row group) but no
        # column headers: no corner either.
        header = (
            '<TablixHeader><Size>1in</Size><CellContents><Textbox Name="PH">'
            "<Paragraphs><Paragraph><TextRuns><TextRun><Value /></TextRun></TextRuns>"
            "</Paragraph></Paragraphs></Textbox></CellContents></TablixHeader>"
        )
        path = _patched(
            tmp_path,
            '<Group Name="Details" />',
            '<Group Name="Product"><GroupExpressions>'
            "<GroupExpression>=Fields!ProductID.Value</GroupExpression>"
            f"</GroupExpressions></Group>{header}"
            '<TablixMembers><TablixMember><Group Name="Details" />'
            "</TablixMember></TablixMembers>",
        )
        before = path.read_bytes()
        with pytest.raises(ValueError, match="0 column-header row"):
            set_tablix_corner(str(path), "MainTable", text="Type")
        assert path.read_bytes() == before

    def _nested(self, path):
        add_row_group(str(path), "MainTable", "Inner", "=Fields!ProductName.Value")
        add_row_group(str(path), "MainTable", "Outer", "=Fields!ProductID.Value")
        add_column_group(str(path), "MainTable", "All", "=1")
        convert_to_matrix(str(path), "MainTable", "Inner", "All")

    def test_addresses_cells_by_row_and_col(self, rdl_path):
        self._nested(rdl_path)
        result = set_tablix_corner(str(rdl_path), "MainTable", text="Name", row=0, col=1)
        assert result["name"] == "MainTable_Corner1"
        assert (result["row"], result["col"]) == (0, 1)
        cells = _corner_cells(_tablix(rdl_path))
        assert [[v.text or "" for v in c.iter(q("Value"))] for c in cells[0]] == [[""], ["Name"]]
        with pytest.raises(ValueError, match="outside"):
            set_tablix_corner(str(rdl_path), "MainTable", text="x", row=1)
        with pytest.raises(ValueError, match="non-negative integer"):
            set_tablix_corner(str(rdl_path), "MainTable", text="x", col=-1)

    def test_span_covered_cell_refused(self, rdl_path):
        self._nested(rdl_path)
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        first, second = _corner_cells(tablix)[0]
        etree.SubElement(find_child(first, "CellContents"), q("ColSpan")).text = "2"
        second.remove(find_child(second, "CellContents"))
        doc.save()
        assert shape_problems(_tablix(rdl_path)) == []
        with pytest.raises(ValueError, match="ColSpan of cell"):
            set_tablix_corner(str(rdl_path), "MainTable", text="x", col=1)
        assert set_tablix_corner(str(rdl_path), "MainTable", text="x", col=0)["changed"]

    def test_misshapen_corner_is_resized_not_replaced(self, rdl_path):
        # Two value columns -> 2 column-header rows. With a corner row
        # removed by hand the call restores the shape and keeps the
        # surviving cell.
        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_column_group(str(rdl_path), "MainTable", "All", "=1")
        convert_to_matrix(str(rdl_path), "MainTable", "Region", "All")
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        rows_root = find_child(find_child(tablix, "TablixCorner"), "TablixCornerRows")
        rows_root.remove(find_children(rows_root, "TablixCornerRow")[1])
        doc.save()
        kept = _textbox_names(find_child(_tablix(rdl_path), "TablixCorner"))

        result = set_tablix_corner(str(rdl_path), "MainTable", text="Type", row=1)
        assert result["changed"] == ["corner_resized", "corner_written"]
        tablix = _assert_clean_matrix(rdl_path)
        assert corner_shape(tablix) == (2, [1, 1])
        assert kept[0] in _textbox_names(find_child(tablix, "TablixCorner"))

    def test_via_server(self, matrix):
        srv = MCPServer()
        register_all_tools(srv)
        payload, is_error = _call(
            srv, "set_tablix_corner", path=str(matrix), tablix_name="MainTable", text="T", row=0
        )
        assert not is_error, payload
        assert payload["changed"] == ["corner_written"]


# ---- end to end -------------------------------------------------------------


class TestEndToEnd:
    def test_cookbook_sequence(self, rdl_path):
        _documented_flow(rdl_path)
        convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")
        set_tablix_corner(str(rdl_path), "MainTable", text="Type")
        add_subtotal_column(
            str(rdl_path),
            "MainTable",
            "Date",
            [{"row": 0, "expression": "=Sum(Fields!Amount.Value)"}],
            position="after",
            width="1in",
        )
        set_body_size(str(rdl_path), width="14in", height="10in")

        tablix = _assert_clean_matrix(rdl_path)
        assert _body_values(tablix) == [["=Sum(Fields!Amount.Value)", "=Sum(Fields!Amount.Value)"]]
        assert corner_shape(tablix) == (1, [1])
        corner = find_child(tablix, "TablixCorner")
        assert [v.text for v in corner.iter(q("Value"))] == ["Type"]

    def test_cookbook_batch(self, rdl_path):
        srv = MCPServer()
        register_all_tools(srv)
        ops = [
            {
                "tool": "add_row_group",
                "args": {
                    "tablix_name": "MainTable",
                    "group_name": "Region",
                    "group_expression": "=Fields!ProductName.Value",
                },
            },
            {
                "tool": "add_column_group",
                "args": {
                    "tablix_name": "MainTable",
                    "group_name": "Date",
                    "group_expression": "=Fields!ProductID.Value",
                },
            },
            {
                "tool": "convert_to_matrix",
                "args": {"tablix_name": "MainTable", "row_group": "Region", "column_group": "Date"},
            },
            {"tool": "set_tablix_corner", "args": {"tablix_name": "MainTable", "text": "Type"}},
        ]
        payload, is_error = _call(srv, "apply_edits", path=str(rdl_path), ops=ops)
        assert not is_error, payload
        assert payload["committed"], payload
        _assert_clean_matrix(rdl_path)
