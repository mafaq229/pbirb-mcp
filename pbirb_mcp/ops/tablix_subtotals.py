"""Group subtotal rows / columns (v0.2 commit 3, v0.4 commit 19, #32).

``add_subtotal_row`` / ``add_subtotal_column`` add one static hierarchy
member for a row/column group plus the matching body row/column. Each
new cell either holds an aggregate expression (when the caller supplies
one for it) or is blank.

Placement (#32) is derived from the hierarchy, never computed
separately: body row/column *i* belongs to hierarchy leaf *i*
(depth-first), so the new body row/column goes exactly at the index its
new leaf ends up owning — just after the group's last leaf for a footer
/ ``after``, at the group's first leaf for a header / ``before``. Two
scopes:

* ``scope="group"`` (default) — the static member goes *inside* the
  group: a per-instance subtotal (one per Region, ...). A header goes
  right after the group's own header row when it has one.
* ``scope="total"`` — the static member becomes a *sibling* of the group
  member in its parent: it renders once per parent instance, i.e. a
  grand total for an outermost group (Report Builder's "Add Total").
  Row totals carry ``KeepWithGroup`` (``Before`` after the group,
  ``After`` before it) like Report Builder's.

When the would-be siblings carry ``TablixHeader`` cells (a matrix), the
new member gets header levels of the same depth and size.

Row aggregates are matched to columns by **textbox name in the group's
data row** — :func:`pbirb_mcp.core.tablix_shape.data_leaf` scoped to the
group (its detail leaf, else the first leaf of the innermost dynamic
group in its subtree) — the same handle used by
:func:`add_tablix_column`. Column aggregates are addressed by body row
index.

New textboxes are named ``<group>_SubtotalHeader_<col>`` /
``<group>_SubtotalFooter_<col>`` (scope ``group``), ``<group>_Total_<col>``
(row scope ``total``) and ``<group>_TotalColumn_<row>`` (columns), all
suffixed through the report-wide name registry when taken.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
from pbirb_mcp.core.tablix_shape import (
    axis_leaves,
    body_count,
    data_row_index,
    find_group_member,
    insert_nth_child,
    leaf_span,
    static_member_like,
    subtree_leaves,
)
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd

_VALID_POSITIONS = frozenset({"header", "footer"})
_VALID_COLUMN_POSITIONS = frozenset({"before", "after"})
_VALID_SCOPES = frozenset({"group", "total"})


def _find_row_member_wrapper(tablix: etree._Element, group_name: str) -> Optional[etree._Element]:
    """Row-axis TablixMember whose <Group Name=...> matches (``None`` if
    absent or only present on the column axis)."""
    return find_group_member(tablix, "row", group_name)


def _find_column_member_wrapper(
    tablix: etree._Element, group_name: str
) -> Optional[etree._Element]:
    """Mirror of :func:`_find_row_member_wrapper` for the column axis."""
    return find_group_member(tablix, "column", group_name)


def _data_row_textbox_names(
    tablix: etree._Element, group: Optional[etree._Element] = None
) -> list[Optional[str]]:
    """Textbox name at each column index of the data row.

    The data row is :func:`~pbirb_mcp.core.tablix_shape.data_leaf`'s row,
    scoped to ``group`` (a row-group ``TablixMember``) when given. With a
    group that always resolves (the group's own first leaf at worst);
    without one, an all-static tablix falls back to its last row.
    """
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    if rows_root is None:
        return []
    rows = find_children(rows_root, "TablixRow")
    if not rows:
        return []

    data_idx = data_row_index(tablix, group)
    if data_idx is None and group is not None:
        data_idx = leaf_span(tablix, "row", group)[0]
    target_row = rows[data_idx] if data_idx is not None and data_idx < len(rows) else rows[-1]

    cells_root = find_child(target_row, "TablixCells")
    if cells_root is None:
        return []
    out: list[Optional[str]] = []
    for cell in find_children(cells_root, "TablixCell"):
        tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
        out.append(tb.get("Name") if tb is not None else None)
    return out


def _build_subtotal_cell(textbox_name: str, cell_text: str) -> etree._Element:
    cell = etree.Element(q("TablixCell"))
    contents = etree.SubElement(cell, q("CellContents"))
    tb = etree.SubElement(contents, q("Textbox"), Name=textbox_name)
    etree.SubElement(tb, q("CanGrow")).text = "true"
    etree.SubElement(tb, q("KeepTogether")).text = "true"
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    paragraph = etree.SubElement(paragraphs, q("Paragraph"))
    textruns = etree.SubElement(paragraph, q("TextRuns"))
    textrun = etree.SubElement(textruns, q("TextRun"))
    value = etree.SubElement(textrun, q("Value"))
    value.text = encode_text(cell_text)
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    default_name = etree.SubElement(tb, qrd("DefaultName"))
    default_name.text = textbox_name
    etree.SubElement(tb, q("Style"))
    return cell


def _check_scope(scope: str) -> None:
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope {scope!r} not valid; expected one of {sorted(_VALID_SCOPES)}")


def _has_keep_after(member: etree._Element) -> bool:
    keep = find_child(member, "KeepWithGroup")
    return keep is not None and (keep.text or "").strip() == "After"


def _is_static_leaf(member: etree._Element) -> bool:
    return find_child(member, "Group") is None and find_child(member, "TablixMembers") is None


def _place_member(
    tablix: etree._Element,
    axis: str,
    wrapper: etree._Element,
    leading: bool,
    scope: str,
    new_member_factory,
) -> tuple[etree._Element, int, list[str]]:
    """Insert a static member for ``wrapper``'s group and return
    ``(member, body_index, header_textbox_names)``.

    ``leading`` = header / before. The body index is the new leaf's own
    index, read back from the hierarchy after insertion.
    """
    if scope == "group":
        inner = find_child(wrapper, "TablixMembers")
        children = find_children(inner, "TablixMember") if inner is not None else []
        if leading:
            # Right after the group's own header row (a leading static leaf
            # marked KeepWithGroup=After, as add_row_group emits), else first.
            first = children[0]
            after_header = axis == "row" and _is_static_leaf(first) and _has_keep_after(first)
            anchor, before = (first, False) if after_header else (first, True)
        else:
            anchor, before = children[-1], False
    else:
        anchor, before = wrapper, leading

    member, header_names = new_member_factory(anchor)
    if before:
        anchor.addprevious(member)
    else:
        anchor.addnext(member)
    leaf = subtree_leaves(member)[0]
    index = next(i for i, candidate in enumerate(axis_leaves(tablix, axis)) if candidate is leaf)
    return member, index, header_names


def _set_keep_with_group(member: etree._Element, value: str) -> None:
    keep = etree.Element(q("KeepWithGroup"))
    keep.text = value
    # RDL child order is free (xsd:choice); Report Builder emits
    # KeepWithGroup after TablixHeader/TablixMembers.
    member.append(keep)


def _require_consistent(tablix: etree._Element, axis: str) -> None:
    n_leaves = len(axis_leaves(tablix, axis))
    n_body = body_count(tablix, axis)
    if n_leaves != n_body:
        raise ValueError(
            f"tablix {tablix.get('Name')!r} has {n_leaves} {axis}-hierarchy leaves for "
            f"{n_body} body {axis}s, so {axis} positions are ambiguous; refusing to add "
            "a subtotal"
        )


def add_subtotal_row(
    path: str,
    tablix_name: str,
    group_name: str,
    aggregates: list[dict[str, str]],
    position: str = "footer",
    scope: str = "group",
) -> dict[str, Any]:
    """Add a subtotal row for row group ``group_name``.

    ``aggregates`` is a list of ``{"column": <textbox-name>, "expression":
    <aggregate>}`` entries. ``column`` matches against the **group's
    data row** textbox names (its detail row, or in a matrix the
    innermost group's row) — the same identifiers ``add_tablix_column``
    uses for its ``column_name``. Columns not listed get blank cells.

    ``position`` is ``"footer"`` (default — the row goes just after the
    group's last row) or ``"header"`` (at the group's first row — after
    the group's own header row when ``scope="group"`` and it has one).

    ``scope`` is ``"group"`` (default — inside the group, one subtotal per
    group instance) or ``"total"`` (a sibling of the group member, once
    per parent instance: a grand total for an outermost group, with
    ``KeepWithGroup`` Before/After).

    Every pre-existing row keeps its hierarchy leaf; the payload's
    ``row_index`` is the new row's body index.
    """
    if position not in _VALID_POSITIONS:
        raise ValueError(
            f"position {position!r} not valid; expected one of {sorted(_VALID_POSITIONS)}"
        )
    _check_scope(scope)

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    wrapper = _find_row_member_wrapper(tablix, group_name)
    if wrapper is None:
        raise ElementNotFoundError(
            f"row-axis group {group_name!r} not found in tablix {tablix_name!r}"
        )
    inner_members = find_child(wrapper, "TablixMembers")
    if scope == "group" and (
        inner_members is None or not find_children(inner_members, "TablixMember")
    ):
        raise ValueError(
            f"group {group_name!r} has no nested members (its own member is the leaf "
            "that owns its row), so a per-instance subtotal has nowhere to go; use "
            "scope='total' for a total row next to the group."
        )
    _require_consistent(tablix, "row")

    column_names = _data_row_textbox_names(tablix, wrapper)
    aggregates_by_column = {a["column"]: a["expression"] for a in aggregates}
    missing = [c for c in aggregates_by_column if c not in column_names]
    if missing:
        raise ElementNotFoundError(
            f"aggregate column(s) {missing!r} not found among data-row "
            f"textbox names {[c for c in column_names if c is not None]!r} "
            f"in tablix {tablix_name!r}"
        )

    registry = names.NameRegistry(doc)
    if scope == "group":
        stem = f"{group_name}_Subtotal{'Footer' if position == 'footer' else 'Header'}"
    else:
        stem = f"{group_name}_Total"
    emitted = [registry.unique(f"{stem}_{col_idx}") for col_idx in range(len(column_names))]

    def factory(reference: etree._Element):
        return static_member_like(reference, lambda: registry.unique(f"{stem}_RowHeader"))

    member, row_index, header_names = _place_member(
        tablix, "row", wrapper, position == "header", scope, factory
    )
    _set_keep_with_group(member, "After" if position == "header" else "Before")

    new_row = etree.Element(q("TablixRow"))
    etree.SubElement(new_row, q("Height")).text = "0.25in"
    cells_root = etree.SubElement(new_row, q("TablixCells"))
    for col_idx, col_textbox in enumerate(column_names):
        cell_text = (
            aggregates_by_column[col_textbox]
            if col_textbox is not None and col_textbox in aggregates_by_column
            else ""
        )
        cells_root.append(_build_subtotal_cell(emitted[col_idx], cell_text))

    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows")
    if rows_root is None:
        rows_root = etree.SubElement(body, q("TablixRows"))
    insert_nth_child(rows_root, "TablixRow", row_index, new_row)

    doc.save()
    result: dict[str, Any] = {
        "tablix": tablix_name,
        "group": group_name,
        "position": position,
        "scope": scope,
        "aggregates": list(aggregates),
        "row_index": row_index,
        "textboxes": emitted,
    }
    if header_names:
        result["header_textboxes"] = header_names
    return result


# ---- add_subtotal_column (v0.4 commit 19) ---------------------------------


def _data_column_count(tablix: etree._Element) -> int:
    """Number of <TablixColumn> elements in TablixBody/TablixColumns."""
    return body_count(tablix, "column")


def add_subtotal_column(
    path: str,
    tablix_name: str,
    group_name: str,
    aggregates: list[dict[str, Any]],
    position: str = "after",
    width: str = "1in",
    scope: str = "group",
) -> dict[str, Any]:
    """Add a subtotal column for column group ``group_name``.

    Column-axis mirror of :func:`add_subtotal_row`: a static
    column-hierarchy member, a new ``<TablixColumn>`` and a cell in every
    body row, at the body index the new leaf owns.

    ``aggregates`` is a list of ``{"row": <int>, "expression":
    <aggregate>}`` entries. ``row`` is the 0-based body row index;
    rows not listed get a blank cell. Negative indices and indices
    ≥ row count raise :class:`ElementNotFoundError`.

    ``position`` is ``"after"`` (default — just right of the group's last
    column) or ``"before"`` (at the group's first column). ``scope`` is
    ``"group"`` (default — inside the group: one subtotal column per group
    instance) or ``"total"`` (a sibling of the group member: once per
    parent instance — a grand total column for an outermost group).
    Static columns next to the group stay outside it.

    ``width`` is the RDL size string for the new ``<TablixColumn>``;
    defaults to ``"1in"``.

    Returns ``{tablix, group, position, scope, aggregates, kind:
    'TablixMember', column_index, changed, textboxes}``.
    """
    if position not in _VALID_COLUMN_POSITIONS:
        raise ValueError(
            f"position {position!r} not valid; expected one of {sorted(_VALID_COLUMN_POSITIONS)}"
        )
    _check_scope(scope)
    width = units.validate_size(width, field="width", allow_zero=False)

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    wrapper = _find_column_member_wrapper(tablix, group_name)
    if wrapper is None:
        raise ElementNotFoundError(
            f"column-axis group {group_name!r} not found in tablix {tablix_name!r}"
        )
    inner_members = find_child(wrapper, "TablixMembers")
    if scope == "group" and (
        inner_members is None or not find_children(inner_members, "TablixMember")
    ):
        raise ValueError(
            f"column group {group_name!r} has no nested members (its own member is the "
            "leaf that owns its column), so a per-instance subtotal has nowhere to go; "
            "use scope='total' for a total column next to the group."
        )

    body = find_child(tablix, "TablixBody")
    if body is None:
        raise ValueError(f"tablix {tablix_name!r} has no <TablixBody>")
    rows_root = find_child(body, "TablixRows")
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    row_count = len(rows)

    # Validate aggregate row indices BEFORE mutating anything.
    aggregates_by_row: dict[int, str] = {}
    for entry in aggregates:
        row_idx = entry.get("row")
        expression = entry.get("expression")
        if not isinstance(row_idx, int):
            raise ValueError(f"aggregate entry missing integer 'row' key: {entry!r}")
        if not isinstance(expression, str):
            raise ValueError(f"aggregate entry missing string 'expression' key: {entry!r}")
        if row_idx < 0 or row_idx >= row_count:
            raise ElementNotFoundError(
                f"aggregate row index {row_idx} out of range — tablix "
                f"{tablix_name!r} has {row_count} body row(s)"
            )
        aggregates_by_row[row_idx] = expression

    cols_root = find_child(body, "TablixColumns")
    if cols_root is None:
        cols_root = etree.SubElement(body, q("TablixColumns"))
    n_leaves = len(axis_leaves(tablix, "column"))
    n_cols = _data_column_count(tablix)

    registry = names.NameRegistry(doc)
    label = f"{group_name}_TotalColumn"
    warnings: list[str] = []

    def factory(reference: etree._Element):
        return static_member_like(reference, lambda: registry.unique(f"{label}Header"))

    if n_leaves == n_cols:
        member, column_insert_index, header_names = _place_member(
            tablix, "column", wrapper, position == "before", scope, factory
        )
    else:
        # The column hierarchy is already out of step with the body (e.g.
        # a column group added by add_column_group before #29), so leaf
        # indices can't be trusted. Keep the legacy "the group spans the
        # right-most columns" placement for scope='group' and say so.
        if scope != "group":
            _require_consistent(tablix, "column")
        inner_leaves = find_children(inner_members, "TablixMember")
        member, header_names = factory(None)
        if position == "after":
            column_insert_index = n_cols
            inner_members.append(member)
        else:
            column_insert_index = max(0, n_cols - len(inner_leaves))
            inner_members.insert(0, member)
        warnings.append(
            f"the column hierarchy had {n_leaves} leaves for {n_cols} body columns before "
            "this call (an existing inconsistency Report Builder rejects); the subtotal "
            "column was placed assuming the group spans the right-most columns"
        )

    new_column = etree.Element(q("TablixColumn"))
    etree.SubElement(new_column, q("Width")).text = width
    insert_nth_child(cols_root, "TablixColumn", column_insert_index, new_column)

    emitted: list[str] = []
    for row_idx, row in enumerate(rows):
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            cells_root = etree.SubElement(row, q("TablixCells"))
        textbox_name = registry.unique(f"{label}_{row_idx}")
        emitted.append(textbox_name)
        new_cell = _build_subtotal_cell(textbox_name, aggregates_by_row.get(row_idx, ""))
        insert_nth_child(cells_root, "TablixCell", column_insert_index, new_cell)

    doc.save()
    result: dict[str, Any] = {
        "tablix": tablix_name,
        "group": group_name,
        "position": position,
        "scope": scope,
        "aggregates": list(aggregates),
        "kind": "TablixMember",
        "column_index": column_insert_index,
        "changed": ["member_added", "column_added", "cells_added"],
        "textboxes": emitted,
    }
    if header_names:
        result["header_textboxes"] = header_names
    if warnings:
        result["warnings"] = warnings
    return result


__all__ = ["add_subtotal_column", "add_subtotal_row"]
