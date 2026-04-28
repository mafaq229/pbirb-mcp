"""Tests for add_subtotal_row (v0.2 commit 3)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.tablix import add_row_group
from pbirb_mcp.ops.tablix_subtotals import add_subtotal_row
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def rdl_with_region_group(rdl_path):
    add_row_group(
        path=str(rdl_path),
        tablix_name="MainTable",
        group_name="Region",
        group_expression="=Fields!Region.Value",
    )
    return rdl_path


def _tablix(doc: RDLDocument, name: str):
    return doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='{name}']")


def _wrapper_member_count(tablix, group_name: str) -> int:
    for member in tablix.find(q("TablixRowHierarchy")).iter(q("TablixMember")):
        g = find_child(member, "Group")
        if g is not None and g.get("Name") == group_name:
            inner = find_child(member, "TablixMembers")
            return len(list(inner)) if inner is not None else 0
    return 0


def _body_rows(tablix):
    return find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")


def _row_cell_textboxes(row):
    return [
        cell.find(f"{q('CellContents')}/{q('Textbox')}")
        for cell in row.findall(f"{q('TablixCells')}/{q('TablixCell')}")
    ]


def _textrun_value(textbox):
    if textbox is None:
        return None
    val = textbox.find(
        f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
    )
    if val is None:
        return None
    # lxml round-trips empty Value text as None — normalise to "" so blank
    # cells compare equal to the "" literal we emit at write time.
    return val.text if val.text is not None else ""


class TestAddSubtotalRow:
    def test_appends_member_in_wrapper_at_footer(self, rdl_with_region_group):
        tablix_before = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        before = _wrapper_member_count(tablix_before, "Region")

        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[
                {"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"},
            ],
            position="footer",
        )
        tablix = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        assert _wrapper_member_count(tablix, "Region") == before + 1

    def test_appends_body_row_at_end(self, rdl_with_region_group):
        before = len(_body_rows(_tablix(RDLDocument.open(rdl_with_region_group), "MainTable")))
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[
                {"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"},
            ],
            position="footer",
        )
        rows = _body_rows(_tablix(RDLDocument.open(rdl_with_region_group), "MainTable"))
        assert len(rows) == before + 1

    def test_aggregate_lands_in_correct_column(self, rdl_with_region_group):
        # Fixture has data-row textboxes [ProductID, ProductName, Amount].
        # An aggregate keyed on "Amount" should land in column 2.
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[
                {"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"},
            ],
            position="footer",
        )
        tablix = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        rows = _body_rows(tablix)
        last_row = rows[-1]
        textboxes = _row_cell_textboxes(last_row)
        # Column 0 + 1 are blank; column 2 holds the aggregate.
        assert _textrun_value(textboxes[0]) == ""
        assert _textrun_value(textboxes[1]) == ""
        assert _textrun_value(textboxes[2]) == "=Sum(Fields!Amount.Value)"

    def test_header_position_inserts_after_group_header(self, rdl_with_region_group):
        # Body before footer/header: [group header (row 0), header (1), data (2)].
        # header position: new subtotal row goes at index 1.
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[],
            position="header",
        )
        tablix = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        rows = _body_rows(tablix)
        # Find the new (all-blank) row — it should be at index 1.
        textboxes = _row_cell_textboxes(rows[1])
        for tb in textboxes:
            assert _textrun_value(tb) == ""

    def test_invalid_position_raises(self, rdl_with_region_group):
        with pytest.raises(ValueError):
            add_subtotal_row(
                path=str(rdl_with_region_group),
                tablix_name="MainTable",
                group_name="Region",
                aggregates=[],
                position="middle",
            )

    def test_unknown_group_raises(self, rdl_with_region_group):
        with pytest.raises(ElementNotFoundError):
            add_subtotal_row(
                path=str(rdl_with_region_group),
                tablix_name="MainTable",
                group_name="NoSuch",
                aggregates=[],
            )

    def test_unknown_aggregate_column_raises(self, rdl_with_region_group):
        with pytest.raises(ElementNotFoundError):
            add_subtotal_row(
                path=str(rdl_with_region_group),
                tablix_name="MainTable",
                group_name="Region",
                aggregates=[{"column": "NoSuchColumn", "expression": "=Sum(0)"}],
            )

    def test_round_trip_safe(self, rdl_with_region_group):
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        RDLDocument.open(rdl_with_region_group).validate()


class TestToolRegistration:
    def test_tool_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "add_subtotal_row" in server._tools
