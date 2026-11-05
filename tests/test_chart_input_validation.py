"""Chart builders validate their inputs and list the available names in
errors (#101)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops.chart import (
    add_chart_series,
    insert_chart_from_template,
    set_chart_data_labels,
    set_chart_legend,
    set_chart_series_grouping,
    set_chart_title,
)
from pbirb_mcp.ops.validate import validate_report

FIXTURE_RICH = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"
N = f"{{{RDL_NS}}}"
CHART = "SalesByProduct"
BOX = ("7in", "0in", "5in", "3in")


@pytest.fixture
def path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_RICH, dest)
    return dest


def _refused(path: Path, fn, *args, match: str, **kwargs) -> str:
    """``fn`` raises ValueError mentioning ``match`` and leaves the file
    byte-identical; returns the message."""
    before = path.read_bytes()
    with pytest.raises(ValueError, match=match) as exc:
        fn(str(path), *args, **kwargs)
    assert path.read_bytes() == before
    return str(exc.value)


def _chart(path: Path, name: str = CHART) -> etree._Element:
    return RDLDocument.open(path).root.find(f".//{N}Chart[@Name='{name}']")


class TestInsertFields:
    def test_unknown_fields_refused_with_available_list(self, path):
        msg = _refused(
            path,
            insert_chart_from_template,
            "C1",
            "MainDataset",
            "NoSuchCat",
            "Amount",
            *BOX,
            match="category_field",
        )
        assert "ProductName" in msg and "Amount" in msg
        msg = _refused(
            path,
            insert_chart_from_template,
            "C1",
            "MainDataset",
            "ProductName",
            "NoSuchVal",
            *BOX,
            match="value_field",
        )
        assert "ProductID" in msg

    def test_expression_as_field_refused(self, path):
        msg = _refused(
            path,
            insert_chart_from_template,
            "C2",
            "MainDataset",
            "=Fields!ProductName.Value",
            "Amount",
            *BOX,
            match="category_field",
        )
        assert "category_expression" in msg

    def test_field_with_space_refused(self, path):
        _refused(
            path,
            insert_chart_from_template,
            "C2",
            "MainDataset",
            "ProductName",
            "Unit Price",
            *BOX,
            match="value_field",
        )

    @pytest.mark.parametrize(
        "name, box, param",
        [
            ("Bad Name & <x>", BOX, "chart name"),
            ("", BOX, "chart name"),
            ("C3", ("abc", "0in", "1in", "1in"), "top"),
            ("C3", ("0in", "-3in", "1in", "1in"), "left"),
            ("C3", ("0in", "0in", "0cm", "1in"), "width"),
            ("C3", ("0in", "0in", "1in", "10 furlongs"), "height"),
        ],
    )
    def test_bad_name_or_size_refused(self, path, name, box, param):
        _refused(
            path,
            insert_chart_from_template,
            name,
            "MainDataset",
            "ProductName",
            "Amount",
            *box,
            match=param,
        )

    def test_valid_fields_unaffected(self, path):
        result = insert_chart_from_template(
            str(path), "C4", "MainDataset", "ProductName", "Amount", *BOX
        )
        assert result["series"] == "Amount"
        assert validate_report(str(path))["valid"]

    def test_expressions_accepted(self, path):
        insert_chart_from_template(
            str(path),
            "C5",
            "MainDataset",
            top="7in",
            left="0in",
            width="5in",
            height="3in",
            category_expression="=Left(Fields!ProductName.Value, 1)",
            value_expression="=Sum(Fields!Amount.Value) / Count(Fields!ProductID.Value)",
        )
        chart = _chart(path, "C5")
        assert chart.find(f".//{N}GroupExpression").text == "=Left(Fields!ProductName.Value, 1)"
        assert chart.find(f".//{N}Y").text.startswith("=Sum(Fields!Amount.Value) /")
        assert chart.find(f".//{N}ChartSeries").get("Name") == "Series1"
        assert validate_report(str(path))["valid"]

    def test_field_and_expression_exclusive(self, path):
        _refused(
            path,
            insert_chart_from_template,
            "C6",
            "MainDataset",
            "ProductName",
            "Amount",
            *BOX,
            category_expression="=Fields!ProductName.Value",
            match="mutually exclusive",
        )

    def test_value_expression_needs_equals(self, path):
        _refused(
            path,
            insert_chart_from_template,
            "C6",
            "MainDataset",
            "ProductName",
            None,
            *BOX,
            value_expression="Sum(Fields!Amount.Value)",
            match="value_expression",
        )


class TestAddSeries:
    @pytest.mark.parametrize("name", ["", "Bad Name", "1st"])
    def test_bad_series_name_refused(self, path, name):
        _refused(path, add_chart_series, CHART, name, "Amount", match="series name")

    def test_unknown_value_field_refused(self, path):
        msg = _refused(path, add_chart_series, CHART, "S2", "NoSuch", match="value_field")
        assert "available" in msg and "ProductName" in msg

    def test_expression_value_field_refused(self, path):
        _refused(
            path, add_chart_series, CHART, "S2", "=Fields!Amount.Value", match="value_expression"
        )

    def test_value_expression_accepted(self, path):
        add_chart_series(str(path), CHART, "Ratio", value_expression="=Avg(Fields!Amount.Value)")
        series = _chart(path).find(f".//{N}ChartSeries[@Name='Ratio']")
        assert series.find(f".//{N}Y").text == "=Avg(Fields!Amount.Value)"


class TestGrouping:
    def test_group_expression_without_equals_refused(self, path):
        _refused(
            path,
            set_chart_series_grouping,
            CHART,
            "Amount",
            group_expression="Fields!ProductName.Value",
            match="group_expression",
        )

    @pytest.mark.parametrize("field", ["", "=Fields!X.Value", "Unit Price"])
    def test_bad_group_field_refused(self, path, field):
        _refused(
            path,
            set_chart_series_grouping,
            CHART,
            "Amount",
            group_field=field,
            replace=True,
            match="group_field",
        )


class TestDataLabels:
    def test_visible_expression_without_equals_refused(self, path):
        _refused(
            path,
            set_chart_data_labels,
            CHART,
            visible_expression="IIf(1, 2, 3)",
            match="visible_expression",
        )

    def test_visible_expression_literal_boolean_accepted(self, path):
        set_chart_data_labels(str(path), CHART, series_name="Amount", visible_expression="True")
        label = _chart(path).find(f".//{N}ChartSeries[@Name='Amount']//{N}ChartDataLabel")
        assert label.find(f"{N}Visible").text == "true"

    def test_font_weight_enum_checked(self, path):
        _refused(path, set_chart_data_labels, CHART, font_weight="Heavyish", match="font_weight")

    def test_font_weight_canonicalised(self, path):
        set_chart_data_labels(str(path), CHART, series_name="Amount", font_weight="700")
        label = _chart(path).find(f".//{N}ChartSeries[@Name='Amount']//{N}ChartDataLabel")
        assert label.find(f"{N}Style/{N}FontWeight").text == "Bold"


def _rename(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


class TestTitleAndLegendLookup:
    def test_only_title_used_when_not_named_default(self, path):
        _rename(path, '<ChartTitle Name="Default">', '<ChartTitle Name="Title">')
        result = set_chart_title(str(path), CHART, "New")
        assert result["title"] == "Title"
        assert result["changed"] is True
        assert _chart(path).find(f".//{N}ChartTitle[@Name='Title']/{N}Caption").text == "New"

    def test_two_titles_without_name_lists_both(self, path):
        _rename(
            path,
            '<ChartTitle Name="Default"><Caption>Sales by Product</Caption></ChartTitle>',
            '<ChartTitle Name="Title"><Caption>A</Caption></ChartTitle>'
            '<ChartTitle Name="Subtitle"><Caption>B</Caption></ChartTitle>',
        )
        with pytest.raises(ElementNotFoundError) as exc:
            set_chart_title(str(path), CHART, "New")
        assert "Title" in str(exc.value) and "Subtitle" in str(exc.value)

    def test_unknown_title_name_lists_present(self, path):
        with pytest.raises(ElementNotFoundError, match=r"present: \['Default'\]"):
            set_chart_title(str(path), CHART, "New", title_name="Nope")

    def test_only_legend_used_when_not_named_default(self, path):
        _rename(path, '<ChartLegend Name="Default" />', '<ChartLegend Name="Legend1" />')
        result = set_chart_legend(str(path), CHART, position="TopLeft")
        assert result["legend"] == "Legend1"
        assert result["changed"] == ["Position"]

    def test_unknown_legend_lists_present(self, path):
        with pytest.raises(ElementNotFoundError, match="Default"):
            set_chart_legend(str(path), CHART, legend_name="Nope", position="TopLeft")
