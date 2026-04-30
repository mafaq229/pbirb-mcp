"""Chart authoring tools.

Charts in RDL are positioned report items with a fixed-but-deep child
schema:

    <Chart Name="...">
      <ChartCategoryHierarchy>...</ChartCategoryHierarchy>
      <ChartSeriesHierarchy>...</ChartSeriesHierarchy>
      <ChartData>
        <ChartSeriesCollection>
          <ChartSeries Name="...">...</ChartSeries>
          ...
        </ChartSeriesCollection>
      </ChartData>
      <ChartAreas>
        <ChartArea Name="Default">
          <ChartCategoryAxes>
            <ChartAxis Name="Primary"/>
          </ChartCategoryAxes>
          <ChartValueAxes>
            <ChartAxis Name="Primary"/>
          </ChartValueAxes>
        </ChartArea>
      </ChartAreas>
      <ChartLegends>...</ChartLegends>
      <ChartTitles>...</ChartTitles>
      <Palette>Default</Palette>          (optional)
      <DataSetName>...</DataSetName>
      <Top>/<Left>/<Height>/<Width>
      <Style/>
    </Chart>

Critical schema gotchas (learned the hard way; comments preserved from
v0.2 templates.py):

- ``ChartCategoryAxes`` and ``ChartValueAxes`` contain ``<ChartAxis>``
  children directly. There is NO ``<ChartCategoryAxis>`` /
  ``<ChartValueAxis>`` wrapper element — Report Builder rejects the
  wrapped form with "invalid child element 'ChartCategoryAxis' ...
  expected 'ChartAxis'".

- ``ChartMember`` requires a ``<Label>`` child even for empty members.
  Omit it and Report Builder's deserializer fails with "ChartMember is
  empty ... missing mandatory child element of type 'Label'", even
  though the parser accepts the well-formed XML.

This module currently exposes ``insert_chart_from_template`` (extracted
from v0.2's ``templates.py``); v0.3 commits add series CRUD, axis
controls, and decoration tools.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError, resolve_dataset
from pbirb_mcp.core.xpath import RDL_NS, XPATH_NS, find_child, find_children, q
from pbirb_mcp.ops.body import _ensure_body_report_items, _names_in, _resolve_body


# RDL ChartSeriesType + Subtype enumeration (covers the common shapes;
# extend if a real session needs Funnel / Pyramid / Polar / Range etc.).
_VALID_SERIES_TYPES = frozenset(
    {
        "Column",
        "Bar",
        "Line",
        "Area",
        "Pie",
        "Doughnut",
        "Range",
        "Scatter",
        "Bubble",
        "Stock",
        "Polar",
        "Radar",
        "Funnel",
        "Pyramid",
    }
)
_VALID_SERIES_SUBTYPES = frozenset(
    {
        "Plain",
        "Stacked",
        "PercentStacked",
        "Smooth",
        "Exploded",
        "SmoothLine",
        "100",
        "Line",
        "Spline",
    }
)


def _resolve_chart(doc: RDLDocument, name: str) -> etree._Element:
    """Find a ``<Chart Name="...">`` anywhere in the report (body, header,
    footer, rectangle children). Raises ``ElementNotFoundError`` on miss
    and ``AmbiguousElementError`` on duplicate-name."""
    matches = list(
        doc.root.xpath(
            f".//*[local-name()='Chart' and @Name=$n]",
            namespaces=XPATH_NS,
            n=name,
        )
    )
    if not matches:
        raise ElementNotFoundError(f"no Chart named {name!r}")
    if len(matches) > 1:
        raise AmbiguousElementError(
            f"Chart name {name!r} matches {len(matches)} elements"
        )
    return matches[0]


def _series_collection(chart: etree._Element) -> etree._Element:
    """Return the ``<ChartSeriesCollection>`` for ``chart``. Defensive: a
    well-formed chart always has one (the template emits it), but this
    raises a clear error if it's missing rather than NPEing."""
    cd = find_child(chart, "ChartData")
    if cd is None:
        raise ElementNotFoundError(
            f"chart {chart.get('Name')!r} has no <ChartData>"
        )
    sc = find_child(cd, "ChartSeriesCollection")
    if sc is None:
        raise ElementNotFoundError(
            f"chart {chart.get('Name')!r} has no <ChartSeriesCollection>"
        )
    return sc


def _series_names(series_collection: etree._Element) -> list[str]:
    return [s.get("Name") for s in find_children(series_collection, "ChartSeries")]


def _find_series(
    series_collection: etree._Element, series_name: str
) -> etree._Element:
    for s in find_children(series_collection, "ChartSeries"):
        if s.get("Name") == series_name:
            return s
    raise ElementNotFoundError(
        f"no ChartSeries named {series_name!r} (present: {_series_names(series_collection)})"
    )


def _all_named_items(doc: RDLDocument) -> set[str]:
    """Names of every Tablix / Textbox / Image / Chart in the body."""
    body = _resolve_body(doc)
    items = find_child(body, "ReportItems")
    return _names_in(items) if items is not None else set()


def _ensure_no_collision(doc: RDLDocument, name: str) -> None:
    if name in _all_named_items(doc):
        raise ValueError(f"body item named {name!r} already exists; pick a unique name")


# ---- insert_chart_from_template -------------------------------------------


def insert_chart_from_template(
    path: str,
    name: str,
    dataset_name: str,
    category_field: str,
    value_field: str,
    top: str,
    left: str,
    width: str,
    height: str,
) -> dict[str, Any]:
    """Insert a basic Column chart bound to ``dataset_name``.

    The chart has one category axis (grouped by ``category_field``) and
    one Y series (``=Sum(Fields!<value_field>.Value)``). Type defaults to
    Column; callers can change it post-insert via :func:`set_chart_series_type`.
    """
    doc = RDLDocument.open(path)
    resolve_dataset(doc, dataset_name)
    _ensure_no_collision(doc, name)

    chart = etree.Element(q("Chart"), Name=name)

    # ChartCategoryHierarchy — single category group.
    cat_hier = etree.SubElement(chart, q("ChartCategoryHierarchy"))
    cat_members = etree.SubElement(cat_hier, q("ChartMembers"))
    cat_member = etree.SubElement(cat_members, q("ChartMember"))
    cat_group = etree.SubElement(cat_member, q("Group"), Name=f"{name}_CategoryGroup")
    cat_expressions = etree.SubElement(cat_group, q("GroupExpressions"))
    cat_expression = etree.SubElement(cat_expressions, q("GroupExpression"))
    cat_expression.text = encode_text(f"=Fields!{category_field}.Value")
    cat_label = etree.SubElement(cat_member, q("Label"))
    cat_label.text = encode_text(f"=Fields!{category_field}.Value")

    # ChartSeriesHierarchy — single static series. Per RDL XSD a ChartMember
    # is required to have a <Label>; an empty member fails Report Builder's
    # deserializer with "ChartMember is empty ... missing mandatory child
    # element of type 'Label'", even though it parses as well-formed XML.
    series_hier = etree.SubElement(chart, q("ChartSeriesHierarchy"))
    series_members = etree.SubElement(series_hier, q("ChartMembers"))
    series_member = etree.SubElement(series_members, q("ChartMember"))
    etree.SubElement(series_member, q("Label")).text = encode_text(value_field)

    # ChartData — one ChartSeries with Y = Sum(value_field).
    chart_data = etree.SubElement(chart, q("ChartData"))
    series_collection = etree.SubElement(chart_data, q("ChartSeriesCollection"))
    series = etree.SubElement(series_collection, q("ChartSeries"), Name=value_field)
    data_points = etree.SubElement(series, q("ChartDataPoints"))
    data_point = etree.SubElement(data_points, q("ChartDataPoint"))
    data_point_values = etree.SubElement(data_point, q("ChartDataPointValues"))
    y_node = etree.SubElement(data_point_values, q("Y"))
    y_node.text = encode_text(f"=Sum(Fields!{value_field}.Value)")
    etree.SubElement(series, q("Type")).text = "Column"
    etree.SubElement(series, q("Subtype")).text = "Plain"

    # ChartAreas — Default with primary category and value axes. Per the
    # RDL XSD, ChartCategoryAxes / ChartValueAxes contain <ChartAxis>
    # children DIRECTLY; there is no <ChartCategoryAxis>/<ChartValueAxis>
    # wrapper element. Report Builder's deserializer rejects the wrapped
    # form with "invalid child element 'ChartCategoryAxis' ... expected
    # 'ChartAxis'".
    chart_areas = etree.SubElement(chart, q("ChartAreas"))
    chart_area = etree.SubElement(chart_areas, q("ChartArea"), Name="Default")
    cat_axes = etree.SubElement(chart_area, q("ChartCategoryAxes"))
    etree.SubElement(cat_axes, q("ChartAxis"), Name="Primary")
    val_axes = etree.SubElement(chart_area, q("ChartValueAxes"))
    etree.SubElement(val_axes, q("ChartAxis"), Name="Primary")

    # Legend + title.
    legends = etree.SubElement(chart, q("ChartLegends"))
    etree.SubElement(legends, q("ChartLegend"), Name="Default")
    titles = etree.SubElement(chart, q("ChartTitles"))
    title = etree.SubElement(titles, q("ChartTitle"), Name="Default")
    etree.SubElement(title, q("Caption")).text = encode_text(name)

    # Dataset binding + layout.
    etree.SubElement(chart, q("DataSetName")).text = dataset_name
    etree.SubElement(chart, q("Top")).text = top
    etree.SubElement(chart, q("Left")).text = left
    etree.SubElement(chart, q("Height")).text = height
    etree.SubElement(chart, q("Width")).text = width
    etree.SubElement(chart, q("Style"))

    body = _resolve_body(doc)
    items = _ensure_body_report_items(body)
    items.append(chart)

    doc.save()
    return {
        "name": name,
        "kind": "Chart",
        "type": "Column",
        "category_field": category_field,
        "value_field": value_field,
    }


_VALID_AXIS_KINDS = ("Category", "Value")


# RDL Position enum for ChartLegend. Other valid positions exist
# (LeftCenter, RightCenter, etc.); this list mirrors what Report Builder's
# legend dropdown shows by default.
_VALID_LEGEND_POSITIONS = frozenset(
    {
        "TopLeft",
        "TopCenter",
        "TopRight",
        "LeftTop",
        "LeftCenter",
        "LeftBottom",
        "RightTop",
        "RightCenter",
        "RightBottom",
        "BottomLeft",
        "BottomCenter",
        "BottomRight",
    }
)


# Per RDL XSD, ChartLegend child order (subset relevant here):
#   Name (attr), Hidden, Position, Layout, DockOutsideChartArea, ...
_CHART_LEGEND_CHILD_ORDER = (
    "Hidden",
    "Position",
    "Layout",
    "DockOutsideChartArea",
    "ChartElementPosition",
    "AutoFitTextDisabled",
    "MinFontSize",
    "BorderSkin",
    "Style",
)


# Per RDL XSD, ChartDataLabel child order (subset):
#   Visible, UseValueAsLabel, Position, Rotation, Style
_CHART_DATA_LABEL_CHILD_ORDER = (
    "Visible",
    "UseValueAsLabel",
    "Position",
    "Rotation",
    "Style",
)


# Per RDL 2016 XSD, ChartAxis child order (subset relevant to
# set_chart_axis): Visible, Style, ChartAxisTitle, Margin, Interval,
# IntervalType, ChartMajorGridLines, ChartMinorGridLines, ...
# Reverse, CrossAt, Location, ..., Minimum, Maximum, LogScale.
#
# **`Title` is NOT a valid ChartAxis child** — the title element is
# `ChartAxisTitle`. Pre-v0.3.1 set_chart_axis emitted bare <Title>
# which RB rejected with "has invalid child element 'Title'".
_CHART_AXIS_CHILD_ORDER = (
    "Visible",
    "Style",
    "ChartAxisTitle",
    "Margin",
    "Interval",
    "IntervalType",
    "ChartMajorGridLines",
    "ChartMinorGridLines",
    "ChartMajorTickMarks",
    "ChartMinorTickMarks",
    "Minimum",
    "Maximum",
    "LogScale",
)


def _resolve_chart_axis(
    chart: etree._Element, axis_kind: str, axis_name: str = "Primary"
) -> etree._Element:
    """Resolve the named ``<ChartAxis>`` inside the Default ChartArea's
    Category or Value collection. Raises ``ElementNotFoundError`` if the
    chart's structure is malformed or the named axis isn't present.
    """
    if axis_kind not in _VALID_AXIS_KINDS:
        raise ValueError(
            f"axis must be one of {_VALID_AXIS_KINDS}; got {axis_kind!r}"
        )
    chart_areas = find_child(chart, "ChartAreas")
    if chart_areas is None:
        raise ElementNotFoundError(
            f"chart {chart.get('Name')!r} has no <ChartAreas>"
        )
    chart_area = find_child(chart_areas, "ChartArea")
    if chart_area is None:
        raise ElementNotFoundError(
            f"chart {chart.get('Name')!r} has no <ChartArea>"
        )
    axes_local = "ChartCategoryAxes" if axis_kind == "Category" else "ChartValueAxes"
    axes_root = find_child(chart_area, axes_local)
    if axes_root is None:
        raise ElementNotFoundError(
            f"chart {chart.get('Name')!r} has no <{axes_local}>"
        )
    for axis in find_children(axes_root, "ChartAxis"):
        if axis.get("Name") == axis_name:
            return axis
    raise ElementNotFoundError(
        f"no {axis_kind} axis named {axis_name!r} in chart "
        f"{chart.get('Name')!r}"
    )


def _insert_axis_child_in_order(
    axis: etree._Element, new_child: etree._Element
) -> None:
    """Insert ``new_child`` into ``axis`` respecting the schema-required
    sibling order; replace any existing element of the same local name."""
    new_local = etree.QName(new_child).localname
    existing = find_child(axis, new_local)
    if existing is not None:
        axis.replace(existing, new_child)
        return
    if new_local in _CHART_AXIS_CHILD_ORDER:
        new_idx = _CHART_AXIS_CHILD_ORDER.index(new_local)
        for i, child in enumerate(list(axis)):
            local = etree.QName(child).localname
            if (
                local in _CHART_AXIS_CHILD_ORDER
                and _CHART_AXIS_CHILD_ORDER.index(local) > new_idx
            ):
                axis.insert(i, new_child)
                return
    axis.append(new_child)


def _set_axis_title(axis: etree._Element, title: str) -> None:
    """Write or rewrite
    ``<ChartAxisTitle><Caption>title</Caption></ChartAxisTitle>``.

    Empty string clears the title block. Migrates pre-v0.3.1 reports
    that have a bare ``<Title>`` element by removing it (RB rejects
    that name on a ChartAxis).
    """
    legacy = find_child(axis, "Title")
    if legacy is not None:
        axis.remove(legacy)
    existing = find_child(axis, "ChartAxisTitle")
    if title == "":
        if existing is not None:
            axis.remove(existing)
        return
    new_title = etree.Element(q("ChartAxisTitle"))
    etree.SubElement(new_title, q("Caption")).text = encode_text(title)
    if existing is not None:
        axis.replace(existing, new_title)
    else:
        _insert_axis_child_in_order(axis, new_title)


def _set_axis_format(axis: etree._Element, format_value: str) -> None:
    """Write the axis numeric/date format. The format lives at
    ``ChartAxis/Style/Format`` (the box-style sub-element), not as a
    direct ChartAxis child."""
    style = find_child(axis, "Style")
    if style is None:
        style = etree.Element(q("Style"))
        _insert_axis_child_in_order(axis, style)
    fmt = find_child(style, "Format")
    if format_value == "":
        if fmt is not None:
            style.remove(fmt)
        return
    if fmt is None:
        fmt = etree.SubElement(style, q("Format"))
    fmt.text = encode_text(format_value)


def _set_axis_simple_text(
    axis: etree._Element, local: str, value: str
) -> None:
    """Common path for Minimum / Maximum / Interval / LogScale / Visible."""
    if value == "":
        existing = find_child(axis, local)
        if existing is not None:
            axis.remove(existing)
        return
    new_node = etree.Element(q(local))
    new_node.text = encode_text(value)
    _insert_axis_child_in_order(axis, new_node)


def set_chart_axis(
    path: str,
    chart_name: str,
    axis: str,
    axis_name: str = "Primary",
    title: Optional[str] = None,
    format: Optional[str] = None,  # noqa: A002 - tool-facing arg name; intentional
    min: Optional[str] = None,  # noqa: A002 - tool-facing
    max: Optional[str] = None,  # noqa: A002 - tool-facing
    log_scale: Optional[bool] = None,
    interval: Optional[str] = None,
    visible: Optional[bool] = None,
) -> dict[str, Any]:
    """Configure a chart axis: title, numeric format, range, log scale,
    interval, visibility.

    ``axis`` ∈ {Category, Value}. ``axis_name`` defaults to ``Primary``
    (the only axis the template emits today; pass an explicit name when
    the chart has secondary axes).

    Each optional field follows the canonical "None = unchanged, '' =
    clear the element" convention. ``log_scale`` / ``visible`` accept
    booleans; pass ``None`` to leave them unchanged.

    Returns ``{chart, axis, axis_name, kind, changed: list[str]}`` —
    empty list when nothing was actually set (no-op short-circuit, no
    save).
    """
    if axis not in _VALID_AXIS_KINDS:
        raise ValueError(
            f"axis must be one of {_VALID_AXIS_KINDS}; got {axis!r}"
        )

    flags = {
        "title": title,
        "format": format,
        "min": min,
        "max": max,
        "log_scale": log_scale,
        "interval": interval,
        "visible": visible,
    }
    if all(v is None for v in flags.values()):
        return {
            "chart": chart_name,
            "axis": axis,
            "axis_name": axis_name,
            "kind": "ChartAxis",
            "changed": [],
        }

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    axis_el = _resolve_chart_axis(chart, axis, axis_name)

    changed: list[str] = []

    if title is not None:
        _set_axis_title(axis_el, title)
        changed.append("Title")

    if format is not None:
        _set_axis_format(axis_el, format)
        changed.append("Style.Format")

    if min is not None:
        _set_axis_simple_text(axis_el, "Minimum", min)
        changed.append("Minimum")

    if max is not None:
        _set_axis_simple_text(axis_el, "Maximum", max)
        changed.append("Maximum")

    if log_scale is not None:
        _set_axis_simple_text(axis_el, "LogScale", "true" if log_scale else "false")
        changed.append("LogScale")

    if interval is not None:
        _set_axis_simple_text(axis_el, "Interval", interval)
        changed.append("Interval")

    if visible is not None:
        _set_axis_simple_text(axis_el, "Visible", "true" if visible else "false")
        changed.append("Visible")

    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "axis": axis,
        "axis_name": axis_name,
        "kind": "ChartAxis",
        "changed": changed,
    }


# ---- add_chart_series -----------------------------------------------------


def _build_chart_series(
    series_name: str,
    value_field: str,
    series_type: str = "Column",
    series_subtype: str = "Plain",
) -> etree._Element:
    """Construct a single ``<ChartSeries>`` with the canonical shape.

    Mirrors the structure :func:`insert_chart_from_template` builds for
    the initial series, so a chart with N series via this tool reads
    consistently via :func:`get_chart`.
    """
    series = etree.Element(q("ChartSeries"), Name=series_name)
    data_points = etree.SubElement(series, q("ChartDataPoints"))
    data_point = etree.SubElement(data_points, q("ChartDataPoint"))
    values = etree.SubElement(data_point, q("ChartDataPointValues"))
    y_node = etree.SubElement(values, q("Y"))
    y_node.text = encode_text(f"=Sum(Fields!{value_field}.Value)")
    etree.SubElement(series, q("Type")).text = series_type
    etree.SubElement(series, q("Subtype")).text = series_subtype
    return series


def add_chart_series(
    path: str,
    chart_name: str,
    series_name: str,
    value_field: str,
    series_type: str = "Column",
    series_subtype: str = "Plain",
) -> dict[str, Any]:
    """Append a new ``<ChartSeries>`` to the named chart.

    The series is built with ``Y = Sum(Fields!<value_field>.Value)`` —
    same shape :func:`insert_chart_from_template` emits for the first
    series. Override after the fact via :func:`set_chart_series_type`
    or by editing the data point values.

    Refuses if ``series_name`` already exists in the chart.
    """
    if series_type not in _VALID_SERIES_TYPES:
        raise ValueError(
            f"series_type {series_type!r} not valid; "
            f"expected one of {sorted(_VALID_SERIES_TYPES)}"
        )
    if series_subtype not in _VALID_SERIES_SUBTYPES:
        raise ValueError(
            f"series_subtype {series_subtype!r} not valid; "
            f"expected one of {sorted(_VALID_SERIES_SUBTYPES)}"
        )

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)

    if series_name in _series_names(sc):
        raise ValueError(
            f"chart {chart_name!r} already has a series named {series_name!r}"
        )

    sc.append(
        _build_chart_series(
            series_name=series_name,
            value_field=value_field,
            series_type=series_type,
            series_subtype=series_subtype,
        )
    )
    doc.save()
    return {
        "chart": chart_name,
        "name": series_name,
        "kind": "ChartSeries",
        "type": series_type,
        "subtype": series_subtype,
        "value_field": value_field,
    }


# ---- remove_chart_series --------------------------------------------------


def remove_chart_series(
    path: str,
    chart_name: str,
    series_name: str,
) -> dict[str, Any]:
    """Remove a ``<ChartSeries>`` by name. Refuses if removing it would
    leave the chart with zero series — RB renders a chart with no series
    as an empty box, which is rarely the user's intent. Use
    :func:`add_chart_series` first if you really want to swap series.
    """
    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)
    target = _find_series(sc, series_name)

    remaining = [s for s in find_children(sc, "ChartSeries") if s is not target]
    if not remaining:
        raise ValueError(
            f"refusing to remove the last series in chart {chart_name!r}; "
            "add another series first or remove the chart entirely "
            "via remove_body_item."
        )

    sc.remove(target)
    doc.save()
    return {
        "chart": chart_name,
        "removed": series_name,
        "remaining": [s.get("Name") for s in remaining],
    }


# ---- set_chart_series_type ------------------------------------------------


def set_chart_series_type(
    path: str,
    chart_name: str,
    series_name: str,
    series_type: str,
    series_subtype: str = "Plain",
) -> dict[str, Any]:
    """Update the ``<Type>`` and ``<Subtype>`` of a named series.

    Combo-chart pattern: the chart can hold series of mixed types — a
    Bar series + a Line series in the same chart renders as a combo
    bar+line. Set each series's type independently with this tool.

    ``series_type`` ∈ Column / Bar / Line / Area / Pie / Doughnut /
    Range / Scatter / Bubble / Stock / Polar / Radar / Funnel / Pyramid.

    ``series_subtype`` ∈ Plain / Stacked / PercentStacked / Smooth /
    Exploded / SmoothLine / 100 / Line / Spline.

    Returns ``{chart, series, changed: list[str]}`` — empty list when the
    inputs match the existing values (no-op short-circuit, no save).
    """
    if series_type not in _VALID_SERIES_TYPES:
        raise ValueError(
            f"series_type {series_type!r} not valid; "
            f"expected one of {sorted(_VALID_SERIES_TYPES)}"
        )
    if series_subtype not in _VALID_SERIES_SUBTYPES:
        raise ValueError(
            f"series_subtype {series_subtype!r} not valid; "
            f"expected one of {sorted(_VALID_SERIES_SUBTYPES)}"
        )

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)
    series = _find_series(sc, series_name)

    type_node = find_child(series, "Type")
    subtype_node = find_child(series, "Subtype")
    changed: list[str] = []

    if type_node is None:
        type_node = etree.SubElement(series, q("Type"))
    if type_node.text != series_type:
        type_node.text = series_type
        changed.append("Type")

    if subtype_node is None:
        subtype_node = etree.SubElement(series, q("Subtype"))
    if subtype_node.text != series_subtype:
        subtype_node.text = series_subtype
        changed.append("Subtype")

    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "series": series_name,
        "kind": "ChartSeries",
        "changed": changed,
    }


# ---- set_chart_legend ----------------------------------------------------


def _resolve_chart_legend(
    chart: etree._Element, legend_name: str = "Default"
) -> etree._Element:
    """Resolve the named ``<ChartLegend>``. Raises ElementNotFoundError
    if the chart's <ChartLegends> block or the named legend is missing."""
    legends_root = find_child(chart, "ChartLegends")
    if legends_root is None:
        raise ElementNotFoundError(
            f"chart {chart.get('Name')!r} has no <ChartLegends>"
        )
    for legend in find_children(legends_root, "ChartLegend"):
        if legend.get("Name") == legend_name:
            return legend
    raise ElementNotFoundError(
        f"no ChartLegend named {legend_name!r} in chart {chart.get('Name')!r}"
    )


def _insert_legend_child_in_order(
    legend: etree._Element, new_child: etree._Element
) -> None:
    new_local = etree.QName(new_child).localname
    existing = find_child(legend, new_local)
    if existing is not None:
        legend.replace(existing, new_child)
        return
    if new_local in _CHART_LEGEND_CHILD_ORDER:
        new_idx = _CHART_LEGEND_CHILD_ORDER.index(new_local)
        for i, child in enumerate(list(legend)):
            local = etree.QName(child).localname
            if (
                local in _CHART_LEGEND_CHILD_ORDER
                and _CHART_LEGEND_CHILD_ORDER.index(local) > new_idx
            ):
                legend.insert(i, new_child)
                return
    legend.append(new_child)


def set_chart_legend(
    path: str,
    chart_name: str,
    legend_name: str = "Default",
    position: Optional[str] = None,
    visible: Optional[bool] = None,
) -> dict[str, Any]:
    """Configure the named chart legend.

    ``position`` ∈ TopLeft / TopCenter / TopRight / LeftTop / LeftCenter
    / LeftBottom / RightTop / RightCenter / RightBottom / BottomLeft /
    BottomCenter / BottomRight. ``visible=False`` writes ``<Hidden>true``;
    ``visible=True`` writes ``<Hidden>false`` (or removes the element).

    Returns ``{chart, legend, kind, changed: list[str]}`` — empty list
    when nothing was supplied or all values match existing (no save).
    """
    if position is not None and position not in _VALID_LEGEND_POSITIONS:
        raise ValueError(
            f"position {position!r} not valid; expected one of "
            f"{sorted(_VALID_LEGEND_POSITIONS)}"
        )

    if position is None and visible is None:
        return {
            "chart": chart_name,
            "legend": legend_name,
            "kind": "ChartLegend",
            "changed": [],
        }

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    legend = _resolve_chart_legend(chart, legend_name)

    changed: list[str] = []

    if position is not None:
        existing = find_child(legend, "Position")
        if existing is None or existing.text != position:
            new = etree.Element(q("Position"))
            new.text = position
            _insert_legend_child_in_order(legend, new)
            changed.append("Position")

    if visible is not None:
        # RDL semantic: <Hidden>true</Hidden> hides; absence/false shows.
        # Map visible=True → Hidden=false; visible=False → Hidden=true.
        hidden_value = "false" if visible else "true"
        existing = find_child(legend, "Hidden")
        if existing is None or existing.text != hidden_value:
            new = etree.Element(q("Hidden"))
            new.text = hidden_value
            _insert_legend_child_in_order(legend, new)
            changed.append("Hidden")

    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "legend": legend_name,
        "kind": "ChartLegend",
        "changed": changed,
    }


# ---- set_chart_data_labels ----------------------------------------------


def _ensure_series_data_label(series: etree._Element) -> etree._Element:
    """Locate or create ``<ChartSeries>/<ChartDataLabel>``.

    Per RDL XSD, ChartSeries child order: ChartDataPoints, Type, Subtype,
    EmptyPoints, Style, ChartItemInLegend, ChartDataLabel, ChartMarker,
    ChartEmptyPoints, LegendName, ... — ChartDataLabel comes after
    ChartItemInLegend / Style.
    """
    label = find_child(series, "ChartDataLabel")
    if label is not None:
        return label
    label = etree.Element(q("ChartDataLabel"))
    # Insert immediately after ChartItemInLegend or Style if present;
    # otherwise after Subtype (which is always present in our writes).
    for anchor_local in ("ChartItemInLegend", "Style", "Subtype", "Type"):
        anchor = find_child(series, anchor_local)
        if anchor is not None:
            anchor.addnext(label)
            return label
    series.append(label)
    return label


def _insert_data_label_child_in_order(
    label: etree._Element, new_child: etree._Element
) -> None:
    new_local = etree.QName(new_child).localname
    existing = find_child(label, new_local)
    if existing is not None:
        label.replace(existing, new_child)
        return
    if new_local in _CHART_DATA_LABEL_CHILD_ORDER:
        new_idx = _CHART_DATA_LABEL_CHILD_ORDER.index(new_local)
        for i, child in enumerate(list(label)):
            local = etree.QName(child).localname
            if (
                local in _CHART_DATA_LABEL_CHILD_ORDER
                and _CHART_DATA_LABEL_CHILD_ORDER.index(local) > new_idx
            ):
                label.insert(i, new_child)
                return
    label.append(new_child)


def set_chart_data_labels(
    path: str,
    chart_name: str,
    series_name: Optional[str] = None,
    visible: Optional[bool] = None,
    format: Optional[str] = None,  # noqa: A002
) -> dict[str, Any]:
    """Configure ``<ChartDataLabel>`` for one series or all series in
    a chart.

    When ``series_name`` is None, the change applies to every series.
    Otherwise only the named series is touched.

    ``visible=True`` writes ``<Visible>true</Visible>``; False writes
    false; None leaves the element unchanged.

    ``format`` writes a numeric/date format into the per-label
    ``<Style>/<Format>`` sub-element. Pass ``''`` to clear it.

    Returns ``{chart, series: list[str], kind, changed: list[str]}`` —
    ``series`` lists the affected series names (one or many);
    ``changed`` is the union of sub-element names touched.
    """
    if visible is None and format is None:
        return {
            "chart": chart_name,
            "series": [series_name] if series_name else [],
            "kind": "ChartDataLabel",
            "changed": [],
        }

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)
    if series_name is not None:
        targets = [_find_series(sc, series_name)]
    else:
        targets = find_children(sc, "ChartSeries")

    changed: set[str] = set()
    affected: list[str] = []

    for series in targets:
        label = _ensure_series_data_label(series)

        if visible is not None:
            new = etree.Element(q("Visible"))
            new.text = "true" if visible else "false"
            existing = find_child(label, "Visible")
            if existing is None or existing.text != new.text:
                _insert_data_label_child_in_order(label, new)
                changed.add("Visible")

        if format is not None:
            style = find_child(label, "Style")
            if style is None:
                style = etree.Element(q("Style"))
                _insert_data_label_child_in_order(label, style)
            fmt = find_child(style, "Format")
            if format == "":
                if fmt is not None:
                    style.remove(fmt)
                    changed.add("Style.Format")
            else:
                if fmt is None:
                    fmt = etree.SubElement(style, q("Format"))
                if fmt.text != encode_text(format):
                    fmt.text = encode_text(format)
                    changed.add("Style.Format")

        affected.append(series.get("Name"))

    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "series": affected,
        "kind": "ChartDataLabel",
        "changed": sorted(changed),
    }


# ---- set_chart_palette ----------------------------------------------------


# RDL Palette enum. The values match Report Builder's "Palette" dropdown.
_VALID_PALETTES = frozenset(
    {
        "Default",
        "EarthTones",
        "Excel",
        "GrayScale",
        "Light",
        "Pastel",
        "SemiTransparent",
        "Berry",
        "Chocolate",
        "Fire",
        "SeaGreen",
        "BrightPastel",
    }
)


# Per RDL XSD, the position of <Palette> inside <Chart>: it sits AFTER
# the chart-content blocks (ChartCategoryHierarchy, ChartSeriesHierarchy,
# ChartData, ChartAreas, ChartLegends, ChartTitles) but BEFORE the
# layout fields (DataSetName, Top, Left, ...). The ordering helper
# inserts before DataSetName when present.
def _set_chart_palette_text(chart: etree._Element, palette_text: str) -> bool:
    """Write or rewrite ``<Palette>`` on a chart. Returns True iff the
    text actually changed. Empty string ``""`` removes the element."""
    existing = find_child(chart, "Palette")
    if palette_text == "":
        if existing is not None:
            chart.remove(existing)
            return True
        return False
    if existing is not None:
        if existing.text == palette_text:
            return False
        existing.text = palette_text
        return True
    new = etree.Element(q("Palette"))
    new.text = palette_text
    # Insert before DataSetName (always present in our writes); fall
    # back to before the next layout child or append.
    for anchor_local in (
        "DataSetName",
        "Top",
        "Left",
        "Height",
        "Width",
        "Style",
    ):
        anchor = find_child(chart, anchor_local)
        if anchor is not None:
            anchor.addprevious(new)
            return True
    chart.append(new)
    return True


def set_chart_palette(
    path: str,
    chart_name: str,
    palette: str,
) -> dict[str, Any]:
    """Set the chart's ``<Palette>`` element.

    ``palette`` ∈ Default / EarthTones / Excel / GrayScale / Light /
    Pastel / SemiTransparent / Berry / Chocolate / Fire / SeaGreen /
    BrightPastel. Pass ``""`` to clear (Report Builder falls back to
    its default palette).

    Returns ``{chart, kind, changed: bool}``. False when the palette
    text was already what was requested (no save).
    """
    if palette != "" and palette not in _VALID_PALETTES:
        raise ValueError(
            f"palette {palette!r} not valid; expected one of "
            f"{sorted(_VALID_PALETTES)} or '' to clear"
        )

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    changed = _set_chart_palette_text(chart, palette)
    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "kind": "Chart",
        "changed": changed,
    }


# ---- set_series_color -----------------------------------------------------


def set_series_color(
    path: str,
    chart_name: str,
    series_name: str,
    color: str,
) -> dict[str, Any]:
    """Write ``<Color>`` into a named series's ``<Style>`` block.

    Overrides the chart palette for this single series. Pass ``""`` to
    clear the explicit color (the series falls back to the palette).

    The color value can be any RDL color string Report Builder accepts:
    a named color (``"Red"``), a hex string (``"#FF0000"``), or an
    expression (``"=IIf(...)"``).

    Returns ``{chart, series, kind, changed: bool}``.
    """
    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)
    series = _find_series(sc, series_name)

    style = find_child(series, "Style")
    if color == "":
        if style is None:
            return {
                "chart": chart_name,
                "series": series_name,
                "kind": "ChartSeries",
                "changed": False,
            }
        existing = find_child(style, "Color")
        if existing is None:
            return {
                "chart": chart_name,
                "series": series_name,
                "kind": "ChartSeries",
                "changed": False,
            }
        style.remove(existing)
        if len(list(style)) == 0:
            series.remove(style)
        doc.save()
        return {
            "chart": chart_name,
            "series": series_name,
            "kind": "ChartSeries",
            "changed": True,
        }

    if style is None:
        style = etree.Element(q("Style"))
        # ChartSeries child order: Type, Subtype, EmptyPoints, Style,
        # ChartItemInLegend, ChartDataLabel, ChartMarker, ...
        # Insert after Subtype (always present) — that places Style
        # in the right position relative to the children we emit.
        anchor = find_child(series, "Subtype")
        if anchor is None:
            anchor = find_child(series, "Type")
        if anchor is not None:
            anchor.addnext(style)
        else:
            series.append(style)

    encoded = encode_text(color)
    existing = find_child(style, "Color")
    if existing is not None:
        if existing.text == encoded:
            return {
                "chart": chart_name,
                "series": series_name,
                "kind": "ChartSeries",
                "changed": False,
            }
        existing.text = encoded
    else:
        new = etree.SubElement(style, q("Color"))
        new.text = encoded

    doc.save()
    return {
        "chart": chart_name,
        "series": series_name,
        "kind": "ChartSeries",
        "changed": True,
    }


# ---- set_chart_title -----------------------------------------------------


def _resolve_chart_title(
    chart: etree._Element, title_name: str = "Default"
) -> etree._Element:
    """Resolve ``<ChartTitle Name="...">``. Raises if missing."""
    titles_root = find_child(chart, "ChartTitles")
    if titles_root is None:
        raise ElementNotFoundError(
            f"chart {chart.get('Name')!r} has no <ChartTitles>"
        )
    for title in find_children(titles_root, "ChartTitle"):
        if title.get("Name") == title_name:
            return title
    raise ElementNotFoundError(
        f"no ChartTitle named {title_name!r} in chart {chart.get('Name')!r}"
    )


def set_chart_title(
    path: str,
    chart_name: str,
    text: str,
    title_name: str = "Default",
) -> dict[str, Any]:
    """Update ``<ChartTitle>/<Caption>``.

    ``text`` can be literal text or an ``=expression``. ``title_name``
    defaults to ``"Default"`` (the only title the template emits).

    Returns ``{chart, title, kind, changed: bool}``.
    """
    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    title = _resolve_chart_title(chart, title_name)

    encoded = encode_text(text)
    caption = find_child(title, "Caption")
    if caption is not None:
        if caption.text == encoded:
            return {
                "chart": chart_name,
                "title": title_name,
                "kind": "ChartTitle",
                "changed": False,
            }
        caption.text = encoded
    else:
        # Per RDL XSD, ChartTitle's Caption is the first child.
        new = etree.Element(q("Caption"))
        new.text = encoded
        title.insert(0, new)

    doc.save()
    return {
        "chart": chart_name,
        "title": title_name,
        "kind": "ChartTitle",
        "changed": True,
    }


__all__ = [
    "add_chart_series",
    "insert_chart_from_template",
    "remove_chart_series",
    "set_chart_axis",
    "set_chart_data_labels",
    "set_chart_legend",
    "set_chart_palette",
    "set_chart_series_type",
    "set_chart_title",
    "set_series_color",
]
