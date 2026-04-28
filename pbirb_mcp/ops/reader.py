"""Read-only inventory tools.

These tools are the LLM's situational-awareness layer: every multi-step edit
plan begins with one of them. They never mutate the document — open, read,
return JSON-friendly dicts.

The output shapes are part of the public API. Field names are stable; new
fields may be added but existing ones are not renamed or removed without a
deprecation cycle.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, find_child, find_children


def _text(node: Optional[etree._Element]) -> Optional[str]:
    return node.text if node is not None else None


def _rd_text(parent: etree._Element, local: str) -> Optional[str]:
    found = parent.find(f"{{{RD_NS}}}{local}")
    return found.text if found is not None else None


# ---- describe_report -------------------------------------------------------


def describe_report(path: str) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    root = doc.root

    page_node = root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Page")
    page: dict[str, Any] = {}
    if page_node is not None:
        page = {
            "height": _text(find_child(page_node, "PageHeight")),
            "width": _text(find_child(page_node, "PageWidth")),
            "margin_top": _text(find_child(page_node, "TopMargin")),
            "margin_bottom": _text(find_child(page_node, "BottomMargin")),
            "margin_left": _text(find_child(page_node, "LeftMargin")),
            "margin_right": _text(find_child(page_node, "RightMargin")),
        }

    body_node = root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body")
    header_node = page_node.find(f"{{{RDL_NS}}}PageHeader") if page_node is not None else None
    footer_node = page_node.find(f"{{{RDL_NS}}}PageFooter") if page_node is not None else None

    return {
        "path": str(doc.path),
        "data_sources": [ds.get("Name") for ds in root.iter(f"{{{RDL_NS}}}DataSource")],
        "datasets": [ds.get("Name") for ds in root.iter(f"{{{RDL_NS}}}DataSet")],
        "parameters": [p.get("Name") for p in root.iter(f"{{{RDL_NS}}}ReportParameter")],
        "tablixes": [t.get("Name") for t in root.iter(f"{{{RDL_NS}}}Tablix")],
        # v0.2: full body / header / footer item enumeration so the LLM can
        # see every named ReportItem (Tablix, Textbox, Image, Rectangle, etc.)
        # without a follow-up list_*_items call.
        "body_items": _list_items_in(body_node),
        "header_items": _list_items_in(header_node),
        "footer_items": _list_items_in(footer_node),
        "page": page,
    }


# ---- get_datasets ----------------------------------------------------------


def _filter_dict(filter_node: etree._Element) -> dict[str, Any]:
    expr = find_child(filter_node, "FilterExpression")
    op = find_child(filter_node, "Operator")
    values = [
        _text(v) for v in filter_node.findall(f"{{{RDL_NS}}}FilterValues/{{{RDL_NS}}}FilterValue")
    ]
    return {
        "expression": _text(expr),
        "operator": _text(op),
        "values": values,
    }


def get_datasets(path: str) -> list[dict[str, Any]]:
    doc = RDLDocument.open(path)
    out: list[dict[str, Any]] = []
    for ds in doc.root.iter(f"{{{RDL_NS}}}DataSet"):
        query = find_child(ds, "Query")
        command_text = None
        data_source = None
        query_parameters: list[dict[str, Any]] = []
        if query is not None:
            command_text = _text(find_child(query, "CommandText"))
            data_source = _text(find_child(query, "DataSourceName"))
            qp_root = find_child(query, "QueryParameters")
            if qp_root is not None:
                for qp in find_children(qp_root, "QueryParameter"):
                    query_parameters.append(
                        {
                            "name": qp.get("Name"),
                            "value": _text(find_child(qp, "Value")),
                        }
                    )

        fields: list[dict[str, Any]] = []
        fields_root = find_child(ds, "Fields")
        if fields_root is not None:
            for f in find_children(fields_root, "Field"):
                fields.append(
                    {
                        "name": f.get("Name"),
                        "data_field": _text(find_child(f, "DataField")),
                        "type_name": _rd_text(f, "TypeName"),
                    }
                )

        filters = []
        filters_root = find_child(ds, "Filters")
        if filters_root is not None:
            filters = [_filter_dict(f) for f in find_children(filters_root, "Filter")]

        out.append(
            {
                "name": ds.get("Name"),
                "data_source": data_source,
                "command_text": command_text,
                "fields": fields,
                "query_parameters": query_parameters,
                "filters": filters,
            }
        )
    return out


# ---- get_parameters --------------------------------------------------------


def get_parameters(path: str) -> list[dict[str, Any]]:
    doc = RDLDocument.open(path)
    out: list[dict[str, Any]] = []
    for p in doc.root.iter(f"{{{RDL_NS}}}ReportParameter"):
        out.append(
            {
                "name": p.get("Name"),
                "data_type": _text(find_child(p, "DataType")),
                "prompt": _text(find_child(p, "Prompt")),
                "nullable": _text(find_child(p, "Nullable")) == "true",
                "allow_blank": _text(find_child(p, "AllowBlank")) == "true",
                "multi_value": _text(find_child(p, "MultiValue")) == "true",
                "hidden": _text(find_child(p, "Hidden")) == "true",
            }
        )
    return out


# ---- get_tablixes ----------------------------------------------------------


def _group_dict(group: etree._Element) -> dict[str, Any]:
    expr_node = find_child(group, "GroupExpressions")
    expressions: list[str] = []
    if expr_node is not None:
        for e in find_children(expr_node, "GroupExpression"):
            if e.text:
                expressions.append(e.text)
    return {
        "name": group.get("Name"),
        "expressions": expressions,
    }


def _hierarchy_groups(tablix: etree._Element, hierarchy_local: str) -> list[dict[str, Any]]:
    h = find_child(tablix, hierarchy_local)
    if h is None:
        return []
    groups: list[dict[str, Any]] = []
    members = h.find(f"{{{RDL_NS}}}TablixMembers")
    if members is None:
        return []
    for member in find_children(members, "TablixMember"):
        for group in member.iter(f"{{{RDL_NS}}}Group"):
            groups.append(_group_dict(group))
    return groups


def _sort_expressions(tablix: etree._Element) -> list[str]:
    sorts: list[str] = []
    for s in tablix.iter(f"{{{RDL_NS}}}SortExpression"):
        v = find_child(s, "Value")
        if v is not None and v.text:
            sorts.append(v.text)
    return sorts


def _visibility(node: etree._Element) -> Optional[dict[str, Any]]:
    vis = find_child(node, "Visibility")
    if vis is None:
        return None
    hidden = find_child(vis, "Hidden")
    toggle = find_child(vis, "ToggleItem")
    return {
        "hidden": _text(hidden),
        "toggle_item": _text(toggle),
    }


def _tablix_cells(tablix: etree._Element) -> list[dict[str, Any]]:
    """Enumerate every body cell as (row, col, textbox_name, row_span, col_span).
    textbox_name is None for cells with no Textbox child (rare but possible)."""
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    if rows_root is None:
        return []
    out: list[dict[str, Any]] = []
    for row_idx, row in enumerate(find_children(rows_root, "TablixRow")):
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            continue
        for col_idx, cell in enumerate(find_children(cells_root, "TablixCell")):
            tb = cell.find(f"{{{RDL_NS}}}CellContents/{{{RDL_NS}}}Textbox")
            row_span = _text(find_child(cell, "RowSpan"))
            col_span = _text(find_child(cell, "ColSpan"))
            out.append(
                {
                    "row": row_idx,
                    "col": col_idx,
                    "textbox_name": tb.get("Name") if tb is not None else None,
                    "row_span": int(row_span) if row_span else 1,
                    "col_span": int(col_span) if col_span else 1,
                }
            )
    return out


def get_tablixes(path: str) -> list[dict[str, Any]]:
    doc = RDLDocument.open(path)
    out: list[dict[str, Any]] = []
    for t in doc.root.iter(f"{{{RDL_NS}}}Tablix"):
        body = find_child(t, "TablixBody")
        columns: list[dict[str, Any]] = []
        if body is not None:
            cols_root = find_child(body, "TablixColumns")
            if cols_root is not None:
                for c in find_children(cols_root, "TablixColumn"):
                    columns.append({"width": _text(find_child(c, "Width"))})

        filters = []
        filters_root = find_child(t, "Filters")
        if filters_root is not None:
            filters = [_filter_dict(f) for f in find_children(filters_root, "Filter")]

        out.append(
            {
                "name": t.get("Name"),
                "dataset": _text(find_child(t, "DataSetName")),
                "top": _text(find_child(t, "Top")),
                "left": _text(find_child(t, "Left")),
                "width": _text(find_child(t, "Width")),
                "height": _text(find_child(t, "Height")),
                "columns": columns,
                "row_groups": _hierarchy_groups(t, "TablixRowHierarchy"),
                "column_groups": _hierarchy_groups(t, "TablixColumnHierarchy"),
                "sort_expressions": _sort_expressions(t),
                "filters": filters,
                "visibility": _visibility(t),
                # v0.2: cell-level inventory so LLMs can discover which
                # textbox names live where without parsing raw XML.
                "cells": _tablix_cells(t),
            }
        )
    return out


# ---- v0.2: list_*_items / get_textbox / get_image / get_rectangle --------


_REPORT_ITEM_TAGS = (
    "Tablix",
    "Textbox",
    "Image",
    "Rectangle",
    "Subreport",
    "Chart",
    "Map",
    "Gauge",
    "Line",
    "List",
)


def _layout_dict(el: etree._Element) -> dict[str, Any]:
    """Return name/type/top/left/width/height for a ReportItem."""
    return {
        "name": el.get("Name"),
        "type": etree.QName(el).localname,
        "top": _text(find_child(el, "Top")),
        "left": _text(find_child(el, "Left")),
        "width": _text(find_child(el, "Width")),
        "height": _text(find_child(el, "Height")),
    }


def _list_items_in(container: Optional[etree._Element]) -> list[dict[str, Any]]:
    if container is None:
        return []
    items_root = find_child(container, "ReportItems")
    if items_root is None:
        return []
    out: list[dict[str, Any]] = []
    for child in items_root:
        if etree.QName(child).localname in _REPORT_ITEM_TAGS:
            out.append(_layout_dict(child))
    return out


def _resolve_body(doc: RDLDocument) -> Optional[etree._Element]:
    return doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body")


def _resolve_page(doc: RDLDocument) -> Optional[etree._Element]:
    return doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Page")


def _resolve_header(doc: RDLDocument) -> Optional[etree._Element]:
    page = _resolve_page(doc)
    if page is None:
        return None
    return find_child(page, "PageHeader")


def _resolve_footer(doc: RDLDocument) -> Optional[etree._Element]:
    page = _resolve_page(doc)
    if page is None:
        return None
    return find_child(page, "PageFooter")


def list_body_items(path: str) -> list[dict[str, Any]]:
    """Enumerate every named ReportItem at the top level of <Body>.
    Returns a list of {name, type, top, left, width, height}."""
    doc = RDLDocument.open(path)
    return _list_items_in(_resolve_body(doc))


def list_header_items(path: str) -> list[dict[str, Any]]:
    """Same shape as ``list_body_items`` for <PageHeader>."""
    doc = RDLDocument.open(path)
    return _list_items_in(_resolve_header(doc))


def list_footer_items(path: str) -> list[dict[str, Any]]:
    """Same shape as ``list_body_items`` for <PageFooter>."""
    doc = RDLDocument.open(path)
    return _list_items_in(_resolve_footer(doc))


def _runs_of(textbox: etree._Element) -> list[dict[str, Any]]:
    runs = []
    for paragraph in textbox.iter(f"{{{RDL_NS}}}Paragraph"):
        for run in paragraph.iter(f"{{{RDL_NS}}}TextRun"):
            value = find_child(run, "Value")
            runs.append({"value": _text(value)})
    return runs


def _style_dict(el: Optional[etree._Element]) -> Optional[dict[str, Any]]:
    """Capture key Style children. Best-effort; returns None if no style."""
    style = find_child(el, "Style") if el is not None else None
    if style is None:
        return None
    out: dict[str, Any] = {}
    for child_local in (
        "FontFamily",
        "FontSize",
        "FontWeight",
        "FontStyle",
        "Color",
        "BackgroundColor",
        "TextAlign",
        "VerticalAlign",
        "Format",
        "PaddingTop",
        "PaddingBottom",
        "PaddingLeft",
        "PaddingRight",
    ):
        node = find_child(style, child_local)
        if node is not None and node.text is not None:
            out[child_local] = node.text
    return out or None


def _find_named_anywhere(doc: RDLDocument, local_name: str, name: str) -> Optional[etree._Element]:
    for el in doc.root.iter(f"{{{RDL_NS}}}{local_name}"):
        if el.get("Name") == name:
            return el
    return None


def get_textbox(path: str, name: str) -> dict[str, Any]:
    """Return position, size, runs, style, visibility for a named Textbox.

    Searches the entire report (body, header, footer, AND tablix cell
    contents). Tablix-cell textboxes don't have top/left/width/height —
    those fields are returned as None.
    """
    doc = RDLDocument.open(path)
    tb = _find_named_anywhere(doc, "Textbox", name)
    if tb is None:
        from pbirb_mcp.core.ids import ElementNotFoundError

        raise ElementNotFoundError(f"no Textbox named {name!r}")
    return {
        "name": name,
        "type": "Textbox",
        "top": _text(find_child(tb, "Top")),
        "left": _text(find_child(tb, "Left")),
        "width": _text(find_child(tb, "Width")),
        "height": _text(find_child(tb, "Height")),
        "runs": _runs_of(tb),
        "style": _style_dict(tb),
        "visibility": _visibility(tb),
        "can_grow": _text(find_child(tb, "CanGrow")),
        "can_shrink": _text(find_child(tb, "CanShrink")),
    }


def get_image(path: str, name: str) -> dict[str, Any]:
    """Return position, size, source, value, sizing for a named Image."""
    doc = RDLDocument.open(path)
    img = _find_named_anywhere(doc, "Image", name)
    if img is None:
        from pbirb_mcp.core.ids import ElementNotFoundError

        raise ElementNotFoundError(f"no Image named {name!r}")
    return {
        "name": name,
        "type": "Image",
        "top": _text(find_child(img, "Top")),
        "left": _text(find_child(img, "Left")),
        "width": _text(find_child(img, "Width")),
        "height": _text(find_child(img, "Height")),
        "source": _text(find_child(img, "Source")),
        "value": _text(find_child(img, "Value")),
        "sizing": _text(find_child(img, "Sizing")),
        "mime_type": _text(find_child(img, "MIMEType")),
        "style": _style_dict(img),
        "visibility": _visibility(img),
    }


def get_rectangle(path: str, name: str) -> dict[str, Any]:
    """Return position, size, contained-item names, and style for a Rectangle."""
    doc = RDLDocument.open(path)
    rect = _find_named_anywhere(doc, "Rectangle", name)
    if rect is None:
        from pbirb_mcp.core.ids import ElementNotFoundError

        raise ElementNotFoundError(f"no Rectangle named {name!r}")
    contained: list[str] = []
    items_root = find_child(rect, "ReportItems")
    if items_root is not None:
        for child in items_root:
            n = child.get("Name")
            if n is not None:
                contained.append(n)
    return {
        "name": name,
        "type": "Rectangle",
        "top": _text(find_child(rect, "Top")),
        "left": _text(find_child(rect, "Left")),
        "width": _text(find_child(rect, "Width")),
        "height": _text(find_child(rect, "Height")),
        "contained_items": contained,
        "style": _style_dict(rect),
        "visibility": _visibility(rect),
    }


__all__ = [
    "describe_report",
    "get_datasets",
    "get_image",
    "get_parameters",
    "get_rectangle",
    "get_tablixes",
    "get_textbox",
    "list_body_items",
    "list_footer_items",
    "list_header_items",
]
