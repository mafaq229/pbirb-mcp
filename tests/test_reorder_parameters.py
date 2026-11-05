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
    return [find_child(c, "ParameterName").text for c in find_children(cells, "CellDefinition")]


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
        result = reorder_parameters(path=str(rdl_path), names=["DateTo", "DateFrom"])
        assert result["changed"] is True
        assert result["order"] == ["DateTo", "DateFrom"]
        assert _parameter_names(rdl_path) == ["DateTo", "DateFrom"]

    def test_idempotent_when_unchanged(self, rdl_path):
        before = (rdl_path).read_bytes()
        result = reorder_parameters(path=str(rdl_path), names=["DateFrom", "DateTo"])
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
        reorder_parameters(path=str(rdl_path), names=["DateTo", "DateFrom"])
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
        reorder_parameters(path=str(rdl_path), names=["DateTo", "DateFrom"])
        assert _parameter_names(rdl_path) == ["DateTo", "DateFrom"]
        # CellDefinitions reordered too.
        assert _layout_cell_names(rdl_path) == ["DateTo", "DateFrom"]

    def test_layout_absent_no_op_for_layout(self, rdl_path):
        # Without a layout block, reorder still works on parameters.
        reorder_parameters(path=str(rdl_path), names=["DateTo", "DateFrom"])
        assert _parameter_names(rdl_path) == ["DateTo", "DateFrom"]


# ---- forward dependencies and layout reflow (#74) ---------------------------

MULTI = Path(__file__).parent / "fixtures" / "pbi_multi_datasource.rdl"


@pytest.fixture
def cascading(tmp_path: Path) -> Path:
    """The issue's repro: Region's available values come from LookupSet,
    whose query parameter reads DateFrom; DateTo defaults from DateFrom."""
    from pbirb_mcp.ops import parameters as P
    from pbirb_mcp.ops.dataset import add_query_parameter

    p = tmp_path / "cascade.rdl"
    shutil.copy(MULTI, p)
    P.add_parameter(str(p), "Region", "String", prompt="Region")
    P.set_parameter_available_values(
        str(p), "Region", "query", query_dataset="LookupSet", query_value_field="LookupKey"
    )
    add_query_parameter(str(p), "LookupSet", "From", "=Parameters!DateFrom.Value")
    P.set_parameter_default_values(str(p), "DateTo", "static", ["=Parameters!DateFrom.Value"])
    P.set_parameter_layout(str(p), 1, 3, ["DateFrom", "DateTo", "Region"])
    return p


def _slots(path: Path) -> dict:
    doc = RDLDocument.open(path)
    return {
        find_child(c, "ParameterName").text: (
            int(find_child(c, "RowIndex").text),
            int(find_child(c, "ColumnIndex").text),
        )
        for c in doc.root.iter(q("CellDefinition"))
    }


class TestForwardDependencyGuard:
    def test_refuses_order_with_forward_dependency(self, cascading):
        before = cascading.read_bytes()
        with pytest.raises(ValueError, match="Region -> DateFrom") as exc:
            reorder_parameters(str(cascading), ["Region", "DateTo", "DateFrom"])
        assert "DateTo -> DateFrom" in str(exc.value)
        assert "LookupSet" in str(exc.value)
        assert cascading.read_bytes() == before

    def test_dependency_through_dataset_filter(self, cascading):
        from pbirb_mcp.ops.dataset import add_dataset_filter

        add_dataset_filter(
            str(cascading),
            "LookupSet",
            "=Fields!LookupKey.Value",
            "Equal",
            ["=Parameters!DateTo.Value"],
        )
        with pytest.raises(ValueError, match="Region -> DateTo"):
            reorder_parameters(str(cascading), ["DateFrom", "Region", "DateTo"])

    def test_allows_an_order_that_keeps_dependencies_first(self, cascading):
        result = reorder_parameters(str(cascading), ["DateFrom", "Region", "DateTo"])
        assert result["changed"] is True
        assert _parameter_names(cascading) == ["DateFrom", "Region", "DateTo"]

    def test_self_dependency_does_not_block(self, rdl_path):
        from pbirb_mcp.ops.parameters import set_parameter_default_values

        set_parameter_default_values(
            str(rdl_path), "DateTo", "static", ["=Parameters!DateTo.Value"]
        )
        reorder_parameters(str(rdl_path), ["DateTo", "DateFrom"])
        assert _parameter_names(rdl_path) == ["DateTo", "DateFrom"]

    def test_lint_and_guard_share_the_graph(self, cascading):
        from pbirb_mcp.ops import expr_walk

        doc = RDLDocument.open(cascading)
        assert expr_walk.parameter_dependencies(doc.root) == {
            "DateFrom": set(),
            "DateTo": {"DateFrom"},
            "Region": {"DateFrom"},
        }


class TestReflowLayout:
    def test_default_keeps_the_pane_positions(self, cascading):
        reorder_parameters(str(cascading), ["DateFrom", "Region", "DateTo"])
        assert _slots(cascading) == {"DateFrom": (0, 0), "DateTo": (0, 1), "Region": (0, 2)}

    def test_reflow_moves_cells_to_the_new_order(self, cascading):
        from pbirb_mcp.ops.validate import validate_report

        result = reorder_parameters(
            str(cascading), ["DateFrom", "Region", "DateTo"], reflow_layout=True
        )
        assert result["layout_reflowed"] is True
        assert _slots(cascading) == {"DateFrom": (0, 0), "Region": (0, 1), "DateTo": (0, 2)}
        assert validate_report(str(cascading))["valid"]
        # Repeat: nothing left to do, file untouched.
        before = cascading.read_bytes()
        again = reorder_parameters(
            str(cascading), ["DateFrom", "Region", "DateTo"], reflow_layout=True
        )
        assert again["changed"] is False and cascading.read_bytes() == before

    def test_reflow_wraps_rows_and_sets_number_of_rows(self, cascading):
        from pbirb_mcp.ops.parameters import set_parameter_layout

        set_parameter_layout(str(cascading), 3, 2, ["DateFrom", "DateTo", "Region"])
        reorder_parameters(str(cascading), ["DateFrom", "Region", "DateTo"], reflow_layout=True)
        assert _slots(cascading) == {"DateFrom": (0, 0), "Region": (0, 1), "DateTo": (1, 0)}
        doc = RDLDocument.open(cascading)
        assert doc.root.find(f".//{{{RDL_NS}}}NumberOfRows").text == "2"

    def test_reflow_with_unchanged_order_reflows_a_scattered_pane(self, cascading):
        doc = RDLDocument.open(cascading)  # swap DateTo and Region on the pane
        for cell in doc.root.iter(q("CellDefinition")):
            col = find_child(cell, "ColumnIndex")
            col.text = {"1": "2", "2": "1"}.get(col.text, col.text)
        doc.save()
        assert _slots(cascading)["DateTo"] == (0, 2)
        result = reorder_parameters(
            str(cascading), ["DateFrom", "DateTo", "Region"], reflow_layout=True
        )
        assert result["changed"] is True
        assert _slots(cascading) == {"DateFrom": (0, 0), "DateTo": (0, 1), "Region": (0, 2)}


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_reorder_parameters_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "reorder_parameters" in names
