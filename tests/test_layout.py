"""Tests for Phase 10 — pagination tools.

The fixture has a single ``Details`` group out of the box; tests that
need a named user-group call ``add_row_group`` first to create a real
``<Group Name="...">`` to operate on.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_group
from pbirb_mcp.core.xpath import find_child, q
from pbirb_mcp.ops.layout import set_group_page_break, set_repeat_on_new_page
from pbirb_mcp.ops.tablix import add_row_group
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def rdl_with_region_group(rdl_path: Path) -> Path:
    # Add a real row group so PageBreak / RepeatOnNewPage have a target.
    add_row_group(
        path=str(rdl_path),
        tablix_name="MainTable",
        group_name="Region",
        group_expression="=Fields!ProductName.Value",
    )
    return rdl_path


def _read_break_location(path: Path, tablix: str, group: str) -> str | None:
    doc = RDLDocument.open(path)
    g = resolve_group(doc, tablix, group)
    pb = find_child(g, "PageBreak")
    if pb is None:
        return None
    bl = find_child(pb, "BreakLocation")
    return bl.text if bl is not None else None


def _read_repeat(path: Path, tablix: str, group: str) -> str | None:
    doc = RDLDocument.open(path)
    g = resolve_group(doc, tablix, group)
    member = g.getparent()
    rep = find_child(member, "RepeatOnNewPage")
    return rep.text if rep is not None else None


# ---- set_group_page_break -----------------------------------------------


class TestSetGroupPageBreak:
    def test_writes_break_location(self, rdl_with_region_group):
        result = set_group_page_break(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            location="End",
        )
        assert result == {
            "tablix": "MainTable",
            "group": "Region",
            "kind": "Group",
            "location": "End",
            "changed": True,
        }
        assert _read_break_location(rdl_with_region_group, "MainTable", "Region") == "End"

    def test_idempotent_same_value(self, rdl_with_region_group):
        set_group_page_break(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            location="Start",
        )
        result = set_group_page_break(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            location="Start",
        )
        assert result["changed"] is False

    def test_replaces_existing_value(self, rdl_with_region_group):
        set_group_page_break(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            location="Start",
        )
        result = set_group_page_break(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            location="StartAndEnd",
        )
        assert result["changed"] is True
        assert _read_break_location(rdl_with_region_group, "MainTable", "Region") == "StartAndEnd"

    def test_none_removes_block(self, rdl_with_region_group):
        set_group_page_break(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            location="End",
        )
        result = set_group_page_break(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            location="None",
        )
        assert result == {
            "tablix": "MainTable",
            "group": "Region",
            "kind": "Group",
            "location": "None",
            "changed": True,
        }
        assert _read_break_location(rdl_with_region_group, "MainTable", "Region") is None

    def test_none_on_clean_group_is_noop(self, rdl_with_region_group):
        result = set_group_page_break(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            location="None",
        )
        assert result["changed"] is False

    def test_unknown_break_location_rejected(self, rdl_with_region_group):
        with pytest.raises(ValueError, match="unknown BreakLocation"):
            set_group_page_break(
                path=str(rdl_with_region_group),
                tablix_name="MainTable",
                group_name="Region",
                location="Often",
            )

    def test_unknown_group_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_group_page_break(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="NoSuchGroup",
                location="End",
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_group_page_break(
                path=str(rdl_path),
                tablix_name="NoSuchTablix",
                group_name="Details",
                location="End",
            )

    def test_pagebreak_placed_before_filters(self, rdl_with_region_group):
        # If a Group has both PageBreak and Filters, PageBreak must come
        # FIRST (RDL XSD order). Inject a Filters block then call the
        # tool and assert the document order.
        doc = RDLDocument.open(rdl_with_region_group)
        g = resolve_group(doc, "MainTable", "Region")
        # Insert a Filters block after GroupExpressions.
        ge = find_child(g, "GroupExpressions")
        filters = etree.Element(q("Filters"))
        if ge is not None:
            ge.addnext(filters)
        else:
            g.insert(0, filters)
        doc.save()

        set_group_page_break(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            location="End",
        )

        doc = RDLDocument.open(rdl_with_region_group)
        g = resolve_group(doc, "MainTable", "Region")
        children = [etree.QName(c.tag).localname for c in g]
        assert children.index("PageBreak") < children.index("Filters")


# ---- set_repeat_on_new_page ---------------------------------------------


class TestSetRepeatOnNewPage:
    def test_writes_true(self, rdl_with_region_group):
        result = set_repeat_on_new_page(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            repeat=True,
        )
        assert result == {
            "tablix": "MainTable",
            "group": "Region",
            "kind": "TablixMember",
            "repeat": True,
            "changed": True,
        }
        assert _read_repeat(rdl_with_region_group, "MainTable", "Region") == "true"

    def test_false_removes_block(self, rdl_with_region_group):
        set_repeat_on_new_page(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            repeat=True,
        )
        result = set_repeat_on_new_page(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            repeat=False,
        )
        assert result["changed"] is True
        assert _read_repeat(rdl_with_region_group, "MainTable", "Region") is None

    def test_false_on_clean_member_is_noop(self, rdl_with_region_group):
        result = set_repeat_on_new_page(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            repeat=False,
        )
        assert result["changed"] is False

    def test_idempotent_true(self, rdl_with_region_group):
        set_repeat_on_new_page(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            repeat=True,
        )
        result = set_repeat_on_new_page(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            repeat=True,
        )
        assert result["changed"] is False

    def test_unknown_group_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_repeat_on_new_page(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="NoSuchGroup",
                repeat=True,
            )

    def test_member_child_order_respected(self, rdl_with_region_group):
        # Add a KeepWithGroup to the member so the test exercises
        # _insert_member_child's ordering logic.
        doc = RDLDocument.open(rdl_with_region_group)
        g = resolve_group(doc, "MainTable", "Region")
        member = g.getparent()
        # Manually inject a KeepWithGroup BEFORE Group (where it belongs).
        # _insert_member_child should put RepeatOnNewPage between
        # KeepWithGroup and Group.
        existing_kwg = find_child(member, "KeepWithGroup")
        if existing_kwg is None:
            kwg = etree.Element(q("KeepWithGroup"))
            kwg.text = "After"
            member.insert(0, kwg)
        doc.save()

        set_repeat_on_new_page(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            repeat=True,
        )

        doc = RDLDocument.open(rdl_with_region_group)
        g = resolve_group(doc, "MainTable", "Region")
        member = g.getparent()
        children = [etree.QName(c.tag).localname for c in member]
        assert children.index("KeepWithGroup") < children.index("RepeatOnNewPage")
        assert children.index("RepeatOnNewPage") < children.index("Group")


# ---- registration ------------------------------------------------------


class TestToolRegistration:
    def test_pagination_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "set_group_page_break" in names
        assert "set_repeat_on_new_page" in names
