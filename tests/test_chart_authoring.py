"""Chart authoring capabilities that had no tool (#104).

``set_chart_series_properties`` — legend entry, secondary axis, chart area,
hidden, series order, data-point tooltip / marker, empty points;
``set_chart_category_group`` — category label / sort / regroup, nested
category levels. Every write is read back through ``get_chart`` (#102),
leaves the report XSD-valid and keeps the ``chart-structure`` lint clean.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children
from pbirb_mcp.ops.chart import set_chart_category_group, set_chart_series_properties
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.reader import get_chart
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"
CH = "SalesByProduct"


@pytest.fixture
def rdl(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _patch(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _healthy(path: Path) -> None:
    assert validate_report(str(path))["valid"]
    assert lint_report(str(path), ["chart-structure"])["issues"] == []


def _series(path: Path, name: str) -> dict:
    return next(s for s in get_chart(str(path), CH)["series"] if s["name"] == name)


def _chart(path: Path):
    doc = RDLDocument.open(str(path))
    return doc, doc.root.find(f".//{{{RDL_NS}}}Chart[@Name='{CH}']")


def _props(path: Path, series: str = "Amount", **kwargs) -> dict:
    return set_chart_series_properties(str(path), CH, series, **kwargs)


# ---- set_chart_series_properties -----------------------------------------------


class TestLegendEntry:
    def test_legend_text_and_hide_read_back(self, rdl):
        out = _props(rdl, legend_text="Sales (USD)", hide_in_legend=True)
        assert out["changed"] == ["ChartItemInLegend"]
        s = _series(rdl, "Amount")
        assert (s["legend_text"], s["hide_in_legend"]) == ("Sales (USD)", "true")
        _healthy(rdl)

    def test_clearing_drops_the_emptied_wrapper(self, rdl):
        _props(rdl, legend_text="X", hide_in_legend=True)
        _props(rdl, legend_text="", hide_in_legend=False)
        assert "ChartItemInLegend" not in rdl.read_text(encoding="utf-8")
        assert _series(rdl, "Amount")["legend_text"] is None

    def test_legend_name_must_exist(self, rdl):
        with pytest.raises(ElementNotFoundError):
            _props(rdl, legend_name="Nope")
        _props(rdl, legend_name="Default")
        assert _series(rdl, "Amount")["legend_name"] == "Default"
        _healthy(rdl)


class TestAxes:
    def test_secondary_value_axis_is_created_opposite(self, rdl):
        out = _props(rdl, "Quantity", value_axis="Secondary")
        assert out["created_axes"] == ["Value/Secondary"]
        assert out["changed"] == ["ChartValueAxes", "ValueAxisName"]
        c = get_chart(str(rdl), CH)
        assert [(a["name"], a["location"]) for a in c["axes"]["value"]] == [
            ("Primary", None),
            ("Secondary", "Opposite"),
        ]
        assert _series(rdl, "Quantity")["value_axis"] == "Secondary"
        _healthy(rdl)

    def test_existing_axis_is_not_recreated(self, rdl):
        out = _props(rdl, value_axis="Primary", category_axis="Primary")
        assert out["created_axes"] == []
        assert out["changed"] == ["ValueAxisName", "CategoryAxisName"]
        _healthy(rdl)

    def test_axis_in_named_chart_area(self, rdl):
        _patch(
            rdl,
            "</ChartArea></ChartAreas>",
            '</ChartArea><ChartArea Name="Lower"><ChartCategoryAxes><ChartAxis Name="Primary" />'
            '</ChartCategoryAxes><ChartValueAxes><ChartAxis Name="Primary" /></ChartValueAxes>'
            "</ChartArea></ChartAreas>",
        )
        _props(rdl, "Total", chart_area="Lower", value_axis="Right")
        areas = get_chart(str(rdl), CH)["chart_areas"]
        assert [a["name"] for a in areas[1]["axes"]["value"]] == ["Primary", "Right"]
        assert [a["name"] for a in areas[0]["axes"]["value"]] == ["Primary"]
        assert _series(rdl, "Total")["chart_area"] == "Lower"
        _healthy(rdl)

    def test_unknown_chart_area_refused(self, rdl):
        before = rdl.read_bytes()
        with pytest.raises(ElementNotFoundError, match="Default"):
            _props(rdl, chart_area="Nope")
        assert rdl.read_bytes() == before

    def test_axis_name_must_be_an_identifier(self, rdl):
        with pytest.raises(ValueError):
            _props(rdl, value_axis="bad name")


class TestHiddenAndOrder:
    def test_hidden(self, rdl):
        _props(rdl, "Total", hidden=True)
        assert _series(rdl, "Total")["hidden"] == "true"
        _props(rdl, "Total", hidden=False)
        assert _series(rdl, "Total")["hidden"] is None

    def test_move_series_moves_its_member(self, rdl):
        out = _props(rdl, "Total", position=0)
        assert out["position"] == 0 and out["changed"] == ["position"]
        c = get_chart(str(rdl), CH)
        assert [s["name"] for s in c["series"]] == ["Total", "Amount", "Quantity"]
        assert [g["label"] for g in c["series_groups"]] == ["Total", "Amount", "Quantity"]
        assert [s["label"] for s in c["series"]] == ["Total", "Amount", "Quantity"]
        _healthy(rdl)
        _props(rdl, "Total", position=2)
        c = get_chart(str(rdl), CH)
        assert [s["name"] for s in c["series"]] == ["Amount", "Quantity", "Total"]
        assert [g["series"] for g in c["series_groups"]] == ["Amount", "Quantity", "Total"]

    @pytest.mark.parametrize("series, position", [("Amount", 2), ("Total", 0), ("Quantity", 2)])
    def test_move_keeps_pretty_printed_layout(self, rdl, series, position):
        from lxml import etree

        def pretty(tree):
            etree.indent(tree, space="  ")
            return etree.tostring(tree, encoding="unicode")

        tree = etree.parse(str(rdl))
        pretty(tree)
        tree.write(str(rdl), xml_declaration=True, encoding="utf-8")
        _props(rdl, series, position=position)
        moved = etree.parse(str(rdl))
        # Already laid out exactly as a fresh pretty-print would be.
        assert etree.tostring(moved, encoding="unicode") == pretty(moved)
        names = [s["name"] for s in get_chart(str(rdl), CH)["series"]]
        assert names.index(series) == position
        _healthy(rdl)

    def test_move_to_end(self, rdl):
        _props(rdl, "Amount", position=2)
        assert [s["name"] for s in get_chart(str(rdl), CH)["series"]] == [
            "Quantity",
            "Total",
            "Amount",
        ]
        _healthy(rdl)

    def test_out_of_range_refused(self, rdl):
        with pytest.raises(ValueError, match="out of range"):
            _props(rdl, position=3)

    def test_grouped_series_moves_only_within_its_group(self, rdl):
        # Amount and Quantity share a dynamic parent member.
        _patch(
            rdl,
            "<ChartMember><Label>Amount</Label></ChartMember>"
            "<ChartMember><Label>Quantity</Label></ChartMember>",
            '<ChartMember><Group Name="G"><GroupExpressions><GroupExpression>'
            "=Fields!ProductID.Value</GroupExpression></GroupExpressions></Group>"
            "<ChartMembers><ChartMember><Label>Amount</Label></ChartMember>"
            "<ChartMember><Label>Quantity</Label></ChartMember></ChartMembers>"
            "<Label>=Fields!ProductID.Value</Label></ChartMember>",
        )
        with pytest.raises(ValueError, match=r"position\(s\) \[0, 1\]"):
            _props(rdl, "Amount", position=2)
        _props(rdl, "Amount", position=1)
        groups = get_chart(str(rdl), CH)["series_groups"]
        assert [(g["level"], g["series"]) for g in groups] == [
            (0, None),
            (1, "Quantity"),
            (1, "Amount"),
            (0, "Total"),
        ]
        _healthy(rdl)


class TestDataPoints:
    def test_tooltip_and_marker(self, rdl):
        out = _props(
            rdl,
            "Quantity",
            tooltip="=Fields!ProductName.Value",
            marker_type="circle",
            marker_size="6pt",
        )
        assert out["changed"] == ["ToolTip", "ChartMarker"]
        assert out["warnings"] == []
        s = _series(rdl, "Quantity")
        assert s["tooltip"] == "=Fields!ProductName.Value"
        assert s["marker"] == {"type": "Circle", "size": "6pt"}
        _healthy(rdl)

    def test_every_data_point_gets_it(self, rdl):
        # Two static category members -> two data points per series.
        _patch(
            rdl,
            "</ChartMember></ChartMembers></ChartCategoryHierarchy>",
            "</ChartMember><ChartMember><Label>Other</Label></ChartMember></ChartMembers>"
            "</ChartCategoryHierarchy>",
        )
        for name in ("Amount", "Quantity", "Total"):
            _patch(
                rdl,
                f'<ChartSeries Name="{name}"><ChartDataPoints><ChartDataPoint>',
                f'<ChartSeries Name="{name}"><ChartDataPoints><ChartDataPoint>'
                "<ChartDataPointValues><Y>=0</Y></ChartDataPointValues></ChartDataPoint>"
                "<ChartDataPoint>",
            )
        _props(rdl, tooltip="Tip")
        _doc, chart = _chart(rdl)
        series = chart.find(f".//{{{RDL_NS}}}ChartSeries[@Name='Amount']")
        points = find_children(find_child(series, "ChartDataPoints"), "ChartDataPoint")
        assert [find_child(p, "ToolTip").text for p in points] == ["Tip", "Tip"]
        _healthy(rdl)

    def test_marker_on_column_series_warns(self, rdl):
        out = _props(rdl, "Amount", marker_type="Square")
        assert out["warnings"] and "Column" in out["warnings"][0]

    def test_marker_values_validated(self, rdl):
        with pytest.raises(ValueError, match="marker_type"):
            _props(rdl, marker_type="Hexagon")
        with pytest.raises(ValueError):
            _props(rdl, marker_size="big")

    def test_clearing_marker(self, rdl):
        _props(rdl, "Quantity", marker_type="Circle")
        _props(rdl, "Quantity", marker_type="")
        assert _series(rdl, "Quantity")["marker"] is None


class TestEmptyPoints:
    def test_zero_and_colour(self, rdl):
        out = _props(rdl, "Quantity", empty_point_value="Zero", empty_point_color="Gray")
        assert out["changed"] == ["EmptyPointValue", "ChartEmptyPoints"]
        s = _series(rdl, "Quantity")
        assert (s["empty_point_value"], s["empty_point_color"]) == ("Zero", "Gray")
        _healthy(rdl)

    def test_clear(self, rdl):
        _props(rdl, "Quantity", empty_point_value="Zero", empty_point_color="Gray")
        _props(rdl, "Quantity", empty_point_value="", empty_point_color="")
        text = rdl.read_text(encoding="utf-8")
        assert "EmptyPointValue" not in text and "ChartEmptyPoints" not in text

    def test_bad_colour_refused(self, rdl):
        with pytest.raises(ValueError, match="empty_point_color"):
            _props(rdl, empty_point_color="#GGG")


class TestNoOp:
    def test_repeat_leaves_file_untouched(self, rdl):
        args = dict(legend_text="L", value_axis="Secondary", position=1, tooltip="T")
        _props(rdl, "Total", **args)
        before = rdl.read_bytes()
        out = _props(rdl, "Total", **args)
        assert out["changed"] == [] and out["created_axes"] == []
        assert rdl.read_bytes() == before

    def test_nothing_passed(self, rdl):
        before = rdl.read_bytes()
        assert _props(rdl)["changed"] == []
        assert rdl.read_bytes() == before


# ---- set_chart_category_group -------------------------------------------------


def _cat(path: Path, **kwargs) -> dict:
    return set_chart_category_group(str(path), CH, **kwargs)


class TestCategoryEdit:
    def test_label_and_sort(self, rdl):
        out = _cat(rdl, label_expression="=UCase(Fields!ProductName.Value)", sort="Descending")
        assert out["changed"] == ["Label", "SortExpressions"]
        g = get_chart(str(rdl), CH)["category_groups"][0]
        assert g["label"] == "=UCase(Fields!ProductName.Value)"
        assert g["sort"] == [{"value": "=Fields!ProductName.Value", "direction": "Descending"}]
        _healthy(rdl)

    def test_sort_by_other_expression_then_flip(self, rdl):
        _cat(rdl, sort_expression="=Sum(Fields!Amount.Value)", sort="Descending")
        _cat(rdl, sort="Ascending")
        g = get_chart(str(rdl), CH)["category_groups"][0]
        assert g["sort"] == [{"value": "=Sum(Fields!Amount.Value)", "direction": "Ascending"}]
        _cat(rdl, sort="None")
        assert get_chart(str(rdl), CH)["category_groups"][0]["sort"] == []

    def test_regroup_label_follows(self, rdl):
        out = _cat(rdl, group_field="ProductID")
        assert out["changed"] == ["GroupExpressions", "Label"]
        g = get_chart(str(rdl), CH)["category_groups"][0]
        assert (g["name"], g["expression"], g["label"]) == (
            "SalesByProduct_CategoryGroup",
            "=Fields!ProductID.Value",
            "=Fields!ProductID.Value",
        )
        _healthy(rdl)

    def test_unknown_group(self, rdl):
        with pytest.raises(ElementNotFoundError, match="SalesByProduct_CategoryGroup"):
            _cat(rdl, group_name="Nope", sort="Ascending")

    def test_repeat_is_noop(self, rdl):
        _cat(rdl, label_expression="=1", sort="Descending")
        before = rdl.read_bytes()
        assert _cat(rdl, label_expression="=1", sort="Descending")["changed"] == []
        assert rdl.read_bytes() == before


class TestCategoryAddLevel:
    def test_outer_level(self, rdl):
        out = _cat(rdl, add_level="outer", group_field="ProductID", group_name="ById")
        assert out["changed"] == ["ChartMember"] and out["level"] == 0
        groups = get_chart(str(rdl), CH)["category_groups"]
        assert [(g["name"], g["level"]) for g in groups] == [
            ("ById", 0),
            ("SalesByProduct_CategoryGroup", 1),
        ]
        assert groups[0]["sort"] == [{"value": "=Fields!ProductID.Value", "direction": "Ascending"}]
        _healthy(rdl)

    def test_inner_level_with_label_and_sort(self, rdl):
        out = _cat(
            rdl,
            add_level="inner",
            group_expression="=Fields!ProductID.Value",
            label_expression='=Format(Fields!ProductID.Value, "0")',
            sort="Descending",
        )
        assert out["level"] == 1
        assert out["group_name"] == "SalesByProduct_CategoryGroup1"
        groups = get_chart(str(rdl), CH)["category_groups"]
        assert [(g["name"], g["level"], g["label"]) for g in groups] == [
            ("SalesByProduct_CategoryGroup", 0, "=Fields!ProductName.Value"),
            (
                "SalesByProduct_CategoryGroup1",
                1,
                '=Format(Fields!ProductID.Value, "0")',
            ),
        ]
        _healthy(rdl)

    def test_same_expression_not_added_twice(self, rdl):
        before = rdl.read_bytes()
        out = _cat(rdl, add_level="inner", group_field="ProductName")
        assert out["changed"] == [] and out["group_name"] == "SalesByProduct_CategoryGroup"
        assert rdl.read_bytes() == before

    def test_inner_needs_one_leaf(self, rdl):
        _patch(
            rdl,
            "</ChartMember></ChartMembers></ChartCategoryHierarchy>",
            "<ChartMembers><ChartMember><Label>A</Label></ChartMember><ChartMember>"
            "<Label>B</Label></ChartMember></ChartMembers></ChartMember></ChartMembers>"
            "</ChartCategoryHierarchy>",
        )
        with pytest.raises(ValueError, match="'outer'"):
            _cat(rdl, add_level="inner", group_field="ProductID")

    def test_several_groups_need_group_name(self, rdl):
        _cat(rdl, add_level="outer", group_field="ProductID")
        with pytest.raises(ValueError, match="group_name"):
            _cat(rdl, sort="Descending")

    def test_argument_checks(self, rdl):
        with pytest.raises(ValueError, match="needs group_field"):
            _cat(rdl, add_level="inner")
        with pytest.raises(ValueError, match="=expression"):
            _cat(rdl, add_level="inner", group_expression="Fields!ProductID.Value")
        with pytest.raises(ValueError, match="mutually exclusive"):
            _cat(rdl, group_field="A", group_expression="=1")
        with pytest.raises(ValueError, match="Label"):
            _cat(rdl, label_expression="")

    def test_group_name_must_be_unique(self, rdl):
        with pytest.raises(ValueError):
            _cat(rdl, add_level="outer", group_field="ProductID", group_name="MainTable")


# ---- through the server -------------------------------------------------------


def _call(server, name, args):
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    return resp["result"]


class TestTools:
    def test_both_tools_dispatch(self, rdl):
        srv = MCPServer()
        register_all_tools(srv)
        r = _call(
            srv,
            "set_chart_series_properties",
            {
                "path": str(rdl),
                "chart_name": CH,
                "series_name": "Quantity",
                "value_axis": "Secondary",
                "marker_type": "Diamond",
                "position": 0,
            },
        )
        assert not r.get("isError"), r
        r = _call(
            srv,
            "set_chart_category_group",
            {"path": str(rdl), "chart_name": CH, "add_level": "outer", "group_field": "ProductID"},
        )
        assert not r.get("isError"), r
        r = _call(srv, "get_chart", {"path": str(rdl), "name": CH})
        chart = json.loads(r["content"][0]["text"])
        assert chart["series"][0]["name"] == "Quantity"
        assert chart["series"][0]["marker"]["type"] == "Diamond"
        assert len(chart["category_groups"]) == 2
        _healthy(rdl)
