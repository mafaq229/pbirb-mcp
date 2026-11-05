"""rename_report_item: rename a report item or group and every reference (#111)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError
from pbirb_mcp.core.xpath import find_child, q, qrd
from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.header_footer import add_header_textbox, set_page_header
from pbirb_mcp.ops.layout import add_list
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.rename import rename_report_item
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.ops.visibility import set_element_visibility
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _textbox(path, name, text, top):
    add_body_textbox(str(path), name, text, top, "0.5in", "2in", "0.2in")


def _value(path, textbox):
    root = RDLDocument.open(path).root
    tb = root.find(f".//{q('Textbox')}[@Name='{textbox}']")
    return tb.find(f".//{q('Value')}").text


def _set_value(path, textbox, text):
    doc = RDLDocument.open(path)
    tb = doc.root.find(f".//{q('Textbox')}[@Name='{textbox}']")
    tb.find(f".//{q('Value')}").text = text
    doc.save()


def _clean(path):
    assert validate_report(str(path))["valid"]
    assert lint_report(str(path))["issues"] == []


@pytest.fixture
def repro(rdl_path):
    """The issue's repro: a textbox echoed by another and used as a toggle."""
    _textbox(rdl_path, "Textbox1", "hello", "1.2in")
    _textbox(rdl_path, "Echo", "=ReportItems!Textbox1.Value", "1.5in")
    set_element_visibility(str(rdl_path), "MainTable", "true", "Textbox1")
    return rdl_path


class TestAcceptance:
    def test_textbox_rename_rewrites_every_reference(self, repro):
        result = rename_report_item(str(repro), "Textbox1", "TitleBox")
        assert "Textbox1" not in repro.read_text(encoding="utf-8")
        assert _value(repro, "Echo") == "=ReportItems!TitleBox.Value"
        root = RDLDocument.open(repro).root
        assert root.find(f".//{q('ToggleItem')}").text == "TitleBox"
        tb = root.find(f".//{q('Textbox')}[@Name='TitleBox']")
        assert tb.find(qrd("DefaultName")).text == "TitleBox"
        assert result["renamed"] == "TitleBox" and result["old_name"] == "Textbox1"
        assert result["kind"] == "Textbox" and result["changed"] is True
        after = {(u["location"], u["after"]) for u in result["references_updated"]}
        assert ("Tablix[Name='MainTable']/Visibility/ToggleItem", "TitleBox") in after
        assert (
            "Textbox[Name='Echo']/Paragraphs/Paragraph/TextRuns/TextRun/Value",
            "=ReportItems!TitleBox.Value",
        ) in after
        _clean(repro)

    def test_group_rename_updates_scope_strings(self, rdl_path):
        _set_value(
            rdl_path, "Amount", '=RowNumber("Details") & Sum(Fields!Amount.Value, "Details")'
        )
        _set_value(rdl_path, "ProductID", '=RunningValue(Fields!Amount.Value, Sum, "Details")')
        result = rename_report_item(str(rdl_path), "Details", "ProductRows")
        assert result["kind"] == "Group"
        assert _value(rdl_path, "Amount") == (
            '=RowNumber("ProductRows") & Sum(Fields!Amount.Value, "ProductRows")'
        )
        assert _value(rdl_path, "ProductID") == (
            '=RunningValue(Fields!Amount.Value, Sum, "ProductRows")'
        )
        assert '"Details"' not in rdl_path.read_text(encoding="utf-8")
        root = RDLDocument.open(rdl_path).root
        assert root.find(f".//{q('Group')}").get("Name") == "ProductRows"
        _clean(rdl_path)

    def test_existing_name_refuses_and_leaves_file(self, repro):
        before = repro.read_bytes()
        with pytest.raises(ValueError, match="already exists"):
            rename_report_item(str(repro), "Textbox1", "Echo")
        with pytest.raises(ValueError, match="already exists"):
            rename_report_item(str(repro), "Textbox1", "Details")  # a group
        assert repro.read_bytes() == before

    @pytest.mark.parametrize("bad", ["Title Box", "1Title", "", "Title-Box", "_x"])
    def test_invalid_identifier_refuses_and_leaves_file(self, repro, bad):
        before = repro.read_bytes()
        with pytest.raises(ValueError):
            rename_report_item(str(repro), "Textbox1", bad)
        assert repro.read_bytes() == before


class TestReferences:
    def test_tablix_rename(self, rdl_path):
        _textbox(rdl_path, "Total", '=Sum(Fields!Amount.Value, "MainTable")', "1.2in")
        doc = RDLDocument.open(rdl_path)
        total = doc.root.find(f".//{q('Textbox')}[@Name='Total']")
        style = find_child(total, "Style")
        repeat = etree.Element(q("RepeatWith"))
        repeat.text = "MainTable"
        (style.addprevious if style is not None else total.append)(repeat)
        header = doc.root.find(f".//{q('Textbox')}[@Name='HeaderAmount']")
        sort = etree.Element(q("UserSort"))
        etree.SubElement(sort, q("SortExpression")).text = "=Fields!Amount.Value"
        etree.SubElement(sort, q("SortExpressionScope")).text = "Details"
        etree.SubElement(sort, q("SortTarget")).text = "MainTable"
        find_child(header, "Paragraphs").addprevious(sort)
        doc.save()
        assert validate_report(str(rdl_path))["valid"]

        result = rename_report_item(str(rdl_path), "MainTable", "SalesTable")
        assert result["kind"] == "Tablix"
        text = rdl_path.read_text(encoding="utf-8")
        assert "MainTable" not in text
        assert _value(rdl_path, "Total") == '=Sum(Fields!Amount.Value, "SalesTable")'
        root = RDLDocument.open(rdl_path).root
        assert root.find(f".//{q('RepeatWith')}").text == "SalesTable"
        assert root.find(f".//{q('SortTarget')}").text == "SalesTable"
        assert root.find(f".//{q('SortExpressionScope')}").text == "Details"
        assert validate_report(str(rdl_path))["valid"]

    def test_literals_comments_and_other_names_untouched(self, rdl_path):
        _textbox(rdl_path, "Textbox1", "hello", "1.2in")
        _textbox(rdl_path, "Textbox10", "x", "1.5in")
        expr = (
            '="Textbox1" & ReportItems!Textbox10.Value & ReportItems!Textbox1.Value'
            " ' ReportItems!Textbox1"
        )
        _textbox(rdl_path, "Echo", expr, "1.8in")
        result = rename_report_item(str(rdl_path), "Textbox1", "Title")
        assert _value(rdl_path, "Echo") == (
            '="Textbox1" & ReportItems!Textbox10.Value & ReportItems!Title.Value'
            " ' ReportItems!Textbox1"
        )
        assert [u["after"] for u in result["references_updated"] if "Echo" in u["location"]]

    def test_dataset_named_scope_is_not_rewritten(self, rdl_path):
        # A scope string naming a dataset means the dataset, not the group.
        _set_value(rdl_path, "Amount", '=Sum(Fields!Amount.Value, "MainDataset")')
        rename_report_item(str(rdl_path), "Details", "Rows")
        assert _value(rdl_path, "Amount") == '=Sum(Fields!Amount.Value, "MainDataset")'

    def test_data_region_may_not_take_a_dataset_name(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="dataset name"):
            rename_report_item(str(rdl_path), "MainTable", "MainDataset")
        assert rdl_path.read_bytes() == before

    def test_data_element_name_mirror(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{q('Tablix')}")
        el = etree.Element(q("DataElementName"))
        el.text = "MainTable"
        find_child(tablix, "Top").addprevious(el)
        doc.save()
        assert validate_report(str(rdl_path))["valid"]
        rename_report_item(str(rdl_path), "MainTable", "Sales")
        tablix = RDLDocument.open(rdl_path).root.find(f".//{q('Tablix')}")
        assert find_child(tablix, "DataElementName").text == "Sales"

    def test_header_item_referenced_from_body(self, rdl_path):
        set_page_header(str(rdl_path), height="1in")
        add_header_textbox(str(rdl_path), "HdrTitle", "x", "0in", "0in", "1in", "0.3in")
        _textbox(rdl_path, "Echo", "=ReportItems!HdrTitle.Value", "1.2in")
        rename_report_item(str(rdl_path), "HdrTitle", "PageTitle")
        assert _value(rdl_path, "Echo") == "=ReportItems!PageTitle.Value"
        _clean(rdl_path)


class TestListRenameInner:
    def test_rename_inner(self, rdl_path):
        add_list(str(rdl_path), "Cards", "MainDataset", "1.2in", "0.5in", "4in", "0.6in")
        _textbox(rdl_path, "Count", '=CountRows("Cards_Details")', "2in")
        result = rename_report_item(str(rdl_path), "Cards", "Products", rename_inner=True)
        assert result["also_renamed"] == {
            "Cards_Rect": "Products_Rect",
            "Cards_Details": "Products_Details",
        }
        assert "Cards" not in rdl_path.read_text(encoding="utf-8")
        assert _value(rdl_path, "Count") == '=CountRows("Products_Details")'
        assert validate_report(str(rdl_path))["valid"]

    def test_without_rename_inner_only_the_tablix(self, rdl_path):
        add_list(str(rdl_path), "Cards", "MainDataset", "1.2in", "0.5in", "4in", "0.6in")
        result = rename_report_item(str(rdl_path), "Cards", "Products")
        assert "also_renamed" not in result
        text = rdl_path.read_text(encoding="utf-8")
        assert 'Name="Products"' in text and 'Name="Cards_Rect"' in text

    def test_rename_inner_needs_a_tablix(self, repro):
        with pytest.raises(ValueError, match="rename_inner applies to a Tablix"):
            rename_report_item(str(repro), "Textbox1", "Title", rename_inner=True)

    def test_rename_inner_without_inner_items_warns(self, rdl_path):
        result = rename_report_item(str(rdl_path), "MainTable", "Sales", rename_inner=True)
        assert result["also_renamed"] == {} and result["warnings"]


class TestResolution:
    def test_unknown_name(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            rename_report_item(str(rdl_path), "NoSuchItem", "Other")

    def test_kind_disambiguates_duplicates(self, rdl_path):
        # A malformed report: a textbox and the group share a name.
        _textbox(rdl_path, "Twin", "x", "1.2in")
        doc = RDLDocument.open(rdl_path)
        doc.root.find(f".//{q('Group')}").set("Name", "Twin")
        doc.save()
        before = rdl_path.read_bytes()
        with pytest.raises(AmbiguousElementError, match="pass kind"):
            rename_report_item(str(rdl_path), "Twin", "Other")
        assert rdl_path.read_bytes() == before
        result = rename_report_item(str(rdl_path), "Twin", "Rows", kind="Group")
        assert result["kind"] == "Group"
        root = RDLDocument.open(rdl_path).root
        assert root.find(f".//{q('Group')}").get("Name") == "Rows"
        assert root.find(f".//{q('Textbox')}[@Name='Twin']") is not None

    def test_kind_filters(self, repro):
        with pytest.raises(ElementNotFoundError):
            rename_report_item(str(repro), "Textbox1", "Title", kind="Group")
        with pytest.raises(ValueError, match="kind must be one of"):
            rename_report_item(str(repro), "Textbox1", "Title", kind="ChartSeries")

    def test_repeat_is_a_noop(self, repro):
        rename_report_item(str(repro), "Textbox1", "TitleBox")
        before = repro.read_bytes()
        again = rename_report_item(str(repro), "Textbox1", "TitleBox")
        assert again == {
            "renamed": "TitleBox",
            "old_name": "Textbox1",
            "kind": "Textbox",
            "references_updated": [],
            "changed": False,
        }
        assert repro.read_bytes() == before
        same = rename_report_item(str(repro), "TitleBox", "TitleBox")
        assert same["changed"] is False


class TestTool:
    def test_registered_in_layout_with_annotations(self):
        from pbirb_mcp.core.toolsets import toolset_of

        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        tool = next(t for t in listing if t["name"] == "rename_report_item")
        assert tool["annotations"]["readOnlyHint"] is False
        assert tool["annotations"]["idempotentHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert set(tool["inputSchema"]["required"]) == {"path", "old_name", "new_name"}
        assert toolset_of("rename_report_item") == "layout"

    def test_call_through_server(self, repro):
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "rename_report_item",
                    "arguments": {"path": str(repro), "old_name": "Textbox1", "new_name": "T2"},
                },
            }
        )
        assert not resp["result"].get("isError"), resp
        assert _value(repro, "Echo") == "=ReportItems!T2.Value"
