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

    def test_legend_position_ignores_dock_outside_chart_area(self, rich_path):
        """``<DockOutsideChartArea>`` is a boolean, not a position.
        Pre-v0.3.1 the reader returned its text as ``position`` when
        both elements were present (P1.4 in
        .local/feedback/2026-04-30-v030-live-mcp-sweep.md). After the
        fix, ``position`` is the actual ``<Position>`` text.
        """
        from lxml import etree

        from pbirb_mcp.core.document import RDLDocument
        from pbirb_mcp.core.xpath import RDL_NS, find_child, q

        doc = RDLDocument.open(rich_path)
        legend = doc.root.find(
            f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']"
            f"/{{{RDL_NS}}}ChartLegends/{{{RDL_NS}}}ChartLegend"
        )
        # Inject both boolean DockOutsideChartArea AND a real Position.
        # If the reader's old fallback survives, position would echo the
        # boolean text; with the fix it returns "BottomCenter".
        if find_child(legend, "Position") is None:
            etree.SubElement(legend, q("Position")).text = "BottomCenter"
        if find_child(legend, "DockOutsideChartArea") is None:
            etree.SubElement(legend, q("DockOutsideChartArea")).text = "true"
        doc.save()
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["legend"]["position"] == "BottomCenter"

    def test_title_block_present(self, rich_path):
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["title"] is not None
        assert c["title"]["caption"] == "Sales by Product"

    def test_data_labels_none_when_unset(self, rich_path):
        # Chart-rich fixture has no <ChartDataLabel>.
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        for s in c["series"]:
            assert s["data_labels"] is None


class TestGetChartDataLabelsRoundTrip:
    """``get_chart`` should symmetrically read back what
    ``set_chart_data_labels`` writes — closes the asymmetric reader/
    writer gap flagged in the v0.3.0 sweep (P1.3 in
    .local/feedback/2026-04-30-v030-live-mcp-sweep.md).
    """

    def test_visible_only(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_data_labels

        set_chart_data_labels(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            visible=True,
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        amount = next(s for s in c["series"] if s["name"] == "Amount")
        assert amount["data_labels"] == {
            "source": "point",
            "visible": "true",
            "position": None,
            "use_value_as_label": None,
            "format": None,
            "font_weight": None,
            "color": None,
        }
        # Other series untouched.
        for other in (s for s in c["series"] if s["name"] != "Amount"):
            assert other["data_labels"] is None

    def test_visible_and_format(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_data_labels

        set_chart_data_labels(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Quantity",
            visible=False,
            format="#,0",
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        qty = next(s for s in c["series"] if s["name"] == "Quantity")
        assert qty["data_labels"] == {
            "source": "point",
            "visible": "false",
            "position": None,
            "use_value_as_label": None,
            "format": "#,0",
            "font_weight": None,
            "color": None,
        }

    def test_apply_to_all_series(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_data_labels

        set_chart_data_labels(
            path=str(rich_path),
            chart_name="SalesByProduct",
            visible=True,
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        for s in c["series"]:
            assert s["data_labels"] is not None
            assert s["data_labels"]["visible"] == "true"


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


# ---- #102: every chart mutator's writes read back -------------------------


def _series(c, name):
    return next(s for s in c["series"] if s["name"] == name)


class TestEveryMutatorReadsBack:
    """One check per chart mutator (#102): the value it writes is visible in
    ``get_chart``."""

    def test_insert_chart_from_template(self, plain_path):
        from pbirb_mcp.ops.chart import insert_chart_from_template

        out = insert_chart_from_template(
            str(plain_path),
            "C1",
            "MainDataset",
            "ProductName",
            "Amount",
            "0in",
            "0in",
            "3in",
            "2in",
        )
        c = get_chart(str(plain_path), "C1")
        assert c["dataset"] == "MainDataset"
        assert c["category_groups"][0]["name"] == out["category_group"]
        assert c["series"][0]["value_expression"] == "=Sum(Fields!Amount.Value)"
        assert c["series_groups"] == [
            {
                "name": None,
                "expression": None,
                "label": "Amount",
                "sort": [],
                "static": True,
                "level": 0,
                "series": "Amount",
            }
        ]

    def test_set_chart_axis(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_axis

        set_chart_axis(
            str(rich_path),
            "SalesByProduct",
            "Value",
            title="USD",
            format="#,0",
            min="0",
            max="100",
            interval="10",
        )
        axis = get_chart(str(rich_path), "SalesByProduct")["axes"]["value"][0]
        assert (axis["title"], axis["format"], axis["min"], axis["max"], axis["interval"]) == (
            "USD",
            "#,0",
            "0",
            "100",
            "10",
        )

    def test_add_chart_series(self, rich_path):
        from pbirb_mcp.ops.chart import add_chart_series

        add_chart_series(str(rich_path), "SalesByProduct", "Extra", "Amount", series_type="Line")
        c = get_chart(str(rich_path), "SalesByProduct")
        extra = _series(c, "Extra")
        assert extra["type"] == "Line" and extra["label"] == "Extra"
        assert c["series_groups"][-1]["series"] == "Extra"

    def test_remove_chart_series(self, rich_path):
        from pbirb_mcp.ops.chart import remove_chart_series

        remove_chart_series(str(rich_path), "SalesByProduct", "Quantity")
        c = get_chart(str(rich_path), "SalesByProduct")
        assert [s["name"] for s in c["series"]] == ["Amount", "Total"]
        assert [g["series"] for g in c["series_groups"]] == ["Amount", "Total"]

    def test_set_chart_series_type(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_series_type

        set_chart_series_type(str(rich_path), "SalesByProduct", "Amount", "Line", "Smooth")
        amount = _series(get_chart(str(rich_path), "SalesByProduct"), "Amount")
        assert (amount["type"], amount["subtype"], amount["type_implicit"]) == (
            "Line",
            "Smooth",
            False,
        )

    def test_set_chart_series_grouping(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_series_grouping

        set_chart_series_grouping(
            str(rich_path), "SalesByProduct", "Amount", group_field="ProductName", sort="Descending"
        )
        groups = get_chart(str(rich_path), "SalesByProduct")["series_groups"]
        assert groups[0] == {
            "name": "Amount_Group",
            "expression": "=Fields!ProductName.Value",
            "label": "=Fields!ProductName.Value",
            "sort": [{"value": "=Fields!ProductName.Value", "direction": "Descending"}],
            "static": False,
            "level": 0,
            "series": "Amount",
        }
        assert [g["static"] for g in groups[1:]] == [True, True]

    def test_set_chart_legend(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_legend

        set_chart_legend(str(rich_path), "SalesByProduct", position="BottomCenter", visible=False)
        c = get_chart(str(rich_path), "SalesByProduct")
        assert c["legend"] == {"name": "Default", "position": "BottomCenter", "visible": "false"}
        assert c["legends"] == [c["legend"]]

    def test_set_chart_data_labels(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_data_labels

        set_chart_data_labels(
            str(rich_path),
            "SalesByProduct",
            series_name="Amount",
            visible=True,
            position="Top",
            use_value_as_label=True,
            color="Red",
            font_weight="Bold",
            format="N0",
        )
        labels = _series(get_chart(str(rich_path), "SalesByProduct"), "Amount")["data_labels"]
        assert labels == {
            "source": "point",
            "visible": "true",
            "position": "Top",
            "use_value_as_label": "true",
            "format": "N0",
            "font_weight": "Bold",
            "color": "Red",
        }

    def test_set_chart_palette(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_palette

        set_chart_palette(str(rich_path), "SalesByProduct", "Pastel")
        assert get_chart(str(rich_path), "SalesByProduct")["palette"] == "Pastel"

    def test_set_series_color(self, rich_path):
        from pbirb_mcp.ops.chart import set_series_color

        set_series_color(str(rich_path), "SalesByProduct", "Total", "#336699")
        assert _series(get_chart(str(rich_path), "SalesByProduct"), "Total")["color"] == "#336699"

    def test_set_chart_title(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_title

        set_chart_title(str(rich_path), "SalesByProduct", "Revenue")
        c = get_chart(str(rich_path), "SalesByProduct")
        assert c["title"] == {"name": "Default", "caption": "Revenue"}
        assert c["titles"] == [c["title"]]

    def test_set_chart_series_action_hyperlink(self, rich_path):
        from pbirb_mcp.ops.actions import set_chart_series_action

        set_chart_series_action(
            str(rich_path), "SalesByProduct", "Amount", "Hyperlink", "https://example.test"
        )
        c = get_chart(str(rich_path), "SalesByProduct")
        assert _series(c, "Amount")["action"] == {
            "action_type": "Hyperlink",
            "target_expression": "https://example.test",
        }
        assert _series(c, "Total")["action"] is None

    def test_set_chart_series_action_drillthrough(self, rich_path):
        from pbirb_mcp.ops.actions import set_chart_series_action

        set_chart_series_action(
            str(rich_path),
            "SalesByProduct",
            "Total",
            "Drillthrough",
            "Detail",
            drillthrough_parameters=[{"name": "P", "value": "=Fields!ProductID.Value"}],
        )
        total = _series(get_chart(str(rich_path), "SalesByProduct"), "Total")
        assert total["action"] == {
            "action_type": "Drillthrough",
            "target_expression": "Detail",
            "drillthrough_parameters": [{"name": "P", "value": "=Fields!ProductID.Value"}],
        }


def _patch_chart(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


class TestReportBuilderShapes:
    def test_missing_type_is_column(self, rich_path):
        _patch_chart(rich_path, "<Type>Column</Type>", "")
        amount = _series(get_chart(str(rich_path), "SalesByProduct"), "Amount")
        assert amount["type"] == "Column"
        assert amount["type_implicit"] is True

    def test_series_bindings(self, rich_path):
        _patch_chart(
            rich_path,
            "<Type>Line</Type><Subtype>Plain</Subtype>",
            "<Type>Line</Type><Subtype>Plain</Subtype><LegendName>Side</LegendName>"
            "<ValueAxisName>Secondary</ValueAxisName><CategoryAxisName>Primary</CategoryAxisName>"
            "<ChartAreaName>Default</ChartAreaName>",
        )
        _patch_chart(
            rich_path,
            '<ChartSeries Name="Total">',
            '<ChartSeries Name="Total"><Hidden>true</Hidden>',
        )
        c = get_chart(str(rich_path), "SalesByProduct")
        qty = _series(c, "Quantity")
        assert (qty["legend_name"], qty["value_axis"], qty["category_axis"], qty["chart_area"]) == (
            "Side",
            "Secondary",
            "Primary",
            "Default",
        )
        assert _series(c, "Total")["hidden"] == "true"
        assert _series(c, "Amount")["value_axis"] is None

    def test_every_area_legend_and_title(self, rich_path):
        _patch_chart(
            rich_path,
            "</ChartArea></ChartAreas>",
            '</ChartArea><ChartArea Name="Lower"><ChartCategoryAxes><ChartAxis Name="Primary" />'
            '</ChartCategoryAxes><ChartValueAxes><ChartAxis Name="Primary"><Minimum>5</Minimum>'
            "</ChartAxis></ChartValueAxes></ChartArea></ChartAreas>",
        )
        _patch_chart(
            rich_path,
            '<ChartLegend Name="Default" />',
            '<ChartLegend Name="Default" /><ChartLegend Name="Side"><Hidden>true</Hidden>'
            "</ChartLegend>",
        )
        _patch_chart(
            rich_path,
            "</ChartTitle></ChartTitles>",
            '</ChartTitle><ChartTitle Name="Sub"><Caption>FY</Caption></ChartTitle></ChartTitles>',
        )
        c = get_chart(str(rich_path), "SalesByProduct")
        assert [a["name"] for a in c["chart_areas"]] == ["Default", "Lower"]
        assert c["chart_areas"][1]["axes"]["value"][0]["min"] == "5"
        assert c["axes"] == c["chart_areas"][0]["axes"]
        assert [lg["name"] for lg in c["legends"]] == ["Default", "Side"]
        assert c["legends"][1]["visible"] == "false"
        assert c["titles"][1] == {"name": "Sub", "caption": "FY"}
        assert c["title"]["name"] == "Default"

    def test_derived_series(self, rich_path):
        _patch_chart(
            rich_path,
            "</ChartSeriesCollection>",
            "</ChartSeriesCollection><ChartDerivedSeriesCollection><ChartDerivedSeries>"
            '<ChartSeries Name="AmountAvg"><ChartDataPoints /><Type>Line</Type></ChartSeries>'
            "<SourceChartSeriesName>Amount</SourceChartSeriesName>"
            "<DerivedSeriesFormula>MovingAverage</DerivedSeriesFormula>"
            '<ChartFormulaParameters><ChartFormulaParameter Name="Period"><Value>3</Value>'
            "</ChartFormulaParameter></ChartFormulaParameters>"
            "</ChartDerivedSeries></ChartDerivedSeriesCollection>",
        )
        c = get_chart(str(rich_path), "SalesByProduct")
        assert c["derived_series"] == [
            {
                "name": "AmountAvg",
                "source": "Amount",
                "formula": "MovingAverage",
                "parameters": [{"name": "Period", "value": "3", "source": None}],
                "type": "Line",
            }
        ]
        assert [s["name"] for s in c["series"]] == ["Amount", "Quantity", "Total"]

    def test_nested_series_groups_in_order(self, rich_path):
        _patch_chart(
            rich_path,
            "<ChartMember><Label>Amount</Label></ChartMember>",
            '<ChartMember><Group Name="Year"><GroupExpressions><GroupExpression>'
            "=Fields!ProductID.Value</GroupExpression></GroupExpressions></Group><ChartMembers>"
            "<ChartMember><Label>Amount</Label></ChartMember></ChartMembers>"
            "<Label>=Fields!ProductID.Value</Label></ChartMember>",
        )
        groups = get_chart(str(rich_path), "SalesByProduct")["series_groups"]
        assert [(g["name"], g["level"], g["static"], g["series"]) for g in groups] == [
            ("Year", 0, False, None),
            (None, 1, True, "Amount"),
            (None, 0, True, "Quantity"),
            (None, 0, True, "Total"),
        ]

    def test_custom_palette_colors(self, rich_path):
        _patch_chart(
            rich_path,
            "<Palette>EarthTones</Palette>",
            "<Palette>Custom</Palette><ChartCustomPaletteColors>"
            "<ChartCustomPaletteColor>#112233</ChartCustomPaletteColor>"
            "<ChartCustomPaletteColor>Red</ChartCustomPaletteColor>"
            "</ChartCustomPaletteColors><PaletteHatchBehavior>Always</PaletteHatchBehavior>",
        )
        c = get_chart(str(rich_path), "SalesByProduct")
        assert c["palette"] == "Custom"
        assert c["custom_palette_colors"] == ["#112233", "Red"]
        assert c["palette_hatch_behavior"] == "Always"

    def test_legacy_series_level_action(self, rich_path):
        _patch_chart(
            rich_path,
            '<ChartSeries Name="Total">',
            '<ChartSeries Name="Total"><ActionInfo><Actions><Action><BookmarkLink>B1'
            "</BookmarkLink></Action></Actions></ActionInfo>",
        )
        total = _series(get_chart(str(rich_path), "SalesByProduct"), "Total")
        assert total["action"] == {"action_type": "BookmarkLink", "target_expression": "B1"}

    def test_concise_view_lists_dynamic_series_groups(self, rich_path):
        from pbirb_mcp.ops.chart import set_chart_series_grouping
        from pbirb_mcp.ops.response_format import with_response_format

        set_chart_series_grouping(
            str(rich_path), "SalesByProduct", "Amount", group_field="ProductName"
        )
        concise = with_response_format("get_chart", get_chart)(
            str(rich_path), "SalesByProduct", response_format="concise"
        )
        assert concise["series_groups"] == ["Amount_Group"]
