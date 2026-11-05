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
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q, qrd
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


# ---- add_tablix_column: names, header_name, inherited style (#68) ---------


def _textbox(path: Path, name: str):
    doc = RDLDocument.open(path)
    hits = doc.root.findall(f".//{{{RDL_NS}}}Textbox[@Name='{name}']")
    assert len(hits) == 1, f"{name}: {len(hits)} textboxes"
    return hits[0]


_RUN_STYLE = "/".join(q(x) for x in ("Paragraphs", "Paragraph", "TextRuns", "TextRun", "Style"))


def _look(path: Path, name: str) -> dict:
    tb = _textbox(path, name)
    run_style = tb.find(_RUN_STYLE)
    box_style = find_child(tb, "Style")

    def text(style, local):
        node = find_child(style, local) if style is not None else None
        return node.text if node is not None else None

    return {
        "background": text(box_style, "BackgroundColor"),
        "weight": text(run_style, "FontWeight"),
        "format": text(run_style, "Format"),
    }


@pytest.fixture
def styled_path(rdl_path):
    from pbirb_mcp.ops.styling import style_tablix_row

    style_tablix_row(str(rdl_path), "MainTable", 0, font_weight="Bold", background_color="#1F3864")
    style_tablix_row(str(rdl_path), "MainTable", 1, format="#,0.00")
    return rdl_path


class TestAddTablixColumnNamesAndStyle:
    def test_issue_repro_no_duplicates_and_inherited_look(self, styled_path):
        from collections import Counter

        from pbirb_mcp.ops.body import add_body_textbox

        add_body_textbox(str(styled_path), "bar_0", "x", "3in", "0in", "1in", "0.25in")
        result = add_tablix_column(
            str(styled_path), "MainTable", "bar", "=Fields!Amount.Value", header_text="Bar"
        )
        doc = RDLDocument.open(styled_path)
        all_names = [tb.get("Name") for tb in doc.root.iter(q("Textbox"))]
        assert [n for n, c in Counter(all_names).items() if c > 1] == []
        header = result["header_textbox"]
        assert header == "bar_0_1" and result["data_textbox"] == "bar"
        assert result["row_textboxes"] == [header, "bar"]
        assert result["style_copied_from_column"] == 2
        assert _look(styled_path, header) == {
            "background": "#1F3864",
            "weight": "Bold",
            "format": None,
        }
        assert _look(styled_path, "bar")["format"] == "#,0.00"
        assert validate_report(str(styled_path))["valid"]

    def test_copy_style_from_none_keeps_bare_cells(self, styled_path):
        result = add_tablix_column(str(styled_path), "MainTable", "Qty", "=1", copy_style_from=None)
        assert result["style_copied_from_column"] is None
        for name in result["textboxes"]:
            assert _look(styled_path, name) == {"background": None, "weight": None, "format": None}
            assert len(find_child(_textbox(styled_path, name), "Style")) == 0

    def test_copy_style_from_right_and_from_textbox(self, rdl_path):
        from pbirb_mcp.ops.styling import set_textbox_style

        set_textbox_style(str(rdl_path), "ProductID", background_color="Yellow")
        set_textbox_style(str(rdl_path), "ProductName", background_color="Green")
        r = add_tablix_column(
            str(rdl_path), "MainTable", "A", "=1", position=0, copy_style_from="right"
        )
        assert r["style_copied_from_column"] == 1
        assert _look(rdl_path, "A")["background"] == "Yellow"
        # 'left' at position 0 falls back to the right neighbour.
        add_tablix_column(str(rdl_path), "MainTable", "B", "=1", position=0)
        assert _look(rdl_path, "B")["background"] == "Yellow"
        # A textbox name picks that textbox's column, in every row.
        add_tablix_column(
            str(rdl_path), "MainTable", "C", "=1", copy_style_from="HeaderProductName"
        )
        assert _look(rdl_path, "C")["background"] == "Green"
        assert validate_report(str(rdl_path))["valid"]

    def test_copy_style_from_unknown_textbox_refused(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ElementNotFoundError):
            add_tablix_column(str(rdl_path), "MainTable", "A", "=1", copy_style_from="Ghost")
        assert rdl_path.read_bytes() == before

    def test_copies_style_only(self, rdl_path):
        from pbirb_mcp.ops.styling import set_textbox_style

        set_textbox_style(str(rdl_path), "Amount", text_align="Right", font_size="9pt")
        add_tablix_column(str(rdl_path), "MainTable", "Qty", "=Fields!ProductID.Value")
        new = _textbox(rdl_path, "Qty")
        assert new.find(f".//{q('Value')}").text == "=Fields!ProductID.Value"
        assert new.find(qrd("DefaultName")).text == "Qty"
        par_style = new.find(f"{q('Paragraphs')}/{q('Paragraph')}/{q('Style')}")
        run_style = new.find(_RUN_STYLE)
        styles = {etree.QName(c).localname: c.text for s in (par_style, run_style) for c in s}
        assert styles.get("TextAlign") == "Right" and styles.get("FontSize") == "9pt"
        assert _textbox(rdl_path, "Amount").find(f".//{q('Value')}").text == "=Fields!Amount.Value"

    def test_format_sets_and_clears_data_cell_format(self, styled_path):
        add_tablix_column(str(styled_path), "MainTable", "Pct", "=1", format="0.0%")
        assert _look(styled_path, "Pct")["format"] == "0.0%"
        add_tablix_column(str(styled_path), "MainTable", "Txt", "=1", format="")
        assert _look(styled_path, "Txt")["format"] is None
        # format applies to the data cell only.
        assert _look(styled_path, "Pct_0")["format"] is None
        assert validate_report(str(styled_path))["valid"]

    def test_header_name(self, rdl_path):
        r = add_tablix_column(str(rdl_path), "MainTable", "Qty", "=1", header_name="QtyHeader")
        assert r["header_textbox"] == "QtyHeader"
        assert r["textboxes"] == ["QtyHeader", "Qty"]
        assert _textbox(rdl_path, "QtyHeader").find(f".//{q('Value')}").text == "Qty"

    @pytest.mark.parametrize("bad", ["HeaderAmount", "Qty", "bad name"])
    def test_header_name_taken_or_invalid_refused(self, rdl_path, bad):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError):
            add_tablix_column(str(rdl_path), "MainTable", "Qty", "=1", header_name=bad)
        assert rdl_path.read_bytes() == before

    def test_header_name_without_header_cell_refused(self, tmp_path):
        path = build_band_report(tmp_path / "bands.rdl")
        before = path.read_bytes()
        # Row 0 is a band: inside IVMSBand the span grows over the column.
        with pytest.raises(ValueError, match="no cell to name"):
            add_tablix_column(str(path), "MainTable", "NewCol", "=1", position=1, header_name="H")
        assert path.read_bytes() == before

    def test_band_neighbour_style_comes_from_span_owner(self, tmp_path):
        from pbirb_mcp.ops.styling import set_textbox_style

        path = build_band_report(tmp_path / "bands.rdl")
        set_textbox_style(str(path), "IVMSBand", background_color="Navy")
        r = add_tablix_column(str(path), "MainTable", "NewCol", "=1", position=3)
        # The left neighbour of column 3 is the last column IVMSBand covers.
        assert _look(path, r["row_textboxes"][0])["background"] == "Navy"
        r = add_tablix_column(str(path), "MainTable", "Inside", "=1", position=1)
        assert r["row_textboxes"][0] is None and r["widened_span_rows"] == [0]
        assert validate_report(str(path))["valid"]

    def test_server_accepts_new_params(self, styled_path):
        server = MCPServer()
        register_all_tools(server)
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "add_tablix_column",
                    "arguments": {
                        "path": str(styled_path),
                        "tablix_name": "MainTable",
                        "column_name": "Qty",
                        "expression": "=1",
                        "header_name": "QtyHead",
                        "copy_style_from": None,
                        "format": "0",
                    },
                },
            }
        )
        assert not resp["result"].get("isError"), resp
        assert _look(styled_path, "QtyHead") == {"background": None, "weight": None, "format": None}
        assert _look(styled_path, "Qty")["format"] == "0"


# ---- #89: Tablix/Width follows the column widths --------------------------------


def _widths(path: Path, name: str = "MainTable") -> tuple[list[str], str]:
    tablix = next(t for t in RDLDocument.open(path).root.iter(q("Tablix")) if t.get("Name") == name)
    cols = find_child(find_child(tablix, "TablixBody"), "TablixColumns")
    return [find_child(c, "Width").text for c in cols], find_child(tablix, "Width").text


def _rendered_matches(path: Path, name: str = "MainTable") -> bool:
    from pbirb_mcp.core.geometry import length_in, tablix_columns_width

    tablix = next(t for t in RDLDocument.open(path).root.iter(q("Tablix")) if t.get("Name") == name)
    return abs(tablix_columns_width(tablix) - length_in(tablix, "Width")) < 0.001


def _width_mismatches(path: Path) -> list[dict]:
    from pbirb_mcp.ops.lint import lint_report

    return [i for i in lint_report(str(path))["issues"] if i["rule"] == "tablix-width-mismatch"]


class TestTablixWidthFollowsColumns:
    def test_issue_repro(self, rdl_path):
        from pbirb_mcp.ops.tablix import set_tablix_size
        from pbirb_mcp.ops.tablix_columns import set_column_width

        assert _widths(rdl_path) == (["1in", "2in", "1in"], "4in")
        out = add_tablix_column(str(rdl_path), "MainTable", "Extra", "=1", width="1.5in")
        assert out["tablix_width"] == "5.5in"
        assert _widths(rdl_path) == (["1in", "2in", "1in", "1.5in"], "5.5in")
        out = set_column_width(str(rdl_path), "MainTable", 0, "3in")
        assert out["changed"] is True and out["tablix_width"] == "7.5in"
        assert _widths(rdl_path)[1] == "7.5in"
        out = remove_tablix_column(str(rdl_path), "MainTable", "ProductName")
        assert out["tablix_width"] == "5.5in"
        assert _widths(rdl_path) == (["3in", "1in", "1.5in"], "5.5in")
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=r"add up to 5\.5in.*distribute=true"):
            set_tablix_size(str(rdl_path), "MainTable", width="1in")
        assert rdl_path.read_bytes() == before
        assert _width_mismatches(rdl_path) == []
        assert validate_report(str(rdl_path))["valid"]

    def test_set_column_width_repeat_is_noop(self, rdl_path):
        from pbirb_mcp.ops.tablix_columns import set_column_width

        set_column_width(str(rdl_path), "MainTable", 1, "2.5in")
        before = rdl_path.read_bytes()
        out = set_column_width(str(rdl_path), "MainTable", 1, "2.5in")
        assert out["changed"] is False and "tablix_width" not in out
        assert rdl_path.read_bytes() == before

    def test_set_column_width_repairs_stale_width(self, rdl_path):
        from pbirb_mcp.ops.tablix_columns import set_column_width

        src = rdl_path.read_text(encoding="utf-8")
        rdl_path.write_text(src.replace("<Width>4in</Width>", "<Width>9in</Width>"), "utf-8")
        out = set_column_width(str(rdl_path), "MainTable", 0, "1in")  # same width
        assert out["changed"] is True and out["tablix_width"] == "4in"

    def test_width_keeps_its_unit(self, rdl_path):
        src = rdl_path.read_text(encoding="utf-8")
        rdl_path.write_text(src.replace("<Width>4in</Width>", "<Width>10.16cm</Width>"), "utf-8")
        add_tablix_column(str(rdl_path), "MainTable", "Extra", "=1", width="2.54cm")
        assert _widths(rdl_path)[1] == "12.7cm"

    def test_add_static_column(self, rdl_path):
        from pbirb_mcp.ops.tablix_static import add_static_column

        out = add_static_column(str(rdl_path), "MainTable", "Note", width="0.75in")
        assert out["tablix_width"] == "4.75in"
        assert _width_mismatches(rdl_path) == []

    def test_add_subtotal_column(self, rdl_path):
        from pbirb_mcp.ops.tablix_subtotals import add_subtotal_column

        add_column_group(str(rdl_path), "MainTable", "G", "=Fields!ProductID.Value")
        out = add_subtotal_column(
            str(rdl_path),
            "MainTable",
            "G",
            aggregates=[{"row": 1, "expression": "=Sum(Fields!Amount.Value)"}],
            width="1.25in",
            scope="total",
        )
        assert "tablix_width" in out["changed"] and out["tablix_width"] == "5.25in"
        assert _width_mismatches(rdl_path) == []

    def test_row_group_header_column_and_back(self, rdl_path):
        from pbirb_mcp.ops.tablix import add_row_group, remove_row_group

        out = add_row_group(
            str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value", header="column"
        )
        assert out["tablix_width"] == "5in"  # + the 1in row header
        assert _rendered_matches(rdl_path) and _width_mismatches(rdl_path) == []
        out = remove_row_group(str(rdl_path), "MainTable", "Region")
        assert out["tablix_width"] == "4in"
        assert _widths(rdl_path)[1] == "4in"

    def test_row_group_header_column_repairs_stale_width(self, rdl_path):
        # M5 #65 shifted Width by the header size, keeping an old mismatch.
        from pbirb_mcp.ops.tablix import add_row_group

        src = rdl_path.read_text(encoding="utf-8")
        rdl_path.write_text(src.replace("<Width>4in</Width>", "<Width>6in</Width>"), "utf-8")
        add_row_group(str(rdl_path), "MainTable", "R", "=Fields!ProductName.Value", header="column")
        assert _widths(rdl_path)[1] == "5in"

    def test_template_splits_width_over_columns(self, rdl_path):
        from pbirb_mcp.ops.templates import insert_tablix_from_template

        insert_tablix_from_template(
            str(rdl_path),
            name="T",
            dataset_name="MainDataset",
            columns=["ProductID", "ProductName", "Amount"],
            top="1in",
            left="0in",
            width="10cm",
            height="0.5in",
        )
        cols, width = _widths(rdl_path, "T")
        assert width == "10cm"
        assert cols == ["3.33333cm", "3.33333cm", "3.33334cm"]
        assert _rendered_matches(rdl_path, "T")
        assert _width_mismatches(rdl_path) == []


class TestSetTablixSizeDistribute:
    def test_distribute_scales_columns(self, rdl_path):
        from pbirb_mcp.ops.tablix import set_tablix_size

        out = set_tablix_size(str(rdl_path), "MainTable", width="2in", distribute=True)
        assert out["changed"] == ["columns", "Width"]
        assert out["column_widths"] == ["0.5in", "1in", "0.5in"]
        assert _widths(rdl_path) == (["0.5in", "1in", "0.5in"], "2in")
        assert _width_mismatches(rdl_path) == []
        before = rdl_path.read_bytes()
        again = set_tablix_size(str(rdl_path), "MainTable", width="2in", distribute=True)
        assert again["changed"] == [] and rdl_path.read_bytes() == before

    def test_distribute_keeps_row_headers_and_units(self, rdl_path):
        from pbirb_mcp.ops.tablix import add_row_group, set_tablix_size

        src = rdl_path.read_text(encoding="utf-8")
        rdl_path.write_text(src.replace("<Width>2in</Width>", "<Width>5.08cm</Width>"), "utf-8")
        add_row_group(str(rdl_path), "MainTable", "R", "=Fields!ProductName.Value", header="column")
        assert _widths(rdl_path) == (["1in", "5.08cm", "1in"], "5in")
        out = set_tablix_size(str(rdl_path), "MainTable", width="9in", distribute=True)
        # 8in of columns for 4in: x2; the 1in row header stays.
        assert out["column_widths"] == ["2in", "10.16cm", "2in"]
        assert _rendered_matches(rdl_path)

    def test_distribute_refuses_width_inside_row_headers(self, rdl_path):
        from pbirb_mcp.ops.tablix import add_row_group, set_tablix_size

        add_row_group(str(rdl_path), "MainTable", "R", "=Fields!ProductName.Value", header="column")
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="row headers alone take 1in"):
            set_tablix_size(str(rdl_path), "MainTable", width="0.5in", distribute=True)
        assert rdl_path.read_bytes() == before

    def test_distribute_via_server(self, rdl_path):
        import json

        srv = MCPServer()
        register_all_tools(srv)
        call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "set_tablix_size",
                "arguments": {"path": str(rdl_path), "name": "MainTable", "width": "8in"},
            },
        }
        refused = srv.handle_request(call)["result"]
        assert refused.get("isError")
        assert "distribute=true" in json.loads(refused["content"][0]["text"])["message"]
        call["params"]["arguments"]["distribute"] = True
        ok = srv.handle_request(call)["result"]
        assert not ok.get("isError"), ok
        assert _widths(rdl_path) == (["2in", "4in", "2in"], "8in")
