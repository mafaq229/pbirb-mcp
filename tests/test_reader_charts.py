"""Tests for ``get_chart`` — Phase 1 commit 6 of v0.3.0.

Read-back parity with v0.2's ``get_textbox`` / ``get_image`` /
``get_rectangle``. Exercises the chart-rich fixture
(``pbi_chart_rich.rdl``) which carries 3 series, palette, axis titles,
and a chart title — ground truth for both the read-back tool and the
v0.3 chart mutation tools that follow.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.ops.reader import get_chart
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE_PLAIN = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
FIXTURE_RICH = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"


@pytest.fixture
def rich_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_RICH, dest)
    return dest


@pytest.fixture
def plain_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_PLAIN, dest)
    return dest


# ---- get_chart on chart-rich fixture --------------------------------------


class TestGetChart:
    def test_top_level_layout_fields(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["name"] == "SalesByProduct"
        assert c["type"] == "Chart"
        assert c["top"] == "3in"
        assert c["left"] == "0.5in"
        assert c["width"] == "5in"
        assert c["height"] == "3in"

    def test_dataset_binding(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["dataset"] == "MainDataset"

    def test_palette_surfaced(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["palette"] == "EarthTones"

    def test_three_series_returned(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert [s["name"] for s in c["series"]] == ["Amount", "Quantity", "Total"]

    def test_series_type_subtype_per_entry(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        amount = next(s for s in c["series"] if s["name"] == "Amount")
        quantity = next(s for s in c["series"] if s["name"] == "Quantity")
        total = next(s for s in c["series"] if s["name"] == "Total")
        assert amount["type"] == "Column"
        assert amount["subtype"] == "Plain"
        assert quantity["type"] == "Line"
        assert total["type"] == "Bar"
        assert total["subtype"] == "Stacked"

    def test_series_value_expression(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        amount = next(s for s in c["series"] if s["name"] == "Amount")
        assert amount["value_expression"] == "=Sum(Fields!Amount.Value)"

    def test_category_groups(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        cats = c["category_groups"]
        assert len(cats) == 1
        assert cats[0]["expression"] == "=Fields!ProductName.Value"
        assert cats[0]["label"] == "=Fields!ProductName.Value"

    def test_axes_titles(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        cat_axis = c["axes"]["category"][0]
        val_axis = c["axes"]["value"][0]
        assert cat_axis["title"] == "Product"
        assert val_axis["title"] == "Amount (USD)"

    def test_axis_default_name_primary(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["axes"]["category"][0]["name"] == "Primary"
        assert c["axes"]["value"][0]["name"] == "Primary"

    def test_legend_block_present(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["legend"] is not None
        assert c["legend"]["name"] == "Default"

    def test_title_block_present(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["title"] is not None
        assert c["title"]["caption"] == "Sales by Product"


# ---- minimal-fixture chart (one series, no enrichments) ------------------


class TestGetChartMinimalShape:
    """Read-back of a chart inserted into the minimal fixture via the
    template. Covers the 'just inserted, no further edits' shape."""

    def test_inserted_chart_round_trip(self, plain_path):
        from pbirb_mcp.ops.chart import insert_chart_from_template

        insert_chart_from_template(
            path=str(plain_path),
            name="SimpleChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="0in",
            left="0in",
            width="3in",
            height="2in",
        )
        c = get_chart(path=str(plain_path), name="SimpleChart")
        assert c["name"] == "SimpleChart"
        assert c["dataset"] == "MainDataset"
        assert len(c["series"]) == 1
        assert c["series"][0]["name"] == "Amount"
        assert c["palette"] is None  # template doesn't set one
        assert c["title"]["caption"] == "SimpleChart"  # default = chart name


# ---- error path -----------------------------------------------------------


class TestGetChartErrors:
    def test_unknown_chart_raises(self, rich_path):
        with pytest.raises(ElementNotFoundError):
            get_chart(path=str(rich_path), name="NoSuchChart")


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_get_chart_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "get_chart" in names
