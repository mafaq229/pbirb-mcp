"""Tablix grouping tool tests.

Covers the gnarly Phase-3 commit: adding/removing row groups (which
restructures both ``TablixRowHierarchy`` and ``TablixBody/TablixRows``),
plus the simpler set_group_sort / set_group_visibility editors.

Scope of ``add_row_group`` in this commit: outermost row group only.
Wrapping the entire existing top-level row hierarchy and inserting a
matching group-header row at body row 0. Nested ``parent_group`` is
deliberately deferred — it's a separate kind of restructuring and the
plan flags this commit as the heaviest already.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.reader import get_tablixes
from pbirb_mcp.ops.tablix import (
    add_row_group,
    remove_row_group,
    set_group_sort,
    set_group_visibility,
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


def _row_hierarchy_top_members(tablix):
    return list(tablix.find(q("TablixRowHierarchy")).find(q("TablixMembers")))


def _body_row_count(tablix) -> int:
    return len(find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow"))


# ---- add_row_group --------------------------------------------------------


class TestAddRowGroup:
    def test_wraps_existing_hierarchy_under_new_outer_member(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        top = _row_hierarchy_top_members(tablix)
        # Exactly one top-level member, which holds the new Region group.
        assert len(top) == 1
        group = top[0].find(q("Group"))
        assert group is not None
        assert group.get("Name") == "Region"
        # The fixture's original two members are preserved as children of the
        # new wrapper, plus a new header-row leaf prepended at index 0.
        wrapped = list(top[0].find(q("TablixMembers")))
        assert len(wrapped) == 3  # group-header leaf + 2 originals

    def test_appends_one_body_row_at_position_zero(self, rdl_path):
        before = RDLDocument.open(rdl_path)
        rows_before = _body_row_count(_tablix(before, "MainTable"))
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        after = RDLDocument.open(rdl_path)
        tablix = _tablix(after, "MainTable")
        assert _body_row_count(tablix) == rows_before + 1
        # The first cell of the new row 0 must hold the group expression.
        first_row = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")[0]
        first_cell = first_row.findall(f"{q('TablixCells')}/{q('TablixCell')}")[0]
        textrun_value = first_cell.find(
            f"{q('CellContents')}/{q('Textbox')}/{q('Paragraphs')}/"
            f"{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "=Fields!Region.Value"

    def test_get_tablixes_reports_new_group(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        out = get_tablixes(path=str(rdl_path))[0]
        names = [g["name"] for g in out["row_groups"]]
        assert "Region" in names
        # And the GroupExpression is exposed.
        region = next(g for g in out["row_groups"] if g["name"] == "Region")
        assert region["expressions"] == ["=Fields!Region.Value"]

    def test_duplicate_group_name_rejected(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        with pytest.raises(ValueError):
            add_row_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Region",
                group_expression="=Fields!Region.Value",
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_row_group(
                path=str(rdl_path),
                tablix_name="NoSuch",
                group_name="Region",
                group_expression="=Fields!Region.Value",
            )

    def test_parent_group_not_yet_supported(self, rdl_path):
        with pytest.raises(NotImplementedError):
            add_row_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="City",
                group_expression="=Fields!City.Value",
                parent_group="Region",
            )

    def test_round_trip_safe(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()  # structural validate still passes


# ---- remove_row_group ------------------------------------------------------


class TestRemoveRowGroup:
    def test_inverts_add_row_group(self, rdl_path):
        before_doc = RDLDocument.open(rdl_path)
        before_top = [
            etree_node.tag
            for etree_node in _row_hierarchy_top_members(_tablix(before_doc, "MainTable"))
        ]
        before_rows = _body_row_count(_tablix(before_doc, "MainTable"))

        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        remove_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
        )

        after_doc = RDLDocument.open(rdl_path)
        after_top = [n.tag for n in _row_hierarchy_top_members(_tablix(after_doc, "MainTable"))]
        assert after_top == before_top
        assert _body_row_count(_tablix(after_doc, "MainTable")) == before_rows

    def test_remove_unknown_group_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_row_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="NoSuchGroup",
            )

    def test_cannot_remove_details_group(self, rdl_path):
        # The detail group is not a real group with a header row; refuse to
        # remove it (or callers will end up with a tablix that has no leaves).
        with pytest.raises(ValueError):
            remove_row_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Details",
            )


# ---- set_group_sort -------------------------------------------------------


class TestSetGroupSort:
    def test_creates_sort_expressions_when_absent(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_group_sort(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            sort_expressions=["=Fields!Region.Value"],
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        # Find the TablixMember that holds Region.
        member = None
        for m in tablix.iter(q("TablixMember")):
            g = m.find(q("Group"))
            if g is not None and g.get("Name") == "Region":
                member = m
                break
        assert member is not None
        sorts_root = find_child(member, "SortExpressions")
        assert sorts_root is not None
        values = [s.find(q("Value")).text for s in find_children(sorts_root, "SortExpression")]
        assert values == ["=Fields!Region.Value"]

    def test_replaces_existing_sort_expressions(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_group_sort(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            sort_expressions=["=Fields!Region.Value"],
        )
        set_group_sort(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            sort_expressions=["=Fields!Amount.Value", "=Fields!ProductName.Value"],
        )
        out = get_tablixes(path=str(rdl_path))[0]
        # sort_expressions are returned as a flat list across the tablix.
        assert "=Fields!Amount.Value" in out["sort_expressions"]
        assert "=Fields!ProductName.Value" in out["sort_expressions"]
        # The old "Region" sort must be gone.
        assert "=Fields!Region.Value" not in out["sort_expressions"]

    def test_unknown_group_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_group_sort(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Ghost",
                sort_expressions=["=1"],
            )


# ---- set_group_visibility -------------------------------------------------


class TestSetGroupVisibility:
    def test_sets_hidden_expression(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_group_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            visibility_expression="=Parameters!HideRegion.Value",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        member = next(
            m
            for m in tablix.iter(q("TablixMember"))
            if m.find(q("Group")) is not None and m.find(q("Group")).get("Name") == "Region"
        )
        vis = find_child(member, "Visibility")
        assert vis is not None
        hidden = find_child(vis, "Hidden")
        assert hidden.text == "=Parameters!HideRegion.Value"
        assert find_child(vis, "ToggleItem") is None

    def test_with_toggle_textbox(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_group_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            visibility_expression="true",
            toggle_textbox="HeaderProductID",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        member = next(
            m
            for m in tablix.iter(q("TablixMember"))
            if m.find(q("Group")) is not None and m.find(q("Group")).get("Name") == "Region"
        )
        vis = find_child(member, "Visibility")
        assert find_child(vis, "ToggleItem").text == "HeaderProductID"

    def test_unknown_group_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_group_visibility(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Ghost",
                visibility_expression="true",
            )


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_grouping_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {
            "add_row_group",
            "remove_row_group",
            "set_group_sort",
            "set_group_visibility",
        } <= names
