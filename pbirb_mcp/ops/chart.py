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

from typing import Any

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


__all__ = [
    "add_chart_series",
    "insert_chart_from_template",
    "remove_chart_series",
    "set_chart_series_type",
]
