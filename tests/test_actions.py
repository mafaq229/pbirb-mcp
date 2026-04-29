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
    set_chart_series_action,
    set_document_map_label,
    set_image_action,
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
        action = find_child(tb, "Action")
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
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="BookmarkLink",
            target_expression="anchor1",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        action = find_child(tb, "Action")
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
        drill = tb.find(f"{q('Action')}/{q('Drillthrough')}")
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
        drill = tb.find(f"{q('Action')}/{q('Drillthrough')}")
        assert find_child(drill, "ReportName").text == "DetailReport"
        # No <Parameters> when no parameters supplied.
        assert find_child(drill, "Parameters") is None

    def test_drillthrough_idempotent_with_parameters(self, rdl_path):
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Drillthrough",
            target_expression="DetailReport",
            drillthrough_parameters=[
                {"name": "ProductID", "value": "=Fields!ProductID.Value"}
            ],
        )
        before = (rdl_path).read_bytes()
        result = set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Drillthrough",
            target_expression="DetailReport",
            drillthrough_parameters=[
                {"name": "ProductID", "value": "=Fields!ProductID.Value"}
            ],
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


class TestActionPlacement:
    def test_action_placed_before_style(self, rdl_path):
        # Per RDL XSD, Action precedes Style on a Textbox.
        set_textbox_action(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            action_type="Hyperlink",
            target_expression="https://example.com",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        children_locals = [etree.QName(c).localname for c in tb]
        action_idx = children_locals.index("Action")
        style_idx = children_locals.index("Style")
        assert action_idx < style_idx


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
        link = img.find(f"{q('Action')}/{q('Hyperlink')}")
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
            label_or_expression="=\"Sales (\" & Format(Now(), \"yyyy-MM-dd\") & \")\"",
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
    return next(
        s for s in find_children(sc, "ChartSeries") if s.get("Name") == series_name
    )


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
        link = s.find(f"{q('Action')}/{q('Hyperlink')}")
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
        drill = s.find(f"{q('Action')}/{q('Drillthrough')}")
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
        action = find_child(s, "Action")
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
        # Action sits before Type/Subtype in ChartSeries (and before
        # Style etc.).
        set_chart_series_action(
            path=str(chart_path),
            chart_name="SalesByProduct",
            series_name="Amount",
            action_type="Hyperlink",
            target_expression="https://example.com",
        )
        s = _series(chart_path, "SalesByProduct", "Amount")
        children_locals = [etree.QName(c).localname for c in s]
        action_idx = children_locals.index("Action")
        # Confirm Action came before Type if both present.
        if "Type" in children_locals:
            assert action_idx < children_locals.index("Type")
        # Style isn't always present; only assert when it is.
        if "Style" in children_locals:
            assert action_idx < children_locals.index("Style")

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


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_action_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
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
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "set_chart_series_action" in names
