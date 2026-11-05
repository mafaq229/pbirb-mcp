"""``set_chart_series_grouping`` targets the right member and writes the
Report Builder shape (#77).

* the group lands on the named series' own hierarchy leaf (series match
  leaves by position, :mod:`pbirb_mcp.core.chart_shape`), or on the
  dynamic member it already sits under;
* promoting a static member sets ``<Label>`` to the group expression (or
  ``label_expression``) and adds ``<SortExpressions>`` (``sort``);
* ``replace=True`` keeps ``Group/@Name`` (scope references) and rewrites
  only ``GroupExpressions``; an identical repeat returns ``changed: []``
  and leaves the bytes alone.

Validate and the chart-structure lint stay clean on every output.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import chart_shape
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.chart import insert_chart_from_template, set_chart_series_grouping
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
RICH = FIXTURES / "pbi_chart_rich.rdl"
EXPR = "=Fields!ProductName.Value"


@pytest.fixture
def rich_path(tmp_path: Path) -> Path:
    """``SalesByProduct``: series Amount / Quantity / Total with three
    static members, plus a template chart ``C`` (one static series)."""
    dest = tmp_path / "rich.rdl"
    shutil.copy(RICH, dest)
    insert_chart_from_template(
        str(dest), "C", "MainDataset", "ProductID", "Amount", "7in", "0in", "5in", "3in"
    )
    return dest


def _tree(path: Path) -> etree._ElementTree:
    return etree.parse(str(path))


def _chart(path: Path, name: str = "C") -> etree._Element:
    return _tree(path).getroot().find(f".//{{{RDL_NS}}}Chart[@Name='{name}']")


def _write(tree: etree._ElementTree, path: Path) -> None:
    tree.write(str(path), xml_declaration=True, encoding="utf-8")


def _locals(member: etree._Element) -> list[str]:
    return [etree.QName(c).localname for c in member if isinstance(c.tag, str)]


def _group_exprs(member: etree._Element) -> list[str]:
    exprs = find_child(find_child(member, "Group"), "GroupExpressions")
    return [e.text for e in find_children(exprs, "GroupExpression")]


def _sorts(member: etree._Element) -> list[tuple[str, str]]:
    sorts = find_child(member, "SortExpressions")
    if sorts is None:
        return []
    out = []
    for s in find_children(sorts, "SortExpression"):
        direction = find_child(s, "Direction")
        out.append(
            (find_child(s, "Value").text, direction.text if direction is not None else "Ascending")
        )
    return out


def _assert_clean(path: Path) -> None:
    assert validate_report(str(path))["valid"]
    issues = lint_report(str(path), ["chart-structure"])["issues"]
    assert issues == []
    for name in ("C", "SalesByProduct"):
        assert chart_shape.hierarchy_problems(_chart(path, name)) == []


def _rename_group(path: Path, chart: str, series: str, new_name: str) -> None:
    tree = _tree(path)
    c = tree.getroot().find(f".//{{{RDL_NS}}}Chart[@Name='{chart}']")
    find_child(chart_shape.series_member(c, series), "Group").set("Name", new_name)
    _write(tree, path)


# ---- promotion of a static member --------------------------------------------


class TestPromoteStaticMember:
    def test_label_becomes_group_expression(self, rich_path):
        r = set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert find_child(member, "Label").text == EXPR
        assert r["label"] == EXPR
        assert r["group_name"] == "Amount_Group"
        assert r["changed"] == ["Group", "Label", "SortExpressions"]
        _assert_clean(rich_path)

    def test_report_builder_shape(self, rich_path):
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert _locals(member) == ["Group", "SortExpressions", "Label"]
        assert _group_exprs(member) == [EXPR]
        assert _sorts(member) == [(EXPR, "Ascending")]
        # Ascending is the RDL default: no explicit <Direction>.
        sort = find_child(find_child(member, "SortExpressions"), "SortExpression")
        assert find_child(sort, "Direction") is None

    def test_label_expression_overrides(self, rich_path):
        set_chart_series_grouping(
            str(rich_path),
            "C",
            "Amount",
            group_field="ProductName",
            label_expression='=Fields!ProductName.Value & " sales"',
        )
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert find_child(member, "Label").text == '=Fields!ProductName.Value & " sales"'
        assert _group_exprs(member) == [EXPR]
        _assert_clean(rich_path)

    def test_sort_descending(self, rich_path):
        r = set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductName", sort="Descending"
        )
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert _sorts(member) == [(EXPR, "Descending")]
        assert r["sort"] == [{"value": EXPR, "direction": "Descending"}]
        _assert_clean(rich_path)

    def test_sort_none_writes_no_sort(self, rich_path):
        r = set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductName", sort="None"
        )
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert find_child(member, "SortExpressions") is None
        assert r["sort"] == []
        assert "SortExpressions" not in r["changed"]

    def test_invalid_sort_refused(self, rich_path):
        before = rich_path.read_bytes()
        with pytest.raises(ValueError, match="sort"):
            set_chart_series_grouping(
                str(rich_path), "C", "Amount", group_field="ProductName", sort="Up"
            )
        assert rich_path.read_bytes() == before

    def test_group_name_parameter(self, rich_path):
        r = set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductName", group_name="SeriesGroup1"
        )
        assert r["group_name"] == "SeriesGroup1"
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert find_child(member, "Group").get("Name") == "SeriesGroup1"
        _assert_clean(rich_path)

    def test_group_name_must_be_unique(self, rich_path):
        before = rich_path.read_bytes()
        with pytest.raises(ValueError):
            set_chart_series_grouping(
                str(rich_path), "C", "Amount", group_field="ProductName", group_name="C"
            )
        assert rich_path.read_bytes() == before

    def test_group_name_must_be_identifier(self, rich_path):
        with pytest.raises(ValueError, match="invalid group name"):
            set_chart_series_grouping(
                str(rich_path), "C", "Amount", group_field="ProductName", group_name="bad name"
            )


# ---- target member ------------------------------------------------------------


class TestTargetsNamedSeries:
    def test_third_series_modifies_third_member_only(self, rich_path):
        before = [
            etree.tostring(m)
            for m in chart_shape.series_leaf_members(_chart(rich_path, "SalesByProduct"))
        ]
        r = set_chart_series_grouping(
            str(rich_path), "SalesByProduct", "Total", group_field="ProductName"
        )
        chart = _chart(rich_path, "SalesByProduct")
        leaves = chart_shape.series_leaf_members(chart)
        assert [etree.tostring(m) for m in leaves[:2]] == before[:2]
        assert find_child(leaves[2], "Group").get("Name") == "Total_Group"
        assert find_child(leaves[2], "Label").text == EXPR
        assert chart_shape.member_series(chart, leaves[2]).get("Name") == "Total"
        assert r["group_name"] == "Total_Group"
        _assert_clean(rich_path)

    def test_series_under_dynamic_parent_targets_parent(self, rich_path):
        # Nest Amount and Quantity under one dynamic parent member.
        tree = _tree(rich_path)
        chart = tree.getroot().find(f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']")
        root = chart_shape.series_hierarchy_members(chart)
        amount, quantity, _total = find_children(root, "ChartMember")
        parent = etree.Element(q("ChartMember"))
        group = etree.SubElement(parent, q("Group"), Name="SeriesGroup1")
        etree.SubElement(
            etree.SubElement(group, q("GroupExpressions")), q("GroupExpression")
        ).text = "=Fields!ProductID.Value"
        children = etree.SubElement(parent, q("ChartMembers"))
        etree.SubElement(parent, q("Label")).text = "=Fields!ProductID.Value"
        amount.addprevious(parent)
        children.append(amount)
        children.append(quantity)
        _write(tree, rich_path)
        _assert_clean(rich_path)

        r = set_chart_series_grouping(
            str(rich_path), "SalesByProduct", "Quantity", group_field="ProductName", replace=True
        )
        chart = _chart(rich_path, "SalesByProduct")
        top = find_children(chart_shape.series_hierarchy_members(chart), "ChartMember")
        assert r["group_name"] == "SeriesGroup1"
        assert _group_exprs(top[0]) == [EXPR]
        assert find_child(top[0], "Label").text == EXPR  # mirrored the old expression
        # The static leaves keep their own labels and stay static.
        leaves = chart_shape.series_leaf_members(chart)
        assert [find_child(m, "Label").text for m in leaves] == ["Amount", "Quantity", "Total"]
        assert all(find_child(m, "Group") is None for m in leaves)
        _assert_clean(rich_path)

    def test_legacy_missing_members_repaired(self, rich_path):
        # Pre-#39 shape: 3 series, only the first one has a member.
        tree = _tree(rich_path)
        chart = tree.getroot().find(f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']")
        root = chart_shape.series_hierarchy_members(chart)
        for m in find_children(root, "ChartMember")[1:]:
            root.remove(m)
        _write(tree, rich_path)

        r = set_chart_series_grouping(
            str(rich_path), "SalesByProduct", "Total", group_field="ProductName"
        )
        assert r["repaired_members"] == ["Quantity", "Total"]
        assert "ChartSeriesHierarchy" in r["changed"]
        chart = _chart(rich_path, "SalesByProduct")
        leaves = chart_shape.series_leaf_members(chart)
        assert find_child(leaves[0], "Group") is None
        assert find_child(leaves[0], "Label").text == "Amount"
        assert find_child(leaves[2], "Group").get("Name") == "Total_Group"
        _assert_clean(rich_path)

    def test_extra_members_refused(self, rich_path):
        tree = _tree(rich_path)
        chart = tree.getroot().find(f".//{{{RDL_NS}}}Chart[@Name='C']")
        root = chart_shape.series_hierarchy_members(chart)
        etree.SubElement(etree.SubElement(root, q("ChartMember")), q("Label")).text = "Stale"
        _write(tree, rich_path)
        before = rich_path.read_bytes()
        with pytest.raises(ValueError, match="cannot be identified"):
            set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        assert rich_path.read_bytes() == before


# ---- replace / idempotency ----------------------------------------------------


class TestReplaceKeepsGroup:
    def test_replace_keeps_report_builder_group_name(self, rich_path):
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        _rename_group(rich_path, "C", "Amount", "SeriesGroup1")
        r = set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductID", replace=True
        )
        assert r["group_name"] == "SeriesGroup1"
        assert r["changed"] == ["GroupExpressions", "Label", "SortExpressions"]
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert find_child(member, "Group").get("Name") == "SeriesGroup1"
        assert _group_exprs(member) == ["=Fields!ProductID.Value"]
        assert find_child(member, "Label").text == "=Fields!ProductID.Value"
        assert _sorts(member) == [("=Fields!ProductID.Value", "Ascending")]
        _assert_clean(rich_path)

    def test_identical_replace_is_noop(self, rich_path):
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        _rename_group(rich_path, "C", "Amount", "SeriesGroup1")
        before = rich_path.read_bytes()
        r = set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductName", replace=True
        )
        assert r["changed"] == []
        assert r["group_name"] == "SeriesGroup1"
        assert rich_path.read_bytes() == before

    def test_identical_repeat_without_replace_is_noop(self, rich_path):
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        before = rich_path.read_bytes()
        r = set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        assert r["changed"] == []
        assert rich_path.read_bytes() == before

    def test_different_expression_without_replace_refused(self, rich_path):
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        before = rich_path.read_bytes()
        with pytest.raises(ValueError, match="replace=True"):
            set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductID")
        assert rich_path.read_bytes() == before

    def test_replace_keeps_other_group_children(self, rich_path):
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        tree = _tree(rich_path)
        c = tree.getroot().find(f".//{{{RDL_NS}}}Chart[@Name='C']")
        group = find_child(chart_shape.series_member(c, "Amount"), "Group")
        filters = etree.SubElement(group, q("Filters"))
        f = etree.SubElement(filters, q("Filter"))
        etree.SubElement(f, q("FilterExpression")).text = "=Fields!Amount.Value"
        etree.SubElement(f, q("Operator")).text = "GreaterThan"
        etree.SubElement(etree.SubElement(f, q("FilterValues")), q("FilterValue")).text = "0"
        _write(tree, rich_path)
        set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductID", replace=True
        )
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert find_child(find_child(member, "Group"), "Filters") is not None
        _assert_clean(rich_path)

    def test_replace_fixes_constant_label_of_older_output(self, rich_path):
        # Earlier versions left the static label on the dynamic member.
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        tree = _tree(rich_path)
        c = tree.getroot().find(f".//{{{RDL_NS}}}Chart[@Name='C']")
        find_child(chart_shape.series_member(c, "Amount"), "Label").text = "Amount"
        _write(tree, rich_path)
        r = set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductName", replace=True
        )
        assert r["changed"] == ["Label"]
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert find_child(member, "Label").text == EXPR

    def test_replace_keeps_custom_label_expression(self, rich_path):
        custom = '=Fields!ProductName.Value & "!"'
        set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductName", label_expression=custom
        )
        set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductID", replace=True
        )
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert find_child(member, "Label").text == custom

    def test_replace_keeps_unrelated_sort(self, rich_path):
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        tree = _tree(rich_path)
        c = tree.getroot().find(f".//{{{RDL_NS}}}Chart[@Name='C']")
        member = chart_shape.series_member(c, "Amount")
        value = find_child(
            find_child(find_child(member, "SortExpressions"), "SortExpression"), "Value"
        )
        value.text = "=Sum(Fields!Amount.Value)"
        _write(tree, rich_path)
        set_chart_series_grouping(
            str(rich_path), "C", "Amount", group_field="ProductID", replace=True
        )
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert _sorts(member) == [("=Sum(Fields!Amount.Value)", "Ascending")]

    def test_replace_explicit_sort(self, rich_path):
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        r = set_chart_series_grouping(
            str(rich_path),
            "C",
            "Amount",
            group_field="ProductName",
            sort="Descending",
            replace=True,
        )
        assert r["changed"] == ["SortExpressions"]
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert _sorts(member) == [(EXPR, "Descending")]
        _assert_clean(rich_path)

    def test_replace_with_group_name_renames(self, rich_path):
        set_chart_series_grouping(str(rich_path), "C", "Amount", group_field="ProductName")
        r = set_chart_series_grouping(
            str(rich_path),
            "C",
            "Amount",
            group_field="ProductName",
            group_name="ByProduct",
            replace=True,
        )
        assert r["changed"] == ["Group.Name"]
        assert r["group_name"] == "ByProduct"
        _assert_clean(rich_path)


# ---- tool registration ----------------------------------------------------------


class TestToolSchema:
    def _tool(self):
        srv = MCPServer()
        register_all_tools(srv)
        tools = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        return next(t for t in tools if t["name"] == "set_chart_series_grouping")

    def test_new_params_in_schema(self):
        props = self._tool()["inputSchema"]["properties"]
        assert {"label_expression", "sort", "group_name"} <= set(props)
        assert props["sort"]["enum"] == ["Ascending", "Descending", "None"]

    def test_call_through_server(self, rich_path):
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "set_chart_series_grouping",
                    "arguments": {
                        "path": str(rich_path),
                        "chart_name": "C",
                        "series_name": "Amount",
                        "group_field": "ProductName",
                        "sort": "Descending",
                        "label_expression": None,
                        "group_name": "SeriesGroup1",
                    },
                },
            }
        )
        assert not resp["result"].get("isError"), resp
        member = chart_shape.series_member(_chart(rich_path), "Amount")
        assert find_child(member, "Group").get("Name") == "SeriesGroup1"
        assert _sorts(member) == [(EXPR, "Descending")]

    def test_description_has_no_first_member_claim(self):
        assert "FIRST" not in self._tool()["description"]
