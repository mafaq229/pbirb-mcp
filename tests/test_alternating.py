"""Alternating-row-color tool tests.

``set_alternating_row_color`` walks the tablix row hierarchy to find the
``Details`` leaf, computes its body-row index in depth-first traversal
order, then writes a BackgroundColor IIf-expression on every Textbox in
that row's cells.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.styling import set_alternating_row_color, set_textbox_style
from pbirb_mcp.ops.tablix import add_row_group
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _detail_row_textboxes(rdl_path: Path):
    """Return all Textboxes in the detail row of MainTable, in column order.

    For the bare fixture this is row index 1; helper figures it out via
    the same depth-first leaf walk the tool uses, so tests don't hardcode
    indices.
    """
    doc = RDLDocument.open(rdl_path)
    tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
    rh = find_child(tablix, "TablixRowHierarchy")
    members_root = find_child(rh, "TablixMembers")

    leaf_idx = [0]
    detail_idx = [None]

    def walk(member):
        children = find_child(member, "TablixMembers")
        leaves = list(children) if children is not None else []
        if not leaves:
            group = find_child(member, "Group")
            if group is not None and group.get("Name") == "Details":
                detail_idx[0] = leaf_idx[0]
            leaf_idx[0] += 1
            return
        for sub in leaves:
            walk(sub)

    for m in list(members_root):
        walk(m)

    rows = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")
    detail_row = rows[detail_idx[0]]
    return [
        cell.find(f"{q('CellContents')}/{q('Textbox')}")
        for cell in detail_row.findall(f"{q('TablixCells')}/{q('TablixCell')}")
    ]


# ---- happy path on basic fixture ------------------------------------------


class TestBasicFixture:
    def test_writes_iif_expression_to_every_detail_cell(self, rdl_path):
        set_alternating_row_color(
            path=str(rdl_path),
            tablix_name="MainTable",
            color_a="#F2F2F2",
            color_b="#FFFFFF",
        )
        for tb in _detail_row_textboxes(rdl_path):
            bg = tb.find(f"{q('Style')}/{q('BackgroundColor')}")
            assert bg is not None
            assert bg.text == ('=IIf(RowNumber(Nothing) Mod 2, "#F2F2F2", "#FFFFFF")')

    def test_does_not_touch_header_row_cells(self, rdl_path):
        set_alternating_row_color(
            path=str(rdl_path),
            tablix_name="MainTable",
            color_a="#F2F2F2",
            color_b="#FFFFFF",
        )
        # Header textboxes (row 0) shouldn't have BackgroundColor set.
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        rows = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")
        header_textboxes = [
            cell.find(f"{q('CellContents')}/{q('Textbox')}")
            for cell in rows[0].findall(f"{q('TablixCells')}/{q('TablixCell')}")
        ]
        for tb in header_textboxes:
            bg = tb.find(f"{q('Style')}/{q('BackgroundColor')}")
            assert bg is None or bg.text != ('=IIf(RowNumber(Nothing) Mod 2, "#F2F2F2", "#FFFFFF")')

    def test_overwrites_existing_background_color(self, rdl_path):
        # Pre-stamp one detail textbox with a static colour, then verify
        # the alternating call replaces (not duplicates) it.
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="Amount",
            background_color="#000000",
        )
        set_alternating_row_color(
            path=str(rdl_path),
            tablix_name="MainTable",
            color_a="#A",
            color_b="#B",
        )
        doc = RDLDocument.open(rdl_path)
        tb = doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='Amount']")
        bgs = tb.findall(f"{q('Style')}/{q('BackgroundColor')}")
        assert len(bgs) == 1
        assert bgs[0].text == '=IIf(RowNumber(Nothing) Mod 2, "#A", "#B")'


# ---- detail-row tracks group structure ------------------------------------


class TestWithGroups:
    def test_after_add_row_group_targets_correct_row(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!ProductName.Value",
        )
        set_alternating_row_color(
            path=str(rdl_path),
            tablix_name="MainTable",
            color_a="#A",
            color_b="#B",
        )
        # Detail textboxes (now at body row index 2) should have the IIf.
        for tb in _detail_row_textboxes(rdl_path):
            bg = tb.find(f"{q('Style')}/{q('BackgroundColor')}")
            assert bg is not None
            assert bg.text == '=IIf(RowNumber(Nothing) Mod 2, "#A", "#B")'

        # Group-header textboxes (row 0) must NOT have it.
        doc = RDLDocument.open(rdl_path)
        gh_tb = doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='Region_Header_0']")
        gh_bg = gh_tb.find(f"{q('Style')}/{q('BackgroundColor')}")
        assert gh_bg is None or "#A" not in (gh_bg.text or "")


# ---- error paths ----------------------------------------------------------


class TestErrors:
    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_alternating_row_color(
                path=str(rdl_path),
                tablix_name="NoSuch",
                color_a="#A",
                color_b="#B",
            )

    def test_tablix_without_details_group_raises(self, rdl_path):
        # Strip the Details group from the fixture and confirm we refuse
        # rather than guessing which row is "detail".
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        for m in list(tablix.iter(q("TablixMember"))):
            g = find_child(m, "Group")
            if g is not None and g.get("Name") == "Details":
                m.getparent().remove(m)
        doc.save()
        with pytest.raises(ElementNotFoundError):
            set_alternating_row_color(
                path=str(rdl_path),
                tablix_name="MainTable",
                color_a="#A",
                color_b="#B",
            )

    def test_round_trip_safe(self, rdl_path):
        set_alternating_row_color(
            path=str(rdl_path),
            tablix_name="MainTable",
            color_a="#F2F2F2",
            color_b="#FFFFFF",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "set_alternating_row_color" in names
