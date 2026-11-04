"""Page-header and page-footer tool tests.

Header and footer share identical structure (``<PageHeader>`` /
``<PageFooter>``), so most behavioural tests are parametrised over the two
regions to keep coverage symmetric without duplicating assertions.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops import positioning
from pbirb_mcp.ops.header_footer import (
    add_footer_image,
    add_footer_textbox,
    add_header_image,
    add_header_textbox,
    remove_footer_item,
    remove_header_item,
    set_page_footer,
    set_page_header,
)
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _section(rdl_path: Path, local: str):
    doc = RDLDocument.open(rdl_path)
    return doc.root.find(f".//{{{RDL_NS}}}Page/{{{RDL_NS}}}{local}")


# Parametrised per-region table: (region_name, set_fn, add_textbox, add_image, remove_fn, section_local)
REGIONS = [
    (
        "header",
        set_page_header,
        add_header_textbox,
        add_header_image,
        remove_header_item,
        "PageHeader",
    ),
    (
        "footer",
        set_page_footer,
        add_footer_textbox,
        add_footer_image,
        remove_footer_item,
        "PageFooter",
    ),
]
REGION_IDS = [r[0] for r in REGIONS]


# ---- set_page_header / set_page_footer ------------------------------------


@pytest.mark.parametrize("region,set_fn,_a,_b,_c,section_local", REGIONS, ids=REGION_IDS)
class TestSetPageSection:
    def test_creates_section_when_absent(self, rdl_path, region, set_fn, _a, _b, _c, section_local):
        set_fn(
            path=str(rdl_path),
            height="0.5in",
            print_on_first_page=True,
            print_on_last_page=True,
        )
        sec = _section(rdl_path, section_local)
        assert sec is not None
        assert find_child(sec, "Height").text == "0.5in"
        assert find_child(sec, "PrintOnFirstPage").text == "true"
        assert find_child(sec, "PrintOnLastPage").text == "true"

    def test_updates_existing_section_in_place(
        self, rdl_path, region, set_fn, _a, _b, _c, section_local
    ):
        set_fn(path=str(rdl_path), height="0.5in")
        set_fn(
            path=str(rdl_path),
            height="0.75in",
            print_on_first_page=False,
            print_on_last_page=False,
        )
        sec = _section(rdl_path, section_local)
        assert find_child(sec, "Height").text == "0.75in"
        assert find_child(sec, "PrintOnFirstPage").text == "false"
        assert find_child(sec, "PrintOnLastPage").text == "false"

    def test_partial_update_leaves_other_fields_alone(
        self, rdl_path, region, set_fn, _a, _b, _c, section_local
    ):
        set_fn(
            path=str(rdl_path),
            height="0.5in",
            print_on_first_page=True,
            print_on_last_page=True,
        )
        # Update height only.
        set_fn(path=str(rdl_path), height="1in")
        sec = _section(rdl_path, section_local)
        assert find_child(sec, "Height").text == "1in"
        assert find_child(sec, "PrintOnFirstPage").text == "true"
        assert find_child(sec, "PrintOnLastPage").text == "true"

    def test_round_trip_safe(self, rdl_path, region, set_fn, _a, _b, _c, section_local):
        set_fn(path=str(rdl_path), height="0.5in")
        doc = RDLDocument.open(rdl_path)
        doc.validate()

    def test_returns_changed_list_naming_what_was_written(
        self, rdl_path, region, set_fn, _a, _b, _c, section_local
    ):
        result = set_fn(
            path=str(rdl_path),
            height="0.5in",
            print_on_first_page=True,
        )
        assert result["region"] == region
        assert sorted(result["changed"]) == ["Height", "PrintOnFirstPage"]

    def test_idempotent_no_op_returns_empty_changed(
        self, rdl_path, region, set_fn, _a, _b, _c, section_local
    ):
        set_fn(path=str(rdl_path), height="0.5in", print_on_first_page=True)
        result = set_fn(path=str(rdl_path), height="0.5in", print_on_first_page=True)
        assert result["changed"] == []


# ---- add_*_textbox --------------------------------------------------------


@pytest.mark.parametrize("region,set_fn,add_textbox,_b,_c,section_local", REGIONS, ids=REGION_IDS)
class TestAddTextbox:
    def test_static_text_lands_in_textrun_value(
        self, rdl_path, region, set_fn, add_textbox, _b, _c, section_local
    ):
        set_fn(path=str(rdl_path), height="0.5in")
        add_textbox(
            path=str(rdl_path),
            name="Title",
            text="Quarterly Sales Report",
            top="0in",
            left="0in",
            width="4in",
            height="0.3in",
        )
        sec = _section(rdl_path, section_local)
        report_items = find_child(sec, "ReportItems")
        textbox = report_items.find(f"{{{RDL_NS}}}Textbox[@Name='Title']")
        assert textbox is not None
        value = textbox.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert value.text == "Quarterly Sales Report"

    def test_expression_text_preserves_leading_equals(
        self, rdl_path, region, set_fn, add_textbox, _b, _c, section_local
    ):
        set_fn(path=str(rdl_path), height="0.5in")
        add_textbox(
            path=str(rdl_path),
            name="ParamLine",
            text="=Parameters!DateFrom.Value",
            top="0.4in",
            left="0in",
            width="4in",
            height="0.25in",
        )
        sec = _section(rdl_path, section_local)
        textbox = sec.find(f"{q('ReportItems')}/{q('Textbox')}[@Name='ParamLine']")
        value = textbox.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert value.text == "=Parameters!DateFrom.Value"

    def test_creates_section_with_default_height_when_missing(
        self, rdl_path, region, set_fn, add_textbox, _b, _c, section_local
    ):
        # Caller adds a textbox without first calling set_page_header/footer;
        # we still need a section to host it. The tool auto-creates one.
        add_textbox(
            path=str(rdl_path),
            name="Stamp",
            text="Confidential",
            top="0in",
            left="0in",
            width="2in",
            height="0.3in",
        )
        sec = _section(rdl_path, section_local)
        assert sec is not None
        # Default height present so Report Builder doesn't render a 0in band.
        assert find_child(sec, "Height") is not None

    def test_duplicate_name_in_same_section_rejected(
        self, rdl_path, region, set_fn, add_textbox, _b, _c, section_local
    ):
        add_textbox(
            path=str(rdl_path),
            name="Title",
            text="Hello",
            top="0in",
            left="0in",
            width="2in",
            height="0.3in",
        )
        with pytest.raises(ValueError):
            add_textbox(
                path=str(rdl_path),
                name="Title",
                text="World",
                top="0in",
                left="0in",
                width="2in",
                height="0.3in",
            )


# ---- add_*_image ----------------------------------------------------------


@pytest.mark.parametrize("region,set_fn,_a,add_image,_c,section_local", REGIONS, ids=REGION_IDS)
class TestAddImage:
    def test_external_source_writes_url(
        self, rdl_path, region, set_fn, _a, add_image, _c, section_local
    ):
        set_fn(path=str(rdl_path), height="0.5in")
        add_image(
            path=str(rdl_path),
            name="Logo",
            image_source="External",
            value="https://example.com/logo.png",
            top="0in",
            left="0in",
            width="1in",
            height="0.5in",
        )
        sec = _section(rdl_path, section_local)
        image = sec.find(f"{q('ReportItems')}/{q('Image')}[@Name='Logo']")
        assert image is not None
        assert find_child(image, "Source").text == "External"
        assert find_child(image, "Value").text == "https://example.com/logo.png"

    def test_embedded_source_writes_image_name(
        self, rdl_path, region, set_fn, _a, add_image, _c, section_local
    ):
        set_fn(path=str(rdl_path), height="0.5in")
        add_image(
            path=str(rdl_path),
            name="Logo",
            image_source="Embedded",
            value="LogoImage",
            top="0in",
            left="0in",
            width="1in",
            height="0.5in",
        )
        sec = _section(rdl_path, section_local)
        image = sec.find(f"{q('ReportItems')}/{q('Image')}[@Name='Logo']")
        assert find_child(image, "Source").text == "Embedded"
        assert find_child(image, "Value").text == "LogoImage"

    def test_unknown_image_source_rejected(
        self, rdl_path, region, set_fn, _a, add_image, _c, section_local
    ):
        with pytest.raises(ValueError):
            add_image(
                path=str(rdl_path),
                name="Logo",
                image_source="Magic",
                value="x",
                top="0in",
                left="0in",
                width="1in",
                height="1in",
            )


# ---- remove_*_item --------------------------------------------------------


@pytest.mark.parametrize(
    "region,set_fn,add_textbox,add_image,remove_fn,section_local",
    REGIONS,
    ids=REGION_IDS,
)
class TestRemoveItem:
    def test_removes_named_textbox(
        self, rdl_path, region, set_fn, add_textbox, add_image, remove_fn, section_local
    ):
        add_textbox(
            path=str(rdl_path),
            name="Stamp",
            text="Confidential",
            top="0in",
            left="0in",
            width="2in",
            height="0.3in",
        )
        remove_fn(path=str(rdl_path), name="Stamp")
        sec = _section(rdl_path, section_local)
        report_items = find_child(sec, "ReportItems")
        # Either ReportItems is removed when empty or it has no children.
        assert report_items is None or len(list(report_items)) == 0

    def test_removes_named_image_keeps_other_items(
        self, rdl_path, region, set_fn, add_textbox, add_image, remove_fn, section_local
    ):
        add_textbox(
            path=str(rdl_path),
            name="Title",
            text="Hello",
            top="0in",
            left="0in",
            width="2in",
            height="0.3in",
        )
        add_image(
            path=str(rdl_path),
            name="Logo",
            image_source="External",
            value="https://example.com/logo.png",
            top="0in",
            left="2.1in",
            width="1in",
            height="0.5in",
        )
        remove_fn(path=str(rdl_path), name="Logo")
        sec = _section(rdl_path, section_local)
        report_items = find_child(sec, "ReportItems")
        names = [el.get("Name") for el in list(report_items)]
        assert names == ["Title"]

    def test_unknown_name_raises(
        self, rdl_path, region, set_fn, add_textbox, add_image, remove_fn, section_local
    ):
        from pbirb_mcp.core.ids import ElementNotFoundError

        with pytest.raises(ElementNotFoundError):
            remove_fn(path=str(rdl_path), name="Ghost")


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_eight_header_footer_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {
            "set_page_header",
            "set_page_footer",
            "add_header_textbox",
            "add_footer_textbox",
            "add_header_image",
            "add_footer_image",
            "remove_header_item",
            "remove_footer_item",
        } <= names


# ---- #50: band Height is always written and sized to its items ----------


def _height(rdl_path: Path, local: str):
    node = find_child(_section(rdl_path, local), "Height")
    return None if node is None else node.text


@pytest.mark.parametrize(
    "region,set_fn,add_textbox,add_image,_c,section_local", REGIONS, ids=REGION_IDS
)
class TestBandSizing:
    def test_flags_only_on_new_section_writes_default_height(
        self, rdl_path, region, set_fn, add_textbox, add_image, _c, section_local
    ):
        result = set_fn(path=str(rdl_path), print_on_first_page=False)
        assert _height(rdl_path, section_local) == "0.5in"
        assert result["changed"] == ["Height", "PrintOnFirstPage"]
        # Height is the first child, per the PageSection order.
        assert etree.QName(_section(rdl_path, section_local)[0]).localname == "Height"
        assert validate_report(str(rdl_path))["valid"]

    def test_explicit_height_shorter_than_items_warns(
        self, rdl_path, region, set_fn, add_textbox, add_image, _c, section_local
    ):
        add_textbox(str(rdl_path), "Tall", "x", "0in", "0in", "2in", "1in")
        result = set_fn(path=str(rdl_path), height="0.25in")
        assert _height(rdl_path, section_local) == "0.25in"
        assert any("shorter than its items" in w for w in result["warnings"])

    def test_new_band_sized_to_tall_textbox(
        self, rdl_path, region, set_fn, add_textbox, add_image, _c, section_local
    ):
        result = add_textbox(str(rdl_path), "Title", "x", "0in", "0in", "4in", "1.2in")
        assert _height(rdl_path, section_local) == "1.2in"
        assert result["band_height"] == "1.2in"
        assert "warnings" not in result

    def test_new_band_keeps_half_inch_floor(
        self, rdl_path, region, set_fn, add_textbox, add_image, _c, section_local
    ):
        result = add_textbox(str(rdl_path), "Small", "x", "0in", "0in", "2in", "0.25in")
        assert _height(rdl_path, section_local) == "0.5in"
        assert result["band_height"] == "0.5in"

    def test_existing_short_band_grows_and_warns(
        self, rdl_path, region, set_fn, add_textbox, add_image, _c, section_local
    ):
        set_fn(path=str(rdl_path), height="0.5in")
        result = add_image(
            str(rdl_path), "Logo", "External", "http://x/logo.png", "0.25in", "0in", "1in", "1in"
        )
        assert _height(rdl_path, section_local) == "1.25in"
        assert result["band_height"] == "1.25in"
        assert any("grown from '0.5in' to '1.25in'" in w for w in result["warnings"])

    def test_existing_tall_band_is_not_shrunk(
        self, rdl_path, region, set_fn, add_textbox, add_image, _c, section_local
    ):
        set_fn(path=str(rdl_path), height="2in")
        result = add_textbox(str(rdl_path), "Small", "x", "0in", "0in", "2in", "0.25in")
        assert _height(rdl_path, section_local) == "2in"
        assert "warnings" not in result

    def test_band_grows_in_its_own_unit(
        self, rdl_path, region, set_fn, add_textbox, add_image, _c, section_local
    ):
        set_fn(path=str(rdl_path), height="1cm")
        add_textbox(str(rdl_path), "Title", "x", "1cm", "0cm", "4cm", "2cm")
        assert _height(rdl_path, section_local) == "3cm"

    def test_item_past_printable_width_warns(
        self, rdl_path, region, set_fn, add_textbox, add_image, _c, section_local
    ):
        # Fixture page: 8.5in wide, 1in margins -> 6.5in printable.
        result = add_textbox(str(rdl_path), "Wide", "x", "0in", "1in", "6in", "0.25in")
        assert any("printable width of 6.5in" in w for w in result["warnings"])


_RESIZE = [
    (
        "header",
        add_header_textbox,
        "set_header_item_size",
        "set_header_item_position",
        "PageHeader",
    ),
    (
        "footer",
        add_footer_textbox,
        "set_footer_item_size",
        "set_footer_item_position",
        "PageFooter",
    ),
]


@pytest.mark.parametrize(
    "region,add_textbox,size_tool,move_tool,section_local", _RESIZE, ids=REGION_IDS
)
class TestItemFitsBand:
    def test_resize_past_band_warns_without_growing(
        self, rdl_path, region, add_textbox, size_tool, move_tool, section_local
    ):
        add_textbox(str(rdl_path), "Title", "x", "0in", "0in", "4in", "1.2in")
        result = getattr(positioning, size_tool)(str(rdl_path), "Title", height="3in")
        assert result["changed"] is True
        assert "band_height" not in result
        assert any("ends at 3in" in w and "grow_band" in w for w in result["warnings"])
        assert _height(rdl_path, section_local) == "1.2in"

    def test_resize_with_grow_band_grows_band(
        self, rdl_path, region, add_textbox, size_tool, move_tool, section_local
    ):
        add_textbox(str(rdl_path), "Title", "x", "0in", "0in", "4in", "1.2in")
        result = getattr(positioning, size_tool)(
            str(rdl_path), "Title", height="3in", grow_band=True
        )
        assert result["band_height"] == "3in"
        assert "warnings" not in result
        assert _height(rdl_path, section_local) == "3in"

    def test_resize_that_fits_is_silent(
        self, rdl_path, region, add_textbox, size_tool, move_tool, section_local
    ):
        add_textbox(str(rdl_path), "Title", "x", "0in", "0in", "4in", "1.2in")
        result = getattr(positioning, size_tool)(str(rdl_path), "Title", height="1in")
        assert "warnings" not in result
        assert "band_height" not in result

    def test_resize_past_printable_width_warns(
        self, rdl_path, region, add_textbox, size_tool, move_tool, section_local
    ):
        add_textbox(str(rdl_path), "Title", "x", "0in", "0in", "4in", "0.25in")
        result = getattr(positioning, size_tool)(
            str(rdl_path), "Title", width="7in", grow_band=True
        )
        assert any("printable width" in w for w in result["warnings"])

    def test_move_below_band_warns_or_grows(
        self, rdl_path, region, add_textbox, size_tool, move_tool, section_local
    ):
        add_textbox(str(rdl_path), "Title", "x", "0in", "0in", "4in", "0.25in")
        move = getattr(positioning, move_tool)
        result = move(str(rdl_path), "Title", top="1in", left="0in")
        assert any("ends at 1.25in" in w for w in result["warnings"])
        assert _height(rdl_path, section_local) == "0.5in"
        result = move(str(rdl_path), "Title", top="1in", left="0in", grow_band=True)
        # Position already applied; only the band changes now.
        assert result["changed"] is True
        assert result["band_height"] == "1.25in"
        assert _height(rdl_path, section_local) == "1.25in"

    def test_grow_band_through_the_server(
        self, rdl_path, region, add_textbox, size_tool, move_tool, section_local
    ):
        add_textbox(str(rdl_path), "Title", "x", "0in", "0in", "4in", "0.25in")
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": size_tool,
                    "arguments": {
                        "path": str(rdl_path),
                        "name": "Title",
                        "height": "2in",
                        "grow_band": True,
                    },
                },
            }
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["band_height"] == "2in"


def test_issue_50_repro_sizes(rdl_path):
    p = str(rdl_path)
    set_page_header(p, print_on_first_page=False)
    assert _height(rdl_path, "PageHeader") == "0.5in"
    add_footer_textbox(p, "Title", "x", "0in", "0in", "4in", "1.2in")
    assert _height(rdl_path, "PageFooter") == "1.2in"
    assert positioning.set_footer_item_size(p, "Title", height="3in")["warnings"]
    grown = positioning.set_footer_item_size(p, "Title", height="3in", grow_band=True)
    assert grown["band_height"] == "3in"
    assert validate_report(p)["valid"]
