"""Column-hierarchy tablix tools (v0.2 commit 1).

Mirrors the row-group API in ``pbirb_mcp.ops.tablix`` but operates on
``<TablixColumnHierarchy>`` and the body's ``<TablixColumns>`` /
per-row ``<TablixCells>`` rather than ``<TablixRowHierarchy>`` and
``<TablixRows>``.

What ``add_column_group`` does:

1. Wraps the existing top-level column hierarchy under a new outer
   ``<TablixMember>`` carrying ``<Group Name=...>``. Existing members
   become children of the wrapper's ``<TablixMembers>``.
2. Gives that member a ``<TablixHeader>`` — the group caption, rendered
   as a header row above the body — holding a ``<group>_Header``
   textbox with the group expression. The body (columns and cells) is
   not touched: column-hierarchy leaves must stay 1:1 with body
   columns, and wrapping adds no leaf.
3. Grows (or creates) the ``<TablixCorner>`` when the tablix has row
   headers, so the corner stays column-header depth x row-header depth
   (:func:`pbirb_mcp.core.tablix_headers.ensure_corner`).

``set_column_group_sort`` and ``set_column_group_visibility`` are thin
wrappers over the row-group equivalents in ``pbirb_mcp.ops.tablix`` —
those underlying helpers are hierarchy-agnostic, so the wrappers add
a hierarchy sanity check that produces a clearer error than the
generic versions when an LLM calls the wrong tool for the wrong axis.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names, tablix_headers, tablix_spans, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
from pbirb_mcp.core.tablix_shape import column_leaves, data_row_index
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd
from pbirb_mcp.ops.tablix import (
    _UNSET,
    _apply_sort_to_member,
    _apply_visibility_to_member,
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


def _legacy_header_column(tablix: etree._Element, group_name: str) -> bool:
    """True iff ``tablix`` still carries the body column that
    ``add_column_group`` used to insert at column 0 (the defect of issue
    #29): one more body column than column leaves, and every row's cell 0
    is a ``<group>_HeaderCol_<i>`` textbox."""
    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    rows_root = find_child(body, "TablixRows") if body is not None else None
    if cols_root is None or rows_root is None:
        return False
    if len(find_children(cols_root, "TablixColumn")) != len(column_leaves(tablix)) + 1:
        return False
    rows = find_children(rows_root, "TablixRow")
    if not rows:
        return False
    for row in rows:
        cells_root = find_child(row, "TablixCells")
        cells = find_children(cells_root, "TablixCell") if cells_root is not None else []
        tb = cells[0].find(f"{q('CellContents')}/{q('Textbox')}") if cells else None
        if tb is None or not (tb.get("Name") or "").startswith(f"{group_name}_HeaderCol_"):
            return False
    return True


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
    holding ``<Group Name=...>`` and a ``<TablixHeader>`` (0.25in, one
    textbox ``<group>_Header`` showing the group expression) — the group's
    caption is a new header row above the body, not a body column.
    Previously top-level members move underneath it, so body columns and
    cells are untouched and column leaves stay equal to body columns.
    When the tablix has row headers, the ``<TablixCorner>`` gains a row
    (or is created) so it stays column-header depth x row-header depth.

    ``parent_group`` is reserved for nesting a new group beneath an
    existing one — not yet implemented.
    """
    if parent_group is not None:
        raise NotImplementedError(
            "parent_group nesting is not yet supported; only top-level "
            "outer-group wrapping is implemented in this commit."
        )

    names.validate_identifier(group_name, "group")
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    # Group names share the report-wide namespace with report items —
    # Report Builder enforces this; error early, before any mutation.
    registry = names.NameRegistry(doc)
    registry.ensure_unique(group_name)
    registry.claim(group_name)

    # Refuse to build on a tablix whose columns already disagree with its
    # column leaves — wrapping would bake the mismatch in further.
    n_leaves = len(column_leaves(tablix))
    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    n_cols = len(find_children(cols_root, "TablixColumn")) if cols_root is not None else 0
    if n_leaves == 0 or n_leaves != n_cols:
        raise ValueError(
            f"tablix {tablix_name!r} has {n_leaves} column-hierarchy leaves but "
            f"{n_cols} body columns; refusing to add a column group to an "
            "inconsistent tablix (a group added by an older add_column_group "
            "can be undone with remove_column_group)."
        )
    header_name = registry.unique(f"{group_name}_Header")

    # ---- mutate column hierarchy ------------------------------------------
    members_root = _column_hierarchy_members(tablix)
    existing_children = list(members_root)

    new_outer = etree.Element(q("TablixMember"))
    group = etree.SubElement(new_outer, q("Group"), Name=group_name)
    expr_root = etree.SubElement(group, q("GroupExpressions"))
    expr_node = etree.SubElement(expr_root, q("GroupExpression"))
    expr_node.text = encode_text(group_expression)
    new_outer.append(
        tablix_headers.new_header(
            tablix_headers.DEFAULT_HEADER_SIZE[tablix_headers.COLUMN],
            header_name,
            group_expression,
        )
    )

    inner_members = etree.SubElement(new_outer, q("TablixMembers"))
    for child in existing_children:
        members_root.remove(child)
        inner_members.append(child)
    members_root.append(new_outer)

    # ---- corner: one more column-header row ------------------------------
    corner: dict[str, Any] = {"created": [], "removed": []}
    if tablix_headers.header_depth(tablix, tablix_headers.ROW) > 0:
        corner = tablix_headers.ensure_corner(tablix, registry, rows_at="top")

    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "expression": group_expression,
        "header_textbox": header_name,
        "header_textboxes": [header_name],
        "corner_textboxes": corner["created"],
    }


# ---- remove_column_group --------------------------------------------------


def remove_column_group(
    path: str,
    tablix_name: str,
    group_name: str,
) -> dict[str, Any]:
    """Inverse of :func:`add_column_group`. Unwraps the group's children
    back into its parent ``<TablixMembers>`` (its ``<TablixHeader>`` goes
    with the group) and, when the tablix has row headers, shrinks the
    ``<TablixCorner>`` by the header row the group owned. Body columns are
    untouched; add then remove restores the file byte-for-byte.

    If the group sat beside other members, its unwrapped columns get blank
    headers so every column keeps the same header depth. A group added by
    an older ``add_column_group`` (which inserted a ``<group>_HeaderCol_<i>``
    body column at column 0) has that column removed as well.

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

    header = find_child(member, "TablixHeader")
    legacy = header is None and _legacy_header_column(tablix, group_name)
    removed_textboxes: list[str] = []
    if header is not None:
        removed_textboxes = [
            el.get("Name") for el in header.iter() if el is not header and el.get("Name")
        ]

    # Replace the wrapper at its original position with its wrapped
    # originals, re-indented for their old depth.
    inner_children = list(inner_members)
    for child in inner_children:
        member.addprevious(child)
    tablix_spans.detach(member)
    for child in inner_children:
        if isinstance(child.tag, str):
            tablix_spans.reindent(child)

    registry = names.NameRegistry(doc)
    padded = tablix_headers.pad_header_levels(tablix, tablix_headers.COLUMN, registry)

    if legacy:
        body = find_child(tablix, "TablixBody")
        cols_root = find_child(body, "TablixColumns")
        tablix_spans.detach(find_children(cols_root, "TablixColumn")[0])
        for row in find_children(find_child(body, "TablixRows"), "TablixRow"):
            cell = find_children(find_child(row, "TablixCells"), "TablixCell")[0]
            removed_textboxes.extend(tablix_spans.report_item_names(cell))
            tablix_spans.detach(cell)

    corner: dict[str, Any] = {"created": [], "removed": []}
    if tablix_headers.header_depth(tablix, tablix_headers.ROW) > 0:
        corner = tablix_headers.ensure_corner(tablix, registry, rows_at="top")
    removed_textboxes.extend(corner["removed"])

    doc.save()
    result: dict[str, Any] = {
        "tablix": tablix_name,
        "removed": group_name,
        "removed_textboxes": removed_textboxes,
    }
    if padded:
        result["padded_header_textboxes"] = padded
    if legacy:
        result["legacy_header_column_removed"] = True
    return result


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
    toggle_textbox: Any = _UNSET,
) -> dict[str, Any]:
    """Set ``<Visibility>`` on a column-axis group's TablixMember.

    Refuses up front if ``group_name`` is not in the column hierarchy —
    use :func:`set_group_visibility` for the row-axis case. Same
    validation and ``toggle_textbox`` contract as set_group_visibility
    (the toggle may not be in the columns the group hides).
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    member = _find_column_member_for_group(tablix, group_name)
    if member is None:
        raise ElementNotFoundError(
            f"column-axis group {group_name!r} not found in tablix "
            f"{tablix_name!r}; for a row-axis group use set_group_visibility."
        )
    out = _apply_visibility_to_member(
        doc, tablix, member, visibility_expression, toggle_textbox, axis="column"
    )
    if out["changed"]:
        doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "visibility_expression": out["hidden"],
        "toggle_textbox": out["toggle"],
        "changed": out["changed"],
    }


# ---- add_tablix_column / remove_tablix_column (v0.2 commit 2) -------------


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
    value.text = encode_text(cell_text)
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
    row (the row whose ``<TablixMember>`` carries the detail group — a
    ``<Group>`` without ``<GroupExpressions>``, whatever its name — walked
    depth-first through any wrapping groups; on a matrix, the innermost
    dynamic row group's leaf row). It must be unique
    report-wide. ``expression`` is the value placed inside that
    textbox's ``<TextRun>`` (typically ``=Fields!X.Value``).

    For a tablix with ≥ 2 rows: row 0 (header) gets ``header_text``
    (default = ``column_name``) as a literal; the data row gets
    ``expression``; every other row (group headers between row 0 and
    the data row, subtotal/footer rows after it) gets a blank cell.
    Tablixes with neither a detail group nor a dynamic row group fall
    back to "last row = data row".
    Cell textbox names follow ``<column_name>`` for the data-row cell
    and ``<column_name>_<row_index>`` for non-data cells (suffixed when
    taken report-wide; the payload's ``textboxes`` lists the names
    emitted, top to bottom).

    ``position`` is a 0-indexed insertion position; default = append.
    ``width`` defaults to ``"1in"``. Both the body's ``<TablixColumn>``
    and the column hierarchy's leaf ``<TablixMember>`` are inserted at
    the same position; existing column-group wrappers are not modified.
    """
    names.validate_identifier(column_name, "textbox")
    width = units.dimension("1in" if width is None else width, "width")

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    # Report-wide name uniqueness (report items + groups) — Report Builder
    # enforces it and a collision is a silent corruption.
    registry = names.NameRegistry(doc)
    registry.ensure_unique(column_name)
    registry.claim(column_name)

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

    # Find the data row by walking the row hierarchy for the detail leaf
    # (structurally: a Group without GroupExpressions), else the innermost
    # dynamic group's leaf. This is correct after add_row_group nests the
    # original hierarchy and after add_subtotal_row appends a footer row —
    # the literal "last row" is the subtotal in that case, not the data.
    # Fall back to last-row behavior for all-static tablixes.
    data_idx = data_row_index(tablix)
    data_row_idx = data_idx if data_idx is not None and data_idx < n_rows else n_rows - 1

    emitted: list[str] = []
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
            tb_name = registry.unique(f"{column_name}_{i}")
        else:
            # Middle rows (between header and the data row) and footer
            # rows (subtotals after it): blank cells.
            cell_text = ""
            tb_name = registry.unique(f"{column_name}_{i}")
        emitted.append(tb_name)

        new_cell = _build_tablix_column_cell(tb_name, cell_text)
        if insert_at == len(find_children(cells_root, "TablixCell")):
            cells_root.append(new_cell)
        else:
            cells_root.insert(insert_at, new_cell)

    # A column beside a column group (whose caption is a TablixHeader)
    # needs a header of its own so every column keeps the same header
    # depth (#29).
    column_headers = tablix_headers.pad_header_levels(
        tablix, tablix_headers.COLUMN, registry, f"{column_name}_Header"
    )

    doc.save()
    result: dict[str, Any] = {
        "tablix": tablix_name,
        "column_name": column_name,
        "position": insert_at,
        "width": width,
        "textboxes": emitted,
    }
    if column_headers:
        result["column_header_textboxes"] = column_headers
    return result


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


# ---- set_column_width ----------------------------------------------------


def _resolve_tablix_column_index(
    tablix: etree._Element,
    column: int | str,
) -> int:
    """Resolve a column handle (0-based int OR textbox-name in a cell) to
    its 0-based ``<TablixColumns>`` index.

    Integer handles validate against the column count. String handles
    look for any cell whose Textbox Name matches and return that cell's
    column position. Raises ``ElementNotFoundError`` on a missing name
    and ``IndexError`` on an out-of-range integer.
    """
    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    cols = find_children(cols_root, "TablixColumn") if cols_root is not None else []

    # tools/list publishes `column` as a string; a digit string is an
    # index (textbox names can't be all digits).
    if isinstance(column, str) and column.isdigit():
        column = int(column)
    if isinstance(column, int):
        if column < 0 or column >= len(cols):
            raise IndexError(
                f"tablix {tablix.get('Name')!r} has {len(cols)} columns; "
                f"index {column} is out of range"
            )
        return column

    if not isinstance(column, str):
        raise TypeError(f"column must be int or str; got {type(column).__name__}")

    # String → search every row's cells for a Textbox whose Name matches.
    rows_root = find_child(body, "TablixRows") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    for row in rows:
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            continue
        for col_idx, cell in enumerate(find_children(cells_root, "TablixCell")):
            tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
            if tb is not None and tb.get("Name") == column:
                return col_idx
    raise ElementNotFoundError(
        f"no cell with textbox named {column!r} in tablix "
        f"{tablix.get('Name')!r}; cannot resolve column index"
    )


def set_column_width(
    path: str,
    tablix_name: str,
    column: int | str,
    width: str,
) -> dict[str, Any]:
    """Set ``<Width>`` on a tablix's body column.

    ``column`` accepts either a 0-based integer index OR a textbox name
    living in any cell of that column (mirrors how
    ``set_cell_span`` / ``add_subtotal_row`` address columns).

    ``width`` is an RDL size string (``'1.5in'``, ``'4cm'``, ``'80pt'``).
    Malformed (e.g. unitless, empty) or non-positive values are rejected.

    Idempotent: same width → ``{changed: false}``, no save.
    Returns ``{tablix, column_index, width, kind: 'TablixColumn',
    changed: bool}``.
    """
    width = units.validate_size(width, field="width", allow_zero=False)

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    column_index = _resolve_tablix_column_index(tablix, column)

    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    cols = find_children(cols_root, "TablixColumn") if cols_root is not None else []
    target = cols[column_index]

    width_node = find_child(target, "Width")
    if width_node is None:
        # Defensive: TablixColumn always carries Width per RDL XSD;
        # synthesize one if absent.
        width_node = etree.SubElement(target, q("Width"))
    if width_node.text == width:
        return {
            "tablix": tablix_name,
            "column_index": column_index,
            "width": width,
            "kind": "TablixColumn",
            "changed": False,
        }
    width_node.text = width
    doc.save()
    return {
        "tablix": tablix_name,
        "column_index": column_index,
        "width": width,
        "kind": "TablixColumn",
        "changed": True,
    }


__all__ = [
    "add_column_group",
    "add_tablix_column",
    "remove_column_group",
    "remove_tablix_column",
    "set_column_group_sort",
    "set_column_group_visibility",
    "set_column_width",
]
