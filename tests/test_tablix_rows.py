"""Detail-row visibility and row-height tool tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.tablix import set_detail_row_visibility, set_row_height
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _details_member(rdl_path: Path):
    doc = RDLDocument.open(rdl_path)
    tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
    for m in tablix.iter(q("TablixMember")):
        g = find_child(m, "Group")
        if g is not None and g.get("Name") == "Details":
            return m
    return None


# ---- set_detail_row_visibility --------------------------------------------


class TestSetDetailRowVisibility:
    def test_writes_visibility_on_details_member(self, rdl_path):
        set_detail_row_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!Amount.Value < 100",
        )
        details = _details_member(rdl_path)
        vis = find_child(details, "Visibility")
        assert vis is not None
        assert find_child(vis, "Hidden").text == "=Fields!Amount.Value < 100"
        assert find_child(vis, "ToggleItem") is None

    def test_with_toggle_textbox(self, rdl_path):
        set_detail_row_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="true",
            toggle_textbox="HeaderAmount",
        )
        details = _details_member(rdl_path)
        vis = find_child(details, "Visibility")
        assert find_child(vis, "ToggleItem").text == "HeaderAmount"

    def test_replaces_existing_visibility(self, rdl_path):
        set_detail_row_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=A",
        )
        set_detail_row_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=B",
        )
        details = _details_member(rdl_path)
        # Exactly one Visibility node, with the new expression.
        visibilities = find_children(details, "Visibility")
        assert len(visibilities) == 1
        assert find_child(visibilities[0], "Hidden").text == "=B"

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_detail_row_visibility(
                path=str(rdl_path),
                tablix_name="NoSuch",
                expression="=true",
            )

    def test_tablix_without_details_group_raises(self, rdl_path):
        # Mutate the fixture to drop the Details group.
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        for m in list(tablix.iter(q("TablixMember"))):
            g = find_child(m, "Group")
            if g is not None and g.get("Name") == "Details":
                m.getparent().remove(m)
        doc.save()
        with pytest.raises(ElementNotFoundError):
            set_detail_row_visibility(
                path=str(rdl_path),
                tablix_name="MainTable",
                expression="=true",
            )


# ---- set_row_height -------------------------------------------------------


class TestSetRowHeight:
    def test_changes_existing_height(self, rdl_path):
        set_row_height(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=1,
            height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        rows = tablix.findall(
            f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}"
        )
        assert find_child(rows[1], "Height").text == "0.5in"
        # Other rows untouched.
        assert find_child(rows[0], "Height").text == "0.25in"

    def test_accepts_metric_units(self, rdl_path):
        set_row_height(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=0,
            height="1.2cm",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        rows = tablix.findall(
            f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}"
        )
        assert find_child(rows[0], "Height").text == "1.2cm"

    def test_invalid_row_index_raises(self, rdl_path):
        with pytest.raises(IndexError):
            set_row_height(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_index=42,
                height="0.5in",
            )

    def test_negative_row_index_raises(self, rdl_path):
        with pytest.raises(IndexError):
            set_row_height(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_index=-1,
                height="0.5in",
            )

    def test_empty_height_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            set_row_height(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_index=0,
                height="",
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_row_height(
                path=str(rdl_path),
                tablix_name="NoSuch",
                row_index=0,
                height="0.5in",
            )


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_row_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {"set_detail_row_visibility", "set_row_height"} <= names
