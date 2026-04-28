"""Tests for v0.2 reader extensions: list_*_items, get_textbox/image/rectangle,
extended describe_report and get_tablixes outputs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.ops.body import add_body_image, add_body_textbox
from pbirb_mcp.ops.reader import (
    describe_report,
    get_image,
    get_rectangle,
    get_tablixes,
    get_textbox,
    list_body_items,
    list_footer_items,
    list_header_items,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


# ---- list_body_items / list_header_items / list_footer_items --------------


class TestListBodyItems:
    def test_includes_main_tablix(self, rdl_path):
        items = list_body_items(path=str(rdl_path))
        names = {i["name"] for i in items}
        assert "MainTable" in names
        # Each entry exposes layout fields.
        main = next(i for i in items if i["name"] == "MainTable")
        assert main["type"] == "Tablix"
        assert main["top"] is not None

    def test_picks_up_added_textbox(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="Confidential",
            text="CONFIDENTIAL",
            top="0in",
            left="0in",
            width="2in",
            height="0.25in",
        )
        items = list_body_items(path=str(rdl_path))
        types = {(i["name"], i["type"]) for i in items}
        assert ("Confidential", "Textbox") in types

    def test_no_header_or_footer_returns_empty(self, rdl_path):
        # Fixture has no PageHeader / PageFooter.
        assert list_header_items(path=str(rdl_path)) == []
        assert list_footer_items(path=str(rdl_path)) == []


# ---- describe_report extended ---------------------------------------------


class TestDescribeReportExtended:
    def test_includes_body_items_field(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert "body_items" in out
        names = {i["name"] for i in out["body_items"]}
        assert "MainTable" in names

    def test_header_items_and_footer_items_keys_present(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        # Empty lists for fixture which has no header/footer.
        assert out["header_items"] == []
        assert out["footer_items"] == []


# ---- get_tablixes extended ------------------------------------------------


class TestGetTablixesCells:
    def test_cells_field_present(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))[0]
        assert "cells" in out
        # Fixture has 2 rows × 3 cols = 6 cells.
        assert len(out["cells"]) == 6

    def test_cell_textbox_names_match_fixture(self, rdl_path):
        cells = get_tablixes(path=str(rdl_path))[0]["cells"]
        names = {(c["row"], c["col"]): c["textbox_name"] for c in cells}
        # Header row 0.
        assert names[(0, 0)] == "HeaderProductID"
        assert names[(0, 1)] == "HeaderProductName"
        assert names[(0, 2)] == "HeaderAmount"
        # Data row 1.
        assert names[(1, 0)] == "ProductID"
        assert names[(1, 1)] == "ProductName"
        assert names[(1, 2)] == "Amount"

    def test_default_spans_are_one(self, rdl_path):
        cells = get_tablixes(path=str(rdl_path))[0]["cells"]
        for c in cells:
            assert c["row_span"] == 1
            assert c["col_span"] == 1


# ---- get_textbox / get_image / get_rectangle ------------------------------


class TestGetTextbox:
    def test_returns_runs_for_cell_textbox(self, rdl_path):
        out = get_textbox(path=str(rdl_path), name="HeaderProductID")
        assert out["type"] == "Textbox"
        # Cell-level textboxes have no top/left/width/height.
        assert out["top"] is None
        assert out["runs"]
        assert any("Product ID" in (r["value"] or "") for r in out["runs"])

    def test_returns_layout_for_body_textbox(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="Banner",
            text="BANNER",
            top="0in",
            left="0in",
            width="3in",
            height="0.5in",
        )
        out = get_textbox(path=str(rdl_path), name="Banner")
        assert out["top"] == "0in"
        assert out["width"] == "3in"
        assert out["height"] == "0.5in"

    def test_unknown_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            get_textbox(path=str(rdl_path), name="NoSuch")


class TestGetImage:
    def test_returns_image_metadata(self, rdl_path, tmp_path):
        # Add a body image first; fixture has none.
        png = tmp_path / "img.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)  # minimal-ish header
        # Use add_body_image which only needs source/value, not real file content.
        add_body_image(
            path=str(rdl_path),
            name="Logo",
            image_source="External",
            value="http://example.com/logo.png",
            top="0in",
            left="0in",
            width="1in",
            height="0.5in",
        )
        out = get_image(path=str(rdl_path), name="Logo")
        assert out["type"] == "Image"
        assert out["source"] == "External"
        assert out["value"] == "http://example.com/logo.png"
        assert out["top"] == "0in"

    def test_unknown_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            get_image(path=str(rdl_path), name="NoSuch")


class TestGetRectangle:
    def test_unknown_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            get_rectangle(path=str(rdl_path), name="NoSuch")


# ---- registration --------------------------------------------------------


class TestToolRegistration:
    def test_all_six_v02_reader_tools_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert {
            "list_body_items",
            "list_header_items",
            "list_footer_items",
            "get_textbox",
            "get_image",
            "get_rectangle",
        }.issubset(server._tools.keys())
