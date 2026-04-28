"""Tests for add_static_row / add_static_column (v0.2 commit 5)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.tablix_static import add_static_column, add_static_row
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


def _body_rows(tablix):
    return find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")


def _body_cols(tablix):
    return find_children(tablix.find(f"{q('TablixBody')}/{q('TablixColumns')}"), "TablixColumn")


def _row_textboxes(row):
    return [
        c.find(f"{q('CellContents')}/{q('Textbox')}")
        for c in row.findall(f"{q('TablixCells')}/{q('TablixCell')}")
    ]


def _textrun_value(tb):
    if tb is None:
        return None
    v = tb.find(f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}")
    if v is None:
        return None
    return v.text if v.text is not None else ""


# ---- add_static_row ------------------------------------------------------


class TestAddStaticRow:
    def test_appends_row_with_literal_cells(self, rdl_path):
        before = len(_body_rows(_tablix(RDLDocument.open(rdl_path), "MainTable")))
        add_static_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_name="TotalsLabel",
            cells=["Totals", "", ""],
        )
        rows = _body_rows(_tablix(RDLDocument.open(rdl_path), "MainTable"))
        assert len(rows) == before + 1
        last = rows[-1]
        textboxes = _row_textboxes(last)
        assert _textrun_value(textboxes[0]) == "Totals"
        assert _textrun_value(textboxes[1]) == ""

    def test_inserts_at_explicit_position(self, rdl_path):
        add_static_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_name="Banner",
            cells=["Banner", "", ""],
            position=0,
        )
        rows = _body_rows(_tablix(RDLDocument.open(rdl_path), "MainTable"))
        textboxes = _row_textboxes(rows[0])
        assert textboxes[0].get("Name") == "Banner"
        assert _textrun_value(textboxes[0]) == "Banner"

    def test_too_many_cells_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            add_static_row(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_name="X",
                cells=["a", "b", "c", "d"],  # fixture has 3 columns
            )

    def test_duplicate_row_name_rejected(self, rdl_path):
        # "ProductID" already exists in the fixture.
        with pytest.raises(ValueError):
            add_static_row(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_name="ProductID",
                cells=["x"],
            )

    def test_round_trip_safe(self, rdl_path):
        add_static_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_name="TotalsLabel",
            cells=["Totals"],
        )
        RDLDocument.open(rdl_path).validate()


# ---- add_static_column ---------------------------------------------------


class TestAddStaticColumn:
    def test_appends_column(self, rdl_path):
        before = len(_body_cols(_tablix(RDLDocument.open(rdl_path), "MainTable")))
        add_static_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="Notes",
            cells=["Notes", ""],
        )
        cols = _body_cols(_tablix(RDLDocument.open(rdl_path), "MainTable"))
        assert len(cols) == before + 1
        # Last column got our cells; row 0's last cell holds "Notes".
        rows = _body_rows(_tablix(RDLDocument.open(rdl_path), "MainTable"))
        textboxes = _row_textboxes(rows[0])
        assert textboxes[-1].get("Name") == "Notes"
        assert _textrun_value(textboxes[-1]) == "Notes"

    def test_inserts_at_explicit_position(self, rdl_path):
        add_static_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="LeftMargin",
            cells=[" ", " "],
            position=0,
            width="0.5in",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cols = _body_cols(tablix)
        assert find_child(cols[0], "Width").text == "0.5in"
        rows = _body_rows(tablix)
        textboxes = _row_textboxes(rows[0])
        assert textboxes[0].get("Name") == "LeftMargin"

    def test_too_many_cells_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            add_static_column(
                path=str(rdl_path),
                tablix_name="MainTable",
                column_name="X",
                cells=["a", "b", "c"],  # fixture has 2 rows
            )

    def test_duplicate_column_name_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            add_static_column(
                path=str(rdl_path),
                tablix_name="MainTable",
                column_name="Amount",  # already in fixture
                cells=["x"],
            )

    def test_round_trip_safe(self, rdl_path):
        add_static_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="Notes",
            cells=["Notes"],
        )
        RDLDocument.open(rdl_path).validate()


class TestToolRegistration:
    def test_both_tools_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert {"add_static_row", "add_static_column"}.issubset(server._tools.keys())
