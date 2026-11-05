"""Chart data labels live on the template ``ChartDataPoint`` (#78).

Report Builder stores a series' data labels on its data point
(``ChartSeries/ChartDataPoints/ChartDataPoint/ChartDataLabel``); a point's
label overrides ``ChartSeries/ChartDataLabel`` at render time. So
``set_chart_data_labels`` writes the point-level label (moving a
series-level one there) and ``get_chart`` reports the label that renders,
with its ``source``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import chart_shape
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.chart import (
    DATA_LABEL_POSITIONS,
    insert_chart_from_template,
    set_chart_data_labels,
    set_chart_series_type,
)
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.reader import get_chart
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
RICH = FIXTURES / "pbi_chart_rich.rdl"
MINIMAL = FIXTURES / "pbi_paginated_minimal.rdl"
NS = RDL_NS


@pytest.fixture
def rb_path(tmp_path: Path) -> Path:
    """``SalesByProduct`` with a Report Builder-shaped label on series
    Amount's template data point."""
    dest = tmp_path / "rich.rdl"
    shutil.copy(RICH, dest)
    tree = etree.parse(str(dest))
    point = tree.getroot().find(
        f".//{{{NS}}}ChartSeries[@Name='Amount']/{{{NS}}}ChartDataPoints/{{{NS}}}ChartDataPoint"
    )
    point.append(
        etree.fromstring(
            f'<ChartDataLabel xmlns="{NS}"><Style><FontWeight>Bold</FontWeight></Style>'
            "<UseValueAsLabel>true</UseValueAsLabel>"
            "<Visible>=Sum(Fields!Amount.Value) &gt;= 3</Visible>"
            "<Position>Center</Position></ChartDataLabel>"
        )
    )
    tree.write(str(dest), xml_declaration=True, encoding="utf-8")
    return dest


@pytest.fixture
def template_path(tmp_path: Path) -> Path:
    dest = tmp_path / "plain.rdl"
    shutil.copy(MINIMAL, dest)
    insert_chart_from_template(
        str(dest), "C", "MainDataset", "ProductName", "Amount", "3in", "0in", "5in", "3in"
    )
    return dest


def _series(path: Path, chart: str, name: str) -> etree._Element:
    root = etree.parse(str(path)).getroot()
    return root.find(f".//{{{NS}}}Chart[@Name='{chart}']//{{{NS}}}ChartSeries[@Name='{name}']")


def _point_label(series: etree._Element):
    return find_child(chart_shape.template_data_point(series), "ChartDataLabel")


def _labels(path: Path, chart: str, name: str) -> dict:
    return next(s for s in get_chart(str(path), chart)["series"] if s["name"] == name)[
        "data_labels"
    ]


def _assert_clean(path: Path) -> None:
    assert validate_report(str(path))["valid"]
    assert lint_report(str(path), ["chart-structure"])["issues"] == []


# ---- Report Builder-shaped data point ---------------------------------------


class TestReportBuilderShapedPoint:
    def test_reader_reports_point_label(self, rb_path):
        assert _labels(rb_path, "SalesByProduct", "Amount") == {
            "source": "point",
            "visible": "=Sum(Fields!Amount.Value) >= 3",
            "position": "Center",
            "use_value_as_label": "true",
            "format": None,
            "font_weight": "Bold",
            "color": None,
        }

    def test_visible_false_hits_point_label(self, rb_path):
        r = set_chart_data_labels(
            str(rb_path), "SalesByProduct", series_name="Amount", visible=False, position="Top"
        )
        assert r["changed"] == ["Position", "Visible"]
        series = _series(rb_path, "SalesByProduct", "Amount")
        label = _point_label(series)
        assert find_child(label, "Visible").text == "false"
        assert find_child(label, "Position").text == "Top"
        # Untouched settings survive; no series-level label appears.
        assert find_child(label, "UseValueAsLabel").text == "true"
        assert find_child(series, "ChartDataLabel") is None
        _assert_clean(rb_path)

    def test_identical_repeat_is_noop(self, rb_path):
        set_chart_data_labels(str(rb_path), "SalesByProduct", series_name="Amount", visible=False)
        before = rb_path.read_bytes()
        r = set_chart_data_labels(
            str(rb_path), "SalesByProduct", series_name="Amount", visible=False
        )
        assert r["changed"] == []
        assert rb_path.read_bytes() == before

    def test_reader_before_and_after(self, rb_path):
        assert _labels(rb_path, "SalesByProduct", "Amount")["position"] == "Center"
        set_chart_data_labels(
            str(rb_path),
            "SalesByProduct",
            series_name="Amount",
            visible=False,
            position="Top",
            color="Red",
            format="#,0",
        )
        assert _labels(rb_path, "SalesByProduct", "Amount") == {
            "source": "point",
            "visible": "false",
            "position": "Top",
            "use_value_as_label": "true",
            "format": "#,0",
            "font_weight": "Bold",
            "color": "Red",
        }


# ---- template-inserted chart (no labels yet) ---------------------------------


class TestTemplateChart:
    def test_label_created_on_data_point(self, template_path):
        assert _labels(template_path, "C", "Amount") is None
        r = set_chart_data_labels(str(template_path), "C", visible=True, use_value_as_label=True)
        assert r["changed"] == ["UseValueAsLabel", "Visible"]
        series = _series(template_path, "C", "Amount")
        assert find_child(series, "ChartDataLabel") is None
        label = _point_label(series)
        assert find_child(label, "Visible").text == "true"
        # ChartDataPoint child order: ChartDataPointValues ... ChartDataLabel ...
        point = chart_shape.template_data_point(series)
        children = [etree.QName(c).localname for c in point if isinstance(c.tag, str)]
        assert children.index("ChartDataPointValues") < children.index("ChartDataLabel")
        got = _labels(template_path, "C", "Amount")
        assert got["source"] == "point" and got["visible"] == "true"
        _assert_clean(template_path)

    def test_second_call_noop(self, template_path):
        set_chart_data_labels(str(template_path), "C", visible=True, format="#,0")
        before = template_path.read_bytes()
        r = set_chart_data_labels(str(template_path), "C", visible=True, format="#,0")
        assert r["changed"] == []
        assert template_path.read_bytes() == before

    def test_clearing_on_unlabelled_series_adds_nothing(self, template_path):
        before = template_path.read_bytes()
        r = set_chart_data_labels(str(template_path), "C", format="", color="")
        assert r["changed"] == []
        assert template_path.read_bytes() == before

    def test_every_data_point_is_labelled(self, template_path):
        # Two static category members -> two data points per series.
        tree = etree.parse(str(template_path))
        chart = tree.getroot().find(f".//{{{NS}}}Chart[@Name='C']")
        cat_members = chart.find(f"{q('ChartCategoryHierarchy')}/{q('ChartMembers')}")
        for m in list(cat_members):
            cat_members.remove(m)
        for text in ("A", "B"):
            etree.SubElement(
                etree.SubElement(cat_members, q("ChartMember")), q("Label")
            ).text = text
        (series,) = chart_shape.series_list(chart)
        points = find_child(series, "ChartDataPoints")
        points.append(etree.fromstring(etree.tostring(points[0])))
        tree.write(str(template_path), xml_declaration=True, encoding="utf-8")
        _assert_clean(template_path)

        set_chart_data_labels(str(template_path), "C", visible=True)
        series = _series(template_path, "C", "Amount")
        labels = [find_child(p, "ChartDataLabel") for p in chart_shape.data_points(series)]
        assert len(labels) == 2
        assert all(find_child(lb, "Visible").text == "true" for lb in labels)
        _assert_clean(template_path)


# ---- series-level label (earlier versions) ------------------------------------


class TestSeriesLevelLabel:
    def _add_series_label(self, path: Path) -> None:
        tree = etree.parse(str(path))
        series = tree.getroot().find(f".//{{{NS}}}ChartSeries[@Name='Amount']")
        label = etree.fromstring(
            f'<ChartDataLabel xmlns="{NS}"><Visible>true</Visible>'
            "<Style><Format>#,0</Format></Style></ChartDataLabel>"
        )
        find_child(series, "Subtype").addnext(label)
        tree.write(str(path), xml_declaration=True, encoding="utf-8")

    def test_reader_falls_back_to_series_level(self, template_path):
        self._add_series_label(template_path)
        got = _labels(template_path, "C", "Amount")
        assert got["source"] == "series"
        assert got["visible"] == "true" and got["format"] == "#,0"

    def test_series_label_moved_to_point(self, template_path):
        self._add_series_label(template_path)
        r = set_chart_data_labels(str(template_path), "C", series_name="Amount", visible=False)
        assert r["changed"] == ["ChartDataLabel", "Visible"]
        series = _series(template_path, "C", "Amount")
        assert find_child(series, "ChartDataLabel") is None
        label = _point_label(series)
        assert find_child(label, "Visible").text == "false"
        assert find_child(find_child(label, "Style"), "Format").text == "#,0"
        assert _labels(template_path, "C", "Amount")["source"] == "point"
        _assert_clean(template_path)
        before = template_path.read_bytes()
        r = set_chart_data_labels(str(template_path), "C", series_name="Amount", visible=False)
        assert r["changed"] == []
        assert template_path.read_bytes() == before

    def test_point_label_wins_over_series_label(self, rb_path):
        self._add_series_label(rb_path)
        assert _labels(rb_path, "SalesByProduct", "Amount")["source"] == "point"
        set_chart_data_labels(str(rb_path), "SalesByProduct", series_name="Amount", visible=False)
        series = _series(rb_path, "SalesByProduct", "Amount")
        assert find_child(series, "ChartDataLabel") is None
        assert find_child(_point_label(series), "Visible").text == "false"
        assert find_child(_point_label(series), "Position").text == "Center"
        _assert_clean(rb_path)


# ---- position enum --------------------------------------------------------------


class TestPositions:
    def test_enum_matches_rdl(self):
        assert DATA_LABEL_POSITIONS == (
            "Auto",
            "Top",
            "TopLeft",
            "TopRight",
            "Left",
            "Center",
            "Right",
            "BottomRight",
            "Bottom",
            "BottomLeft",
            "Outside",
        )

    @pytest.mark.parametrize("bad", ["TopCenter", "BottomCenter"])
    def test_legend_positions_refused(self, template_path, bad):
        before = template_path.read_bytes()
        with pytest.raises(ValueError, match="legend positions"):
            set_chart_data_labels(str(template_path), "C", position=bad)
        assert template_path.read_bytes() == before

    def test_outside_on_column_warns(self, template_path):
        r = set_chart_data_labels(str(template_path), "C", position="Outside")
        assert r["changed"] == ["Position"]
        assert len(r["warnings"]) == 1 and "Shape" in r["warnings"][0]

    def test_outside_on_pie_no_warning(self, template_path):
        set_chart_series_type(str(template_path), "C", "Amount", "Pie")
        r = set_chart_data_labels(str(template_path), "C", position="Outside")
        assert r["warnings"] == []

    def test_published_schema(self):
        srv = MCPServer()
        register_all_tools(srv)
        tools = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        tool = next(t for t in tools if t["name"] == "set_chart_data_labels")
        assert tool["inputSchema"]["properties"]["position"]["enum"] == list(DATA_LABEL_POSITIONS)
