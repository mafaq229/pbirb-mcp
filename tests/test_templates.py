"""Snippet-template tool tests.

``insert_tablix_from_template`` and ``insert_chart_from_template`` build
the requested element programmatically and append it to
``<Body>/<ReportItems>`` so it sits alongside the existing tablix.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.templates import (
    insert_chart_from_template,
    insert_tablix_from_template,
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
    items = doc.root.find(
        f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body/{{{RDL_NS}}}ReportItems"
    )
    return list(items) if items is not None else []


# ---- insert_tablix_from_template ------------------------------------------


class TestInsertTablixTemplate:
    def test_inserts_tablix_into_body(self, rdl_path):
        result = insert_tablix_from_template(
            path=str(rdl_path),
            name="DetailTable",
            dataset_name="MainDataset",
            columns=["ProductID", "ProductName", "Amount"],
            top="3in", left="0.5in", width="4in", height="0.5in",
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
            columns=["A", "B", "C", "D"],
            top="3in", left="0.5in", width="4in", height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='DetailTable']")
        cols = tablix.findall(
            f"{q('TablixBody')}/{q('TablixColumns')}/{q('TablixColumn')}"
        )
        assert len(cols) == 4

    def test_header_row_has_static_label_per_column(self, rdl_path):
        insert_tablix_from_template(
            path=str(rdl_path),
            name="DetailTable",
            dataset_name="MainDataset",
            columns=["ProductID", "ProductName"],
            top="3in", left="0.5in", width="3in", height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='DetailTable']")
        header_row = tablix.findall(
            f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}"
        )[0]
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
            top="3in", left="0.5in", width="3in", height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='DetailTable']")
        detail_row = tablix.findall(
            f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}"
        )[1]
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
            columns=["A"],
            top="3in", left="0.5in", width="2in", height="0.5in",
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
                top="0in", left="0in", width="1in", height="0.5in",
            )

    def test_unknown_dataset_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            insert_tablix_from_template(
                path=str(rdl_path),
                name="DetailTable",
                dataset_name="NoSuchDataset",
                columns=["X"],
                top="0in", left="0in", width="1in", height="0.5in",
            )

    def test_empty_columns_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            insert_tablix_from_template(
                path=str(rdl_path),
                name="DetailTable",
                dataset_name="MainDataset",
                columns=[],
                top="0in", left="0in", width="1in", height="0.5in",
            )

    def test_round_trip_safe(self, rdl_path):
        insert_tablix_from_template(
            path=str(rdl_path),
            name="DetailTable",
            dataset_name="MainDataset",
            columns=["ProductID", "Amount"],
            top="3in", left="0.5in", width="3in", height="0.5in",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- insert_chart_from_template -------------------------------------------


class TestInsertChartTemplate:
    def test_inserts_chart_into_body(self, rdl_path):
        result = insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in", left="0.5in", width="5in", height="3in",
        )
        assert result["name"] == "SalesChart"
        names = [el.get("Name") for el in _body_items(rdl_path)]
        assert "SalesChart" in names

    def test_dataset_name_wired_through(self, rdl_path):
        insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in", left="0.5in", width="5in", height="3in",
        )
        doc = RDLDocument.open(rdl_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesChart']")
        assert find_child(chart, "DataSetName").text == "MainDataset"

    def test_category_group_expression_uses_category_field(self, rdl_path):
        insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in", left="0.5in", width="5in", height="3in",
        )
        doc = RDLDocument.open(rdl_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesChart']")
        expr = chart.find(
            f"{q('ChartCategoryHierarchy')}/{q('ChartMembers')}/"
            f"{q('ChartMember')}/{q('Group')}/{q('GroupExpressions')}/"
            f"{q('GroupExpression')}"
        )
        assert expr.text == "=Fields!ProductName.Value"

    def test_series_y_uses_value_field(self, rdl_path):
        insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in", left="0.5in", width="5in", height="3in",
        )
        doc = RDLDocument.open(rdl_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesChart']")
        y = chart.find(
            f"{q('ChartData')}/{q('ChartSeriesCollection')}/{q('ChartSeries')}/"
            f"{q('ChartDataPoints')}/{q('ChartDataPoint')}/{q('ChartDataPointValues')}/{q('Y')}"
        )
        assert y.text == "=Sum(Fields!Amount.Value)"

    def test_default_chart_type_is_column(self, rdl_path):
        insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in", left="0.5in", width="5in", height="3in",
        )
        doc = RDLDocument.open(rdl_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesChart']")
        chart_type = chart.find(
            f"{q('ChartData')}/{q('ChartSeriesCollection')}/{q('ChartSeries')}/{q('Type')}"
        )
        assert chart_type.text == "Column"

    def test_duplicate_name_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            insert_chart_from_template(
                path=str(rdl_path),
                name="MainTable",  # already exists
                dataset_name="MainDataset",
                category_field="ProductName",
                value_field="Amount",
                top="0in", left="0in", width="3in", height="2in",
            )

    def test_unknown_dataset_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            insert_chart_from_template(
                path=str(rdl_path),
                name="SalesChart",
                dataset_name="NoSuchDataset",
                category_field="ProductName",
                value_field="Amount",
                top="0in", left="0in", width="3in", height="2in",
            )

    def test_round_trip_safe(self, rdl_path):
        insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in", left="0.5in", width="5in", height="3in",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_template_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {
            "insert_tablix_from_template",
            "insert_chart_from_template",
        } <= names
