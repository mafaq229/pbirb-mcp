"""Tests for positioning tools (v0.2 commits 6-8)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child
from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.header_footer import add_header_textbox, set_page_header
from pbirb_mcp.ops.positioning import (
    set_body_item_position,
    set_body_item_size,
    set_footer_item_position,
    set_header_item_position,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _body(doc: RDLDocument):
    return doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body")


def _named(container, name):
    items = find_child(container, "ReportItems")
    if items is None:
        return None
    for el in items:
        if el.get("Name") == name:
            return el
    return None


# ---- set_body_item_position ----------------------------------------------


class TestSetBodyItemPosition:
    def test_moves_existing_tablix(self, rdl_path):
        # Fixture has a Tablix named "MainTable" with Top=0.5in.
        set_body_item_position(
            path=str(rdl_path),
            name="MainTable",
            top="2cm",
            left="0.25in",
        )
        body = _body(RDLDocument.open(rdl_path))
        item = _named(body, "MainTable")
        assert find_child(item, "Top").text == "2cm"
        assert find_child(item, "Left").text == "0.25in"

    def test_unknown_item_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_body_item_position(
                path=str(rdl_path),
                name="NoSuch",
                top="0in",
                left="0in",
            )

    def test_round_trip_safe(self, rdl_path):
        set_body_item_position(
            path=str(rdl_path),
            name="MainTable",
            top="3in",
            left="0.5in",
        )
        RDLDocument.open(rdl_path).validate()


# ---- set_header_item_position --------------------------------------------


class TestSetHeaderItemPosition:
    def test_moves_existing_header_textbox(self, rdl_path):
        set_page_header(
            path=str(rdl_path),
            height="0.5in",
            print_on_first_page=True,
            print_on_last_page=True,
        )
        add_header_textbox(
            path=str(rdl_path),
            name="HeaderTitle",
            text="Hello",
            top="0in",
            left="0in",
            width="2in",
            height="0.25in",
        )
        set_header_item_position(
            path=str(rdl_path),
            name="HeaderTitle",
            top="0.1in",
            left="1in",
        )
        doc = RDLDocument.open(rdl_path)
        page = doc.root.find(f".//{{{RDL_NS}}}Page")
        header = find_child(page, "PageHeader")
        item = _named(header, "HeaderTitle")
        assert find_child(item, "Top").text == "0.1in"
        assert find_child(item, "Left").text == "1in"

    def test_no_header_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_header_item_position(
                path=str(rdl_path),
                name="X",
                top="0",
                left="0",
            )


# ---- set_footer_item_position --------------------------------------------


class TestSetFooterItemPosition:
    def test_no_footer_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_footer_item_position(
                path=str(rdl_path),
                name="X",
                top="0",
                left="0",
            )


# ---- set_body_item_size --------------------------------------------------


class TestSetBodyItemSize:
    def test_resizes_width_only(self, rdl_path):
        set_body_item_size(
            path=str(rdl_path),
            name="MainTable",
            width="6in",
        )
        body = _body(RDLDocument.open(rdl_path))
        item = _named(body, "MainTable")
        assert find_child(item, "Width").text == "6in"
        # Existing height (0.5in in fixture) is preserved.
        assert find_child(item, "Height").text == "0.5in"

    def test_resizes_height_only(self, rdl_path):
        set_body_item_size(
            path=str(rdl_path),
            name="MainTable",
            height="2in",
        )
        body = _body(RDLDocument.open(rdl_path))
        item = _named(body, "MainTable")
        assert find_child(item, "Height").text == "2in"

    def test_resizes_both(self, rdl_path):
        set_body_item_size(
            path=str(rdl_path),
            name="MainTable",
            width="6in",
            height="3in",
        )
        body = _body(RDLDocument.open(rdl_path))
        item = _named(body, "MainTable")
        assert find_child(item, "Width").text == "6in"
        assert find_child(item, "Height").text == "3in"

    def test_no_args_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            set_body_item_size(path=str(rdl_path), name="MainTable")

    def test_unknown_item_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_body_item_size(
                path=str(rdl_path),
                name="NoSuch",
                width="1in",
            )

    def test_round_trip_safe(self, rdl_path):
        # Add a body textbox, then move + resize it.
        add_body_textbox(
            path=str(rdl_path),
            name="Confidential",
            text="CONFIDENTIAL",
            top="0in",
            left="0in",
            width="2in",
            height="0.25in",
        )
        set_body_item_position(path=str(rdl_path), name="Confidential", top="0.1in", left="1in")
        set_body_item_size(path=str(rdl_path), name="Confidential", width="3in", height="0.5in")
        RDLDocument.open(rdl_path).validate()


# ---- registration --------------------------------------------------------


class TestIdempotency:
    def test_set_body_item_position_changed_true_on_move(self, rdl_path):
        result = set_body_item_position(
            path=str(rdl_path),
            name="MainTable",
            top="2cm",
            left="0in",
        )
        assert result["changed"] is True

    def test_set_body_item_position_changed_false_on_no_op(self, rdl_path):
        # First move sets the position; the second move with the same args
        # is a no-op and should report changed=False without rewriting the
        # file.
        set_body_item_position(
            path=str(rdl_path),
            name="MainTable",
            top="2cm",
            left="0in",
        )
        mtime_before = Path(rdl_path).stat().st_mtime_ns
        result = set_body_item_position(
            path=str(rdl_path),
            name="MainTable",
            top="2cm",
            left="0in",
        )
        assert result["changed"] is False
        # File mtime is unchanged — we didn't rewrite the document.
        assert Path(rdl_path).stat().st_mtime_ns == mtime_before

    def test_set_body_item_size_changed_false_on_no_op(self, rdl_path):
        # Read the existing tablix size first so we know what's a no-op.
        doc = RDLDocument.open(rdl_path)
        tablix = _named(_body(doc), "MainTable")
        current_width = find_child(tablix, "Width").text
        result = set_body_item_size(
            path=str(rdl_path),
            name="MainTable",
            width=current_width,
        )
        assert result["changed"] is False


class TestToolRegistration:
    def test_all_four_tools_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert {
            "set_body_item_position",
            "set_header_item_position",
            "set_footer_item_position",
            "set_body_item_size",
        }.issubset(server._tools.keys())
