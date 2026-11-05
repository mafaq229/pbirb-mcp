"""``chart-structure`` — chart wiring errors the XSD cannot see (#45).

The bundled XSD types nearly every chart element as an unordered
``xsd:string`` choice, so a chart whose series, hierarchies and named
references disagree passes ``validate_report`` while Report Builder
refuses it. Per ``<Chart>`` this rule reports (all ``error``):

* ``ChartSeries`` count ≠ leaf ``ChartMember`` count of
  ``ChartSeriesHierarchy``, and per series ``ChartDataPoint`` count ≠ leaf
  count of ``ChartCategoryHierarchy`` — series and data points belong to
  hierarchy leaves by position (:func:`pbirb_mcp.core.chart_shape.hierarchy_findings`;
  a hierarchy with no member counts as one);
* ``ChartAreaName`` not naming a ``ChartAreas/ChartArea``;
  ``ValueAxisName`` / ``CategoryAxisName`` not naming an axis of that area
  (the first area when ``ChartAreaName`` is absent);
* ``LegendName`` not naming a ``ChartLegends/ChartLegend``;
* ``ChartDerivedSeries/SourceChartSeriesName`` not naming another series
  of the chart (regular or derived — a derived series can be computed
  from another derived series);
* two series (regular and derived share one namespace) with one name;
* ``Type`` / ``Subtype`` not an RDL enum value or not a compatible pair
  (:func:`pbirb_mcp.core.chart_types.series_type_problem`, the table
  ``set_chart_series_type`` writes from);
* ``Palette`` not an RDL ChartPalette value, or ``Custom`` without
  ``ChartCustomPaletteColors``.

Field references in chart expressions (``Y``, ``X``, ...) and their
dataset scope are checked by the expression rules, not here.
"""

from __future__ import annotations

from typing import Optional

from lxml import etree

from pbirb_mcp.core import chart_shape, chart_types
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import find_child, find_children, q
from pbirb_mcp.ops.lint import Issue, _ancestor_chain

RULE = "chart-structure"

# RDL 2016 ChartPalette enumeration — shared with set_chart_palette.
PALETTES: tuple[str, ...] = chart_types.PALETTES


def _text(elem: etree._Element, local: str) -> Optional[str]:
    node = find_child(elem, local)
    if node is None:
        return None
    return (node.text or "").strip()


def _names(parent: Optional[etree._Element], container: str, child: str) -> list[str]:
    block = find_child(parent, container) if parent is not None else None
    if block is None:
        return []
    return [el.get("Name") for el in find_children(block, child) if el.get("Name")]


def _issue(location: str, chart: etree._Element, message: str, suggestion: str) -> Issue:
    return {
        "severity": "error",
        "rule": RULE,
        "location": location,
        "message": f"chart {chart.get('Name')!r}: {message}",
        "suggestion": suggestion,
    }


def _all_series(chart: etree._Element) -> list[tuple[etree._Element, Optional[etree._Element]]]:
    """``(ChartSeries, ChartDerivedSeries or None)`` for every series of
    the chart, regular ones first."""
    out: list[tuple[etree._Element, Optional[etree._Element]]] = [
        (s, None) for s in chart_shape.series_list(chart)
    ]
    for derived in chart_shape.derived_series_list(chart):
        inner = find_child(derived, "ChartSeries")
        if inner is not None:
            out.append((inner, derived))
    return out


def _hierarchy_issues(chart: etree._Element) -> list[Issue]:
    issues: list[Issue] = []
    for message, elem in chart_shape.hierarchy_findings(chart, empty_as_one=True):
        if elem is chart:
            location = _ancestor_chain(chart) + ">ChartSeriesHierarchy"
            suggestion = (
                "give every ChartSeries its own leaf ChartMember (with a Label) in "
                "ChartSeriesHierarchy, in the same order; add_chart_series / "
                "remove_chart_series keep them in step"
            )
        else:
            location = _ancestor_chain(elem) + ">ChartDataPoints"
            suggestion = (
                "give the series one ChartDataPoint per leaf ChartMember of ChartCategoryHierarchy"
            )
        issues.append(_issue(location, chart, message, suggestion))
    return issues


def _reference_issues(chart: etree._Element) -> list[Issue]:
    issues: list[Issue] = []
    areas_root = find_child(chart, "ChartAreas")
    areas = find_children(areas_root, "ChartArea") if areas_root is not None else []
    area_by_name = {a.get("Name"): a for a in areas if a.get("Name")}
    legends = _names(chart, "ChartLegends", "ChartLegend")

    for series, _derived in _all_series(chart):
        where = _ancestor_chain(series)
        sname = series.get("Name")
        area_name = _text(series, "ChartAreaName")
        area: Optional[etree._Element]
        if area_name is not None:
            area = area_by_name.get(area_name)
            if area is None:
                issues.append(
                    _issue(
                        f"{where}>ChartAreaName",
                        chart,
                        f"series {sname!r} has ChartAreaName {area_name!r} but the chart's "
                        f"ChartAreas are {sorted(area_by_name)}",
                        "point ChartAreaName at an existing ChartArea, or remove it",
                    )
                )
        else:
            area = areas[0] if areas else None
        if area is not None:
            for local, container in (
                ("ValueAxisName", "ChartValueAxes"),
                ("CategoryAxisName", "ChartCategoryAxes"),
            ):
                axis_name = _text(series, local)
                if axis_name is None:
                    continue
                axes = _names(area, container, "ChartAxis")
                if axis_name not in axes:
                    issues.append(
                        _issue(
                            f"{where}>{local}",
                            chart,
                            f"series {sname!r} has {local} {axis_name!r} but ChartArea "
                            f"{area.get('Name')!r} has {container} {axes}",
                            f"point {local} at an axis of the series' chart area, or remove it",
                        )
                    )
        legend_name = _text(series, "LegendName")
        if legend_name is not None and legend_name not in legends:
            issues.append(
                _issue(
                    f"{where}>LegendName",
                    chart,
                    f"series {sname!r} has LegendName {legend_name!r} but the chart's "
                    f"ChartLegends are {legends}",
                    "point LegendName at an existing ChartLegend, or remove it",
                )
            )
    return issues


def _name_issues(chart: etree._Element) -> list[Issue]:
    issues: list[Issue] = []
    all_names = chart_shape.series_names(chart)
    seen: set = set()
    for name in all_names:
        if all_names.count(name) > 1 and name not in seen:
            seen.add(name)
            issues.append(
                _issue(
                    f"{_ancestor_chain(chart)}>ChartData",
                    chart,
                    f"{all_names.count(name)} series are named {name!r}; series names "
                    "(ChartSeriesCollection and ChartDerivedSeriesCollection) must be unique",
                    "rename all but one of them",
                )
            )
    for derived in chart_shape.derived_series_list(chart):
        own = chart_shape.derived_series_name(derived)
        source = chart_shape.derived_source_name(derived)
        if source is not None and source != own and source in all_names:
            continue
        what = "itself" if source is not None and source == own else repr(source)
        issues.append(
            _issue(
                f"{_ancestor_chain(chart)}>ChartDerivedSeries[Name={own!r}]>SourceChartSeriesName",
                chart,
                f"derived series {own!r} has SourceChartSeriesName {what}, which is not "
                f"another series of the chart ({[n for n in all_names if n != own]})",
                "point SourceChartSeriesName at an existing series, or remove the derived series",
            )
        )
    return issues


def _type_issues(chart: etree._Element) -> list[Issue]:
    issues: list[Issue] = []
    for series, _derived in _all_series(chart):
        problem = chart_types.series_type_problem(_text(series, "Type"), _text(series, "Subtype"))
        if problem is None:
            continue
        issues.append(
            _issue(
                f"{_ancestor_chain(series)}>Type",
                chart,
                f"series {series.get('Name')!r}: {problem}",
                "set_chart_series_type writes a valid Type/Subtype pair "
                f"({chart_types.describe()})",
            )
        )
    return issues


def _palette_issues(chart: etree._Element) -> list[Issue]:
    palette = _text(chart, "Palette")
    if palette is None or palette.startswith("="):
        return []
    location = f"{_ancestor_chain(chart)}>Palette"
    if palette not in PALETTES:
        return [
            _issue(
                location,
                chart,
                f"Palette {palette!r} is not an RDL ChartPalette; expected one of {list(PALETTES)}",
                "set_chart_palette writes a valid palette",
            )
        ]
    if palette == "Custom":
        colors = find_child(chart, "ChartCustomPaletteColors")
        if colors is None or not find_children(colors, "ChartCustomPaletteColor"):
            return [
                _issue(
                    location,
                    chart,
                    "Palette is 'Custom' but the chart has no "
                    "ChartCustomPaletteColors/ChartCustomPaletteColor",
                    "add <ChartCustomPaletteColors> with at least one "
                    "<ChartCustomPaletteColor>, or pick a built-in palette",
                )
            ]
    return []


def _rule_chart_structure(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    for chart in doc.root.iter(q("Chart")):
        issues.extend(_hierarchy_issues(chart))
        issues.extend(_reference_issues(chart))
        issues.extend(_name_issues(chart))
        issues.extend(_type_issues(chart))
        issues.extend(_palette_issues(chart))
    return issues


RULES = {RULE: _rule_chart_structure}
