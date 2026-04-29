"""Tests for ``reorder_parameters`` (Phase 4 commit 22).

Deferred from v0.2 ("v0.3+ if needed"). Strict permutation check
prevents accidental partial reorders that would silently lose a
parameter from the report. Layout sync keeps the parameter pane's
<CellDefinition> entries in lockstep so the rendered order matches
the new declaration order.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.parameters import add_parameter, reorder_parameters
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _parameter_names(rdl_path: Path) -> list[str]:
    doc = RDLDocument.open(rdl_path)
    root = doc.root
    return [
        p.get("Name")
        for p in root.iter(f"{{{RDL_NS}}}ReportParameter")
        if p.get("Name") is not None
    ]


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
    return [
        find_child(c, "ParameterName").text
        for c in find_children(cells, "CellDefinition")
    ]


def _inject_layout(rdl_path: Path):
    """Add a <ReportParametersLayout> with one CellDefinition per
    parameter, matching declaration order."""
    doc = RDLDocument.open(rdl_path)
    root = doc.root
    layout = etree.SubElement(root, q("ReportParametersLayout"))
    grid = etree.SubElement(layout, q("GridLayoutDefinition"))
    etree.SubElement(grid, q("NumberOfColumns")).text = "4"
    cells = etree.SubElement(grid, q("CellDefinitions"))
    for i, p in enumerate(root.iter(f"{{{RDL_NS}}}ReportParameter")):
        cell = etree.SubElement(cells, q("CellDefinition"))
        etree.SubElement(cell, q("ColumnIndex")).text = str(i % 4)
        etree.SubElement(cell, q("RowIndex")).text = str(i // 4)
        etree.SubElement(cell, q("ParameterName")).text = p.get("Name")
    etree.SubElement(grid, q("NumberOfRows")).text = "1"
    doc.save()


# ---- happy path ---------------------------------------------------------


class TestReorderParametersHappyPath:
    def test_reorder_swaps_two(self, rdl_path):
        # Fixture has DateFrom, DateTo in that order.
        result = reorder_parameters(
            path=str(rdl_path), names=["DateTo", "DateFrom"]
        )
        assert result["changed"] is True
        assert result["order"] == ["DateTo", "DateFrom"]
        assert _parameter_names(rdl_path) == ["DateTo", "DateFrom"]

    def test_idempotent_when_unchanged(self, rdl_path):
        before = (rdl_path).read_bytes()
        result = reorder_parameters(
            path=str(rdl_path), names=["DateFrom", "DateTo"]
        )
        assert result["changed"] is False
        assert (rdl_path).read_bytes() == before

    def test_reorder_three_params(self, rdl_path):
        add_parameter(path=str(rdl_path), name="Region", type="String")
        # Now have DateFrom, DateTo, Region. Swap to Region, DateFrom, DateTo.
        reorder_parameters(
            path=str(rdl_path),
            names=["Region", "DateFrom", "DateTo"],
        )
        assert _parameter_names(rdl_path) == ["Region", "DateFrom", "DateTo"]

    def test_round_trip_safe(self, rdl_path):
        reorder_parameters(
            path=str(rdl_path), names=["DateTo", "DateFrom"]
        )
        RDLDocument.open(rdl_path).validate()


# ---- permutation enforcement -------------------------------------------


class TestReorderPermutationCheck:
    def test_missing_name_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="permutation"):
            reorder_parameters(
                path=str(rdl_path),
                names=["DateFrom"],  # missing DateTo
            )

    def test_unknown_name_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="Unknown names"):
            reorder_parameters(
                path=str(rdl_path),
                names=["DateFrom", "Ghost"],  # Ghost doesn't exist
            )

    def test_duplicate_name_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="duplicate"):
            reorder_parameters(
                path=str(rdl_path),
                names=["DateFrom", "DateFrom"],
            )

    def test_extra_count_rejected(self, rdl_path):
        # Length mismatch — caught before the unknown-name check.
        with pytest.raises(ValueError, match="permutation"):
            reorder_parameters(
                path=str(rdl_path),
                names=["DateFrom", "DateTo", "DateFrom"],  # 3 entries
            )

    def test_non_list_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="must be a list"):
            reorder_parameters(
                path=str(rdl_path),
                names="DateFrom",  # type: ignore[arg-type]
            )

    def test_missing_report_parameters_rejected(self, tmp_path):
        # Build a fixture with no ReportParameters block.
        from lxml import etree as _etree

        from pbirb_mcp.core.xpath import find_child as _fc

        dst = tmp_path / "noparams.rdl"
        shutil.copy(FIXTURE, dst)
        doc = RDLDocument.open(dst)
        rp = _fc(doc.root, "ReportParameters")
        if rp is not None:
            doc.root.remove(rp)
        doc.save()

        with pytest.raises(ElementNotFoundError, match="no <ReportParameters>"):
            reorder_parameters(path=str(dst), names=["X"])


# ---- layout sync --------------------------------------------------------


class TestReorderLayoutSync:
    def test_layout_cells_reordered_in_lockstep(self, rdl_path):
        _inject_layout(rdl_path)
        # Initial cell order matches declaration order.
        assert _layout_cell_names(rdl_path) == ["DateFrom", "DateTo"]
        reorder_parameters(
            path=str(rdl_path), names=["DateTo", "DateFrom"]
        )
        assert _parameter_names(rdl_path) == ["DateTo", "DateFrom"]
        # CellDefinitions reordered too.
        assert _layout_cell_names(rdl_path) == ["DateTo", "DateFrom"]

    def test_layout_absent_no_op_for_layout(self, rdl_path):
        # Without a layout block, reorder still works on parameters.
        reorder_parameters(
            path=str(rdl_path), names=["DateTo", "DateFrom"]
        )
        assert _parameter_names(rdl_path) == ["DateTo", "DateFrom"]


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_reorder_parameters_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "reorder_parameters" in names
