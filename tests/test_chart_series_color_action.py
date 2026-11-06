"""set_chart_series_action and set_series_color gaps (#103)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops.actions import set_chart_series_action
from pbirb_mcp.ops.chart import set_series_color
from pbirb_mcp.ops.validate import validate_report

FIXTURE_RICH = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"
N = f"{{{RDL_NS}}}"
CHART = "SalesByProduct"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_RICH, dest)
    return dest


def _series(path: Path, name: str) -> etree._Element:
    return RDLDocument.open(path).root.find(f".//{N}ChartSeries[@Name='{name}']")


def _point_color_fixture(path: Path) -> None:
    """Give Quantity's template data point a Style/Color, the shape Report
    Builder's Series Properties > Fill writes."""
    text = path.read_text(encoding="utf-8")
    old = "<Y>=Sum(Fields!ProductID.Value)</Y></ChartDataPointValues>"
    assert old in text
    path.write_text(
        text.replace(old, old + "<Style><Color>Blue</Color></Style>", 1), encoding="utf-8"
    )


class TestSeriesAction:
    def test_hyperlink_with_parameters_refused(self, path):
        before = path.read_bytes()
        with pytest.raises(ValueError, match="drillthrough_parameters"):
            set_chart_series_action(
                str(path),
                CHART,
                "Amount",
                "Hyperlink",
                "https://example.test",
                [{"name": "P", "value": "=1"}],
            )
        assert path.read_bytes() == before

    def test_bookmark_with_parameters_refused(self, path):
        with pytest.raises(ValueError, match="Drillthrough"):
            set_chart_series_action(
                str(path), CHART, "Amount", "BookmarkLink", "B1", [{"name": "P", "value": "1"}]
            )

    def test_drillthrough_with_parameters_still_works(self, path):
        result = set_chart_series_action(
            str(path), CHART, "Amount", "Drillthrough", "Detail", [{"name": "P", "value": "=1"}]
        )
        assert result["changed"] is True
        assert '<Parameter Name="P">' in path.read_text(encoding="utf-8")

    def test_none_removes_action_and_repeats_as_noop(self, path):
        set_chart_series_action(str(path), CHART, "Amount", "Hyperlink", "https://example.test")
        result = set_chart_series_action(str(path), CHART, "Amount", "None")
        assert result == {
            "chart": CHART,
            "series": "Amount",
            "kind": "ChartSeries",
            "action_type": "None",
            "changed": True,
        }
        assert _series(path, "Amount").find(f".//{N}ActionInfo") is None
        assert validate_report(str(path))["valid"]
        before = path.read_bytes()
        assert set_chart_series_action(str(path), CHART, "Amount", "None")["changed"] is False
        assert path.read_bytes() == before

    def test_none_removes_legacy_series_level_action(self, path):
        doc = RDLDocument.open(path)
        series = doc.root.find(f".//{N}ChartSeries[@Name='Amount']")
        etree.SubElement(series, f"{N}ActionInfo")
        doc.save()
        assert set_chart_series_action(str(path), CHART, "Amount", "None")["changed"] is True
        assert _series(path, "Amount").find(f"{N}ActionInfo") is None

    def test_none_with_target_refused(self, path):
        with pytest.raises(ValueError, match="None"):
            set_chart_series_action(str(path), CHART, "Amount", "None", "https://x.test")


class TestSeriesColorValidation:
    @pytest.mark.parametrize("color", ["notacolor", "#GGG", "#12345", "   "])
    def test_bad_colours_refused(self, path, color):
        before = path.read_bytes()
        with pytest.raises(ValueError, match="color"):
            set_series_color(str(path), CHART, "Quantity", color)
        assert path.read_bytes() == before

    def test_whitespace_trimmed(self, path):
        set_series_color(str(path), CHART, "Quantity", "  #FF0000  ")
        assert _series(path, "Quantity").find(f"{N}Style/{N}Color").text == "#FF0000"
        # The trimmed value is what is compared on a repeat.
        assert set_series_color(str(path), CHART, "Quantity", "#FF0000")["changed"] is False

    @pytest.mark.parametrize("color", ["red", "#80FF0000", '=IIf(1 > 0, "Red", "Blue")'])
    def test_valid_colours_accepted(self, path, color):
        assert set_series_color(str(path), CHART, "Quantity", color)["changed"] is True


class TestSeriesColorTarget:
    def test_point_colour_is_changed_by_default(self, path):
        _point_color_fixture(path)
        result = set_series_color(str(path), CHART, "Quantity", "Green")
        assert result["target"] == "point"
        assert result["changed"] is True
        series = _series(path, "Quantity")
        point = series.find(f".//{N}ChartDataPoint")
        assert point.find(f"{N}Style/{N}Color").text == "Green"
        assert series.find(f"{N}Style") is None  # nothing written at series level
        assert validate_report(str(path))["valid"]

    def test_series_target_warns_when_point_overrides(self, path):
        _point_color_fixture(path)
        result = set_series_color(str(path), CHART, "Quantity", "Green", target="series")
        assert result["target"] == "series"
        assert result["warnings"] and "target='point'" in result["warnings"][0]
        assert _series(path, "Quantity").find(f"{N}Style/{N}Color").text == "Green"

    def test_point_target_on_plain_series(self, path):
        result = set_series_color(
            str(path), CHART, "Amount", '=IIf(Fields!Amount.Value > 0, "Green", "Red")', "point"
        )
        assert result["target"] == "point"
        point = _series(path, "Amount").find(f".//{N}ChartDataPoint")
        assert point.find(f"{N}Style/{N}Color").text.startswith("=IIf(")
        # Style goes right after ChartDataPointValues.
        assert [etree.QName(c).localname for c in point] == ["ChartDataPointValues", "Style"]
        assert validate_report(str(path))["valid"]

    def test_default_target_is_series_without_point_colour(self, path):
        result = set_series_color(str(path), CHART, "Amount", "Red")
        assert result["target"] == "series"
        assert result["warnings"] == []

    def test_bad_target_refused(self, path):
        with pytest.raises(ValueError, match="target"):
            set_series_color(str(path), CHART, "Amount", "Red", target="legend")


class TestSeriesColorClear:
    def test_clear_keeps_existing_empty_style(self, path):
        doc = RDLDocument.open(path)
        series = doc.root.find(f".//{N}ChartSeries[@Name='Amount']")
        etree.SubElement(series, f"{N}Style")
        doc.save()
        set_series_color(str(path), CHART, "Amount", "Red")
        result = set_series_color(str(path), CHART, "Amount", "")
        assert result["changed"] is True
        style = _series(path, "Amount").find(f"{N}Style")
        assert style is not None and len(style) == 0

    def test_clear_point_colour(self, path):
        _point_color_fixture(path)
        result = set_series_color(str(path), CHART, "Quantity", "")
        assert result["target"] == "point"
        assert result["changed"] is True
        point = _series(path, "Quantity").find(f".//{N}ChartDataPoint")
        assert point.find(f"{N}Style/{N}Color") is None
        before = path.read_bytes()
        assert set_series_color(str(path), CHART, "Quantity", "")["changed"] is False
        assert path.read_bytes() == before
