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

from pbirb_mcp.core import chart_shape, chart_types, style_values
from pbirb_mcp.core.columns import describe_columns
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.geometry import PAGE_DEFAULTS, REPORT_ITEM_TAGS, size_unit
from pbirb_mcp.core.tablix_shape import is_detail_group, is_dynamic_group
from pbirb_mcp.core.units import format_size, to_inches
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, find_child, find_children, q, qrd
from pbirb_mcp.ops.dataset import dataset_to_dict
from pbirb_mcp.ops.parameters import _cell_parameter_name, _cell_slot
from pbirb_mcp.ops.styling import _resolve_row_index
from pbirb_mcp.ops.tablix import read_sort


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


def _tablix_summary(tablix: etree._Element) -> dict[str, Any]:
    """Compact shape hint for ``describe_report.tablixes``.

    Returns ``{name, rows, columns, has_groups, has_subtotals, has_spans,
    column_details}`` — just enough to let a planner pick between ``style_tablix_row`` /
    ``set_textbox_style`` / ``add_subtotal_row`` without first calling
    ``get_tablixes``.

    Heuristics:
    * ``rows``/``columns`` — direct counts of ``<TablixRow>`` and
      ``<TablixColumn>`` inside ``<TablixBody>``.
    * ``has_groups`` — True iff any dynamic ``<Group>`` (one with
      ``<GroupExpressions>``) exists in either the row or column
      hierarchy. The detail group (no ``<GroupExpressions>``) does NOT
      count, whatever it is named — ``Details``, ``Details1``, ... (it
      ships in every template-inserted tablix).
    * ``has_subtotals`` — True iff any static ``<TablixMember>`` (no
      ``<Group>`` child) is nested INSIDE another member's
      ``<TablixMembers>`` that DOES have a ``<Group>``. That's exactly
      the shape ``add_subtotal_row`` / ``add_subtotal_column`` create.
    * ``has_spans`` — True iff any ``<ColSpan>`` or ``<RowSpan>``
      element exists anywhere inside the tablix.
    * ``column_details`` (#59) — one ``{index, header_text, header_cell,
      data_cell, bound_fields}`` per body column
      (:func:`pbirb_mcp.core.columns.describe_columns`), so "the Asset
      Name column" maps to a column index and cell textbox names.
    """
    name = tablix.get("Name")
    body = find_child(tablix, "TablixBody")
    rows = 0
    columns = 0
    if body is not None:
        rows_root = find_child(body, "TablixRows")
        cols_root = find_child(body, "TablixColumns")
        if rows_root is not None:
            rows = sum(1 for _ in find_children(rows_root, "TablixRow"))
        if cols_root is not None:
            columns = sum(1 for _ in find_children(cols_root, "TablixColumn"))

    has_groups = False
    for hierarchy_local in ("TablixRowHierarchy", "TablixColumnHierarchy"):
        h = find_child(tablix, hierarchy_local)
        if h is None:
            continue
        for group in h.iter(f"{{{RDL_NS}}}Group"):
            if is_dynamic_group(group):
                has_groups = True
                break
        if has_groups:
            break

    has_subtotals = False
    for hierarchy_local in ("TablixRowHierarchy", "TablixColumnHierarchy"):
        h = find_child(tablix, hierarchy_local)
        if h is None:
            continue
        for outer_member in h.iter(f"{{{RDL_NS}}}TablixMember"):
            if find_child(outer_member, "Group") is None:
                continue
            inner_members = find_child(outer_member, "TablixMembers")
            if inner_members is None:
                continue
            for child in find_children(inner_members, "TablixMember"):
                if find_child(child, "Group") is None:
                    has_subtotals = True
                    break
            if has_subtotals:
                break
        if has_subtotals:
            break

    has_spans = any(
        e is not None
        for e in (
            next(iter(tablix.iter(f"{{{RDL_NS}}}ColSpan")), None),
            next(iter(tablix.iter(f"{{{RDL_NS}}}RowSpan")), None),
        )
    )

    return {
        "name": name,
        "rows": rows,
        "columns": columns,
        "has_groups": has_groups,
        "has_subtotals": has_subtotals,
        "has_spans": has_spans,
        "column_details": [c.as_dict() for c in describe_columns(tablix)],
    }


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


# describe_report.page keys -> <Page> child element.
_PAGE_FIELDS = (
    ("height", "PageHeight"),
    ("width", "PageWidth"),
    ("margin_top", "TopMargin"),
    ("margin_bottom", "BottomMargin"),
    ("margin_left", "LeftMargin"),
    ("margin_right", "RightMargin"),
)


def _page_summary(page_node: Optional[etree._Element], body: dict[str, Any]) -> dict[str, Any]:
    """``describe_report.page``: page size and margins as written, with the
    RDL default (what Report Builder renders: 11in x 8.5in, 0in margins)
    filled in for every absent element. ``defaulted`` lists the keys of
    those six that came from a default rather than the file.

    #49 adds what an overflow check needs: ``columns`` / ``column_spacing``
    (defaults 1 / 0.5in), ``body`` ({width, height}, as in
    ``describe_report.body``) and ``printable_width`` — page width minus
    the left and right margins, in the page width's unit (``None`` when a
    size doesn't parse)."""
    page: dict[str, Any] = {}
    defaulted: list[str] = []
    for key, local in _PAGE_FIELDS:
        value = _text(find_child(page_node, local)) if page_node is not None else None
        if value is None:
            value = PAGE_DEFAULTS[local]
            defaulted.append(key)
        page[key] = value
    page["defaulted"] = defaulted
    columns_text = _text(find_child(page_node, "Columns")) if page_node is not None else None
    try:
        page["columns"] = max(1, int(columns_text)) if columns_text else 1
    except ValueError:
        page["columns"] = columns_text
    page["column_spacing"] = (
        _text(find_child(page_node, "ColumnSpacing")) if page_node is not None else None
    ) or PAGE_DEFAULTS["ColumnSpacing"]
    page["body"] = dict(body)
    try:
        printable = (
            to_inches(page["width"])
            - to_inches(page["margin_left"])
            - to_inches(page["margin_right"])
        )
        page["printable_width"] = format_size(round(printable, 5), size_unit(page["width"]))
    except ValueError:
        page["printable_width"] = None
    return page


def _body_summary(body_node: Optional[etree._Element]) -> dict[str, Any]:
    """``{height, width}`` of the first ReportSection's body: ``height`` is
    ``<Body>/<Height>``, ``width`` is ``<ReportSection>/<Width>`` (RDL keeps
    the body width on the section). ``None`` for an absent element."""
    if body_node is None:
        return {"height": None, "width": None}
    section = body_node.getparent()
    return {
        "height": _text(find_child(body_node, "Height")),
        "width": _text(find_child(section, "Width")) if section is not None else None,
    }


def _subreport_summary(subreport: etree._Element) -> dict[str, Any]:
    """``{name, report_name, parameters: [{name, value}]}`` for a <Subreport>."""
    params_root = find_child(subreport, "Parameters")
    params = find_children(params_root, "Parameter") if params_root is not None else []
    return {
        "name": subreport.get("Name"),
        "report_name": _text(find_child(subreport, "ReportName")),
        "parameters": [
            {"name": p.get("Name"), "value": _text(find_child(p, "Value"))} for p in params
        ],
    }


def describe_report(path: str) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    root = doc.root

    page_node = root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Page")
    body_node = root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body")
    body = _body_summary(body_node)
    page = _page_summary(page_node, body)

    header_node = page_node.find(f"{{{RDL_NS}}}PageHeader") if page_node is not None else None
    footer_node = page_node.find(f"{{{RDL_NS}}}PageFooter") if page_node is not None else None

    return {
        "path": str(doc.path),
        "data_sources": [ds.get("Name") for ds in root.iter(f"{{{RDL_NS}}}DataSource")],
        "datasets": [ds.get("Name") for ds in root.iter(f"{{{RDL_NS}}}DataSet")],
        "parameters": [p.get("Name") for p in root.iter(f"{{{RDL_NS}}}ReportParameter")],
        # v0.4 BREAKING — tablixes is now [{name, rows, columns,
        # has_groups, has_subtotals, has_spans}, ...] (was [name, ...]).
        # Migration: `[t["name"] for t in out["tablixes"]]` recovers
        # the old shape. The richer shape lets planners pick between
        # style_tablix_row / set_textbox_style / add_subtotal_row
        # without an intermediate get_tablixes call.
        "tablixes": [_tablix_summary(t) for t in root.iter(f"{{{RDL_NS}}}Tablix")],
        # v0.4: top-level charts array — parity with tablixes. Saves a
        # filter step ("body_items where type==Chart") when the LLM is
        # planning a chart-targeted edit.
        "charts": [c.get("Name") for c in root.iter(f"{{{RDL_NS}}}Chart")],
        # v0.2: full body / header / footer item enumeration so the LLM can
        # see every named ReportItem (Tablix, Textbox, Image, Rectangle, etc.)
        # without a follow-up list_*_items call.
        "body_items": _list_items_in(body_node),
        "header_items": _list_items_in(header_node),
        "footer_items": _list_items_in(footer_node),
        "page": page,
        # #52: items inside Rectangles (body_items lists the top level
        # only), the body's own size, and subreport / map / gauge
        # inventories (subreports matter for dependency checks).
        "nested_items": _nested_items(
            [("body", body_node), ("header", header_node), ("footer", footer_node)]
        ),
        "body": body,
        "subreports": [_subreport_summary(s) for s in root.iter(q("Subreport"))],
        "maps": [m.get("Name") for m in root.iter(q("Map"))],
        "gauges": [g.get("Name") for g in root.iter(q("GaugePanel"))],
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
    """Every ``<DataSet>``, each entry exactly what ``get_dataset`` returns
    for it (#97: built by the same :func:`dataset.dataset_to_dict`)."""
    doc = RDLDocument.open(path)
    return [dataset_to_dict(ds) for ds in doc.root.iter(f"{{{RDL_NS}}}DataSet")]


# ---- get_parameters --------------------------------------------------------


def _dataset_reference_dict(ref: etree._Element, *, with_label: bool) -> dict[str, Any]:
    out = {
        "source": "query",
        "dataset": _text(find_child(ref, "DataSetName")),
        "value_field": _text(find_child(ref, "ValueField")),
    }
    if with_label:
        out["label_field"] = _text(find_child(ref, "LabelField"))
    return out


def _parameter_default_values(parameter: etree._Element) -> Optional[dict[str, Any]]:
    """``{source: 'static', values}`` / ``{source: 'query', dataset,
    value_field}`` for ``<DefaultValue>``; ``None`` when absent."""
    default_value = find_child(parameter, "DefaultValue")
    if default_value is None:
        return None
    ref = find_child(default_value, "DataSetReference")
    if ref is not None:
        return _dataset_reference_dict(ref, with_label=False)
    values_root = find_child(default_value, "Values")
    values = (
        [_text(v) for v in find_children(values_root, "Value")] if values_root is not None else []
    )
    return {"source": "static", "values": values}


def _parameter_valid_values(parameter: etree._Element) -> Optional[dict[str, Any]]:
    """``{source: 'static', values: [{value, label}]}`` / ``{source: 'query',
    dataset, value_field, label_field}`` for ``<ValidValues>``; ``None``
    when absent."""
    valid_values = find_child(parameter, "ValidValues")
    if valid_values is None:
        return None
    ref = find_child(valid_values, "DataSetReference")
    if ref is not None:
        return _dataset_reference_dict(ref, with_label=True)
    pvs = find_child(valid_values, "ParameterValues")
    values = [
        {
            "value": _text(find_child(pv, "Value")),
            "label": _text(find_child(pv, "Label")),
        }
        for pv in (find_children(pvs, "ParameterValue") if pvs is not None else [])
    ]
    return {"source": "static", "values": values}


def _parameter_layout_cells(root: etree._Element) -> dict[str, dict[str, Optional[int]]]:
    """Parameter name -> ``{row, column}`` of its first ``<CellDefinition>``
    in ``<ReportParametersLayout>`` (0-based, as RDL stores them)."""
    layout = find_child(root, "ReportParametersLayout")
    grid = find_child(layout, "GridLayoutDefinition") if layout is not None else None
    cells_root = find_child(grid, "CellDefinitions") if grid is not None else None
    cells: dict[str, dict[str, Optional[int]]] = {}
    if cells_root is None:
        return cells
    for cell in find_children(cells_root, "CellDefinition"):
        name = _cell_parameter_name(cell)
        if name is not None and name not in cells:
            row, column = _cell_slot(cell)
            cells[name] = {"row": row, "column": column}
    return cells


def get_parameters(path: str) -> list[dict[str, Any]]:
    """Every ``<ReportParameter>``: type, prompt, flags, plus (#97) its
    ``default_values``, ``valid_values`` and parameter-pane ``layout``
    cell (each ``None`` when not set)."""
    doc = RDLDocument.open(path)
    layout_cells = _parameter_layout_cells(doc.root)
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
                "default_values": _parameter_default_values(p),
                "valid_values": _parameter_valid_values(p),
                "layout": layout_cells.get(p.get("Name")),
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
    """The tablix's own ``<SortExpressions>`` values (group sorts are on
    their members in ``row_members`` / ``column_members``)."""
    return [s["value"] for s in read_sort(tablix) if s["value"]]


def _member_kind(member: etree._Element) -> str:
    group = find_child(member, "Group")
    if group is None:
        return "static"
    return "details" if is_detail_group(group) else "group"


def _page_break(group: Optional[etree._Element]) -> Optional[str]:
    node = find_child(group, "PageBreak") if group is not None else None
    return _text(find_child(node, "BreakLocation")) if node is not None else None


def _header_cell(header: etree._Element) -> dict[str, Any]:
    tb = header.find(f"{q('CellContents')}/{q('Textbox')}")
    return {
        "textbox_name": tb.get("Name") if tb is not None else None,
        "size": _text(find_child(header, "Size")),
    }


def _member_tree(tablix: etree._Element, axis: str) -> tuple[list, list, list]:
    """``(members, leaves, header_cells)`` for one hierarchy axis.

    ``members`` is the nested member tree; ``leaves`` one entry per body
    row/column ``{index, path, kind, groups}`` (``path``: child positions
    from the top level); ``header_cells`` the ``TablixHeader`` textboxes
    ``{axis, level, group, textbox_name, size, leaves: [first, last]}``,
    ``level`` counting header-carrying ancestors (0 = outermost).
    """
    hierarchy = find_child(tablix, f"Tablix{axis.capitalize()}Hierarchy")
    top = find_child(hierarchy, "TablixMembers") if hierarchy is not None else None
    leaves: list[dict[str, Any]] = []
    headers: list[dict[str, Any]] = []

    def walk(member: etree._Element, path: list[int], groups: list[str], level: int) -> dict:
        group = find_child(member, "Group")
        name = group.get("Name") if group is not None else None
        groups_here = groups + [name] if name else groups
        repeat = (_text(find_child(member, "RepeatOnNewPage")) or "").strip().lower()
        node: dict[str, Any] = {
            "group": name,
            "kind": _member_kind(member),
            "expressions": _group_dict(group)["expressions"] if group is not None else [],
            "keep_with_group": _text(find_child(member, "KeepWithGroup")),
            "repeat_on_new_page": repeat == "true",
            "visibility": _visibility(member),
            "sort": read_sort(member),
            "page_break": _page_break(group),
        }
        first_leaf = len(leaves)
        header = find_child(member, "TablixHeader")
        header_entry = None
        if header is not None:
            node["header"] = _header_cell(header)
            header_entry = {"axis": axis, "level": level, "group": name, **node["header"]}
            headers.append(header_entry)
        members_root = find_child(member, "TablixMembers")
        children = find_children(members_root, "TablixMember") if members_root is not None else []
        next_level = level + (1 if header is not None else 0)
        node["children"] = [
            walk(child, path + [i], groups_here, next_level) for i, child in enumerate(children)
        ]
        if not children:
            node["leaf_index"] = len(leaves)
            leaves.append(
                {"index": len(leaves), "path": path, "kind": node["kind"], "groups": groups_here}
            )
        if header_entry is not None:
            header_entry["leaves"] = [first_leaf, len(leaves) - 1]
        return node

    members = []
    if top is not None:
        members = [walk(m, [i], [], 0) for i, m in enumerate(find_children(top, "TablixMember"))]
    return members, leaves, headers


def _row_roles(tablix: etree._Element, row_groups: list[str]) -> dict[int, list[str]]:
    """Body row index → the ``style_tablix_row`` role names resolving to it."""
    candidates = ["header", "details"]
    for name in row_groups:
        candidates += [f"{name}_header", f"{name}_footer"]
    roles: dict[int, list[str]] = {}
    for role in candidates:
        try:
            idx = _resolve_row_index(tablix, role)
        except (LookupError, ValueError, TypeError):
            continue
        roles.setdefault(idx, []).append(role)
    return roles


def _corner_cells(tablix: etree._Element) -> list[dict[str, Any]]:
    corner = find_child(tablix, "TablixCorner")
    rows_root = find_child(corner, "TablixCornerRows") if corner is not None else None
    if rows_root is None:
        return []
    grid = [
        find_children(row, "TablixCornerCell")
        for row in find_children(rows_root, "TablixCornerRow")
    ]
    return _grid_cells(grid)


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


def _cell_span(cell: etree._Element, local: str) -> int:
    """``ColSpan`` / ``RowSpan`` of a cell: on ``<CellContents>`` (RDL
    2010+), else on the cell itself (pre-2010 layout); 1 when absent or
    unparsable."""
    contents = find_child(cell, "CellContents")
    node = find_child(contents, local) if contents is not None else None
    if node is None:
        node = find_child(cell, local)
    try:
        value = int((_text(node) or "").strip())
    except ValueError:
        return 1
    return max(value, 1)


def _grid_cells(grid: list[list[etree._Element]]) -> list[dict[str, Any]]:
    """``{row, col, textbox_name, row_span, col_span}`` per cell of a body
    or corner grid; a cell inside another cell's span also gets
    ``covered_by: [row, col]`` (the span's owner). ``textbox_name`` is
    None for a placeholder or a cell holding another report item."""
    covered: dict[tuple[int, int], list[int]] = {}
    spans: dict[tuple[int, int], tuple[int, int]] = {}
    for r, cells in enumerate(grid):
        for c, cell in enumerate(cells):
            if find_child(cell, "CellContents") is None:
                continue
            rs, cs = _cell_span(cell, "RowSpan"), _cell_span(cell, "ColSpan")
            spans[(r, c)] = (rs, cs)
            if (r, c) in covered:
                continue
            for dr in range(rs):
                for dc in range(cs):
                    if dr or dc:
                        covered.setdefault((r + dr, c + dc), [r, c])
    out: list[dict[str, Any]] = []
    for r, cells in enumerate(grid):
        for c, cell in enumerate(cells):
            tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
            rs, cs = spans.get((r, c), (1, 1))
            entry: dict[str, Any] = {
                "row": r,
                "col": c,
                "textbox_name": tb.get("Name") if tb is not None else None,
                "row_span": rs,
                "col_span": cs,
            }
            if (r, c) in covered:
                entry["covered_by"] = covered[(r, c)]
            out.append(entry)
    return out


def _body_rows(tablix: etree._Element) -> list[etree._Element]:
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    return find_children(rows_root, "TablixRow") if rows_root is not None else []


def _tablix_cells(tablix: etree._Element) -> list[dict[str, Any]]:
    """Every body cell (see :func:`_grid_cells`)."""
    grid = []
    for row in _body_rows(tablix):
        cells_root = find_child(row, "TablixCells")
        grid.append(find_children(cells_root, "TablixCell") if cells_root is not None else [])
    return _grid_cells(grid)


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

        row_members, row_leaf_list, row_headers = _member_tree(t, "row")
        column_members, column_leaf_list, column_headers = _member_tree(t, "column")
        row_groups = _hierarchy_groups(t, "TablixRowHierarchy")
        roles = _row_roles(t, [g["name"] for g in row_groups if g.get("name")])
        for leaf in row_leaf_list:
            leaf["roles"] = roles.get(leaf["index"], [])
            leaf["role"] = leaf["roles"][0] if leaf["roles"] else None

        out.append(
            {
                "name": t.get("Name"),
                "dataset": _text(find_child(t, "DataSetName")),
                "top": _text(find_child(t, "Top")),
                "left": _text(find_child(t, "Left")),
                "width": _text(find_child(t, "Width")),
                "height": _text(find_child(t, "Height")),
                "columns": columns,
                "rows": [{"height": _text(find_child(r, "Height"))} for r in _body_rows(t)],
                "row_groups": row_groups,
                "column_groups": _hierarchy_groups(t, "TablixColumnHierarchy"),
                # The tablix's own sort; group sorts are under row_members /
                # column_members (#69).
                "sort_expressions": _sort_expressions(t),
                "sort": read_sort(t),
                "filters": filters,
                "visibility": _visibility(t),
                # v0.2: cell-level inventory so LLMs can discover which
                # textbox names live where without parsing raw XML.
                "cells": _tablix_cells(t),
                "header_cells": row_headers + column_headers,
                "corner_cells": _corner_cells(t),
                "row_members": row_members,
                "column_members": column_members,
                "row_leaves": row_leaf_list,
                "column_leaves": column_leaf_list,
            }
        )
    return out


# ---- v0.2: list_*_items / get_textbox / get_image / get_rectangle --------


# Every RDL ReportItem element name (#52: the old list carried the
# non-RDL "Gauge" / "List" and missed GaugePanel / CustomReportItem).
_REPORT_ITEM_TAGS = REPORT_ITEM_TAGS


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


def _list_items_in(
    container: Optional[etree._Element],
    *,
    recursive: bool = False,
    parent: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Report items directly inside ``container/ReportItems``, in document
    order. Each entry carries ``parent`` — the containing Rectangle's
    name, or ``None`` at the top level of the region. With ``recursive``
    the items inside every Rectangle follow that Rectangle (depth-first)."""
    if container is None:
        return []
    items_root = find_child(container, "ReportItems")
    if items_root is None:
        return []
    out: list[dict[str, Any]] = []
    for child in items_root:
        if not isinstance(child.tag, str):
            continue
        local = etree.QName(child).localname
        if local not in _REPORT_ITEM_TAGS:
            continue
        entry = _layout_dict(child)
        entry["parent"] = parent
        out.append(entry)
        if recursive and local == "Rectangle":
            out.extend(_list_items_in(child, recursive=True, parent=child.get("Name")))
    return out


def _nested_items(regions: list[tuple[str, Optional[etree._Element]]]) -> list[dict[str, Any]]:
    """Every item inside a Rectangle (at any depth) in the given regions,
    each tagged with its ``region`` (``body`` / ``header`` / ``footer``)
    and ``parent`` Rectangle."""
    out: list[dict[str, Any]] = []
    for region, container in regions:
        for entry in _list_items_in(container, recursive=True):
            if entry["parent"] is not None:
                out.append({**entry, "region": region})
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


def list_body_items(path: str, recursive: bool = False) -> list[dict[str, Any]]:
    """Enumerate every named ReportItem at the top level of <Body>.
    Returns a list of {name, type, top, left, width, height, parent}.
    ``parent`` is ``None`` for top-level items; with ``recursive=True``
    the items inside each Rectangle follow it, with ``parent`` set to the
    Rectangle's name (their top/left are relative to that Rectangle)."""
    doc = RDLDocument.open(path)
    return _list_items_in(_resolve_body(doc), recursive=recursive)


def list_header_items(path: str, recursive: bool = False) -> list[dict[str, Any]]:
    """Same shape as ``list_body_items`` for <PageHeader>."""
    doc = RDLDocument.open(path)
    return _list_items_in(_resolve_header(doc), recursive=recursive)


def list_footer_items(path: str, recursive: bool = False) -> list[dict[str, Any]]:
    """Same shape as ``list_body_items`` for <PageFooter>."""
    doc = RDLDocument.open(path)
    return _list_items_in(_resolve_footer(doc), recursive=recursive)


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


def _border_dict(
    style: Optional[etree._Element], local: str = "Border"
) -> Optional[dict[str, str]]:
    """Capture a ``Style`` border sub-element (``Border`` by default, or a
    per-side ``TopBorder`` / ``BottomBorder`` / ``LeftBorder`` /
    ``RightBorder``) as ``{Style, Color, Width}``; None when absent."""
    if style is None:
        return None
    border = find_child(style, local)
    return _capture_style_fields(border, _BORDER_STYLE_FIELDS) if border is not None else None


# Per-side border elements → their key in the nested style dict.
_SIDE_BORDERS = (
    ("TopBorder", "top_border"),
    ("BottomBorder", "bottom_border"),
    ("LeftBorder", "left_border"),
    ("RightBorder", "right_border"),
)


def _borders_into(out: dict[str, Any], style: Optional[etree._Element]) -> None:
    """Add ``border`` and any per-side ``*_border`` dicts of ``style`` to
    ``out`` (empty ones are skipped)."""
    border = _border_dict(style)
    if border:
        out["border"] = border
    for local, key in _SIDE_BORDERS:
        side = _border_dict(style, local)
        if side:
            out[key] = side


def _item_style_dict(el: etree._Element) -> Optional[dict[str, Any]]:
    """Nested ``{box, border, top_border, ...}`` style of a non-text item
    (Image, Rectangle) — the textbox shape without paragraph / run.
    Empty branches are dropped; None when nothing is set (#108)."""
    style = find_child(el, "Style")
    out: dict[str, Any] = {}
    box = _capture_style_fields(style, _BOX_STYLE_FIELDS)
    if box:
        out["box"] = box
    _borders_into(out, style)
    return out or None


def _action_dict(action: etree._Element) -> Optional[dict[str, Any]]:
    """``{type, target, parameters}`` of one ``<Action>``: type is
    Hyperlink / Drillthrough / BookmarkLink, target the URL / report name
    / bookmark, parameters the drillthrough ``[{name, value}]`` (plus
    ``omit`` when the parameter carries ``<Omit>``)."""
    for kind in ("Hyperlink", "BookmarkLink"):
        node = find_child(action, kind)
        if node is not None:
            return {"type": kind, "target": node.text, "parameters": []}
    drill = find_child(action, "Drillthrough")
    if drill is None:
        return None
    params: list[dict[str, Any]] = []
    params_root = find_child(drill, "Parameters")
    if params_root is not None:
        for param in find_children(params_root, "Parameter"):
            entry: dict[str, Any] = {
                "name": param.get("Name"),
                "value": _text(find_child(param, "Value")),
            }
            omit = find_child(param, "Omit")
            if omit is not None:
                entry["omit"] = omit.text
            params.append(entry)
    return {
        "type": "Drillthrough",
        "target": _text(find_child(drill, "ReportName")),
        "parameters": params,
    }


def _action_of(el: etree._Element) -> Optional[dict[str, Any]]:
    """The first action of ``el``'s ``<ActionInfo>`` (or of a legacy bare
    ``<Action>`` child), or None."""
    info = find_child(el, "ActionInfo")
    if info is not None:
        actions_root = find_child(info, "Actions")
        for action in find_children(actions_root, "Action") if actions_root is not None else []:
            found = _action_dict(action)
            if found is not None:
                return found
        return None
    legacy = find_child(el, "Action")
    return _action_dict(legacy) if legacy is not None else None


def _interactivity_dict(el: etree._Element) -> dict[str, Any]:
    """``{action, tooltip, document_map_label, bookmark}`` of a report item
    — what the action / tooltip / document-map / bookmark tools write
    (#108). Each is None when absent."""
    return {
        "action": _action_of(el),
        "tooltip": _text(find_child(el, "ToolTip")),
        "document_map_label": _text(find_child(el, "DocumentMapLabel")),
        "bookmark": _text(find_child(el, "Bookmark")),
    }


# Paragraph-level properties → their key in get_textbox's ``paragraphs``.
_PARAGRAPH_FIELDS = (
    ("ListStyle", "list_style"),
    ("ListLevel", "list_level"),
    ("LeftIndent", "left_indent"),
    ("RightIndent", "right_indent"),
    ("HangingIndent", "hanging_indent"),
    ("SpaceBefore", "space_before"),
    ("SpaceAfter", "space_after"),
)


def _paragraph_elements(textbox: etree._Element) -> list[etree._Element]:
    paragraphs_root = find_child(textbox, "Paragraphs")
    return find_children(paragraphs_root, "Paragraph") if paragraphs_root is not None else []


def _paragraphs_of(textbox: etree._Element) -> list[dict[str, Any]]:
    """``[{index, text_align, list_style, list_level, left_indent,
    right_indent, hanging_indent, space_before, space_after}]`` per
    ``<Paragraph>`` (None for each property that is not set)."""
    out: list[dict[str, Any]] = []
    for index, paragraph in enumerate(_paragraph_elements(textbox)):
        style = find_child(paragraph, "Style")
        entry: dict[str, Any] = {
            "index": index,
            "text_align": _text(find_child(style, "TextAlign")) if style is not None else None,
        }
        for local, key in _PARAGRAPH_FIELDS:
            entry[key] = _text(find_child(paragraph, local))
        out.append(entry)
    return out


def _run_interactivity(textbox: etree._Element) -> list[dict[str, Any]]:
    """``[{paragraph_index, run_index, tooltip, action}]`` for each text run
    that carries a ``<ToolTip>`` or an action (run_index counts within its
    paragraph). Kept out of ``runs`` so ``runs`` stays exactly the shape
    ``set_textbox_runs`` accepts."""
    out: list[dict[str, Any]] = []
    for paragraph_index, paragraph in enumerate(_paragraph_elements(textbox)):
        runs_root = find_child(paragraph, "TextRuns")
        if runs_root is None:
            continue
        for run_index, run in enumerate(find_children(runs_root, "TextRun")):
            tooltip = _text(find_child(run, "ToolTip"))
            action = _action_of(run)
            if tooltip is None and action is None:
                continue
            out.append(
                {
                    "paragraph_index": paragraph_index,
                    "run_index": run_index,
                    "tooltip": tooltip,
                    "action": action,
                }
            )
    return out


def _runs_of(textbox: etree._Element) -> list[dict[str, Any]]:
    """Enumerate text runs as ``{value, style?, paragraph_index,
    markup_type?}`` so callers see the same per-run style fields
    ``set_textbox_style`` writes (font / color / format).

    This is exactly the shape ``set_textbox_runs`` accepts, so feeding
    the list straight back is a no-op. ``paragraph_index`` is the run's
    ``<Paragraph>`` position; ``markup_type`` appears when the run carries
    a ``<MarkupType>`` (``HTML`` runs render their tags)."""
    runs: list[dict[str, Any]] = []
    paragraphs_root = find_child(textbox, "Paragraphs")
    paragraphs = find_children(paragraphs_root, "Paragraph") if paragraphs_root is not None else []
    for paragraph_index, paragraph in enumerate(paragraphs):
        runs_root = find_child(paragraph, "TextRuns")
        if runs_root is None:
            continue
        for run in find_children(runs_root, "TextRun"):
            entry: dict[str, Any] = {"value": _text(find_child(run, "Value"))}
            run_style = _run_style_dict(run)
            if run_style is not None:
                entry["style"] = run_style
            entry["paragraph_index"] = paragraph_index
            markup = find_child(run, "MarkupType")
            if markup is not None and markup.text:
                entry["markup_type"] = markup.text
            runs.append(entry)
    return runs


def _full_textbox_style(textbox: etree._Element) -> Optional[dict[str, Any]]:
    """Return a nested style dict matching the ``set_textbox_style`` routing.

    Shape::

        {
          "box":       {BackgroundColor, VerticalAlign, Padding*, ...},
          "border":    {Style, Color, Width},
          "top_border" / "bottom_border" / "left_border" / "right_border":
                       {Style, Color, Width}  (per-side overrides),
          "paragraph": {TextAlign},
          "run":       {FontFamily, FontSize, FontWeight, Color, Format, ...},
        }

    Empty branches are dropped. Returns ``None`` when nothing of interest is
    present so callers can treat it like the previous flat-or-None shape.

    Paragraph and run capture *only the first* paragraph / first run — the
    same behavior ``set_textbox_style`` uses for writes.
    """
    box = _capture_style_fields(find_child(textbox, "Style"), _BOX_STYLE_FIELDS)

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
    _borders_into(out, find_child(textbox, "Style"))
    if paragraph_style:
        out["paragraph"] = paragraph_style
    if run_style:
        out["run"] = run_style
    return out or None


def _style_dict(el: Optional[etree._Element]) -> Optional[dict[str, Any]]:
    """Capture key Style children. Best-effort; returns None if no style.

    Used by get_chart, where there's only one Style block to look at —
    a flat field dump. (Image and Rectangle use :func:`_item_style_dict`.)
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

    Also returns ``paragraphs`` (per-paragraph TextAlign, list and indent
    / spacing properties), ``run_interactivity`` (runs with their own
    ToolTip / action) and the item's ``action`` / ``tooltip`` /
    ``document_map_label`` / ``bookmark`` (#108).
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
        "paragraphs": _paragraphs_of(tb),
        "run_interactivity": _run_interactivity(tb),
        "style": _full_textbox_style(tb),
        "visibility": _visibility(tb),
        "can_grow": _text(find_child(tb, "CanGrow")),
        "can_shrink": _text(find_child(tb, "CanShrink")),
        **_interactivity_dict(tb),
    }


def get_image(path: str, name: str) -> dict[str, Any]:
    """Return position, size, source, value, sizing, nested style
    (``box`` / ``border`` / per-side borders), visibility, action,
    tooltip, document_map_label and bookmark for a named Image."""
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
        "style": _item_style_dict(img),
        "visibility": _visibility(img),
        **_interactivity_dict(img),
    }


def get_rectangle(path: str, name: str) -> dict[str, Any]:
    """Return position, size, contained-item names, nested style (as
    get_image), visibility and interactivity for a Rectangle."""
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
        "style": _item_style_dict(rect),
        "visibility": _visibility(rect),
        **_interactivity_dict(rect),
    }


# ---- get_chart ------------------------------------------------------------


def _chart_data_labels_dict(series: etree._Element) -> Optional[dict[str, Any]]:
    """Read-back shape for the series' effective ``<ChartDataLabel>``,
    symmetric with what :func:`set_chart_data_labels` writes: the template
    data point's label (``source: "point"``, Report Builder's place, which
    overrides a series-level one) else ``ChartSeries/ChartDataLabel``
    (``source: "series"``). Returns ``None`` when neither exists so
    consumers can distinguish "no data labels configured" from "configured
    with no visible/format".
    """
    label, source = chart_shape.data_label(series)
    if label is None:
        return None
    style = find_child(label, "Style")

    def style_text(local: str) -> Optional[str]:
        return _text(find_child(style, local)) if style is not None else None

    return {
        "source": source,
        "visible": _text(find_child(label, "Visible")),
        "position": _text(find_child(label, "Position")),
        "use_value_as_label": _text(find_child(label, "UseValueAsLabel")),
        "format": style_text("Format"),
        "font_weight": style_text("FontWeight"),
        "color": style_text("Color"),
    }


def _chart_action_dict(host: Optional[etree._Element]) -> Optional[dict[str, Any]]:
    """Read-back of the first action on ``host`` (``ActionInfo/Actions/Action``,
    or a legacy bare ``<Action>``), in the argument shape of the
    ``set_*_action`` tools: ``{action_type, target_expression,
    drillthrough_parameters: [{name, value}]}``. None when there is none."""
    if host is None:
        return None
    action = None
    info = find_child(host, "ActionInfo")
    if info is not None:
        actions = find_child(info, "Actions")
        action = find_child(actions, "Action") if actions is not None else None
    if action is None:
        action = find_child(host, "Action")
    if action is None:
        return None
    for kind in ("Hyperlink", "BookmarkLink"):
        node = find_child(action, kind)
        if node is not None:
            return {"action_type": kind, "target_expression": _text(node)}
    drill = find_child(action, "Drillthrough")
    if drill is None:
        return None
    params_root = find_child(drill, "Parameters")
    params = (
        [
            {"name": p.get("Name"), "value": _text(find_child(p, "Value"))}
            for p in find_children(params_root, "Parameter")
        ]
        if params_root is not None
        else []
    )
    return {
        "action_type": "Drillthrough",
        "target_expression": _text(find_child(drill, "ReportName")),
        "drillthrough_parameters": params,
    }


def _chart_series_dict(
    series: etree._Element, member: Optional[etree._Element] = None
) -> dict[str, Any]:
    """Return the read-back shape for one ``<ChartSeries>``: name, type,
    subtype, value/category expressions on the template data point, style
    color (when explicitly set via ``<Style>/<Color>``), ``data_labels``
    (``None`` when ``<ChartDataLabel>`` is absent), the axis / area /
    legend bindings, ``hidden``, the click ``action`` and the series'
    hierarchy ``label`` (its leaf ``member``'s ``<Label>``).

    ``type`` is the effective type: RDL's default ``Column`` when
    ``<Type>`` is absent (Report Builder omits it for Column series), with
    ``type_implicit: true``.
    """
    point = chart_shape.template_data_point(series)
    values = find_child(point, "ChartDataPointValues") if point is not None else None
    y = find_child(values, "Y") if values is not None else None
    x = find_child(values, "X") if values is not None else None

    style = find_child(series, "Style")
    color = _text(find_child(style, "Color")) if style is not None else None

    type_text = _text(find_child(series, "Type"))
    type_implicit = type_text is None or not type_text.strip()

    action = _chart_action_dict(point)
    if action is None:  # legacy series-level placement
        action = _chart_action_dict(series)

    return {
        "name": series.get("Name"),
        "type": chart_types.RDL_DEFAULT_TYPE if type_implicit else type_text,
        "type_implicit": type_implicit,
        "subtype": _text(find_child(series, "Subtype")),
        "value_expression": _text(y),
        "category_expression": _text(x),
        "color": color,
        "data_labels": _chart_data_labels_dict(series),
        "label": _text(find_child(member, "Label")) if member is not None else None,
        "value_axis": _text(find_child(series, "ValueAxisName")),
        "category_axis": _text(find_child(series, "CategoryAxisName")),
        "chart_area": _text(find_child(series, "ChartAreaName")),
        "legend_name": _text(find_child(series, "LegendName")),
        "hidden": _text(find_child(series, "Hidden")),
        "action": action,
        **_chart_series_extras(series, point),
    }


def _sub_text(parent: Optional[etree._Element], *path: str) -> Optional[str]:
    """Text of ``parent/path[0]/path[1]/...`` (None anywhere along it)."""
    node = parent
    for local in path:
        if node is None:
            return None
        node = find_child(node, local)
    return _text(node)


def _chart_series_extras(series: etree._Element, point: Optional[etree._Element]) -> dict[str, Any]:
    """What set_chart_series_properties writes (#104): the legend entry,
    and the template data point's tooltip / marker / colour, and the
    empty-point display."""
    empty_value = None
    props = find_child(series, "CustomProperties")
    for cp in find_children(props, "CustomProperty") if props is not None else []:
        if (_text(find_child(cp, "Name")) or "").strip() == "EmptyPointValue":
            empty_value = _text(find_child(cp, "Value"))
    marker = find_child(point, "ChartMarker") if point is not None else None
    return {
        "legend_text": _sub_text(series, "ChartItemInLegend", "LegendText"),
        "hide_in_legend": _sub_text(series, "ChartItemInLegend", "Hidden"),
        "tooltip": _sub_text(point, "ToolTip"),
        "point_color": _sub_text(point, "Style", "Color"),
        "marker": (
            {"type": _sub_text(marker, "Type"), "size": _sub_text(marker, "Size")}
            if marker is not None
            else None
        ),
        "empty_point_value": empty_value,
        "empty_point_color": _sub_text(series, "ChartEmptyPoints", "Style", "Color"),
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
        "location": _text(find_child(axis, "Location")),
    }


def _chart_area_axes(chart_area: etree._Element) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"category": [], "value": []}
    for key, local in (("category", "ChartCategoryAxes"), ("value", "ChartValueAxes")):
        root = find_child(chart_area, local)
        if root is not None:
            out[key] = [_chart_axis_dict(a) for a in find_children(root, "ChartAxis")]
    return out


def _chart_areas(chart: etree._Element) -> list[dict[str, Any]]:
    areas_root = find_child(chart, "ChartAreas")
    if areas_root is None:
        return []
    return [
        {
            "name": area.get("Name"),
            "hidden": _text(find_child(area, "Hidden")),
            "axes": _chart_area_axes(area),
        }
        for area in find_children(areas_root, "ChartArea")
    ]


def _chart_legend_entry(legend: etree._Element) -> dict[str, Any]:
    # set_chart_legend writes <Hidden>true|false</Hidden> as the inverse
    # of `visible` (visible=True → Hidden=false). Echo the inverse on
    # read so the field name matches the writer's input arg semantics:
    # set_chart_legend(visible=true) ⇒ get_chart().legend.visible == "true".
    hidden_text = _text(find_child(legend, "Hidden"))
    if hidden_text is None:
        visible_text: Optional[str] = None
    else:
        visible_text = "false" if hidden_text.strip().lower() == "true" else "true"
    # Read only <Position> — DockOutsideChartArea is a separate boolean
    # and doesn't belong in this field.
    return {
        "name": legend.get("Name"),
        "position": _text(find_child(legend, "Position")),
        "visible": visible_text,
    }


def _chart_legends(chart: etree._Element) -> list[dict[str, Any]]:
    legends_root = find_child(chart, "ChartLegends")
    if legends_root is None:
        return []
    return [_chart_legend_entry(lg) for lg in find_children(legends_root, "ChartLegend")]


def _chart_titles(chart: etree._Element) -> list[dict[str, Any]]:
    titles_root = find_child(chart, "ChartTitles")
    if titles_root is None:
        return []
    return [
        {"name": title.get("Name"), "caption": _text(find_child(title, "Caption"))}
        for title in find_children(titles_root, "ChartTitle")
    ]


def _chart_member_entry(member: etree._Element, level: int) -> dict[str, Any]:
    """``{name, expression, label, sort, static, level}`` for one ChartMember
    (``name`` / ``expression`` are the ``<Group>``'s; None when static)."""
    group = find_child(member, "Group")
    expressions: list[str] = []
    if group is not None:
        expr_root = find_child(group, "GroupExpressions")
        if expr_root is not None:
            expressions = [
                e.text for e in find_children(expr_root, "GroupExpression") if e.text is not None
            ]
    return {
        "name": group.get("Name") if group is not None else None,
        "expression": expressions[0] if expressions else None,
        "label": _text(find_child(member, "Label")),
        "sort": [{"value": v, "direction": d} for v, d in chart_shape.member_sorts(member)],
        "static": group is None,
        "level": level,
    }


def _chart_category_groups(chart: etree._Element) -> list[dict[str, Any]]:
    """Every category-hierarchy member, depth-first (outer level first)."""
    return [
        _chart_member_entry(member, level)
        for member, level in chart_shape.walk_members(chart_shape.category_hierarchy_members(chart))
    ]


def _chart_series_groups(chart: etree._Element) -> list[dict[str, Any]]:
    """Every series-hierarchy member, depth-first; a leaf also names the
    ``series`` it belongs to (series and leaves match by position)."""
    all_series = chart_shape.series_list(chart)
    leaf_index = {id(m): i for i, m in enumerate(chart_shape.series_leaf_members(chart))}
    out: list[dict[str, Any]] = []
    for member, level in chart_shape.walk_members(chart_shape.series_hierarchy_members(chart)):
        entry = _chart_member_entry(member, level)
        idx = leaf_index.get(id(member))
        entry["series"] = (
            all_series[idx].get("Name") if idx is not None and idx < len(all_series) else None
        )
        out.append(entry)
    return out


def _chart_derived_series(chart: etree._Element) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for derived in chart_shape.derived_series_list(chart):
        inner = find_child(derived, "ChartSeries")
        params_root = find_child(derived, "ChartFormulaParameters")
        params = (
            [
                {
                    "name": p.get("Name"),
                    "value": _text(find_child(p, "Value")),
                    "source": _text(find_child(p, "Source")),
                }
                for p in find_children(params_root, "ChartFormulaParameter")
            ]
            if params_root is not None
            else []
        )
        type_text = _text(find_child(inner, "Type")) if inner is not None else None
        out.append(
            {
                "name": chart_shape.derived_series_name(derived),
                "source": chart_shape.derived_source_name(derived),
                "formula": _text(find_child(derived, "DerivedSeriesFormula")),
                "parameters": params,
                "type": type_text or chart_types.RDL_DEFAULT_TYPE,
            }
        )
    return out


def get_chart(path: str, name: str) -> dict[str, Any]:
    """Read-back for a named ``<Chart>``: everything the chart tools write.

    Symmetric with :func:`get_textbox` / :func:`get_image` /
    :func:`get_rectangle`. Returns shape::

        {
          "name": "...",
          "type": "Chart",
          "top": "..", "left": "..", "width": "..", "height": "..",
          "dataset": "..",
          "palette": "EarthTones" | None,
          "custom_palette_colors": [..], "palette_hatch_behavior": .. | None,
          "series": [{name, type, type_implicit, subtype, value_expression,
                      category_expression, color, data_labels, label,
                      value_axis, category_axis, chart_area, legend_name,
                      hidden, action}, ...],
          "series_groups": [{name, expression, label, sort, static, level,
                             series}, ...],
          "derived_series": [{name, source, formula, parameters, type}, ...],
          "category_groups": [{name, expression, label, sort, static, level}, ...],
          "axes": {"category": [...], "value": [...]},      # first ChartArea
          "chart_areas": [{name, hidden, axes: {category, value}}, ...],
          "legend": {name, position, visible} | None,         # first legend
          "legends": [...],
          "title": {name, caption} | None,                    # first title
          "titles": [...],
          "style": {...} | None,
          "visibility": {...} | None,
        }
    """
    doc = RDLDocument.open(path)
    chart = _find_named_anywhere(doc, "Chart", name)
    if chart is None:
        from pbirb_mcp.core.ids import ElementNotFoundError

        raise ElementNotFoundError(f"no Chart named {name!r}")

    leaves = chart_shape.series_leaf_members(chart)
    series_list = [
        _chart_series_dict(s, leaves[i] if i < len(leaves) else None)
        for i, s in enumerate(chart_shape.series_list(chart))
    ]

    areas = _chart_areas(chart)
    legends = _chart_legends(chart)
    titles = _chart_titles(chart)
    custom_root = find_child(chart, "ChartCustomPaletteColors")
    custom_colors = (
        [_text(c) for c in find_children(custom_root, "ChartCustomPaletteColor")]
        if custom_root is not None
        else []
    )

    return {
        "name": name,
        "type": "Chart",
        **_layout_dict_for(chart),
        "dataset": _text(find_child(chart, "DataSetName")),
        "palette": _text(find_child(chart, "Palette")),
        "custom_palette_colors": custom_colors,
        "palette_hatch_behavior": _text(find_child(chart, "PaletteHatchBehavior")),
        "series": series_list,
        "series_groups": _chart_series_groups(chart),
        "derived_series": _chart_derived_series(chart),
        "category_groups": _chart_category_groups(chart),
        "axes": areas[0]["axes"] if areas else {"category": [], "value": []},
        "chart_areas": areas,
        "legend": legends[0] if legends else None,
        "legends": legends,
        "title": titles[0] if titles else None,
        "titles": titles,
        "style": _style_dict(chart),
        "visibility": _visibility(chart),
    }


# ---- find_textboxes_by_style ---------------------------------------------


# Filter kwargs accepted by find_textboxes_by_style. Each entry maps a
# user-facing kwarg name to (style_level, RDL local name, comparison kind).
#
# Box-level filters read <Textbox/Style>/X, border filters
# <Textbox/Style/Border>/X; paragraph filters are tried on every
# <Paragraph/Style>, run filters on every <TextRun/Style>.
_FILTER_LOCATIONS: dict[str, tuple[str, str, str]] = {
    "background_color": ("box", "BackgroundColor", "color"),
    "vertical_align": ("box", "VerticalAlign", "enum"),
    "writing_mode": ("box", "WritingMode", "enum"),
    "padding_top": ("box", "PaddingTop", "size"),
    "padding_bottom": ("box", "PaddingBottom", "size"),
    "padding_left": ("box", "PaddingLeft", "size"),
    "padding_right": ("box", "PaddingRight", "size"),
    "border_style": ("border", "Style", "enum"),
    "border_color": ("border", "Color", "color"),
    "border_width": ("border", "Width", "size"),
    "text_align": ("paragraph", "TextAlign", "enum"),
    "font_family": ("run", "FontFamily", "text"),
    "font_size": ("run", "FontSize", "size"),
    "font_weight": ("run", "FontWeight", "enum"),
    "font_style": ("run", "FontStyle", "enum"),
    "color": ("run", "Color", "color"),
    "format": ("run", "Format", "exact"),
    "text_decoration": ("run", "TextDecoration", "enum"),
}

# What Report Builder renders when a property is unset — matched by an
# unset property under ``effective=True``.
_STYLE_DEFAULTS: dict[str, str] = {
    "BackgroundColor": "Transparent",
    "VerticalAlign": "Top",
    "WritingMode": "Horizontal",
    "PaddingTop": "0pt",
    "PaddingBottom": "0pt",
    "PaddingLeft": "0pt",
    "PaddingRight": "0pt",
    "Style": "None",
    "Width": "1pt",
    "TextAlign": "General",
    "FontFamily": "Arial",
    "FontSize": "10pt",
    "FontWeight": "Normal",
    "FontStyle": "Normal",
    "Color": "Black",
    "TextDecoration": "None",
}
_BORDER_DEFAULTS = {"Style": "None", "Color": "Black", "Width": "1pt"}


def _style_key(kind: str, value: str) -> Any:
    """Comparison key for a style value: colours by value (named, hex
    of either case), sizes in points (``10pt`` == ``10.0pt`` ==
    ``0.13889in``), enums and font families case-insensitively (font
    weights 100-900 as their names). Expressions compare as written."""
    text = value.strip()
    if text.startswith("="):
        return text
    if kind == "color":
        return style_values.normalize_color(text)
    if kind == "size":
        try:
            return round(to_inches(text) * 72.0, 4)
        except ValueError:
            return text
    if kind == "enum":
        canonical = style_values.font_weight(text) if text.isdigit() else text
        return (canonical or text).lower()
    if kind == "text":
        return text.lower()
    return value


def _style_text(style: Optional[etree._Element], local: str) -> Optional[str]:
    node = find_child(style, local) if style is not None else None
    return node.text if node is not None and node.text is not None else None


def _style_candidates(
    textbox: etree._Element, level: str
) -> list[tuple[Optional[etree._Element], dict[str, int]]]:
    """``(style element or None, position)`` pairs a filter at ``level``
    is tried against: the box/border style once, every paragraph's style
    (``{paragraph_index}``) or every run's (``{paragraph_index,
    run_index}``). A textbox with no paragraph/run still yields one
    style-less candidate, so ``effective`` can match its defaults."""
    if level in ("box", "border"):
        style = find_child(textbox, "Style")
        if level == "border" and style is not None:
            style = find_child(style, "Border")
        return [(style, {})]
    paragraphs_root = find_child(textbox, "Paragraphs")
    paragraphs = find_children(paragraphs_root, "Paragraph") if paragraphs_root is not None else []
    out: list[tuple[Optional[etree._Element], dict[str, int]]] = []
    for p_idx, paragraph in enumerate(paragraphs):
        if level == "paragraph":
            out.append((find_child(paragraph, "Style"), {"paragraph_index": p_idx}))
            continue
        runs_root = find_child(paragraph, "TextRuns")
        runs = find_children(runs_root, "TextRun") if runs_root is not None else []
        for r_idx, run in enumerate(runs):
            out.append((find_child(run, "Style"), {"paragraph_index": p_idx, "run_index": r_idx}))
    if not out:
        out.append((None, {"paragraph_index": 0} if level == "paragraph" else {}))
    return out


def _match_level(
    textbox: etree._Element,
    level: str,
    wanted: dict[str, tuple[str, str, Any]],
    effective: bool,
) -> Optional[tuple[dict[str, str], dict[str, int]]]:
    """Try every candidate style at ``level``; the first one on which
    every ``wanted`` filter (kwarg -> (local, kind, key)) matches gives
    ``(matched values, position)``. ``None`` when none does."""
    defaults = _BORDER_DEFAULTS if level == "border" else _STYLE_DEFAULTS
    for style, position in _style_candidates(textbox, level):
        matched: dict[str, str] = {}
        for kwarg, (local, kind, key) in wanted.items():
            actual = _style_text(style, local)
            if actual is None and effective:
                actual = defaults.get(local)
            if actual is None or _style_key(kind, actual) != key:
                break
            matched[kwarg] = actual
        else:
            return matched, position
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
    effective: bool = False,
) -> list[dict[str, Any]]:
    """Search for textboxes matching one or more style filters.

    Each filter is optional; supplied filters AND together — a textbox
    must match every supplied filter to appear in the result. Useful as
    a discovery step before :func:`set_textbox_style_bulk`.

    Values compare normalised (#107): colours by value (``White`` ==
    ``#FFFFFF`` == ``#ffffff``), sizes in points (``10pt`` == ``10.0pt``),
    enums and font families case-insensitively; ``format`` and
    ``=expressions`` compare as written. Paragraph filters match when
    any paragraph matches them all, run filters when any run (of any
    paragraph) does. ``effective=True`` lets an unset property match the
    RDL default (FontSize 10pt, FontFamily Arial, Color Black,
    FontWeight Normal, TextAlign General, VerticalAlign Top, padding
    0pt, ...).

    Returns a list of ``{name, location, matched_fields, matched_at}``:
    ``location`` is best-effort (``body`` / ``header`` / ``footer`` /
    ``tablix:<name>`` / ``rectangle:<name>`` / ``unknown``),
    ``matched_fields`` maps each filter to the value found on the
    textbox (the default under ``effective`` when unset), and
    ``matched_at`` gives ``{paragraph_index}`` / ``{paragraph_index,
    run_index}`` for paragraph / run filters.

    Returns ``[]`` when no filters are supplied (refusing to match
    everything would be a footgun, since the caller almost certainly
    wants to feed the result into ``set_textbox_style_bulk``).
    """
    if not isinstance(effective, bool):
        raise ValueError(f"effective: expected true or false; got {effective!r}")
    by_level: dict[str, dict[str, tuple[str, str, Any]]] = {}
    supplied = locals()
    for kwarg, (level, local, kind) in _FILTER_LOCATIONS.items():
        v = supplied.get(kwarg)
        if v is not None:
            by_level.setdefault(level, {})[kwarg] = (local, kind, _style_key(kind, v))
    if not by_level:
        return []

    doc = RDLDocument.open(path)
    out: list[dict[str, Any]] = []
    for textbox in doc.root.iter(f"{{{RDL_NS}}}Textbox"):
        name = textbox.get("Name")
        if name is None:
            continue
        matched: dict[str, str] = {}
        matched_at: dict[str, dict[str, int]] = {}
        for level, wanted in by_level.items():
            hit = _match_level(textbox, level, wanted, effective)
            if hit is None:
                break
            values, position = hit
            matched.update(values)
            if position:
                matched_at.update({kwarg: dict(position) for kwarg in values})
        else:
            # Report filters in the order the tool lists them.
            ordered = {k: matched[k] for k in _FILTER_LOCATIONS if k in matched}
            out.append(
                {
                    "name": name,
                    "location": _textbox_location(textbox),
                    "matched_fields": ordered,
                    "matched_at": {k: matched_at[k] for k in ordered if k in matched_at},
                }
            )
    return out


# ---- find_textbox_by_value (Phase 12 commit 44) -------------------------

_VALUE_KINDS = ("tooltip", "action", "visibility")


def _value_hits(textbox: etree._Element, include: tuple[str, ...]) -> list[tuple[str, str]]:
    """``(kind, text)`` for every text ``find_textbox_by_value`` searches
    in ``textbox``: each run's ``<Value>`` (``"value"``), then — when
    included — ``ToolTip``s, ``ActionInfo`` texts (hyperlink, drillthrough
    report and parameter values, bookmark link) and ``Visibility/Hidden``."""
    hits: list[tuple[str, str]] = []
    paragraphs_root = find_child(textbox, "Paragraphs")
    paragraphs = find_children(paragraphs_root, "Paragraph") if paragraphs_root is not None else []
    runs = [
        run
        for paragraph in paragraphs
        for runs_root in [find_child(paragraph, "TextRuns")]
        if runs_root is not None
        for run in find_children(runs_root, "TextRun")
    ]
    for run in runs:
        value = find_child(run, "Value")
        if value is not None:
            hits.append(("value", value.text or ""))
    if "tooltip" in include:
        for owner in [textbox, *runs]:
            tip = find_child(owner, "ToolTip")
            if tip is not None and tip.text:
                hits.append(("tooltip", tip.text))
    if "action" in include:
        for owner in [textbox, *runs]:
            action_info = find_child(owner, "ActionInfo")
            if action_info is None:
                continue
            for node in action_info.iter():
                if isinstance(node.tag, str) and len(node) == 0 and (node.text or "").strip():
                    hits.append(("action", node.text))
    if "visibility" in include:
        visibility = find_child(textbox, "Visibility")
        hidden = find_child(visibility, "Hidden") if visibility is not None else None
        if hidden is not None and hidden.text:
            hits.append(("visibility", hidden.text))
    return hits


def find_textbox_by_value(
    path: str, pattern: str, include: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """Return every ``Textbox`` whose displayed value matches ``pattern``
    (a Python regex, not RDL/SQL glob).

    Searches the ``<Value>`` of each ``Paragraph/TextRun`` in every
    textbox under ``<Body>`` / ``<PageHeader>`` / ``<PageFooter>``. A
    textbox with multiple matching runs surfaces once per matching run.
    Other expressions on the textbox (a drillthrough parameter's
    ``<Value>``, a tooltip) are not its value and are only searched when
    ``include`` names them: ``"tooltip"``, ``"action"`` (hyperlink,
    drillthrough report / parameter values, bookmark link),
    ``"visibility"`` (``Visibility/Hidden``).

    Returns ``[{textbox, value, region, kind}]`` where ``region`` is one
    of ``Body``, ``PageHeader``, ``PageFooter`` (the nearest top-level
    ancestor) and ``kind`` is ``"value"`` or the ``include`` kind.

    Useful for finding stale ``=Parameters!Old.Value`` references after
    ``rename_parameter``, or any other cross-cutting expression edit.
    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex pattern {pattern!r}: {exc}") from exc
    kinds = tuple(dict.fromkeys(include or []))
    bad = [k for k in kinds if k not in _VALUE_KINDS]
    if bad:
        raise ValueError(f"include: unknown kind(s) {bad}; expected any of {list(_VALUE_KINDS)}")

    doc = RDLDocument.open(path)
    out: list[dict[str, Any]] = []

    for tb in doc.root.iter(q("Textbox")):
        name = tb.get("Name")
        if not name:
            continue
        for kind, text in _value_hits(tb, kinds):
            if not regex.search(text):
                continue
            out.append(
                {
                    "textbox": name,
                    "value": text,
                    "region": _region_of(tb),
                    "kind": kind,
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
