"""Column-hierarchy tablix tools — first commit of v0.2.

Mirrors the row-group test pattern in ``test_tablix_groups.py`` for the
column hierarchy. Adding a column group wraps existing column members
under a new outer ``<TablixMember>`` and inserts a matching header
column at body column 0 with the group expression in the topmost cell.
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

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.reader import get_tablixes
from pbirb_mcp.ops.tablix_columns import (
    add_column_group,
    remove_column_group,
    set_column_group_sort,
    set_column_group_visibility,
)
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

    def test_inserts_new_body_column_at_position_zero(self, rdl_path):
        before = RDLDocument.open(rdl_path)
        before_cols = _body_column_count(_tablix(before, "MainTable"))

        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )

        after = RDLDocument.open(rdl_path)
        tablix = _tablix(after, "MainTable")
        assert _body_column_count(tablix) == before_cols + 1

    def test_inserts_new_cell_in_every_row_at_position_zero(self, rdl_path):
        before = RDLDocument.open(rdl_path)
        before_row_count = _body_row_count(_tablix(before, "MainTable"))
        # Snapshot per-row cell counts before the edit.
        before_cells_per_row = [
            _row_cell_count(_tablix(before, "MainTable"), i) for i in range(before_row_count)
        ]

        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )

        after = RDLDocument.open(rdl_path)
        tablix = _tablix(after, "MainTable")
        # Row count is unchanged; cell count in each row grows by exactly one.
        assert _body_row_count(tablix) == before_row_count
        for i, before_n in enumerate(before_cells_per_row):
            assert _row_cell_count(tablix, i) == before_n + 1

    def test_topmost_new_cell_holds_group_expression(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        # First cell of row 0 = the group-header cell.
        first_row = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")[0]
        first_cell = first_row.findall(f"{q('TablixCells')}/{q('TablixCell')}")[0]
        textrun_value = first_cell.find(
            f"{q('CellContents')}/{q('Textbox')}/{q('Paragraphs')}/"
            f"{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value is not None
        assert textrun_value.text == "=Fields!Region.Value"

    def test_subsequent_new_cells_are_blank(self, rdl_path):
        # Rows below the topmost get a blank textbox in the new column.
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        rows = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")
        for row in rows[1:]:
            first_cell = row.findall(f"{q('TablixCells')}/{q('TablixCell')}")[0]
            textrun_value = first_cell.find(
                f"{q('CellContents')}/{q('Textbox')}/{q('Paragraphs')}/"
                f"{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
            )
            assert textrun_value is not None
            assert (textrun_value.text or "") == ""

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
