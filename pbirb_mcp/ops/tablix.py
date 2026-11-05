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

from pbirb_mcp.core import names, tablix_headers, tablix_spans, units
from pbirb_mcp.core.document import RDLDocument, indent_inserted
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_dataset, resolve_tablix
from pbirb_mcp.core.mutation import changed_by, set_node_text
from pbirb_mcp.core.tablix_shape import (
    NO_DETAIL_GROUP,
    column_leaves,
    detach,
    detail_member,
    is_detail_group,
    is_dynamic_group,
    leaf_column_index,
    leaf_row_index,
    removal_member,
    row_leaves,
    subtree_leaves,
)
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd
from pbirb_mcp.ops import vbexpr
from pbirb_mcp.ops import visibility as _visibility
from pbirb_mcp.ops.filter_types import (
    _canonical_typename_group,
    type_mismatch_warnings,
    wrap_with_format,
)

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
    field_format: Optional[str] = None,
) -> dict[str, Any]:
    """Append a ``<Filter>`` to a tablix's ``<Filters>`` block.

    ``field_format`` (Phase 8 commit 36): when provided, the filter
    expression is wrapped as ``=Format(<body>, "<field_format>")``.
    Closes the date-vs-string mismatch class — e.g. filter on a
    DateTime field with a ``"MMM, yyyy"``-typed String parameter
    without hand-writing the Format() call.

    Type-aware warnings are emitted in the response when a
    ``Fields!X.Value`` reference in the expression is cross-checked
    against a ``Parameters!Y.Value`` in the values and their declared
    types differ. Best-effort, silent when either side isn't readable.
    """
    if operator not in _VALID_OPERATORS:
        raise ValueError(
            f"unknown filter operator {operator!r}; valid operators are: {sorted(_VALID_OPERATORS)}"
        )
    if not values:
        raise ValueError("at least one filter value is required")

    effective_expression = (
        wrap_with_format(expression, field_format) if field_format else expression
    )

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    # Resolve the bound dataset for the type-mismatch check. Tablix
    # binding lives at <Tablix>/<DataSetName>; missing means we skip
    # the check (no dataset, no field types).
    warnings: list[str] = []
    ds_name_node = find_child(tablix, "DataSetName")
    if ds_name_node is not None and ds_name_node.text:
        try:
            dataset = resolve_dataset(doc, ds_name_node.text)
            warnings = type_mismatch_warnings(doc.root, dataset, effective_expression, values)
        except Exception:  # noqa: BLE001 — ElementNotFoundError is fine to swallow
            warnings = []

    filters_root = _ensure_filters_block(tablix)

    filter_node = etree.SubElement(filters_root, q("Filter"))
    expr_node = etree.SubElement(filter_node, q("FilterExpression"))
    expr_node.text = encode_text(effective_expression)
    op_node = etree.SubElement(filter_node, q("Operator"))
    op_node.text = operator
    values_root = etree.SubElement(filter_node, q("FilterValues"))
    for v in values:
        v_node = etree.SubElement(values_root, q("FilterValue"))
        v_node.text = encode_text(v)

    new_index = len(find_children(filters_root, "Filter")) - 1
    doc.save()
    return {
        "tablix": tablix_name,
        "index": new_index,
        "expression": effective_expression,
        "operator": operator,
        "values": list(values),
        "formatted": field_format is not None,
        "warnings": warnings,
    }


# ---- remove_tablix_filter -------------------------------------------------


def remove_tablix_filter(path: str, tablix_name: str, filter_index: int) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    filters_root = find_child(tablix, "Filters")
    filters = find_children(filters_root, "Filter") if filters_root is not None else []
    if not filters or filter_index < 0 or filter_index >= len(filters):
        raise IndexError(f"tablix {tablix_name!r} has no filter at index {filter_index}")

    target = filters[filter_index]
    filters_root.remove(target)
    if len(find_children(filters_root, "Filter")) == 0:
        filters_root.getparent().remove(filters_root)

    doc.save()
    return {"tablix": tablix_name, "removed_index": filter_index}


# ---- group helpers --------------------------------------------------------


def _row_hierarchy_members(tablix: etree._Element) -> etree._Element:
    """Return the top-level <TablixRowHierarchy>/<TablixMembers> element."""
    hierarchy = find_child(tablix, "TablixRowHierarchy")
    if hierarchy is None:
        # Defensive — every well-formed Tablix has a row hierarchy.
        hierarchy = etree.SubElement(tablix, q("TablixRowHierarchy"))
    members = find_child(hierarchy, "TablixMembers")
    if members is None:
        members = etree.SubElement(hierarchy, q("TablixMembers"))
    return members


def _find_member_for_group(tablix: etree._Element, group_name: str) -> Optional[etree._Element]:
    """Find a TablixMember bearing ``<Group Name=...>`` in the **row** axis only.

    Scoped to ``TablixRowHierarchy`` so that row-axis tools (``set_group_sort``,
    ``set_group_visibility``, ``remove_row_group``, ``set_detail_row_visibility``)
    refuse to act on a column-axis group with a name collision. Mirrors the
    explicit row/column split already in ``tablix_columns._find_column_member_for_group``.
    Use ``resolve_group`` from ``core.ids`` for hierarchy-agnostic lookup when
    that's the right semantic.
    """
    row_hierarchy = find_child(tablix, "TablixRowHierarchy")
    if row_hierarchy is None:
        return None
    for member in row_hierarchy.iter(q("TablixMember")):
        group = find_child(member, "Group")
        if group is not None and group.get("Name") == group_name:
            return member
    return None


def _column_count(tablix: etree._Element) -> int:
    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    if cols_root is None:
        return 0
    return len(find_children(cols_root, "TablixColumn"))


def _build_group_header_row(
    textbox_names: list[str],
    group_expression: str,
) -> etree._Element:
    """Build a TablixRow whose first cell renders the group expression and
    whose remaining cells are blank textboxes — one cell per entry of
    ``textbox_names``. The caller generates those names (normally
    ``<group>_Header_<col_index>``) through :mod:`pbirb_mcp.core.names`
    so they are report-wide unique.
    """
    row = etree.Element(q("TablixRow"))
    height = etree.SubElement(row, q("Height"))
    height.text = "0.25in"
    cells_root = etree.SubElement(row, q("TablixCells"))
    for i, textbox_name in enumerate(textbox_names):
        cell = etree.SubElement(cells_root, q("TablixCell"))
        contents = etree.SubElement(cell, q("CellContents"))
        tb = etree.SubElement(contents, q("Textbox"), Name=textbox_name)
        etree.SubElement(tb, q("CanGrow")).text = "true"
        etree.SubElement(tb, q("KeepTogether")).text = "true"
        paragraphs = etree.SubElement(tb, q("Paragraphs"))
        paragraph = etree.SubElement(paragraphs, q("Paragraph"))
        textruns = etree.SubElement(paragraph, q("TextRuns"))
        textrun = etree.SubElement(textruns, q("TextRun"))
        value = etree.SubElement(textrun, q("Value"))
        # Only the first cell shows the group expression; others stay blank
        # so the group-header row visually reads as a left-anchored caption.
        value.text = encode_text(group_expression) if i == 0 else ""
        etree.SubElement(textrun, q("Style"))
        etree.SubElement(paragraph, q("Style"))
        default_name = etree.SubElement(tb, qrd("DefaultName"))
        default_name.text = textbox_name
        etree.SubElement(tb, q("Style"))
    return row


# Per RDL XSD, child order inside <TablixMember> is roughly:
#   KeepWithGroup, RepeatOnNewPage, FixedData, Group, SortExpressions,
#   TablixHeader, Visibility, HideIfNoRows, KeepTogether, DataElementName,
#   DataElementOutput, TablixMembers, ID, ...
# We only ever emit a small subset; this list captures the local-name order
# we care about so we can place an inserted child at the right position.
_TABLIX_MEMBER_CHILD_ORDER = (
    "KeepWithGroup",
    "RepeatOnNewPage",
    "FixedData",
    "Group",
    "SortExpressions",
    "TablixHeader",
    "Visibility",
    "HideIfNoRows",
    "KeepTogether",
    "DataElementName",
    "DataElementOutput",
    "TablixMembers",
)


def _insert_member_child(member: etree._Element, new_child: etree._Element) -> None:
    """Insert ``new_child`` into ``member`` respecting the schema-required
    sibling order. Replaces any existing element of the same local name so
    callers can use this for both create and replace flows.
    """
    new_local = etree.QName(new_child).localname
    existing = find_child(member, new_local)
    if existing is not None:
        member.replace(existing, new_child)
        return

    new_idx = _TABLIX_MEMBER_CHILD_ORDER.index(new_local)
    # Find the first existing child whose order index is greater — insert
    # immediately before it. If none, append.
    for i, child in enumerate(list(member)):
        local = etree.QName(child).localname
        if (
            local in _TABLIX_MEMBER_CHILD_ORDER
            and _TABLIX_MEMBER_CHILD_ORDER.index(local) > new_idx
        ):
            member.insert(i, new_child)
            return
    member.append(new_child)


# ---- add_row_group --------------------------------------------------------


def add_row_group(
    path: str,
    tablix_name: str,
    group_name: str,
    group_expression: str,
    parent_group: Optional[str] = None,
) -> dict[str, Any]:
    """Wrap the current top-level row hierarchy in a new outer group.

    A new ``<TablixMember>`` is created at the top of the row hierarchy
    holding ``<Group Name=...>`` and a fresh group-header leaf member; all
    previously top-level members move underneath it. A matching group-header
    row is inserted at body row 0 with the group expression in the first
    cell.

    ``parent_group`` is reserved for nesting a new group beneath an existing
    one (e.g. add City under Region) — not yet implemented.
    """
    if parent_group is not None:
        raise NotImplementedError(
            "parent_group nesting is not yet supported; only top-level "
            "outer-group wrapping is implemented in this commit."
        )

    names.validate_identifier(group_name, "group")
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    # Group names share the report-wide namespace with report items.
    registry = names.NameRegistry(doc)
    registry.ensure_unique(group_name)
    registry.claim(group_name)
    header_names = [
        registry.unique(f"{group_name}_Header_{i}") for i in range(_column_count(tablix))
    ]

    members_root = _row_hierarchy_members(tablix)
    existing_children = list(members_root)

    # New outer wrapper member:
    #   <TablixMember>
    #     <Group Name=...><GroupExpressions><GroupExpression>...
    #     <TablixMembers>
    #       <TablixMember><KeepWithGroup>After</KeepWithGroup></TablixMember>  -- group header leaf
    #       ...existing children moved here...
    #     </TablixMembers>
    #   </TablixMember>
    new_outer = etree.Element(q("TablixMember"))
    group = etree.SubElement(new_outer, q("Group"), Name=group_name)
    expr_root = etree.SubElement(group, q("GroupExpressions"))
    expr_node = etree.SubElement(expr_root, q("GroupExpression"))
    expr_node.text = encode_text(group_expression)

    inner_members = etree.SubElement(new_outer, q("TablixMembers"))
    header_leaf = etree.SubElement(inner_members, q("TablixMember"))
    keep = etree.SubElement(header_leaf, q("KeepWithGroup"))
    keep.text = "After"

    for child in existing_children:
        members_root.remove(child)
        inner_members.append(child)

    members_root.append(new_outer)

    # Body: insert a new row at position 0 for the group header.
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows")
    if rows_root is None:
        rows_root = etree.SubElement(body, q("TablixRows"))
    new_row = _build_group_header_row(header_names, group_expression)
    rows_root.insert(0, new_row)

    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "expression": group_expression,
        "header_textboxes": header_names,
    }


# ---- remove_row_group ------------------------------------------------------


def _is_static_leaf(member: etree._Element) -> bool:
    return find_child(member, "Group") is None and find_child(member, "TablixMembers") is None


def _row_textbox_names(row: Optional[etree._Element]) -> list[str]:
    if row is None:
        return []
    return [tb.get("Name") for tb in row.iter(q("Textbox")) if tb.get("Name")]


def _is_subtotal_leaf(
    group_name: str,
    row: Optional[etree._Element],
    legacy_names: bool = True,
) -> bool:
    """True iff the body ``row`` of a static leaf under ``group_name`` is
    one of the subtotal rows ``add_subtotal_row`` emits: every textbox is
    named ``<group>_SubtotalHeader_…`` / ``<group>_SubtotalFooter_…``
    (current) or ``<group>_Header_…`` / ``<group>_Footer_…`` (before #32;
    ``legacy_names``).

    Deliberately name-based: Report Builder marks both a group's total and
    a table footer with ``KeepWithGroup=Before``, and add_row_group wraps
    table footers into the group, so that marker can't tell "this group's
    subtotal" from "a row the group merely wraps".
    """
    textboxes = _row_textbox_names(row)
    stems = ("SubtotalHeader_", "SubtotalFooter_")
    if legacy_names:
        stems += ("Header_", "Footer_")
    prefixes = tuple(f"{group_name}_{stem}" for stem in stems)
    return bool(textboxes) and all(n.startswith(prefixes) for n in textboxes)


def _reindent(doc: RDLDocument, el: etree._Element) -> None:
    """Re-indent a moved (already known) subtree to its new depth; the
    save-time pass only indents newly created elements."""
    root_text = doc.root.text or ""
    if "\n" not in root_text:
        return
    unit = root_text.rsplit("\n", 1)[1]
    indent_inserted(el, unit if unit and not unit.strip() else "  ")


def remove_row_group(
    path: str,
    tablix_name: str,
    group_name: str,
    keep_subtotals: bool = False,
) -> dict[str, Any]:
    """Remove a row group: unwrap the members it wraps into its parent
    and delete the group's own rows.

    The group's rows are located through the hierarchy (leaf *i* owns body
    row *i*), never assumed to be body row 0:

    * **group header** — the group's first child when it is a static leaf,
      other members follow it, and it either precedes nested/detail members
      or is marked as a header (``KeepWithGroup=After`` or a
      ``<group>_Header_…`` row) — what :func:`add_row_group` creates, and
      Report Builder's group header row. Its body row is deleted.
    * **subtotals** — static leaf children emitted by ``add_subtotal_row``
      (textboxes named ``<group>_Subtotal…``, or ``<group>_Header_…`` /
      ``<group>_Footer_…`` from older versions). Deleted with their rows
      unless ``keep_subtotals`` — kept, their group-scoped aggregates
      silently become totals over the parent scope. Other static rows
      marked ``KeepWithGroup=Before`` (Report Builder totals *or* table
      footers) are kept, with a ``warnings`` entry.
    * everything else is **wrapped** and moves, in order, to where the
      group member was; its body rows are untouched. A Report
      Builder-shaped group without a header (a ``<Group>`` wrapping just
      the data leaf) is therefore unwrapped without deleting any row.

    Refuses, leaving the file untouched: the detail group (a ``<Group>``
    without ``<GroupExpressions>``); a group whose member is itself a leaf
    (it owns a body row, e.g. after convert_to_matrix); a group carrying a
    row-header ``TablixHeader`` (removing it changes the matrix's header
    depth); and a group holding only its own header/subtotal rows.

    Returns ``{tablix, removed, removed_rows, kept_subtotal_rows}`` — body
    row indices as they were before the call.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    member = _find_member_for_group(tablix, group_name)
    if member is None:
        raise ElementNotFoundError(
            f"row-axis group {group_name!r} not found in tablix {tablix_name!r}; "
            "use remove_column_group for a column-axis group."
        )
    if is_detail_group(find_child(member, "Group")):
        raise ValueError(
            f"{group_name!r} is the detail group (a Group without "
            "GroupExpressions) and cannot be removed; drop the entire "
            "tablix instead if you really mean it."
        )

    inner_members = find_child(member, "TablixMembers")
    inner_children = (
        find_children(inner_members, "TablixMember") if inner_members is not None else []
    )
    if not inner_children:
        raise ValueError(
            f"group {group_name!r} has no nested members: its own member is the "
            "leaf that owns a body row, so removing it would delete or re-bind "
            "that row; refusing."
        )
    if find_child(member, "TablixHeader") is not None:
        raise ValueError(
            f"group {group_name!r} carries a row-header cell (TablixHeader); "
            "removing it would change the matrix's header depth, which this "
            "tool does not do."
        )

    # Leaf -> body row map, taken before any mutation.
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    row_of = {id(leaf): i for i, leaf in enumerate(row_leaves(tablix))}

    def body_row(leaf: etree._Element) -> Optional[etree._Element]:
        idx = row_of.get(id(leaf))
        return rows[idx] if idx is not None and idx < len(rows) else None

    # Classify the group's children: its own header row, its subtotal
    # rows, and the members it wraps (everything else).
    def is_subtotal(child: etree._Element, legacy_names: bool) -> bool:
        return _is_static_leaf(child) and _is_subtotal_leaf(
            group_name, body_row(child), legacy_names=legacy_names
        )

    first = inner_children[0]
    # The legacy "<group>_Header_*" stem is also add_row_group's header-row
    # name, so on the first child only the current subtotal stems count.
    subtotals = [first] if is_subtotal(first, legacy_names=False) else []
    subtotals += [c for c in inner_children[1:] if is_subtotal(c, legacy_names=True)]
    header: Optional[etree._Element] = None
    if _is_static_leaf(first) and all(first is not s for s in subtotals):
        followers = [c for c in inner_children[1:] if all(c is not s for s in subtotals)]
        keep = find_child(first, "KeepWithGroup")
        marked = (keep is not None and (keep.text or "").strip() == "After") or any(
            n.startswith(f"{group_name}_Header_") for n in _row_textbox_names(body_row(first))
        )
        # A leading static row is the group header when wrapped members
        # follow it and it either precedes nested/detail members or is
        # marked as a header; otherwise (e.g. a lone static child, or
        # static data + total) it is the group's data row.
        if followers and (marked or any(not _is_static_leaf(c) for c in followers)):
            header = first

    doomed = ([header] if header is not None else []) + ([] if keep_subtotals else subtotals)
    wrapped = [c for c in inner_children if all(c is not d for d in doomed)]
    if not wrapped:
        raise ValueError(
            f"group {group_name!r} holds only its own header/subtotal rows; "
            "removing it would leave nothing to unwrap."
        )

    def indices(leaves: list[etree._Element]) -> list[int]:
        return sorted(
            row_of[id(leaf)] for leaf in leaves if row_of.get(id(leaf), len(rows)) < len(rows)
        )

    removed_rows = indices(doomed)
    kept_rows = indices(subtotals) if keep_subtotals else []
    warnings = [
        f"body row {row_of[id(c)]} (a static row marked KeepWithGroup=Before) was kept "
        "and moved out of the group; if it was the group's total, its aggregates now "
        "cover the parent scope."
        for c in wrapped
        if _is_static_leaf(c)
        and id(c) in row_of
        and (getattr(find_child(c, "KeepWithGroup"), "text", None) or "").strip() == "Before"
    ]

    # ---- hierarchy: the wrapped members take the group member's place ----
    anchor = member
    for child in wrapped:
        anchor.addnext(child)
        anchor = child
    detach(member)
    for child in wrapped:
        _reindent(doc, child)

    # ---- body: delete exactly the group's own rows -----------------------
    for idx in reversed(removed_rows):
        detach(rows[idx])

    doc.save()
    return {
        "tablix": tablix_name,
        "removed": group_name,
        "removed_rows": removed_rows,
        "kept_subtotal_rows": kept_rows,
        **({"warnings": warnings} if warnings else {}),
    }


# ---- set_group_sort -------------------------------------------------------


def _apply_sort_to_member(member: etree._Element, sort_expressions: list[str]) -> None:
    """Hierarchy-agnostic core of set_group_sort / set_column_group_sort —
    operates on an already-resolved TablixMember."""
    new_block = etree.Element(q("SortExpressions"))
    for expr in sort_expressions:
        sort = etree.SubElement(new_block, q("SortExpression"))
        value = etree.SubElement(sort, q("Value"))
        value.text = encode_text(expr)
    _insert_member_child(member, new_block)


def set_group_sort(
    path: str,
    tablix_name: str,
    group_name: str,
    sort_expressions: list[str],
) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    member = _find_member_for_group(tablix, group_name)
    if member is None:
        raise ElementNotFoundError(
            f"row-axis group {group_name!r} not found in tablix {tablix_name!r}; "
            "for a column-axis group use set_column_group_sort."
        )

    changed = changed_by(member, _apply_sort_to_member, member, sort_expressions)
    if changed:
        doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "sort_expressions": list(sort_expressions),
        "changed": changed,
    }


# ---- set_group_visibility -------------------------------------------------

# Default for `toggle_textbox`: "not passed" (keep the existing ToggleItem),
# distinct from None / "" (remove it). Same contract as set_element_visibility.
_UNSET: Any = _visibility._UNSET


def _cell_position(tablix: etree._Element, element: etree._Element) -> Optional[tuple]:
    """``(row_index, column_index)`` of the body cell holding ``element`` in
    ``tablix``, or ``None`` when it isn't in one of this tablix's body cells
    (a TablixHeader / corner textbox, or another tablix's cell)."""
    cell = next((a for a in element.iterancestors(q("TablixCell"))), None)
    if cell is None:
        return None
    row = cell.getparent().getparent()  # TablixCells → TablixRow
    rows_root = row.getparent()
    body = rows_root.getparent() if rows_root is not None else None
    if body is None or body.getparent() is not tablix:
        return None
    rows = find_children(rows_root, "TablixRow")
    cells = find_children(cell.getparent(), "TablixCell")
    return rows.index(row), cells.index(cell)


def _member_toggle_problem(
    root: etree._Element,
    tablix: etree._Element,
    member: etree._Element,
    toggle_name: str,
    axis: str,
) -> Optional[str]:
    """Why ``toggle_name`` can't toggle the group/detail ``member`` — or
    ``None``. Report-wide checks (exists, is a Textbox, same region, tablix
    scope) come from :func:`visibility.toggle_item_problem`; on top of that
    the toggle must not sit inside the rows/columns ``member`` hides, or in
    the member's own header — once collapsed it could never be clicked."""
    problem = _visibility.toggle_item_problem(root, member, toggle_name)
    if problem is not None:
        return problem
    toggle = next(el for el in root.iter(q("Textbox")) if el.get("Name") == toggle_name)
    group = find_child(member, "Group")
    label = f"group {group.get('Name')!r}" if group is not None else "this member"
    if any(a is member for a in toggle.iterancestors()):
        return (
            f"ToggleItem {toggle_name!r} is in the header of {label}, which it would "
            "hide along with itself"
        )
    pos = _cell_position(tablix, toggle)
    if pos is not None:
        hidden_leaves = subtree_leaves(member)
        if axis == "row":
            hidden = {leaf_row_index(tablix, leaf) for leaf in hidden_leaves}
            index, what = pos[0], "row"
        else:
            hidden = {leaf_column_index(tablix, leaf) for leaf in hidden_leaves}
            index, what = pos[1], "column"
        if index in hidden:
            return (
                f"ToggleItem {toggle_name!r} is in body {what} {index}, which {label} "
                "hides; once collapsed it could never be clicked. Use a textbox "
                "outside the hidden rows/columns (e.g. the parent group's header)"
            )
    return None


def _apply_visibility_to_member(
    doc: RDLDocument,
    tablix: etree._Element,
    member: etree._Element,
    visibility_expression: str,
    toggle_textbox: Any,
    *,
    axis: str,
) -> dict[str, Any]:
    """Hierarchy-agnostic core of set_group_visibility /
    set_column_group_visibility / set_detail_row_visibility.

    Validates everything before touching the tree: ``Hidden`` must be
    ``true`` / ``false`` or a well-formed ``=`` expression, and a toggle
    must be a textbox that can actually toggle ``member`` (see
    :func:`_member_toggle_problem`). ``toggle_textbox`` omitted keeps the
    existing ``<ToggleItem>``; ``None`` / ``""`` removes it. Edits the
    existing ``<Visibility>`` in place. Returns ``{hidden, toggle, changed}``.
    """
    hidden_text = _visibility.normalise_hidden(visibility_expression)
    if toggle_textbox is _UNSET:
        toggle_wanted: Any = _UNSET
    elif toggle_textbox is None or toggle_textbox == "":
        toggle_wanted = None
    elif not isinstance(toggle_textbox, str):
        raise ValueError(
            f"toggle_textbox must be a string or null; got {type(toggle_textbox).__name__}"
        )
    else:
        toggle_wanted = toggle_textbox.strip()
        problem = _member_toggle_problem(doc.root, tablix, member, toggle_wanted, axis)
        if problem is not None:
            raise ValueError(problem)

    changed = False
    vis = find_child(member, "Visibility")
    if vis is None:
        vis = etree.Element(q("Visibility"))
        _insert_member_child(member, vis)
        changed = True
    hidden = find_child(vis, "Hidden")
    if hidden is None:
        hidden = etree.Element(q("Hidden"))
        vis.insert(0, hidden)
        changed = True
    if (hidden.text or "").strip() != hidden_text:
        hidden.text = hidden_text
        changed = True
    toggle = find_child(vis, "ToggleItem")
    if toggle_wanted is None:
        if toggle is not None:
            vis.remove(toggle)
            changed = True
    elif toggle_wanted is not _UNSET:
        if toggle is None:
            toggle = etree.Element(q("ToggleItem"))
            hidden.addnext(toggle)
            changed = True
        if toggle.text != toggle_wanted:
            toggle.text = toggle_wanted
            changed = True
    final = find_child(vis, "ToggleItem")
    return {
        "hidden": hidden.text,
        "toggle": final.text if final is not None else None,
        "changed": changed,
    }


def set_group_visibility(
    path: str,
    tablix_name: str,
    group_name: str,
    visibility_expression: str,
    toggle_textbox: Any = _UNSET,
) -> dict[str, Any]:
    """Set ``<Visibility>`` on a row-axis group's TablixMember.

    ``visibility_expression``: ``true`` / ``false`` or an ``=`` expression.
    ``toggle_textbox``: omitted keeps the existing toggle; a textbox name
    sets it (it must exist, be in the same region/tablix scope, and not sit
    in the rows the group hides); ``None`` / ``""`` removes it. Saves only
    when something changed; every error is raised before writing.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    member = _find_member_for_group(tablix, group_name)
    if member is None:
        raise ElementNotFoundError(
            f"row-axis group {group_name!r} not found in tablix {tablix_name!r}; "
            "for a column-axis group use set_column_group_visibility."
        )

    out = _apply_visibility_to_member(
        doc, tablix, member, visibility_expression, toggle_textbox, axis="row"
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


# ---- set_detail_row_visibility --------------------------------------------


def set_detail_row_visibility(
    path: str,
    tablix_name: str,
    expression: str,
    toggle_textbox: Any = _UNSET,
) -> dict[str, Any]:
    """Set ``<Visibility>`` on the detail group's TablixMember.

    Same validation and ``toggle_textbox`` contract as
    :func:`set_group_visibility` (the toggle may not be in the detail rows
    themselves — e.g. use the parent group's header cell).

    The detail group is the row-hierarchy leaf whose ``<Group>`` has no
    ``<GroupExpressions>``. Report Builder names it ``Details`` for the
    first table in a report, ``Details1``... for later ones (group names
    are report-wide unique) and authors rename it, so it is found by
    shape, not by name. Hiding it is how callers conditionally suppress
    the per-row body without restructuring the hierarchy.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    member = detail_member(tablix)
    if member is None:
        raise ElementNotFoundError(f"tablix {tablix_name!r} has {NO_DETAIL_GROUP}")

    out = _apply_visibility_to_member(doc, tablix, member, expression, toggle_textbox, axis="row")
    if out["changed"]:
        doc.save()
    return {
        "tablix": tablix_name,
        "expression": out["hidden"],
        "toggle_textbox": out["toggle"],
        "changed": out["changed"],
    }


# ---- set_row_height -------------------------------------------------------


def set_row_height(
    path: str,
    tablix_name: str,
    row_index: int,
    height: str,
) -> dict[str, Any]:
    height = units.validate_size(height, field="height", allow_zero=False)

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    if row_index < 0 or row_index >= len(rows):
        raise IndexError(
            f"tablix {tablix_name!r} has no row at index {row_index} (rows={len(rows)})"
        )

    row = rows[row_index]
    height_node = find_child(row, "Height")
    if height_node is None:
        # Per RDL XSD, <Height> is the first child of <TablixRow>.
        height_node = etree.Element(q("Height"))
        row.insert(0, height_node)
    changed = set_node_text(height_node, height)

    if changed:
        doc.save()
    return {"tablix": tablix_name, "row_index": row_index, "height": height, "changed": changed}


# ---- set_tablix_size -----------------------------------------------------


def set_tablix_size(
    path: str,
    name: str,
    height: Optional[str] = None,
    width: Optional[str] = None,
) -> dict[str, Any]:
    """Resize a tablix by setting its outer ``<Height>`` and / or
    ``<Width>`` directly.

    Each argument is independently optional; only the supplied fields
    are written. v0.2's positioning tools cover top/left moves but not
    sizing — adding a couple of header rows often required a manual
    ``str_replace`` on the tablix's outer Height (RAG-Report session
    feedback bug #10).

    Both values are RDL size strings (``'4in'``, ``'10cm'``, etc.).
    Malformed (e.g. unitless, empty) or non-positive values are rejected.

    Returns ``{tablix, kind, changed: list[str]}`` — empty list when
    inputs match existing (no save).
    """
    height = units.dimension(height, "height")
    width = units.dimension(width, "width")
    if height is None and width is None:
        return {"tablix": name, "kind": "Tablix", "changed": []}

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, name)
    changed: list[str] = []

    if height is not None:
        h_node = find_child(tablix, "Height")
        if h_node is None:
            # Per RDL XSD, Height sits between Top/Left and Width/Style.
            h_node = etree.Element(q("Height"))
            anchor = find_child(tablix, "Width") or find_child(tablix, "Style")
            if anchor is not None:
                anchor.addprevious(h_node)
            else:
                tablix.append(h_node)
            h_node.text = height
            changed.append("Height")
        elif h_node.text != height:
            h_node.text = height
            changed.append("Height")

    if width is not None:
        w_node = find_child(tablix, "Width")
        if w_node is None:
            w_node = etree.Element(q("Width"))
            anchor = find_child(tablix, "Style")
            if anchor is not None:
                anchor.addprevious(w_node)
            else:
                tablix.append(w_node)
            w_node.text = width
            changed.append("Width")
        elif w_node.text != width:
            w_node.text = width
            changed.append("Width")

    if changed:
        doc.save()
    return {"tablix": name, "kind": "Tablix", "changed": changed}


# ---- convert_to_matrix (#31) ----------------------------------------------

# Aggregates convert_to_matrix can wrap a data cell in: the RDL aggregate
# functions taking a single expression argument.
MATRIX_AGGREGATES = (
    "Sum",
    "Avg",
    "Min",
    "Max",
    "Count",
    "CountDistinct",
    "First",
    "Last",
    "StDev",
    "StDevP",
    "Var",
    "VarP",
)
# Aggregates that only make sense over numbers.
_NUMERIC_AGGREGATES = frozenset({"Sum", "Avg", "StDev", "StDevP", "Var", "VarP"})
# A call to any of these means the expression already aggregates.
_AGGREGATE_CALLS = frozenset(
    a.lower()
    for a in MATRIX_AGGREGATES + ("Aggregate", "CountRows", "RunningValue", "RowNumber", "Previous")
)
_ARITHMETIC_OPERATORS = frozenset({"+", "-", "*", "/", "\\", "^"})


def _has_column_group(tablix: etree._Element) -> bool:
    """True iff the column hierarchy contains any dynamic ``<Group>``
    (one with ``<GroupExpressions>``; detail-shaped groups excluded,
    whatever their name). Matches the matrix-shape precondition: a
    column hierarchy with at least one dynamic group is what makes a
    matrix matrix-shaped."""
    column_hierarchy = find_child(tablix, "TablixColumnHierarchy")
    if column_hierarchy is None:
        return False
    return any(is_dynamic_group(group) for group in column_hierarchy.iter(q("Group")))


def _cell_textbox(cell: etree._Element) -> Optional[etree._Element]:
    """The textbox a cell holds when it holds exactly one report item and
    that item is a textbox."""
    contents = find_child(cell, "CellContents")
    if contents is None:
        return None
    items = [
        c
        for c in contents
        if isinstance(c.tag, str) and etree.QName(c).localname not in ("ColSpan", "RowSpan")
    ]
    if len(items) == 1 and items[0].tag == q("Textbox"):
        return items[0]
    return None


def _textbox_value(textbox: Optional[etree._Element]) -> Optional[str]:
    """The text of a textbox's only ``<Value>`` (``None`` for several)."""
    if textbox is None:
        return None
    values = list(textbox.iter(q("Value")))
    return (values[0].text or "") if len(values) == 1 else None


def _expression_key(text: Optional[str]) -> Optional[str]:
    """``text`` as significant tokens (whitespace and comments dropped) so a
    cell can be compared with a group expression; ``None`` for a literal."""
    tokens = vbexpr.tokenize_expression(text)
    if tokens is None:
        return None
    return "".join(t.text for t in vbexpr.significant(tokens)) or None


def _group_key(member: etree._Element) -> Optional[str]:
    """:func:`_expression_key` of a group member's single GroupExpression."""
    group = find_child(member, "Group")
    root = find_child(group, "GroupExpressions") if group is not None else None
    exprs = find_children(root, "GroupExpression") if root is not None else []
    return _expression_key(exprs[0].text) if len(exprs) == 1 else None


def _row_cells(row: etree._Element) -> list[etree._Element]:
    cells_root = find_child(row, "TablixCells")
    return find_children(cells_root, "TablixCell") if cells_root is not None else []


def _keep_with_group(member: etree._Element) -> str:
    keep = find_child(member, "KeepWithGroup")
    return (keep.text or "").strip() if keep is not None else ""


def _group_header_row(
    tablix: etree._Element,
    member: etree._Element,
    rows: list[etree._Element],
) -> Optional[tuple[etree._Element, etree._Element, etree._Element]]:
    """The caption row :func:`add_row_group` gives a group: the group's
    first child, a static leaf followed by other members, whose row shows
    the group expression in one single-column cell and nothing else.
    Returns ``(leaf, row, caption textbox)`` or ``None``."""
    key = _group_key(member)
    members_root = find_child(member, "TablixMembers")
    children = find_children(members_root, "TablixMember") if members_root is not None else []
    if key is None or len(children) < 2:
        return None
    first = children[0]
    if not _is_static_leaf(first) or find_child(first, "TablixHeader") is not None:
        return None
    index = leaf_row_index(tablix, first)
    if index is None or index >= len(rows):
        return None
    caption: Optional[etree._Element] = None
    for cell in _row_cells(rows[index]):
        if tablix_spans.is_blank(cell):
            continue
        textbox = _cell_textbox(cell)
        if (
            caption is not None
            or textbox is None
            or tablix_spans.span(cell) != 1
            or _expression_key(_textbox_value(textbox)) != key
        ):
            return None
        caption = textbox
    if caption is None:
        return None
    return first, rows[index], caption


def _is_title_row(row: etree._Element) -> bool:
    """A column-title row: every cell blank or a textbox of literal text
    (no expression), at least one of them non-blank."""
    seen_text = False
    for cell in _row_cells(row):
        if tablix_spans.is_blank(cell):
            continue
        textbox = _cell_textbox(cell)
        if textbox is None:
            return False
        if any(vbexpr.expression_body(v.text) is not None for v in textbox.iter(q("Value"))):
            return False
        seen_text = True
    return seen_text


def _title_rows(
    tablix: etree._Element,
    path: list[etree._Element],
    rows: list[etree._Element],
    skip: list[etree._Element],
) -> list[tuple[etree._Element, etree._Element]]:
    """Static ``KeepWithGroup=After`` leaves that precede the data leaf or
    one of its enclosing groups (``path``) and hold only literal captions
    — the table's column-title row(s). Returns ``(leaf, row)`` pairs."""
    out: list[tuple[etree._Element, etree._Element]] = []
    for member in path:
        sibling = member.getprevious()
        while sibling is not None:
            if (
                sibling.tag == q("TablixMember")
                and all(sibling is not s for s in skip)
                and _is_static_leaf(sibling)
                and find_child(sibling, "TablixHeader") is None
                and _keep_with_group(sibling) == "After"
            ):
                index = leaf_row_index(tablix, sibling)
                if index is not None and index < len(rows) and _is_title_row(rows[index]):
                    out.append((sibling, rows[index]))
            sibling = sibling.getprevious()
    return out


def _disposable_cell(cells: list[etree._Element], index: int) -> bool:
    """True iff removing column ``index`` from this row loses nothing the
    author wrote: a blank cell, a span placeholder, or a spanning owner
    (which keeps its contents and shrinks)."""
    if index >= len(cells):
        return True
    cell = cells[index]
    if tablix_spans.is_placeholder(cell) or tablix_spans.span(cell) > 1:
        return True
    return tablix_spans.is_blank(cell)


def _field_types(doc: RDLDocument, tablix: etree._Element) -> dict[str, str]:
    """Field name → shape group (``string``, ``number``, ...) of the
    tablix's dataset, for fields whose rd:TypeName is known."""
    ds_name = find_child(tablix, "DataSetName")
    if ds_name is None or not (ds_name.text or "").strip():
        return {}
    try:
        dataset = resolve_dataset(doc, ds_name.text.strip())
    except ElementNotFoundError:
        return {}
    fields_root = find_child(dataset, "Fields")
    out: dict[str, str] = {}
    for field in find_children(fields_root, "Field") if fields_root is not None else []:
        type_name = field.find(qrd("TypeName"))
        group = _canonical_typename_group(type_name.text if type_name is not None else "")
        if group is not None and field.get("Name"):
            out[field.get("Name")] = group
    return out


def _aggregate_value(text: Optional[str], aggregate: str) -> tuple[Optional[str], Optional[str]]:
    """Rewrite a data-cell value for group scope.

    Returns ``(new_text, problem)``: ``new_text`` is ``=<aggregate>(...)``
    when ``text`` is a field reference or arithmetic over field
    references, numbers and parentheses; ``None`` when it is left alone
    — a literal, no ``Fields!`` reference, already aggregated (any
    aggregate call), or (with ``problem`` set) anything more complex.
    """
    tokens = vbexpr.tokenize_expression(text)
    if tokens is None:
        return None, None
    refs = vbexpr.bang_references(tokens, "Fields")
    if not refs:
        return None, None
    sig = vbexpr.significant(tokens)
    for i, tok in enumerate(sig[:-1]):
        if (
            tok.kind == vbexpr.IDENTIFIER
            and tok.text.lower() in _AGGREGATE_CALLS
            and sig[i + 1].text == "("
        ):
            return None, None
    covered: set[int] = set()
    for ref in refs:
        if (ref.member or "").lower() != "value":
            return None, "it references a field member other than .Value"
        covered.update(range(ref.first, ref.last + 1))
    if any(t.kind in (vbexpr.COMMENT, vbexpr.ERROR) for t in tokens):
        return None, "it holds a comment or an unterminated string"
    for i, tok in enumerate(sig):
        if i in covered or tok.kind == vbexpr.NUMBER:
            continue
        if tok.kind == vbexpr.OPERATOR and tok.text in _ARITHMETIC_OPERATORS:
            continue
        if tok.kind == vbexpr.DELIMITER and tok.text in "()":
            continue
        return None, "it is more than arithmetic over field references"
    body = vbexpr.expression_body(text) or ""
    return f"={aggregate}({body.strip()})", None


def convert_to_matrix(
    path: str,
    tablix_name: str,
    row_group: str,
    column_group: str,
    aggregate: str = "Sum",
) -> dict[str, Any]:
    """Turn a grouped table into a Report Builder-shaped matrix: group
    captions in ``<TablixHeader>`` cells, aggregated data cells, and a
    ``<TablixCorner>`` sized column-header depth x row-header depth.

    Pre-conditions (refuses with :class:`ValueError`, file untouched):

    * ``row_group`` names a dynamic row group (not the detail group) that
      encloses the detail rows; ``column_group`` names a dynamic column
      group.
    * The row hierarchy still has a detail group (a leaf ``<Group>``
      without ``<GroupExpressions>``, any name) — without one the tablix
      already is a matrix (idempotent-by-refusal).
    * Hierarchy leaves match body rows / columns, and at least one body
      column holds something other than a group expression (a value to
      aggregate).

    What it does:

    1. **Row headers** — every dynamic row group enclosing the detail rows
       gets a ``TablixHeader``. Its content is the caption row
       :func:`add_row_group` emitted (that textbox moves, keeping its name
       and style; the row is deleted), else the body column showing the
       group expression (below), else a new ``<group>_Header`` textbox.
    2. **Caption columns** — a body column whose data cell is exactly a
       row- or column-group expression only repeats a header; it is
       removed (its width becomes the row header's ``Size``) unless
       another kept row has content in it or it is a column group's only
       column (then it stays, unaggregated, with a warning).
    3. **Title row** — static ``KeepWithGroup=After`` rows of literal
       captions above the data (the table's column titles) are deleted;
       with two or more value columns left, their captions move into
       ``TablixHeader`` cells of those columns' static members instead (a
       header level under the column group, as Report Builder does).
    4. **Data row** — the detail row is kept and re-bound to the
       innermost enclosing group (the detail member becomes static, or is
       dropped when it was the group's only child, the group becoming the
       leaf). Its ``=Fields!X.Value`` cells (or arithmetic over fields)
       become ``=<aggregate>(…)``; already-aggregated cells are left
       alone, anything else is left with a warning. Other rows (totals,
       footers) are untouched.
    5. Header levels are padded so every hierarchy path has the same
       depth, and the corner is created / resized
       (:func:`pbirb_mcp.core.tablix_headers.ensure_corner`). The result
       is checked with :func:`~pbirb_mcp.core.tablix_headers.shape_problems`;
       any problem refuses the whole call.

    ``aggregate`` is one of :data:`MATRIX_AGGREGATES` (default ``Sum``).

    Returns ``{tablix, kind, changed, row_headers, aggregated,
    removed_rows, removed_columns, removed_textboxes, corner_textboxes,
    warnings}`` — ``removed_rows`` / ``removed_columns`` are body indices
    as they were before the call.
    """
    canonical = {a.lower(): a for a in MATRIX_AGGREGATES}
    if not isinstance(aggregate, str) or aggregate.lower() not in canonical:
        raise ValueError(
            f"aggregate must be one of {', '.join(MATRIX_AGGREGATES)}; got {aggregate!r}"
        )
    aggregate = canonical[aggregate.lower()]

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    # ---- preconditions -----------------------------------------------------
    row_member = _find_member_for_group(tablix, row_group)
    if row_member is None:
        raise ValueError(
            f"row group {row_group!r} not found in tablix {tablix_name!r}; "
            "call add_row_group first or check the name."
        )
    if is_detail_group(find_child(row_member, "Group")):
        raise ValueError(
            f"{row_group!r} is the detail group (a Group without GroupExpressions); "
            "convert_to_matrix aggregates the detail rows into a row group — pass "
            "that group (add one with add_row_group first)."
        )
    column_hierarchy = find_child(tablix, "TablixColumnHierarchy")
    column_member = None
    for member in column_hierarchy.iter(q("TablixMember")) if column_hierarchy is not None else ():
        group = find_child(member, "Group")
        if group is not None and group.get("Name") == column_group:
            column_member = member
            break
    if column_member is None or not is_dynamic_group(find_child(column_member, "Group")):
        raise ValueError(
            f"column group {column_group!r} not found in tablix {tablix_name!r}; "
            "call add_column_group first."
        )
    detail = detail_member(tablix)
    if detail is None:
        raise ValueError(
            f"tablix {tablix_name!r} is already a matrix ({NO_DETAIL_GROUP} "
            "in the row hierarchy); convert_to_matrix is idempotent-by-refusal."
        )
    row_hierarchy = find_child(tablix, "TablixRowHierarchy")
    enclosing = []  # dynamic row groups around the detail leaf, innermost first
    for ancestor in detail.iterancestors():
        if ancestor is row_hierarchy:
            break
        if ancestor.tag == q("TablixMember") and is_dynamic_group(find_child(ancestor, "Group")):
            enclosing.append(ancestor)
    if all(m is not row_member for m in enclosing):
        raise ValueError(
            f"row group {row_group!r} does not enclose the detail rows of tablix "
            f"{tablix_name!r}; pass a group the detail group is nested in."
        )

    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    columns = find_children(cols_root, "TablixColumn") if cols_root is not None else []
    n_row_leaves, n_col_leaves = len(row_leaves(tablix)), len(column_leaves(tablix))
    if n_row_leaves != len(rows) or n_col_leaves != len(columns):
        raise ValueError(
            f"tablix {tablix_name!r} has {n_row_leaves} row leaves for {len(rows)} body "
            f"rows and {n_col_leaves} column leaves for {len(columns)} body columns; "
            "refusing to convert an inconsistent tablix."
        )
    data_index = leaf_row_index(tablix, detail)
    data_row = rows[data_index]
    data_cells = _row_cells(data_row)

    # ---- analysis (no mutation yet) ----------------------------------------
    warnings: list[str] = []
    changed: list[str] = []
    header_rows = {}  # id(group member) -> (leaf, row, caption textbox)
    for member in enclosing:
        if find_child(member, "TablixHeader") is None:
            found = _group_header_row(tablix, member, rows)
            if found is not None:
                header_rows[id(member)] = found
    header_leaves = [leaf for leaf, _row, _tb in header_rows.values()]
    titles = _title_rows(tablix, [detail] + enclosing, rows, header_leaves)
    dropped_rows = [row for _leaf, row, _tb in header_rows.values()] + [r for _l, r in titles]
    kept_rows = [r for r in rows if r is not data_row and all(r is not d for d in dropped_rows)]

    group_keys = {}  # expression key -> group name, row groups first
    for member in enclosing:
        key = _group_key(member)
        if key is not None:
            group_keys.setdefault(key, find_child(member, "Group").get("Name"))
    for group in column_hierarchy.iter(q("Group")):
        member = group.getparent()
        key = _group_key(member) if is_dynamic_group(group) else None
        if key is not None:
            group_keys.setdefault(key, group.get("Name"))
    caption_cols: dict[int, str] = {}  # body column -> expression key
    for j, cell in enumerate(data_cells):
        if tablix_spans.is_placeholder(cell) or tablix_spans.span(cell) != 1:
            continue
        key = _expression_key(_textbox_value(_cell_textbox(cell)))
        if key in group_keys:
            caption_cols[j] = key
    if all(j in caption_cols for j in range(len(columns))):
        raise ValueError(
            f"every body column of tablix {tablix_name!r} shows a group expression, so "
            "there is no value to aggregate; add a value column (e.g. "
            "=Fields!Amount.Value) before converting."
        )

    # ---- mutation ----------------------------------------------------------
    registry = names.NameRegistry(doc)
    removed_textboxes: list[str] = []
    # Row-header contents: the add_row_group caption textbox, detached
    # before its row goes.
    header_items: dict[int, etree._Element] = {}
    for member_id, (_leaf, _row, textbox) in header_rows.items():
        header_items[member_id] = textbox
        textbox.getparent().remove(textbox)
    header_sizes: dict[int, str] = {}

    # Caption columns, right to left so indices stay valid.
    removed_columns: list[int] = []
    for j in sorted(caption_cols, reverse=True):
        key = caption_cols[j]
        label = group_keys[key]
        if not all(_disposable_cell(_row_cells(r), j) for r in kept_rows):
            warnings.append(
                f"body column {j} shows the expression of group {label!r} but another row holds "
                "content in it; kept (unaggregated)."
            )
            continue
        member, emptied = removal_member(tablix, "column", j)
        if emptied:
            warnings.append(
                f"body column {j} shows the expression of group {label!r} but it is the only "
                "column of its column group; kept (unaggregated)."
            )
            continue
        width = find_child(columns[j], "Width")
        for group_member in enclosing:
            if _group_key(group_member) != key or id(group_member) in header_sizes:
                continue
            if width is not None and (width.text or "").strip():
                header_sizes[id(group_member)] = width.text.strip()
            if id(group_member) not in header_items:
                textbox = _cell_textbox(data_cells[j])
                if textbox is not None:
                    textbox.getparent().remove(textbox)
                    header_items[id(group_member)] = textbox
            break
        detach(member)
        detach(columns[j])
        for row in rows:
            cells_root = find_child(row, "TablixCells")
            if cells_root is not None:
                removed_textboxes.extend(tablix_spans.remove_cell(cells_root, j))
        removed_columns.append(j)
    removed_columns.sort()
    if removed_columns:
        changed.append("caption_columns_removed")

    # Title row(s): captions move into column headers when they still
    # label two or more value columns, otherwise they go with the row.
    column_textboxes: list[str] = []
    remaining = column_leaves(tablix)
    if len(titles) == 1 and len(remaining) >= 2:
        _title_leaf, title_row = titles[0]
        cells = _row_cells(title_row)
        movable = len(cells) == len(remaining) and all(
            tablix_spans.span(c) == 1
            and not tablix_spans.is_placeholder(c)
            and _is_static_leaf(leaf)
            and find_child(leaf, "TablixHeader") is None
            for c, leaf in zip(cells, remaining)
        )
        if movable:
            height = find_child(title_row, "Height")
            size = (height.text or "").strip() if height is not None else ""
            size = size or tablix_headers.DEFAULT_HEADER_SIZE[tablix_headers.COLUMN]
            for cell, leaf in zip(cells, remaining):
                textbox = _cell_textbox(cell)
                if textbox is None:
                    textbox = tablix_headers.new_textbox(registry.unique(f"{tablix_name}_Header"))
                column_textboxes.append(textbox.get("Name"))
                tablix_headers.insert_member_header(
                    leaf, tablix_headers.header_around(size, textbox)
                )
            changed.append("column_headers_added")
    row_index = {id(r): i for i, r in enumerate(rows)}
    removed_rows = sorted(row_index[id(r)] for r in dropped_rows)
    for leaf, row in [(leaf, row) for leaf, row, _tb in header_rows.values()] + titles:
        detach(leaf)
        removed_textboxes.extend(el.get("Name") for el in row.iter(q("Textbox")) if el.get("Name"))
        detach(row)
    if removed_rows:
        changed.append("caption_rows_removed")

    # The detail rows become the innermost group's data row.
    parent_members = detail.getparent()
    owner = parent_members.getparent()
    if len(find_children(parent_members, "TablixMember")) == 1 and owner.tag == q("TablixMember"):
        if find_child(detail, "Visibility") is not None:
            warnings.append(
                "the detail group's Visibility went with it; set it on the row group "
                "with set_group_visibility if still wanted."
            )
        detach(parent_members)
    else:
        for local in ("Group", "SortExpressions"):
            child = find_child(detail, local)
            if child is not None:
                tablix_spans.detach(child)
    changed.append("details_member_removed")

    # Aggregate the data cells (group-expression cells stay as they are:
    # they are constant within the group).
    field_types = _field_types(doc, tablix)
    aggregated: list[str] = []
    for cell in _row_cells(data_row):
        contents = find_child(cell, "CellContents")
        if contents is None:
            continue
        if _expression_key(_textbox_value(_cell_textbox(cell))) in group_keys:
            continue
        for textbox in contents.iter(q("Textbox")):
            touched = False
            for value in textbox.iter(q("Value")):
                new_text, problem = _aggregate_value(value.text, aggregate)
                if problem is not None:
                    warnings.append(
                        f"textbox {textbox.get('Name')!r} left as {value.text!r}: {problem}; "
                        "aggregate it by hand."
                    )
                if new_text is None:
                    continue
                if aggregate in _NUMERIC_AGGREGATES:
                    for ref in vbexpr.bang_references(value.text, "Fields"):
                        kind = field_types.get(ref.name)
                        if kind in ("string", "datetime", "boolean"):
                            warnings.append(
                                f"textbox {textbox.get('Name')!r}: {aggregate}() over "
                                f"{kind} field {ref.name!r}; pick another aggregate "
                                "(e.g. First or Count) if that is not intended."
                            )
                value.text = new_text
                touched = True
            if touched:
                aggregated.append(textbox.get("Name"))
    if aggregated:
        changed.append("cells_aggregated")

    # Row headers for every enclosing group.
    row_headers: list[str] = []
    for member in reversed(enclosing):  # outermost first
        if find_child(member, "TablixHeader") is not None:
            continue
        item = header_items.get(id(member))
        if item is None:
            name = registry.unique(f"{find_child(member, 'Group').get('Name')}_Header")
            expr = find_children(
                find_child(find_child(member, "Group"), "GroupExpressions"), "GroupExpression"
            )
            item = tablix_headers.new_textbox(name, expr[0].text or "" if expr else "")
        size = header_sizes.get(id(member), tablix_headers.DEFAULT_HEADER_SIZE[tablix_headers.ROW])
        tablix_headers.insert_member_header(member, tablix_headers.header_around(size, item))
        row_headers.append(item.get("Name"))
    if row_headers:
        changed.append("row_headers_added")

    padded = tablix_headers.pad_header_levels(tablix, tablix_headers.ROW, registry)
    padded += tablix_headers.pad_header_levels(tablix, tablix_headers.COLUMN, registry)
    corner_before = tablix_headers.corner_shape(tablix)
    corner = tablix_headers.ensure_corner(
        tablix, registry, rows_at="bottom" if column_textboxes else "top"
    )
    removed_textboxes.extend(corner["removed"])
    if corner_before is None and tablix_headers.corner_shape(tablix) is not None:
        changed.append("corner_created")
    elif corner["created"] or corner["removed"]:
        changed.append("corner_resized")

    problems = tablix_headers.shape_problems(tablix)
    if problems:
        raise ValueError(
            f"converting tablix {tablix_name!r} would leave an inconsistent matrix "
            f"({'; '.join(problems)}); nothing was changed."
        )

    doc.save()
    return {
        "tablix": tablix_name,
        "kind": "Tablix",
        "changed": changed,
        "row_headers": row_headers,
        "column_headers": column_textboxes,
        "aggregated": aggregated,
        "removed_rows": removed_rows,
        "removed_columns": removed_columns,
        "removed_textboxes": removed_textboxes,
        "blank_headers": padded,
        "corner_textboxes": corner["created"],
        "warnings": warnings,
    }


# ---- set_tablix_corner (#31) ------------------------------------------------


def _set_textbox_value(textbox: etree._Element, value: str) -> bool:
    """Make ``value`` the textbox's only text run, in place: the first
    run's ``<Value>`` is set (its Style and the textbox's Name / Style
    stay); further runs and paragraphs are removed. Returns True iff
    anything changed."""
    paragraphs_root = find_child(textbox, "Paragraphs")
    paragraphs = find_children(paragraphs_root, "Paragraph") if paragraphs_root is not None else []
    runs_root = find_child(paragraphs[0], "TextRuns") if paragraphs else None
    runs = find_children(runs_root, "TextRun") if runs_root is not None else []
    value_node = find_child(runs[0], "Value") if runs else None
    if value_node is None:
        # No run to edit: give the textbox Report Builder's paragraph block.
        fresh = find_child(tablix_headers.new_textbox("_", value), "Paragraphs")
        if paragraphs_root is not None:
            textbox.replace(paragraphs_root, fresh)
        else:
            anchor = find_child(textbox, "KeepTogether")
            if anchor is None:
                anchor = find_child(textbox, "CanGrow")
            if anchor is not None:
                anchor.addnext(fresh)
            else:
                textbox.insert(0, fresh)
        return True
    changed = False
    if (value_node.text or "") != value:
        value_node.text = value
        changed = True
    for extra in runs[1:] + paragraphs[1:]:
        tablix_spans.detach(extra)
        changed = True
    return changed


def set_tablix_corner(
    path: str,
    tablix_name: str,
    text: Optional[str] = None,
    expression: Optional[str] = None,
    row: int = 0,
    col: int = 0,
) -> dict[str, Any]:
    """Set the value of one ``<TablixCorner>`` cell of a matrix.

    Either ``text`` (literal value) or ``expression`` (VB.NET ``=...``
    string) — mutually exclusive. ``row`` / ``col`` (0-based, default the
    top-left cell) address a cell of the corner, which is column-header
    depth x row-header depth.

    * The corner is first created or resized to that shape
      (:func:`pbirb_mcp.core.tablix_headers.ensure_corner`; new cells are
      blank textboxes named ``<tablix_name>_Corner``, suffixed through the
      report-wide name registry).
    * The value is set **in place**: the cell's textbox keeps its name and
      style (extra text runs / paragraphs are dropped). An identical call
      changes nothing and returns ``changed: []``.

    Refuses when either header depth is 0 (no corner exists in that shape
    — run convert_to_matrix first), for a cell outside the corner or
    covered by another cell's ColSpan / RowSpan, and for a cell holding
    something other than a single textbox.

    Returns ``{tablix, name, kind: 'TablixCorner', row, col, changed}``;
    ``changed`` lists ``corner_created`` / ``corner_resized`` and
    ``corner_written`` (the value changed).
    """
    if text is None and expression is None:
        raise ValueError(
            "set_tablix_corner requires either text or expression (both cannot be None)"
        )
    if text is not None and expression is not None:
        raise ValueError(
            "set_tablix_corner: text and expression are mutually exclusive — pass only one"
        )
    for label, index in (("row", row), ("col", col)):
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError(f"{label} must be a non-negative integer; got {index!r}")

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    n_rows = tablix_headers.header_depth(tablix, tablix_headers.COLUMN)
    n_cols = tablix_headers.header_depth(tablix, tablix_headers.ROW)
    if n_rows == 0 or n_cols == 0:
        raise ValueError(
            f"tablix {tablix_name!r} has {n_rows} column-header row(s) x {n_cols} "
            "row-header column(s); a TablixCorner exists only when both are non-zero. "
            "Build the matrix first (add_column_group, then convert_to_matrix gives the "
            "row groups their TablixHeaders)."
        )
    if row >= n_rows or col >= n_cols:
        raise ValueError(
            f"corner cell (row {row}, col {col}) is outside tablix {tablix_name!r}'s "
            f"{n_rows} x {n_cols} corner."
        )

    registry = names.NameRegistry(doc)
    shape_before = tablix_headers.corner_shape(tablix)
    resized = tablix_headers.ensure_corner(tablix, registry)
    changed: list[str] = []
    if shape_before is None:
        changed.append("corner_created")
    elif resized["created"] or resized["removed"]:
        changed.append("corner_resized")

    corner = find_child(tablix, "TablixCorner")
    corner_rows = find_children(find_child(corner, "TablixCornerRows"), "TablixCornerRow")
    cells = find_children(corner_rows[row], "TablixCornerCell")
    cell = cells[col]
    if tablix_spans.is_placeholder(cell):
        owner = tablix_spans.owner_map(cells)[col]
        if owner != col:
            raise ValueError(
                f"corner cell (row {row}, col {col}) is covered by the ColSpan of cell "
                f"(row {row}, col {owner}); address that cell."
            )
        raise ValueError(
            f"corner cell (row {row}, col {col}) is covered by the RowSpan of a cell "
            "above it; address that cell."
        )
    textbox = _cell_textbox(cell)
    if textbox is None:
        raise ValueError(
            f"corner cell (row {row}, col {col}) of tablix {tablix_name!r} does not hold a "
            "single textbox; refusing to overwrite it."
        )
    value = expression if expression is not None else text
    if _set_textbox_value(textbox, encode_text(value)):
        changed.append("corner_written")

    if changed:
        doc.save()
    return {
        "tablix": tablix_name,
        "name": textbox.get("Name"),
        "kind": "TablixCorner",
        "row": row,
        "col": col,
        "changed": changed,
    }


__all__ = [
    "add_row_group",
    "add_tablix_filter",
    "MATRIX_AGGREGATES",
    "convert_to_matrix",
    "list_tablix_filters",
    "remove_row_group",
    "remove_tablix_filter",
    "set_detail_row_visibility",
    "set_group_sort",
    "set_group_visibility",
    "set_row_height",
    "set_tablix_corner",
    "set_tablix_size",
]
