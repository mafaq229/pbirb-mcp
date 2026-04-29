"""Tests for <ReportParametersLayout>/<GridLayoutDefinition>/<CellDefinitions>
sync — Phase 0 commit 4 of v0.3.0.

Source: RAG-Report session feedback bug #3.

The bug class: add_parameter / remove_parameter / rename_parameter (v0.2)
mutated <ReportParameters> but didn't touch <ReportParametersLayout>.
First runtime exposure was Report Builder crashing with "number of
defined parameters is not equal to the number of cell definitions".

The fixture has no layout block, so tests inject one and assert the
sync helpers maintain it correctly.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.parameters import (
    add_parameter,
    remove_parameter,
    rename_parameter,
    sync_parameter_layout,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _inject_layout(
    rdl_path: Path,
    parameter_names: list[str],
    columns_per_row: int = 4,
):
    """Add a <ReportParametersLayout>/<GridLayoutDefinition> block to the
    fixture, populated with one cell per parameter name. Cells lay out
    left-to-right, top-to-bottom in row-major order."""
    doc = RDLDocument.open(rdl_path)
    root = doc.root
    layout = etree.SubElement(root, q("ReportParametersLayout"))
    grid = etree.SubElement(layout, q("GridLayoutDefinition"))
    n_rows = max(1, (len(parameter_names) + columns_per_row - 1) // columns_per_row)
    etree.SubElement(grid, q("NumberOfColumns")).text = str(columns_per_row)
    etree.SubElement(grid, q("NumberOfRows")).text = str(n_rows)
    cells = etree.SubElement(grid, q("CellDefinitions"))
    for i, pname in enumerate(parameter_names):
        cell = etree.SubElement(cells, q("CellDefinition"))
        etree.SubElement(cell, q("ColumnIndex")).text = str(i % columns_per_row)
        etree.SubElement(cell, q("RowIndex")).text = str(i // columns_per_row)
        etree.SubElement(cell, q("ParameterName")).text = pname
    doc.save()


def _layout_cell_names(rdl_path: Path) -> list[str]:
    doc = RDLDocument.open(rdl_path)
    layout = find_child(doc.root, "ReportParametersLayout")
    if layout is None:
        return []
    grid = find_child(layout, "GridLayoutDefinition")
    if grid is None:
        return []
    cells = find_child(grid, "CellDefinitions")
    if cells is None:
        return []
    out: list[str] = []
    for cell in find_children(cells, "CellDefinition"):
        pn = find_child(cell, "ParameterName")
        if pn is not None and pn.text is not None:
            out.append(pn.text)
    return out


def _layout_grid(rdl_path: Path) -> Optional[etree._Element]:
    doc = RDLDocument.open(rdl_path)
    layout = find_child(doc.root, "ReportParametersLayout")
    if layout is None:
        return None
    return find_child(layout, "GridLayoutDefinition")


def _grid_dim(grid: etree._Element, local: str) -> Optional[int]:
    n = find_child(grid, local)
    if n is None or n.text is None:
        return None
    return int(n.text)


# ---- add_parameter sync ---------------------------------------------------


class TestAddParameterLayoutSync:
    def test_layout_absent_no_sync(self, rdl_path):
        # Fixture has no <ReportParametersLayout>. add_parameter is a no-op
        # for the layout — no synthesis.
        result = add_parameter(
            path=str(rdl_path), name="NewParam", type="String"
        )
        assert result["layout_synced"] is False
        # Confirm we didn't synthesise a layout block.
        doc = RDLDocument.open(rdl_path)
        assert find_child(doc.root, "ReportParametersLayout") is None

    def test_layout_present_appends_cell(self, rdl_path):
        _inject_layout(rdl_path, ["DateFrom", "DateTo"], columns_per_row=4)
        result = add_parameter(
            path=str(rdl_path), name="NewParam", type="String"
        )
        assert result["layout_synced"] is True
        assert _layout_cell_names(rdl_path) == ["DateFrom", "DateTo", "NewParam"]

    def test_layout_present_wraps_to_new_row_on_full(self, rdl_path):
        # 4-column grid filled to capacity → new param wraps to row 1.
        _inject_layout(
            rdl_path,
            ["DateFrom", "DateTo", "P3", "P4"],  # 4 cells, fills row 0
            columns_per_row=4,
        )
        # Add the missing P3, P4 first (they're in layout but not in
        # ReportParameters yet, since the fixture only has DateFrom/DateTo).
        # Sync should drop them as orphans.
        sync_parameter_layout(path=str(rdl_path))
        assert _layout_cell_names(rdl_path) == ["DateFrom", "DateTo"]
        # Now refill.
        add_parameter(path=str(rdl_path), name="P3", type="String")
        add_parameter(path=str(rdl_path), name="P4", type="String")
        # All four occupied row 0.
        add_parameter(path=str(rdl_path), name="P5", type="String")
        # P5 should be at (row=1, col=0).
        grid = _layout_grid(rdl_path)
        cells = find_child(grid, "CellDefinitions")
        p5_cell = next(
            c
            for c in find_children(cells, "CellDefinition")
            if find_child(c, "ParameterName").text == "P5"
        )
        assert find_child(p5_cell, "RowIndex").text == "1"
        assert find_child(p5_cell, "ColumnIndex").text == "0"
        # NumberOfRows should be at least 2.
        assert _grid_dim(grid, "NumberOfRows") >= 2


# ---- remove_parameter sync ------------------------------------------------


class TestRemoveParameterLayoutSync:
    def test_layout_present_drops_cell(self, rdl_path):
        _inject_layout(rdl_path, ["DateFrom", "DateTo"], columns_per_row=4)
        result = remove_parameter(path=str(rdl_path), name="DateTo", force=True)
        assert result["layout_synced"] is True
        assert _layout_cell_names(rdl_path) == ["DateFrom"]

    def test_layout_absent_no_sync(self, rdl_path):
        result = remove_parameter(path=str(rdl_path), name="DateTo", force=True)
        assert result["layout_synced"] is False


# ---- rename_parameter sync ------------------------------------------------


class TestRenameParameterLayoutSync:
    def test_layout_cell_renamed(self, rdl_path):
        _inject_layout(rdl_path, ["DateFrom", "DateTo"], columns_per_row=4)
        result = rename_parameter(
            path=str(rdl_path), old_name="DateFrom", new_name="StartDate"
        )
        assert result["layout_cells_rewritten"] == 1
        assert _layout_cell_names(rdl_path) == ["StartDate", "DateTo"]

    def test_no_layout_returns_zero(self, rdl_path):
        result = rename_parameter(
            path=str(rdl_path), old_name="DateFrom", new_name="StartDate"
        )
        assert result["layout_cells_rewritten"] == 0


# ---- standalone sync_parameter_layout ------------------------------------


class TestStandaloneSyncTool:
    def test_repairs_drift(self, rdl_path):
        """Inject a layout that's out of sync (extra cells, missing params)
        and confirm sync_parameter_layout repairs it."""
        # Fixture has DateFrom and DateTo in <ReportParameters>.
        _inject_layout(
            rdl_path,
            ["DateFrom", "DateTo", "Orphan1", "Orphan2"],  # two orphans
            columns_per_row=4,
        )
        result = sync_parameter_layout(path=str(rdl_path))
        assert sorted(result["removed"]) == ["Orphan1", "Orphan2"]
        assert result["added"] == []
        assert _layout_cell_names(rdl_path) == ["DateFrom", "DateTo"]

    def test_appends_missing_cells(self, rdl_path):
        # Two ReportParameters but no layout cells for them.
        _inject_layout(rdl_path, [], columns_per_row=4)
        result = sync_parameter_layout(path=str(rdl_path))
        assert sorted(result["added"]) == ["DateFrom", "DateTo"]
        assert result["removed"] == []
        assert _layout_cell_names(rdl_path) == ["DateFrom", "DateTo"]

    def test_no_op_when_in_sync(self, rdl_path):
        _inject_layout(rdl_path, ["DateFrom", "DateTo"], columns_per_row=4)
        before_bytes = rdl_path.read_bytes()
        result = sync_parameter_layout(path=str(rdl_path))
        assert result == {"added": [], "removed": []}
        # In-sync → file untouched.
        assert rdl_path.read_bytes() == before_bytes

    def test_no_layout_block_no_op(self, rdl_path):
        result = sync_parameter_layout(path=str(rdl_path))
        assert result == {"added": [], "removed": []}


# ---- in_sync invariant after CRUD ---------------------------------------


class TestEndToEndInSyncInvariant:
    """The ReportParameters element count and the layout's CellDefinitions
    count must stay equal after every CRUD op when a layout exists."""

    def _counts(self, rdl_path: Path) -> tuple[int, int]:
        doc = RDLDocument.open(rdl_path)
        params = doc.root.findall(
            f".//{{{RDL_NS}}}ReportParameters/{{{RDL_NS}}}ReportParameter"
        )
        layout = find_child(doc.root, "ReportParametersLayout")
        cell_count = 0
        if layout is not None:
            grid = find_child(layout, "GridLayoutDefinition")
            if grid is not None:
                cells = find_child(grid, "CellDefinitions")
                if cells is not None:
                    cell_count = len(find_children(cells, "CellDefinition"))
        return len(params), cell_count

    def test_invariant_after_add(self, rdl_path):
        _inject_layout(rdl_path, ["DateFrom", "DateTo"], columns_per_row=4)
        add_parameter(path=str(rdl_path), name="X1", type="String")
        add_parameter(path=str(rdl_path), name="X2", type="String")
        params, cells = self._counts(rdl_path)
        assert params == cells == 4

    def test_invariant_after_remove(self, rdl_path):
        _inject_layout(rdl_path, ["DateFrom", "DateTo"], columns_per_row=4)
        remove_parameter(path=str(rdl_path), name="DateFrom", force=True)
        params, cells = self._counts(rdl_path)
        assert params == cells == 1

    def test_invariant_after_rename(self, rdl_path):
        _inject_layout(rdl_path, ["DateFrom", "DateTo"], columns_per_row=4)
        rename_parameter(
            path=str(rdl_path), old_name="DateFrom", new_name="StartDate"
        )
        params, cells = self._counts(rdl_path)
        assert params == cells == 2
        # Names match.
        assert sorted(_layout_cell_names(rdl_path)) == ["DateTo", "StartDate"]


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_sync_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "sync_parameter_layout" in names
