"""Tests for the Phase 5 action / tooltip / document-map tools.

Covers ``set_textbox_action``, ``set_image_action``,
``set_textbox_tooltip``, ``set_document_map_label`` (commit 23) and
``set_chart_series_action`` (commit 24).

Each tool exercises the schema-aware insertion helper
``_insert_in_item_order`` so the trailing children land in the right
RDL XSD position relative to <Style>.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.actions import (
    set_bookmark,
    set_chart_series_action,
    set_document_map_label,
    set_image_action,
    set_item_action,
    set_item_tooltip,
    set_textbox_action,
    set_textbox_tooltip,
)
from pbirb_mcp.ops.body import add_body_image
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
FIXTURE_CHART = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def chart_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_CHART, dest)
    return dest


def _textbox(rdl_path: Path, name: str) -> etree._Element:
    doc = RDLDocument.open(rdl_path)
    return doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='{name}']")


def _image(rdl_path: Path, name: str) -> etree._Element:
    doc = RDLDocument.open(rdl_path)
    return doc.root.find(f".//{{{RDL_NS}}}Image[@Name='{name}']")


def _inner_action(item: etree._Element) -> etree._Element:
    """Walk a ReportItem (or ChartSeries) to its inner ``<Action>``.

    The wire shape since v0.3.1 is
    ``<ActionInfo>/<Actions>/<Action>`` — Report Builder rejects a
    bare ``<Action>``. Returns the first Action inside the
    ActionInfo/Actions chain, or None when no ActionInfo exists.
    """
    info = find_child(item, "ActionInfo")
    if info is None:
        return None
    actions_root = find_child(info, "Actions")
    if actions_root is None:
        return None
    inner = find_children(actions_root, "Action")
    return inner[0] if inner else None


# ---- set_textbox_action --------------------------------------------------


class TestSetTextboxActionHyperlink:
    def test_writes_hyperlink(self, rdl_path):
        result = set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Hyperlink",
            target_expression="https://example.com",
        )
        assert result["action_type"] == "Hyperlink"
        assert result["changed"] is True
        tb = _textbox(rdl_path, "HeaderProductID")
        action = _inner_action(tb)
        link = find_child(action, "Hyperlink")
        assert link is not None
        assert link.text == "https://example.com"

    def test_idempotent_when_unchanged(self, rdl_path):
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Hyperlink",
            target_expression="https://example.com",
        )
        before = (rdl_path).read_bytes()
        result = set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Hyperlink",
            target_expression="https://example.com",
        )
        assert result["changed"] is False
        assert (rdl_path).read_bytes() == before

    def test_replaces_existing_action(self, rdl_path):
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Hyperlink",
            target_expression="https://old.example.com",
        )
        # A BookmarkLink needs its target bookmark to exist (#109).
        set_bookmark(str(rdl_path), "MainTable", "anchor1")
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="BookmarkLink",
            target_expression="anchor1",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        action = _inner_action(tb)
        # Old Hyperlink is gone; BookmarkLink replaces.
        assert find_child(action, "Hyperlink") is None
        assert find_child(action, "BookmarkLink").text == "anchor1"

    def test_pre_encoded_target_no_double_encode(self, rdl_path):
        # &amp; in the URL must end up &amp; on disk, not &amp;amp;.
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Hyperlink",
            target_expression="https://x?a=1&amp;b=2",
        )
        assert b"&amp;amp;" not in (rdl_path).read_bytes()


class TestSetTextboxActionDrillthrough:
    def test_writes_drillthrough_with_parameters(self, rdl_path):
        result = set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Drillthrough",
            target_expression="DetailReport",
            drillthrough_parameters=[
                {"name": "ProductID", "value": "=Fields!ProductID.Value"},
                {"name": "Category", "value": "Electronics"},
            ],
        )
        assert result["action_type"] == "Drillthrough"
        tb = _textbox(rdl_path, "HeaderProductID")
        action = _inner_action(tb)
        drill = find_child(action, "Drillthrough")
        assert find_child(drill, "ReportName").text == "DetailReport"
        params = drill.findall(f"{q('Parameters')}/{q('Parameter')}")
        assert len(params) == 2
        assert params[0].get("Name") == "ProductID"
        assert find_child(params[0], "Value").text == "=Fields!ProductID.Value"
        assert params[1].get("Name") == "Category"
        assert find_child(params[1], "Value").text == "Electronics"

    def test_drillthrough_no_parameters(self, rdl_path):
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Drillthrough",
            target_expression="DetailReport",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        action = _inner_action(tb)
        drill = find_child(action, "Drillthrough")
        assert find_child(drill, "ReportName").text == "DetailReport"
        # No <Parameters> when no parameters supplied.
        assert find_child(drill, "Parameters") is None

    def test_drillthrough_idempotent_with_parameters(self, rdl_path):
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Drillthrough",
            target_expression="DetailReport",
            drillthrough_parameters=[{"name": "ProductID", "value": "=Fields!ProductID.Value"}],
        )
        before = (rdl_path).read_bytes()
        result = set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Drillthrough",
            target_expression="DetailReport",
            drillthrough_parameters=[{"name": "ProductID", "value": "=Fields!ProductID.Value"}],
        )
        assert result["changed"] is False
        assert (rdl_path).read_bytes() == before

    def test_drillthrough_param_missing_keys_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="must have 'name' and 'value'"):
            set_textbox_action(
                path=str(rdl_path),
                textbox_name="HeaderProductID",
                action_type="Drillthrough",
                target_expression="DetailReport",
                drillthrough_parameters=[{"name": "X"}],  # missing value
            )

    def test_invalid_action_type(self, rdl_path):
        with pytest.raises(ValueError, match="not valid"):
            set_textbox_action(
                path=str(rdl_path),
                textbox_name="HeaderProductID",
                action_type="Magic",
                target_expression="x",
            )

    def test_empty_target_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            set_textbox_action(
                path=str(rdl_path),
                textbox_name="HeaderProductID",
                action_type="Hyperlink",
                target_expression="   ",
            )

    def test_unknown_textbox_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_textbox_action(
                path=str(rdl_path),
                textbox_name="NoSuchBox",
                action_type="Hyperlink",
                target_expression="x",
            )


class TestLegacyBareActionMigration:
    """Pre-v0.3.1 setters wrote a bare ``<Action>`` directly under a
    ReportItem / ChartSeries. Report Builder rejects that wire shape
    with ``has invalid child element 'Action'``. A repeat call after
    upgrading must DROP the legacy bare Action and write the canonical
    ``<ActionInfo>/<Actions>/<Action>`` envelope, leaving the file
    RB-loadable.
    """

    def test_textbox_legacy_action_dropped_on_rewrite(self, rdl_path):
        # Manually inject the buggy shape that pre-v0.3.1 emitted.
        from pbirb_mcp.core.xpath import q as _q

        doc = RDLDocument.open(rdl_path)
        tb = doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='HeaderProductID']")
        legacy = etree.SubElement(tb, _q("Action"))
        etree.SubElement(legacy, _q("Hyperlink")).text = "https://stale.example"
        doc.save()

        # Re-write via the fixed setter — should remove legacy + add ActionInfo.
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Hyperlink",
            target_expression="https://new.example",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        children_locals = [etree.QName(c).localname for c in tb]
        assert "Action" not in children_locals
        assert "ActionInfo" in children_locals
        link = find_child(_inner_action(tb), "Hyperlink")
        assert link.text == "https://new.example"

    def test_chart_series_legacy_actioninfo_dropped_on_rewrite(self, chart_path):
        # First-pass v0.3.1 wrote <ActionInfo> directly on ChartSeries,
        # which RB still rejects (it's only valid on ChartDataPoint).
        # Inject the wrong-host shape AND legacy bare <Action>; rewriting
        # via the fixed setter must drop both from the ChartSeries level
        # and write ActionInfo on the template ChartDataPoint instead.
        from pbirb_mcp.core.xpath import q as _q

        doc = RDLDocument.open(chart_path)
        s = doc.root.find(
            f".//{{{RDL_NS}}}Chart[@Name='SalesByProduct']/"
            f"{{{RDL_NS}}}ChartData/{{{RDL_NS}}}ChartSeriesCollection/"
            f"{{{RDL_NS}}}ChartSeries"
        )
        # Bare <Action> on ChartSeries (pre-v0.3.1).
        legacy_action = etree.SubElement(s, _q("Action"))
        etree.SubElement(legacy_action, _q("Hyperlink")).text = "https://stale.example"
        # Wrong-host <ActionInfo> on ChartSeries (first-pass v0.3.1).
        legacy_info = etree.SubElement(s, _q("ActionInfo"))
        legacy_actions = etree.SubElement(legacy_info, _q("Actions"))
        legacy_inner = etree.SubElement(legacy_actions, _q("Action"))
        etree.SubElement(legacy_inner, _q("Hyperlink")).text = "https://staler.example"
        doc.save()

        set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="Hyperlink",
            target_expression="https://new.example",
        )

        s = _series(chart_path, "SalesByProduct", "Amount")
        series_locals = [etree.QName(c).localname for c in s]
        # Both legacy shapes scrubbed from ChartSeries.
        assert "Action" not in series_locals
        assert "ActionInfo" not in series_locals
        # ActionInfo is on the template ChartDataPoint with the new value.
        action = _series_data_point_inner_action(s)
        link = find_child(action, "Hyperlink")
        assert link.text == "https://new.example"

    def test_image_legacy_action_dropped_on_rewrite(self, rdl_path):
        from pbirb_mcp.core.xpath import q as _q

        add_body_image(
            path=str(rdl_path),
            name="LegacyLogo",
            image_source="External",
            value="http://x/img.png",
            top="0in",
            left="0in",
            width="2in",
            height="1in",
        )
        # Inject the buggy shape.
        doc = RDLDocument.open(rdl_path)
        img = doc.root.find(f".//{{{RDL_NS}}}Image[@Name='LegacyLogo']")
        legacy = etree.SubElement(img, _q("Action"))
        etree.SubElement(legacy, _q("Hyperlink")).text = "https://stale.example"
        doc.save()

        set_image_action(
            path=str(rdl_path),
            image_name="LegacyLogo",
            action_type="Hyperlink",
            target_expression="https://new.example",
        )
        img = _image(rdl_path, "LegacyLogo")
        children_locals = [etree.QName(c).localname for c in img]
        assert "Action" not in children_locals
        assert "ActionInfo" in children_locals


class TestActionPlacement:
    def test_actioninfo_placed_before_style(self, rdl_path):
        # Per RDL 2016 schema, ActionInfo (the Action wrapper) sits
        # before Style on a Textbox. Bare <Action> directly under a
        # ReportItem makes Report Builder reject the file with
        # "has invalid child element 'Action'".
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Hyperlink",
            target_expression="https://example.com",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        children_locals = [etree.QName(c).localname for c in tb]
        # No bare <Action> at the ReportItem level.
        assert "Action" not in children_locals
        action_info_idx = children_locals.index("ActionInfo")
        style_idx = children_locals.index("Style")
        assert action_info_idx < style_idx


# ---- set_image_action ---------------------------------------------------


class TestSetImageAction:
    def test_writes_hyperlink_on_image(self, rdl_path):
        add_body_image(
            path=str(rdl_path),
            name="Logo",
            image_source="External",
            value="http://x/img.png",
            top="0in",
            left="0in",
            width="2in",
            height="1in",
        )
        result = set_image_action(
            path=str(rdl_path),
            image_name="Logo",
            action_type="Hyperlink",
            target_expression="https://example.com",
        )
        assert result["image"] == "Logo"
        assert result["kind"] == "Image"
        img = _image(rdl_path, "Logo")
        action = _inner_action(img)
        link = find_child(action, "Hyperlink")
        assert link is not None
        assert link.text == "https://example.com"

    def test_unknown_image_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError, match="no Image"):
            set_image_action(
                path=str(rdl_path),
                image_name="NoSuch",
                action_type="Hyperlink",
                target_expression="x",
            )


# ---- set_textbox_tooltip -------------------------------------------------


class TestSetTextboxTooltip:
    def test_writes_tooltip(self, rdl_path):
        result = set_textbox_tooltip(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            text_or_expression="Click for details",
        )
        assert result["changed"] is True
        tb = _textbox(rdl_path, "HeaderProductID")
        assert find_child(tb, "ToolTip").text == "Click for details"

    def test_writes_expression_tooltip(self, rdl_path):
        set_textbox_tooltip(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            text_or_expression="=Fields!ProductName.Value",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        assert find_child(tb, "ToolTip").text == "=Fields!ProductName.Value"

    def test_idempotent_when_unchanged(self, rdl_path):
        set_textbox_tooltip(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            text_or_expression="Hi",
        )
        before = (rdl_path).read_bytes()
        result = set_textbox_tooltip(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            text_or_expression="Hi",
        )
        assert result["changed"] is False
        assert (rdl_path).read_bytes() == before

    def test_clears_with_empty_string(self, rdl_path):
        set_textbox_tooltip(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            text_or_expression="Hi",
        )
        result = set_textbox_tooltip(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            text_or_expression="",
        )
        assert result["changed"] is True
        tb = _textbox(rdl_path, "HeaderProductID")
        assert find_child(tb, "ToolTip") is None

    def test_clear_idempotent_when_no_tooltip(self, rdl_path):
        result = set_textbox_tooltip(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            text_or_expression="",
        )
        assert result["changed"] is False

    def test_tooltip_pre_encoded_no_double_encode(self, rdl_path):
        set_textbox_tooltip(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            text_or_expression="A &amp; B",
        )
        assert b"&amp;amp;" not in (rdl_path).read_bytes()

    def test_tooltip_placed_in_correct_order(self, rdl_path):
        # ToolTip should come before Style.
        set_textbox_tooltip(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            text_or_expression="X",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        children_locals = [etree.QName(c).localname for c in tb]
        tt_idx = children_locals.index("ToolTip")
        style_idx = children_locals.index("Style")
        assert tt_idx < style_idx


# ---- set_document_map_label ----------------------------------------------


class TestSetDocumentMapLabel:
    def test_writes_label_on_textbox(self, rdl_path):
        result = set_document_map_label(
            path=str(rdl_path),
            element_name="HeaderProductID",
            label_or_expression="Product Header",
        )
        assert result["changed"] is True
        assert result["kind"] == "Textbox"
        tb = _textbox(rdl_path, "HeaderProductID")
        assert find_child(tb, "DocumentMapLabel").text == "Product Header"

    def test_writes_label_on_tablix(self, rdl_path):
        result = set_document_map_label(
            path=str(rdl_path),
            element_name="MainTable",
            label_or_expression='="Sales (" & Format(Now(), "yyyy-MM-dd") & ")"',
        )
        assert result["kind"] == "Tablix"
        doc = RDLDocument.open(rdl_path)
        t = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        assert find_child(t, "DocumentMapLabel") is not None

    def test_idempotent_when_unchanged(self, rdl_path):
        set_document_map_label(
            path=str(rdl_path),
            element_name="HeaderProductID",
            label_or_expression="X",
        )
        before = (rdl_path).read_bytes()
        result = set_document_map_label(
            path=str(rdl_path),
            element_name="HeaderProductID",
            label_or_expression="X",
        )
        assert result["changed"] is False
        assert (rdl_path).read_bytes() == before

    def test_clears_with_empty_string(self, rdl_path):
        set_document_map_label(
            path=str(rdl_path),
            element_name="HeaderProductID",
            label_or_expression="X",
        )
        result = set_document_map_label(
            path=str(rdl_path),
            element_name="HeaderProductID",
            label_or_expression="",
        )
        assert result["changed"] is True
        tb = _textbox(rdl_path, "HeaderProductID")
        assert find_child(tb, "DocumentMapLabel") is None

    def test_unknown_element_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError, match="no ReportItem"):
            set_document_map_label(
                path=str(rdl_path),
                element_name="NoSuch",
                label_or_expression="X",
            )


# ---- set_chart_series_action ---------------------------------------------


def _series(chart_path: Path, chart_name: str, series_name: str) -> etree._Element:
    doc = RDLDocument.open(chart_path)
    chart = doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='{chart_name}']")
    sc = chart.find(f"{q('ChartData')}/{q('ChartSeriesCollection')}")
    return next(s for s in find_children(sc, "ChartSeries") if s.get("Name") == series_name)


def _series_data_point_inner_action(series: etree._Element) -> etree._Element:
    """Walk a ChartSeries to its template ChartDataPoint's inner Action.

    Per RDL 2016 schema, ActionInfo lives on
    ``<ChartDataPoints>/<ChartDataPoint>/<ActionInfo>/<Actions>/<Action>``,
    NOT directly under ChartSeries. RB rejects the wrong-host shape.
    """
    cdps = find_child(series, "ChartDataPoints")
    if cdps is None:
        return None
    cdp = find_child(cdps, "ChartDataPoint")
    if cdp is None:
        return None
    return _inner_action(cdp)


class TestSetChartSeriesAction:
    def test_writes_hyperlink_on_series(self, chart_path):
        result = set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="Hyperlink",
            target_expression="https://example.com/{0}",
        )
        assert result["chart"] == "SalesByProduct"
        assert result["series"] == "Amount"
        assert result["kind"] == "ChartSeries"
        assert result["action_type"] == "Hyperlink"
        assert result["changed"] is True
        s = _series(chart_path, "SalesByProduct", "Amount")
        # ActionInfo lives on the template ChartDataPoint, not the series.
        assert find_child(s, "ActionInfo") is None
        assert find_child(s, "Action") is None
        action = _series_data_point_inner_action(s)
        link = find_child(action, "Hyperlink")
        assert link is not None
        assert link.text == "https://example.com/{0}"

    def test_writes_drillthrough_with_parameters(self, chart_path):
        set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="Drillthrough",
            target_expression="ProductDetail",
            drillthrough_parameters=[
                {"name": "ProductID", "value": "=Fields!ProductID.Value"},
            ],
        )
        s = _series(chart_path, "SalesByProduct", "Amount")
        action = _series_data_point_inner_action(s)
        drill = find_child(action, "Drillthrough")
        assert find_child(drill, "ReportName").text == "ProductDetail"
        param = drill.find(f"{q('Parameters')}/{q('Parameter')}")
        assert param.get("Name") == "ProductID"

    def test_idempotent_when_unchanged(self, chart_path):
        set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="Hyperlink",
            target_expression="https://x.example.com",
        )
        before = (chart_path).read_bytes()
        result = set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="Hyperlink",
            target_expression="https://x.example.com",
        )
        assert result["changed"] is False
        assert (chart_path).read_bytes() == before

    def test_replaces_existing_action(self, chart_path):
        set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="Hyperlink",
            target_expression="https://old.example.com",
        )
        set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="BookmarkLink",
            target_expression="amount-anchor",
        )
        s = _series(chart_path, "SalesByProduct", "Amount")
        action = _series_data_point_inner_action(s)
        assert find_child(action, "Hyperlink") is None
        assert find_child(action, "BookmarkLink").text == "amount-anchor"

    def test_unknown_chart_raises(self, chart_path):
        with pytest.raises(ElementNotFoundError, match="no Chart"):
            set_chart_series_action(
                path=str(chart_path),
                chart_name="NoSuchChart",
                series_name="Amount",
                action_type="Hyperlink",
                target_expression="x",
            )

    def test_unknown_series_raises(self, chart_path):
        with pytest.raises(ElementNotFoundError, match="ChartSeries"):
            set_chart_series_action(
                path=str(chart_path),
                chart_name="SalesByProduct",
                series_name="NoSuchSeries",
                action_type="Hyperlink",
                target_expression="x",
            )

    def test_invalid_action_type(self, chart_path):
        with pytest.raises(ValueError, match="not valid"):
            set_chart_series_action(
                path=str(chart_path),
                chart_name="SalesByProduct",
                series_name="Amount",
                action_type="NotReal",
                target_expression="x",
            )

    def test_action_placement_respects_schema_order(self, chart_path):
        # Per RDL 2016 schema, neither Action nor ActionInfo is a child
        # of ChartSeries. The action lives one level deeper, on the
        # template <ChartDataPoint> inside <ChartDataPoints>.
        set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="Hyperlink",
            target_expression="https://example.com",
        )
        s = _series(chart_path, "SalesByProduct", "Amount")
        series_locals = [etree.QName(c).localname for c in s]
        assert "Action" not in series_locals
        assert "ActionInfo" not in series_locals
        # ActionInfo IS present on the template ChartDataPoint.
        cdps = find_child(s, "ChartDataPoints")
        cdp = find_child(cdps, "ChartDataPoint")
        cdp_locals = [etree.QName(c).localname for c in cdp]
        assert "ActionInfo" in cdp_locals
        # Per ChartDataPoint XSD, ActionInfo sits between ChartDataLabel
        # and CustomProperties (or before, when no preceding optionals
        # are present). At minimum it must come AFTER ChartDataPointValues.
        if "ChartDataPointValues" in cdp_locals:
            assert cdp_locals.index("ActionInfo") > cdp_locals.index("ChartDataPointValues")

    def test_round_trip_safe(self, chart_path):
        set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="Drillthrough",
            target_expression="DetailReport",
            drillthrough_parameters=[
                {"name": "X", "value": "=1"},
            ],
        )
        RDLDocument.open(chart_path).validate()


# ---- read-back through the get_* readers (#108) ---------------------------


def _add_image(rdl_path: Path, name: str = "Logo") -> None:
    add_body_image(
        path=str(rdl_path),
        name=name,
        image_source="External",
        value="http://example.com/logo.png",
        top="1.5in",
        left="0in",
        width="1in",
        height="0.5in",
    )


class TestReadBack:
    def test_textbox_action(self, rdl_path):
        from pbirb_mcp.ops.reader import get_textbox

        set_textbox_action(
            str(rdl_path),
            "ProductID",
            "Drillthrough",
            "Detail",
            [{"name": "Id", "value": "=Fields!ProductID.Value"}],
        )
        assert get_textbox(str(rdl_path), "ProductID")["action"] == {
            "type": "Drillthrough",
            "target": "Detail",
            "parameters": [{"name": "Id", "value": "=Fields!ProductID.Value"}],
        }

    def test_image_action(self, rdl_path):
        from pbirb_mcp.ops.reader import get_image

        _add_image(rdl_path)
        set_bookmark(str(rdl_path), "MainTable", "Top")
        set_image_action(str(rdl_path), "Logo", "BookmarkLink", "Top")
        assert get_image(str(rdl_path), "Logo")["action"] == {
            "type": "BookmarkLink",
            "target": "Top",
            "parameters": [],
        }

    def test_textbox_tooltip(self, rdl_path):
        from pbirb_mcp.ops.reader import get_textbox

        set_textbox_tooltip(str(rdl_path), "ProductID", "=Fields!ProductName.Value")
        assert get_textbox(str(rdl_path), "ProductID")["tooltip"] == "=Fields!ProductName.Value"
        set_textbox_tooltip(str(rdl_path), "ProductID", "")
        assert get_textbox(str(rdl_path), "ProductID")["tooltip"] is None

    def test_document_map_label(self, rdl_path):
        from pbirb_mcp.ops.layout import add_rectangle
        from pbirb_mcp.ops.reader import get_image, get_rectangle, get_textbox

        p = str(rdl_path)
        _add_image(rdl_path)
        add_rectangle(p, "Frame", "2.5in", "0in", "1in", "1in")
        set_document_map_label(p, "ProductID", "Products")
        set_document_map_label(p, "Logo", "Logo")
        set_document_map_label(p, "Frame", "Frame")
        assert get_textbox(p, "ProductID")["document_map_label"] == "Products"
        assert get_image(p, "Logo")["document_map_label"] == "Logo"
        assert get_rectangle(p, "Frame")["document_map_label"] == "Frame"


# ---- #109: validation, clearing, generic item tools, bookmarks ------------


def _valid(path: Path) -> bool:
    from pbirb_mcp.ops.validate import validate_report

    return validate_report(str(path))["valid"]


def _lint_rules(path: Path) -> list[str]:
    from pbirb_mcp.ops.lint import lint_report

    return [i["rule"] for i in lint_report(str(path))["issues"]]


class TestDrillthroughValidation:
    @pytest.mark.parametrize(
        "kwargs, match",
        [
            (
                {
                    "textbox_name": "ProductID",
                    "action_type": "Drillthrough",
                    "target_expression": "Detail",
                    "drillthrough_parameters": [
                        {"name": "", "value": "1"},
                        {"name": "A", "value": "2"},
                    ],
                },
                "not a parameter name",
            ),
            (
                {
                    "textbox_name": "ProductID",
                    "action_type": "Drillthrough",
                    "target_expression": "Detail",
                    "drillthrough_parameters": [
                        {"name": "A", "value": "2"},
                        {"name": "A", "value": "3"},
                    ],
                },
                "twice",
            ),
            (
                {
                    "textbox_name": "ProductName",
                    "action_type": "Hyperlink",
                    "target_expression": "https://example.test",
                    "drillthrough_parameters": [{"name": "A", "value": "1"}],
                },
                "only apply to action_type='Drillthrough'",
            ),
            (
                {
                    "textbox_name": "Amount",
                    "action_type": "BookmarkLink",
                    "target_expression": "NoSuchBookmark",
                },
                "matches no <Bookmark>",
            ),
            (
                {
                    "textbox_name": "ProductID",
                    "action_type": "Drillthrough",
                    "target_expression": "Detail",
                    "drillthrough_parameters": [{"name": "A", "value": "1", "omit": "maybe"}],
                },
                "omit",
            ),
            (
                {"textbox_name": "ProductID", "action_type": "Hyperlink", "target_expression": ""},
                "clear=true",
            ),
        ],
    )
    def test_bad_actions_refused_file_unchanged(self, rdl_path, kwargs, match):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=match):
            set_textbox_action(path=str(rdl_path), **kwargs)
        assert rdl_path.read_bytes() == before

    def test_omit_written_and_read_back(self, rdl_path):
        from pbirb_mcp.ops.reader import get_textbox

        params = [
            {"name": "A", "value": "1", "omit": True},
            {"name": "B", "value": "2", "omit": "=Parameters!X.Value = 0"},
            {"name": "C", "value": "3"},
        ]
        set_textbox_action(str(rdl_path), "ProductID", "Drillthrough", "Detail", params)
        got = get_textbox(str(rdl_path), "ProductID")["action"]["parameters"]
        assert got == [
            {"name": "A", "value": "1", "omit": "true"},
            {"name": "B", "value": "2", "omit": "=Parameters!X.Value = 0"},
            {"name": "C", "value": "3"},
        ]
        # Same call again: no-op; a changed omit is a change.
        before = rdl_path.read_bytes()
        r = set_textbox_action(str(rdl_path), "ProductID", "Drillthrough", "Detail", params)
        assert r["changed"] is False and rdl_path.read_bytes() == before
        params[0]["omit"] = False
        r = set_textbox_action(str(rdl_path), "ProductID", "Drillthrough", "Detail", params)
        assert r["changed"] is True
        assert _valid(rdl_path)


class TestClearAction:
    def test_clear_restores_original_bytes_textbox(self, rdl_path):
        before = rdl_path.read_bytes()
        set_textbox_action(str(rdl_path), "ProductID", "Hyperlink", "https://example.test")
        assert rdl_path.read_bytes() != before
        r = set_textbox_action(str(rdl_path), "ProductID", clear=True)
        assert r["changed"] is True and r["action_type"] is None
        assert rdl_path.read_bytes() == before
        # Clearing again is a no-op.
        assert set_textbox_action(str(rdl_path), "ProductID", clear=True)["changed"] is False
        assert rdl_path.read_bytes() == before

    def test_clear_restores_original_bytes_image(self, rdl_path):
        _add_image(rdl_path)
        before = rdl_path.read_bytes()
        set_image_action(str(rdl_path), "Logo", "Hyperlink", "https://example.test")
        assert set_image_action(str(rdl_path), "Logo", clear=True)["changed"] is True
        assert rdl_path.read_bytes() == before

    def test_clear_drops_legacy_bare_action(self, rdl_path):
        text = rdl_path.read_text(encoding="utf-8")
        rdl_path.write_text(
            text.replace(
                '<Textbox Name="ProductID">',
                '<Textbox Name="ProductID"><Action><Hyperlink>x</Hyperlink></Action>',
                1,
            ),
            encoding="utf-8",
        )
        assert set_textbox_action(str(rdl_path), "ProductID", clear=True)["changed"] is True
        assert find_child(_textbox(rdl_path, "ProductID"), "Action") is None

    def test_clear_with_action_args_refused(self, rdl_path):
        with pytest.raises(ValueError, match="clear=true"):
            set_textbox_action(str(rdl_path), "ProductID", "Hyperlink", "x", clear=True)

    def test_missing_action_type_refused(self, rdl_path):
        with pytest.raises(ValueError, match="required"):
            set_textbox_action(str(rdl_path), "ProductID")


class TestSetItemAction:
    def test_rectangle_and_line(self, rdl_path):
        from pbirb_mcp.ops.layout import add_line, add_rectangle
        from pbirb_mcp.ops.reader import get_rectangle

        p = str(rdl_path)
        add_rectangle(p, "Frame", "2.5in", "0in", "1in", "1in")
        add_line(p, "Rule", "3.6in", "0in", "2in", "0in")
        r = set_item_action(p, "Frame", "Hyperlink", "https://example.test")
        assert r == {
            "name": "Frame",
            "kind": "Rectangle",
            "action_type": "Hyperlink",
            "changed": True,
        }
        assert set_item_action(p, "Rule", "Drillthrough", "Detail")["kind"] == "Line"
        assert get_rectangle(p, "Frame")["action"]["target"] == "https://example.test"
        doc = RDLDocument.open(p)
        line = doc.root.find(f".//{{{RDL_NS}}}Line[@Name='Rule']")
        assert _inner_action(line) is not None
        assert _valid(rdl_path)
        # Repeat is a no-op; clear removes.
        assert set_item_action(p, "Frame", "Hyperlink", "https://example.test")["changed"] is False
        assert set_item_action(p, "Frame", clear=True)["changed"] is True
        assert get_rectangle(p, "Frame")["action"] is None

    def test_textbox_and_image_via_generic_tool(self, rdl_path):
        from pbirb_mcp.ops.reader import get_image, get_textbox

        _add_image(rdl_path)
        set_item_action(str(rdl_path), "ProductID", "Hyperlink", "https://a.test")
        set_item_action(str(rdl_path), "Logo", "Hyperlink", "https://b.test")
        assert get_textbox(str(rdl_path), "ProductID")["action"]["target"] == "https://a.test"
        assert get_image(str(rdl_path), "Logo")["action"]["target"] == "https://b.test"

    def test_other_kinds_refused(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="can't carry an action"):
            set_item_action(str(rdl_path), "MainTable", "Hyperlink", "https://x.test")
        assert rdl_path.read_bytes() == before

    def test_unknown_item(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_item_action(str(rdl_path), "NoSuch", "Hyperlink", "https://x.test")


class TestSetItemTooltip:
    def test_image_tooltip(self, rdl_path):
        from pbirb_mcp.ops.reader import get_image

        _add_image(rdl_path, "Img")
        r = set_item_tooltip(str(rdl_path), "Img", "tip")
        assert r == {"name": "Img", "kind": "Image", "changed": True}
        assert find_child(_image(rdl_path, "Img"), "ToolTip").text == "tip"
        assert get_image(str(rdl_path), "Img")["tooltip"] == "tip"
        before = rdl_path.read_bytes()
        assert set_item_tooltip(str(rdl_path), "Img", "tip")["changed"] is False
        assert rdl_path.read_bytes() == before
        assert _valid(rdl_path)

    def test_clear_restores_bytes(self, rdl_path):
        before = rdl_path.read_bytes()
        set_item_tooltip(str(rdl_path), "MainTable", "=Fields!ProductName.Value")
        assert set_item_tooltip(str(rdl_path), "MainTable", "")["changed"] is True
        assert rdl_path.read_bytes() == before

    @pytest.mark.parametrize("name, kind", [("MainTable", "Tablix"), ("ProductID", "Textbox")])
    def test_any_item(self, rdl_path, name, kind):
        assert set_item_tooltip(str(rdl_path), name, "tip")["kind"] == kind
        assert _valid(rdl_path)

    def test_chart_and_rectangle(self, chart_path):
        from pbirb_mcp.ops.layout import add_rectangle
        from pbirb_mcp.ops.reader import get_rectangle

        p = str(chart_path)
        assert set_item_tooltip(p, "SalesByProduct", "Sales")["kind"] == "Chart"
        add_rectangle(p, "Frame", "5in", "0in", "1in", "1in")
        set_item_tooltip(p, "Frame", "frame tip")
        assert get_rectangle(p, "Frame")["tooltip"] == "frame tip"
        assert _valid(chart_path)


class TestSetBookmark:
    def test_bookmark_then_link_passes_lint(self, rdl_path):
        from pbirb_mcp.ops.reader import get_textbox

        p = str(rdl_path)
        r = set_bookmark(p, "HeaderProductID", "Top")
        assert r == {"name": "HeaderProductID", "kind": "Textbox", "changed": True}
        assert get_textbox(p, "HeaderProductID")["bookmark"] == "Top"
        set_textbox_action(p, "Amount", "BookmarkLink", "Top")
        assert "dangling-bookmark-link" not in _lint_rules(rdl_path)
        assert _valid(rdl_path)
        # Clearing the bookmark leaves a dangling link, which lint flags.
        assert set_bookmark(p, "HeaderProductID", "")["changed"] is True
        assert get_textbox(p, "HeaderProductID")["bookmark"] is None
        assert "dangling-bookmark-link" in _lint_rules(rdl_path)

    def test_idempotent(self, rdl_path):
        set_bookmark(str(rdl_path), "MainTable", "T")
        before = rdl_path.read_bytes()
        assert set_bookmark(str(rdl_path), "MainTable", "T")["changed"] is False
        assert rdl_path.read_bytes() == before
        assert set_bookmark(str(rdl_path), "MainTable", "")["changed"]

    def test_expression_bookmark_allows_any_literal_link(self, rdl_path):
        set_bookmark(str(rdl_path), "ProductID", "=Fields!ProductID.Value")
        r = set_textbox_action(str(rdl_path), "Amount", "BookmarkLink", "42")
        assert r["changed"] is True

    def test_expression_link_needs_no_bookmark(self, rdl_path):
        r = set_textbox_action(str(rdl_path), "Amount", "BookmarkLink", "=Fields!X.Value")
        assert r["changed"] is True

    def test_duplicate_literal_warns(self, rdl_path):
        set_bookmark(str(rdl_path), "MainTable", "Top")
        r = set_bookmark(str(rdl_path), "ProductID", "Top")
        assert r["changed"] is True
        assert "MainTable" in r["warnings"][0]

    def test_unknown_item(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_bookmark(str(rdl_path), "NoSuch", "Top")


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_action_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {
            "set_textbox_action",
            "set_image_action",
            "set_textbox_tooltip",
            "set_document_map_label",
        } <= names

    def test_chart_series_action_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "set_chart_series_action" in names

    def test_item_tools_through_the_server(self, rdl_path):
        import json

        from pbirb_mcp.core.toolsets import toolset_of

        srv = MCPServer()
        register_all_tools(srv)

        def call(tool, **arguments):
            resp = srv.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": {"path": str(rdl_path), **arguments}},
                }
            )["result"]
            assert not resp.get("isError"), resp
            return json.loads(resp["content"][0]["text"])

        for tool in ("set_item_action", "set_item_tooltip", "set_bookmark"):
            assert toolset_of(tool) == "interactivity"
        assert call("set_bookmark", name="MainTable", bookmark_or_expression="Top")["changed"]
        assert call(
            "set_item_action",
            name="ProductID",
            action_type="Drillthrough",
            target_expression="Detail",
            drillthrough_parameters=[{"name": "Id", "value": "1", "omit": True}],
        )["changed"]
        assert call("set_textbox_action", textbox_name="ProductID", clear=True)["changed"]
        assert call("set_item_tooltip", name="MainTable", text_or_expression="tip")["changed"]
