"""set_chart_palette: every RDL ChartPalette value, custom palette colours,
hatching (#100)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import chart_shape, chart_types
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops.chart import set_chart_palette
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.lint_rules import chart_structure
from pbirb_mcp.ops.validate import validate_report

FIXTURE_RICH = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"
N = f"{{{RDL_NS}}}"
CHART = "SalesByProduct"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_RICH, dest)
    return dest


def _chart(path: Path) -> etree._Element:
    return RDLDocument.open(path).root.find(f".//{N}Chart[@Name='{CHART}']")


def _locals(chart: etree._Element) -> list[str]:
    return [etree.QName(c).localname for c in chart if isinstance(c.tag, str)]


def _chart_issues(path: Path) -> list[dict]:
    return [i for i in lint_report(str(path))["issues"] if i["rule"] == "chart-structure"]


def test_one_palette_list_shared_with_lint():
    assert chart_structure.PALETTES is chart_types.PALETTES
    assert len(chart_types.PALETTES) == 16


@pytest.mark.parametrize("palette", [p for p in chart_types.PALETTES if p != "Custom"])
def test_every_builtin_palette_accepted(path, palette):
    result = set_chart_palette(str(path), CHART, palette)
    assert result["changed"] is (palette != "EarthTones")  # the fixture's palette
    assert _chart(path).find(f"{N}Palette").text == palette
    assert validate_report(str(path))["valid"]
    assert _chart_issues(path) == []


def test_palette_matched_case_insensitively(path):
    set_chart_palette(str(path), CHART, "pacificlight")
    assert _chart(path).find(f"{N}Palette").text == "PacificLight"


def test_unknown_palette_refused(path):
    with pytest.raises(ValueError, match="palette"):
        set_chart_palette(str(path), CHART, "Rainbow")


class TestCustom:
    def test_custom_writes_colours_in_order(self, path):
        result = set_chart_palette(str(path), CHART, "Custom", colors=["#112233", "Red"])
        assert result["changed"] is True
        assert result["custom_colors"] == ["#112233", "Red"]
        chart = _chart(path)
        assert chart_shape.custom_palette_colors(chart) == ["#112233", "Red"]
        order = _locals(chart)
        # Next to <Palette>, before DataSetName.
        assert order.index("ChartCustomPaletteColors") == order.index("Palette") + 1
        assert order.index("ChartCustomPaletteColors") < order.index("DataSetName")
        assert validate_report(str(path))["valid"]
        assert _chart_issues(path) == []

    def test_same_call_again_is_a_noop(self, path):
        set_chart_palette(str(path), CHART, "Custom", colors=["#112233", "Red"])
        before = path.read_bytes()
        result = set_chart_palette(str(path), CHART, "Custom", colors=["#112233", "Red"])
        assert result["changed"] is False
        assert path.read_bytes() == before

    def test_new_colours_replace_old(self, path):
        set_chart_palette(str(path), CHART, "Custom", colors=["#112233", "Red"])
        result = set_chart_palette(str(path), CHART, "Custom", colors=["Blue"])
        assert result["changed"] is True
        assert chart_shape.custom_palette_colors(_chart(path)) == ["Blue"]

    def test_custom_without_colours_refused(self, path):
        before = path.read_bytes()
        with pytest.raises(ValueError, match="colors"):
            set_chart_palette(str(path), CHART, "Custom")
        assert path.read_bytes() == before

    def test_custom_without_colours_keeps_existing(self, path):
        set_chart_palette(str(path), CHART, "Custom", colors=["Red"])
        set_chart_palette(str(path), CHART, "Pastel")
        # Built-in palettes leave the custom colours alone ...
        assert chart_shape.custom_palette_colors(_chart(path)) == ["Red"]
        # ... so switching back needs no colours.
        result = set_chart_palette(str(path), CHART, "Custom")
        assert result["changed"] is True
        assert result["custom_colors"] == ["Red"]

    def test_colours_with_builtin_palette_refused(self, path):
        with pytest.raises(ValueError, match="colors"):
            set_chart_palette(str(path), CHART, "Pastel", colors=["Red"])

    @pytest.mark.parametrize("colors", [[], ["notacolor"], ["#GGG"], ["Red", ""]])
    def test_bad_colours_refused(self, path, colors):
        before = path.read_bytes()
        with pytest.raises(ValueError, match="colors"):
            set_chart_palette(str(path), CHART, "Custom", colors=colors)
        assert path.read_bytes() == before

    def test_colours_trimmed_and_expressions_kept(self, path):
        set_chart_palette(
            str(path), CHART, "Custom", colors=["  #FF0000 ", "=Parameters!Brand.Value"]
        )
        assert chart_shape.custom_palette_colors(_chart(path)) == [
            "#FF0000",
            "=Parameters!Brand.Value",
        ]

    def test_clear_removes_custom_colours(self, path):
        set_chart_palette(str(path), CHART, "Custom", colors=["Red"])
        result = set_chart_palette(str(path), CHART, "")
        assert result["changed"] is True
        order = _locals(_chart(path))
        assert "Palette" not in order
        assert "ChartCustomPaletteColors" not in order


class TestHatch:
    def test_hatch_behavior_written_after_palette_block(self, path):
        set_chart_palette(str(path), CHART, "Custom", colors=["Red"], hatch_behavior="always")
        chart = _chart(path)
        assert chart.find(f"{N}PaletteHatchBehavior").text == "Always"
        order = _locals(chart)
        assert order.index("PaletteHatchBehavior") == order.index("ChartCustomPaletteColors") + 1
        assert validate_report(str(path))["valid"]

    def test_hatch_noop_and_clear(self, path):
        set_chart_palette(str(path), CHART, "EarthTones", hatch_behavior="None")
        before = path.read_bytes()
        assert (
            set_chart_palette(str(path), CHART, "EarthTones", hatch_behavior="None")["changed"]
            is False
        )
        assert path.read_bytes() == before
        assert set_chart_palette(str(path), CHART, "EarthTones", hatch_behavior="")["changed"]
        assert _chart(path).find(f"{N}PaletteHatchBehavior") is None

    def test_bad_hatch_refused(self, path):
        with pytest.raises(ValueError, match="hatch_behavior"):
            set_chart_palette(str(path), CHART, "Pastel", hatch_behavior="Sometimes")


def test_tool_schema_accepts_custom_colors(path):
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
                "name": "set_chart_palette",
                "arguments": {
                    "path": str(path),
                    "chart_name": CHART,
                    "palette": "Custom",
                    "colors": ["#112233", "Red"],
                    "hatch_behavior": "Always",
                },
            },
        }
    )
    assert not response["result"].get("isError"), response
    assert chart_shape.custom_palette_colors(_chart(path)) == ["#112233", "Red"]
