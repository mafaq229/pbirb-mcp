"""set_chart_axis: title style kept, true no-ops, every ChartArea, value
validation (#99)."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops.chart import set_chart_axis
from pbirb_mcp.ops.validate import validate_report

FIXTURE_RICH = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"
N = f"{{{RDL_NS}}}"
CHART = "SalesByProduct"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    """The rich chart, its value-axis title carrying a Report Builder
    Style, plus a second ChartArea 'Area2' whose value axis is 'Right'."""
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_RICH, dest)
    tree = etree.parse(str(dest))
    areas = tree.find(f".//{N}ChartAreas")
    title = areas.find(f".//{N}ChartValueAxes/{N}ChartAxis/{N}ChartAxisTitle")
    title.append(etree.fromstring(f'<Style xmlns="{RDL_NS}"><FontWeight>Bold</FontWeight></Style>'))
    area2 = copy.deepcopy(areas[0])
    area2.set("Name", "Area2")
    area2.find(f".//{N}ChartValueAxes/{N}ChartAxis").set("Name", "Right")
    areas.append(area2)
    tree.write(str(dest), xml_declaration=True, encoding="utf-8")
    return dest


def _axis(path: Path, area: str = "Default", kind: str = "ChartValueAxes") -> etree._Element:
    tree = etree.parse(str(path))
    return tree.find(f".//{N}ChartArea[@Name='{area}']/{N}{kind}/{N}ChartAxis")


def _text(axis: etree._Element, local: str):
    node = axis.find(f"{N}{local}")
    return None if node is None else node.text


class TestTitle:
    def test_title_change_keeps_axis_title_style(self, path):
        result = set_chart_axis(str(path), CHART, "Value", title="Revenue")
        assert result["changed"] == ["Title"]
        title = _axis(path).find(f"{N}ChartAxisTitle")
        assert title.find(f"{N}Caption").text == "Revenue"
        assert title.find(f"{N}Style/{N}FontWeight").text == "Bold"
        assert validate_report(str(path))["valid"]

    def test_identical_second_call_is_a_true_noop(self, path):
        set_chart_axis(str(path), CHART, "Value", title="Revenue", min="0", visible=True)
        before = path.read_bytes()
        result = set_chart_axis(str(path), CHART, "Value", title="Revenue", min="0", visible=True)
        assert result["changed"] == []
        assert path.read_bytes() == before

    def test_only_really_changed_fields_reported(self, path):
        set_chart_axis(str(path), CHART, "Value", title="Revenue", min="0")
        result = set_chart_axis(str(path), CHART, "Value", title="Revenue", min="5")
        assert result["changed"] == ["Minimum"]

    def test_title_created_when_absent(self, path):
        set_chart_axis(str(path), CHART, "Value", title="")
        assert _axis(path).find(f"{N}ChartAxisTitle") is None
        set_chart_axis(str(path), CHART, "Value", title="Back")
        assert _axis(path).find(f"{N}ChartAxisTitle/{N}Caption").text == "Back"


class TestChartArea:
    def test_chart_area_addresses_second_area(self, path):
        result = set_chart_axis(str(path), CHART, "Value", chart_area="Area2", title="Right side")
        assert result["chart_area"] == "Area2"
        assert result["axis_name"] == "Right"  # the area's only value axis
        assert _axis(path, "Area2").find(f"{N}ChartAxisTitle/{N}Caption").text == "Right side"
        # The first area's axis is untouched.
        assert _axis(path).find(f"{N}ChartAxisTitle/{N}Caption").text == "Amount (USD)"

    def test_axis_name_unique_to_other_area_is_found(self, path):
        result = set_chart_axis(str(path), CHART, "Value", axis_name="Right", title="x")
        assert result["chart_area"] == "Area2"
        assert _axis(path, "Area2").find(f"{N}ChartAxisTitle/{N}Caption").text == "x"

    def test_miss_lists_area_axis_pairs(self, path):
        before = path.read_bytes()
        with pytest.raises(ElementNotFoundError) as exc:
            set_chart_axis(str(path), CHART, "Value", axis_name="Nope", title="x")
        assert "Default/Primary" in str(exc.value)
        assert "Area2/Right" in str(exc.value)
        assert path.read_bytes() == before

    def test_unknown_chart_area_lists_areas(self, path):
        with pytest.raises(ElementNotFoundError, match="Area2"):
            set_chart_axis(str(path), CHART, "Value", chart_area="Nope", title="x")

    def test_named_axis_must_be_in_given_area(self, path):
        with pytest.raises(ElementNotFoundError):
            set_chart_axis(
                str(path), CHART, "Value", chart_area="Default", axis_name="Right", title="x"
            )


class TestValueValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"interval": "abc"},
            {"min": "ten"},
            {"max": "1,000"},
            {"interval": "-5"},
            {"min": "  "},
        ],
    )
    def test_bad_values_refused(self, path, kwargs):
        before = path.read_bytes()
        with pytest.raises(ValueError, match=next(iter(kwargs))):
            set_chart_axis(str(path), CHART, "Value", **kwargs)
        assert path.read_bytes() == before

    def test_expression_number_and_nan_accepted(self, path):
        result = set_chart_axis(
            str(path),
            CHART,
            "Value",
            interval="=Parameters!I.Value",
            min="-5",
            max=" 1e6 ",
        )
        assert result["changed"] == ["Minimum", "Maximum", "Interval"]
        axis = _axis(path)
        assert _text(axis, "Interval") == "=Parameters!I.Value"
        assert _text(axis, "Maximum") == "1e6"
        set_chart_axis(str(path), CHART, "Value", min="NaN")
        assert _text(_axis(path), "Minimum") == "NaN"

    def test_log_scale_with_non_positive_min_refused(self, path):
        before = path.read_bytes()
        with pytest.raises(ValueError, match="log"):
            set_chart_axis(str(path), CHART, "Value", log_scale=True, min="0")
        assert path.read_bytes() == before

    def test_log_scale_refused_against_stored_min(self, path):
        set_chart_axis(str(path), CHART, "Value", min="-1")
        with pytest.raises(ValueError, match="log"):
            set_chart_axis(str(path), CHART, "Value", log_scale=True)

    def test_min_zero_refused_on_log_axis(self, path):
        set_chart_axis(str(path), CHART, "Value", log_scale=True, min="1")
        with pytest.raises(ValueError, match="log"):
            set_chart_axis(str(path), CHART, "Value", min="0")
        # An expression minimum cannot be checked and is accepted.
        set_chart_axis(str(path), CHART, "Value", min="=Parameters!M.Value")

    def test_log_scale_with_positive_min_accepted(self, path):
        result = set_chart_axis(str(path), CHART, "Value", log_scale=True, min="1")
        assert result["changed"] == ["Minimum", "LogScale"]


class TestVisible:
    def test_visible_auto(self, path):
        set_chart_axis(str(path), CHART, "Value", visible=False)
        result = set_chart_axis(str(path), CHART, "Value", visible="Auto")
        assert result["changed"] == ["Visible"]
        assert _text(_axis(path), "Visible") == "Auto"
        assert validate_report(str(path))["valid"]

    def test_visible_booleans(self, path):
        set_chart_axis(str(path), CHART, "Value", visible=True)
        assert _text(_axis(path), "Visible") == "True"

    def test_visible_bad_string_refused(self, path):
        with pytest.raises(ValueError, match="visible"):
            set_chart_axis(str(path), CHART, "Value", visible="Sometimes")


class TestToolSchema:
    def test_visible_auto_passes_argument_validation(self, path):
        from pbirb_mcp.server import MCPServer
        from pbirb_mcp.tools import register_all_tools

        server = MCPServer()
        register_all_tools(server)
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "set_chart_axis",
                    "arguments": {
                        "path": str(path),
                        "chart_name": CHART,
                        "axis": "Value",
                        "chart_area": "Area2",
                        "visible": "Auto",
                    },
                },
            }
        )
        assert not response["result"].get("isError"), response
        assert _text(_axis(path, "Area2"), "Visible") == "Auto"
