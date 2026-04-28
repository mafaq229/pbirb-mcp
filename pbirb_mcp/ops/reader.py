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

    return {
        "path": str(doc.path),
        "data_sources": [ds.get("Name") for ds in root.iter(f"{{{RDL_NS}}}DataSource")],
        "datasets": [ds.get("Name") for ds in root.iter(f"{{{RDL_NS}}}DataSet")],
        "parameters": [p.get("Name") for p in root.iter(f"{{{RDL_NS}}}ReportParameter")],
        "tablixes": [t.get("Name") for t in root.iter(f"{{{RDL_NS}}}Tablix")],
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
            }
        )
    return out


__all__ = [
    "describe_report",
    "get_datasets",
    "get_parameters",
    "get_tablixes",
]
