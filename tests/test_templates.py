"""Snippet-template tool tests.

``insert_tablix_from_template`` builds a tablix programmatically and
appends it to ``<Body>/<ReportItems>``. Chart-template tests moved to
``tests/test_chart.py`` in v0.3.0 alongside the extraction of chart
authoring into ``pbirb_mcp.ops.chart``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.tablix_headers import shape_problems
from pbirb_mcp.core.xpath import RDL_NS, XPATH_NS, find_child, q
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.templates import insert_tablix_from_template
from pbirb_mcp.ops.validate import validate_report
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
    items = doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body/{{{RDL_NS}}}ReportItems")
    return list(items) if items is not None else []


# ---- insert_tablix_from_template ------------------------------------------


class TestInsertTablixTemplate:
    def test_inserts_tablix_into_body(self, rdl_path):
        result = insert_tablix_from_template(
            path=str(rdl_path),
            name="DetailTable",
            dataset_name="MainDataset",
            columns=["ProductID", "ProductName", "Amount"],
            top="3in",
            left="0.5in",
            width="4in",
            height="0.5in",
        )
        assert result["name"] == "DetailTable"
        # Body now has both MainTable and DetailTable.
        names = [el.get("Name") for el in _body_items(rdl_path)]
        assert "MainTable" in names
        assert "DetailTable" in names

    def test_column_count_matches_requested_columns(self, rdl_path):
        insert_tablix_from_template(
            path=str(rdl_path),
            name="DetailTable",
            dataset_name="MainDataset",
            columns=["ProductID", "ProductName", "Amount"],
            top="3in",
            left="0.5in",
            width="4in",
            height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='DetailTable']")
        cols = tablix.findall(f"{q('TablixBody')}/{q('TablixColumns')}/{q('TablixColumn')}")
        assert len(cols) == 3

    def test_header_row_has_static_label_per_column(self, rdl_path):
        insert_tablix_from_template(
            path=str(rdl_path),
            name="DetailTable",
            dataset_name="MainDataset",
            columns=["ProductID", "ProductName"],
            top="3in",
            left="0.5in",
            width="3in",
            height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='DetailTable']")
        header_row = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")[0]
        cells = header_row.findall(f"{q('TablixCells')}/{q('TablixCell')}")
        labels = [
            cell.find(
                f"{q('CellContents')}/{q('Textbox')}/{q('Paragraphs')}/"
                f"{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
            ).text
            for cell in cells
        ]
        assert labels == ["ProductID", "ProductName"]

    def test_detail_row_binds_each_column_to_field(self, rdl_path):
        insert_tablix_from_template(
            path=str(rdl_path),
            name="DetailTable",
            dataset_name="MainDataset",
            columns=["ProductID", "ProductName"],
            top="3in",
            left="0.5in",
            width="3in",
            height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='DetailTable']")
        detail_row = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")[1]
        cells = detail_row.findall(f"{q('TablixCells')}/{q('TablixCell')}")
        values = [
            cell.find(
                f"{q('CellContents')}/{q('Textbox')}/{q('Paragraphs')}/"
                f"{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
            ).text
            for cell in cells
        ]
        assert values == ["=Fields!ProductID.Value", "=Fields!ProductName.Value"]

    def test_dataset_name_wired_through(self, rdl_path):
        insert_tablix_from_template(
            path=str(rdl_path),
            name="DetailTable",
            dataset_name="MainDataset",
            columns=["Amount"],
            top="3in",
            left="0.5in",
            width="2in",
            height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='DetailTable']")
        assert find_child(tablix, "DataSetName").text == "MainDataset"

    def test_duplicate_name_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            insert_tablix_from_template(
                path=str(rdl_path),
                name="MainTable",  # already exists
                dataset_name="MainDataset",
                columns=["X"],
                top="0in",
                left="0in",
                width="1in",
                height="0.5in",
            )

    def test_unknown_dataset_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            insert_tablix_from_template(
                path=str(rdl_path),
                name="DetailTable",
                dataset_name="NoSuchDataset",
                columns=["X"],
                top="0in",
                left="0in",
                width="1in",
                height="0.5in",
            )

    def test_empty_columns_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            insert_tablix_from_template(
                path=str(rdl_path),
                name="DetailTable",
                dataset_name="MainDataset",
                columns=[],
                top="0in",
                left="0in",
                width="1in",
                height="0.5in",
            )

    def test_round_trip_safe(self, rdl_path):
        insert_tablix_from_template(
            path=str(rdl_path),
            name="DetailTable",
            dataset_name="MainDataset",
            columns=["ProductID", "Amount"],
            top="3in",
            left="0.5in",
            width="3in",
            height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_template_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {
            "insert_tablix_from_template",
            "insert_chart_from_template",
        } <= names


# ---- #36: unique names, captions, field check -----------------------------

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.rdl"))
BOX = ("3in", "0in", "2in", "0.5in")


def _dupes(path: Path, xpath: str) -> list:
    doc = RDLDocument.open(path)
    seen: dict = {}
    for n in doc.root.xpath(xpath, namespaces=XPATH_NS):
        seen[n] = seen.get(n, 0) + 1
    return [n for n, c in seen.items() if c > 1]


def _row_values(path: Path, tablix_name: str, row_index: int) -> list:
    doc = RDLDocument.open(path)
    tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='{tablix_name}']")
    row = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")[row_index]
    return [(tb.get("Name"), tb.find(f".//{q('Value')}").text) for tb in row.iter(q("Textbox"))]


class TestTemplateNamesAndFields:
    @pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
    def test_group_and_item_names_stay_unique_on_every_fixture(self, tmp_path, fixture):
        path = tmp_path / fixture.name
        shutil.copy(fixture, path)
        for name in ("T2", "T3"):
            insert_tablix_from_template(str(path), name, "MainDataset", ["Amount"], *BOX)
        assert _dupes(path, "//r:Group/@Name") == []
        assert _dupes(path, "//r:*[@Name][self::r:Textbox or self::r:Tablix]/@Name") == []
        assert [i for i in lint_report(str(path))["issues"] if i["rule"] == "duplicate-name"] == []
        assert validate_report(str(path))["valid"]
        doc = RDLDocument.open(path)
        for tablix in doc.root.iter(q("Tablix")):
            assert shape_problems(tablix) == []

    def test_colliding_tablix_name_raises(self, rdl_path):
        before = rdl_path.read_bytes()
        # A textbox inside MainTable's cells, not a body-level item.
        with pytest.raises(ValueError, match="'HeaderAmount' already exists"):
            insert_tablix_from_template(
                str(rdl_path), "HeaderAmount", "MainDataset", ["Amount"], *BOX
            )
        assert rdl_path.read_bytes() == before

    def test_non_identifier_column_rejected_with_caption_hint(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match='"header": "Unit Price \\(USD\\)"'):
            insert_tablix_from_template(
                str(rdl_path), "T3", "MainDataset", ["Unit Price (USD)"], *BOX
            )
        assert rdl_path.read_bytes() == before

    def test_field_with_display_caption(self, rdl_path):
        result = insert_tablix_from_template(
            str(rdl_path),
            "T3",
            "MainDataset",
            [{"field": "Amount", "header": "Unit Price (USD)"}, "ProductID"],
            *BOX,
        )
        assert result["columns"] == ["Amount", "ProductID"]
        assert result["headers"] == ["Unit Price (USD)", "ProductID"]
        assert _row_values(rdl_path, "T3", 0) == [
            ("T3_Header_Amount", "Unit Price (USD)"),
            ("T3_Header_ProductID", "ProductID"),
        ]
        assert _row_values(rdl_path, "T3", 1) == [
            ("T3_Amount", "=Fields!Amount.Value"),
            ("T3_ProductID", "=Fields!ProductID.Value"),
        ]
        assert lint_report(str(rdl_path))["issues"] == []
        assert validate_report(str(rdl_path))["valid"]

    def test_unknown_field_raises_listing_available_fields(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError) as exc:
            insert_tablix_from_template(
                str(rdl_path), "T3", "MainDataset", ["Amount", "NoSuchField"], *BOX
            )
        message = str(exc.value)
        assert "'NoSuchField'" in message
        assert "ProductID, ProductName, Amount" in message
        assert rdl_path.read_bytes() == before

    def test_fields_resolve_against_the_named_dataset(self, tmp_path):
        path = tmp_path / "multi.rdl"
        shutil.copy(Path(__file__).parent / "fixtures" / "pbi_multi_datasource.rdl", path)
        with pytest.raises(ValueError, match="available fields: LookupKey"):
            insert_tablix_from_template(str(path), "T3", "LookupSet", ["Amount"], *BOX)
        insert_tablix_from_template(str(path), "T3", "LookupSet", ["LookupKey"], *BOX)

    @pytest.mark.parametrize(
        "entry",
        [
            {"header": "No field"},
            {"field": "Amount", "caption": "x"},
            {"field": "Amount", "header": 5},
            5,
        ],
    )
    def test_malformed_column_entries_rejected(self, rdl_path, entry):
        with pytest.raises(ValueError, match="columns\\[0\\]"):
            insert_tablix_from_template(str(rdl_path), "T3", "MainDataset", [entry], *BOX)

    def test_caption_entries_pass_the_server_schema(self, rdl_path):
        srv = MCPServer()
        register_all_tools(srv)
        response = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "insert_tablix_from_template",
                    "arguments": {
                        "path": str(rdl_path),
                        "name": "T3",
                        "dataset_name": "MainDataset",
                        "columns": [{"field": "Amount", "header": "Unit Price (USD)"}],
                        "top": "3in",
                        "left": "0in",
                        "width": "2in",
                        "height": "0.5in",
                    },
                },
            }
        )
        assert not response["result"].get("isError"), response
        assert _row_values(rdl_path, "T3", 0) == [("T3_Header_Amount", "Unit Price (USD)")]
