"""Tablix-mutation tools.

Covers tablix filters (this commit), groupings, sorting, and visibility
(later commits in Phase 3). Every tool addresses elements by stable name —
``tablix_name`` and ``group_name`` — never by index, with one deliberate
exception: ``<Filter>`` elements are anonymous in RDL, so callers index them
by ordinal position within the tablix's ``<Filters>`` block.

Filter operators are constrained to the RDL 2016 enumeration. Passing an
unknown operator raises ``ValueError`` rather than letting Report Builder
silently load an invalid file.

Insertion order matters for the RDL XSD. ``<Filters>`` belongs in a Tablix
between ``<DataSetName>`` and ``<SortExpressions>``; the helper here finds
the right anchor before placing a freshly-created ``<Filters>`` element.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import resolve_tablix
from pbirb_mcp.core.xpath import find_child, find_children, q


# RDL 2016 FilterOperator enumeration.
_VALID_OPERATORS = frozenset(
    {
        "Equal",
        "NotEqual",
        "GreaterThan",
        "GreaterThanOrEqual",
        "LessThan",
        "LessThanOrEqual",
        "Like",
        "TopN",
        "BottomN",
        "TopPercent",
        "BottomPercent",
        "In",
        "Between",
    }
)


# Per RDL XSD, on a Tablix the <Filters> block sits between <DataSetName>
# (which we always have) and <SortExpressions>. We try to insert immediately
# after <DataSetName>; if it's missing we fall back to inserting before the
# layout block (<Top>), which is also a valid position.
_FILTERS_PRECEDED_BY = ("DataSetName",)
_FILTERS_FOLLOWED_BY = ("SortExpressions", "Top")


def _ensure_filters_block(tablix: etree._Element) -> etree._Element:
    existing = find_child(tablix, "Filters")
    if existing is not None:
        return existing

    block = etree.Element(q("Filters"))
    # Prefer "after DataSetName"; that matches Report Builder's emitted order.
    for local in _FILTERS_PRECEDED_BY:
        anchor = find_child(tablix, local)
        if anchor is not None:
            anchor.addnext(block)
            return block
    # Fall back to "before <Top>" or other followers.
    for local in _FILTERS_FOLLOWED_BY:
        anchor = find_child(tablix, local)
        if anchor is not None:
            anchor.addprevious(block)
            return block
    # Last resort — append. Report Builder usually accepts this for a Tablix
    # without DataSetName / layout, which is degenerate but possible.
    tablix.append(block)
    return block


def _filter_to_dict(filter_node: etree._Element) -> dict[str, Any]:
    expr = find_child(filter_node, "FilterExpression")
    op = find_child(filter_node, "Operator")
    values_root = find_child(filter_node, "FilterValues")
    values: list[str] = []
    if values_root is not None:
        for v in find_children(values_root, "FilterValue"):
            values.append(v.text or "")
    return {
        "expression": expr.text if expr is not None else None,
        "operator": op.text if op is not None else None,
        "values": values,
    }


# ---- list_tablix_filters --------------------------------------------------


def list_tablix_filters(path: str, tablix_name: str) -> list[dict[str, Any]]:
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    filters_root = find_child(tablix, "Filters")
    if filters_root is None:
        return []
    return [_filter_to_dict(f) for f in find_children(filters_root, "Filter")]


# ---- add_tablix_filter ----------------------------------------------------


def add_tablix_filter(
    path: str,
    tablix_name: str,
    expression: str,
    operator: str,
    values: list[str],
) -> dict[str, Any]:
    if operator not in _VALID_OPERATORS:
        raise ValueError(
            f"unknown filter operator {operator!r}; "
            f"valid operators are: {sorted(_VALID_OPERATORS)}"
        )
    if not values:
        raise ValueError("at least one filter value is required")

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    filters_root = _ensure_filters_block(tablix)

    filter_node = etree.SubElement(filters_root, q("Filter"))
    expr_node = etree.SubElement(filter_node, q("FilterExpression"))
    expr_node.text = expression
    op_node = etree.SubElement(filter_node, q("Operator"))
    op_node.text = operator
    values_root = etree.SubElement(filter_node, q("FilterValues"))
    for v in values:
        v_node = etree.SubElement(values_root, q("FilterValue"))
        v_node.text = v

    new_index = len(find_children(filters_root, "Filter")) - 1
    doc.save()
    return {
        "tablix": tablix_name,
        "index": new_index,
        "expression": expression,
        "operator": operator,
        "values": list(values),
    }


# ---- remove_tablix_filter -------------------------------------------------


def remove_tablix_filter(path: str, tablix_name: str, filter_index: int) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    filters_root = find_child(tablix, "Filters")
    filters = find_children(filters_root, "Filter") if filters_root is not None else []
    if not filters or filter_index < 0 or filter_index >= len(filters):
        raise IndexError(
            f"tablix {tablix_name!r} has no filter at index {filter_index}"
        )

    target = filters[filter_index]
    filters_root.remove(target)
    if len(find_children(filters_root, "Filter")) == 0:
        filters_root.getparent().remove(filters_root)

    doc.save()
    return {"tablix": tablix_name, "removed_index": filter_index}


__all__ = [
    "add_tablix_filter",
    "list_tablix_filters",
    "remove_tablix_filter",
]
