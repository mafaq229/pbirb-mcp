"""Body-composition tool tests.

The body's ``<ReportItems>`` already holds the fixture's ``MainTable``
tablix, so adds must coexist with existing items and the duplicate-name
check has to ignore tablixes when a new textbox/image arrives.
``remove_body_item`` is willing to remove tablixes too — that's the
explicit "redesign" workflow — but it raises a clear error on unknown
names so accidental misspellings don't silently delete the wrong thing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.body import (
    add_body_image,
    add_body_textbox,
    remove_body_item,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _body_items(rdl_path: Path):
    doc = RDLDocument.open(rdl_path)
    body = doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body")
    items = find_child(body, "ReportItems")
    return list(items) if items is not None else []


# ---- add_body_textbox -----------------------------------------------------


class TestAddBodyTextbox:
    def test_appends_to_existing_report_items(self, rdl_path):
        before = _body_items(rdl_path)
        names_before = [el.get("Name") for el in before]
        add_body_textbox(
            path=str(rdl_path),
            name="Stamp",
            text="CONFIDENTIAL",
            top="0in",
            left="0in",
            width="2in",
            height="0.4in",
        )
        after = _body_items(rdl_path)
        names_after = [el.get("Name") for el in after]
        # Existing items preserved, Stamp appended.
        assert names_before == names_after[:-1]
        assert names_after[-1] == "Stamp"

    def test_static_text_lands_in_textrun_value(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="Stamp",
            text="CONFIDENTIAL",
            top="0in",
            left="0in",
            width="2in",
            height="0.4in",
        )
        doc = RDLDocument.open(rdl_path)
        tb = doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='Stamp']")
        value = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert value.text == "CONFIDENTIAL"

    def test_expression_text_preserves_leading_equals(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="Watermark",
            text="=Globals!ReportName",
            top="0in",
            left="0in",
            width="3in",
            height="0.3in",
        )
        doc = RDLDocument.open(rdl_path)
        tb = doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='Watermark']")
        value = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert value.text == "=Globals!ReportName"

    def test_duplicate_name_rejected_against_any_body_item(self, rdl_path):
        # MainTable is the existing tablix in body — a textbox can't reuse
        # that name.
        with pytest.raises(ValueError):
            add_body_textbox(
                path=str(rdl_path),
                name="MainTable",
                text="oops",
                top="0in",
                left="0in",
                width="1in",
                height="0.3in",
            )


# ---- add_body_image -------------------------------------------------------


class TestAddBodyImage:
    def test_external_image_lands_in_report_items(self, rdl_path):
        add_body_image(
            path=str(rdl_path),
            name="Banner",
            image_source="External",
            value="https://example.com/banner.png",
            top="0in",
            left="0in",
            width="6.5in",
            height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        img = doc.root.find(f".//{{{RDL_NS}}}Image[@Name='Banner']")
        assert img is not None
        assert find_child(img, "Source").text == "External"
        assert find_child(img, "Value").text == "https://example.com/banner.png"

    def test_unknown_image_source_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            add_body_image(
                path=str(rdl_path),
                name="Bad",
                image_source="Magic",
                value="x",
                top="0in",
                left="0in",
                width="1in",
                height="1in",
            )

    def test_duplicate_name_rejected(self, rdl_path):
        add_body_image(
            path=str(rdl_path),
            name="Banner",
            image_source="External",
            value="https://example.com/banner.png",
            top="0in",
            left="0in",
            width="1in",
            height="0.5in",
        )
        with pytest.raises(ValueError):
            add_body_image(
                path=str(rdl_path),
                name="Banner",
                image_source="External",
                value="https://example.com/other.png",
                top="0in",
                left="0in",
                width="1in",
                height="0.5in",
            )


# ---- remove_body_item -----------------------------------------------------


class TestRemoveBodyItem:
    def test_removes_added_textbox(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="Stamp",
            text="CONFIDENTIAL",
            top="0in",
            left="0in",
            width="2in",
            height="0.4in",
        )
        remove_body_item(path=str(rdl_path), name="Stamp")
        names = [el.get("Name") for el in _body_items(rdl_path)]
        assert "Stamp" not in names
        # Existing tablix untouched.
        assert "MainTable" in names

    def test_removes_existing_tablix_when_explicitly_requested(self, rdl_path):
        # Destructive but explicit: caller asked to remove MainTable, so we do.
        remove_body_item(path=str(rdl_path), name="MainTable")
        names = [el.get("Name") for el in _body_items(rdl_path)]
        assert "MainTable" not in names

    def test_unknown_name_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_body_item(path=str(rdl_path), name="NoSuchThing")

    def test_round_trip_safe_after_add_and_remove(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="Stamp",
            text="X",
            top="0in",
            left="0in",
            width="1in",
            height="0.3in",
        )
        remove_body_item(path=str(rdl_path), name="Stamp")
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_three_body_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {"add_body_textbox", "add_body_image", "remove_body_item"} <= names
