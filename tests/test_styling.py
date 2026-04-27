"""Textbox-styling tool tests.

``set_textbox_style`` writes properties across the three Style nodes
nested inside a Textbox:

- ``Textbox/Style``         — box-level (BackgroundColor, Border, VerticalAlign)
- ``Paragraph/Style``       — paragraph-level (TextAlign)
- ``TextRun/Style``         — run-level (FontFamily, FontSize, FontWeight, Color, Format)

Each Property is independently optional; only fields the caller passes
are written, leaving everything else untouched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.styling import set_textbox_style
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _textbox(rdl_path: Path, name: str):
    doc = RDLDocument.open(rdl_path)
    return doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='{name}']")


# ---- box-level properties -------------------------------------------------


class TestBoxLevelProps:
    def test_background_color_lands_on_textbox_style(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#003366",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        outer_style = find_child(tb, "Style")
        bg = find_child(outer_style, "BackgroundColor")
        assert bg is not None and bg.text == "#003366"

    def test_border_props_land_on_textbox_style_border(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            border_style="Solid",
            border_color="#000000",
            border_width="1pt",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        border = tb.find(f"{q('Style')}/{q('Border')}")
        assert border is not None
        assert find_child(border, "Style").text == "Solid"
        assert find_child(border, "Color").text == "#000000"
        assert find_child(border, "Width").text == "1pt"

    def test_vertical_align_lands_on_textbox_style(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="Amount",
            vertical_align="Middle",
        )
        tb = _textbox(rdl_path, "Amount")
        va = tb.find(f"{q('Style')}/{q('VerticalAlign')}")
        assert va is not None and va.text == "Middle"


# ---- paragraph-level properties ------------------------------------------


class TestParagraphLevelProps:
    def test_text_align_lands_on_paragraph_style(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="Amount",
            text_align="Right",
        )
        tb = _textbox(rdl_path, "Amount")
        # Paragraph/Style/TextAlign — NOT Textbox/Style/TextAlign.
        ta = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('Style')}/{q('TextAlign')}"
        )
        assert ta is not None and ta.text == "Right"
        # And specifically NOT on Textbox/Style.
        outer = tb.find(f"{q('Style')}/{q('TextAlign')}")
        assert outer is None


# ---- run-level properties (font, color, format) -------------------------


class TestRunLevelProps:
    def test_font_props_land_on_textrun_style(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            font_family="Segoe UI",
            font_size="11pt",
            font_weight="Bold",
            color="#FFFFFF",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        run_style = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/"
            f"{q('TextRun')}/{q('Style')}"
        )
        assert find_child(run_style, "FontFamily").text == "Segoe UI"
        assert find_child(run_style, "FontSize").text == "11pt"
        assert find_child(run_style, "FontWeight").text == "Bold"
        assert find_child(run_style, "Color").text == "#FFFFFF"

    def test_format_lands_on_textrun_style(self, rdl_path):
        # Fixture's Amount cell already has a Format — overwriting it is the
        # canonical "change number format" workflow.
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="Amount",
            format="C2",
        )
        tb = _textbox(rdl_path, "Amount")
        fmt = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/"
            f"{q('TextRun')}/{q('Style')}/{q('Format')}"
        )
        assert fmt is not None and fmt.text == "C2"


# ---- partial-update / no-op behaviour ------------------------------------


class TestPartialUpdate:
    def test_only_specified_props_are_written(self, rdl_path):
        # Set background only; verify other Style children are unchanged.
        before_outer = _textbox(rdl_path, "HeaderProductID").find(q("Style"))
        before_children = [etree_node.tag for etree_node in list(before_outer)]
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#003366",
        )
        outer = _textbox(rdl_path, "HeaderProductID").find(q("Style"))
        # Old children still there, BackgroundColor newly added.
        for child_tag in before_children:
            assert any(c.tag == child_tag for c in outer)
        assert any(c.tag.endswith("}BackgroundColor") for c in outer)

    def test_no_args_is_no_op(self, rdl_path):
        before = rdl_path.read_bytes()
        set_textbox_style(path=str(rdl_path), textbox_name="HeaderProductID")
        assert rdl_path.read_bytes() == before

    def test_repeat_set_overwrites_in_place(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#111111",
        )
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#222222",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        bgs = tb.findall(f"{q('Style')}/{q('BackgroundColor')}")
        # Exactly one BackgroundColor element with the latest value.
        assert len(bgs) == 1
        assert bgs[0].text == "#222222"


# ---- error paths ----------------------------------------------------------


class TestErrors:
    def test_unknown_textbox_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_textbox_style(
                path=str(rdl_path),
                textbox_name="NoSuchBox",
                background_color="#000",
            )

    def test_round_trip_safe(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#003366",
            font_weight="Bold",
            color="#FFFFFF",
            text_align="Center",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "set_textbox_style" in names
