"""Chart-authoring tool tests.

Covers ``insert_chart_from_template`` (originally in
``test_templates.py``; moved here in v0.3.0 alongside the extraction
to ``pbirb_mcp.ops.chart``). v0.3.0 chart-mutation tests
(``add_chart_series`` / ``set_chart_axis`` / etc.) extend this file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.chart import (
    add_chart_series,
    insert_chart_from_template,
    remove_chart_series,
    set_chart_axis,
    set_chart_data_labels,
    set_chart_legend,
    set_chart_palette,
    set_chart_series_type,
    set_chart_title,
    set_series_color,
)
from pbirb_mcp.ops.reader import get_chart
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
FIXTURE_RICH = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def rich_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_RICH, dest)
    return dest


def _body_items(rdl_path: Path):
    doc = RDLDocument.open(rdl_path)
    items = doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body/{{{RDL_NS}}}ReportItems")
    return list(items) if items is not None else []


class TestInsertChartTemplate:
    def test_inserts_chart_into_body(self, rdl_path):
        result = insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in",
            left="0.5in",
            width="5in",
            height="3in",
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
            top="3in",
            left="0.5in",
            width="5in",
            height="3in",
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
            top="3in",
            left="0.5in",
            width="5in",
            height="3in",
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
            top="3in",
            left="0.5in",
            width="5in",
            height="3in",
        )
        doc = RDLDocument.open(rdl_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesChart']")
        y = chart.find(
            f"{q('ChartData')}/{q('ChartSeriesCollection')}/{q('ChartSeries')}/"
            f"{q('ChartDataPoints')}/{q('ChartDataPoint')}/{q('ChartDataPointValues')}/{q('Y')}"
        )
        assert y.text == "=Sum(Fields!Amount.Value)"

    def test_series_chart_member_has_label(self, rdl_path):
        # Regression: Report Builder's deserializer rejects an empty
        # <ChartMember> with "missing mandatory child element of type
        # 'Label'", even though lxml round-trips it cleanly. Both the
        # category and series ChartMembers need a Label.
        insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in",
            left="0.5in",
            width="5in",
            height="3in",
        )
        doc = RDLDocument.open(rdl_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesChart']")
        series_label = chart.find(
            f"{q('ChartSeriesHierarchy')}/{q('ChartMembers')}/{q('ChartMember')}/{q('Label')}"
        )
        assert series_label is not None
        assert series_label.text == "Amount"

    def test_axes_collection_holds_chart_axis_directly(self, rdl_path):
        # Regression: ChartCategoryAxes / ChartValueAxes accept <ChartAxis>
        # children DIRECTLY per the RDL XSD. Wrapping them in
        # <ChartCategoryAxis>/<ChartValueAxis> is invalid and Report
        # Builder's deserializer rejects it explicitly.
        insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in",
            left="0.5in",
            width="5in",
            height="3in",
        )
        doc = RDLDocument.open(rdl_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesChart']")
        cat_axes = chart.find(f".//{q('ChartArea')}/{q('ChartCategoryAxes')}")
        val_axes = chart.find(f".//{q('ChartArea')}/{q('ChartValueAxes')}")
        assert [etree.QName(c).localname for c in list(cat_axes)] == ["ChartAxis"]
        assert [etree.QName(c).localname for c in list(val_axes)] == ["ChartAxis"]
        assert chart.find(f".//{q('ChartCategoryAxis')}") is None
        assert chart.find(f".//{q('ChartValueAxis')}") is None

    def test_default_chart_type_is_column(self, rdl_path):
        insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in",
            left="0.5in",
            width="5in",
            height="3in",
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
                name="MainTable",  # already exists in fixture
                dataset_name="MainDataset",
                category_field="ProductName",
                value_field="Amount",
                top="0in",
                left="0in",
                width="3in",
                height="2in",
            )

    def test_unknown_dataset_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            insert_chart_from_template(
                path=str(rdl_path),
                name="SalesChart",
                dataset_name="NoSuchDataset",
                category_field="ProductName",
                value_field="Amount",
                top="0in",
                left="0in",
                width="3in",
                height="2in",
            )

    def test_round_trip_safe(self, rdl_path):
        insert_chart_from_template(
            path=str(rdl_path),
            name="SalesChart",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="3in",
            left="0.5in",
            width="5in",
            height="3in",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


class TestAddChartSeries:
    def test_appends_series(self, rich_path):
        result = add_chart_series(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Discount",
            value_field="ProductID",
        )
        assert result["name"] == "Discount"
        assert result["kind"] == "ChartSeries"
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert "Discount" in [s["name"] for s in c["series"]]

    def test_default_type_and_subtype(self, rich_path):
        add_chart_series(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Q1",
            value_field="Amount",
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        q1 = next(s for s in c["series"] if s["name"] == "Q1")
        assert q1["type"] == "Column"
        assert q1["subtype"] == "Plain"

    def test_explicit_type_subtype(self, rich_path):
        add_chart_series(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Trend",
            value_field="Amount",
            series_type="Line",
            series_subtype="Smooth",
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        trend = next(s for s in c["series"] if s["name"] == "Trend")
        assert trend["type"] == "Line"
        assert trend["subtype"] == "Smooth"

    def test_value_expression_uses_sum(self, rich_path):
        add_chart_series(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Latest",
            value_field="Amount",
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        latest = next(s for s in c["series"] if s["name"] == "Latest")
        assert latest["value_expression"] == "=Sum(Fields!Amount.Value)"

    def test_duplicate_series_name_rejected(self, rich_path):
        with pytest.raises(ValueError):
            add_chart_series(
                path=str(rich_path),
                chart_name="SalesByProduct",
                series_name="Amount",  # already exists
                value_field="ProductID",
            )

    def test_unknown_chart_rejected(self, rich_path):
        with pytest.raises(ElementNotFoundError):
            add_chart_series(
                path=str(rich_path),
                chart_name="NoSuchChart",
                series_name="X",
                value_field="Amount",
            )

    def test_invalid_type_rejected(self, rich_path):
        with pytest.raises(ValueError):
            add_chart_series(
                path=str(rich_path),
                chart_name="SalesByProduct",
                series_name="X",
                value_field="Amount",
                series_type="NotARealType",
            )

    def test_round_trip_safe(self, rich_path):
        add_chart_series(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Z",
            value_field="Amount",
        )
        RDLDocument.open(rich_path).validate()


class TestRemoveChartSeries:
    def test_removes_named(self, rich_path):
        result = remove_chart_series(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Quantity",
        )
        assert result["removed"] == "Quantity"
        assert "Quantity" not in result["remaining"]
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert "Quantity" not in [s["name"] for s in c["series"]]

    def test_refuses_to_remove_last_series(self, rdl_path):
        # Insert a chart with the template (1 series only).
        insert_chart_from_template(
            path=str(rdl_path),
            name="OneSeries",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="0in",
            left="0in",
            width="3in",
            height="2in",
        )
        with pytest.raises(ValueError, match="last series"):
            remove_chart_series(
                path=str(rdl_path),
                chart_name="OneSeries",
                series_name="Amount",
            )

    def test_unknown_series_rejected(self, rich_path):
        with pytest.raises(ElementNotFoundError):
            remove_chart_series(
                path=str(rich_path),
                chart_name="SalesByProduct",
                series_name="NoSuch",
            )


class TestSetChartSeriesType:
    def test_changes_type_and_subtype(self, rich_path):
        result = set_chart_series_type(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            series_type="Bar",
            series_subtype="Stacked",
        )
        assert "Type" in result["changed"]
        assert "Subtype" in result["changed"]
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        amount = next(s for s in c["series"] if s["name"] == "Amount")
        assert amount["type"] == "Bar"
        assert amount["subtype"] == "Stacked"

    def test_no_op_when_unchanged(self, rich_path):
        # Total is already Bar/Stacked in the fixture.
        result = set_chart_series_type(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Total",
            series_type="Bar",
            series_subtype="Stacked",
        )
        assert result["changed"] == []

    def test_changed_omits_unmodified_field(self, rich_path):
        # Total is Bar/Stacked. Change only subtype to Plain.
        result = set_chart_series_type(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Total",
            series_type="Bar",       # unchanged
            series_subtype="Plain",  # changed
        )
        assert "Type" not in result["changed"]
        assert "Subtype" in result["changed"]

    def test_invalid_type_rejected(self, rich_path):
        with pytest.raises(ValueError):
            set_chart_series_type(
                path=str(rich_path),
                chart_name="SalesByProduct",
                series_name="Amount",
                series_type="NotReal",
            )

    def test_unknown_series_rejected(self, rich_path):
        with pytest.raises(ElementNotFoundError):
            set_chart_series_type(
                path=str(rich_path),
                chart_name="SalesByProduct",
                series_name="Ghost",
                series_type="Bar",
            )


class TestComboChartSurface:
    """Verify that mixed series types coexist — the combo-chart pattern
    that motivates set_chart_series_type and add_chart_series."""

    def test_three_distinct_series_types(self, rich_path):
        # Fixture already has Column, Line, Bar/Stacked.
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        types = sorted({s["type"] for s in c["series"]})
        assert types == ["Bar", "Column", "Line"]


class TestSetChartAxis:
    def test_sets_title(self, rich_path):
        result = set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Value",
            title="Revenue (USD)",
        )
        assert "Title" in result["changed"]
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["axes"]["value"][0]["title"] == "Revenue (USD)"

    def test_clears_title_with_empty_string(self, rich_path):
        # Fixture's value axis has title "Amount (USD)"; clear it.
        set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Value",
            title="",
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["axes"]["value"][0]["title"] is None

    def test_title_writes_chartaxistitle_not_bare_title(self, rich_path):
        # Per RDL 2016 schema, the title element on a ChartAxis is
        # <ChartAxisTitle>, NOT <Title>. RB rejects bare <Title> with
        # "has invalid child element 'Title'". Pre-v0.3.1 emitted the
        # wrong name.
        set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Value",
            title="Revenue",
        )
        # Walk to the axis element and inspect children directly.
        doc = RDLDocument.open(rich_path)
        axis_el = doc.root.find(
            f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']/"
            f"{{{RDL_NS}}}ChartAreas/{{{RDL_NS}}}ChartArea/"
            f"{{{RDL_NS}}}ChartValueAxes/{{{RDL_NS}}}ChartAxis"
        )
        children_locals = [etree.QName(c).localname for c in axis_el]
        assert "ChartAxisTitle" in children_locals
        assert "Title" not in children_locals

    def test_legacy_title_element_migrates_on_rewrite(self, rich_path):
        # Inject a legacy <Title> element directly under the axis (the
        # buggy pre-v0.3.1 shape). On rewrite, the setter must REMOVE
        # the bad element and emit <ChartAxisTitle> instead.
        from pbirb_mcp.core.xpath import q as _q

        doc = RDLDocument.open(rich_path)
        axis_el = doc.root.find(
            f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']/"
            f"{{{RDL_NS}}}ChartAreas/{{{RDL_NS}}}ChartArea/"
            f"{{{RDL_NS}}}ChartValueAxes/{{{RDL_NS}}}ChartAxis"
        )
        # Drop any existing ChartAxisTitle then inject the legacy shape.
        for old in list(axis_el):
            if etree.QName(old).localname in ("ChartAxisTitle", "Title"):
                axis_el.remove(old)
        legacy = etree.SubElement(axis_el, _q("Title"))
        etree.SubElement(legacy, _q("Caption")).text = "Stale Title"
        doc.save()

        set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Value",
            title="New Title",
        )

        doc = RDLDocument.open(rich_path)
        axis_el = doc.root.find(
            f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']/"
            f"{{{RDL_NS}}}ChartAreas/{{{RDL_NS}}}ChartArea/"
            f"{{{RDL_NS}}}ChartValueAxes/{{{RDL_NS}}}ChartAxis"
        )
        children_locals = [etree.QName(c).localname for c in axis_el]
        assert "Title" not in children_locals
        assert "ChartAxisTitle" in children_locals
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["axes"]["value"][0]["title"] == "New Title"

    def test_sets_format_via_style(self, rich_path):
        set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Value",
            format="$#,0",
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["axes"]["value"][0]["format"] == "$#,0"

    def test_sets_min_max_interval(self, rich_path):
        result = set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Value",
            min="0",
            max="1000",
            interval="100",
        )
        assert set(result["changed"]) >= {"Minimum", "Maximum", "Interval"}
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        val = c["axes"]["value"][0]
        assert val["min"] == "0"
        assert val["max"] == "1000"
        assert val["interval"] == "100"

    def test_log_scale_visible_booleans(self, rich_path):
        result = set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Value",
            log_scale=True,
            visible=False,
        )
        assert "LogScale" in result["changed"]
        assert "Visible" in result["changed"]
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        val = c["axes"]["value"][0]
        assert val["log_scale"] == "true"
        assert val["visible"] == "false"

    def test_no_args_returns_empty_changed(self, rich_path):
        before = (rich_path).read_bytes()
        result = set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Value",
        )
        assert result["changed"] == []
        # No-op short-circuit: no save.
        assert (rich_path).read_bytes() == before

    def test_invalid_axis_kind_rejected(self, rich_path):
        with pytest.raises(ValueError):
            set_chart_axis(
                path=str(rich_path),
                chart_name="SalesByProduct",
                axis="Whatever",
            )

    def test_unknown_axis_name_rejected(self, rich_path):
        with pytest.raises(ElementNotFoundError):
            set_chart_axis(
                path=str(rich_path),
                chart_name="SalesByProduct",
                axis="Value",
                axis_name="Secondary",  # not present in fixture
                title="X",
            )

    def test_category_axis_too(self, rich_path):
        result = set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Category",
            title="Category",
        )
        assert "Title" in result["changed"]
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["axes"]["category"][0]["title"] == "Category"

    def test_round_trip_safe(self, rich_path):
        set_chart_axis(
            path=str(rich_path),
            chart_name="SalesByProduct",
            axis="Value",
            title="Y",
            format="#,0.00",
            min="0",
            max="1000",
            log_scale=False,
            interval="50",
            visible=True,
        )
        RDLDocument.open(rich_path).validate()


class TestSetChartLegend:
    def test_sets_position(self, rich_path):
        result = set_chart_legend(
            path=str(rich_path),
            chart_name="SalesByProduct",
            position="BottomCenter",
        )
        assert "Position" in result["changed"]
        # Confirm via raw XML.
        doc = RDLDocument.open(rich_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']")
        legend = chart.find(f"{q('ChartLegends')}/{q('ChartLegend')}")
        pos = find_child(legend, "Position")
        assert pos.text == "BottomCenter"

    def test_sets_hidden_when_visible_false(self, rich_path):
        result = set_chart_legend(
            path=str(rich_path),
            chart_name="SalesByProduct",
            visible=False,
        )
        assert "Hidden" in result["changed"]
        doc = RDLDocument.open(rich_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']")
        legend = chart.find(f"{q('ChartLegends')}/{q('ChartLegend')}")
        assert find_child(legend, "Hidden").text == "true"

    def test_no_args_returns_empty_changed(self, rich_path):
        before = (rich_path).read_bytes()
        result = set_chart_legend(
            path=str(rich_path),
            chart_name="SalesByProduct",
        )
        assert result["changed"] == []
        assert (rich_path).read_bytes() == before

    def test_invalid_position_rejected(self, rich_path):
        with pytest.raises(ValueError):
            set_chart_legend(
                path=str(rich_path),
                chart_name="SalesByProduct",
                position="Floating",  # not in enum
            )

    def test_unknown_legend_name_rejected(self, rich_path):
        with pytest.raises(ElementNotFoundError):
            set_chart_legend(
                path=str(rich_path),
                chart_name="SalesByProduct",
                legend_name="Secondary",
                position="TopRight",
            )

    def test_idempotent_when_position_unchanged(self, rich_path):
        set_chart_legend(
            path=str(rich_path),
            chart_name="SalesByProduct",
            position="BottomCenter",
        )
        result = set_chart_legend(
            path=str(rich_path),
            chart_name="SalesByProduct",
            position="BottomCenter",
        )
        assert result["changed"] == []

    def test_get_chart_visible_inverts_hidden(self, rich_path):
        # Regression: a live MCP smoke-test revealed that
        # set_chart_legend(visible=True) wrote <Hidden>false</Hidden>
        # correctly, but get_chart echoed the raw <Hidden> text under
        # the field name 'visible' — flipping the contract. Fix lives
        # in reader._chart_legend_dict; this test guards both ends.
        set_chart_legend(
            path=str(rich_path),
            chart_name="SalesByProduct",
            visible=True,
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["legend"]["visible"] == "true"

        set_chart_legend(
            path=str(rich_path),
            chart_name="SalesByProduct",
            visible=False,
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["legend"]["visible"] == "false"


class TestSetChartDataLabels:
    def test_visible_on_one_series(self, rich_path):
        result = set_chart_data_labels(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            visible=True,
        )
        assert result["series"] == ["Amount"]
        assert "Visible" in result["changed"]
        # Verify via raw XML.
        doc = RDLDocument.open(rich_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']")
        amount_series = next(
            s
            for s in chart.iter(f"{{{RDL_NS}}}ChartSeries")
            if s.get("Name") == "Amount"
        )
        label = find_child(amount_series, "ChartDataLabel")
        assert label is not None
        assert find_child(label, "Visible").text == "true"

    def test_visible_on_all_series_when_none(self, rich_path):
        result = set_chart_data_labels(
            path=str(rich_path),
            chart_name="SalesByProduct",
            visible=True,
        )
        # All three series in the rich fixture get touched.
        assert sorted(result["series"]) == ["Amount", "Quantity", "Total"]
        doc = RDLDocument.open(rich_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']")
        for series in chart.iter(f"{{{RDL_NS}}}ChartSeries"):
            label = find_child(series, "ChartDataLabel")
            assert label is not None
            assert find_child(label, "Visible").text == "true"

    def test_format_lands_in_style(self, rich_path):
        set_chart_data_labels(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            format="$#,0",
        )
        doc = RDLDocument.open(rich_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']")
        amount_series = next(
            s
            for s in chart.iter(f"{{{RDL_NS}}}ChartSeries")
            if s.get("Name") == "Amount"
        )
        label = find_child(amount_series, "ChartDataLabel")
        style = find_child(label, "Style")
        assert find_child(style, "Format").text == "$#,0"

    def test_clear_format_with_empty_string(self, rich_path):
        set_chart_data_labels(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            format="$#,0",
        )
        result = set_chart_data_labels(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            format="",
        )
        assert "Style.Format" in result["changed"]
        doc = RDLDocument.open(rich_path)
        chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']")
        amount_series = next(
            s
            for s in chart.iter(f"{{{RDL_NS}}}ChartSeries")
            if s.get("Name") == "Amount"
        )
        label = find_child(amount_series, "ChartDataLabel")
        style = find_child(label, "Style")
        assert find_child(style, "Format") is None

    def test_no_args_returns_empty_changed(self, rich_path):
        result = set_chart_data_labels(
            path=str(rich_path),
            chart_name="SalesByProduct",
        )
        assert result["changed"] == []

    def test_unknown_series_rejected(self, rich_path):
        with pytest.raises(ElementNotFoundError):
            set_chart_data_labels(
                path=str(rich_path),
                chart_name="SalesByProduct",
                series_name="NoSuch",
                visible=True,
            )


class TestSetChartPalette:
    def test_changes_palette(self, rich_path):
        result = set_chart_palette(
            path=str(rich_path),
            chart_name="SalesByProduct",
            palette="Pastel",
        )
        assert result["changed"] is True
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["palette"] == "Pastel"

    def test_no_op_when_unchanged(self, rich_path):
        # Fixture's palette is already EarthTones.
        before = (rich_path).read_bytes()
        result = set_chart_palette(
            path=str(rich_path),
            chart_name="SalesByProduct",
            palette="EarthTones",
        )
        assert result["changed"] is False
        assert (rich_path).read_bytes() == before

    def test_clear_with_empty_string(self, rich_path):
        result = set_chart_palette(
            path=str(rich_path),
            chart_name="SalesByProduct",
            palette="",
        )
        assert result["changed"] is True
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["palette"] is None

    def test_clear_idempotent_when_already_absent(self, rdl_path):
        # Insert a chart without a palette (template-default).
        insert_chart_from_template(
            path=str(rdl_path),
            name="NoPalette",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="0in",
            left="0in",
            width="3in",
            height="2in",
        )
        result = set_chart_palette(
            path=str(rdl_path),
            chart_name="NoPalette",
            palette="",
        )
        assert result["changed"] is False

    def test_invalid_palette_rejected(self, rich_path):
        with pytest.raises(ValueError):
            set_chart_palette(
                path=str(rich_path),
                chart_name="SalesByProduct",
                palette="NotARealPalette",
            )

    def test_round_trip_safe(self, rich_path):
        set_chart_palette(
            path=str(rich_path),
            chart_name="SalesByProduct",
            palette="GrayScale",
        )
        RDLDocument.open(rich_path).validate()


class TestSetSeriesColor:
    def test_writes_color_into_style(self, rich_path):
        result = set_series_color(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            color="#FF0000",
        )
        assert result["changed"] is True
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        amount = next(s for s in c["series"] if s["name"] == "Amount")
        assert amount["color"] == "#FF0000"

    def test_named_color(self, rich_path):
        set_series_color(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Quantity",
            color="Red",
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        qty = next(s for s in c["series"] if s["name"] == "Quantity")
        assert qty["color"] == "Red"

    def test_no_op_when_color_unchanged(self, rich_path):
        set_series_color(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            color="#FF0000",
        )
        result = set_series_color(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            color="#FF0000",
        )
        assert result["changed"] is False

    def test_clear_with_empty_string(self, rich_path):
        set_series_color(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            color="#FF0000",
        )
        result = set_series_color(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            color="",
        )
        assert result["changed"] is True
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        amount = next(s for s in c["series"] if s["name"] == "Amount")
        assert amount["color"] is None

    def test_clear_idempotent_when_no_color_set(self, rich_path):
        # Fresh fixture has no Color on any series.
        result = set_series_color(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            color="",
        )
        assert result["changed"] is False

    def test_unknown_series_rejected(self, rich_path):
        with pytest.raises(ElementNotFoundError):
            set_series_color(
                path=str(rich_path),
                chart_name="SalesByProduct",
                series_name="Ghost",
                color="Red",
            )

    def test_round_trip_safe(self, rich_path):
        set_series_color(
            path=str(rich_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            color="#00FF00",
        )
        RDLDocument.open(rich_path).validate()


class TestSetChartTitle:
    def test_changes_caption(self, rich_path):
        result = set_chart_title(
            path=str(rich_path),
            chart_name="SalesByProduct",
            text="Quarterly Sales",
        )
        assert result["changed"] is True
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["title"]["caption"] == "Quarterly Sales"

    def test_no_op_when_unchanged(self, rich_path):
        # Fixture title is "Sales by Product".
        result = set_chart_title(
            path=str(rich_path),
            chart_name="SalesByProduct",
            text="Sales by Product",
        )
        assert result["changed"] is False

    def test_expression_caption(self, rich_path):
        set_chart_title(
            path=str(rich_path),
            chart_name="SalesByProduct",
            text="=Globals!ExecutionTime",
        )
        c = get_chart(path=str(rich_path), name="SalesByProduct")
        assert c["title"]["caption"] == "=Globals!ExecutionTime"

    def test_unknown_title_name_rejected(self, rich_path):
        with pytest.raises(ElementNotFoundError):
            set_chart_title(
                path=str(rich_path),
                chart_name="SalesByProduct",
                title_name="Subtitle",
                text="X",
            )

    def test_round_trip_safe(self, rich_path):
        set_chart_title(
            path=str(rich_path),
            chart_name="SalesByProduct",
            text="Round-Trip Title",
        )
        RDLDocument.open(rich_path).validate()


class TestBackwardCompatibleImport:
    """v0.3 moved insert_chart_from_template from ops.templates → ops.chart.
    The old import path stays valid via re-export so existing callers
    don't break."""

    def test_re_export_via_templates(self, rdl_path):
        from pbirb_mcp.ops.templates import insert_chart_from_template as via_templates
        from pbirb_mcp.ops.chart import insert_chart_from_template as via_chart

        assert via_templates is via_chart

        # The re-exported handle works end-to-end.
        result = via_templates(
            path=str(rdl_path),
            name="ReExport",
            dataset_name="MainDataset",
            category_field="ProductName",
            value_field="Amount",
            top="0in",
            left="0in",
            width="3in",
            height="2in",
        )
        assert result["name"] == "ReExport"


class TestToolRegistration:
    def test_chart_tool_still_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "insert_chart_from_template" in names

    def test_series_crud_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {
            "add_chart_series",
            "remove_chart_series",
            "set_chart_series_type",
        } <= names

    def test_set_chart_axis_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "set_chart_axis" in names

    def test_legend_and_labels_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {"set_chart_legend", "set_chart_data_labels"} <= names

    def test_palette_color_title_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {
            "set_chart_palette",
            "set_series_color",
            "set_chart_title",
        } <= names
