"""Tests for add_tablix_column / remove_tablix_column (v0.2 commit 2).

These tools change body shape (one extra TablixColumn + one TablixMember
in the column hierarchy + one extra cell per row). Removal is the inverse
addressed by ``column_name`` — the textbox name in the data row.

Cell-text convention:

* For an n-row tablix where n >= 2: row 0 (header) gets ``header_text``
  (defaulting to ``column_name``); rows 1..n-2 (middle) get blank
  textboxes; row n-1 (data) gets ``expression``.
* For a 1-row tablix: the single row gets ``expression``.
* For a 0-row tablix: only the column + hierarchy member are added.

Textbox names: the data-row cell uses ``column_name`` exactly; other
rows use ``<column_name>_<row_index>`` so report-wide uniqueness holds
provided ``column_name`` itself is unique.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.tablix_columns import (
    add_tablix_column,
    remove_tablix_column,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _tablix(doc: RDLDocument, name: str):
    return doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='{name}']")


def _body_columns(tablix):
    return find_children(tablix.find(f"{q('TablixBody')}/{q('TablixColumns')}"), "TablixColumn")


def _body_rows(tablix):
    return find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")


def _row_cells(row):
    return row.findall(f"{q('TablixCells')}/{q('TablixCell')}")


def _cell_textbox(cell):
    return cell.find(f"{q('CellContents')}/{q('Textbox')}")


def _column_hierarchy_top_members(tablix):
    return list(tablix.find(q("TablixColumnHierarchy")).find(q("TablixMembers")))


# ---- add_tablix_column ----------------------------------------------------


class TestAddTablixColumn:
    def test_appends_column_at_end_by_default(self, rdl_path):
        doc_before = RDLDocument.open(rdl_path)
        cols_before = len(_body_columns(_tablix(doc_before, "MainTable")))
        members_before = len(_column_hierarchy_top_members(_tablix(doc_before, "MainTable")))
        per_row_before = [len(_row_cells(r)) for r in _body_rows(_tablix(doc_before, "MainTable"))]

        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )

        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        assert len(_body_columns(tablix)) == cols_before + 1
        assert len(_column_hierarchy_top_members(tablix)) == members_before + 1
        for i, row in enumerate(_body_rows(tablix)):
            assert len(_row_cells(row)) == per_row_before[i] + 1

    def test_appends_with_default_width_one_inch(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        last_col = _body_columns(tablix)[-1]
        assert find_child(last_col, "Width").text == "1in"

    def test_inserts_at_explicit_position_zero(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
            position=0,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        rows = _body_rows(tablix)
        # Data row (last row) — first cell now holds the new column's textbox.
        first_data_cell = _row_cells(rows[-1])[0]
        tb = _cell_textbox(first_data_cell)
        assert tb.get("Name") == "MainTable_Discount"
        textrun_value = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "=Fields!Discount.Value"

    def test_data_row_holds_expression_with_bare_column_name(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        rows = _body_rows(tablix)
        # Last row (= data row), last cell (= just-added column).
        data_cell = _row_cells(rows[-1])[-1]
        tb = _cell_textbox(data_cell)
        assert tb.get("Name") == "MainTable_Discount"
        textrun_value = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "=Fields!Discount.Value"

    def test_header_row_holds_default_header_text(self, rdl_path):
        # Default header_text is column_name itself, placed as a literal.
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        rows = _body_rows(tablix)
        # First row (header), last cell (= just-added column).
        header_cell = _row_cells(rows[0])[-1]
        tb = _cell_textbox(header_cell)
        # Header textbox name suffix.
        assert tb.get("Name") == "MainTable_Discount_0"
        textrun_value = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "MainTable_Discount"

    def test_header_text_override(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
            header_text="Discount",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        rows = _body_rows(tablix)
        header_cell = _row_cells(rows[0])[-1]
        textrun_value = _cell_textbox(header_cell).find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "Discount"

    def test_explicit_width(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
            width="2.5cm",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        last_col = _body_columns(tablix)[-1]
        assert find_child(last_col, "Width").text == "2.5cm"

    def test_duplicate_column_name_rejected_against_existing_textbox(self, rdl_path):
        # Fixture already has a textbox named "Amount".
        with pytest.raises(ValueError):
            add_tablix_column(
                path=str(rdl_path),
                tablix_name="MainTable",
                column_name="Amount",
                expression="=Fields!Amount.Value",
            )

    def test_position_out_of_range_rejected(self, rdl_path):
        with pytest.raises(IndexError):
            add_tablix_column(
                path=str(rdl_path),
                tablix_name="MainTable",
                column_name="MainTable_Discount",
                expression="=Fields!Discount.Value",
                position=999,
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_tablix_column(
                path=str(rdl_path),
                tablix_name="NoSuch",
                column_name="X",
                expression="=Fields!X.Value",
            )

    def test_round_trip_safe(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        RDLDocument.open(rdl_path).validate()


# ---- remove_tablix_column -------------------------------------------------


class TestRemoveTablixColumn:
    def test_inverts_add_tablix_column(self, rdl_path):
        before = RDLDocument.open(rdl_path)
        cols_before = len(_body_columns(_tablix(before, "MainTable")))
        members_before = len(_column_hierarchy_top_members(_tablix(before, "MainTable")))
        per_row_before = [len(_row_cells(r)) for r in _body_rows(_tablix(before, "MainTable"))]

        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        remove_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
        )

        after = RDLDocument.open(rdl_path)
        tablix = _tablix(after, "MainTable")
        assert len(_body_columns(tablix)) == cols_before
        assert len(_column_hierarchy_top_members(tablix)) == members_before
        for i, row in enumerate(_body_rows(tablix)):
            assert len(_row_cells(row)) == per_row_before[i]

    def test_removes_existing_fixture_column(self, rdl_path):
        # Fixture has an "Amount" data textbox at column index 2.
        before = RDLDocument.open(rdl_path)
        cols_before = len(_body_columns(_tablix(before, "MainTable")))

        remove_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="Amount",
        )

        after = RDLDocument.open(rdl_path)
        tablix = _tablix(after, "MainTable")
        assert len(_body_columns(tablix)) == cols_before - 1
        # No row should still contain a textbox with that name.
        for row in _body_rows(tablix):
            for cell in _row_cells(row):
                tb = _cell_textbox(cell)
                if tb is not None:
                    assert tb.get("Name") != "Amount"

    def test_unknown_column_name_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_tablix_column(
                path=str(rdl_path),
                tablix_name="MainTable",
                column_name="NoSuchColumn",
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_tablix_column(
                path=str(rdl_path),
                tablix_name="NoSuch",
                column_name="X",
            )


# ---- tools.py registration smoke ------------------------------------------


class TestToolRegistration:
    def test_both_tools_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert {"add_tablix_column", "remove_tablix_column"}.issubset(server._tools.keys())
