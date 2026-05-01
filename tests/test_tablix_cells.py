"""Tests for set_cell_span.

Per RDL 2016 schema, ``<ColSpan>`` and ``<RowSpan>`` are children of
``<CellContents>`` (not direct children of ``<TablixCell>``). v0.2 placed
them on TablixCell — Report Builder rejected those documents with
``"invalid child element 'ColSpan'"``. v0.3 corrects placement and
includes a migration path for v0.2-written files.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

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


def _cell_contents(tablix, row_index: int, col_index: int):
    return find_child(_cell(tablix, row_index, col_index), "CellContents")


class TestSetCellSpanPlacement:
    """Spans MUST land inside <CellContents>, not directly on <TablixCell>."""

    def test_row_span_lands_inside_cell_contents(self, rdl_path):
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            column_name="ProductID",
            row_span=2,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 1, 0)
        contents = find_child(cell, "CellContents")
        # Correct placement: child of CellContents.
        rs_inside = find_child(contents, "RowSpan")
        assert rs_inside is not None and rs_inside.text == "2"
        # Bug-regression check: NOT a child of TablixCell directly.
        assert find_child(cell, "RowSpan") is None
        assert find_child(cell, "ColSpan") is None

    def test_col_span_lands_inside_cell_contents(self, rdl_path):
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=0,
            column_name="HeaderProductID",
            col_span=3,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 0, 0)
        contents = find_child(cell, "CellContents")
        cs_inside = find_child(contents, "ColSpan")
        assert cs_inside is not None and cs_inside.text == "3"
        assert find_child(cell, "ColSpan") is None

    def test_both_spans_inside_cell_contents(self, rdl_path):
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            column_name="ProductName",
            row_span=2,
            col_span=2,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        contents = _cell_contents(tablix, 1, 1)
        assert find_child(contents, "RowSpan").text == "2"
        assert find_child(contents, "ColSpan").text == "2"
        # Neither lives on the TablixCell directly.
        cell = _cell(tablix, 1, 1)
        assert find_child(cell, "RowSpan") is None
        assert find_child(cell, "ColSpan") is None

    def test_span_order_inside_cell_contents(self, rdl_path):
        """Schema order: report item first, then ColSpan, then RowSpan."""
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            column_name="ProductName",
            row_span=2,
            col_span=2,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        contents = _cell_contents(tablix, 1, 1)
        children_locals = [etree.QName(c).localname for c in contents]
        # Textbox first, then ColSpan, then RowSpan
        assert children_locals[0] == "Textbox"
        col_idx = children_locals.index("ColSpan")
        row_idx = children_locals.index("RowSpan")
        assert col_idx < row_idx

    def test_replaces_existing_span_inside_cell_contents(self, rdl_path):
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
        contents = _cell_contents(tablix, 1, 0)
        spans = [r.text for r in find_children(contents, "RowSpan")]
        assert spans == ["3"]


class TestLegacyV02Migration:
    """A file with v0.2 misplaced spans is repaired on the next set_cell_span."""

    def _inject_legacy_span(self, rdl_path: Path, row: int, col: int, local: str, value: str):
        """Mimic v0.2 behaviour: write ColSpan/RowSpan as a TablixCell child."""
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        cell = _cell(tablix, row, col)
        node = etree.SubElement(cell, q(local))
        node.text = value
        doc.save()

    def test_legacy_col_span_migrated_to_cell_contents(self, rdl_path):
        # Simulate a v0.2-broken file.
        self._inject_legacy_span(rdl_path, row=0, col=0, local="ColSpan", value="2")
        # Re-touch the cell with another span — the migration should run.
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=0,
            column_name="HeaderProductID",
            row_span=2,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 0, 0)
        contents = find_child(cell, "CellContents")
        # Old (legacy) ColSpan moved inside.
        assert find_child(contents, "ColSpan").text == "2"
        # New RowSpan also inside.
        assert find_child(contents, "RowSpan").text == "2"
        # Nothing left on TablixCell.
        assert find_child(cell, "ColSpan") is None
        assert find_child(cell, "RowSpan") is None

    def test_legacy_collision_prefers_legacy_value(self, rdl_path):
        """If both old (TablixCell child) and new (CellContents child) exist,
        the legacy one was the active source-of-truth pre-migration."""
        # Inject both placements. The CellContents one is "stale" (was inert).
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        cell = _cell(tablix, 0, 0)
        contents = find_child(cell, "CellContents")
        # Stale inside-CellContents value (would have been ignored by RB).
        stale = etree.SubElement(contents, q("ColSpan"))
        stale.text = "99"
        # Active (legacy v0.2-style) value.
        legacy = etree.SubElement(cell, q("ColSpan"))
        legacy.text = "2"
        doc.save()

        # Touch a different span on the same cell to trigger migration.
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=0,
            column_name="HeaderProductID",
            row_span=3,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 0, 0)
        contents = find_child(cell, "CellContents")
        # Legacy "2" wins (was active); the stale "99" was discarded.
        assert find_child(contents, "ColSpan").text == "2"
        assert find_child(contents, "RowSpan").text == "3"
        assert find_child(cell, "ColSpan") is None


class TestRejectionsPreserved:
    """Validation behaviour from v0.2 still holds."""

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
