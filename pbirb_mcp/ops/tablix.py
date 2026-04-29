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
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd

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
            f"unknown filter operator {operator!r}; valid operators are: {sorted(_VALID_OPERATORS)}"
        )
    if not values:
        raise ValueError("at least one filter value is required")

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    filters_root = _ensure_filters_block(tablix)

    filter_node = etree.SubElement(filters_root, q("Filter"))
    expr_node = etree.SubElement(filter_node, q("FilterExpression"))
    expr_node.text = encode_text(expression)
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


def _group_names_in_tablix(tablix: etree._Element) -> set[str]:
    return {g.get("Name") for g in tablix.iter(q("Group")) if g.get("Name") is not None}


def _column_count(tablix: etree._Element) -> int:
    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    if cols_root is None:
        return 0
    return len(find_children(cols_root, "TablixColumn"))


def _build_group_header_row(
    column_count: int,
    group_name: str,
    group_expression: str,
) -> etree._Element:
    """Build a TablixRow whose first cell renders the group expression and
    whose remaining cells are blank textboxes. Textbox names are derived
    from the group name (``<group>_Header_<col_index>``) — Report Builder
    enforces report-wide uniqueness, so the caller is responsible for
    unique group names.
    """
    row = etree.Element(q("TablixRow"))
    height = etree.SubElement(row, q("Height"))
    height.text = "0.25in"
    cells_root = etree.SubElement(row, q("TablixCells"))
    for i in range(column_count):
        cell = etree.SubElement(cells_root, q("TablixCell"))
        contents = etree.SubElement(cell, q("CellContents"))
        textbox_name = f"{group_name}_Header_{i}"
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

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    if group_name in _group_names_in_tablix(tablix):
        raise ValueError(f"group name {group_name!r} already exists in tablix {tablix_name!r}")

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
    new_row = _build_group_header_row(
        column_count=_column_count(tablix),
        group_name=group_name,
        group_expression=group_expression,
    )
    rows_root.insert(0, new_row)

    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "expression": group_expression,
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

    Refuses to remove the conventional ``Details`` group — a tablix without
    leaves is not useful, and Report Builder treats Details specially.
    """
    if group_name == "Details":
        raise ValueError(
            "Details is the conventional leaf group and cannot be removed; "
            "drop the entire tablix instead if you really mean it."
        )

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    member = _find_member_for_group(tablix, group_name)
    if member is None:
        raise ElementNotFoundError(
            f"row-axis group {group_name!r} not found in tablix {tablix_name!r}; "
            "use remove_column_group for a column-axis group."
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


def _apply_visibility_to_member(
    member: etree._Element,
    visibility_expression: str,
    toggle_textbox: Optional[str],
) -> None:
    """Hierarchy-agnostic core of set_group_visibility / set_column_group_visibility."""
    new_vis = etree.Element(q("Visibility"))
    hidden = etree.SubElement(new_vis, q("Hidden"))
    hidden.text = encode_text(visibility_expression)
    if toggle_textbox is not None:
        toggle = etree.SubElement(new_vis, q("ToggleItem"))
        toggle.text = encode_text(toggle_textbox)
    _insert_member_child(member, new_vis)


def set_group_visibility(
    path: str,
    tablix_name: str,
    group_name: str,
    visibility_expression: str,
    toggle_textbox: Optional[str] = None,
) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    member = _find_member_for_group(tablix, group_name)
    if member is None:
        raise ElementNotFoundError(
            f"row-axis group {group_name!r} not found in tablix {tablix_name!r}; "
            "for a column-axis group use set_column_group_visibility."
        )

    _apply_visibility_to_member(member, visibility_expression, toggle_textbox)

    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "visibility_expression": visibility_expression,
        "toggle_textbox": toggle_textbox,
    }


# ---- set_detail_row_visibility --------------------------------------------


def set_detail_row_visibility(
    path: str,
    tablix_name: str,
    expression: str,
    toggle_textbox: Optional[str] = None,
) -> dict[str, Any]:
    """Set ``<Visibility>`` on the ``Details`` group's TablixMember.

    The detail group is the conventional leaf in a Power BI paginated
    tablix — Report Builder always emits it as ``<Group Name="Details" />``.
    Hiding it is how callers conditionally suppress the per-row body
    without restructuring the hierarchy.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    member = _find_member_for_group(tablix, "Details")
    if member is None:
        raise ElementNotFoundError(f"tablix {tablix_name!r} has no Details group")

    new_vis = etree.Element(q("Visibility"))
    hidden = etree.SubElement(new_vis, q("Hidden"))
    hidden.text = encode_text(expression)
    if toggle_textbox is not None:
        toggle = etree.SubElement(new_vis, q("ToggleItem"))
        toggle.text = encode_text(toggle_textbox)

    _insert_member_child(member, new_vis)

    doc.save()
    return {
        "tablix": tablix_name,
        "expression": expression,
        "toggle_textbox": toggle_textbox,
    }


# ---- set_row_height -------------------------------------------------------


def set_row_height(
    path: str,
    tablix_name: str,
    row_index: int,
    height: str,
) -> dict[str, Any]:
    if not height or not height.strip():
        raise ValueError("height must be a non-empty RDL size (e.g. '0.25in', '1cm')")

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
    Empty / whitespace-only values are rejected.

    Returns ``{tablix, kind, changed: list[str]}`` — empty list when
    inputs match existing (no save).
    """
    if height is not None and (not height or not height.strip()):
        raise ValueError("height must be a non-empty RDL size (e.g. '4in', '10cm')")
    if width is not None and (not width or not width.strip()):
        raise ValueError("width must be a non-empty RDL size (e.g. '4in', '10cm')")
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


__all__ = [
    "add_row_group",
    "add_tablix_filter",
    "list_tablix_filters",
    "remove_row_group",
    "remove_tablix_filter",
    "set_detail_row_visibility",
    "set_group_sort",
    "set_group_visibility",
    "set_row_height",
    "set_tablix_size",
]
