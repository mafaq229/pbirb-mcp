"""Tests for set_cell_span (v0.2 commit 4)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.tablix_cells import set_cell_span
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


def _cell(tablix, row_index: int, col_index: int):
    rows = find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")
    cells = find_children(rows[row_index].find(q("TablixCells")), "TablixCell")
    return cells[col_index]


class TestSetCellSpan:
    def test_writes_row_span(self, rdl_path):
        # Fixture row 1 is the data row; column 0 textbox = ProductID.
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            column_name="ProductID",
            row_span=2,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 1, 0)
        rs = find_child(cell, "RowSpan")
        assert rs is not None and rs.text == "2"
        assert find_child(cell, "ColSpan") is None

    def test_writes_col_span(self, rdl_path):
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=0,
            column_name="HeaderProductID",
            col_span=3,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 0, 0)
        cs = find_child(cell, "ColSpan")
        assert cs is not None and cs.text == "3"

    def test_writes_both_spans(self, rdl_path):
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            column_name="ProductName",
            row_span=2,
            col_span=2,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 1, 1)
        assert find_child(cell, "RowSpan").text == "2"
        assert find_child(cell, "ColSpan").text == "2"

    def test_replaces_existing_span(self, rdl_path):
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            column_name="ProductID",
            row_span=2,
        )
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            column_name="ProductID",
            row_span=3,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 1, 0)
        spans = [r.text for r in find_children(cell, "RowSpan")]
        # Exactly one RowSpan child, with the new value.
        assert spans == ["3"]

    def test_no_args_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            set_cell_span(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_index=0,
                column_name="HeaderProductID",
            )

    def test_zero_span_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            set_cell_span(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_index=0,
                column_name="HeaderProductID",
                row_span=0,
            )

    def test_unknown_textbox_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_cell_span(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_index=0,
                column_name="NoSuch",
                row_span=2,
            )

    def test_row_index_out_of_range_raises(self, rdl_path):
        with pytest.raises(IndexError):
            set_cell_span(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_index=99,
                column_name="HeaderProductID",
                row_span=2,
            )

    def test_round_trip_safe(self, rdl_path):
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=0,
            column_name="HeaderAmount",
            col_span=2,
        )
        RDLDocument.open(rdl_path).validate()


class TestToolRegistration:
    def test_tool_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "set_cell_span" in server._tools
