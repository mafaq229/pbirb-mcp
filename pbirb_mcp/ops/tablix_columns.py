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
from pbirb_mcp.ops.styling import _detail_row_index
from pbirb_mcp.ops.tablix import (
    _apply_sort_to_member,
    _apply_visibility_to_member,
    _group_names_in_tablix,
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

    Refuses up front if ``group_name`` is not in the column hierarchy —
    use :func:`set_group_sort` for the row-axis case.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    member = _find_column_member_for_group(tablix, group_name)
    if member is None:
        raise ElementNotFoundError(
            f"column-axis group {group_name!r} not found in tablix "
            f"{tablix_name!r}; for a row-axis group use set_group_sort."
        )
    _apply_sort_to_member(member, sort_expressions)
    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "sort_expressions": list(sort_expressions),
    }


# ---- set_column_group_visibility ------------------------------------------


def set_column_group_visibility(
    path: str,
    tablix_name: str,
    group_name: str,
    visibility_expression: str,
    toggle_textbox: Optional[str] = None,
) -> dict[str, Any]:
    """Set ``<Visibility>`` on a column-axis group's TablixMember.

    Refuses up front if ``group_name`` is not in the column hierarchy —
    use :func:`set_group_visibility` for the row-axis case.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    member = _find_column_member_for_group(tablix, group_name)
    if member is None:
        raise ElementNotFoundError(
            f"column-axis group {group_name!r} not found in tablix "
            f"{tablix_name!r}; for a row-axis group use set_group_visibility."
        )
    _apply_visibility_to_member(member, visibility_expression, toggle_textbox)
    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "visibility_expression": visibility_expression,
        "toggle_textbox": toggle_textbox,
    }


# ---- add_tablix_column / remove_tablix_column (v0.2 commit 2) -------------


def _all_textbox_names(doc: RDLDocument) -> set[str]:
    """Every Textbox.Name in the entire report. Report Builder enforces
    report-wide uniqueness, so this is the right scope for collision check."""
    names = set()
    for tb in doc.root.iter(q("Textbox")):
        n = tb.get("Name")
        if n:
            names.add(n)
    return names


def _build_tablix_column_cell(
    textbox_name: str,
    cell_text: str,
) -> etree._Element:
    """Build one TablixCell for an add_tablix_column insertion. Same shape as
    ``templates._build_cell_textbox`` so the new cell looks like everything
    else Report Builder emits."""
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
    value.text = cell_text
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    default_name = etree.SubElement(tb, qrd("DefaultName"))
    default_name.text = textbox_name
    etree.SubElement(tb, q("Style"))
    return cell


def add_tablix_column(
    path: str,
    tablix_name: str,
    column_name: str,
    expression: str,
    position: Optional[int] = None,
    width: Optional[str] = None,
    header_text: Optional[str] = None,
) -> dict[str, Any]:
    """Append (or insert) a new column into a tablix.

    ``column_name`` is the textbox name placed in the column's *data*
    row (the row whose ``<TablixMember>`` carries ``<Group Name="Details">``,
    walked depth-first through any wrapping groups). It must be unique
    report-wide. ``expression`` is the value placed inside that
    textbox's ``<TextRun>`` (typically ``=Fields!X.Value``).

    For a tablix with ≥ 2 rows: row 0 (header) gets ``header_text``
    (default = ``column_name``) as a literal; the Details row gets
    ``expression``; every other row (group headers between row 0 and
    Details, subtotal/footer rows after Details) gets a blank cell.
    Tablixes with no Details group fall back to "last row = data row".
    Cell textbox names follow ``<column_name>`` for the data-row cell
    and ``<column_name>_<row_index>`` for non-data cells.

    ``position`` is a 0-indexed insertion position; default = append.
    ``width`` defaults to ``"1in"``. Both the body's ``<TablixColumn>``
    and the column hierarchy's leaf ``<TablixMember>`` are inserted at
    the same position; existing column-group wrappers are not modified.
    """
    if width is None:
        width = "1in"

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    # Report-wide name uniqueness — Report Builder enforces it and a
    # collision is a silent corruption.
    if column_name in _all_textbox_names(doc):
        raise ValueError(
            f"textbox name {column_name!r} already exists in this report; "
            "pick a unique name (Report Builder enforces report-wide unique textbox names)."
        )

    body = find_child(tablix, "TablixBody")
    if body is None:
        raise ValueError(f"tablix {tablix_name!r} has no <TablixBody>")
    cols_root = find_child(body, "TablixColumns")
    if cols_root is None:
        cols_root = etree.SubElement(body, q("TablixColumns"))
    existing_cols = find_children(cols_root, "TablixColumn")
    n_cols = len(existing_cols)

    if position is None:
        insert_at = n_cols
    else:
        if position < 0 or position > n_cols:
            raise IndexError(f"position {position} out of range; tablix has {n_cols} column(s)")
        insert_at = position

    # ---- body: insert <TablixColumn> at insert_at ------------------------
    new_column = etree.Element(q("TablixColumn"))
    etree.SubElement(new_column, q("Width")).text = width
    if insert_at == n_cols:
        cols_root.append(new_column)
    else:
        cols_root.insert(insert_at, new_column)

    # ---- column hierarchy: insert a top-level <TablixMember /> at the
    # mirroring position. We add at the top level so a column added next
    # to (rather than under) an existing column group lands as a sibling.
    members_root = _column_hierarchy_members(tablix)
    new_member = etree.Element(q("TablixMember"))
    top_members = list(members_root)
    if insert_at >= len(top_members):
        members_root.append(new_member)
    else:
        members_root.insert(insert_at, new_member)

    # ---- body rows: insert a fresh cell at insert_at ---------------------
    rows_root = find_child(body, "TablixRows")
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    n_rows = len(rows)
    header_text_value = header_text if header_text is not None else column_name

    # Find the data row by walking the row hierarchy for the Details leaf.
    # This is correct after add_row_group nests the original hierarchy and
    # after add_subtotal_row appends a footer row — the literal "last row"
    # is the subtotal in that case, not the detail. Fall back to last-row
    # behavior for tablixes without a Details group.
    detail_idx = _detail_row_index(tablix)
    data_row_idx = detail_idx if detail_idx is not None else n_rows - 1

    for i, row in enumerate(rows):
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            cells_root = etree.SubElement(row, q("TablixCells"))

        if n_rows == 1:
            # Single-row tablix: that row is the data row.
            cell_text = expression
            tb_name = column_name
        elif i == data_row_idx:
            cell_text = expression
            tb_name = column_name
        elif i == 0:
            # First row of multi-row tablix: header row.
            cell_text = header_text_value
            tb_name = f"{column_name}_{i}"
        else:
            # Middle rows (between header and Details) and footer rows
            # (subtotals after Details): blank cells.
            cell_text = ""
            tb_name = f"{column_name}_{i}"

        new_cell = _build_tablix_column_cell(tb_name, cell_text)
        if insert_at == len(find_children(cells_root, "TablixCell")):
            cells_root.append(new_cell)
        else:
            cells_root.insert(insert_at, new_cell)

    doc.save()
    return {
        "tablix": tablix_name,
        "column_name": column_name,
        "position": insert_at,
        "width": width,
    }


def remove_tablix_column(
    path: str,
    tablix_name: str,
    column_name: str,
) -> dict[str, Any]:
    """Remove the column whose data-row textbox is named ``column_name``.

    The column-index is discovered by scanning every row's cells for a
    textbox with that exact name; the first hit's column-index is the
    target. Removes the matching ``<TablixColumn>`` from
    ``<TablixColumns>``, the matching top-level ``<TablixMember>`` from
    the column hierarchy, and the cell at that index from every row.

    Errors with :class:`ElementNotFoundError` if no row contains a
    textbox with the given name.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    body = find_child(tablix, "TablixBody")
    if body is None:
        raise ElementNotFoundError(
            f"tablix {tablix_name!r} has no <TablixBody>; nothing to remove."
        )
    rows_root = find_child(body, "TablixRows")
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []

    target_index: Optional[int] = None
    for row in rows:
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            continue
        for col_idx, cell in enumerate(find_children(cells_root, "TablixCell")):
            tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
            if tb is not None and tb.get("Name") == column_name:
                target_index = col_idx
                break
        if target_index is not None:
            break
    if target_index is None:
        raise ElementNotFoundError(
            f"no textbox named {column_name!r} found in any row of tablix {tablix_name!r}"
        )

    # ---- body: remove <TablixColumn> at target_index ---------------------
    cols_root = find_child(body, "TablixColumns")
    if cols_root is not None:
        cols = find_children(cols_root, "TablixColumn")
        if target_index < len(cols):
            cols_root.remove(cols[target_index])

    # ---- column hierarchy: remove the top-level <TablixMember> at the
    # same position. If a column group wraps that position, fall back to
    # leaving the hierarchy alone — defensive, since group-wrapped removal
    # is more nuanced and out of scope for this commit.
    members_root = _column_hierarchy_members(tablix)
    top_members = list(members_root)
    if target_index < len(top_members):
        candidate = top_members[target_index]
        # Only remove a leaf-style member (no <Group> child); a group
        # wrapper would mean the column belongs to a group whose width
        # we'd otherwise silently change.
        if find_child(candidate, "Group") is None:
            members_root.remove(candidate)

    # ---- rows: remove the cell at target_index from every row ------------
    for row in rows:
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            continue
        cells = find_children(cells_root, "TablixCell")
        if target_index < len(cells):
            cells_root.remove(cells[target_index])

    doc.save()
    return {"tablix": tablix_name, "removed_column": column_name, "position": target_index}


__all__ = [
    "add_column_group",
    "add_tablix_column",
    "remove_column_group",
    "remove_tablix_column",
    "set_column_group_sort",
    "set_column_group_visibility",
]
