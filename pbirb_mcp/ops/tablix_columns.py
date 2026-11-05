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

import copy
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names, tablix_headers, tablix_spans, units
from pbirb_mcp.core.columns import describe_columns, resolve_column
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
from pbirb_mcp.core.mutation import remove_child_if_present, set_text_if_different
from pbirb_mcp.core.tablix_shape import (
    column_leaves,
    data_row_index,
    detach,
    insert_nth_child,
    place_static_leaf,
    placement_warnings,
    removal_member,
)
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd
from pbirb_mcp.ops import visibility as _visibility
from pbirb_mcp.ops.styling import _STYLE_CHILD_ORDER
from pbirb_mcp.ops.tablix import (
    _UNSET,
    _apply_visibility_to_member,
    _set_member_sort,
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
    sort_expressions: list[Any],
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
    return _set_member_sort(doc, member, tablix_name, group_name, sort_expressions)


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


COPY_STYLE_SIDES = ("left", "right")


def _first_descendant(el: etree._Element, *path: str) -> Optional[etree._Element]:
    for local in path:
        if el is None:
            return None
        el = find_child(el, local)
    return el


def _cell_style_source(row: etree._Element, col: int) -> Optional[etree._Element]:
    """The Textbox whose look a new cell at body column ``col`` of ``row``
    copies: the textbox of the cell covering ``col`` (a span's owner for a
    placeholder), or ``None`` (no cell, or not a textbox)."""
    cells_root = find_child(row, "TablixCells")
    cells = find_children(cells_root, "TablixCell") if cells_root is not None else []
    if not 0 <= col < len(cells):
        return None
    owner = cells[tablix_spans.owner_map(cells)[col]]
    return _first_descendant(owner, "CellContents", "Textbox")


def _replace_style(target_parent: etree._Element, source_parent: Optional[etree._Element]) -> None:
    """Make ``target_parent``'s ``<Style>`` a copy of ``source_parent``'s."""
    if source_parent is None:
        return
    source = find_child(source_parent, "Style")
    target = find_child(target_parent, "Style")
    if source is None or target is None:
        return
    clone = copy.deepcopy(source)
    clone.tail = target.tail
    target_parent.replace(target, clone)


def _copy_cell_style(new_tb: etree._Element, source_tb: etree._Element) -> None:
    """Copy the box, first paragraph and first run ``<Style>`` of
    ``source_tb`` onto the freshly built ``new_tb`` (its Name and Value
    stay; nothing else — actions, visibility, sorts — is copied)."""
    _replace_style(new_tb, source_tb)
    new_par = _first_descendant(new_tb, "Paragraphs", "Paragraph")
    src_par = _first_descendant(source_tb, "Paragraphs", "Paragraph")
    _replace_style(new_par, src_par)
    _replace_style(
        _first_descendant(new_par, "TextRuns", "TextRun"),
        _first_descendant(src_par, "TextRuns", "TextRun") if src_par is not None else None,
    )


def _set_run_format(textbox: etree._Element, fmt: str) -> None:
    """Set (``""``: remove) the first run's ``<Format>``."""
    run = _first_descendant(textbox, "Paragraphs", "Paragraph", "TextRuns", "TextRun")
    style = find_child(run, "Style")
    if style is None:
        style = etree.SubElement(run, q("Style"))
    if fmt == "":
        remove_child_if_present(style, "Format")
    else:
        set_text_if_different(style, "Format", encode_text(fmt), order=_STYLE_CHILD_ORDER)


def _style_source_column(tablix: etree._Element, copy_style_from: Optional[str]) -> Optional[Any]:
    """Validate ``copy_style_from``; resolve a textbox name to its body
    column now, before the body changes ("left"/"right" are resolved
    against the insert point later)."""
    if copy_style_from is None or copy_style_from in COPY_STYLE_SIDES:
        return copy_style_from
    if not isinstance(copy_style_from, str) or not copy_style_from:
        raise ValueError(
            "copy_style_from must be 'left', 'right', the name of a textbox in the "
            f"tablix, or null; got {copy_style_from!r}"
        )
    return resolve_column(tablix, copy_style_from)


def _refuse_data_row_span(
    tablix: etree._Element, body: etree._Element, insert_at: int, column_name: str
) -> None:
    """Raise if a column inserted at ``insert_at`` would land inside a
    merged (ColSpan) cell of the tablix's data row."""
    rows_root = find_child(body, "TablixRows")
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    if not rows:
        return
    data_idx = data_row_index(tablix)
    row = rows[data_idx if data_idx is not None and data_idx < len(rows) else -1]
    cells_root = find_child(row, "TablixCells")
    cells = find_children(cells_root, "TablixCell") if cells_root is not None else []
    if tablix_spans.inside_span(cells, insert_at):
        owner = cells[tablix_spans.owner_map(cells)[insert_at]]
        raise ValueError(
            f"position {insert_at} is inside the merged data-row cell "
            f"{', '.join(tablix_spans.report_item_names(owner)) or '(unnamed)'}; "
            f"{column_name!r} would have no cell. Insert outside the span or "
            "shrink it with set_cell_span first."
        )


def add_tablix_column(
    path: str,
    tablix_name: str,
    column_name: str,
    expression: str,
    position: Optional[int] = None,
    width: Optional[str] = None,
    header_text: Optional[str] = None,
    inside_group: Optional[str] = None,
    header_name: Optional[str] = None,
    copy_style_from: Optional[str] = "left",
    format: Optional[str] = None,  # noqa: A002 — the tool's public parameter name
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
    Cell textbox names follow ``<column_name>`` for the data-row cell,
    ``header_name`` (default ``<column_name>_0``) for the header cell and
    ``<column_name>_<row_index>`` for other cells; generated names are
    suffixed when taken report-wide, while ``column_name`` and an explicit
    ``header_name`` must be free. The payload's ``textboxes`` lists the
    names emitted, top to bottom; ``row_textboxes`` has one entry per body
    row (``None`` where a merged cell grew over the column), plus
    ``header_textbox`` / ``data_textbox``.

    ``copy_style_from`` gives each new cell the look of the cell beside it
    in the same row: ``"left"`` (default; the right neighbour at position
    0), ``"right"`` (the left neighbour at the end), the name of a textbox
    in the tablix (that textbox's column), or ``None`` for bare cells.
    The textbox, first-paragraph and first-run ``<Style>`` are copied
    (background, border, font, alignment, number format); Name and Value
    stay the new cell's own and nothing else is copied. ``format`` then
    sets the data cell's number format (``""`` clears an inherited one).

    ``position`` is a 0-indexed body-column position; default = append.
    ``width`` defaults to ``"1in"``. The new static column-hierarchy leaf
    is placed so it owns body column ``position`` (leaf ``i`` ↔ column
    ``i``) and stays outside every column group: at a group's edge it
    becomes the group member's sibling; strictly inside a group it goes
    just before the group instead (``position`` in the payload is then the
    actual column and ``warnings`` says why). ``inside_group`` names a
    column group to insert inside on purpose; ``position`` must then lie
    within that group's columns (or just past them). When sibling members
    carry ``TablixHeader`` cells, the new member gets headers of the same
    depth and size.
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
    if header_name is not None:
        names.validate_identifier(header_name, "textbox")
        registry.ensure_unique(header_name)
        registry.claim(header_name)
    if format is not None and not isinstance(format, str):
        raise ValueError(f"format must be a string, got {format!r}")
    style_from = _style_source_column(tablix, copy_style_from)

    body = find_child(tablix, "TablixBody")
    if body is None:
        raise ValueError(f"tablix {tablix_name!r} has no <TablixBody>")
    cols_root = find_child(body, "TablixColumns")
    if cols_root is None:
        cols_root = etree.SubElement(body, q("TablixColumns"))

    # ---- column hierarchy: a static leaf owning body column `position`
    # (leaf index, not top-level index — #33). Validates position and
    # inside_group before the body is touched.
    point, header_names = place_static_leaf(
        tablix,
        "column",
        position,
        inside_group,
        lambda: registry.unique(f"{column_name}_ColumnHeader"),
    )
    insert_at = point.index

    # Inside a merged cell of the data row the new column would get no
    # column_name textbox (the span grows over it) — refuse up front (#34).
    _refuse_data_row_span(tablix, body, insert_at, column_name)

    # Body column whose cells the new ones copy their look from (indices
    # before the insertion).
    n_cols = len(find_children(cols_root, "TablixColumn"))
    if style_from == "left":
        source_col: Optional[int] = insert_at - 1 if insert_at > 0 else 0
    elif style_from == "right":
        source_col = insert_at if insert_at < n_cols else n_cols - 1
    else:
        source_col = style_from
    if source_col is not None and not 0 <= source_col < n_cols:
        source_col = None

    # ---- body: insert <TablixColumn> at insert_at ------------------------
    new_column = etree.Element(q("TablixColumn"))
    etree.SubElement(new_column, q("Width")).text = width
    insert_nth_child(cols_root, "TablixColumn", insert_at, new_column)

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

    sources = [
        _cell_style_source(row, source_col) if source_col is not None else None for row in rows
    ]

    emitted: list[str] = []
    row_textboxes: list[Optional[str]] = []
    widened: list[int] = []
    header_tb: Optional[str] = None
    data_tb: Optional[str] = None
    for i, row in enumerate(rows):
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            cells_root = etree.SubElement(row, q("TablixCells"))

        # Inside a merged cell (e.g. a band header) the span grows over
        # the new column and a placeholder goes in, not a textbox (#34).
        if tablix_spans.widen_span(cells_root, insert_at):
            widened.append(i)
            row_textboxes.append(None)
            continue

        is_data = n_rows == 1 or i == data_row_idx
        if is_data:
            # The data row (a single-row tablix's only row).
            cell_text = expression
            tb_name = data_tb = column_name
        elif i == 0:
            # First row of multi-row tablix: header row.
            cell_text = header_text_value
            tb_name = header_name or registry.unique(f"{column_name}_{i}")
            header_tb = tb_name
        else:
            # Middle rows (between header and the data row) and footer
            # rows (subtotals after it): blank cells.
            cell_text = ""
            tb_name = registry.unique(f"{column_name}_{i}")
        emitted.append(tb_name)
        row_textboxes.append(tb_name)

        new_cell = _build_tablix_column_cell(tb_name, cell_text)
        new_tb = _first_descendant(new_cell, "CellContents", "Textbox")
        if sources[i] is not None:
            _copy_cell_style(new_tb, sources[i])
        if is_data and format is not None:
            _set_run_format(new_tb, format)
        insert_nth_child(cells_root, "TablixCell", insert_at, new_cell)

    if header_name is not None and header_tb is None:
        raise ValueError(
            f"header_name {header_name!r} has no cell to name: tablix {tablix_name!r} "
            "has no header row above the data row, or a merged cell covers the new "
            "column there"
        )

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
        "row_textboxes": row_textboxes,
        "header_textbox": header_tb,
        "data_textbox": data_tb,
        "style_copied_from_column": (
            None if source_col is None else source_col + (source_col >= insert_at)
        ),
    }
    if header_names:
        result["header_textboxes"] = header_names
    if column_headers:
        result["column_header_textboxes"] = column_headers
    if widened:
        result["widened_span_rows"] = widened
    if point.relocated_from is not None:
        result["requested_position"] = point.requested
    warnings = placement_warnings(point, "column")
    if warnings:
        result["warnings"] = warnings
    return result


def remove_tablix_column(
    path: str,
    tablix_name: str,
    column_name: Optional[str] = None,
    remove_group: bool = False,
    force: bool = False,
    column_ref: Optional[dict] = None,
) -> dict[str, Any]:
    """Remove the column holding a body textbox named ``column_name`` — or,
    with ``column_ref`` (#59), the column with that header text / field
    (``{"header": ...}`` / ``{"field": ...}`` / ``{"index": n}``).

    The column index is found by scanning every row's cells for a textbox
    with that exact name (first hit wins). The column's own hierarchy leaf
    is found via the column hierarchy (leaf *i* owns body column *i*,
    whatever groups wrap it) and removed together with the
    ``<TablixColumn>`` and that cell of every row, so leaves and columns
    stay equal. Inside a column group with other columns, exactly that
    leaf goes.

    Refuses, leaving the file untouched:

    * the tablix's last column (an empty ``<TablixColumns>`` is invalid);
    * a column whose removal would empty a column group (it is the
      group's only column) — the error names the group; pass
      ``remove_group=True`` to remove the group member (with its
      ``TablixHeader``) as well;
    * a tablix whose column hierarchy is already out of step with its
      body (leaf count ≠ column count), where no leaf can be trusted;
    * a removal that would delete a textbox some ``<ToggleItem>`` points
      at (#91) — unless ``force=True``, which also removes those
      dangling ToggleItems (reported as ``removed_toggle_targets``).

    Errors with :class:`ElementNotFoundError` if no row contains a
    textbox with the given name. Returns ``{tablix, removed_column,
    position, removed_textboxes, removed_groups, removed_toggle_targets?}``.
    """
    _column_handle(column_name, column_ref, "column_name")
    doc = RDLDocument.open(path)
    textboxes_before = _visibility.textbox_names(doc.root)
    tablix = resolve_tablix(doc, tablix_name)

    body = find_child(tablix, "TablixBody")
    if body is None:
        raise ElementNotFoundError(
            f"tablix {tablix_name!r} has no <TablixBody>; nothing to remove."
        )
    rows_root = find_child(body, "TablixRows")
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []

    target_index: Optional[int] = None
    if column_ref is not None:
        target_index = resolve_column(tablix, column_ref)
        column_name = _column_label(tablix, target_index)
    for row in rows if target_index is None else ():
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

    cols_root = find_child(body, "TablixColumns")
    cols = find_children(cols_root, "TablixColumn") if cols_root is not None else []
    if len(cols) <= 1:
        raise ValueError(
            f"{column_name!r} is in the only column of tablix {tablix_name!r}; a tablix "
            "needs at least one column — remove the tablix instead."
        )
    leaves = column_leaves(tablix)
    if len(leaves) != len(cols):
        raise ValueError(
            f"tablix {tablix_name!r} has {len(leaves)} column-hierarchy leaves for "
            f"{len(cols)} body columns, so the column's leaf can't be identified; "
            "refusing to remove it."
        )

    # ---- column hierarchy: the column's own leaf (climbing out of any
    # member it is the only child of, so no <TablixMembers/> is left empty).
    member, emptied = removal_member(tablix, "column", target_index)
    group_names = [find_child(g, "Group").get("Name") for g in emptied]
    if emptied and not remove_group:
        raise ValueError(
            f"column {column_name!r} is the only column of column group "
            f"{group_names[-1]!r}; removing it would empty the group. Pass "
            "remove_group=true to remove the group with it, or use "
            "remove_column_group."
        )
    detach(member)

    # ---- body: remove <TablixColumn> at target_index ---------------------
    detach(cols[target_index])

    # ---- rows: remove the cell at target_index from every row ------------
    # Span-aware (#34): a column covered by a merged cell shrinks that
    # span; removing a merged cell's own column keeps its contents (the
    # span shrinks and a placeholder goes instead).
    removed_textboxes: list[str] = []
    for row in rows:
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            continue
        removed_textboxes.extend(tablix_spans.remove_cell(cells_root, target_index))

    # A ToggleItem pointing at a textbox this removal deleted would dangle
    # (#91): refuse before saving, or with force drop those toggles.
    toggle_targets = _visibility.guard_removed_toggle_targets(
        doc.root, textboxes_before, force=force, what=f"removing column {column_name!r}"
    )

    doc.save()
    result: dict[str, Any] = {
        "tablix": tablix_name,
        "removed_column": column_name,
        "position": target_index,
        "removed_textboxes": removed_textboxes,
        "removed_groups": group_names,
    }
    if toggle_targets:
        result["removed_toggle_targets"] = toggle_targets
    return result


def _column_label(tablix: etree._Element, index: int) -> str:
    """How messages / payloads name a column picked by ``column_ref``: its
    data-cell textbox, else its header cell, else ``column <index>``."""
    info = describe_columns(tablix)[index]
    return info.data_cell or info.header_cell or f"column {index}"


# ---- set_column_width ----------------------------------------------------


def _resolve_tablix_column_index(
    tablix: etree._Element,
    column: int | str | dict,
) -> int:
    """Resolve a column handle to its 0-based ``<TablixColumns>`` index:
    a 0-based int (or digit string), a textbox name in any cell of the
    column, or a column spec ``{header|field|index|textbox: ...}`` (#59,
    :func:`pbirb_mcp.core.columns.resolve_column`). Raises
    ``ElementNotFoundError`` on a missing name / unmatched spec,
    ``AmbiguousColumnError`` when a spec matches several columns and
    ``IndexError`` on an out-of-range integer.
    """
    return resolve_column(tablix, column)


def _column_handle(column: Any, column_ref: Optional[dict], what: str = "column") -> Any:
    """The one column handle of ``column`` (legacy) / ``column_ref`` (#59)."""
    if column is not None and column_ref is not None:
        raise ValueError(f"pass {what} or column_ref, not both")
    if column is None and column_ref is None:
        raise ValueError(
            f"{what} or column_ref is required (column_ref picks the column by "
            'header text or field, e.g. {"header": "Amount"})'
        )
    return column if column is not None else column_ref


def set_column_width(
    path: str,
    tablix_name: str,
    column: int | str | dict | None = None,
    width: Optional[str] = None,
    column_ref: Optional[dict] = None,
) -> dict[str, Any]:
    """Set ``<Width>`` on a tablix's body column.

    ``column`` accepts either a 0-based integer index OR a textbox name
    living in any cell of that column (mirrors how
    ``set_cell_span`` / ``add_subtotal_row`` address columns).
    ``column_ref`` (#59) picks it by header text or field instead:
    ``{"header": "Asset Name"}`` / ``{"field": "AssetID"}`` (or
    ``{"index": n}`` / ``{"textbox": name}``); pass one of the two.

    ``width`` is an RDL size string (``'1.5in'``, ``'4cm'``, ``'80pt'``).
    Malformed (e.g. unitless, empty) or non-positive values are rejected.

    Idempotent: same width → ``{changed: false}``, no save.
    Returns ``{tablix, column_index, width, kind: 'TablixColumn',
    changed: bool}``.
    """
    if width is None:
        raise TypeError("set_column_width() missing required argument: 'width'")
    handle = _column_handle(column, column_ref)
    width = units.validate_size(width, field="width", allow_zero=False)

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    column_index = _resolve_tablix_column_index(tablix, handle)

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
