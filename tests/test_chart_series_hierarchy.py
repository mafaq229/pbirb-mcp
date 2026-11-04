"""ChartSeriesHierarchy stays in sync with ChartSeriesCollection (#39).

RDL matches ``ChartSeries[i]`` to leaf ``ChartMember`` *i* of the series
hierarchy (its ``<Label>`` is the legend text) and ``ChartDataPoint[j]``
to category leaf *j*. ``add_chart_series`` / ``remove_chart_series`` must
keep both counts in step, keep derived-series references intact, and
leave the remaining series on their own labels and groups.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import chart_shape
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.chart import (
    add_chart_series,
    insert_chart_from_template,
    remove_chart_series,
    set_chart_series_grouping,
)
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "pbi_paginated_minimal.rdl"
RICH = FIXTURES / "pbi_chart_rich.rdl"


# ---- helpers ---------------------------------------------------------------


def _tree(path: Path) -> etree._ElementTree:
    return etree.parse(str(path))


def _chart(tree: etree._ElementTree, name: str = "C") -> etree._Element:
    return tree.getroot().find(f".//{{{RDL_NS}}}Chart[@Name='{name}']")


def _labels(chart: etree._Element) -> list[str]:
    return [find_child(m, "Label").text for m in chart_shape.series_leaf_members(chart)]


def _names(chart: etree._Element) -> list[str]:
    return [s.get("Name") for s in chart_shape.series_list(chart)]


def _assert_in_sync(path: Path, name: str = "C") -> etree._Element:
    chart = _chart(_tree(path), name)
    assert chart_shape.hierarchy_problems(chart) == []
    series = chart_shape.series_list(chart)
    assert len(series) == len(chart_shape.series_leaf_members(chart))
    categories = len(chart_shape.category_leaf_members(chart))
    for s in series:
        assert chart_shape.data_point_count(s) == categories
    assert validate_report(str(path))["valid"]
    return chart


def _write(tree: etree._ElementTree, path: Path) -> None:
    tree.write(str(path), xml_declaration=True, encoding="utf-8")


@pytest.fixture
def static_path(tmp_path: Path) -> Path:
    """A template chart ``C`` (one static series ``Amount``)."""
    dest = tmp_path / "report.rdl"
    shutil.copy(MINIMAL, dest)
    insert_chart_from_template(
        str(dest), "C", "MainDataset", "ProductName", "Amount", "3in", "0in", "5in", "3in"
    )
    return dest


@pytest.fixture
def rich_path(tmp_path: Path) -> Path:
    """``pbi_chart_rich.rdl`` forced into the pre-#39 shape every older
    ``add_chart_series`` produced: 3 ChartSeries, only the first one with a
    hierarchy member."""
    dest = tmp_path / "rich.rdl"
    shutil.copy(RICH, dest)
    tree = _tree(dest)
    root = chart_shape.series_hierarchy_members(_chart(tree, "SalesByProduct"))
    for member in find_children(root, "ChartMember")[1:]:
        root.remove(member)
    _write(tree, dest)
    return dest


def _make_nested_dynamic(path: Path) -> None:
    """Turn ``C``'s series hierarchy into ``[G[Amount, Qty], Extra]`` —
    a dynamic series group with two value series under it, plus a static
    sibling."""
    add_chart_series(str(path), "C", "Qty", "ProductID", "Line")
    add_chart_series(str(path), "C", "Extra", "Amount")
    tree = _tree(path)
    chart = _chart(tree)
    root = chart_shape.series_hierarchy_members(chart)
    amount, qty, _extra = find_children(root, "ChartMember")
    group_member = etree.Element(q("ChartMember"))
    group = etree.SubElement(group_member, q("Group"), Name="C_SeriesGroup")
    exprs = etree.SubElement(group, q("GroupExpressions"))
    etree.SubElement(exprs, q("GroupExpression")).text = "=Fields!ProductID.Value"
    inner = etree.SubElement(group_member, q("ChartMembers"))
    etree.SubElement(group_member, q("Label")).text = "=Fields!ProductID.Value"
    root.insert(0, group_member)
    inner.append(amount)
    inner.append(qty)
    _write(tree, path)


def _make_two_categories(path: Path) -> None:
    """Give ``C`` two static category leaves (Q1, Q2) and two data points
    per existing series."""
    tree = _tree(path)
    chart = _chart(tree)
    cat_root = find_child(find_child(chart, "ChartCategoryHierarchy"), "ChartMembers")
    for m in find_children(cat_root, "ChartMember"):
        cat_root.remove(m)
    for label in ("Q1", "Q2"):
        m = etree.SubElement(cat_root, q("ChartMember"))
        etree.SubElement(m, q("Label")).text = label
    for s in chart_shape.series_list(chart):
        points = find_child(s, "ChartDataPoints")
        points.append(copy.deepcopy(find_child(points, "ChartDataPoint")))
    _write(tree, path)


def _add_derived(path: Path, name: str, source: str) -> None:
    tree = _tree(path)
    chart = _chart(tree)
    data = find_child(chart, "ChartData")
    coll = find_child(data, "ChartDerivedSeriesCollection")
    if coll is None:
        coll = etree.SubElement(data, q("ChartDerivedSeriesCollection"))
    d = etree.SubElement(coll, q("ChartDerivedSeries"))
    etree.SubElement(d, q("ChartSeries"), Name=name)
    etree.SubElement(d, q("SourceChartSeriesName")).text = source
    etree.SubElement(d, q("DerivedSeriesFormula")).text = "MovingAverage"
    _write(tree, path)


# ---- add_chart_series --------------------------------------------------------


class TestAddKeepsHierarchyInSync:
    def test_static_chart(self, static_path):
        result = add_chart_series(str(static_path), "C", "Qty", "ProductID", "Line")
        assert result["repaired_members"] == []
        chart = _assert_in_sync(static_path)
        assert _names(chart) == ["Amount", "Qty"]
        assert _labels(chart) == ["Amount", "Qty"]

    def test_added_member_label_is_series_name(self, static_path):
        add_chart_series(str(static_path), "C", "Revenue_2024", "Amount")
        chart = _chart(_tree(static_path))
        member = chart_shape.series_member(chart, "Revenue_2024")
        assert member is not None
        assert find_child(member, "Label").text == "Revenue_2024"
        assert find_child(member, "Group") is None  # static member

    def test_data_points_follow_category_leaves(self, static_path):
        _make_two_categories(static_path)
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        chart = _assert_in_sync(static_path)
        qty = chart_shape.series_list(chart)[-1]
        assert chart_shape.data_point_count(qty) == 2

    def test_dynamic_top_level_member_gets_static_sibling(self, static_path):
        set_chart_series_grouping(str(static_path), "C", "Amount", group_field="ProductID")
        add_chart_series(str(static_path), "C", "Qty", "ProductID", "Line")
        chart = _assert_in_sync(static_path)
        top = find_children(chart_shape.series_hierarchy_members(chart), "ChartMember")
        assert len(top) == 2
        assert find_child(top[0], "Group") is not None  # dynamic, still series 0
        assert find_child(top[1], "Group") is None
        assert find_child(top[1], "Label").text == "Qty"
        assert chart_shape.member_series(chart, top[0]).get("Name") == "Amount"
        assert chart_shape.member_series(chart, top[1]).get("Name") == "Qty"

    def test_nested_dynamic_chart(self, static_path):
        _make_nested_dynamic(static_path)
        add_chart_series(str(static_path), "C", "More", "Amount")
        chart = _assert_in_sync(static_path)
        assert _labels(chart)[1:] == ["Qty", "Extra", "More"]
        groups = [
            [g.get("Name") for g in chart_shape.member_groups(m)]
            for m in chart_shape.series_leaf_members(chart)
        ]
        assert groups == [["C_SeriesGroup"], ["C_SeriesGroup"], [], []]

    def test_repairs_legacy_missing_members(self, rich_path):
        # rich_path is in the legacy shape: 3 ChartSeries, 1 hierarchy member.
        result = add_chart_series(str(rich_path), "SalesByProduct", "Extra", "Amount")
        assert result["repaired_members"] == ["Quantity", "Total"]
        chart = _assert_in_sync(rich_path, "SalesByProduct")
        assert _labels(chart) == ["Amount", "Quantity", "Total", "Extra"]

    def test_refuses_name_of_derived_series(self, static_path):
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        _add_derived(static_path, "AmountMA", "Amount")
        before = static_path.read_bytes()
        with pytest.raises(ValueError, match="derived series named 'AmountMA'"):
            add_chart_series(str(static_path), "C", "AmountMA", "Amount")
        assert static_path.read_bytes() == before

    def test_refuses_when_hierarchy_has_extra_members(self, static_path):
        tree = _tree(static_path)
        root = chart_shape.series_hierarchy_members(_chart(tree))
        etree.SubElement(etree.SubElement(root, q("ChartMember")), q("Label")).text = "Stale"
        _write(tree, static_path)
        before = static_path.read_bytes()
        with pytest.raises(ValueError, match="cannot be identified"):
            add_chart_series(str(static_path), "C", "Qty", "ProductID")
        assert static_path.read_bytes() == before


# ---- remove_chart_series -----------------------------------------------------


class TestRemoveKeepsHierarchyInSync:
    def test_static_chart_removes_member(self, static_path):
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        add_chart_series(str(static_path), "C", "Extra", "Amount")
        result = remove_chart_series(str(static_path), "C", "Qty")
        assert result["remaining"] == ["Amount", "Extra"]
        assert result["pruned_groups"] == []
        chart = _assert_in_sync(static_path)
        assert _labels(chart) == ["Amount", "Extra"]

    def test_first_series_removal_keeps_later_labels(self, static_path):
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        remove_chart_series(str(static_path), "C", "Amount")
        chart = _assert_in_sync(static_path)
        assert _labels(chart) == ["Qty"]

    def test_removing_series_in_shared_group_keeps_group(self, static_path):
        _make_nested_dynamic(static_path)
        result = remove_chart_series(str(static_path), "C", "Amount")
        assert result["pruned_groups"] == []
        chart = _assert_in_sync(static_path)
        leaves = chart_shape.series_leaf_members(chart)
        assert _labels(chart) == ["Qty", "Extra"]
        assert [g.get("Name") for g in chart_shape.member_groups(leaves[0])] == ["C_SeriesGroup"]
        assert chart_shape.member_groups(leaves[1]) == []

    def test_removing_last_series_of_group_prunes_group(self, static_path):
        _make_nested_dynamic(static_path)
        remove_chart_series(str(static_path), "C", "Amount")
        result = remove_chart_series(str(static_path), "C", "Qty")
        assert result["pruned_groups"] == ["C_SeriesGroup"]
        chart = _assert_in_sync(static_path)
        assert _labels(chart) == ["Extra"]
        assert chart.find(f".//{q('Group')}[@Name='C_SeriesGroup']") is None

    def test_static_sibling_of_dynamic_member(self, static_path):
        set_chart_series_grouping(str(static_path), "C", "Amount", group_field="ProductID")
        add_chart_series(str(static_path), "C", "Qty", "ProductID", "Line")
        remove_chart_series(str(static_path), "C", "Qty")
        chart = _assert_in_sync(static_path)
        (leaf,) = chart_shape.series_leaf_members(chart)
        assert find_child(leaf, "Group").get("Name") == "Amount_Group"

    def test_repairs_legacy_missing_members(self, rich_path):
        result = remove_chart_series(str(rich_path), "SalesByProduct", "Quantity")
        assert result["repaired_members"] == ["Quantity", "Total"]
        chart = _assert_in_sync(rich_path, "SalesByProduct")
        assert _labels(chart) == ["Amount", "Total"]

    def test_refuses_when_hierarchy_has_extra_members(self, static_path):
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        tree = _tree(static_path)
        root = chart_shape.series_hierarchy_members(_chart(tree))
        etree.SubElement(etree.SubElement(root, q("ChartMember")), q("Label")).text = "Stale"
        _write(tree, static_path)
        before = static_path.read_bytes()
        with pytest.raises(ValueError, match="cannot be identified"):
            remove_chart_series(str(static_path), "C", "Qty")
        assert static_path.read_bytes() == before


class TestRemoveDerivedSource:
    def test_refuses_and_names_derived_series(self, static_path):
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        _add_derived(static_path, "AmountMA", "Amount")
        before = static_path.read_bytes()
        with pytest.raises(ValueError, match="AmountMA"):
            remove_chart_series(str(static_path), "C", "Amount")
        assert static_path.read_bytes() == before

    def test_cascade_removes_derived_transitively(self, static_path):
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        _add_derived(static_path, "AmountMA", "Amount")
        _add_derived(static_path, "AmountMA2", "AmountMA")
        _add_derived(static_path, "QtyMA", "Qty")
        result = remove_chart_series(str(static_path), "C", "Amount", cascade=True)
        assert result["removed_derived"] == ["AmountMA", "AmountMA2"]
        chart = _assert_in_sync(static_path)
        assert chart_shape.series_names(chart) == ["Qty", "QtyMA"]

    def test_cascade_drops_emptied_collection(self, static_path):
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        _add_derived(static_path, "AmountMA", "Amount")
        remove_chart_series(str(static_path), "C", "Amount", cascade=True)
        chart = _assert_in_sync(static_path)
        data = find_child(chart, "ChartData")
        assert find_child(data, "ChartDerivedSeriesCollection") is None

    def test_unrelated_derived_series_do_not_block(self, static_path):
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        _add_derived(static_path, "QtyMA", "Qty")
        result = remove_chart_series(str(static_path), "C", "Amount")
        assert result["removed_derived"] == []
        _assert_in_sync(static_path)

    def test_derived_series_name_is_not_removable_here(self, static_path):
        add_chart_series(str(static_path), "C", "Qty", "ProductID")
        _add_derived(static_path, "AmountMA", "Amount")
        with pytest.raises(ValueError, match="derived series"):
            remove_chart_series(str(static_path), "C", "AmountMA")


# ---- chart_shape mapping helpers -------------------------------------------------


class TestChartShapeHelpers:
    def test_series_member_by_index_name_and_element(self, static_path):
        _make_nested_dynamic(static_path)
        chart = _chart(_tree(static_path))
        leaves = chart_shape.series_leaf_members(chart)
        series = chart_shape.series_list(chart)
        for i, s in enumerate(series):
            assert chart_shape.series_member(chart, i) is leaves[i]
            assert chart_shape.series_member(chart, s.get("Name")) is leaves[i]
            assert chart_shape.series_member(chart, s) is leaves[i]
            assert chart_shape.member_series(chart, leaves[i]) is s
            assert chart_shape.series_index(chart, s) == i
        assert chart_shape.series_member(chart, "Nope") is None
        assert chart_shape.series_member(chart, 99) is None

    def test_hierarchy_problems_reports_mismatch(self, rich_path):
        chart = _chart(_tree(rich_path), "SalesByProduct")
        problems = chart_shape.hierarchy_problems(chart)
        assert problems == ["3 ChartSeries but 1 leaf ChartMember(s) in ChartSeriesHierarchy"]


# ---- every fixture chart satisfies the invariant ---------------------------------


def _fixture_params():
    params = []
    for path in sorted(FIXTURES.glob("*.rdl")):
        params.append(pytest.param(path, id=path.name))
    return params


@pytest.mark.parametrize("path", _fixture_params())
def test_fixture_charts_satisfy_series_member_invariant(path):
    for chart in etree.parse(str(path)).getroot().iter(q("Chart")):
        assert chart_shape.hierarchy_problems(chart) == [], chart.get("Name")


# ---- tool surface ----------------------------------------------------------------


def _tool(name: str) -> dict:
    srv = MCPServer()
    register_all_tools(srv)
    listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return next(t for t in listing["result"]["tools"] if t["name"] == name)


def test_add_description_mentions_hierarchy_member():
    assert "ChartSeriesHierarchy member" in _tool("add_chart_series")["description"]


def test_remove_description_and_cascade_schema():
    tool = _tool("remove_chart_series")
    assert "ChartSeriesHierarchy leaf member" in tool["description"]
    assert tool["inputSchema"]["properties"]["cascade"]["type"] == "boolean"
