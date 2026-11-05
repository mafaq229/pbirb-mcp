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
import re
from typing import Any, Optional, Union

from lxml import etree

from pbirb_mcp.core import chart_shape, chart_types, mutation, names, style_values, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError, resolve_dataset
from pbirb_mcp.core.mutation import (
    ChangeTracker,
    changed_by,
    remove_child_if_present,
    set_node_text,
    set_text_if_different,
)
from pbirb_mcp.core.xpath import XPATH_NS, find_child, find_children, q
from pbirb_mcp.ops.actions import _insert_in_chart_data_point_order
from pbirb_mcp.ops.containers import place

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


# ---- input validation (#101) ----------------------------------------------


def _dataset_field_names(dataset: etree._Element) -> list[str]:
    """``Fields/Field/@Name`` of a ``<DataSet>``, in order."""
    fields = find_child(dataset, "Fields")
    if fields is None:
        return []
    return [f.get("Name") for f in find_children(fields, "Field") if f.get("Name")]


def _chart_dataset_fields(
    doc: RDLDocument, chart: etree._Element
) -> tuple[Optional[str], Optional[list[str]]]:
    """``(dataset name, its field names)`` for a chart: its own
    ``DataSetName``, else the nearest enclosing data region's (a chart in a
    tablix cell). Fields are None when no dataset can be resolved — the
    caller then skips the field-existence check."""
    node: Optional[etree._Element] = chart
    while node is not None:
        ds = find_child(node, "DataSetName")
        if ds is not None and (ds.text or "").strip():
            name = ds.text.strip()
            try:
                return name, _dataset_field_names(resolve_dataset(doc, name))
            except (ElementNotFoundError, AmbiguousElementError):
                return name, None
        node = node.getparent()
    return None, None


def _check_field_name(
    value: object,
    param: str,
    *,
    expression_param: Optional[str] = None,
) -> str:
    """A ``*_field`` argument must be a bare field name: not empty, not an
    expression (``expression_param`` takes those), a valid identifier."""
    hint = f"; pass an expression as {expression_param}" if expression_param else ""
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{param}: a field name is required; got {value!r}")
    if value.lstrip().startswith("="):
        raise ValueError(f"{param}: {value!r} is an expression, not a field name{hint}")
    if not names.is_identifier(value):
        raise ValueError(
            f"{param}: {value!r} is not a valid field name (RDL field names start with a "
            f"letter and contain only letters, digits and underscores){hint}"
        )
    return value


def _check_field_exists(
    value: str, param: str, dataset_name: Optional[str], available: Optional[list[str]]
) -> None:
    if available is not None and value not in available:
        raise ValueError(
            f"{param}: dataset {dataset_name!r} has no field {value!r}; available fields: "
            f"{available}"
        )


def _check_expression(value: object, param: str, hint: str = "") -> str:
    """An ``*_expression`` argument must be an RDL ``=expression``."""
    if not isinstance(value, str) or not value.startswith("="):
        raise ValueError(
            f"{param}: must be an =expression (e.g. '=Fields!Region.Value'); got {value!r}"
            + (f"; {hint}" if hint else "")
        )
    return value


def _field_or_expression(
    field: Optional[str],
    expression: Optional[str],
    field_param: str,
    expression_param: str,
) -> None:
    if field is not None and expression is not None:
        raise ValueError(f"{field_param} and {expression_param} are mutually exclusive")
    if field is None and expression is None:
        raise ValueError(f"{field_param} (or {expression_param}) is required")
    if field is not None:
        _check_field_name(field, field_param, expression_param=expression_param)
    else:
        _check_expression(expression, expression_param, f"for a plain field use {field_param}")


# ---- insert_chart_from_template -------------------------------------------


# Name of the series a chart gets when its value is an expression.
_EXPRESSION_SERIES_NAME = "Series1"


def insert_chart_from_template(
    path: str,
    name: str,
    dataset_name: str,
    category_field: Optional[str] = None,
    value_field: Optional[str] = None,
    top: Optional[str] = None,
    left: Optional[str] = None,
    width: Optional[str] = None,
    height: Optional[str] = None,
    container: Optional[str] = None,
    category_expression: Optional[str] = None,
    value_expression: Optional[str] = None,
) -> dict[str, Any]:
    """Insert a basic Column chart bound to ``dataset_name``.

    The chart has one category axis (grouped by ``category_field``) and
    one Y series (``=Sum(Fields!<value_field>.Value)``). Type defaults to
    Column; callers can change it post-insert via :func:`set_chart_series_type`.
    With ``container`` it goes inside that body Rectangle (#83; a List's
    only when the list is bound to ``dataset_name``).

    ``category_field`` / ``value_field`` must be fields of the dataset
    (an unknown one is refused, listing the available fields).
    ``category_expression`` (group + label expression) and
    ``value_expression`` (the whole Y expression, e.g.
    ``=Sum(Fields!A.Value) / Sum(Fields!B.Value)``; the series is then
    named ``Series1``) take ``=expressions`` instead — each is exclusive
    with its ``*_field``. ``top`` / ``left`` / ``width`` / ``height`` are
    required RDL sizes.
    """
    names.validate_identifier(name, "chart")
    _field_or_expression(
        category_field, category_expression, "category_field", "category_expression"
    )
    _field_or_expression(value_field, value_expression, "value_field", "value_expression")
    top, left, width, height = units.box(top, left, width, height)
    doc = RDLDocument.open(path)
    available = _dataset_field_names(resolve_dataset(doc, dataset_name))
    if category_field is not None:
        _check_field_exists(category_field, "category_field", dataset_name, available)
    if value_field is not None:
        _check_field_exists(value_field, "value_field", dataset_name, available)
    category_text = encode_text(
        category_expression
        if category_expression is not None
        else f"=Fields!{category_field}.Value"
    )
    y_text = encode_text(
        value_expression if value_expression is not None else f"=Sum(Fields!{value_field}.Value)"
    )
    series_name = value_field if value_field is not None else _EXPRESSION_SERIES_NAME
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
    cat_expression.text = category_text
    cat_label = etree.SubElement(cat_member, q("Label"))
    cat_label.text = category_text

    # ChartSeriesHierarchy — single static series. Per RDL XSD a ChartMember
    # is required to have a <Label>; an empty member fails Report Builder's
    # deserializer with "ChartMember is empty ... missing mandatory child
    # element of type 'Label'", even though it parses as well-formed XML.
    series_hier = etree.SubElement(chart, q("ChartSeriesHierarchy"))
    series_members = etree.SubElement(series_hier, q("ChartMembers"))
    series_member = etree.SubElement(series_members, q("ChartMember"))
    etree.SubElement(series_member, q("Label")).text = encode_text(series_name)

    # ChartData — one ChartSeries with Y = Sum(value_field).
    chart_data = etree.SubElement(chart, q("ChartData"))
    series_collection = etree.SubElement(chart_data, q("ChartSeriesCollection"))
    series = etree.SubElement(series_collection, q("ChartSeries"), Name=series_name)
    data_points = etree.SubElement(series, q("ChartDataPoints"))
    data_point = etree.SubElement(data_points, q("ChartDataPoint"))
    data_point_values = etree.SubElement(data_point, q("ChartDataPointValues"))
    y_node = etree.SubElement(data_point_values, q("Y"))
    y_node.text = y_text
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

    result = {
        "name": name,
        "kind": "Chart",
        "type": "Column",
        "category_field": category_field,
        "value_field": value_field,
        "series": series_name,
        "category_group": category_group,
    }
    return place(doc, chart, container, result, dataset_name=dataset_name)


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


def _area_axes(area: etree._Element, axis_kind: str) -> list[etree._Element]:
    axes_local = "ChartCategoryAxes" if axis_kind == "Category" else "ChartValueAxes"
    axes_root = find_child(area, axes_local)
    return find_children(axes_root, "ChartAxis") if axes_root is not None else []


def _resolve_chart_axis(
    chart: etree._Element,
    axis_kind: str,
    axis_name: Optional[str] = None,
    chart_area: Optional[str] = None,
) -> tuple[etree._Element, etree._Element]:
    """Resolve a ``<ChartAxis>`` of kind ``axis_kind``; return ``(area, axis)``.

    ``chart_area`` names the ``ChartArea`` (default: the first one).
    ``axis_name`` names the axis; omitted, it is ``Primary`` when the area
    has one, else the area's only axis of that kind. With ``chart_area``
    omitted, a named axis missing from the first area but present in
    exactly one other area resolves there. Misses raise
    ``ElementNotFoundError`` listing the ``area/axis`` pairs present.
    """
    if axis_kind not in _VALID_AXIS_KINDS:
        raise ValueError(f"axis must be one of {_VALID_AXIS_KINDS}; got {axis_kind!r}")
    chart_label = chart.get("Name")
    areas = chart_shape.chart_areas(chart)
    if not areas:
        raise ElementNotFoundError(f"chart {chart_label!r} has no <ChartArea>")
    pairs = [f"{a.get('Name')}/{x.get('Name')}" for a in areas for x in _area_axes(a, axis_kind)]

    def _miss(what: str) -> ElementNotFoundError:
        return ElementNotFoundError(f"{what} in chart {chart_label!r} (present: {pairs})")

    if chart_area is not None:
        scoped = [a for a in areas if a.get("Name") == chart_area]
        if not scoped:
            raise ElementNotFoundError(
                f"no ChartArea named {chart_area!r} in chart {chart_label!r} "
                f"(present: {[a.get('Name') for a in areas]})"
            )
        area = scoped[0]
    else:
        area = areas[0]

    axes = _area_axes(area, axis_kind)
    if axis_name is None:
        for x in axes:
            if x.get("Name") == "Primary":
                return area, x
        if len(axes) == 1:
            return area, axes[0]
        raise _miss(
            f"axis_name is required: ChartArea {area.get('Name')!r} has {len(axes)} "
            f"{axis_kind} axes and none is named 'Primary'"
        )
    for x in axes:
        if x.get("Name") == axis_name:
            return area, x
    if chart_area is None:
        others = [
            (a, x)
            for a in areas[1:]
            for x in _area_axes(a, axis_kind)
            if x.get("Name") == axis_name
        ]
        if len(others) == 1:
            return others[0]
    raise _miss(f"no {axis_kind} axis named {axis_name!r} in ChartArea {area.get('Name')!r}")


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
    """Write the axis title text, ``ChartAxisTitle/Caption``.

    Only the ``Caption`` is written: the title's other children
    (``Style`` with the font Report Builder authors, ``Position``,
    ``TextOrientation``) are kept. Empty string clears the whole title
    block. Migrates pre-v0.3.1 reports that have a bare ``<Title>``
    element by removing it (RB rejects that name on a ChartAxis).
    """
    legacy = find_child(axis, "Title")
    if legacy is not None:
        axis.remove(legacy)
    existing = find_child(axis, "ChartAxisTitle")
    if title == "":
        if existing is not None:
            axis.remove(existing)
        return
    if existing is None:
        existing = etree.Element(q("ChartAxisTitle"))
        _insert_axis_child_in_order(axis, existing)
    caption = find_child(existing, "Caption")
    if caption is None:
        # Caption first: Report Builder's order (the XSD is a choice).
        caption = etree.Element(q("Caption"))
        existing.insert(0, caption)
    set_node_text(caption, encode_text(title))


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
        remove_child_if_present(axis, local)
        return
    set_text_if_different(axis, local, encode_text(value), order=_CHART_AXIS_CHILD_ORDER)


# A literal axis bound / interval: a number (RDL invariant culture), or
# NaN (Report Builder's "Auto").
_AXIS_NUMBER_RE = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")


def _axis_number(value: Optional[str], field: str) -> Optional[str]:
    """Validate ``min`` / ``max`` / ``interval``: a number, ``NaN``, an
    ``=expression`` or ``''`` (clear). Surrounding whitespace is trimmed."""
    if value is None or value.startswith("="):
        return value
    text = value.strip()
    if value == "" or text == "NaN" or _AXIS_NUMBER_RE.match(text):
        return text
    raise ValueError(
        f"{field}: invalid axis value {value!r}; expected a number (e.g. '0', '2.5', "
        "'1e6'), 'NaN' (auto), an =expression, or '' to clear"
    )


def _axis_flag(value: Union[bool, str, None], field: str, allow_auto: bool) -> Optional[str]:
    """``True``/``False`` (or their strings, any case) -> ``'True'`` /
    ``'False'``; ``'Auto'`` when ``allow_auto``. RDL's ChartAxis booleans
    are case-sensitive (lowercase ``true`` is refused by Report Builder)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "True" if value else "False"
    allowed = ("Auto", "True", "False") if allow_auto else ("True", "False")
    if isinstance(value, str):
        for candidate in allowed:
            if value.strip().lower() == candidate.lower():
                return candidate
    raise ValueError(
        f"{field}: invalid value {value!r}; expected "
        + ("true, false or 'Auto'" if allow_auto else "true or false")
    )


def _literal_number(text: Optional[str]) -> Optional[float]:
    """The value of a literal numeric axis bound; None for an expression,
    NaN, empty or absent."""
    if text is None:
        return None
    text = text.strip()
    if not _AXIS_NUMBER_RE.match(text):
        return None
    return float(text)


def set_chart_axis(
    path: str,
    chart_name: str,
    axis: str,
    axis_name: Optional[str] = None,
    title: Optional[str] = None,
    format: Optional[str] = None,  # noqa: A002 - tool-facing arg name; intentional
    min: Optional[str] = None,  # noqa: A002 - tool-facing
    max: Optional[str] = None,  # noqa: A002 - tool-facing
    log_scale: Optional[bool] = None,
    interval: Optional[str] = None,
    visible: Union[bool, str, None] = None,
    chart_area: Optional[str] = None,
) -> dict[str, Any]:
    """Configure a chart axis: title, numeric format, range, log scale,
    interval, visibility.

    ``axis`` ∈ {Category, Value}. ``chart_area`` names the ``ChartArea``
    (default: the first). ``axis_name`` defaults to ``Primary``, or the
    area's only axis of that kind; a named axis that is not in the first
    area but in exactly one other area is found there. A miss lists the
    ``area/axis`` pairs present.

    Each optional field follows the canonical "None = unchanged, '' =
    clear the element" convention. ``title`` rewrites only
    ``ChartAxisTitle/Caption`` (the title's ``Style`` etc. are kept).
    ``min`` / ``max`` / ``interval`` must be a number, ``NaN`` (auto) or
    an ``=expression``. ``log_scale`` is a boolean; a log scale with a
    literal minimum ``<= 0`` is refused. ``visible`` is ``true`` /
    ``false`` / ``"Auto"`` (written ``True`` / ``False`` / ``Auto``).

    Returns ``{chart, axis, axis_name, chart_area, kind, changed:
    list[str]}`` — ``changed`` lists only the fields whose value really
    changed; empty when nothing did (file untouched).
    """
    if axis not in _VALID_AXIS_KINDS:
        raise ValueError(f"axis must be one of {_VALID_AXIS_KINDS}; got {axis!r}")
    min = _axis_number(min, "min")
    max = _axis_number(max, "max")
    interval = _axis_number(interval, "interval")
    log_text = _axis_flag(log_scale, "log_scale", allow_auto=False)
    visible_text = _axis_flag(visible, "visible", allow_auto=True)
    interval_value = _literal_number(interval)
    if interval_value is not None and interval_value < 0:
        raise ValueError(f"interval: must not be negative; got {interval!r}")

    flags = (title, format, min, max, log_text, interval, visible_text)
    if all(v is None for v in flags):
        return {
            "chart": chart_name,
            "axis": axis,
            "axis_name": axis_name,
            "chart_area": chart_area,
            "kind": "ChartAxis",
            "changed": [],
        }

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    area_el, axis_el = _resolve_chart_axis(chart, axis, axis_name, chart_area)

    # A logarithmic scale cannot start at or below zero: refuse a call that
    # sets up that combination (with either argument).
    if log_text is not None or min is not None:
        log_node = find_child(axis_el, "LogScale")
        min_node = find_child(axis_el, "Minimum")
        eff_log = (
            log_text if log_text is not None else (log_node.text if log_node is not None else None)
        )
        eff_min = min if min is not None else (min_node.text if min_node is not None else None)
        min_value = _literal_number(eff_min)
        if (eff_log or "").strip() == "True" and min_value is not None and min_value <= 0:
            raise ValueError(
                f"log_scale: a logarithmic axis needs a minimum greater than zero; the "
                f"axis minimum is {eff_min!r}. Pass min (e.g. '1', 'NaN' for auto) or "
                "log_scale=false"
            )

    changed: list[str] = []
    # ChartAxis/LogScale and Visible are BooleanLocalizableType — a
    # case-sensitive enum {True, False} (Visible also Auto). Lowercase
    # 'true'/'false' is rejected by RB with "true is not a valid value."
    # This differs from BooleanExpressionType used on most other
    # ReportItem booleans.
    writes = (
        ("Title", title, _set_axis_title),
        ("Style.Format", format, _set_axis_format),
        ("Minimum", min, lambda a, v: _set_axis_simple_text(a, "Minimum", v)),
        ("Maximum", max, lambda a, v: _set_axis_simple_text(a, "Maximum", v)),
        ("LogScale", log_text, lambda a, v: _set_axis_simple_text(a, "LogScale", v)),
        ("Interval", interval, lambda a, v: _set_axis_simple_text(a, "Interval", v)),
        ("Visible", visible_text, lambda a, v: _set_axis_simple_text(a, "Visible", v)),
    )
    for label, value, writer in writes:
        if value is not None and changed_by(axis_el, writer, axis_el, value):
            changed.append(label)

    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "axis": axis,
        "axis_name": axis_el.get("Name"),
        "chart_area": area_el.get("Name"),
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
    y_expression: str,
    series_type: str = "Column",
    series_subtype: str = "Plain",
    data_points: int = 1,
) -> etree._Element:
    """Construct a single ``<ChartSeries>`` with the canonical shape.

    Mirrors the structure :func:`insert_chart_from_template` builds for
    the initial series, so a chart with N series via this tool reads
    consistently via :func:`get_chart`. ``y_expression`` is each point's
    ``<Y>``; ``data_points`` is the number of ``<ChartDataPoint>`` to
    emit — one per category leaf member.
    """
    series = etree.Element(q("ChartSeries"), Name=series_name)
    points_root = etree.SubElement(series, q("ChartDataPoints"))
    for _ in range(max(data_points, 1)):
        data_point = etree.SubElement(points_root, q("ChartDataPoint"))
        values = etree.SubElement(data_point, q("ChartDataPointValues"))
        y_node = etree.SubElement(values, q("Y"))
        y_node.text = encode_text(y_expression)
    etree.SubElement(series, q("Type")).text = series_type
    etree.SubElement(series, q("Subtype")).text = series_subtype
    return series


def add_chart_series(
    path: str,
    chart_name: str,
    series_name: str,
    value_field: Optional[str] = None,
    series_type: str = "Column",
    series_subtype: Optional[str] = None,
    value_expression: Optional[str] = None,
) -> dict[str, Any]:
    """Append a new ``<ChartSeries>`` to the named chart, together with its
    ``ChartSeriesHierarchy`` member.

    ``series_name`` must be a valid RDL identifier. ``value_field`` must be
    a field of the chart's dataset (an unknown one is refused, listing the
    available fields); ``value_expression`` takes the whole Y
    ``=expression`` instead (exclusive with ``value_field``).

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
    names.validate_identifier(series_name, "series")
    _field_or_expression(value_field, value_expression, "value_field", "value_expression")
    series_type, series_subtype = chart_types.normalize_series_type(series_type, series_subtype)
    if series_subtype is None:
        series_subtype = chart_types.DEFAULT_SUBTYPE[series_type]

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)
    if value_field is not None:
        dataset_name, available = _chart_dataset_fields(doc, chart)
        _check_field_exists(value_field, "value_field", dataset_name, available)

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
            y_expression=(
                value_expression
                if value_expression is not None
                else f"=Sum(Fields!{value_field}.Value)"
            ),
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


def _resolve_named_chart_child(
    chart: etree._Element, container: str, local: str, name: Optional[str]
) -> etree._Element:
    """Resolve ``<container>/<local Name=name>`` (a ChartTitle or
    ChartLegend). With ``name`` None: the one named ``Default`` (what the
    template emits), else the chart's only one. Misses raise
    ``ElementNotFoundError`` listing the names present."""
    root = find_child(chart, container)
    items = find_children(root, local) if root is not None else []
    present = [el.get("Name") for el in items]
    if name is None:
        for el in items:
            if el.get("Name") == "Default":
                return el
        if len(items) == 1:
            return items[0]
        raise ElementNotFoundError(
            f"chart {chart.get('Name')!r} has {len(items)} {local} elements and none is "
            f"named 'Default'; pass its name (present: {present})"
        )
    for el in items:
        if el.get("Name") == name:
            return el
    raise ElementNotFoundError(
        f"no {local} named {name!r} in chart {chart.get('Name')!r} (present: {present})"
    )


def _resolve_chart_legend(
    chart: etree._Element, legend_name: Optional[str] = None
) -> etree._Element:
    """Resolve a ``<ChartLegend>`` (see :func:`_resolve_named_chart_child`)."""
    return _resolve_named_chart_child(chart, "ChartLegends", "ChartLegend", legend_name)


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
    legend_name: Optional[str] = None,
    position: Optional[str] = None,
    visible: Optional[bool] = None,
) -> dict[str, Any]:
    """Configure the named chart legend.

    ``legend_name`` defaults to the legend named ``Default``, else the
    chart's only legend; a miss lists the legends present.

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
        "legend": legend.get("Name"),
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
    if visible_expression is not None and not visible_expression.startswith("="):
        if visible_expression.strip().lower() not in ("true", "false"):
            raise ValueError(
                f"visible_expression: must be an =expression (e.g. '=Fields!Show.Value') or "
                f"true/false; got {visible_expression!r}"
            )
        visible_expression = visible_expression.strip().lower()
    if font_weight not in (None, ""):
        font_weight = style_values.font_weight(font_weight, "font_weight")
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


# The palette block of a <Chart> in Report Builder's order. It follows the
# chart-content blocks (ChartCategoryHierarchy … ChartTitles) and precedes
# the elements in _PALETTE_ANCHORS (the XSD is a choice, so this is
# Report Builder's layout rather than a schema rule).
_PALETTE_BLOCK = ("Palette", "ChartCustomPaletteColors", "PaletteHatchBehavior")
_PALETTE_ANCHORS = (
    "ChartBorderSkin",
    "ChartNoDataMessage",
    "DataSetName",
    "Top",
    "Left",
    "Height",
    "Width",
    "Style",
)


def _place_palette_child(chart: etree._Element, node: etree._Element) -> None:
    """Insert a palette-block element after its present predecessors in
    :data:`_PALETTE_BLOCK`, else before the first later block element or
    anchor, else at the end."""
    idx = _PALETTE_BLOCK.index(etree.QName(node).localname)
    for prev_local in reversed(_PALETTE_BLOCK[:idx]):
        prev = find_child(chart, prev_local)
        if prev is not None:
            prev.addnext(node)
            return
    for later in _PALETTE_BLOCK[idx + 1 :] + _PALETTE_ANCHORS:
        anchor = find_child(chart, later)
        if anchor is not None:
            anchor.addprevious(node)
            return
    chart.append(node)


def _set_palette_text(chart: etree._Element, local: str, value: str) -> None:
    """Write ``<local>value</local>`` in the palette block; ``""`` removes it."""
    existing = find_child(chart, local)
    if value == "":
        if existing is not None:
            chart.remove(existing)
        return
    if existing is None:
        existing = etree.Element(q(local))
        _place_palette_child(chart, existing)
    set_node_text(existing, value)


def _set_custom_palette_colors(chart: etree._Element, colors: list[str]) -> None:
    """Make ``ChartCustomPaletteColors`` hold exactly ``colors`` (in order);
    an empty list removes it. An identical block is left as it is."""
    existing = find_child(chart, "ChartCustomPaletteColors")
    if not colors:
        if existing is not None:
            chart.remove(existing)
        return
    if existing is not None and chart_shape.custom_palette_colors(chart) == colors:
        return
    block = etree.Element(q("ChartCustomPaletteColors"))
    for value in colors:
        etree.SubElement(block, q("ChartCustomPaletteColor")).text = value
    if existing is not None:
        chart.replace(existing, block)
    else:
        _place_palette_child(chart, block)


def _canonical_choice(value: str, allowed: tuple[str, ...], field: str) -> str:
    """``value`` matched case-insensitively against ``allowed``, in its
    canonical spelling (no expressions: these are plain RDL enums)."""
    for candidate in allowed:
        if candidate.lower() == value.strip().lower():
            return candidate
    raise ValueError(f"{field} {value!r} not valid; expected one of {list(allowed)} or '' to clear")


def set_chart_palette(
    path: str,
    chart_name: str,
    palette: str,
    colors: Optional[list[str]] = None,
    hatch_behavior: Optional[str] = None,
) -> dict[str, Any]:
    """Set the chart's ``<Palette>``, its custom colours and hatching.

    ``palette`` ∈ :data:`pbirb_mcp.core.chart_types.PALETTES` (the 16 RDL
    ChartPalette values, matched case-insensitively). ``Custom`` takes its
    colours from ``colors`` — named colours, ``#RRGGBB`` / ``#AARRGGBB``
    or ``=expressions``, written in order as
    ``<ChartCustomPaletteColors><ChartCustomPaletteColor>``. ``colors`` is
    required with ``Custom`` (unless the chart already has custom colours,
    which are then kept) and refused with any other palette. ``""`` clears
    ``<Palette>`` and ``<ChartCustomPaletteColors>`` (Report Builder falls
    back to its default palette).

    ``hatch_behavior`` ∈ Default / None / Always writes
    ``<PaletteHatchBehavior>``; ``""`` removes it; ``None`` leaves it.

    Returns ``{chart, kind, palette, custom_colors, changed: bool}``.
    False when everything already matched (no save).
    """
    if palette != "":
        palette = _canonical_choice(palette, chart_types.PALETTES, "palette")
    if hatch_behavior not in (None, ""):
        hatch_behavior = _canonical_choice(
            hatch_behavior, chart_types.PALETTE_HATCH_BEHAVIORS, "hatch_behavior"
        )
    if colors is not None:
        if palette != "Custom":
            raise ValueError(
                "colors: only valid with palette='Custom' "
                f"(got palette={palette!r}); a built-in palette has fixed colours"
            )
        if not isinstance(colors, list) or not colors:
            raise ValueError("colors: give at least one colour for the Custom palette")
        colors = [_palette_color(c, f"colors[{i}]") for i, c in enumerate(colors)]

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    if palette == "Custom" and colors is None and not chart_shape.custom_palette_colors(chart):
        raise ValueError(
            "colors: required with palette='Custom' — pass the palette's colours, "
            "e.g. ['#112233', 'Red']"
        )

    with ChangeTracker(chart) as tracker:
        _set_palette_text(chart, "Palette", palette)
        if palette == "":
            _set_custom_palette_colors(chart, [])
        elif colors is not None:
            _set_custom_palette_colors(chart, colors)
        if hatch_behavior is not None:
            _set_palette_text(chart, "PaletteHatchBehavior", hatch_behavior)
    if tracker.changed:
        doc.save()
    return {
        "chart": chart_name,
        "kind": "Chart",
        "palette": palette or None,
        "custom_colors": chart_shape.custom_palette_colors(chart),
        "changed": tracker.changed,
    }


def _palette_color(value: object, field: str) -> str:
    """A colour for a custom palette or a series: whitespace around a
    literal is trimmed, then :func:`pbirb_mcp.core.style_values.color`
    validates it (named colour, ``#RRGGBB``, ``#AARRGGBB``, ``=expr``)."""
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected a colour string; got {type(value).__name__}")
    text = value if value.startswith("=") else value.strip()
    if text == "":
        raise ValueError(f"{field}: empty colour")
    return encode_text(style_values.color(text, field))


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


def _resolve_chart_title(chart: etree._Element, title_name: Optional[str] = None) -> etree._Element:
    """Resolve a ``<ChartTitle>`` (see :func:`_resolve_named_chart_child`)."""
    return _resolve_named_chart_child(chart, "ChartTitles", "ChartTitle", title_name)


def set_chart_title(
    path: str,
    chart_name: str,
    text: str,
    title_name: Optional[str] = None,
) -> dict[str, Any]:
    """Update ``<ChartTitle>/<Caption>``.

    ``text`` can be literal text or an ``=expression``. ``title_name``
    defaults to the title named ``Default`` (what the template emits),
    else the chart's only title (Report Builder names it ``Title``); a
    miss lists the titles present.

    Returns ``{chart, title, kind, changed: bool}``.
    """
    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    title = _resolve_chart_title(chart, title_name)
    title_name = title.get("Name")

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
    if group_field is not None:
        _check_field_name(group_field, "group_field", expression_param="group_expression")
    else:
        _check_expression(
            group_expression,
            "group_expression",
            "without '=' it is a constant and every row lands in one series; "
            "for a plain field use group_field",
        )

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


# ---- set_chart_series_properties (#104) -----------------------------------


# Report Builder's child orders (the XSD types these as unordered choices).
_SERIES_ORDER = (
    "Hidden",
    "ChartDataPoints",
    "Type",
    "Subtype",
    "Style",
    "ChartEmptyPoints",
    "CustomProperties",
    "LegendName",
    "ChartItemInLegend",
    "ChartAreaName",
    "ValueAxisName",
    "CategoryAxisName",
    "ChartSmartLabel",
    "ChartDataLabel",
    "ChartMarker",
)
_POINT_ORDER = (
    "ChartDataPointValues",
    "ChartDataLabel",
    "AxisLabel",
    "ToolTip",
    "ActionInfo",
    "Style",
    "ChartMarker",
    "ChartItemInLegend",
    "CustomProperties",
    "DataElementName",
    "DataElementOutput",
)
_IN_LEGEND_ORDER = ("LegendText", "ToolTip", "ActionInfo", "Hidden")
_MARKER_ORDER = ("Type", "Size", "Style")
_EMPTY_POINTS_ORDER = ("Style", "ChartMarker", "ChartDataLabel", "AxisLabel", "ToolTip")
_AREA_ORDER = ("Hidden", "ChartCategoryAxes", "ChartValueAxes")
_AXIS_LOCATION_ORDER = _CHART_AXIS_CHILD_ORDER + ("Reverse", "CrossAt", "Location")

_EMPTY_POINT_PROPERTY = "EmptyPointValue"


def _child(parent: etree._Element, local: str, order: tuple[str, ...]) -> etree._Element:
    """``parent``'s ``<local>`` child, created in ``order`` when absent."""
    existing = find_child(parent, local)
    if existing is not None:
        return existing
    node = etree.Element(q(local))
    mutation._insert_in_order(parent, node, order)
    return node


def _write_text(
    parent: etree._Element, local: str, value: Optional[str], order: tuple[str, ...]
) -> bool:
    """None leaves ``<local>`` alone, ``''`` removes it, anything else is
    written (in ``order``). True iff the tree changed."""
    if value is None:
        return False
    if value == "":
        return mutation.remove_child_if_present(parent, local)
    return mutation.set_text_if_different(parent, local, value, order=order)


def _write_in(
    parent: etree._Element,
    wrapper: str,
    parent_order: tuple[str, ...],
    writes: list[tuple[str, Optional[str], tuple[str, ...]]],
) -> bool:
    """Apply ``(local, value, order)`` writes inside ``parent/<wrapper>``:
    the wrapper is created only for a value to write, and dropped when the
    call emptied it (one that was already empty is left alone)."""
    writes = [w for w in writes if w[1] is not None]
    if not writes:
        return False
    holder = find_child(parent, wrapper)
    if holder is None:
        if all(value == "" for _, value, _ in writes):
            return False
        holder = _child(parent, wrapper, parent_order)
    was_empty = len(holder) == 0
    changed = False
    for local, value, order in writes:
        changed |= _write_text(holder, local, value, order)
    if changed and not was_empty and len(holder) == 0:
        parent.remove(holder)
    return changed


def _custom_property(series: etree._Element, prop: str, value: Optional[str]) -> bool:
    """Write (``''`` removes) ``CustomProperties/CustomProperty[Name=prop]``."""
    if value is None:
        return False
    props = find_child(series, "CustomProperties")
    existing = None
    if props is not None:
        for cp in find_children(props, "CustomProperty"):
            name = find_child(cp, "Name")
            if name is not None and (name.text or "").strip() == prop:
                existing = cp
                break
    if value == "":
        if existing is None:
            return False
        props.remove(existing)
        if not find_children(props, "CustomProperty"):
            series.remove(props)
        return True
    if existing is not None:
        return mutation.set_text_if_different(existing, "Value", value, order=("Name", "Value"))
    props = _child(series, "CustomProperties", _SERIES_ORDER)
    cp = etree.SubElement(props, q("CustomProperty"))
    etree.SubElement(cp, q("Name")).text = prop
    etree.SubElement(cp, q("Value")).text = value
    return True


def _chart_area(chart: etree._Element, name: Optional[str]) -> etree._Element:
    """The named ``ChartArea`` (the first one for None); raises listing the
    areas when it doesn't exist."""
    root = find_child(chart, "ChartAreas")
    areas = find_children(root, "ChartArea") if root is not None else []
    if name is None and areas:
        return areas[0]
    for area in areas:
        if area.get("Name") == name:
            return area
    raise ElementNotFoundError(
        f"chart {chart.get('Name')!r} has no ChartArea named {name!r}; "
        f"areas: {[a.get('Name') for a in areas]}"
    )


def _ensure_axis(area: etree._Element, kind: str, axis_name: str) -> bool:
    """Make sure ``area`` has a ``kind`` (Value / Category) axis named
    ``axis_name``; a new one is drawn on the opposite side
    (``<Location>Opposite</Location>``), the secondary-axis layout. True
    iff it was created."""
    container = "ChartValueAxes" if kind == "Value" else "ChartCategoryAxes"
    axes = find_child(area, container)
    if axes is not None and any(
        a.get("Name") == axis_name for a in find_children(axes, "ChartAxis")
    ):
        return False
    names.validate_identifier(axis_name, f"{kind.lower()} axis")
    if axes is None:
        axes = _child(area, container, _AREA_ORDER)
    axis = etree.SubElement(axes, q("ChartAxis"), Name=axis_name)
    mutation.set_text_if_different(axis, "Location", "Opposite", order=_AXIS_LOCATION_ORDER)
    return True


def _relocate(node: etree._Element, anchor: etree._Element, before: bool) -> None:
    """Move ``node`` next to its sibling ``anchor``, keeping a pretty-printed
    parent's layout (the last child's tail closes the parent)."""
    prev = node.getprevious()
    if node.getnext() is None and prev is not None:
        prev.tail, node.tail = node.tail, prev.tail
    if before:
        anchor.addprevious(node)
    else:
        last = anchor.getnext() is None
        anchor.addnext(node)
        if last:
            anchor.tail, node.tail = node.tail, anchor.tail


def _move_series(chart: etree._Element, series: etree._Element, position: int) -> bool:
    """Move ``series`` to index ``position`` of ``ChartSeriesCollection``,
    moving its hierarchy member with it (series and leaf members match by
    position). The member moves among its siblings, so a series inside a
    group of several can only move within that group; other positions are
    refused with the reachable ones."""
    all_series = chart_shape.series_list(chart)
    if position >= len(all_series):
        raise ValueError(
            f"position {position} is out of range; chart {chart.get('Name')!r} has "
            f"{len(all_series)} series (positions 0..{len(all_series) - 1})"
        )
    index = all_series.index(series)
    if index == position:
        return False
    _sync_series_members(chart)
    leaf = chart_shape.series_member(chart, index)
    node = leaf
    while True:
        owner = node.getparent().getparent()
        if (
            owner is not None
            and owner.tag == q("ChartMember")
            and chart_shape.subtree_leaves(owner) == [leaf]
        ):
            node = owner
            continue
        break
    siblings = find_children(node.getparent(), "ChartMember")
    leaves = chart_shape.series_leaf_members(chart)
    start = leaves.index(chart_shape.subtree_leaves(siblings[0])[0])
    rest = [s for s in siblings if s is not node]
    starts = [start]
    for member in rest:
        starts.append(starts[-1] + len(chart_shape.subtree_leaves(member)))
    if position not in starts:
        raise ValueError(
            f"series {series.get('Name')!r} shares a series group with other series; "
            f"it can only move to position(s) {starts}"
        )
    slot = starts.index(position)
    if slot < len(rest):
        _relocate(node, rest[slot], before=True)
    else:
        _relocate(node, rest[-1], before=False)
    others = [s for s in all_series if s is not series]
    if position < len(others):
        _relocate(series, others[position], before=True)
    else:
        _relocate(series, others[-1], before=False)
    return True


def set_chart_series_properties(
    path: str,
    chart_name: str,
    series_name: str,
    legend_text: Optional[str] = None,
    hide_in_legend: Optional[bool] = None,
    legend_name: Optional[str] = None,
    value_axis: Optional[str] = None,
    category_axis: Optional[str] = None,
    chart_area: Optional[str] = None,
    hidden: Optional[bool] = None,
    position: Optional[int] = None,
    tooltip: Optional[str] = None,
    marker_type: Optional[str] = None,
    marker_size: Optional[str] = None,
    empty_point_value: Optional[str] = None,
    empty_point_color: Optional[str] = None,
) -> dict[str, Any]:
    """Set a series' legend entry, axes, chart area, visibility, order and
    per-point tooltip / marker / empty-point display.

    Every argument is optional (None = unchanged; ``''`` clears a string
    one):

    * ``legend_text`` — ``ChartItemInLegend/LegendText`` (overrides the
      member label in the legend; ``#SERIESNAME`` style keywords work);
      ``hide_in_legend`` — ``ChartItemInLegend/Hidden``; ``legend_name`` —
      ``LegendName``, an existing ``ChartLegend``.
    * ``value_axis`` / ``category_axis`` — ``ValueAxisName`` /
      ``CategoryAxisName``. An axis the series' chart area doesn't have is
      created there with ``Location=Opposite`` (a secondary axis, e.g.
      ``value_axis="Secondary"`` for a two-scale combo chart; listed in
      ``created_axes``). ``chart_area`` — ``ChartAreaName``, an existing
      ``ChartArea``.
    * ``hidden`` — ``ChartSeries/Hidden``.
    * ``position`` — move the series (and its hierarchy member, which sets
      the legend order) to this 0-based index.
    * ``tooltip``, ``marker_type`` (:data:`chart_types.MARKER_TYPES`),
      ``marker_size`` — ``ToolTip`` and ``ChartMarker/Type|Size`` on every
      data point of the series (where Report Builder keeps them).
    * ``empty_point_value`` — the ``EmptyPointValue`` custom property
      (Average | Zero); ``empty_point_color`` —
      ``ChartEmptyPoints/Style/Color`` (empty points are not drawn — a
      gap — until they get a colour).

    Returns ``{chart, series, kind, position, created_axes, changed,
    warnings}``; ``changed`` is empty (file untouched) when everything
    already matched.
    """
    if position is not None and (isinstance(position, bool) or position < 0):
        raise ValueError(f"position must be a non-negative integer; got {position!r}")
    marker_type = (
        style_values.enum(marker_type, "marker_type", chart_types.MARKER_TYPES)
        if marker_type
        else marker_type
    )
    if marker_size:
        marker_size = units.style_size(marker_size, "marker_size", positive=True)
    empty_point_value = (
        style_values.enum(empty_point_value, "empty_point_value", chart_types.EMPTY_POINT_VALUES)
        if empty_point_value
        else empty_point_value
    )
    if empty_point_color:
        empty_point_color = style_values.color(empty_point_color, "empty_point_color")

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    series = _find_series(_series_collection(chart), series_name)
    changed: list[str] = []
    created: list[str] = []
    warnings: list[str] = []

    def note(label: str, did: bool) -> None:
        if did:
            changed.append(label)

    if hidden is not None:
        note("Hidden", _write_text(series, "Hidden", "true" if hidden else "", _SERIES_ORDER))
    if legend_name:
        _resolve_chart_legend(chart, legend_name)
    note("LegendName", _write_text(series, "LegendName", legend_name, _SERIES_ORDER))
    in_legend = [
        ("LegendText", None if legend_text is None else encode_text(legend_text), _IN_LEGEND_ORDER),
        (
            "Hidden",
            None if hide_in_legend is None else ("true" if hide_in_legend else ""),
            _IN_LEGEND_ORDER,
        ),
    ]
    note("ChartItemInLegend", _write_in(series, "ChartItemInLegend", _SERIES_ORDER, in_legend))
    if chart_area:
        _chart_area(chart, chart_area)
    note("ChartAreaName", _write_text(series, "ChartAreaName", chart_area, _SERIES_ORDER))

    for kind, local, value in (
        ("Value", "ValueAxisName", value_axis),
        ("Category", "CategoryAxisName", category_axis),
    ):
        area_name = find_child(series, "ChartAreaName")
        if value and _ensure_axis(
            _chart_area(chart, area_name.text if area_name is not None else None), kind, value
        ):
            created.append(f"{kind}/{value}")
            changed.append("ChartValueAxes" if kind == "Value" else "ChartCategoryAxes")
        note(local, _write_text(series, local, value, _SERIES_ORDER))

    point_writes = (tooltip, marker_type, marker_size)
    if any(v is not None for v in point_writes):
        points = chart_shape.data_points(series)
        if not points:
            raise ValueError(f"series {series_name!r} has no ChartDataPoint to set")
        tip = None if tooltip is None else encode_text(tooltip)
        for point in points:
            note("ToolTip", _write_text(point, "ToolTip", tip, _POINT_ORDER))
            note(
                "ChartMarker",
                _write_in(
                    point,
                    "ChartMarker",
                    _POINT_ORDER,
                    [("Type", marker_type, _MARKER_ORDER), ("Size", marker_size, _MARKER_ORDER)],
                ),
            )
        type_node = find_child(series, "Type")
        stype = (type_node.text or "").strip() if type_node is not None else "Column"
        if marker_type and stype not in chart_types.MARKER_SERIES_TYPES:
            warnings.append(
                f"series {series_name!r} is a {stype} series; markers are drawn on "
                f"{sorted(chart_types.MARKER_SERIES_TYPES)} series"
            )

    note("EmptyPointValue", _custom_property(series, _EMPTY_POINT_PROPERTY, empty_point_value))
    if empty_point_color is not None:
        empty = find_child(series, "ChartEmptyPoints")
        if empty is None and empty_point_color != "":
            empty = _child(series, "ChartEmptyPoints", _SERIES_ORDER)
        if empty is not None:
            did = _write_in(empty, "Style", _EMPTY_POINTS_ORDER, [("Color", empty_point_color, ())])
            if did and len(empty) == 0:
                series.remove(empty)
            note("ChartEmptyPoints", did)

    if position is not None:
        note("position", _move_series(chart, series, position))

    changed = list(dict.fromkeys(changed))
    if changed:
        doc.save()
    return {
        "chart": chart_name,
        "series": series_name,
        "kind": "ChartSeries",
        "position": chart_shape.series_index(chart, series),
        "created_axes": created,
        "changed": changed,
        "warnings": warnings,
    }


# ---- set_chart_category_group (#104) --------------------------------------


_ADD_LEVELS = ("inner", "outer")


def _category_members(chart: etree._Element) -> list[etree._Element]:
    return [m for m, _ in chart_shape.walk_members(chart_shape.category_hierarchy_members(chart))]


def _category_target(chart: etree._Element, group_name: Optional[str]) -> etree._Element:
    """The category ChartMember whose Group is ``group_name``; with None,
    the only dynamic one. Raises listing the groups otherwise."""
    dynamic = [m for m in _category_members(chart) if find_child(m, "Group") is not None]
    names_ = [find_child(m, "Group").get("Name") for m in dynamic]
    if group_name is None:
        if len(dynamic) == 1:
            return dynamic[0]
        raise ValueError(
            f"chart {chart.get('Name')!r} has {len(dynamic)} category groups {names_}; "
            "pass group_name"
        )
    for member, name in zip(dynamic, names_):
        if name == group_name:
            return member
    raise ElementNotFoundError(
        f"chart {chart.get('Name')!r} has no category group {group_name!r}; groups: {names_}"
    )


def _category_member(
    name: str, expression: str, label: str, sort_value: str, direction: str
) -> etree._Element:
    member = etree.Element(q("ChartMember"))
    group = etree.SubElement(member, q("Group"), Name=name)
    exprs = etree.SubElement(group, q("GroupExpressions"))
    etree.SubElement(exprs, q("GroupExpression")).text = expression
    _write_sort(member, sort_value, direction)
    node = etree.Element(q("Label"))
    node.text = label
    _insert_member_child(member, node)
    return member


def set_chart_category_group(
    path: str,
    chart_name: str,
    group_name: Optional[str] = None,
    label_expression: Optional[str] = None,
    sort: Optional[str] = None,
    sort_expression: Optional[str] = None,
    add_level: Optional[str] = None,
    group_field: Optional[str] = None,
    group_expression: Optional[str] = None,
) -> dict[str, Any]:
    """Edit a chart category group, or add a nested category level.

    Edit (``add_level`` None): the group ``group_name`` (default: the
    chart's only category group). ``group_field`` / ``group_expression``
    regroup it (the label and a sort that followed the old expression
    follow the new one); ``label_expression`` sets the member ``<Label>``
    (axis label text, e.g. ``=Format(Fields!Date.Value,"MMM d")``);
    ``sort`` (Ascending / Descending / None) and ``sort_expression``
    (default: the group expression) set ``<SortExpressions>``.

    Add (``add_level`` = ``inner`` / ``outer``): a new dynamic level on
    ``group_field`` / ``group_expression`` (required) — inside the
    innermost member (``inner``; the chart must have one category leaf) or
    around every top-level member (``outer``). Its label is
    ``label_expression`` or the expression, its sort ``sort`` (default
    Ascending) on ``sort_expression`` or the expression; it is named
    ``group_name`` or ``<chart>_CategoryGroup<n>``. Leaf members, and so
    every series' data points, stay as they are. A level on an
    expression some category group already uses is not added again
    (``changed: []``).

    Returns ``{chart, kind: 'ChartGroup', group_name, expression, label,
    sort, level, changed}``.
    """
    if group_field is not None and group_expression is not None:
        raise ValueError("group_field and group_expression are mutually exclusive — pass only one")
    if add_level is not None and add_level not in _ADD_LEVELS:
        raise ValueError(f"add_level must be one of {list(_ADD_LEVELS)}; got {add_level!r}")
    if sort is not None and sort not in _SERIES_SORT_DIRECTIONS:
        raise ValueError(
            f"sort {sort!r} not valid; expected one of {list(_SERIES_SORT_DIRECTIONS)}"
        )
    if group_field is not None:
        names.validate_identifier(group_field, "field")
        expression: Optional[str] = f"=Fields!{group_field}.Value"
    elif group_expression is not None:
        if not group_expression.strip().startswith("="):
            raise ValueError(
                f"group_expression {group_expression!r} must be an =expression "
                "(a constant puts every row in one category)"
            )
        expression = encode_text(group_expression.strip())
    else:
        expression = None
    if add_level is not None and expression is None:
        raise ValueError("add_level needs group_field or group_expression")
    if label_expression == "":
        raise ValueError("label_expression cannot be empty: every ChartMember needs a <Label>")
    label_text = encode_text(label_expression) if label_expression is not None else None
    sort_text = encode_text(sort_expression) if sort_expression else None

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    changed: list[str] = []

    if add_level is not None:
        for member in _category_members(chart):
            group = find_child(member, "Group")
            if group is not None and _group_expression_texts(group) == [expression]:
                return _category_result(chart_name, member, [])
        if group_name is not None:
            names.validate_identifier(group_name, "group")
            names.ensure_unique(doc, group_name)
            name = group_name
        else:
            name = names.unique_name(doc, f"{chart_name}_CategoryGroup")
        member = _category_member(
            name,
            expression,
            label_text if label_text is not None else expression,
            sort_text or expression,
            sort or "Ascending",
        )
        top = chart_shape.category_hierarchy_members(chart)
        if top is None:
            hierarchy = find_child(chart, "ChartCategoryHierarchy")
            if hierarchy is None:
                hierarchy = etree.Element(q("ChartCategoryHierarchy"))
                chart.insert(0, hierarchy)
            top = etree.SubElement(hierarchy, q("ChartMembers"))
        existing = find_children(top, "ChartMember")
        if add_level == "outer" or not existing:
            if existing:
                inner = etree.Element(q("ChartMembers"))
                for old in existing:
                    inner.append(old)
                _insert_member_child(member, inner)
            top.append(member)
        else:
            leaves = chart_shape.category_leaf_members(chart)
            if len(leaves) != 1:
                raise ValueError(
                    f"chart {chart_name!r} has {len(leaves)} innermost category members; "
                    "add_level='inner' needs exactly one (use 'outer')"
                )
            holder = etree.Element(q("ChartMembers"))
            holder.append(member)
            _insert_member_child(leaves[0], holder)
        doc.save()
        return _category_result(chart_name, member, ["ChartMember"])

    member = _category_target(chart, group_name)
    group = find_child(member, "Group")
    old_exprs = _group_expression_texts(group)
    old_expr = old_exprs[0] if len(old_exprs) == 1 else None
    label = find_child(member, "Label")
    old_label = label.text if label is not None else None
    old_sort = chart_shape.member_sorts(member)

    new_expr = old_expr
    if expression is not None and old_exprs != [expression]:
        expressions = find_child(group, "GroupExpressions")
        if expressions is None:
            expressions = etree.Element(q("GroupExpressions"))
            group.insert(0, expressions)
        for old in list(expressions):
            expressions.remove(old)
        etree.SubElement(expressions, q("GroupExpression")).text = expression
        changed.append("GroupExpressions")
        new_expr = expression
        if label_text is None and (
            old_label is None or not old_label.lstrip().startswith("=") or old_label == old_expr
        ):
            label_text = expression
        if sort_text is None and len(old_sort) == 1 and old_sort[0][0] == old_expr:
            sort_text = expression
            sort = sort or old_sort[0][1]

    if label_text is not None:
        if label is None:
            label = etree.Element(q("Label"))
            _insert_member_child(member, label)
        if mutation.set_node_text(label, label_text):
            changed.append("Label")

    if sort is not None or sort_text is not None:
        # Direction defaults to the current one; the sorted expression to
        # sort_expression, else the current single sort's, else the group's.
        direction = sort or (old_sort[0][1] if len(old_sort) == 1 else "Ascending")
        target = sort_text or (old_sort[0][0] if len(old_sort) == 1 else new_expr)
        if target is None and direction != "None":
            raise ValueError("pass sort_expression: the group has no single group expression")
        if _write_sort(member, target or "", direction):
            changed.append("SortExpressions")

    if changed:
        doc.save()
    return _category_result(chart_name, member, changed)


def _category_result(chart_name: str, member: etree._Element, changed: list[str]) -> dict[str, Any]:
    group = find_child(member, "Group")
    exprs = _group_expression_texts(group) if group is not None else []
    label = find_child(member, "Label")
    level = len(chart_shape.member_ancestors(member)) - 1
    return {
        "chart": chart_name,
        "kind": "ChartGroup",
        "group_name": group.get("Name") if group is not None else None,
        "expression": exprs[0] if exprs else None,
        "label": label.text if label is not None else None,
        "sort": [{"value": v, "direction": d} for v, d in chart_shape.member_sorts(member)],
        "level": level,
        "changed": changed,
    }


__all__ = [
    "add_chart_series",
    "insert_chart_from_template",
    "remove_chart_series",
    "set_chart_axis",
    "set_chart_category_group",
    "set_chart_data_labels",
    "set_chart_legend",
    "set_chart_palette",
    "set_chart_series_grouping",
    "set_chart_series_properties",
    "set_chart_series_type",
    "set_chart_title",
    "set_series_color",
]
