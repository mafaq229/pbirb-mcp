"""Column-hierarchy tablix tools — first commit of v0.2.

Mirrors the row-group test pattern in ``test_tablix_groups.py`` for the
column hierarchy. Adding a column group wraps existing column members
under a new outer ``<TablixMember>`` whose ``<TablixHeader>`` holds the
group expression; the body is untouched, so column-hierarchy leaves stay
equal to body columns (#29).
``set_column_group_sort`` and ``set_column_group_visibility`` are thin
wrappers over the existing row-group equivalents — the underlying
helpers are already hierarchy-agnostic, so the new tools mostly add a
hierarchy sanity check that produces a clearer error than the generic
versions when called on the wrong axis.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import names
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.tablix_headers import (
    ensure_corner,
    header_depth,
    new_header,
    shape_problems,
)
from pbirb_mcp.core.tablix_shape import column_leaves, row_leaves
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.reader import get_tablixes
from pbirb_mcp.ops.tablix_cells import set_cell_span
from pbirb_mcp.ops.tablix_columns import (
    add_column_group,
    add_tablix_column,
    remove_column_group,
    remove_tablix_column,
    set_column_group_sort,
    set_column_group_visibility,
)
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools
from tests._band_fixture import BANDS, band_row, build_band_report

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _tablix(doc: RDLDocument, name: str):
    return doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='{name}']")


def _column_hierarchy_top_members(tablix):
    return list(tablix.find(q("TablixColumnHierarchy")).find(q("TablixMembers")))


def _row_hierarchy_top_members(tablix):
    return list(tablix.find(q("TablixRowHierarchy")).find(q("TablixMembers")))


def _body_column_count(tablix) -> int:
    return len(
        find_children(
            tablix.find(f"{q('TablixBody')}/{q('TablixColumns')}"),
            "TablixColumn",
        )
    )


def _body_row_count(tablix) -> int:
    return len(find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow"))


def _row_cell_count(tablix, row_index: int) -> int:
    rows = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")
    return len(rows[row_index].findall(f"{q('TablixCells')}/{q('TablixCell')}"))


# ---- add_column_group -----------------------------------------------------


class TestAddColumnGroup:
    def test_wraps_existing_column_hierarchy_under_new_outer_member(self, rdl_path):
        before = RDLDocument.open(rdl_path)
        before_top_count = len(_column_hierarchy_top_members(_tablix(before, "MainTable")))

        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )

        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        top = _column_hierarchy_top_members(tablix)
        # Exactly one top-level member, holding the new Region group.
        assert len(top) == 1
        group = top[0].find(q("Group"))
        assert group is not None
        assert group.get("Name") == "Region"
        # Original top-level members are preserved as children of the wrapper.
        wrapped = list(top[0].find(q("TablixMembers")))
        assert len(wrapped) == before_top_count

    def test_records_group_expression(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        expr_node = tablix.find(
            f"{q('TablixColumnHierarchy')}/{q('TablixMembers')}/{q('TablixMember')}/"
            f"{q('Group')}/{q('GroupExpressions')}/{q('GroupExpression')}"
        )
        assert expr_node is not None
        assert expr_node.text == "=Fields!Region.Value"

    def test_body_columns_and_cells_unchanged(self, rdl_path):
        # The caption lives in the group's TablixHeader (a header row above
        # the body), so wrapping adds no body column and no cells (#29).
        before = RDLDocument.open(rdl_path)
        tablix_before = _tablix(before, "MainTable")
        before_cols = _body_column_count(tablix_before)
        before_cells = [
            _row_cell_count(tablix_before, i) for i in range(_body_row_count(tablix_before))
        ]

        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )

        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        assert _body_column_count(tablix) == before_cols
        assert [_row_cell_count(tablix, i) for i in range(len(before_cells))] == before_cells
        assert len(column_leaves(tablix)) == before_cols

    def test_group_member_carries_tablix_header_with_expression(self, rdl_path):
        result = add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        outer = _column_hierarchy_top_members(tablix)[0]
        header = outer.find(q("TablixHeader"))
        assert header is not None
        assert header.find(q("Size")).text == "0.25in"
        textbox = header.find(f"{q('CellContents')}/{q('Textbox')}")
        assert textbox.get("Name") == "Region_Header"
        assert textbox.find(f".//{q('Value')}").text == "=Fields!Region.Value"
        assert result["header_textbox"] == "Region_Header"
        assert result["header_textboxes"] == ["Region_Header"]
        # Group, then TablixHeader, then the wrapped members.
        assert [etree.QName(c).localname for c in outer if isinstance(c.tag, str)] == [
            "Group",
            "TablixHeader",
            "TablixMembers",
        ]
        assert header_depth(tablix, "column") == 1

    def test_get_tablixes_reports_new_column_group(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        out = get_tablixes(path=str(rdl_path))[0]
        names = [g["name"] for g in out["column_groups"]]
        assert "Region" in names
        region = next(g for g in out["column_groups"] if g["name"] == "Region")
        assert region["expressions"] == ["=Fields!Region.Value"]

    def test_duplicate_group_name_rejected(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        with pytest.raises(ValueError):
            add_column_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Region",
                group_expression="=Fields!Region.Value",
            )

    def test_clashes_with_existing_row_group_name(self, rdl_path):
        # Group names are unique per tablix across both axes — adding a
        # column group with a name already used by a row group must fail.
        from pbirb_mcp.ops.tablix import add_row_group

        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        with pytest.raises(ValueError):
            add_column_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Region",
                group_expression="=Fields!Region.Value",
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_column_group(
                path=str(rdl_path),
                tablix_name="NoSuch",
                group_name="Region",
                group_expression="=Fields!Region.Value",
            )

    def test_parent_group_not_yet_supported(self, rdl_path):
        with pytest.raises(NotImplementedError):
            add_column_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="City",
                group_expression="=Fields!City.Value",
                parent_group="Region",
            )

    def test_round_trip_safe(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- remove_column_group --------------------------------------------------


class TestRemoveColumnGroup:
    def test_inverts_add_column_group(self, rdl_path):
        before_doc = RDLDocument.open(rdl_path)
        before_top = [
            n.tag for n in _column_hierarchy_top_members(_tablix(before_doc, "MainTable"))
        ]
        before_cols = _body_column_count(_tablix(before_doc, "MainTable"))
        before_rows = _body_row_count(_tablix(before_doc, "MainTable"))
        before_cells_per_row = [
            _row_cell_count(_tablix(before_doc, "MainTable"), i) for i in range(before_rows)
        ]

        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        remove_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
        )

        after_doc = RDLDocument.open(rdl_path)
        after_top = [n.tag for n in _column_hierarchy_top_members(_tablix(after_doc, "MainTable"))]
        assert after_top == before_top
        tablix = _tablix(after_doc, "MainTable")
        assert _body_column_count(tablix) == before_cols
        assert _body_row_count(tablix) == before_rows
        for i, before_n in enumerate(before_cells_per_row):
            assert _row_cell_count(tablix, i) == before_n

    def test_remove_unknown_group_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_column_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="NoSuchGroup",
            )

    def test_refuses_to_remove_row_group_from_column_axis(self, rdl_path):
        # A group that exists, but on the row axis, is not removable via
        # remove_column_group — surface a clear hierarchy mismatch error.
        from pbirb_mcp.ops.tablix import add_row_group

        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        with pytest.raises(ElementNotFoundError):
            remove_column_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Region",
            )


# ---- set_column_group_sort ------------------------------------------------


class TestSetColumnGroupSort:
    def test_writes_sort_expressions_on_column_group_member(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_column_group_sort(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            sort_expressions=["=Fields!Region.Value", "=Fields!Sales.Value"],
        )

        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        # Find the column-axis member holding Region.
        member = None
        ch = tablix.find(q("TablixColumnHierarchy"))
        for m in ch.iter(q("TablixMember")):
            g = find_child(m, "Group")
            if g is not None and g.get("Name") == "Region":
                member = m
                break
        assert member is not None
        sort_block = find_child(member, "SortExpressions")
        assert sort_block is not None
        values = [find_child(s, "Value").text for s in find_children(sort_block, "SortExpression")]
        assert values == ["=Fields!Region.Value", "=Fields!Sales.Value"]

    def test_replaces_existing_sort_block(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_column_group_sort(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            sort_expressions=["=Fields!Region.Value"],
        )
        set_column_group_sort(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            sort_expressions=["=Fields!Sales.Value"],
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        ch = tablix.find(q("TablixColumnHierarchy"))
        member = next(
            m
            for m in ch.iter(q("TablixMember"))
            if find_child(m, "Group") is not None and find_child(m, "Group").get("Name") == "Region"
        )
        sort_block = find_child(member, "SortExpressions")
        values = [find_child(s, "Value").text for s in find_children(sort_block, "SortExpression")]
        assert values == ["=Fields!Sales.Value"]

    def test_direction_set_kept_and_cleared(self, rdl_path):
        # #66: same contract as set_group_sort.
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )

        def sorts():
            ch = _tablix(RDLDocument.open(rdl_path), "MainTable").find(q("TablixColumnHierarchy"))
            member = next(
                m
                for m in ch.iter(q("TablixMember"))
                if find_child(m, "Group") is not None
                and find_child(m, "Group").get("Name") == "Region"
            )
            block = find_child(member, "SortExpressions")
            if block is None:
                return None
            return [
                (find_child(s, "Value").text, getattr(find_child(s, "Direction"), "text", None))
                for s in find_children(block, "SortExpression")
            ]

        out = set_column_group_sort(
            str(rdl_path),
            "MainTable",
            "Region",
            [{"value": "=Fields!Region.Value", "direction": "Descending"}],
        )
        assert out["sort"] == [{"value": "=Fields!Region.Value", "direction": "Descending"}]
        assert sorts() == [("=Fields!Region.Value", "Descending")]
        out = set_column_group_sort(
            str(rdl_path), "MainTable", "Region", ["=Fields!Region.Value", "=Fields!Sales.Value"]
        )
        assert sorts() == [("=Fields!Region.Value", "Descending"), ("=Fields!Sales.Value", None)]
        raw = rdl_path.read_bytes()
        again = set_column_group_sort(
            str(rdl_path), "MainTable", "Region", ["=Fields!Region.Value", "=Fields!Sales.Value"]
        )
        assert again["changed"] is False and rdl_path.read_bytes() == raw
        set_column_group_sort(str(rdl_path), "MainTable", "Region", [])
        assert sorts() is None
        assert validate_report(str(rdl_path))["valid"]

    def test_refuses_row_group(self, rdl_path):
        from pbirb_mcp.ops.tablix import add_row_group

        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="RowRegion",
            group_expression="=Fields!Region.Value",
        )
        with pytest.raises(ElementNotFoundError):
            set_column_group_sort(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="RowRegion",
                sort_expressions=["=Fields!Region.Value"],
            )


# ---- set_column_group_visibility ------------------------------------------


class TestSetColumnGroupVisibility:
    def test_writes_visibility_on_column_group_member(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_column_group_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            visibility_expression="=Parameters!ShowRegion.Value = false",
            toggle_textbox=None,
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        ch = tablix.find(q("TablixColumnHierarchy"))
        member = next(
            m
            for m in ch.iter(q("TablixMember"))
            if find_child(m, "Group") is not None and find_child(m, "Group").get("Name") == "Region"
        )
        vis = find_child(member, "Visibility")
        assert vis is not None
        assert find_child(vis, "Hidden").text == "=Parameters!ShowRegion.Value = false"
        assert find_child(vis, "ToggleItem") is None

    def test_records_toggle_textbox(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        # The toggle must be a real textbox outside the columns it hides.
        from pbirb_mcp.ops.body import add_body_textbox

        add_body_textbox(str(rdl_path), "ToggleRegion", "+", "0in", "6in", "0.3in", "0.25in")
        set_column_group_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            visibility_expression="=Parameters!ShowRegion.Value = false",
            toggle_textbox="ToggleRegion",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        ch = tablix.find(q("TablixColumnHierarchy"))
        member = next(
            m
            for m in ch.iter(q("TablixMember"))
            if find_child(m, "Group") is not None and find_child(m, "Group").get("Name") == "Region"
        )
        vis = find_child(member, "Visibility")
        assert find_child(vis, "ToggleItem").text == "ToggleRegion"

    def test_refuses_row_group(self, rdl_path):
        from pbirb_mcp.ops.tablix import add_row_group

        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="RowRegion",
            group_expression="=Fields!Region.Value",
        )
        with pytest.raises(ElementNotFoundError):
            set_column_group_visibility(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="RowRegion",
                visibility_expression="=true",
            )


# ---- tools.py registration smoke ------------------------------------------


class TestToolRegistration:
    def test_all_four_tools_registered(self):
        server = MCPServer()
        register_all_tools(server)
        names = set(server._tools.keys())
        assert {
            "add_column_group",
            "remove_column_group",
            "set_column_group_sort",
            "set_column_group_visibility",
        }.issubset(names)


# ---- #29: leaf / column invariant, TablixHeader, corner, exact inverse ------

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.rdl"))


def _all_tablixes(path: Path):
    doc = RDLDocument.open(path)
    return [t.get("Name") for t in doc.root.iter(q("Tablix"))]


def _with_row_headers(path: Path) -> Path:
    """Give every row-hierarchy leaf of MainTable a 1in TablixHeader —
    the shape of a Report Builder matrix with one row-header column."""
    doc = RDLDocument.open(path)
    tablix = _tablix(doc, "MainTable")
    for i, leaf in enumerate(row_leaves(tablix)):
        header = new_header("1in", f"RowHead{i}")
        group = leaf.find(q("Group"))
        if group is not None:
            group.addnext(header)
        else:
            leaf.insert(0, header)
    doc.save()
    return path


def _corner_cells(tablix):
    corner = tablix.find(q("TablixCorner"))
    if corner is None:
        return None
    return [
        [c.find(f"{q('CellContents')}/{q('Textbox')}") for c in row.findall(q("TablixCornerCell"))]
        for row in corner.find(q("TablixCornerRows")).findall(q("TablixCornerRow"))
    ]


class TestColumnGroupShapeInvariant:
    @pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
    def test_every_fixture_tablix_keeps_leaves_equal_to_columns(self, tmp_path, fixture):
        path = tmp_path / fixture.name
        shutil.copy(fixture, path)
        original = path.read_bytes()
        tablixes = _all_tablixes(path)
        for i, name in enumerate(tablixes):
            add_column_group(str(path), name, f"ColGroup{i}", "=Fields!ProductID.Value")
            tablix = _tablix(RDLDocument.open(path), name)
            n_cols = _body_column_count(tablix)
            assert len(column_leaves(tablix)) == n_cols
            for r in range(_body_row_count(tablix)):
                assert _row_cell_count(tablix, r) == n_cols
            assert shape_problems(tablix) == []
            assert header_depth(tablix, "column") == 1
        assert validate_report(str(path))["valid"]
        for i, name in reversed(list(enumerate(tablixes))):
            remove_column_group(str(path), name, f"ColGroup{i}")
        assert path.read_bytes() == original

    def test_add_then_remove_is_byte_identical(self, rdl_path):
        original = rdl_path.read_bytes()
        add_column_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_column_group(str(rdl_path), "MainTable", "Year", "=Fields!ProductID.Value")
        remove_column_group(str(rdl_path), "MainTable", "Year")
        remove_column_group(str(rdl_path), "MainTable", "Region")
        assert rdl_path.read_bytes() == original

    def test_nested_groups_stack_header_levels(self, rdl_path):
        add_column_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_column_group(str(rdl_path), "MainTable", "Year", "=Fields!ProductID.Value")
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        assert header_depth(tablix, "column") == 2
        assert shape_problems(tablix) == []
        # A table without row headers has no corner to grow.
        assert tablix.find(q("TablixCorner")) is None

    @pytest.mark.parametrize("position", [None, 0])
    def test_column_added_beside_the_group_gets_a_header(self, rdl_path, position):
        add_column_group(str(rdl_path), "MainTable", "Region", "=1")
        result = add_tablix_column(
            str(rdl_path), "MainTable", "Qty", "=Fields!Amount.Value", position=position
        )
        # Exactly one header for the new column: #33's placement copies the
        # siblings' TablixHeader depth; #29's padding fills any gap left.
        headers = result.get("header_textboxes", []) + result.get("column_header_textboxes", [])
        assert len(headers) == 1 and headers[0].startswith("Qty_")
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        assert shape_problems(tablix) == []
        assert validate_report(str(rdl_path))["valid"]
        if position == 0:
            # (Removing a column by top-level member index past a group is
            # remove_tablix_column's own leaf-indexing fix, #33.)
            remove_tablix_column(str(rdl_path), "MainTable", "Qty")
            tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
            assert shape_problems(tablix) == []

    def test_refuses_tablix_whose_columns_already_disagree(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        members = _tablix(doc, "MainTable").find(
            f"{q('TablixColumnHierarchy')}/{q('TablixMembers')}"
        )
        members.remove(members[0])
        doc.save()
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="2 column-hierarchy leaves but 3 body columns"):
            add_column_group(str(rdl_path), "MainTable", "Region", "=1")
        assert rdl_path.read_bytes() == before


class TestColumnGroupCorner:
    def test_creates_corner_sized_header_rows_by_row_header_columns(self, rdl_path):
        _with_row_headers(rdl_path)
        result = add_column_group(str(rdl_path), "MainTable", "Region", "=1")
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cells = _corner_cells(tablix)
        assert [len(r) for r in cells] == [1]
        assert cells[0][0].get("Name") == "MainTable_Corner"
        assert result["corner_textboxes"] == ["MainTable_Corner"]
        # Report Builder's slot: TablixCorner is the first Tablix child.
        assert etree.QName(tablix[0]).localname == "TablixCorner"
        assert shape_problems(tablix) == []
        assert validate_report(str(rdl_path))["valid"]

    def test_second_group_grows_corner_at_the_top(self, rdl_path):
        _with_row_headers(rdl_path)
        add_column_group(str(rdl_path), "MainTable", "Region", "=1")
        add_column_group(str(rdl_path), "MainTable", "Year", "=2")
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cells = _corner_cells(tablix)
        assert [[tb.get("Name") for tb in r] for r in cells] == [
            ["MainTable_Corner1"],
            ["MainTable_Corner"],
        ]
        assert shape_problems(tablix) == []

    def test_remove_is_exact_inverse_with_corner(self, rdl_path):
        _with_row_headers(rdl_path)
        baseline = rdl_path.read_bytes()
        add_column_group(str(rdl_path), "MainTable", "Region", "=1")
        after_one = rdl_path.read_bytes()
        add_column_group(str(rdl_path), "MainTable", "Year", "=2")
        result = remove_column_group(str(rdl_path), "MainTable", "Year")
        assert result["removed_textboxes"] == ["Year_Header", "MainTable_Corner1"]
        assert rdl_path.read_bytes() == after_one
        remove_column_group(str(rdl_path), "MainTable", "Region")
        assert rdl_path.read_bytes() == baseline

    def test_removing_top_corner_row_keeps_a_row_spanning_cell(self, rdl_path):
        _with_row_headers(rdl_path)
        add_column_group(str(rdl_path), "MainTable", "Region", "=1")
        add_column_group(str(rdl_path), "MainTable", "Year", "=2")
        # Author merges the 2-row corner into one cell (RowSpan=2) on top.
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        rows = tablix.find(f"{q('TablixCorner')}/{q('TablixCornerRows')}")
        top, bottom = rows.findall(q("TablixCornerRow"))
        top_contents = top.find(f"{q('TablixCornerCell')}/{q('CellContents')}")
        etree.SubElement(top_contents, q("RowSpan")).text = "2"
        bottom_cell = bottom.find(q("TablixCornerCell"))
        bottom_cell.remove(bottom_cell.find(q("CellContents")))
        doc.save()
        assert shape_problems(_tablix(RDLDocument.open(rdl_path), "MainTable")) == []

        remove_column_group(str(rdl_path), "MainTable", "Year")
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        cells = _corner_cells(tablix)
        assert [[tb.get("Name") for tb in r] for r in cells] == [["MainTable_Corner1"]]
        owner = tablix.find(f".//{q('TablixCornerCell')}/{q('CellContents')}")
        assert owner.find(q("RowSpan")).text == "1"
        assert shape_problems(tablix) == []

    def test_ensure_corner_drops_corner_without_row_headers(self, rdl_path):
        _with_row_headers(rdl_path)
        add_column_group(str(rdl_path), "MainTable", "Region", "=1")
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        for header in list(tablix.find(q("TablixRowHierarchy")).iter(q("TablixHeader"))):
            header.getparent().remove(header)
        result = ensure_corner(tablix, names.NameRegistry(doc))
        assert result["removed"] == ["MainTable_Corner"]
        assert tablix.find(q("TablixCorner")) is None
        assert shape_problems(tablix) == []

    def test_ensure_corner_grows_columns_at_the_left(self, rdl_path):
        _with_row_headers(rdl_path)
        add_column_group(str(rdl_path), "MainTable", "Region", "=1")
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        # A second row-header level (a ColSpan=2 header covers both).
        for header in tablix.find(q("TablixRowHierarchy")).iter(q("TablixHeader")):
            etree.SubElement(header.find(q("CellContents")), q("ColSpan")).text = "2"
        assert header_depth(tablix, "row") == 2
        result = ensure_corner(tablix, names.NameRegistry(doc))
        assert result["created"] == ["MainTable_Corner1"]
        cells = _corner_cells(tablix)
        assert [[tb.get("Name") for tb in r] for r in cells] == [
            ["MainTable_Corner1", "MainTable_Corner"]
        ]
        assert shape_problems(tablix) == []


class TestRemoveColumnGroupEdgeCases:
    def test_unwrapped_columns_beside_a_sibling_get_blank_headers(self, rdl_path):
        add_column_group(str(rdl_path), "MainTable", "Region", "=1")
        # A sibling static column beside the group, with its own header.
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        sibling = etree.SubElement(
            tablix.find(f"{q('TablixColumnHierarchy')}/{q('TablixMembers')}"), q("TablixMember")
        )
        sibling.append(new_header("0.25in", "TotalHead", "Total"))
        etree.SubElement(
            tablix.find(f"{q('TablixBody')}/{q('TablixColumns')}"), q("TablixColumn")
        ).append(etree.Element(q("Width")))
        tablix.find(f".//{q('TablixColumn')}[last()]/{q('Width')}").text = "1in"
        for i, row in enumerate(tablix.iter(q("TablixRow"))):
            cell = etree.SubElement(row.find(q("TablixCells")), q("TablixCell"))
            contents = etree.SubElement(cell, q("CellContents"))
            contents.append(new_header("1in", f"TotalCell{i}").find(f".//{q('Textbox')}"))
        doc.save()
        assert shape_problems(_tablix(RDLDocument.open(rdl_path), "MainTable")) == []

        result = remove_column_group(str(rdl_path), "MainTable", "Region")
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        assert len(result["padded_header_textboxes"]) == 3
        assert shape_problems(tablix) == []
        assert validate_report(str(rdl_path))["valid"]

    def test_repairs_group_added_by_the_old_body_column_shape(self, rdl_path):
        # Recreate what add_column_group wrote before #29: a group without
        # a TablixHeader plus a <group>_HeaderCol_<i> column at body col 0.
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        members = tablix.find(f"{q('TablixColumnHierarchy')}/{q('TablixMembers')}")
        outer = etree.Element(q("TablixMember"))
        group = etree.SubElement(outer, q("Group"), Name="Legacy")
        etree.SubElement(etree.SubElement(group, q("GroupExpressions")), q("GroupExpression"))
        inner = etree.SubElement(outer, q("TablixMembers"))
        for child in list(members):
            inner.append(child)
        members.append(outer)
        cols = tablix.find(f"{q('TablixBody')}/{q('TablixColumns')}")
        col = etree.Element(q("TablixColumn"))
        etree.SubElement(col, q("Width")).text = "1in"
        cols.insert(0, col)
        for i, row in enumerate(tablix.iter(q("TablixRow"))):
            cell = etree.Element(q("TablixCell"))
            contents = etree.SubElement(cell, q("CellContents"))
            contents.append(new_header("1in", f"Legacy_HeaderCol_{i}").find(f".//{q('Textbox')}"))
            row.find(q("TablixCells")).insert(0, cell)
        doc.save()
        assert shape_problems(_tablix(RDLDocument.open(rdl_path), "MainTable")) != []

        result = remove_column_group(str(rdl_path), "MainTable", "Legacy")
        assert result["legacy_header_column_removed"] is True
        assert result["removed_textboxes"] == ["Legacy_HeaderCol_0", "Legacy_HeaderCol_1"]
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        assert shape_problems(tablix) == []
        assert _body_column_count(tablix) == 3
        assert validate_report(str(rdl_path))["valid"]


def test_tool_description_documents_header_not_body_column():
    server = MCPServer()
    register_all_tools(server)
    desc = server._tools["add_column_group"].description
    assert "TablixHeader" in desc
    assert "body column 0" not in desc


# ---- #34: spans survive column add / remove --------------------------------


def _span_sums(path: Path) -> list:
    tablix = _tablix(RDLDocument.open(path), "MainTable")
    n_cols = _body_column_count(tablix)
    sums = []
    for i in range(_body_row_count(tablix)):
        sums.append(sum(span for name, span in band_row(path, i) if name != "-"))
    return n_cols, sums


class TestColumnSpanBookkeeping:
    @pytest.mark.parametrize("column", range(6))
    def test_removing_any_column_keeps_bands(self, tmp_path, column):
        path = build_band_report(tmp_path / "bands.rdl")
        result = remove_tablix_column(str(path), "MainTable", f"Data{column}")
        bands = [name for name, _ in band_row(path, 0) if name != "-"]
        for name, width in BANDS:
            if width > 1:
                assert name in bands
        n_cols, sums = _span_sums(path)
        assert n_cols == 5
        assert sums == [5, 5, 5]
        assert shape_problems(_tablix(RDLDocument.open(path), "MainTable")) == []
        assert validate_report(str(path))["valid"]
        assert result["removed_textboxes"][-2:] == [f"Head{column}", f"Data{column}"]

    def test_removing_band_owner_column_keeps_its_text(self, tmp_path):
        path = build_band_report(tmp_path / "bands.rdl")
        remove_tablix_column(str(path), "MainTable", "Data0")
        assert band_row(path, 0)[:3] == [("IVMSBand", 2), ("-", 1), ("AFDDBand", 2)]

    def test_removing_covered_column_shrinks_the_span(self, tmp_path):
        path = build_band_report(tmp_path / "bands.rdl")
        remove_tablix_column(str(path), "MainTable", "Data4")
        assert band_row(path, 0)[3:] == [("AFDDBand", 1), ("SoloBand", 1)]

    @pytest.mark.parametrize(
        "position, expected",
        [
            (1, [("IVMSBand", 4), ("-", 1), ("-", 1), ("-", 1), ("AFDDBand", 2)]),
            (2, [("IVMSBand", 4), ("-", 1), ("-", 1), ("-", 1), ("AFDDBand", 2)]),
            (4, [("IVMSBand", 3), ("-", 1), ("-", 1), ("AFDDBand", 3), ("-", 1)]),
        ],
    )
    def test_inserting_inside_a_band_grows_it(self, tmp_path, position, expected):
        path = build_band_report(tmp_path / "bands.rdl")
        result = add_tablix_column(str(path), "MainTable", "NewCol", "=1", position=position)
        assert band_row(path, 0)[:5] == expected
        assert result["widened_span_rows"] == [0]
        assert result["textboxes"] == ["NewCol_1", "NewCol"]
        n_cols, sums = _span_sums(path)
        assert sums == [n_cols] * 3 == [7, 7, 7]
        assert shape_problems(_tablix(RDLDocument.open(path), "MainTable")) == []
        assert validate_report(str(path))["valid"]

    def test_inserting_at_a_band_boundary_adds_a_cell(self, tmp_path):
        path = build_band_report(tmp_path / "bands.rdl")
        result = add_tablix_column(str(path), "MainTable", "NewCol", "=1", position=3)
        assert band_row(path, 0)[3] == ("NewCol_0", 1)
        assert "widened_span_rows" not in result
        assert shape_problems(_tablix(RDLDocument.open(path), "MainTable")) == []

    def test_insert_then_remove_inside_a_band_restores_it(self, tmp_path):
        path = build_band_report(tmp_path / "bands.rdl")
        before = band_row(path, 0)
        add_tablix_column(str(path), "MainTable", "NewCol", "=1", position=1)
        remove_tablix_column(str(path), "MainTable", "NewCol")
        assert band_row(path, 0) == before
        assert shape_problems(_tablix(RDLDocument.open(path), "MainTable")) == []

    def test_inserting_inside_a_merged_data_row_cell_is_refused(self, tmp_path):
        path = build_band_report(tmp_path / "bands.rdl")
        set_cell_span(str(path), "MainTable", 2, "Data0", col_span=2, force=True)
        before = path.read_bytes()
        with pytest.raises(ValueError, match="inside the merged data-row cell Data0"):
            add_tablix_column(str(path), "MainTable", "NewCol", "=1", position=1)
        assert path.read_bytes() == before
