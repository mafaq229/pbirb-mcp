"""Read-only inventory tools.

These tools are the LLM's situational-awareness layer: every multi-step edit
plan begins with one of them. They never mutate the document — open, read,
return JSON-friendly dicts.

The output shapes are part of the public API. Field names are stable; new
fields may be added but existing ones are not renamed or removed without a
deprecation cycle.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, find_child, find_children, q, qrd


def _text(node: Optional[etree._Element]) -> Optional[str]:
    return node.text if node is not None else None


def _rd_text(parent: etree._Element, local: str) -> Optional[str]:
    found = parent.find(f"{{{RD_NS}}}{local}")
    return found.text if found is not None else None


# ---- describe_report -------------------------------------------------------


def _parameter_layout_summary(root: etree._Element) -> Optional[dict[str, Any]]:
    """Return ``{rows, columns, cell_count, parameters_count, in_sync}``
    for ``<ReportParametersLayout>``. ``None`` when no layout block is
    present (the typical no-layout case is silent — the LLM doesn't see
    a phantom block)."""
    layout = find_child(root, "ReportParametersLayout")
    if layout is None:
        return None
    grid = find_child(layout, "GridLayoutDefinition")
    rows_node = find_child(grid, "NumberOfRows") if grid is not None else None
    cols_node = find_child(grid, "NumberOfColumns") if grid is not None else None
    cells_root = find_child(grid, "CellDefinitions") if grid is not None else None
    cell_count = len(find_children(cells_root, "CellDefinition")) if cells_root is not None else 0
    params_block = find_child(root, "ReportParameters")
    parameters_count = (
        len(find_children(params_block, "ReportParameter")) if params_block is not None else 0
    )
    return {
        "rows": int(rows_node.text) if rows_node is not None and rows_node.text else 0,
        "columns": int(cols_node.text) if cols_node is not None and cols_node.text else 0,
        "cell_count": cell_count,
        "parameters_count": parameters_count,
        "in_sync": cell_count == parameters_count,
    }


def _embedded_images_summary(root: etree._Element) -> list[dict[str, Any]]:
    """Return ``[{name, mime_type, byte_size}]`` for every
    ``<EmbeddedImage>``. ``byte_size`` is the decoded length — base64
    text is not echoed back here (use ``get_embedded_image_data`` for
    that)."""
    block = find_child(root, "EmbeddedImages")
    if block is None:
        return []
    out: list[dict[str, Any]] = []
    for entry in find_children(block, "EmbeddedImage"):
        mime = find_child(entry, "MIMEType")
        data = find_child(entry, "ImageData")
        b64 = data.text if data is not None and data.text else ""
        try:
            byte_size = len(base64.b64decode(b64, validate=False))
        except Exception:  # noqa: BLE001
            byte_size = 0
        out.append(
            {
                "name": entry.get("Name"),
                "mime_type": mime.text if mime is not None else None,
                "byte_size": byte_size,
            }
        )
    return out


def _dataset_query_parameters_summary(root: etree._Element) -> list[dict[str, Any]]:
    """Return ``[{dataset, name, value}]`` for every
    ``<DataSet>/<Query>/<QueryParameters>/<QueryParameter>``. Helps the
    LLM see at a glance which DAX parameter bindings exist (and catch
    PBIDATASET ``@``-prefix mismatches on first read)."""
    out: list[dict[str, Any]] = []
    for ds in root.iter(q("DataSet")):
        query = find_child(ds, "Query")
        if query is None:
            continue
        qps = find_child(query, "QueryParameters")
        if qps is None:
            continue
        for qp in find_children(qps, "QueryParameter"):
            value = find_child(qp, "Value")
            out.append(
                {
                    "dataset": ds.get("Name"),
                    "name": qp.get("Name"),
                    "value": value.text if value is not None else None,
                }
            )
    return out


def _designer_state_present(root: etree._Element) -> bool:
    """``True`` iff any ``<DataSet>/<Query>/<rd:DesignerState>`` exists.
    Useful for detecting PBI Query Designer-authored datasets — those
    benefit from ``update_dataset_query``'s DesignerState sync."""
    for ds in root.iter(q("DataSet")):
        query = find_child(ds, "Query")
        if query is None:
            continue
        if query.find(qrd("DesignerState")) is not None:
            return True
    return False


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
        # v0.3 (Phase 12 commit 44): visibility into the blocks the LLM
        # had to scan separately before — parameter-layout sync state,
        # embedded-image inventory, dataset-level query-parameter
        # bindings, and PBI DesignerState presence.
        "parameter_layout": _parameter_layout_summary(root),
        "embedded_images": _embedded_images_summary(root),
        "dataset_query_parameters": _dataset_query_parameters_summary(root),
        "designer_state_present": _designer_state_present(root),
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
                # Calculated fields carry <Value> instead of <DataField>;
                # the reader surfaces both so consumers can disambiguate.
                fields.append(
                    {
                        "name": f.get("Name"),
                        "data_field": _text(find_child(f, "DataField")),
                        "value": _text(find_child(f, "Value")),
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
    """Return name/type/top/left/width/height for a ReportItem.

    Per RDL semantics, a missing ``<Top>`` / ``<Left>`` element means 0 — so
    we coerce those to ``"0in"`` rather than ``None``. This avoids surprising
    callers who do arithmetic on the response.
    """
    return {
        "name": el.get("Name"),
        "type": etree.QName(el).localname,
        "top": _text(find_child(el, "Top")) or "0in",
        "left": _text(find_child(el, "Left")) or "0in",
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


_BOX_STYLE_FIELDS = (
    "BackgroundColor",
    "VerticalAlign",
    "PaddingTop",
    "PaddingBottom",
    "PaddingLeft",
    "PaddingRight",
    "WritingMode",
)
_PARAGRAPH_STYLE_FIELDS = ("TextAlign",)
_RUN_STYLE_FIELDS = (
    "FontFamily",
    "FontSize",
    "FontWeight",
    "FontStyle",
    "Color",
    "Format",
    "TextDecoration",
)
_BORDER_STYLE_FIELDS = ("Style", "Color", "Width")


def _capture_style_fields(
    style: Optional[etree._Element], fields: tuple[str, ...]
) -> Optional[dict[str, str]]:
    """Read the given Style children of ``style``; return ``{name: text}`` or None."""
    if style is None:
        return None
    out: dict[str, str] = {}
    for local in fields:
        node = find_child(style, local)
        if node is not None and node.text is not None:
            out[local] = node.text
    return out or None


def _run_style_dict(run: etree._Element) -> Optional[dict[str, str]]:
    """Style fields that live on a ``TextRun/Style``."""
    return _capture_style_fields(find_child(run, "Style"), _RUN_STYLE_FIELDS)


def _paragraph_style_dict(paragraph: etree._Element) -> Optional[dict[str, str]]:
    """Style fields that live on a ``Paragraph/Style``."""
    return _capture_style_fields(find_child(paragraph, "Style"), _PARAGRAPH_STYLE_FIELDS)


def _border_dict(style: Optional[etree._Element]) -> Optional[dict[str, str]]:
    """Capture the simple ``Border`` sub-element. Per-side borders
    (``TopBorder`` / etc.) are reported separately if useful later."""
    if style is None:
        return None
    border = find_child(style, "Border")
    return _capture_style_fields(border, _BORDER_STYLE_FIELDS) if border is not None else None


def _runs_of(textbox: etree._Element) -> list[dict[str, Any]]:
    """Enumerate text runs as ``{value, style}`` so callers see the same
    per-run style fields ``set_textbox_style`` writes (font / color / format)."""
    runs: list[dict[str, Any]] = []
    paragraphs_root = find_child(textbox, "Paragraphs")
    paragraphs = find_children(paragraphs_root, "Paragraph") if paragraphs_root is not None else []
    for paragraph in paragraphs:
        runs_root = find_child(paragraph, "TextRuns")
        if runs_root is None:
            continue
        for run in find_children(runs_root, "TextRun"):
            entry: dict[str, Any] = {"value": _text(find_child(run, "Value"))}
            run_style = _run_style_dict(run)
            if run_style is not None:
                entry["style"] = run_style
            runs.append(entry)
    return runs


def _full_textbox_style(textbox: etree._Element) -> Optional[dict[str, Any]]:
    """Return a nested style dict matching the ``set_textbox_style`` routing.

    Shape::

        {
          "box":       {BackgroundColor, VerticalAlign, Padding*, ...},
          "border":    {Style, Color, Width},
          "paragraph": {TextAlign},
          "run":       {FontFamily, FontSize, FontWeight, Color, Format, ...},
        }

    Empty branches are dropped. Returns ``None`` when nothing of interest is
    present so callers can treat it like the previous flat-or-None shape.

    Paragraph and run capture *only the first* paragraph / first run — the
    same behavior ``set_textbox_style`` uses for writes.
    """
    box = _capture_style_fields(find_child(textbox, "Style"), _BOX_STYLE_FIELDS)
    border = _border_dict(find_child(textbox, "Style"))

    paragraph_style: Optional[dict[str, str]] = None
    run_style: Optional[dict[str, str]] = None
    paragraphs_root = find_child(textbox, "Paragraphs")
    if paragraphs_root is not None:
        first_paragraph = find_child(paragraphs_root, "Paragraph")
        if first_paragraph is not None:
            paragraph_style = _paragraph_style_dict(first_paragraph)
            runs_root = find_child(first_paragraph, "TextRuns")
            if runs_root is not None:
                first_run = find_child(runs_root, "TextRun")
                if first_run is not None:
                    run_style = _run_style_dict(first_run)

    out: dict[str, Any] = {}
    if box:
        out["box"] = box
    if border:
        out["border"] = border
    if paragraph_style:
        out["paragraph"] = paragraph_style
    if run_style:
        out["run"] = run_style
    return out or None


def _style_dict(el: Optional[etree._Element]) -> Optional[dict[str, Any]]:
    """Capture key Style children. Best-effort; returns None if no style.

    Used by non-Textbox readers (Image, Rectangle) where there's only one
    Style block to look at — fall back to a flat field dump.
    """
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


def _is_positioned_item(el: etree._Element) -> bool:
    """True when ``el`` is a top-level positioned ReportItem (body / header /
    footer / rectangle child), not a tablix-cell textbox.

    Cell textboxes live under ``TablixCell/CellContents`` and have no
    ``Top`` / ``Left`` / ``Width`` / ``Height`` of their own — the cell
    positions them. Top-level items always carry at least Width or Height.
    Use that as the signal so we don't accidentally coerce cell-textbox
    layout fields into "0in".
    """
    return find_child(el, "Width") is not None or find_child(el, "Height") is not None


def _layout_dict_for(el: etree._Element) -> dict[str, Optional[str]]:
    """Top/Left/Width/Height with the right coercion for the item's container.

    Top-level positioned items: missing Top/Left coerce to "0in" (RDL
    semantic default). Cell textboxes: all four fields stay None.
    """
    if _is_positioned_item(el):
        return {
            "top": _text(find_child(el, "Top")) or "0in",
            "left": _text(find_child(el, "Left")) or "0in",
            "width": _text(find_child(el, "Width")),
            "height": _text(find_child(el, "Height")),
        }
    return {
        "top": _text(find_child(el, "Top")),
        "left": _text(find_child(el, "Left")),
        "width": _text(find_child(el, "Width")),
        "height": _text(find_child(el, "Height")),
    }


def get_textbox(path: str, name: str) -> dict[str, Any]:
    """Return position, size, runs, style, visibility for a named Textbox.

    Searches the entire report (body, header, footer, AND tablix cell
    contents). Tablix-cell textboxes don't have top/left/width/height —
    those fields are returned as None.

    ``style`` is a nested dict mirroring how ``set_textbox_style`` routes
    properties: ``{"box": {...}, "border": {...}, "paragraph": {...},
    "run": {...}}``. Empty branches are dropped. ``runs`` entries each
    carry their own ``style`` dict.
    """
    doc = RDLDocument.open(path)
    tb = _find_named_anywhere(doc, "Textbox", name)
    if tb is None:
        from pbirb_mcp.core.ids import ElementNotFoundError

        raise ElementNotFoundError(f"no Textbox named {name!r}")
    return {
        "name": name,
        "type": "Textbox",
        **_layout_dict_for(tb),
        "runs": _runs_of(tb),
        "style": _full_textbox_style(tb),
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
        **_layout_dict_for(img),
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
        **_layout_dict_for(rect),
        "contained_items": contained,
        "style": _style_dict(rect),
        "visibility": _visibility(rect),
    }


# ---- get_chart ------------------------------------------------------------


def _chart_series_dict(series: etree._Element) -> dict[str, Any]:
    """Return the read-back shape for one ``<ChartSeries>``: name, type,
    subtype, value/category expressions on the (single) data point, plus
    style color when set explicitly via ``<Style>/<Color>``.
    """
    data_points = find_child(series, "ChartDataPoints")
    point = find_child(data_points, "ChartDataPoint") if data_points is not None else None
    values = find_child(point, "ChartDataPointValues") if point is not None else None
    y = find_child(values, "Y") if values is not None else None
    x = find_child(values, "X") if values is not None else None

    style = find_child(series, "Style")
    color = _text(find_child(style, "Color")) if style is not None else None

    return {
        "name": series.get("Name"),
        "type": _text(find_child(series, "Type")),
        "subtype": _text(find_child(series, "Subtype")),
        "value_expression": _text(y),
        "category_expression": _text(x),
        "color": color,
    }


def _chart_axis_dict(axis: etree._Element) -> dict[str, Any]:
    """Read-back shape for ``<ChartAxis>``: name + title + min/max/format
    pulled out of the canonical sub-elements emitted by
    :func:`set_chart_axis`. The title element is ``<ChartAxisTitle>``
    in RDL 2016; pre-v0.3.1 mistakenly wrote ``<Title>`` which RB
    rejects, so we read both names for backward-compat with files
    written before the migration ran."""
    title_node = find_child(axis, "ChartAxisTitle")
    if title_node is None:
        title_node = find_child(axis, "Title")
    title_caption = _text(find_child(title_node, "Caption")) if title_node is not None else None
    min_node = find_child(axis, "Minimum")
    max_node = find_child(axis, "Maximum")
    interval = find_child(axis, "Interval")
    visible_node = find_child(axis, "Visible")
    log_node = find_child(axis, "LogScale")
    format_node = None
    style = find_child(axis, "Style")
    if style is not None:
        format_node = find_child(style, "Format")
    return {
        "name": axis.get("Name"),
        "title": title_caption,
        "min": _text(min_node),
        "max": _text(max_node),
        "interval": _text(interval),
        "visible": _text(visible_node),
        "log_scale": _text(log_node),
        "format": _text(format_node),
    }


def _chart_legend_dict(chart: etree._Element) -> Optional[dict[str, Any]]:
    legends_root = find_child(chart, "ChartLegends")
    if legends_root is None:
        return None
    legend = find_child(legends_root, "ChartLegend")
    if legend is None:
        return None
    # set_chart_legend writes <Hidden>true|false</Hidden> as the inverse
    # of `visible` (visible=True → Hidden=false). Echo the inverse on
    # read so the field name matches the writer's input arg semantics:
    # set_chart_legend(visible=true) ⇒ get_chart().legend.visible == "true".
    hidden_text = _text(find_child(legend, "Hidden"))
    if hidden_text is None:
        visible_text: Optional[str] = None
    else:
        visible_text = "false" if hidden_text.strip().lower() == "true" else "true"
    return {
        "name": legend.get("Name"),
        "position": _text(find_child(legend, "DockOutsideChartArea"))
        or _text(find_child(legend, "Position")),
        "visible": visible_text,
    }


def _chart_title_dict(chart: etree._Element) -> Optional[dict[str, Any]]:
    titles_root = find_child(chart, "ChartTitles")
    if titles_root is None:
        return None
    title = find_child(titles_root, "ChartTitle")
    if title is None:
        return None
    return {
        "name": title.get("Name"),
        "caption": _text(find_child(title, "Caption")),
    }


def _chart_category_groups(chart: etree._Element) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cat_h = find_child(chart, "ChartCategoryHierarchy")
    members_root = find_child(cat_h, "ChartMembers") if cat_h is not None else None
    if members_root is None:
        return out
    for member in find_children(members_root, "ChartMember"):
        group = find_child(member, "Group")
        label = _text(find_child(member, "Label"))
        if group is None:
            out.append({"name": None, "expression": None, "label": label})
            continue
        expr_root = find_child(group, "GroupExpressions")
        expressions: list[str] = []
        if expr_root is not None:
            for e in find_children(expr_root, "GroupExpression"):
                if e.text:
                    expressions.append(e.text)
        out.append(
            {
                "name": group.get("Name"),
                "expression": expressions[0] if expressions else None,
                "label": label,
            }
        )
    return out


def get_chart(path: str, name: str) -> dict[str, Any]:
    """Read-back for a named ``<Chart>``: position, size, dataset, series,
    category groups, axes, legend, title, palette.

    Symmetric with :func:`get_textbox` / :func:`get_image` /
    :func:`get_rectangle`. Returns shape::

        {
          "name": "...",
          "type": "Chart",
          "top": "..", "left": "..", "width": "..", "height": "..",
          "dataset": "..",
          "palette": "EarthTones" | None,
          "series": [{name, type, subtype, value_expression, ...}, ...],
          "category_groups": [{name, expression, label}, ...],
          "axes": {"category": [...], "value": [...]},
          "legend": {name, position, visible} | None,
          "title": {name, caption} | None,
          "style": {...} | None,
          "visibility": {...} | None,
        }
    """
    doc = RDLDocument.open(path)
    chart = _find_named_anywhere(doc, "Chart", name)
    if chart is None:
        from pbirb_mcp.core.ids import ElementNotFoundError

        raise ElementNotFoundError(f"no Chart named {name!r}")

    # Series collection.
    series_collection = chart.find(f"{{{RDL_NS}}}ChartData/{{{RDL_NS}}}ChartSeriesCollection")
    series_list: list[dict[str, Any]] = []
    if series_collection is not None:
        for s in find_children(series_collection, "ChartSeries"):
            series_list.append(_chart_series_dict(s))

    # Axes (Default ChartArea only).
    chart_area = chart.find(f"{{{RDL_NS}}}ChartAreas/{{{RDL_NS}}}ChartArea")
    cat_axes: list[dict[str, Any]] = []
    val_axes: list[dict[str, Any]] = []
    if chart_area is not None:
        cat_axes_root = find_child(chart_area, "ChartCategoryAxes")
        if cat_axes_root is not None:
            for a in find_children(cat_axes_root, "ChartAxis"):
                cat_axes.append(_chart_axis_dict(a))
        val_axes_root = find_child(chart_area, "ChartValueAxes")
        if val_axes_root is not None:
            for a in find_children(val_axes_root, "ChartAxis"):
                val_axes.append(_chart_axis_dict(a))

    return {
        "name": name,
        "type": "Chart",
        **_layout_dict_for(chart),
        "dataset": _text(find_child(chart, "DataSetName")),
        "palette": _text(find_child(chart, "Palette")),
        "series": series_list,
        "category_groups": _chart_category_groups(chart),
        "axes": {"category": cat_axes, "value": val_axes},
        "legend": _chart_legend_dict(chart),
        "title": _chart_title_dict(chart),
        "style": _style_dict(chart),
        "visibility": _visibility(chart),
    }


# ---- find_textboxes_by_style ---------------------------------------------


# Filter kwargs accepted by find_textboxes_by_style. Each entry maps a
# user-facing kwarg name to (style_level, RDL local name).
#
# Box-level filters route to <Textbox/Style>/X; paragraph filters to
# <Paragraph/Style>/X; run filters to the FIRST <TextRun/Style>/X.
_FILTER_LOCATIONS: dict[str, tuple[str, str]] = {
    "background_color": ("box", "BackgroundColor"),
    "vertical_align": ("box", "VerticalAlign"),
    "writing_mode": ("box", "WritingMode"),
    "padding_top": ("box", "PaddingTop"),
    "padding_bottom": ("box", "PaddingBottom"),
    "padding_left": ("box", "PaddingLeft"),
    "padding_right": ("box", "PaddingRight"),
    "border_style": ("border", "Style"),
    "border_color": ("border", "Color"),
    "border_width": ("border", "Width"),
    "text_align": ("paragraph", "TextAlign"),
    "font_family": ("run", "FontFamily"),
    "font_size": ("run", "FontSize"),
    "font_weight": ("run", "FontWeight"),
    "font_style": ("run", "FontStyle"),
    "color": ("run", "Color"),
    "format": ("run", "Format"),
    "text_decoration": ("run", "TextDecoration"),
}


def _textbox_style_field_value(textbox: etree._Element, level: str, local: str) -> Optional[str]:
    """Read the effective value of one Style field on a textbox at the
    given level (box / border / paragraph / run). Returns ``None`` when
    absent."""
    if level == "box":
        style = find_child(textbox, "Style")
        if style is None:
            return None
        node = find_child(style, local)
        return node.text if node is not None and node.text is not None else None
    if level == "border":
        style = find_child(textbox, "Style")
        if style is None:
            return None
        border = find_child(style, "Border")
        if border is None:
            return None
        node = find_child(border, local)
        return node.text if node is not None and node.text is not None else None
    if level == "paragraph":
        paragraphs_root = find_child(textbox, "Paragraphs")
        if paragraphs_root is None:
            return None
        paragraph = find_child(paragraphs_root, "Paragraph")
        if paragraph is None:
            return None
        style = find_child(paragraph, "Style")
        if style is None:
            return None
        node = find_child(style, local)
        return node.text if node is not None and node.text is not None else None
    if level == "run":
        paragraphs_root = find_child(textbox, "Paragraphs")
        if paragraphs_root is None:
            return None
        paragraph = find_child(paragraphs_root, "Paragraph")
        if paragraph is None:
            return None
        runs_root = find_child(paragraph, "TextRuns")
        if runs_root is None:
            return None
        run = find_child(runs_root, "TextRun")
        if run is None:
            return None
        style = find_child(run, "Style")
        if style is None:
            return None
        node = find_child(style, local)
        return node.text if node is not None and node.text is not None else None
    return None


def _textbox_location(textbox: etree._Element) -> str:
    """Best-effort short string describing where a textbox lives:
    'body' / 'header' / 'footer' / 'tablix:<TablixName>' / 'rectangle' /
    'unknown'. Used only by find_textboxes_by_style results — not a
    structural API."""
    ancestor = textbox.getparent()
    while ancestor is not None:
        tag = etree.QName(ancestor).localname
        if tag == "Tablix":
            return f"tablix:{ancestor.get('Name')}"
        if tag == "Rectangle":
            return f"rectangle:{ancestor.get('Name')}"
        if tag == "PageHeader":
            return "header"
        if tag == "PageFooter":
            return "footer"
        if tag == "Body":
            return "body"
        ancestor = ancestor.getparent()
    return "unknown"


def find_textboxes_by_style(
    path: str,
    *,
    background_color: Optional[str] = None,
    vertical_align: Optional[str] = None,
    writing_mode: Optional[str] = None,
    padding_top: Optional[str] = None,
    padding_bottom: Optional[str] = None,
    padding_left: Optional[str] = None,
    padding_right: Optional[str] = None,
    border_style: Optional[str] = None,
    border_color: Optional[str] = None,
    border_width: Optional[str] = None,
    text_align: Optional[str] = None,
    font_family: Optional[str] = None,
    font_size: Optional[str] = None,
    font_weight: Optional[str] = None,
    font_style: Optional[str] = None,
    color: Optional[str] = None,
    format: Optional[str] = None,  # noqa: A002 - tool-facing
    text_decoration: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Search for textboxes matching one or more style filters.

    Each filter is optional; supplied filters AND together — a textbox
    must match every supplied filter to appear in the result. Useful as
    a discovery step before :func:`set_textbox_style_bulk`.

    Returns a list of ``{name, location, matched_fields: dict[str,str]}``
    where ``location`` is best-effort (``body`` / ``header`` / ``footer``
    / ``tablix:<name>`` / ``rectangle:<name>`` / ``unknown``) and
    ``matched_fields`` lists the filtered kwargs and their actual values
    on that textbox (always equal to the supplied filter — included so
    callers can confirm the match shape).

    Returns ``[]`` when no filters are supplied (refusing to match
    everything would be a footgun, since the caller almost certainly
    wants to feed the result into ``set_textbox_style_bulk``).
    """
    filters: dict[str, str] = {}
    for kwarg in _FILTER_LOCATIONS:
        v = locals().get(kwarg)
        if v is not None:
            filters[kwarg] = v
    if not filters:
        return []

    doc = RDLDocument.open(path)
    out: list[dict[str, Any]] = []
    for textbox in doc.root.iter(f"{{{RDL_NS}}}Textbox"):
        name = textbox.get("Name")
        if name is None:
            continue
        matched: dict[str, str] = {}
        all_match = True
        for kwarg, expected in filters.items():
            level, local = _FILTER_LOCATIONS[kwarg]
            actual = _textbox_style_field_value(textbox, level, local)
            if actual != expected:
                all_match = False
                break
            matched[kwarg] = actual
        if all_match:
            out.append(
                {
                    "name": name,
                    "location": _textbox_location(textbox),
                    "matched_fields": matched,
                }
            )
    return out


# ---- find_textbox_by_value (Phase 12 commit 44) -------------------------


def find_textbox_by_value(path: str, pattern: str) -> list[dict[str, Any]]:
    """Return every ``Textbox`` whose ``<Value>`` text matches ``pattern``
    (a Python regex, not RDL/SQL glob).

    Searches the value text of each ``<TextRun>`` in every textbox under
    ``<Body>`` / ``<PageHeader>`` / ``<PageFooter>``. A textbox with
    multiple matching runs surfaces once per matching run.

    Returns ``[{textbox, value, region}]`` where ``region`` is one of
    ``Body``, ``PageHeader``, ``PageFooter`` (the nearest top-level
    ancestor).

    Useful for finding stale ``=Parameters!Old.Value`` references after
    ``rename_parameter``, or any other cross-cutting expression edit.
    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex pattern {pattern!r}: {exc}") from exc

    doc = RDLDocument.open(path)
    out: list[dict[str, Any]] = []

    for tb in doc.root.iter(q("Textbox")):
        name = tb.get("Name")
        if not name:
            continue
        # Walk all <Value> elements under this textbox's <Paragraphs>.
        for value in tb.iter(q("Value")):
            text = value.text or ""
            if not regex.search(text):
                continue
            out.append(
                {
                    "textbox": name,
                    "value": text,
                    "region": _region_of(tb),
                }
            )
    return out


def _region_of(elem: etree._Element) -> str:
    """Return the nearest ``Body`` / ``PageHeader`` / ``PageFooter`` /
    ``CellContents`` ancestor's local name, or ``unknown``."""
    cur = elem.getparent()
    while cur is not None:
        local = etree.QName(cur.tag).localname
        if local in ("Body", "PageHeader", "PageFooter"):
            return local
        cur = cur.getparent()
    return "unknown"


__all__ = [
    "describe_report",
    "find_textbox_by_value",
    "find_textboxes_by_style",
    "get_chart",
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
