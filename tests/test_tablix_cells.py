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
from pbirb_mcp.core.tablix_headers import new_textbox, shape_problems
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.tablix_cells import set_cell_span
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools
from tests._band_fixture import band_row, build_band_report

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
        # Body cells can't span rows (#34); a RowSpan reset to 1 is still
        # written inside CellContents.
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            column_name="ProductID",
            row_span=1,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 1, 0)
        contents = find_child(cell, "CellContents")
        # Correct placement: child of CellContents.
        rs_inside = find_child(contents, "RowSpan")
        assert rs_inside is not None and rs_inside.text == "1"
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
            force=True,
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
            row_span=1,
            col_span=2,
            force=True,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        contents = _cell_contents(tablix, 1, 1)
        assert find_child(contents, "RowSpan").text == "1"
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
            row_span=1,
            col_span=2,
            force=True,
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
            col_span=2,
            force=True,
        )
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            column_name="ProductID",
            col_span=3,
            force=True,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        contents = _cell_contents(tablix, 1, 0)
        spans = [r.text for r in find_children(contents, "ColSpan")]
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
            row_span=1,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 0, 0)
        contents = find_child(cell, "CellContents")
        # Old (legacy) ColSpan moved inside.
        assert find_child(contents, "ColSpan").text == "2"
        # New RowSpan also inside.
        assert find_child(contents, "RowSpan").text == "1"
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
            row_span=1,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cell = _cell(tablix, 0, 0)
        contents = find_child(cell, "CellContents")
        # Legacy "2" wins (was active); the stale "99" was discarded.
        assert find_child(contents, "ColSpan").text == "2"
        assert find_child(contents, "RowSpan").text == "1"
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
                col_span=2,
            )

    def test_row_index_out_of_range_raises(self, rdl_path):
        with pytest.raises(IndexError):
            set_cell_span(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_index=99,
                column_name="HeaderProductID",
                col_span=2,
            )

    def test_round_trip_safe(self, rdl_path):
        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=0,
            column_name="HeaderProductName",
            col_span=2,
            force=True,
        )
        RDLDocument.open(rdl_path).validate()


class TestToolRegistration:
    def test_tool_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "set_cell_span" in server._tools


# ---- #34: span-aware validation ------------------------------------------


def _row_names(path: Path, row_index: int) -> list:
    tablix = _tablix(RDLDocument.open(path), "MainTable")
    rows = find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")
    out = []
    for cell in find_children(rows[row_index].find(q("TablixCells")), "TablixCell"):
        tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
        out.append(tb.get("Name") if tb is not None else None)
    return out


def _problems(path: Path) -> list:
    return shape_problems(_tablix(RDLDocument.open(path), "MainTable"))


class TestSpanAwareValidation:
    def test_overflow_rejected(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="overflows"):
            set_cell_span(str(rdl_path), "MainTable", 0, "HeaderProductID", col_span=99)
        with pytest.raises(ValueError, match="overflows"):
            set_cell_span(str(rdl_path), "MainTable", 0, "HeaderAmount", col_span=2, force=True)
        assert rdl_path.read_bytes() == before

    @pytest.mark.parametrize("bad", [True, False, 2.0, "2"])
    def test_non_integer_span_rejected(self, rdl_path, bad):
        with pytest.raises(ValueError, match="integer"):
            set_cell_span(str(rdl_path), "MainTable", 0, "HeaderProductID", col_span=bad)
        with pytest.raises(ValueError, match="integer"):
            set_cell_span(str(rdl_path), "MainTable", 0, "HeaderProductID", row_span=bad)

    def test_body_row_span_rejected(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="TablixCorner"):
            set_cell_span(str(rdl_path), "MainTable", 0, "HeaderProductID", row_span=3)
        assert rdl_path.read_bytes() == before

    def test_refuses_to_cover_non_empty_cells_without_force(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="HeaderProductName") as exc:
            set_cell_span(str(rdl_path), "MainTable", 0, "HeaderProductID", col_span=3)
        assert "force" in str(exc.value)
        assert rdl_path.read_bytes() == before

    def test_force_clears_covered_cells_and_returns_names(self, rdl_path):
        result = set_cell_span(
            str(rdl_path), "MainTable", 0, "HeaderProductID", col_span=3, force=True
        )
        assert result["cleared"] == ["HeaderProductName", "HeaderAmount"]
        assert _row_names(rdl_path, 0) == ["HeaderProductID", None, None]
        assert _problems(rdl_path) == []
        assert validate_report(str(rdl_path))["valid"]

    def test_blank_covered_cells_cleared_without_force(self, rdl_path):
        from pbirb_mcp.ops.styling import set_textbox_value

        set_textbox_value(str(rdl_path), "HeaderProductName", "")
        result = set_cell_span(str(rdl_path), "MainTable", 0, "HeaderProductID", col_span=2)
        assert result["cleared"] == ["HeaderProductName"]
        assert _problems(rdl_path) == []

    def test_shrinking_recreates_blank_textboxes(self, rdl_path):
        set_cell_span(str(rdl_path), "MainTable", 0, "HeaderProductID", col_span=3, force=True)
        result = set_cell_span(str(rdl_path), "MainTable", 0, "HeaderProductID", col_span=1)
        assert result["created"] == ["MainTable_Cell_0_1", "MainTable_Cell_0_2"]
        assert _row_names(rdl_path, 0) == [
            "HeaderProductID",
            "MainTable_Cell_0_1",
            "MainTable_Cell_0_2",
        ]
        assert _problems(rdl_path) == []
        assert validate_report(str(rdl_path))["valid"]

    def test_covered_cell_cannot_be_the_target(self, tmp_path):
        path = build_band_report(tmp_path / "bands.rdl")
        # Give the covered placeholder a textbox the way the old tool left
        # them, then address it.
        doc = RDLDocument.open(path)
        tablix = _tablix(doc, "MainTable")
        cell = _cell(tablix, 0, 1)
        etree.SubElement(cell, q("CellContents")).append(new_textbox("Stray"))
        doc.save()
        with pytest.raises(ValueError, match="covered by the ColSpan"):
            set_cell_span(str(path), "MainTable", 0, "Stray", col_span=1)

    def test_growing_over_a_span_owner_fills_its_orphaned_tail(self, tmp_path):
        path = build_band_report(tmp_path / "bands.rdl")
        # IVMSBand (cols 0-2) grows to 4 → swallows AFDDBand's owner (col 3);
        # AFDDBand's placeholder (col 4) is left uncovered → blank textbox.
        result = set_cell_span(str(path), "MainTable", 0, "IVMSBand", col_span=4, force=True)
        assert result["cleared"] == ["AFDDBand"]
        assert result["created"] == ["MainTable_Cell_0_4"]
        assert band_row(path, 0) == [
            ("IVMSBand", 4),
            ("-", 1),
            ("-", 1),
            ("-", 1),
            ("MainTable_Cell_0_4", 1),
            ("SoloBand", 1),
        ]
        assert _problems(path) == []
