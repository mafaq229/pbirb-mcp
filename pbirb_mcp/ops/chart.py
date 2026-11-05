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

import copy
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import chart_shape, chart_types, names, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError, resolve_dataset
from pbirb_mcp.core.mutation import changed_by
from pbirb_mcp.core.xpath import XPATH_NS, find_child, find_children, q
from pbirb_mcp.ops.actions import _insert_in_chart_data_point_order
from pbirb_mcp.ops.body import _ensure_body_report_items, _resolve_body

# inputSchema descriptions for the series_type / series_subtype args of the
# series tools, generated from the enum tables so they cannot drift.
SERIES_TYPE_DOC = (
    f"RDL ChartSeries Type: {' / '.join(chart_types.SERIES_TYPES)}. "
    f"Shorthands (normalised): {', '.join(sorted(chart_types.TYPE_ALIASES))}."
)
SERIES_SUBTYPE_DOC = (
    f"Must suit the type — {chart_types.describe()}. "
    "Shorthands: SmoothLine/Spline -> Smooth, 100 -> PercentStacked."
)


def _resolve_chart(doc: RDLDocument, name: str) -> etree._Element:
    """Find a ``<Chart Name="...">`` anywhere in the report (body, header,
    footer, rectangle children). Raises ``ElementNotFoundError`` on miss
    and ``AmbiguousElementError`` on duplicate-name."""
    matches = list(
        doc.root.xpath(
            ".//*[local-name()='Chart' and @Name=$n]",
            namespaces=XPATH_NS,
            n=name,
        )
    )
    if not matches:
        raise ElementNotFoundError(f"no Chart named {name!r}")
    if len(matches) > 1:
        raise AmbiguousElementError(f"Chart name {name!r} matches {len(matches)} elements")
    return matches[0]


def _series_collection(chart: etree._Element) -> etree._Element:
    """Return the ``<ChartSeriesCollection>`` for ``chart``. Defensive: a
    well-formed chart always has one (the template emits it), but this
    raises a clear error if it's missing rather than NPEing."""
    cd = find_child(chart, "ChartData")
    if cd is None:
        raise ElementNotFoundError(f"chart {chart.get('Name')!r} has no <ChartData>")
    sc = find_child(cd, "ChartSeriesCollection")
    if sc is None:
        raise ElementNotFoundError(f"chart {chart.get('Name')!r} has no <ChartSeriesCollection>")
    return sc


def _series_names(series_collection: etree._Element) -> list[str]:
    return [s.get("Name") for s in find_children(series_collection, "ChartSeries")]


def _find_series(series_collection: etree._Element, series_name: str) -> etree._Element:
    for s in find_children(series_collection, "ChartSeries"):
        if s.get("Name") == series_name:
            return s
    raise ElementNotFoundError(
        f"no ChartSeries named {series_name!r} (present: {_series_names(series_collection)})"
    )


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
    names.validate_identifier(name, "chart")
    top, left, width, height = units.box(top, left, width, height)
    doc = RDLDocument.open(path)
    resolve_dataset(doc, dataset_name)
    registry = names.NameRegistry(doc)
    registry.ensure_unique(name)
    registry.claim(name)
    category_group = registry.unique(f"{name}_CategoryGroup")

    chart = etree.Element(q("Chart"), Name=name)

    # ChartCategoryHierarchy — single category group.
    cat_hier = etree.SubElement(chart, q("ChartCategoryHierarchy"))
    cat_members = etree.SubElement(cat_hier, q("ChartMembers"))
    cat_member = etree.SubElement(cat_members, q("ChartMember"))
    cat_group = etree.SubElement(cat_member, q("Group"), Name=category_group)
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
        "category_group": category_group,
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
        raise ValueError(f"axis must be one of {_VALID_AXIS_KINDS}; got {axis_kind!r}")
    chart_areas = find_child(chart, "ChartAreas")
    if chart_areas is None:
        raise ElementNotFoundError(f"chart {chart.get('Name')!r} has no <ChartAreas>")
    chart_area = find_child(chart_areas, "ChartArea")
    if chart_area is None:
        raise ElementNotFoundError(f"chart {chart.get('Name')!r} has no <ChartArea>")
    axes_local = "ChartCategoryAxes" if axis_kind == "Category" else "ChartValueAxes"
    axes_root = find_child(chart_area, axes_local)
    if axes_root is None:
        raise ElementNotFoundError(f"chart {chart.get('Name')!r} has no <{axes_local}>")
    for axis in find_children(axes_root, "ChartAxis"):
        if axis.get("Name") == axis_name:
            return axis
    raise ElementNotFoundError(
        f"no {axis_kind} axis named {axis_name!r} in chart {chart.get('Name')!r}"
    )


def _insert_axis_child_in_order(axis: etree._Element, new_child: etree._Element) -> None:
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
            if local in _CHART_AXIS_CHILD_ORDER and _CHART_AXIS_CHILD_ORDER.index(local) > new_idx:
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
        if format_value == "":
            return  # nothing to clear
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


def _set_axis_simple_text(axis: etree._Element, local: str, value: str) -> None:
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
        raise ValueError(f"axis must be one of {_VALID_AXIS_KINDS}; got {axis!r}")

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
    # ChartAxis/LogScale and Visible are BooleanLocalizableType — a
    # case-sensitive enum {True, False}. Lowercase 'true'/'false' is
    # rejected by RB with "true is not a valid value." This differs from
    # BooleanExpressionType used on most other ReportItem booleans.
    writes = (
        ("Title", title, _set_axis_title),
        ("Style.Format", format, _set_axis_format),
        ("Minimum", min, lambda a, v: _set_axis_simple_text(a, "Minimum", v)),
        ("Maximum", max, lambda a, v: _set_axis_simple_text(a, "Maximum", v)),
        (
            "LogScale",
            None if log_scale is None else ("True" if log_scale else "False"),
            lambda a, v: _set_axis_simple_text(a, "LogScale", v),
        ),
        ("Interval", interval, lambda a, v: _set_axis_simple_text(a, "Interval", v)),
        (
            "Visible",
            None if visible is None else ("True" if visible else "False"),
            lambda a, v: _set_axis_simple_text(a, "Visible", v),
        ),
    )
    for label, value, writer in writes:
        if value is not None and changed_by(axis_el, writer, axis_el, value):
            changed.append(label)

    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "axis": axis,
        "axis_name": axis_name,
        "kind": "ChartAxis",
        "changed": changed,
    }


# ---- series <-> ChartSeriesHierarchy bookkeeping -------------------------


def _static_series_member(label: str) -> etree._Element:
    """A static series ``<ChartMember>`` whose ``<Label>`` (the legend text)
    is ``label``. ``Label`` is mandatory on every ChartMember."""
    member = etree.Element(q("ChartMember"))
    etree.SubElement(member, q("Label")).text = encode_text(label)
    return member


def _ensure_series_members_root(chart: etree._Element) -> etree._Element:
    """Return the top-level ``ChartSeriesHierarchy/ChartMembers``, creating
    the hierarchy (right before ``<ChartData>``) when it is missing."""
    members_root = chart_shape.series_hierarchy_members(chart)
    if members_root is not None:
        return members_root
    hierarchy = find_child(chart, "ChartSeriesHierarchy")
    if hierarchy is None:
        hierarchy = etree.Element(q("ChartSeriesHierarchy"))
        anchor = find_child(chart, "ChartData")
        if anchor is not None:
            anchor.addprevious(hierarchy)
        else:
            chart.insert(0, hierarchy)
    return etree.SubElement(hierarchy, q("ChartMembers"))


def _sync_series_members(chart: etree._Element) -> list[str]:
    """Make the series hierarchy cover every ``ChartSeries`` before a
    series is added or removed.

    Series match leaf members by position, so a chart with fewer leaves
    than series is missing the members of its trailing series — the shape
    earlier versions of ``add_chart_series`` left behind (they appended a
    series but no member). Those trailing series get static members
    labelled with their names, which is what the fixed tool gives them.
    Returns the names of the series that got a member.

    A chart with MORE leaves than series cannot be repaired unambiguously
    (any leaf could be the stale one), so that raises ``ValueError``.
    """
    all_series = chart_shape.series_list(chart)
    leaves = chart_shape.series_leaf_members(chart)
    if len(leaves) > len(all_series):
        raise ValueError(
            f"chart {chart.get('Name')!r} has {len(leaves)} leaf ChartMember(s) in "
            f"ChartSeriesHierarchy but only {len(all_series)} ChartSeries; series are "
            "matched to members by position, so the stale member(s) cannot be "
            "identified automatically. Remove the extra ChartMember(s) first."
        )
    missing = all_series[len(leaves) :]
    if not missing:
        return []
    members_root = _ensure_series_members_root(chart)
    for series in missing:
        members_root.append(_static_series_member(series.get("Name")))
    return [s.get("Name") for s in missing]


def _remove_series_member(leaf: etree._Element) -> list[str]:
    """Remove leaf ``leaf`` from the series hierarchy and prune every
    ancestor ``ChartMembers`` / ``ChartMember`` left without children (an
    emptied parent member would otherwise turn into a leaf and take over
    some other series' position). Returns the names of the ``<Group>``
    elements removed with pruned ancestors."""
    pruned_groups: list[str] = []
    node = leaf
    while True:
        members_root = node.getparent()
        members_root.remove(node)
        if find_children(members_root, "ChartMember"):
            break
        owner = members_root.getparent()
        if owner is None or owner.tag != q("ChartMember"):
            # The hierarchy's top-level <ChartMembers>; callers never empty
            # it (a chart keeps at least one series).
            break
        owner.remove(members_root)
        group = find_child(owner, "Group")
        if group is not None and group.get("Name"):
            pruned_groups.append(group.get("Name"))
        node = owner
    return pruned_groups


def _remove_derived(derived: list[etree._Element]) -> None:
    """Drop ``ChartDerivedSeries`` elements, and their collection if emptied."""
    for d in derived:
        collection = d.getparent()
        collection.remove(d)
        if not find_children(collection, "ChartDerivedSeries"):
            collection.getparent().remove(collection)


# ---- add_chart_series -----------------------------------------------------


def _build_chart_series(
    series_name: str,
    value_field: str,
    series_type: str = "Column",
    series_subtype: str = "Plain",
    data_points: int = 1,
) -> etree._Element:
    """Construct a single ``<ChartSeries>`` with the canonical shape.

    Mirrors the structure :func:`insert_chart_from_template` builds for
    the initial series, so a chart with N series via this tool reads
    consistently via :func:`get_chart`. ``data_points`` is the number of
    ``<ChartDataPoint>`` to emit — one per category leaf member.
    """
    series = etree.Element(q("ChartSeries"), Name=series_name)
    points_root = etree.SubElement(series, q("ChartDataPoints"))
    for _ in range(max(data_points, 1)):
        data_point = etree.SubElement(points_root, q("ChartDataPoint"))
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
    series_subtype: Optional[str] = None,
) -> dict[str, Any]:
    """Append a new ``<ChartSeries>`` to the named chart, together with its
    ``ChartSeriesHierarchy`` member.

    ``series_type`` / ``series_subtype`` must be an RDL pair (see
    :mod:`pbirb_mcp.core.chart_types`); shorthands such as ``Pie`` or
    ``Bubble`` are normalised to ``Shape/Pie`` / ``Scatter/Bubble``.
    ``series_subtype=None`` (default) takes the type's default subtype
    (``Plain``; ``Pie`` for Shape). The result's ``type`` / ``subtype``
    are the canonical values written.

    The series is built with ``Y = Sum(Fields!<value_field>.Value)`` —
    same shape :func:`insert_chart_from_template` emits for the first
    series — and one ``<ChartDataPoint>`` per category leaf member. A
    static ``<ChartMember><Label>series_name</Label></ChartMember>`` is
    appended to the top-level series ``<ChartMembers>``: series and leaf
    members are matched by position, and the member's label is the
    legend text. When the existing top-level member is a dynamic series
    group, the new static member becomes its sibling — Report Builder's
    layout for mixed static + dynamic series.

    A chart whose hierarchy lacks the members of trailing series (left by
    earlier versions of this tool) is repaired first; the result's
    ``repaired_members`` lists those series.

    Refuses if ``series_name`` is already used by a series or a derived
    series of the chart.
    """
    series_type, series_subtype = chart_types.normalize_series_type(series_type, series_subtype)
    if series_subtype is None:
        series_subtype = chart_types.DEFAULT_SUBTYPE[series_type]

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)

    if series_name in _series_names(sc):
        raise ValueError(f"chart {chart_name!r} already has a series named {series_name!r}")
    if series_name in chart_shape.series_names(chart, include_derived=True):
        raise ValueError(
            f"chart {chart_name!r} already has a derived series named {series_name!r}; "
            "series and derived series share one name space"
        )

    repaired = _sync_series_members(chart)
    sc.append(
        _build_chart_series(
            series_name=series_name,
            value_field=value_field,
            series_type=series_type,
            series_subtype=series_subtype,
            data_points=len(chart_shape.category_leaf_members(chart)),
        )
    )
    _ensure_series_members_root(chart).append(_static_series_member(series_name))
    doc.save()
    return {
        "chart": chart_name,
        "name": series_name,
        "kind": "ChartSeries",
        "type": series_type,
        "subtype": series_subtype,
        "value_field": value_field,
        "repaired_members": repaired,
    }


# ---- remove_chart_series --------------------------------------------------


def remove_chart_series(
    path: str,
    chart_name: str,
    series_name: str,
    cascade: bool = False,
) -> dict[str, Any]:
    """Remove a ``<ChartSeries>`` by name, together with its leaf
    ``ChartSeriesHierarchy`` member.

    Any parent ``ChartMember`` left without children is pruned too (with
    its ``<Group>``, reported in ``pruned_groups``) — otherwise it would
    become a leaf and shift every later series onto the wrong label or
    group. The remaining series keep their own members.

    Refuses if removing it would leave the chart with zero series — RB
    renders a chart with no series as an empty box, which is rarely the
    user's intent. Use :func:`add_chart_series` first if you really want
    to swap series.

    A ``ChartDerivedSeries`` (moving average, ...) whose
    ``SourceChartSeriesName`` is this series would dangle: the call
    refuses, naming it, unless ``cascade=True``, which removes those
    derived series too (transitively; listed in ``removed_derived``).
    """
    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)
    if series_name not in _series_names(sc) and series_name in chart_shape.series_names(chart):
        raise ValueError(
            f"{series_name!r} is a derived series (ChartDerivedSeries) of chart "
            f"{chart_name!r}; remove_chart_series only removes regular series"
        )
    target = _find_series(sc, series_name)

    remaining = [s for s in find_children(sc, "ChartSeries") if s is not target]
    if not remaining:
        raise ValueError(
            f"refusing to remove the last series in chart {chart_name!r}; "
            "add another series first or remove the chart entirely "
            "via remove_body_item."
        )

    dependents = chart_shape.derived_dependents(chart, series_name)
    dependent_names = [chart_shape.derived_series_name(d) for d in dependents]
    if dependents and not cascade:
        raise ValueError(
            f"series {series_name!r} is the source (SourceChartSeriesName) of derived "
            f"series {dependent_names} in chart {chart_name!r}; removing it would leave "
            "a dangling reference. Pass cascade=True to remove the derived series too."
        )

    repaired = _sync_series_members(chart)
    leaf = chart_shape.series_member(chart, target)
    pruned_groups = _remove_series_member(leaf) if leaf is not None else []
    sc.remove(target)
    _remove_derived(dependents)
    doc.save()
    return {
        "chart": chart_name,
        "removed": series_name,
        "remaining": [s.get("Name") for s in remaining],
        "removed_derived": dependent_names,
        "pruned_groups": pruned_groups,
        "repaired_members": repaired,
    }


# ---- set_chart_series_type ------------------------------------------------


def set_chart_series_type(
    path: str,
    chart_name: str,
    series_name: str,
    series_type: str,
    series_subtype: Optional[str] = None,
) -> dict[str, Any]:
    """Update the ``<Type>`` and optionally ``<Subtype>`` of a named series.

    Combo-chart pattern: the chart can hold series of mixed types — a
    Bar series + a Line series in the same chart renders as a combo
    bar+line. Set each series's type independently with this tool.

    ``series_type`` ∈ Column / Bar / Line / Area / Shape / Scatter /
    Range / Polar, and ``series_subtype`` must suit it:

    * Column, Bar: Plain / Stacked / PercentStacked
    * Line: Plain / Smooth / Stepped
    * Area: Plain / Smooth / Stacked / PercentStacked
    * Shape: Pie / ExplodedPie / Doughnut / ExplodedDoughnut / Funnel /
      Pyramid / TreeMap / Sunburst
    * Scatter: Plain / Bubble
    * Range: Plain / Smooth / Column / Bar / Stock / Candlestick /
      BoxPlot / ErrorBar
    * Polar: Plain / Radar

    Shorthands are normalised before writing (``Pie`` → ``Shape/Pie``,
    ``Bubble`` → ``Scatter/Bubble``, ``Radar`` → ``Polar/Radar``,
    ``Stock`` → ``Range/Stock``, ``SmoothLine`` → ``Line/Smooth``, subtype
    ``100`` → ``PercentStacked`` …; see
    :data:`pbirb_mcp.core.chart_types.TYPE_ALIASES`).

    ``series_subtype=None`` (default) keeps the stored subtype when it
    suits the new type (Bar/Stacked → Column keeps Stacked). When it does
    not (Bar/Stacked → Shape), it is reset to the new type's default
    (``Plain``; ``Pie`` for Shape) and ``"Subtype"`` is reported in
    ``changed``.

    Returns ``{chart, series, kind, type, subtype, changed: list[str]}`` —
    ``type`` / ``subtype`` are the canonical values now on the series;
    ``changed`` is empty when they already matched (no save).
    """
    series_type, series_subtype = chart_types.normalize_series_type(series_type, series_subtype)

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)
    series = _find_series(sc, series_name)

    type_node = find_child(series, "Type")
    subtype_node = find_child(series, "Subtype")

    if series_subtype is None:
        stored = subtype_node.text if subtype_node is not None else None
        effective = chart_types.RDL_DEFAULT_SUBTYPE if stored is None else stored.strip()
        effective = chart_types.SUBTYPE_ALIASES.get(effective, effective)
        valid = chart_types.SUBTYPES_BY_TYPE[series_type]
        if effective not in valid:
            series_subtype = chart_types.DEFAULT_SUBTYPE[series_type]
        elif stored is not None and stored != effective:
            # A legacy shorthand on disk (e.g. 'SmoothLine') that maps to a
            # subtype of the new type: write its canonical form.
            series_subtype = effective

    changed: list[str] = []

    if type_node is None:
        type_node = etree.Element(q("Type"))
        anchor = find_child(series, "ChartDataPoints")
        if anchor is not None:
            anchor.addnext(type_node)
        else:
            series.insert(0, type_node)
    if type_node.text != series_type:
        type_node.text = series_type
        changed.append("Type")

    if series_subtype is not None:
        if subtype_node is None:
            subtype_node = etree.Element(q("Subtype"))
            type_node.addnext(subtype_node)
        if subtype_node.text != series_subtype:
            subtype_node.text = series_subtype
            changed.append("Subtype")

    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "series": series_name,
        "kind": "ChartSeries",
        "type": series_type,
        "subtype": subtype_node.text if subtype_node is not None else None,
        "changed": changed,
    }


# ---- set_chart_legend ----------------------------------------------------


def _resolve_chart_legend(chart: etree._Element, legend_name: str = "Default") -> etree._Element:
    """Resolve the named ``<ChartLegend>``. Raises ElementNotFoundError
    if the chart's <ChartLegends> block or the named legend is missing."""
    legends_root = find_child(chart, "ChartLegends")
    if legends_root is None:
        raise ElementNotFoundError(f"chart {chart.get('Name')!r} has no <ChartLegends>")
    for legend in find_children(legends_root, "ChartLegend"):
        if legend.get("Name") == legend_name:
            return legend
    raise ElementNotFoundError(
        f"no ChartLegend named {legend_name!r} in chart {chart.get('Name')!r}"
    )


def _insert_legend_child_in_order(legend: etree._Element, new_child: etree._Element) -> None:
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
            f"position {position!r} not valid; expected one of {sorted(_VALID_LEGEND_POSITIONS)}"
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
    """Locate or create ``<ChartSeries>/<ChartDataLabel>`` — only used for a
    series without any ``<ChartDataPoint>`` (labels normally live on the
    data points, see :func:`set_chart_data_labels`).

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


def _insert_data_label_child_in_order(label: etree._Element, new_child: etree._Element) -> None:
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


# RDL ChartDataLabel Position values. (TopCenter / BottomCenter are
# ChartLegend positions, not data-label ones.)
DATA_LABEL_POSITIONS: tuple[str, ...] = (
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
_VALID_DATA_LABEL_POSITIONS = frozenset(DATA_LABEL_POSITIONS)


def _data_label_hosts(
    series: etree._Element,
) -> tuple[list[tuple[etree._Element, Optional[etree._Element]]], bool]:
    """``(label, host)`` pairs to edit for one series, and whether a
    series-level label was moved onto the data points.

    Report Builder keeps data labels on the data points
    (``ChartDataPoint/ChartDataLabel``), and a point's label overrides
    ``ChartSeries/ChartDataLabel`` at render time, so edits go to every
    data point (a chart with one category member has one: the template
    point). A point without a label gets a copy of the series-level label
    when there is one (which is then removed — the render is unchanged,
    and the edit cannot be shadowed), else a new label. ``host`` is the
    data point a new label still has to be attached to (None when the
    label is already in the tree), so a call that leaves it empty never
    adds an empty element. A series without data points keeps the
    series-level label.
    """
    points = chart_shape.data_points(series)
    if not points:
        return [(_ensure_series_data_label(series), None)], False
    series_label = find_child(series, "ChartDataLabel")
    hosts: list[tuple[etree._Element, Optional[etree._Element]]] = []
    for point in points:
        label = find_child(point, "ChartDataLabel")
        if label is not None:
            hosts.append((label, None))
        elif series_label is not None:
            moved = copy.deepcopy(series_label)
            moved.tail = None
            _insert_in_chart_data_point_order(point, moved)
            hosts.append((moved, None))
        else:
            hosts.append((etree.Element(q("ChartDataLabel")), point))
    if series_label is not None:
        series.remove(series_label)
    return hosts, series_label is not None


def _edit_data_label(
    label: etree._Element,
    *,
    visible: Optional[bool],
    visible_expression: Optional[str],
    format: Optional[str],  # noqa: A002
    position: Optional[str],
    use_value_as_label: Optional[bool],
    font_weight: Optional[str],
    color: Optional[str],
) -> set[str]:
    """Apply the requested values to one ``ChartDataLabel``; return the
    names of the sub-elements that changed."""
    changed: set[str] = set()

    # ----- Visible (boolean OR expression) ----------------------
    if visible is not None or visible_expression is not None:
        new_text = (
            "true"
            if visible is True
            else "false"
            if visible is False
            else encode_text(visible_expression)
        )
        existing = find_child(label, "Visible")
        if existing is None or existing.text != new_text:
            new = etree.Element(q("Visible"))
            new.text = new_text
            _insert_data_label_child_in_order(label, new)
            changed.add("Visible")

    # ----- UseValueAsLabel (boolean) ----------------------------
    if use_value_as_label is not None:
        text = "true" if use_value_as_label else "false"
        existing = find_child(label, "UseValueAsLabel")
        if existing is None or existing.text != text:
            uval = etree.Element(q("UseValueAsLabel"))
            uval.text = text
            _insert_data_label_child_in_order(label, uval)
            changed.add("UseValueAsLabel")

    # ----- Position (enum) --------------------------------------
    if position is not None:
        existing = find_child(label, "Position")
        if existing is None or existing.text != position:
            pos = etree.Element(q("Position"))
            pos.text = position
            _insert_data_label_child_in_order(label, pos)
            changed.add("Position")

    # ----- Style.{Format, FontWeight, Color} --------------------
    style_values = [
        (local, value)
        for local, value in (("Format", format), ("FontWeight", font_weight), ("Color", color))
        if value is not None
    ]
    style = find_child(label, "Style")
    if style is None and any(value != "" for _, value in style_values):
        style = etree.Element(q("Style"))
        _insert_data_label_child_in_order(label, style)
    if style is not None:
        for child_local, value in style_values:
            existing = find_child(style, child_local)
            if value == "":
                if existing is not None:
                    style.remove(existing)
                    changed.add(f"Style.{child_local}")
            else:
                if existing is None:
                    existing = etree.SubElement(style, q(child_local))
                if existing.text != encode_text(value):
                    existing.text = encode_text(value)
                    changed.add(f"Style.{child_local}")
    return changed


def set_chart_data_labels(
    path: str,
    chart_name: str,
    series_name: Optional[str] = None,
    visible: Optional[bool] = None,
    format: Optional[str] = None,  # noqa: A002
    visible_expression: Optional[str] = None,
    position: Optional[str] = None,
    use_value_as_label: Optional[bool] = None,
    font_weight: Optional[str] = None,
    color: Optional[str] = None,
) -> dict[str, Any]:
    """Configure the data labels of one series or all series in a chart.

    Labels are written where Report Builder keeps them and where they
    render: ``ChartSeries/ChartDataPoints/ChartDataPoint/ChartDataLabel``
    (every data point of the series — one, the template point, unless the
    category hierarchy has several static members). A series-level
    ``ChartSeries/ChartDataLabel`` (written by earlier versions) is moved
    onto the points that have none, since a point's label overrides it.

    When ``series_name`` is None, the change applies to every series.

    ``visible=True`` writes ``<Visible>true</Visible>``; False writes
    false; None leaves the element unchanged. ``visible_expression``
    writes the same element with a VB.NET ``=IIf(...)``-style
    expression — mutually exclusive with ``visible``.

    ``format`` / ``font_weight`` / ``color`` write ``<Style>/<Format>``,
    ``<Style>/<FontWeight>`` and ``<Style>/<Color>``; pass ``''`` to clear.
    ``position`` ∈ :data:`DATA_LABEL_POSITIONS` (``Outside`` is meant for
    Shape series — pie, doughnut, funnel, pyramid — and draws a warning on
    other types). ``use_value_as_label`` writes ``<UseValueAsLabel>``.

    Returns ``{chart, series: list[str], kind, changed: list[str],
    warnings}`` — ``changed`` is the union of sub-element names touched
    (``ChartDataLabel`` when a series-level label was moved to the data
    points); empty when everything already matched (file untouched).
    """
    if visible is not None and visible_expression is not None:
        raise ValueError("visible and visible_expression are mutually exclusive — pass only one")
    if position is not None and position not in _VALID_DATA_LABEL_POSITIONS:
        raise ValueError(
            f"position {position!r} not valid; expected one of {list(DATA_LABEL_POSITIONS)}"
            + (
                " (TopCenter/BottomCenter are legend positions; use Top/Bottom)"
                if position in ("TopCenter", "BottomCenter")
                else ""
            )
        )

    no_op = all(
        v is None
        for v in (
            visible,
            format,
            visible_expression,
            position,
            use_value_as_label,
            font_weight,
            color,
        )
    )
    if no_op:
        return {
            "chart": chart_name,
            "series": [series_name] if series_name else [],
            "kind": "ChartDataLabel",
            "changed": [],
            "warnings": [],
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
    warnings: list[str] = []

    for series in targets:
        hosts, migrated = _data_label_hosts(series)
        if migrated:
            changed.add("ChartDataLabel")
        for label, host in hosts:
            touched = _edit_data_label(
                label,
                visible=visible,
                visible_expression=visible_expression,
                format=format,
                position=position,
                use_value_as_label=use_value_as_label,
                font_weight=font_weight,
                color=color,
            )
            if host is not None and len(label):
                _insert_in_chart_data_point_order(host, label)
            changed |= touched
        type_node = find_child(series, "Type")
        series_type = (type_node.text or "").strip() if type_node is not None else "Column"
        if position == "Outside" and series_type != "Shape":
            warnings.append(
                f"series {series.get('Name')!r} is a {series_type} series: Position "
                "'Outside' is meant for Shape series (pie, doughnut, funnel, pyramid); "
                "Report Builder may ignore it here"
            )
        affected.append(series.get("Name"))

    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "series": affected,
        "kind": "ChartDataLabel",
        "changed": sorted(changed),
        "warnings": warnings,
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


def _resolve_chart_title(chart: etree._Element, title_name: str = "Default") -> etree._Element:
    """Resolve ``<ChartTitle Name="...">``. Raises if missing."""
    titles_root = find_child(chart, "ChartTitles")
    if titles_root is None:
        raise ElementNotFoundError(f"chart {chart.get('Name')!r} has no <ChartTitles>")
    for title in find_children(titles_root, "ChartTitle"):
        if title.get("Name") == title_name:
            return title
    raise ElementNotFoundError(f"no ChartTitle named {title_name!r} in chart {chart.get('Name')!r}")


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


# ---- set_chart_series_grouping (#77) --------------------------------------


# Report Builder's child order inside a <ChartMember> (the XSD is a choice).
_CHART_MEMBER_CHILD_ORDER = (
    "Group",
    "SortExpressions",
    "ChartMembers",
    "Label",
    "CustomProperties",
    "DataElementName",
    "DataElementOutput",
)

_SERIES_SORT_DIRECTIONS = ("Ascending", "Descending", "None")


def _insert_member_child(member: etree._Element, node: etree._Element) -> None:
    """Insert ``node`` into a ChartMember in Report Builder's child order."""
    local = etree.QName(node).localname
    idx = _CHART_MEMBER_CHILD_ORDER.index(local)
    for i, child in enumerate(member):
        if not isinstance(child.tag, str):
            continue
        child_local = etree.QName(child).localname
        if (
            child_local in _CHART_MEMBER_CHILD_ORDER
            and _CHART_MEMBER_CHILD_ORDER.index(child_local) > idx
        ):
            member.insert(i, node)
            return
    member.append(node)


def _group_expression_texts(group: etree._Element) -> list[Optional[str]]:
    exprs = find_child(group, "GroupExpressions")
    if exprs is None:
        return []
    return [e.text for e in find_children(exprs, "GroupExpression")]


def _sort_state(member: etree._Element) -> list[tuple[Optional[str], str]]:
    """``(Value, Direction)`` per SortExpression of a ChartMember (a missing
    Direction is Ascending, the RDL default)."""
    sorts = find_child(member, "SortExpressions")
    if sorts is None:
        return []
    out: list[tuple[Optional[str], str]] = []
    for s in find_children(sorts, "SortExpression"):
        value = find_child(s, "Value")
        direction = find_child(s, "Direction")
        text = (direction.text or "").strip() if direction is not None else ""
        out.append((value.text if value is not None else None, text or "Ascending"))
    return out


def _write_sort(member: etree._Element, expression: str, direction: str) -> bool:
    """Make the member's SortExpressions exactly one SortExpression on
    ``expression`` (``direction`` 'None' removes them). True iff changed."""
    wanted = [] if direction == "None" else [(expression, direction)]
    if _sort_state(member) == wanted:
        return False
    existing = find_child(member, "SortExpressions")
    if existing is not None:
        member.remove(existing)
    if wanted:
        sorts = etree.Element(q("SortExpressions"))
        sort = etree.SubElement(sorts, q("SortExpression"))
        etree.SubElement(sort, q("Value")).text = expression
        if direction == "Descending":
            etree.SubElement(sort, q("Direction")).text = "Descending"
        _insert_member_child(member, sorts)
    return True


def set_chart_series_grouping(
    path: str,
    chart_name: str,
    series_name: str,
    group_field: Optional[str] = None,
    group_expression: Optional[str] = None,
    replace: bool = False,
    label_expression: Optional[str] = None,
    sort: Optional[str] = None,
    group_name: Optional[str] = None,
) -> dict[str, Any]:
    """Make a chart series dynamic: one rendered series per distinct value
    of the group expression (``<ChartMember>/<Group>/<GroupExpressions>``).

    The member is the series' own leaf in ``ChartSeriesHierarchy`` (series
    and leaves match by position, :mod:`pbirb_mcp.core.chart_shape`), or
    the dynamic member it already sits under. Trailing series without a
    member (older files) get one first (``repaired_members``).

    ``group_field`` is a shorthand: ``"Type"`` becomes
    ``=Fields!Type.Value``; ``group_expression`` takes any VB.NET.
    Mutually exclusive.

    Promoting a static member writes the Report Builder shape: the
    ``<Label>`` (legend text of each fanned-out series) becomes the group
    expression, or ``label_expression``; ``<SortExpressions>`` sorts by the
    group expression (``sort``: Ascending (default) / Descending / None);
    the group is named ``group_name`` or ``<series>_Group``.

    An existing group is refused unless ``replace=True``, which rewrites
    only its ``GroupExpressions`` and keeps ``Group/@Name`` (scope
    references such as ``Sum(..., "SeriesGroup1")`` keep working) and the
    group's other children unless ``group_name`` renames it. The label is
    updated when ``label_expression`` is given, or when it was a constant
    or the old group expression; a sort on the old group expression
    follows the new one (``sort`` sets it explicitly). A call that matches
    the current state changes nothing (``changed: []``, file untouched),
    with or without ``replace``.

    Returns ``{chart, series, kind: 'ChartGroup', group_name, expression,
    label, sort, repaired_members, changed}``.
    """
    if group_field is None and group_expression is None:
        raise ValueError(
            "set_chart_series_grouping requires either group_field or group_expression"
        )
    if group_field is not None and group_expression is not None:
        raise ValueError(
            "set_chart_series_grouping: group_field and group_expression "
            "are mutually exclusive — pass only one"
        )
    if sort is not None and sort not in _SERIES_SORT_DIRECTIONS:
        raise ValueError(
            f"sort {sort!r} not valid; expected one of {list(_SERIES_SORT_DIRECTIONS)}"
        )
    if group_name is not None:
        names.validate_identifier(group_name, "group")

    effective_expression = encode_text(
        f"=Fields!{group_field}.Value" if group_field is not None else group_expression
    )
    label_text = encode_text(label_expression) if label_expression is not None else None

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    if chart_shape.series_index(chart, series_name) is None:
        raise ElementNotFoundError(
            f"chart {chart_name!r} has no series named {series_name!r}; "
            f"existing series: {chart_shape.series_names(chart, include_derived=False)!r}"
        )

    # Series match hierarchy leaves by position: give trailing series of
    # an older file their member first (raises when there are extra
    # leaves, where the mapping is ambiguous).
    repaired = _sync_series_members(chart)
    member = chart_shape.series_group_member(chart, series_name)
    if member is None:  # pragma: no cover - _sync_series_members guarantees one
        raise ValueError(f"chart {chart_name!r}: series {series_name!r} has no ChartMember")

    changed: list[str] = ["ChartSeriesHierarchy"] if repaired else []
    group = find_child(member, "Group")
    label = find_child(member, "Label")
    old_label = label.text if label is not None else None

    if group is None:
        new_label = label_text if label_text is not None else effective_expression
        name = group_name or names.unique_name(doc, f"{series_name}_Group")
        if group_name is not None:
            names.ensure_unique(doc, group_name)
        group = etree.Element(q("Group"), Name=name)
        expressions = etree.SubElement(group, q("GroupExpressions"))
        etree.SubElement(expressions, q("GroupExpression")).text = effective_expression
        _insert_member_child(member, group)
        changed.append("Group")
        if _write_sort(member, effective_expression, sort or "Ascending"):
            changed.append("SortExpressions")
    else:
        old_exprs = _group_expression_texts(group)
        old_expr = old_exprs[0] if len(old_exprs) == 1 else None
        name = group.get("Name")
        rename = group_name is not None and group_name != name
        exprs_differ = old_exprs != [effective_expression]

        if label_text is not None:
            new_label = label_text
        elif (
            old_label is None
            or not old_label.lstrip().startswith("=")
            or (old_expr is not None and old_label == old_expr)
        ):
            # A constant label gives every fanned-out series the same
            # legend text; one that mirrors the group expression follows it.
            new_label = effective_expression
        else:
            new_label = old_label  # a hand-written label expression

        old_sort = _sort_state(member)
        direction: Optional[str]
        if sort is not None:
            direction = sort
        elif (
            old_expr is not None
            and len(old_sort) == 1
            and old_sort[0][0] == old_expr
            and exprs_differ
        ):
            direction = old_sort[0][1]  # the sort followed the group expression
        else:
            direction = None  # leave SortExpressions alone

        wanted_sort = (
            old_sort
            if direction is None
            else ([] if direction == "None" else [(effective_expression, direction)])
        )
        if not (rename or exprs_differ or new_label != old_label or wanted_sort != old_sort):
            if repaired:
                doc.save()
            return _grouping_result(
                chart_name, series_name, name, effective_expression, member, repaired, changed
            )
        if not replace:
            raise ValueError(
                f"chart {chart_name!r}: the ChartMember of series {series_name!r} already "
                f"has a <Group> ({name!r} on {old_exprs!r}); pass replace=True to "
                "rewrite its expression (the group name is kept)."
            )
        if rename:
            names.ensure_unique(doc, group_name)
            group.set("Name", group_name)
            name = group_name
            changed.append("Group.Name")
        if exprs_differ:
            expressions = find_child(group, "GroupExpressions")
            if expressions is None:
                expressions = etree.Element(q("GroupExpressions"))
                group.insert(0, expressions)
            for old in list(expressions):
                expressions.remove(old)
            etree.SubElement(expressions, q("GroupExpression")).text = effective_expression
            changed.append("GroupExpressions")
        if direction is not None and _write_sort(member, effective_expression, direction):
            changed.append("SortExpressions")

    if label is None:
        label = etree.Element(q("Label"))
        _insert_member_child(member, label)
    if label.text != new_label:
        label.text = new_label
        changed.append("Label")

    doc.save()
    return _grouping_result(
        chart_name, series_name, name, effective_expression, member, repaired, changed
    )


def _grouping_result(
    chart_name: str,
    series_name: str,
    group_name: Optional[str],
    expression: str,
    member: etree._Element,
    repaired: list[str],
    changed: list[str],
) -> dict[str, Any]:
    label = find_child(member, "Label")
    sorts = _sort_state(member)
    return {
        "chart": chart_name,
        "series": series_name,
        "kind": "ChartGroup",
        "group_name": group_name,
        "expression": expression,
        "label": label.text if label is not None else None,
        "sort": [{"value": v, "direction": d} for v, d in sorts],
        "repaired_members": repaired,
        "changed": sorted(changed),
    }


__all__ = [
    "add_chart_series",
    "insert_chart_from_template",
    "remove_chart_series",
    "set_chart_axis",
    "set_chart_data_labels",
    "set_chart_legend",
    "set_chart_palette",
    "set_chart_series_grouping",
    "set_chart_series_type",
    "set_chart_title",
    "set_series_color",
]
