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
from pbirb_mcp.ops.layout import (
    add_list,
    add_rectangle,
    set_group_page_break,
    set_keep_together,
    set_keep_with_group,
    set_repeat_on_new_page,
)
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


# ---- set_keep_together --------------------------------------------------


def _read_keep_together(path: Path, item_name: str) -> str | None:
    doc = RDLDocument.open(path)
    matches = doc.root.xpath(
        ".//r:ReportItems/r:*[@Name=$n]",
        namespaces={"r": doc.root.nsmap[None] if None in doc.root.nsmap else "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"},
        n=item_name,
    )
    if not matches:
        return None
    kt = find_child(matches[0], "KeepTogether")
    return kt.text if kt is not None else None


class TestSetKeepTogether:
    def test_writes_true_on_tablix(self, rdl_path):
        result = set_keep_together(
            path=str(rdl_path), name="MainTable", keep=True
        )
        assert result == {
            "name": "MainTable",
            "kind": "Tablix",
            "keep": True,
            "changed": True,
        }
        assert _read_keep_together(rdl_path, "MainTable") == "true"

    def test_inserted_before_tablix_body(self, rdl_path):
        # KeepTogether must come before TablixCorner / TablixBody per
        # RDL 2016 XSD.
        set_keep_together(path=str(rdl_path), name="MainTable", keep=True)
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.xpath(
            ".//r:Tablix[@Name='MainTable']", namespaces={"r": "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"}
        )[0]
        children = [etree.QName(c.tag).localname for c in tablix]
        assert children.index("KeepTogether") < children.index("TablixBody")

    def test_writes_true_on_textbox(self, rdl_path):
        # add_body_textbox bootstraps with KeepTogether=true already
        # (the canonical Report Builder shape), so toggle off then on
        # to exercise both the existing-element and insertion paths.
        from pbirb_mcp.ops.body import add_body_textbox

        add_body_textbox(
            path=str(rdl_path),
            name="MyBodyText",
            text="Hello",
            top="3in",
            left="0.5in",
            width="2in",
            height="0.3in",
        )
        # Off → removes the element.
        off = set_keep_together(
            path=str(rdl_path), name="MyBodyText", keep=False
        )
        assert off["kind"] == "Textbox"
        assert off["changed"] is True
        # On → re-inserts via _set_textbox_direct_child.
        on = set_keep_together(
            path=str(rdl_path), name="MyBodyText", keep=True
        )
        assert on["kind"] == "Textbox"
        assert on["changed"] is True

    def test_false_removes_element(self, rdl_path):
        set_keep_together(path=str(rdl_path), name="MainTable", keep=True)
        result = set_keep_together(
            path=str(rdl_path), name="MainTable", keep=False
        )
        assert result["changed"] is True
        assert _read_keep_together(rdl_path, "MainTable") is None

    def test_false_on_clean_is_noop(self, rdl_path):
        result = set_keep_together(
            path=str(rdl_path), name="MainTable", keep=False
        )
        assert result["changed"] is False

    def test_idempotent_true(self, rdl_path):
        set_keep_together(path=str(rdl_path), name="MainTable", keep=True)
        result = set_keep_together(
            path=str(rdl_path), name="MainTable", keep=True
        )
        assert result["changed"] is False

    def test_unknown_name_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_keep_together(path=str(rdl_path), name="NoSuchItem", keep=True)

    def test_image_refused(self, rdl_path):
        # Add a body Image (KeepTogether is not in the Image XSD).
        from pbirb_mcp.ops.body import add_body_image

        add_body_image(
            path=str(rdl_path),
            name="MyImage",
            image_source="External",
            value="http://example.com/x.png",
            top="0in",
            left="0in",
            width="1in",
            height="1in",
        )
        with pytest.raises(ValueError, match="does not support"):
            set_keep_together(path=str(rdl_path), name="MyImage", keep=True)


# ---- set_keep_with_group ------------------------------------------------


def _read_keep_with_group(path: Path, tablix: str, group: str) -> str | None:
    doc = RDLDocument.open(path)
    g = resolve_group(doc, tablix, group)
    kwg = find_child(g.getparent(), "KeepWithGroup")
    return kwg.text if kwg is not None else None


class TestSetKeepWithGroup:
    def test_writes_after(self, rdl_with_region_group):
        result = set_keep_with_group(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            value="After",
        )
        assert result == {
            "tablix": "MainTable",
            "group": "Region",
            "kind": "TablixMember",
            "value": "After",
            "changed": True,
        }
        assert _read_keep_with_group(rdl_with_region_group, "MainTable", "Region") == "After"

    def test_writes_before(self, rdl_with_region_group):
        result = set_keep_with_group(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            value="Before",
        )
        assert _read_keep_with_group(rdl_with_region_group, "MainTable", "Region") == "Before"

    def test_replaces_existing(self, rdl_with_region_group):
        set_keep_with_group(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            value="After",
        )
        result = set_keep_with_group(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            value="Before",
        )
        assert result["changed"] is True
        assert _read_keep_with_group(rdl_with_region_group, "MainTable", "Region") == "Before"

    def test_none_removes_block(self, rdl_with_region_group):
        set_keep_with_group(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            value="After",
        )
        result = set_keep_with_group(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            value="None",
        )
        assert result["changed"] is True
        assert _read_keep_with_group(rdl_with_region_group, "MainTable", "Region") is None

    def test_idempotent(self, rdl_with_region_group):
        set_keep_with_group(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            value="After",
        )
        result = set_keep_with_group(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            value="After",
        )
        assert result["changed"] is False

    def test_unknown_value_rejected(self, rdl_with_region_group):
        with pytest.raises(ValueError, match="unknown KeepWithGroup"):
            set_keep_with_group(
                path=str(rdl_with_region_group),
                tablix_name="MainTable",
                group_name="Region",
                value="Sideways",
            )

    def test_member_child_order_respected(self, rdl_with_region_group):
        # KeepWithGroup must come before Group (and before
        # RepeatOnNewPage if present, since the schema order is
        # KeepWithGroup, RepeatOnNewPage, FixedData, Group, ...).
        set_repeat_on_new_page(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            repeat=True,
        )
        set_keep_with_group(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            value="After",
        )
        doc = RDLDocument.open(rdl_with_region_group)
        g = resolve_group(doc, "MainTable", "Region")
        children = [etree.QName(c.tag).localname for c in g.getparent()]
        assert children.index("KeepWithGroup") < children.index("RepeatOnNewPage")
        assert children.index("RepeatOnNewPage") < children.index("Group")


# ---- add_rectangle ------------------------------------------------------


_RDL = "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"


def _body_report_items(path: Path) -> etree._Element:
    doc = RDLDocument.open(path)
    body = doc.root.find(f".//{{{_RDL}}}Body")
    return body.find(q("ReportItems"))


def _rectangle(path: Path, name: str) -> etree._Element | None:
    doc = RDLDocument.open(path)
    matches = doc.root.xpath(
        f".//r:Rectangle[@Name=$n]", namespaces={"r": _RDL}, n=name
    )
    return matches[0] if matches else None


class TestAddRectangle:
    def test_creates_empty_rectangle(self, rdl_path):
        result = add_rectangle(
            path=str(rdl_path),
            name="EmptyFrame",
            top="3in",
            left="0.5in",
            width="2in",
            height="1in",
        )
        assert result == {
            "name": "EmptyFrame",
            "kind": "Rectangle",
            "moved": [],
        }
        rect = _rectangle(rdl_path, "EmptyFrame")
        assert rect is not None
        # Empty rectangle: no <ReportItems> child.
        assert find_child(rect, "ReportItems") is None
        assert find_child(rect, "Top").text == "3in"
        assert find_child(rect, "Width").text == "2in"

    def test_refuses_duplicate_name(self, rdl_path):
        # MainTable already exists in the fixture's body.
        with pytest.raises(ValueError, match="already exists"):
            add_rectangle(
                path=str(rdl_path),
                name="MainTable",
                top="3in",
                left="0.5in",
                width="2in",
                height="1in",
            )

    def test_refuses_unknown_contained_item(self, rdl_path):
        with pytest.raises(ElementNotFoundError, match="not in"):
            add_rectangle(
                path=str(rdl_path),
                name="Frame",
                top="3in",
                left="0.5in",
                width="2in",
                height="1in",
                contained_items=["NoSuchThing"],
            )

    def test_moves_named_item_into_rectangle(self, rdl_path):
        # Add a body textbox first, then wrap it in a rectangle.
        from pbirb_mcp.ops.body import add_body_textbox

        add_body_textbox(
            path=str(rdl_path),
            name="Inner",
            text="x",
            top="3.5in",
            left="1in",
            width="1in",
            height="0.3in",
        )
        # Body had MainTable + Inner. After wrapping, body has
        # MainTable + Frame; Frame contains Inner.
        result = add_rectangle(
            path=str(rdl_path),
            name="Frame",
            top="3in",
            left="0.5in",
            width="2in",
            height="1in",
            contained_items=["Inner"],
        )
        assert result["moved"] == ["Inner"]
        body_items = _body_report_items(rdl_path)
        body_names = {c.get("Name") for c in body_items}
        assert body_names == {"MainTable", "Frame"}
        # Inner now lives inside Frame's <ReportItems>.
        rect = _rectangle(rdl_path, "Frame")
        rect_items = find_child(rect, "ReportItems")
        assert rect_items is not None
        rect_names = {c.get("Name") for c in rect_items}
        assert rect_names == {"Inner"}

    def test_recalculates_child_top_left_to_local_coords(self, rdl_path):
        from pbirb_mcp.ops.body import add_body_textbox

        # Inner at body coord (3.5in, 1in). Frame at (3in, 0.5in).
        # Local coord should become (0.5in, 0.5in).
        add_body_textbox(
            path=str(rdl_path),
            name="Inner",
            text="x",
            top="3.5in",
            left="1in",
            width="1in",
            height="0.3in",
        )
        add_rectangle(
            path=str(rdl_path),
            name="Frame",
            top="3in",
            left="0.5in",
            width="2in",
            height="1in",
            contained_items=["Inner"],
        )
        rect = _rectangle(rdl_path, "Frame")
        inner = rect.find(q("ReportItems")).find(q("Textbox"))
        assert find_child(inner, "Top").text == "0.5in"
        assert find_child(inner, "Left").text == "0.5in"

    def test_position_preserved_when_units_differ(self, rdl_path):
        # Manually inject a body textbox using points (rare but valid).
        from pbirb_mcp.ops.body import add_body_textbox

        add_body_textbox(
            path=str(rdl_path),
            name="Inner",
            text="x",
            top="216pt",  # 3in
            left="72pt",  # 1in
            width="1in",
            height="0.3in",
        )
        add_rectangle(
            path=str(rdl_path),
            name="Frame",
            top="2in",
            left="0.5in",
            width="2in",
            height="1in",
            contained_items=["Inner"],
        )
        rect = _rectangle(rdl_path, "Frame")
        inner = rect.find(q("ReportItems")).find(q("Textbox"))
        # Frame's unit is "in", so Inner's coords are converted.
        # 216pt - 2in = 3in - 2in = 1in
        # 72pt  - 0.5in = 1in - 0.5in = 0.5in
        assert find_child(inner, "Top").text == "1in"
        assert find_child(inner, "Left").text == "0.5in"

    def test_multiple_items_moved(self, rdl_path):
        from pbirb_mcp.ops.body import add_body_textbox

        for i, n in enumerate(("A", "B")):
            add_body_textbox(
                path=str(rdl_path),
                name=n,
                text=n,
                top=f"{3 + i * 0.5}in",
                left="1in",
                width="1in",
                height="0.3in",
            )
        add_rectangle(
            path=str(rdl_path),
            name="Frame",
            top="3in",
            left="0.5in",
            width="2in",
            height="2in",
            contained_items=["A", "B"],
        )
        rect = _rectangle(rdl_path, "Frame")
        names = {c.get("Name") for c in rect.find(q("ReportItems"))}
        assert names == {"A", "B"}


# ---- add_list -----------------------------------------------------------


def _tablix(path: Path, name: str) -> etree._Element | None:
    doc = RDLDocument.open(path)
    matches = doc.root.xpath(
        f".//r:Tablix[@Name=$n]", namespaces={"r": _RDL}, n=name
    )
    return matches[0] if matches else None


class TestAddList:
    def test_creates_list_at_position(self, rdl_path):
        result = add_list(
            path=str(rdl_path),
            name="MyList",
            dataset_name="MainDataset",
            top="3in",
            left="0.5in",
            width="2in",
            height="1in",
        )
        assert result == {
            "name": "MyList",
            "kind": "Tablix",
            "dataset": "MainDataset",
            "rectangle": "MyList_Rect",
        }
        tablix = _tablix(rdl_path, "MyList")
        assert tablix is not None
        assert find_child(tablix, "DataSetName").text == "MainDataset"
        assert find_child(tablix, "Top").text == "3in"

    def test_inner_rectangle_present(self, rdl_path):
        add_list(
            path=str(rdl_path),
            name="MyList",
            dataset_name="MainDataset",
            top="3in",
            left="0.5in",
            width="2in",
            height="1in",
        )
        rect = _rectangle(rdl_path, "MyList_Rect")
        assert rect is not None
        # Rectangle lives inside the Tablix's single cell.
        # Walk: Tablix/TablixBody/TablixRows/TablixRow/TablixCells/TablixCell/CellContents/Rectangle
        ancestor_names = []
        cur = rect.getparent()
        while cur is not None:
            ancestor_names.append(etree.QName(cur.tag).localname)
            cur = cur.getparent()
        assert "TablixCell" in ancestor_names
        assert "Tablix" in ancestor_names

    def test_single_row_single_column(self, rdl_path):
        add_list(
            path=str(rdl_path),
            name="MyList",
            dataset_name="MainDataset",
            top="3in",
            left="0.5in",
            width="2in",
            height="1in",
        )
        tablix = _tablix(rdl_path, "MyList")
        body_node = find_child(tablix, "TablixBody")
        cols = find_child(body_node, "TablixColumns")
        rows = find_child(body_node, "TablixRows")
        assert len(cols.findall(q("TablixColumn"))) == 1
        assert len(rows.findall(q("TablixRow"))) == 1

    def test_details_group_named_with_list_prefix(self, rdl_path):
        # Group Name should be "<list_name>_Details" for collision-safe
        # multi-list reports.
        add_list(
            path=str(rdl_path),
            name="MyList",
            dataset_name="MainDataset",
            top="3in",
            left="0.5in",
            width="2in",
            height="1in",
        )
        tablix = _tablix(rdl_path, "MyList")
        rh = find_child(tablix, "TablixRowHierarchy")
        members = rh.find(q("TablixMembers"))
        groups = members.iter(q("Group"))
        names = [g.get("Name") for g in groups]
        assert names == ["MyList_Details"]

    def test_two_lists_dont_collide(self, rdl_path):
        add_list(
            path=str(rdl_path),
            name="ListA",
            dataset_name="MainDataset",
            top="3in",
            left="0.5in",
            width="2in",
            height="1in",
        )
        add_list(
            path=str(rdl_path),
            name="ListB",
            dataset_name="MainDataset",
            top="4.5in",
            left="0.5in",
            width="2in",
            height="1in",
        )
        # Both lists exist; rectangles named distinctly.
        assert _tablix(rdl_path, "ListA") is not None
        assert _tablix(rdl_path, "ListB") is not None
        assert _rectangle(rdl_path, "ListA_Rect") is not None
        assert _rectangle(rdl_path, "ListB_Rect") is not None

    def test_refuses_duplicate_name(self, rdl_path):
        with pytest.raises(ValueError, match="already exists"):
            add_list(
                path=str(rdl_path),
                name="MainTable",  # already exists in fixture
                dataset_name="MainDataset",
                top="3in",
                left="0.5in",
                width="2in",
                height="1in",
            )

    def test_refuses_unknown_dataset(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_list(
                path=str(rdl_path),
                name="MyList",
                dataset_name="NoSuchDataset",
                top="3in",
                left="0.5in",
                width="2in",
                height="1in",
            )

    def test_round_trip_safe(self, rdl_path):
        add_list(
            path=str(rdl_path),
            name="MyList",
            dataset_name="MainDataset",
            top="3in",
            left="0.5in",
            width="2in",
            height="1in",
        )
        # Re-open + structural validate must pass.
        RDLDocument.open(rdl_path).validate()


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
        assert "set_keep_together" in names
        assert "set_keep_with_group" in names

    def test_layout_container_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "add_rectangle" in names
        assert "add_list" in names
