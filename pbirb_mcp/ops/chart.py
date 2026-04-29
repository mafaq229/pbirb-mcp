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
from pbirb_mcp.core.ids import resolve_dataset
from pbirb_mcp.core.xpath import find_child, q
from pbirb_mcp.ops.body import _ensure_body_report_items, _names_in, _resolve_body


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


__all__ = ["insert_chart_from_template"]
