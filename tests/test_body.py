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


# ---- descendants and inbound references (#105) ------------------------------


def _textbox(path, name, text, top):
    add_body_textbox(str(path), name, text, top, "0.5in", "2in", "0.2in")


class TestRemoveBodyItemReferences:
    """Acceptance for #105: what goes with an item, and who still points at it."""

    def test_rectangle_reports_its_descendants(self, rdl_path):
        from pbirb_mcp.ops.layout import add_rectangle

        _textbox(rdl_path, "Lbl", "Lbl", "0.05in")
        _textbox(rdl_path, "Val", "Val", "0.3in")
        add_rectangle(str(rdl_path), "Panel", "0in", "0.4in", "3in", "0.5in", ["Lbl", "Val"])
        result = remove_body_item(str(rdl_path), "Panel")
        assert result == {
            "removed": "Panel",
            "kind": "Rectangle",
            "also_removed": ["Lbl", "Val"],
            "dangling_refs": [],
        }
        assert not {"Lbl", "Val", "Panel"} & {
            el.get("Name") for el in RDLDocument.open(rdl_path).root.iter(q("Textbox"))
        }

    def test_tablix_reports_cells_and_groups(self, rdl_path):
        result = remove_body_item(str(rdl_path), "MainTable")
        assert "Details" in result["also_removed"]
        assert {"HeaderAmount", "Amount", "ProductID"} <= set(result["also_removed"])
        assert result["dangling_refs"] == []

    def _echo_and_toggle(self, rdl_path):
        from pbirb_mcp.ops.visibility import set_element_visibility

        _textbox(rdl_path, "Src", "hello", "1.2in")
        _textbox(rdl_path, "Echo", "=ReportItems!Src.Value", "1.5in")
        _textbox(rdl_path, "Toggle", "+", "1.8in")
        set_element_visibility(str(rdl_path), "MainTable", "true", "Toggle")

    def test_report_items_reference_refuses(self, rdl_path):
        self._echo_and_toggle(rdl_path)
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="Echo") as exc:
            remove_body_item(str(rdl_path), "Src")
        assert "ReportItems!Src.Value" in str(exc.value) and "force" in str(exc.value)
        assert rdl_path.read_bytes() == before

    def test_toggle_item_reference_refuses(self, rdl_path):
        self._echo_and_toggle(rdl_path)
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="MainTable") as exc:
            remove_body_item(str(rdl_path), "Toggle")
        assert "ToggleItem" in str(exc.value)
        assert rdl_path.read_bytes() == before

    def test_force_removes_and_reports_dangling_refs(self, rdl_path):
        self._echo_and_toggle(rdl_path)
        src = remove_body_item(str(rdl_path), "Src", force=True)
        assert src["dangling_refs"] == [
            {
                "kind": "report_items",
                "name": "Src",
                "location": "Textbox[Name='Echo']/Paragraphs/Paragraph/TextRuns/TextRun/Value",
                "text": "ReportItems!Src.Value",
            }
        ]
        toggle = remove_body_item(str(rdl_path), "Toggle", force=True)
        assert toggle["dangling_refs"] == [
            {
                "kind": "toggle_item",
                "name": "Toggle",
                "location": "Tablix[Name='MainTable']/Visibility/ToggleItem",
                "text": "Toggle",
            }
        ]
        # The references stay put; validation still passes (the XSD can't see them).
        from pbirb_mcp.ops.validate import validate_report

        assert validate_report(str(rdl_path))["valid"]

    def test_reference_to_a_descendant_refuses(self, rdl_path):
        from pbirb_mcp.ops.layout import add_rectangle

        _textbox(rdl_path, "Inner", "x", "1.2in")
        add_rectangle(str(rdl_path), "Box", "1.1in", "0.4in", "3in", "0.5in", ["Inner"])
        _textbox(rdl_path, "Outside", '="Total: " & ReportItems("Inner").Value', "2in")
        with pytest.raises(ValueError, match=r"Rectangle 'Box' \(or an item or group inside it\)"):
            remove_body_item(str(rdl_path), "Box")

    def test_references_inside_the_removed_subtree_dont_block(self, rdl_path):
        from pbirb_mcp.ops.layout import add_rectangle

        _textbox(rdl_path, "A", "a", "1.2in")
        _textbox(rdl_path, "B", "=ReportItems!A.Value", "1.5in")
        add_rectangle(str(rdl_path), "Pair", "1.1in", "0.4in", "3in", "0.8in", ["A", "B"])
        result = remove_body_item(str(rdl_path), "Pair")
        assert result["also_removed"] == ["A", "B"] and result["dangling_refs"] == []

    def test_scope_string_reference_refuses(self, rdl_path):
        # An aggregate outside the tablix scoped to its group / to the tablix.
        _textbox(rdl_path, "Total", '=Sum(Fields!Amount.Value, "MainTable")', "2in")
        with pytest.raises(ValueError, match="Total"):
            remove_body_item(str(rdl_path), "MainTable")

    def test_names_in_string_literals_and_comments_dont_count(self, rdl_path):
        _textbox(rdl_path, "Src", "hello", "1.2in")
        _textbox(rdl_path, "Echo", '="ReportItems!Src.Value" \' ReportItems!Src', "1.5in")
        assert remove_body_item(str(rdl_path), "Src")["dangling_refs"] == []

    def test_tool_schema_takes_force(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        by_name = {t["name"]: t for t in listing}
        for tool in ("remove_body_item", "remove_header_item", "remove_footer_item"):
            assert by_name[tool]["inputSchema"]["properties"]["force"]["type"] == "boolean"


class TestRemoveHeaderFooterItemReferences:
    def test_header_reference_refuses_then_force(self, rdl_path):
        from pbirb_mcp.ops.header_footer import (
            add_header_textbox,
            remove_header_item,
            set_page_header,
        )

        set_page_header(str(rdl_path), height="1in")
        add_header_textbox(str(rdl_path), "HdrSrc", "x", "0in", "0in", "1in", "0.3in")
        _textbox(rdl_path, "BodyEcho", "=ReportItems!HdrSrc.Value", "1.2in")
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="BodyEcho"):
            remove_header_item(str(rdl_path), "HdrSrc")
        assert rdl_path.read_bytes() == before
        result = remove_header_item(str(rdl_path), "HdrSrc", force=True)
        assert result["kind"] == "Textbox" and result["region"] == "header"
        assert [r["name"] for r in result["dangling_refs"]] == ["HdrSrc"]

    def test_body_item_referenced_from_footer_refuses(self, rdl_path):
        from pbirb_mcp.ops.header_footer import add_footer_textbox, set_page_footer

        _textbox(rdl_path, "PageTotal", "=Sum(Fields!Amount.Value)", "1.2in")
        set_page_footer(str(rdl_path), height="0.5in")
        add_footer_textbox(
            str(rdl_path), "Foot", "=ReportItems!PageTotal.Value", "0in", "0in", "2in", "0.3in"
        )
        with pytest.raises(ValueError, match="Foot"):
            remove_body_item(str(rdl_path), "PageTotal")


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
