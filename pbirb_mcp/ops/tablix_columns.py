"""Column-hierarchy tablix tools (v0.2 commit 1).

Mirrors the row-group API in ``pbirb_mcp.ops.tablix`` but operates on
``<TablixColumnHierarchy>`` and the body's ``<TablixColumns>`` /
per-row ``<TablixCells>`` rather than ``<TablixRowHierarchy>`` and
``<TablixRows>``.

What ``add_column_group`` does:

1. Wraps the existing top-level column hierarchy under a new outer
   ``<TablixMember>`` carrying ``<Group Name=...>``. Existing members
   become children of the wrapper's ``<TablixMembers>``. (Symmetric to
   ``add_row_group`` but column groups don't emit a ``KeepWithGroup``
   leaf header — that attribute applies to row members only.)
2. Inserts a fresh ``<TablixColumn>`` at body column 0 with a default
   1in width.
3. Inserts a fresh cell at column 0 of every existing ``<TablixRow>``.
   The topmost row's new cell holds the group expression; the rest
   are blank textboxes. Cell textbox names follow the pattern
   ``<group>_HeaderCol_<row_index>`` so they're report-wide unique
   given a unique group name.

``set_column_group_sort`` and ``set_column_group_visibility`` are thin
wrappers over the row-group equivalents in ``pbirb_mcp.ops.tablix`` —
those underlying helpers are hierarchy-agnostic, so the wrappers add
a hierarchy sanity check that produces a clearer error than the
generic versions when an LLM calls the wrong tool for the wrong axis.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd
from pbirb_mcp.ops.tablix import (
    _group_names_in_tablix,
    set_group_sort,
    set_group_visibility,
)

# ---- column-hierarchy helpers ---------------------------------------------


def _column_hierarchy_members(tablix: etree._Element) -> etree._Element:
    """Return the top-level <TablixColumnHierarchy>/<TablixMembers> element."""
    hierarchy = find_child(tablix, "TablixColumnHierarchy")
    if hierarchy is None:
        # Defensive — every well-formed Tablix has a column hierarchy.
        hierarchy = etree.SubElement(tablix, q("TablixColumnHierarchy"))
    members = find_child(hierarchy, "TablixMembers")
    if members is None:
        members = etree.SubElement(hierarchy, q("TablixMembers"))
    return members


def _find_column_member_for_group(
    tablix: etree._Element, group_name: str
) -> Optional[etree._Element]:
    """Find a TablixMember bearing ``<Group Name=...>`` *only* in the column
    hierarchy. Returns None for unknown group OR for a row-axis match."""
    column_hierarchy = find_child(tablix, "TablixColumnHierarchy")
    if column_hierarchy is None:
        return None
    for member in column_hierarchy.iter(q("TablixMember")):
        group = find_child(member, "Group")
        if group is not None and group.get("Name") == group_name:
            return member
    return None


def _row_count(tablix: etree._Element) -> int:
    body = find_child(tablix, "TablixBody")
    if body is None:
        return 0
    rows_root = find_child(body, "TablixRows")
    if rows_root is None:
        return 0
    return len(find_children(rows_root, "TablixRow"))


def _build_column_header_cell(
    group_name: str,
    cell_text: str,
    row_index: int,
) -> etree._Element:
    """Build one ``<TablixCell>`` for the new column-group header column.

    ``cell_text`` is the value placed inside the inner ``<TextRun>``;
    callers pass the group expression for row 0 and an empty string for
    rows below. Textbox name is ``<group>_HeaderCol_<row_index>`` so the
    report-wide uniqueness Report Builder enforces lines up cleanly.
    """
    cell = etree.Element(q("TablixCell"))
    contents = etree.SubElement(cell, q("CellContents"))
    textbox_name = f"{group_name}_HeaderCol_{row_index}"
    tb = etree.SubElement(contents, q("Textbox"), Name=textbox_name)
    etree.SubElement(tb, q("CanGrow")).text = "true"
    etree.SubElement(tb, q("KeepTogether")).text = "true"
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    paragraph = etree.SubElement(paragraphs, q("Paragraph"))
    textruns = etree.SubElement(paragraph, q("TextRuns"))
    textrun = etree.SubElement(textruns, q("TextRun"))
    value = etree.SubElement(textrun, q("Value"))
    value.text = cell_text
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    default_name = etree.SubElement(tb, qrd("DefaultName"))
    default_name.text = textbox_name
    etree.SubElement(tb, q("Style"))
    return cell


# ---- add_column_group ------------------------------------------------------


def add_column_group(
    path: str,
    tablix_name: str,
    group_name: str,
    group_expression: str,
    parent_group: Optional[str] = None,
) -> dict[str, Any]:
    """Wrap the current top-level column hierarchy in a new outer group.

    A new ``<TablixMember>`` is created at the top of the column hierarchy
    holding ``<Group Name=...>``; previously top-level members move
    underneath it. A matching column is inserted at body column 0 with a
    default 1in width, and a header cell is inserted at column 0 of every
    existing body row. The topmost cell holds the group expression; the
    others are blank textboxes.

    ``parent_group`` is reserved for nesting a new group beneath an
    existing one — not yet implemented.
    """
    if parent_group is not None:
        raise NotImplementedError(
            "parent_group nesting is not yet supported; only top-level "
            "outer-group wrapping is implemented in this commit."
        )

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    # Group names are unique per tablix across both axes — Report Builder
    # enforces this and we error early to avoid a silent corruption.
    if group_name in _group_names_in_tablix(tablix):
        raise ValueError(f"group name {group_name!r} already exists in tablix {tablix_name!r}")

    # ---- mutate column hierarchy ------------------------------------------
    members_root = _column_hierarchy_members(tablix)
    existing_children = list(members_root)

    new_outer = etree.Element(q("TablixMember"))
    group = etree.SubElement(new_outer, q("Group"), Name=group_name)
    expr_root = etree.SubElement(group, q("GroupExpressions"))
    expr_node = etree.SubElement(expr_root, q("GroupExpression"))
    expr_node.text = group_expression

    inner_members = etree.SubElement(new_outer, q("TablixMembers"))
    for child in existing_children:
        members_root.remove(child)
        inner_members.append(child)
    # Degenerate fallback: a tablix with no existing column members would
    # leave <TablixMembers/> empty after wrapping, which the XSD rejects.
    # Add a single bare leaf to keep the structure valid.
    if not list(inner_members):
        etree.SubElement(inner_members, q("TablixMember"))

    members_root.append(new_outer)

    # ---- mutate body: new column at position 0 ---------------------------
    body = find_child(tablix, "TablixBody")
    if body is None:
        body = etree.SubElement(tablix, q("TablixBody"))
    cols_root = find_child(body, "TablixColumns")
    if cols_root is None:
        cols_root = etree.SubElement(body, q("TablixColumns"))

    new_column = etree.Element(q("TablixColumn"))
    width = etree.SubElement(new_column, q("Width"))
    # 1in matches the default Report Builder uses when adding a column
    # group via the GUI; users can resize after via set_textbox_style or
    # a future set_tablix_column_width tool.
    width.text = "1in"
    cols_root.insert(0, new_column)

    # ---- mutate body: new cell at column 0 of every row ------------------
    rows_root = find_child(body, "TablixRows")
    if rows_root is not None:
        for i, row in enumerate(find_children(rows_root, "TablixRow")):
            cells_root = find_child(row, "TablixCells")
            if cells_root is None:
                # Row with no cells is degenerate; create cells_root rather
                # than skip so the new column is consistent everywhere.
                cells_root = etree.SubElement(row, q("TablixCells"))
            cells_root.insert(
                0,
                _build_column_header_cell(
                    group_name=group_name,
                    cell_text=group_expression if i == 0 else "",
                    row_index=i,
                ),
            )

    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "expression": group_expression,
    }


# ---- remove_column_group --------------------------------------------------


def remove_column_group(
    path: str,
    tablix_name: str,
    group_name: str,
) -> dict[str, Any]:
    """Inverse of :func:`add_column_group`. Unwraps the group's wrapped
    children back to the top of the column hierarchy and removes the
    matching body column at position 0 (along with each row's first cell).

    Errors with :class:`ElementNotFoundError` if ``group_name`` doesn't
    exist on the **column** axis — a group of that name in the row
    hierarchy is not a match for this tool.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    member = _find_column_member_for_group(tablix, group_name)
    if member is None:
        raise ElementNotFoundError(
            f"column-axis group {group_name!r} not found in tablix {tablix_name!r}"
        )

    inner_members = find_child(member, "TablixMembers")
    if inner_members is None or len(list(inner_members)) == 0:
        raise ValueError(
            f"column group {group_name!r} has no nested children; refusing "
            "to remove a leaf group automatically."
        )

    inner_children = list(inner_members)
    parent_members = member.getparent()
    member_idx = list(parent_members).index(member)

    # Replace the wrapper at its original position with its wrapped originals.
    parent_members.remove(member)
    for offset, child in enumerate(inner_children):
        inner_members.remove(child)
        parent_members.insert(member_idx + offset, child)

    # ---- body: drop column 0 + drop cell 0 of every row ------------------
    body = find_child(tablix, "TablixBody")
    if body is not None:
        cols_root = find_child(body, "TablixColumns")
        if cols_root is not None:
            cols = find_children(cols_root, "TablixColumn")
            if cols:
                cols_root.remove(cols[0])
        rows_root = find_child(body, "TablixRows")
        if rows_root is not None:
            for row in find_children(rows_root, "TablixRow"):
                cells_root = find_child(row, "TablixCells")
                if cells_root is None:
                    continue
                cells = find_children(cells_root, "TablixCell")
                if cells:
                    cells_root.remove(cells[0])

    doc.save()
    return {"tablix": tablix_name, "removed": group_name}


# ---- set_column_group_sort ------------------------------------------------


def set_column_group_sort(
    path: str,
    tablix_name: str,
    group_name: str,
    sort_expressions: list[str],
) -> dict[str, Any]:
    """Set ``<SortExpressions>`` on a column-axis group's TablixMember.

    Behaves identically to :func:`pbirb_mcp.ops.tablix.set_group_sort` but
    refuses up front if ``group_name`` is not in the column hierarchy.
    """
    # Hierarchy sanity check before delegating — gives the LLM a clearer
    # error than the generic "group not found" when it's invoked on the
    # wrong axis.
    doc_check = RDLDocument.open(path)
    tablix_check = resolve_tablix(doc_check, tablix_name)
    if _find_column_member_for_group(tablix_check, group_name) is None:
        raise ElementNotFoundError(
            f"column-axis group {group_name!r} not found in tablix "
            f"{tablix_name!r}; for a row-axis group use set_group_sort."
        )
    return set_group_sort(
        path=path,
        tablix_name=tablix_name,
        group_name=group_name,
        sort_expressions=sort_expressions,
    )


# ---- set_column_group_visibility ------------------------------------------


def set_column_group_visibility(
    path: str,
    tablix_name: str,
    group_name: str,
    visibility_expression: str,
    toggle_textbox: Optional[str] = None,
) -> dict[str, Any]:
    """Set ``<Visibility>`` on a column-axis group's TablixMember.

    Behaves identically to
    :func:`pbirb_mcp.ops.tablix.set_group_visibility` but refuses up front
    if ``group_name`` is not in the column hierarchy.
    """
    doc_check = RDLDocument.open(path)
    tablix_check = resolve_tablix(doc_check, tablix_name)
    if _find_column_member_for_group(tablix_check, group_name) is None:
        raise ElementNotFoundError(
            f"column-axis group {group_name!r} not found in tablix "
            f"{tablix_name!r}; for a row-axis group use set_group_visibility."
        )
    return set_group_visibility(
        path=path,
        tablix_name=tablix_name,
        group_name=group_name,
        visibility_expression=visibility_expression,
        toggle_textbox=toggle_textbox,
    )


__all__ = [
    "add_column_group",
    "remove_column_group",
    "set_column_group_sort",
    "set_column_group_visibility",
]
