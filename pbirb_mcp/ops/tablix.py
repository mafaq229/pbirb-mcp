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

from pbirb_mcp.core import names, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_dataset, resolve_tablix
from pbirb_mcp.core.tablix_shape import (
    NO_DETAIL_GROUP,
    detail_member,
    is_detail_group,
    is_dynamic_group,
    leaf_column_index,
    leaf_row_index,
    subtree_leaves,
)
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd
from pbirb_mcp.ops import visibility as _visibility
from pbirb_mcp.ops.filter_types import type_mismatch_warnings, wrap_with_format

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


def remove_row_group(
    path: str,
    tablix_name: str,
    group_name: str,
) -> dict[str, Any]:
    """Inverse of :func:`add_row_group` for groups that were added via this
    tool. Unwraps the group's children back to its parent and removes the
    matching group-header row at body row 0.

    Refuses to remove the detail group (a ``<Group>`` without
    ``<GroupExpressions>`` — ``Details``, ``Details1``, ...): a tablix
    without its detail leaf is not useful, and Report Builder treats the
    detail group specially.
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
    if inner_members is None or len(list(inner_members)) == 0:
        # Nothing to unwrap — this is a leaf group (e.g. a sibling-style add
        # we don't yet support). Refuse, since blindly removing would orphan
        # body rows and leave the tablix in an inconsistent state.
        raise ValueError(
            f"group {group_name!r} has no nested children; refusing to "
            "remove a leaf group automatically."
        )

    inner_children = list(inner_members)
    # First inner child is the group-header leaf added by add_row_group.
    header_leaf, *wrapped = inner_children

    parent_members = member.getparent()
    member_idx = list(parent_members).index(member)

    # Replace the wrapper at its original position with its wrapped originals.
    parent_members.remove(member)
    for offset, child in enumerate(wrapped):
        inner_members.remove(child)
        parent_members.insert(member_idx + offset, child)

    # Remove the body row for the group header. Position is row 0 if the
    # removed group was the top-level outer wrapper (which is all we
    # currently support adding via add_row_group).
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows")
    rows = find_children(rows_root, "TablixRow")
    if rows:
        rows_root.remove(rows[0])

    doc.save()
    return {"tablix": tablix_name, "removed": group_name}


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

    _apply_sort_to_member(member, sort_expressions)

    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "sort_expressions": list(sort_expressions),
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
    height_node.text = height

    doc.save()
    return {"tablix": tablix_name, "row_index": row_index, "height": height}


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


# ---- convert_to_matrix (v0.4 commit 17) -----------------------------------


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


def convert_to_matrix(
    path: str,
    tablix_name: str,
    row_group: str,
    column_group: str,
) -> dict[str, Any]:
    """Drop the detail leaf from a tablix that already has a named
    row group and named column group, converting it into a matrix shape.

    Pre-conditions (all checked, refuses with :class:`ValueError`):

    * ``row_group`` must be the ``Name`` of an existing
      ``<TablixMember>/<Group>`` in the row hierarchy.
    * ``column_group`` must be the ``Name`` of an existing
      ``<TablixMember>/<Group>`` in the column hierarchy.
    * The row hierarchy must contain a detail group — a leaf
      ``<Group>`` without ``<GroupExpressions>``, whatever its name
      (``Details``, ``Details1``, ...). Without one the tablix is
      already matrix-shaped — re-running the conversion is a no-op so
      we refuse with a clear "already a matrix" hint
      (idempotency-by-explicit-refusal).

    Action:

    1. Remove the detail ``<TablixMember>`` from its parent
       ``<TablixMembers>``.
    2. Remove the body row that leaf owned (found by walking the row
       hierarchy, so trailing subtotal/footer rows are preserved).
       If the detail leaf was its parent group's only child, the
       parent group becomes the leaf and takes over that row instead
       (an empty ``<TablixMembers/>`` is schema-invalid).

    ``row_group`` and ``column_group`` are kept as required arguments
    so the caller asserts "yes I know which groups make this a
    matrix" — the verb refuses fast on partially-grouped tablixes.

    Returns ``{tablix, kind, changed}`` with the canonical mutator
    shape. ``changed`` lists the structural mutations applied.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    # Row-group + column-group preconditions.
    if _find_member_for_group(tablix, row_group) is None:
        raise ValueError(
            f"row group {row_group!r} not found in tablix {tablix_name!r}; "
            "call add_row_group first or check the name."
        )
    if not _has_column_group(tablix):
        raise ValueError(
            f"column group {column_group!r} not found in tablix {tablix_name!r}; "
            "call add_column_group first."
        )
    # Optional: also verify the named column group exists by Name
    # (cheap O(n) walk; defensive against typos).
    column_hierarchy = find_child(tablix, "TablixColumnHierarchy")
    column_group_present = any(
        (g.get("Name") or "") == column_group for g in column_hierarchy.iter(q("Group"))
    )
    if not column_group_present:
        raise ValueError(
            f"column group {column_group!r} not found in tablix {tablix_name!r}; "
            "call add_column_group first."
        )

    details_member = detail_member(tablix)
    if details_member is None:
        raise ValueError(
            f"tablix {tablix_name!r} is already a matrix ({NO_DETAIL_GROUP} "
            "in the row hierarchy); convert_to_matrix is idempotent-by-refusal."
        )
    details_row_idx = leaf_row_index(tablix, details_member)

    parent_members = details_member.getparent()
    siblings = find_children(parent_members, "TablixMember")
    owner = parent_members.getparent()
    if len(siblings) == 1 and owner is not None and owner.tag == q("TablixMember"):
        # The detail leaf was its group's only child: drop the now-empty
        # <TablixMembers> so the group member becomes the leaf. Leaf count
        # is unchanged, so the body row stays — it now belongs to the group.
        owner.remove(parent_members)
        changed = ["details_member_removed"]
    else:
        parent_members.remove(details_member)
        body = find_child(tablix, "TablixBody")
        rows_root = find_child(body, "TablixRows") if body is not None else None
        rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
        if details_row_idx is not None and details_row_idx < len(rows):
            rows_root.remove(rows[details_row_idx])
        changed = ["details_member_removed", "details_body_row_removed"]

    doc.save()
    return {
        "tablix": tablix_name,
        "kind": "Tablix",
        "changed": changed,
    }


# ---- set_tablix_corner (v0.4 commit 18) -----------------------------------


# Per RDL XSD, TablixCorner is the FIRST child of Tablix when present —
# before TablixBody and the hierarchies. Inserting before any of these
# anchors places the new element at the correct slot.
_TABLIX_CORNER_FOLLOWED_BY = (
    "TablixBody",
    "TablixColumnHierarchy",
    "TablixRowHierarchy",
    "LayoutDirection",
    "GroupsBeforeRowHeaders",
    "RepeatColumnHeaders",
    "RepeatRowHeaders",
    "FixedColumnHeaders",
    "FixedRowHeaders",
    "DataSetName",
)


def set_tablix_corner(
    path: str,
    tablix_name: str,
    text: Optional[str] = None,
    expression: Optional[str] = None,
) -> dict[str, Any]:
    """Write the ``<TablixCorner>`` block with a single 1×1 textbox.

    Either ``text`` (literal value) or ``expression`` (VB.NET ``=...``
    string) — mutually exclusive. The textbox's ``Name`` is
    deterministic: ``<tablix_name>_Corner`` (suffixed ``_Corner1``, …
    only if another report item or group already uses that name). Replaces any existing
    TablixCorner block; idempotent in the "same text/expression →
    same bytes" sense.

    Refuses if the tablix has no named column-axis group — the corner
    block is only meaningful in a matrix shape. Without a column
    group there are no row-header rows for the corner to sit over.

    Returns the canonical mutator shape
    ``{tablix, name, kind: 'TablixCorner', changed: list[str]}``.
    """
    if text is None and expression is None:
        raise ValueError(
            "set_tablix_corner requires either text or expression (both cannot be None)"
        )
    if text is not None and expression is not None:
        raise ValueError(
            "set_tablix_corner: text and expression are mutually exclusive — pass only one"
        )

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    if not _has_column_group(tablix):
        raise ValueError(
            f"tablix {tablix_name!r} has no named column group; the "
            "TablixCorner block is only meaningful in a matrix shape. "
            "Call add_column_group first."
        )

    value_text = expression if expression is not None else text

    changed: list[str] = []

    # Remove any existing TablixCorner so the rewrite is deterministic.
    existing = find_child(tablix, "TablixCorner")
    # The corner being replaced doesn't count as a clash, so a re-run keeps
    # the same "<tablix>_Corner" name (same text -> same bytes).
    textbox_name = names.NameRegistry(
        doc, exclude=[existing] if existing is not None else []
    ).unique(f"{tablix_name}_Corner")
    if existing is not None:
        tablix.remove(existing)
        changed.append("replaced_existing")

    corner = etree.Element(q("TablixCorner"))
    rows_root = etree.SubElement(corner, q("TablixCornerRows"))
    row = etree.SubElement(rows_root, q("TablixCornerRow"))
    cell = etree.SubElement(row, q("TablixCornerCell"))
    contents = etree.SubElement(cell, q("CellContents"))
    tb = etree.SubElement(contents, q("Textbox"), Name=textbox_name)
    etree.SubElement(tb, q("CanGrow")).text = "true"
    etree.SubElement(tb, q("KeepTogether")).text = "true"
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    paragraph = etree.SubElement(paragraphs, q("Paragraph"))
    textruns = etree.SubElement(paragraph, q("TextRuns"))
    textrun = etree.SubElement(textruns, q("TextRun"))
    value_node = etree.SubElement(textrun, q("Value"))
    value_node.text = encode_text(value_text)
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    etree.SubElement(tb, qrd("DefaultName")).text = textbox_name
    etree.SubElement(tb, q("Style"))

    # Insert before the first known follower (TablixBody, hierarchies,
    # etc.). Per RDL XSD, TablixCorner is the first Tablix child slot.
    for local in _TABLIX_CORNER_FOLLOWED_BY:
        anchor = find_child(tablix, local)
        if anchor is not None:
            anchor.addprevious(corner)
            break
    else:
        # Pathological: tablix has none of the expected children. Append.
        tablix.append(corner)

    changed.append("corner_written")

    doc.save()
    return {
        "tablix": tablix_name,
        "name": textbox_name,
        "kind": "TablixCorner",
        "changed": changed,
    }


__all__ = [
    "add_row_group",
    "add_tablix_filter",
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
