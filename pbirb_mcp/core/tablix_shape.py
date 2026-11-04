"""Structural queries over a tablix's row hierarchy.

Report Builder names a tablix's detail group ``Details`` only for the first
table in a report — group names are unique report-wide, so a second table
gets ``Details1``, a list gets ``<list>_Details``, and authors rename them
freely. Tools must therefore identify the detail group by *shape*, not by
name:

* A **detail group** is a ``<Group>`` without ``<GroupExpressions>`` (it
  repeats once per dataset row). In the row hierarchy it sits on a leaf
  ``<TablixMember>``.
* A **dynamic group** is a ``<Group>`` with ``<GroupExpressions>``.
* A **static member** has no ``<Group>`` at all (headers, subtotals).

Body rows correspond 1:1 with the leaf ``<TablixMember>`` elements of
``<TablixRowHierarchy>`` in depth-first document order, so leaf *i* of
:func:`row_leaves` owns ``TablixBody/TablixRows/TablixRow[i]``.
:func:`leaf_row_index` and :func:`row_leaf` translate between the two.

The query functions are read-only and return ``None`` (rather than
raising) when the requested shape is absent; callers decide how to report
that, using :data:`NO_DETAIL_GROUP` for the detail-group case.

Insertion (#33): a new body row/column at index *k* must get its
hierarchy leaf at leaf position *k* — not at top-level position *k*, which
differs as soon as a group wraps members. :func:`insertion_point` computes
where the new member goes (just before ``leaves[k]``, or after the last
leaf for an append), climbing out of any group whose interior it would
otherwise land in, and :func:`insert_member` places it there.
:func:`static_member_like` builds a static member whose ``TablixHeader``
depth matches its new siblings (a matrix's header area must stay
rectangular).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

from lxml import etree

from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd

# Shared wording for "this tablix has no detail group" errors.
NO_DETAIL_GROUP = "no detail group (a leaf Group without GroupExpressions)"

_TABLIX_MEMBER = q("TablixMember")


def _child_members(member: etree._Element) -> list[etree._Element]:
    """The ``TablixMember`` children of ``member``'s ``<TablixMembers>``
    (comments / processing instructions skipped)."""
    members_root = find_child(member, "TablixMembers")
    if members_root is None:
        return []
    return find_children(members_root, "TablixMember")


def is_leaf_member(member: etree._Element) -> bool:
    """True iff ``member`` has no nested ``TablixMember`` children."""
    return not _child_members(member)


def is_detail_group(group: Optional[etree._Element]) -> bool:
    """True iff ``group`` is a ``<Group>`` with no grouping expression.

    An empty ``<GroupExpressions />`` counts as "no grouping expression"
    too — it groups on nothing, which is the detail semantics.
    """
    if group is None:
        return False
    expressions = find_child(group, "GroupExpressions")
    if expressions is None:
        return True
    return not find_children(expressions, "GroupExpression")


def is_dynamic_group(group: Optional[etree._Element]) -> bool:
    """True iff ``group`` is a ``<Group>`` with at least one
    ``<GroupExpression>``."""
    return group is not None and not is_detail_group(group)


def row_leaves(tablix: etree._Element) -> list[etree._Element]:
    """Leaf ``TablixMember`` elements of the row hierarchy, depth-first in
    document order. ``row_leaves(t)[i]`` owns body row ``i``."""
    hierarchy = find_child(tablix, "TablixRowHierarchy")
    if hierarchy is None:
        return []
    return _leaves_of(find_child(hierarchy, "TablixMembers"))


def _leaves_of(members_root: Optional[etree._Element]) -> list[etree._Element]:
    if members_root is None:
        return []
    out: list[etree._Element] = []

    def walk(member: etree._Element) -> None:
        children = _child_members(member)
        if not children:
            out.append(member)
            return
        for child in children:
            walk(child)

    for top in find_children(members_root, "TablixMember"):
        walk(top)
    return out


def _subtree_leaves(member: etree._Element) -> list[etree._Element]:
    """Leaves of the subtree rooted at ``member`` (``[member]`` if it is a
    leaf itself), in document order."""
    if is_leaf_member(member):
        return [member]
    return _leaves_of(find_child(member, "TablixMembers"))


def leaf_row_index(tablix: etree._Element, leaf: etree._Element) -> Optional[int]:
    """Body-row index owned by the row-hierarchy leaf ``leaf``.

    Returns ``None`` if ``leaf`` is not a leaf of ``tablix``'s row hierarchy
    (e.g. a non-leaf group wrapper, or a column-hierarchy member).
    """
    for idx, candidate in enumerate(row_leaves(tablix)):
        if candidate is leaf:
            return idx
    return None


def row_leaf(tablix: etree._Element, row_index: int) -> Optional[etree._Element]:
    """Row-hierarchy leaf ``TablixMember`` owning body row ``row_index``.

    Returns ``None`` when the index is out of range for the hierarchy.
    """
    if row_index < 0:
        return None
    leaves = row_leaves(tablix)
    return leaves[row_index] if row_index < len(leaves) else None


def column_leaves(tablix: etree._Element) -> list[etree._Element]:
    """Leaf ``TablixMember`` elements of the column hierarchy, depth-first
    in document order. ``column_leaves(t)[i]`` owns body column ``i``
    (``TablixColumns/TablixColumn[i]`` and cell ``i`` of every row)."""
    hierarchy = find_child(tablix, "TablixColumnHierarchy")
    if hierarchy is None:
        return []
    return _leaves_of(find_child(hierarchy, "TablixMembers"))


def leaf_column_index(tablix: etree._Element, leaf: etree._Element) -> Optional[int]:
    """Body-column index owned by the column-hierarchy leaf ``leaf``, or
    ``None`` if it isn't one."""
    for idx, candidate in enumerate(column_leaves(tablix)):
        if candidate is leaf:
            return idx
    return None


def column_leaf(tablix: etree._Element, column_index: int) -> Optional[etree._Element]:
    """Column-hierarchy leaf owning body column ``column_index``, or
    ``None`` when out of range."""
    if column_index < 0:
        return None
    leaves = column_leaves(tablix)
    return leaves[column_index] if column_index < len(leaves) else None


def subtree_leaves(member: etree._Element) -> list[etree._Element]:
    """Leaves of the hierarchy subtree rooted at ``member`` (``[member]``
    for a leaf), in document order — i.e. the contiguous run of body
    rows/columns the member spans."""
    return _subtree_leaves(member)


def find_row_group_member(tablix: etree._Element, group_name: str) -> Optional[etree._Element]:
    """The row-hierarchy ``TablixMember`` whose ``<Group Name=...>`` matches
    ``group_name`` (row axis only), or ``None``."""
    hierarchy = find_child(tablix, "TablixRowHierarchy")
    if hierarchy is None:
        return None
    for member in hierarchy.iter(_TABLIX_MEMBER):
        group = find_child(member, "Group")
        if group is not None and group.get("Name") == group_name:
            return member
    return None


def _detail_in(leaves: list[etree._Element]) -> Optional[etree._Element]:
    for leaf in leaves:
        if is_detail_group(find_child(leaf, "Group")):
            return leaf
    return None


def detail_member(tablix: etree._Element) -> Optional[etree._Element]:
    """The row-hierarchy leaf ``TablixMember`` carrying the detail group —
    a ``<Group>`` without ``<GroupExpressions>`` — whatever its ``Name``.

    Returns the first such leaf in document order, or ``None`` when the
    tablix has no detail group (e.g. a matrix).
    """
    return _detail_in(row_leaves(tablix))


def _member_depth(member: etree._Element) -> int:
    depth = 0
    parent = member.getparent()
    while parent is not None:
        if parent.tag == _TABLIX_MEMBER:
            depth += 1
        parent = parent.getparent()
    return depth


def _innermost_dynamic_member(root_members: list[etree._Element]) -> Optional[etree._Element]:
    """Deepest ``TablixMember`` with a dynamic ``<Group>`` among
    ``root_members`` and their descendants (first in document order on a
    tie)."""
    best: Optional[etree._Element] = None
    best_depth = -1
    for root in root_members:
        for member in root.iter(_TABLIX_MEMBER):
            if not is_dynamic_group(find_child(member, "Group")):
                continue
            depth = _member_depth(member)
            if depth > best_depth:
                best, best_depth = member, depth
    return best


def data_leaf(
    tablix: etree._Element,
    group: Union[str, etree._Element, None] = None,
) -> Optional[etree._Element]:
    """The row-hierarchy leaf that holds the tablix's *data* row.

    * The detail leaf (see :func:`detail_member`) when there is one;
    * otherwise the first leaf of the innermost dynamic row group — the
      row that renders once per group instance in a matrix (its group
      header row when the group wraps static members, or the group's own
      member when that is itself a leaf);
    * ``None`` when the row hierarchy has neither (an all-static tablix).

    ``group`` (a row-group name or its ``TablixMember``) scopes the search
    to that group's subtree, so callers acting on one group (e.g.
    ``add_subtotal_row``) get that group's own data row. Returns ``None``
    if a named group is not in the row hierarchy.
    """
    if group is None:
        hierarchy = find_child(tablix, "TablixRowHierarchy")
        members_root = find_child(hierarchy, "TablixMembers") if hierarchy is not None else None
        roots = find_children(members_root, "TablixMember") if members_root is not None else []
        leaves = row_leaves(tablix)
    else:
        member = find_row_group_member(tablix, group) if isinstance(group, str) else group
        if member is None:
            return None
        roots = [member]
        leaves = _subtree_leaves(member)

    detail = _detail_in(leaves)
    if detail is not None:
        return detail
    innermost = _innermost_dynamic_member(roots)
    if innermost is None:
        return None
    return _subtree_leaves(innermost)[0]


def data_row_index(
    tablix: etree._Element,
    group: Union[str, etree._Element, None] = None,
) -> Optional[int]:
    """Body-row index of :func:`data_leaf` (``None`` if there is none)."""
    leaf = data_leaf(tablix, group)
    return leaf_row_index(tablix, leaf) if leaf is not None else None


def detail_row_index(tablix: etree._Element) -> Optional[int]:
    """Body-row index of :func:`detail_member` (``None`` if there is none)."""
    leaf = detail_member(tablix)
    return leaf_row_index(tablix, leaf) if leaf is not None else None


# ---- axis-generic helpers + insertion (#33) -------------------------------

_AXES = {"row": "TablixRowHierarchy", "column": "TablixColumnHierarchy"}


def _hierarchy_tag(axis: str) -> str:
    try:
        return _AXES[axis]
    except KeyError:
        raise ValueError(f"axis must be 'row' or 'column', got {axis!r}") from None


def axis_leaves(tablix: etree._Element, axis: str) -> list[etree._Element]:
    """:func:`row_leaves` or :func:`column_leaves` by ``axis``
    (``"row"`` / ``"column"``)."""
    if _hierarchy_tag(axis) == "TablixRowHierarchy":
        return row_leaves(tablix)
    return column_leaves(tablix)


def hierarchy_members(tablix: etree._Element, axis: str) -> etree._Element:
    """The top-level ``<TablixMembers>`` of the ``axis`` hierarchy, created
    (with its hierarchy) when absent."""
    tag = _hierarchy_tag(axis)
    hierarchy = find_child(tablix, tag)
    if hierarchy is None:
        hierarchy = etree.SubElement(tablix, q(tag))
    members = find_child(hierarchy, "TablixMembers")
    if members is None:
        members = etree.SubElement(hierarchy, q("TablixMembers"))
    return members


def find_group_member(
    tablix: etree._Element, axis: str, group_name: str
) -> Optional[etree._Element]:
    """The ``TablixMember`` of the ``axis`` hierarchy whose ``<Group
    Name=...>`` is ``group_name``, or ``None``."""
    hierarchy = find_child(tablix, _hierarchy_tag(axis))
    if hierarchy is None:
        return None
    for member in hierarchy.iter(_TABLIX_MEMBER):
        group = find_child(member, "Group")
        if group is not None and group.get("Name") == group_name:
            return member
    return None


def owner_member(member: etree._Element) -> Optional[etree._Element]:
    """The ``TablixMember`` whose ``<TablixMembers>`` contains ``member``,
    or ``None`` for a top-level member."""
    parent = member.getparent()
    owner = parent.getparent() if parent is not None else None
    if owner is not None and owner.tag == _TABLIX_MEMBER:
        return owner
    return None


def leaf_span(tablix: etree._Element, axis: str, member: etree._Element) -> tuple[int, int]:
    """``(first, last)`` body indices spanned by ``member``'s subtree
    (inclusive)."""
    leaves = axis_leaves(tablix, axis)
    sub = _subtree_leaves(member)
    first = next(i for i, leaf in enumerate(leaves) if leaf is sub[0])
    return first, first + len(sub) - 1


@dataclass(frozen=True)
class InsertionPoint:
    """Where a new hierarchy member goes so that it owns body index
    :attr:`index`.

    * ``anchor`` — the existing member the new one becomes the previous
      (``before=True``) or next (``before=False``) sibling of; ``None``
      when the hierarchy has no members yet (insert into ``members``).
    * ``members`` — the ``<TablixMembers>`` list that receives it.
    * ``index`` — the body row/column index the new leaf will own. Equal
      to the requested index unless that fell strictly inside a group, in
      which case the member is placed just before that group and
      ``relocated_from`` names it.
    * ``mismatch`` — ``(leaves, body)`` counts when the hierarchy was
      already out of step with the body (see :func:`place_static_leaf`).
    """

    anchor: Optional[etree._Element]
    before: bool
    members: etree._Element
    index: int
    requested: int
    relocated_from: Optional[str] = None
    mismatch: Optional[tuple[int, int]] = None


def insertion_point(
    tablix: etree._Element,
    axis: str,
    index: int,
    inside: Optional[etree._Element] = None,
) -> InsertionPoint:
    """Hierarchy position for a new static leaf owning body ``index``.

    The new member goes just before ``leaves[index]`` (after the last leaf
    when ``index == len(leaves)``) and is then lifted out of every group it
    would land in, so it stays a static member that renders once:

    * at a group's edge (its first leaf, or just past its last) the lift is
      exact — the member becomes the group member's sibling and still owns
      body ``index``;
    * strictly inside a group (detail or dynamic) the member is placed just
      before that group instead; :attr:`InsertionPoint.index` then reports
      the actual body index and ``relocated_from`` the group's name;
    * static wrapper members (no ``<Group>``) are only climbed out of at an
      edge — inserting between their children is fine.

    ``inside`` (a group ``TablixMember``) deliberately keeps the member
    inside that group (a per-instance row/column): ``index`` must lie in
    ``first..last+1`` of the group's span, and lifting stops at the
    group's own ``<TablixMembers>``.

    Raises ``IndexError`` for an index outside ``0..len(leaves)`` and
    ``ValueError`` for an ``inside`` group that cannot host the index.
    """
    leaves = axis_leaves(tablix, axis)
    n = len(leaves)
    if index < 0 or index > n:
        raise IndexError(f"position {index} out of range; tablix has {n} {axis}(s)")
    if n == 0:
        if inside is not None:
            raise ValueError("cannot insert inside a group of an empty hierarchy")
        return InsertionPoint(None, True, hierarchy_members(tablix, axis), 0, index)

    after_inside_end = False
    if inside is not None:
        group = find_child(inside, "Group")
        name = group.get("Name") if group is not None else None
        if is_leaf_member(inside):
            raise ValueError(
                f"group {name!r} has no child members to insert among; its member "
                "is itself the leaf"
            )
        first, last = leaf_span(tablix, axis, inside)
        if not first <= index <= last + 1:
            raise ValueError(
                f"position {index} is outside group {name!r}, which accepts "
                f"{axis} positions {first}..{last + 1}"
            )
        after_inside_end = index == last + 1

    if index < n and not after_inside_end:
        anchor, before = leaves[index], True
    else:
        anchor, before = leaves[index - 1], False

    relocated: Optional[str] = None
    while True:
        owner = owner_member(anchor)
        if owner is None or owner is inside:
            break
        siblings = find_children(anchor.getparent(), "TablixMember")
        at_edge = siblings[0] is anchor if before else siblings[-1] is anchor
        group = find_child(owner, "Group")
        if group is None and not at_edge:
            break
        if not at_edge:
            relocated = group.get("Name")
        anchor = owner

    first, last = leaf_span(tablix, axis, anchor)
    actual = first if before else last + 1
    return InsertionPoint(
        anchor,
        before,
        anchor.getparent(),
        actual,
        index,
        relocated if actual != index else None,
    )


def insert_member(point: InsertionPoint, new_member: etree._Element) -> None:
    """Place ``new_member`` at ``point`` (see :func:`insertion_point`)."""
    if point.anchor is None:
        point.members.append(new_member)
    elif point.before:
        point.anchor.addprevious(new_member)
    else:
        point.anchor.addnext(new_member)


def header_sizes(member: etree._Element) -> list[str]:
    """``TablixHeader/Size`` of each header level on ``member``'s
    first-leaf path (``member`` itself, then its first child, ...). Empty
    when the hierarchy carries no headers (a plain table)."""
    sizes: list[str] = []
    current: Optional[etree._Element] = member
    while current is not None:
        header = find_child(current, "TablixHeader")
        if header is not None:
            size = find_child(header, "Size")
            sizes.append((size.text or "1in") if size is not None else "1in")
        children = _child_members(current)
        current = children[0] if children else None
    return sizes


def _blank_textbox(name: str) -> etree._Element:
    tb = etree.Element(q("Textbox"), Name=name)
    etree.SubElement(tb, q("CanGrow")).text = "true"
    etree.SubElement(tb, q("KeepTogether")).text = "true"
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    paragraph = etree.SubElement(paragraphs, q("Paragraph"))
    textruns = etree.SubElement(paragraph, q("TextRuns"))
    textrun = etree.SubElement(textruns, q("TextRun"))
    etree.SubElement(textrun, q("Value"))
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    etree.SubElement(tb, qrd("DefaultName")).text = name
    etree.SubElement(tb, q("Style"))
    return tb


def static_member_like(
    reference: Optional[etree._Element],
    header_name: Callable[[], str],
) -> tuple[etree._Element, list[str]]:
    """A new static ``TablixMember`` with the same header depth as
    ``reference`` (a would-be sibling).

    When ``reference`` carries no ``TablixHeader`` on its first-leaf path
    the result is a bare ``<TablixMember/>``. Otherwise each header level
    becomes a nested static member with a ``TablixHeader`` of the same
    ``Size`` holding a blank textbox named by ``header_name()`` — one leaf
    in the end, so it still owns exactly one body row/column. Returns
    ``(member, header_textbox_names)``.
    """
    sizes = header_sizes(reference) if reference is not None else []
    top = etree.Element(_TABLIX_MEMBER)
    emitted: list[str] = []
    current = top
    for depth, size in enumerate(sizes):
        header = etree.SubElement(current, q("TablixHeader"))
        etree.SubElement(header, q("Size")).text = size
        contents = etree.SubElement(header, q("CellContents"))
        name = header_name()
        emitted.append(name)
        contents.append(_blank_textbox(name))
        if depth < len(sizes) - 1:
            members = etree.SubElement(current, q("TablixMembers"))
            current = etree.SubElement(members, _TABLIX_MEMBER)
    return top, emitted


def removal_member(
    tablix: etree._Element, axis: str, index: int
) -> tuple[etree._Element, list[etree._Element]]:
    """The hierarchy member to delete so that body row/column ``index``
    disappears together with exactly its leaf.

    Starts at ``leaves[index]`` and climbs while the member is its
    parent's only child — deleting it alone would leave an empty
    ``<TablixMembers/>`` (XSD-invalid) and turn the parent into a leaf
    with no body row. Returns ``(member, emptied_groups)``, where
    ``emptied_groups`` are the group ``TablixMember`` elements climbed
    through (outermost last): non-empty means the removal takes those
    groups with it. Raises ``IndexError`` for an index with no leaf.
    """
    leaves = axis_leaves(tablix, axis)
    if index < 0 or index >= len(leaves):
        raise IndexError(f"{axis} {index} has no hierarchy leaf ({len(leaves)} leaves)")
    member = leaves[index]
    emptied: list[etree._Element] = []
    if find_child(member, "Group") is not None:
        emptied.append(member)  # a group whose own member is the leaf
    while True:
        owner = owner_member(member)
        if owner is None or len(find_children(member.getparent(), "TablixMember")) > 1:
            break
        if find_child(owner, "Group") is not None:
            emptied.append(owner)
        member = owner
    return member, emptied


def detach(el: etree._Element) -> None:
    """Remove ``el`` from its parent keeping the pretty-print layout: when
    it is the last child, its tail (the parent's closing indent) moves to
    the previous sibling instead of being lost with it."""
    parent = el.getparent()
    prev = el.getprevious()
    if el.getnext() is None and prev is not None and el.tail is not None and not el.tail.strip():
        prev.tail = el.tail
    parent.remove(el)


def insert_nth_child(
    parent: etree._Element, local_name: str, index: int, new_child: etree._Element
) -> None:
    """Insert ``new_child`` so it becomes the ``index``-th ``local_name``
    child of ``parent`` (appended after the last one when ``index`` equals
    their count). Counts only ``local_name`` children, so comments or other
    siblings don't shift the position."""
    existing = find_children(parent, local_name)
    if index < len(existing):
        existing[index].addprevious(new_child)
    elif existing:
        existing[-1].addnext(new_child)
    else:
        parent.append(new_child)


def body_count(tablix: etree._Element, axis: str) -> int:
    """Number of body rows (``axis="row"``) or columns (``"column"``)."""
    body = find_child(tablix, "TablixBody")
    if body is None:
        return 0
    if _hierarchy_tag(axis) == "TablixRowHierarchy":
        root = find_child(body, "TablixRows")
        return len(find_children(root, "TablixRow")) if root is not None else 0
    root = find_child(body, "TablixColumns")
    return len(find_children(root, "TablixColumn")) if root is not None else 0


def place_static_leaf(
    tablix: etree._Element,
    axis: str,
    position: Optional[int],
    inside_group: Optional[str],
    header_name: Callable[[], str],
) -> tuple[InsertionPoint, list[str]]:
    """Insert a new static leaf member into the ``axis`` hierarchy so it
    owns body ``position`` (default: append) — see :func:`insertion_point`
    — and return ``(point, header_textbox_names)``. The caller inserts the
    body row/column at ``point.index``.

    Checks happen before any mutation. ``inside_group`` must name a group
    on this axis (:class:`~pbirb_mcp.core.ids.ElementNotFoundError`
    otherwise). When the hierarchy's leaf count already differs from the
    body's (leaf *i* ↔ body *i* is broken, so no index can be trusted) only
    a plain append is allowed — the new leaf goes after the last leaf, the
    body row/column at the end, and ``point.mismatch`` records the counts
    so the caller can warn; an explicit ``position`` or ``inside_group``
    is refused with ``ValueError``.
    """
    leaves = axis_leaves(tablix, axis)
    n_body = body_count(tablix, axis)
    if leaves and len(leaves) != n_body:
        if position is not None or inside_group is not None:
            raise ValueError(
                f"the {axis} hierarchy has {len(leaves)} leaf member(s) but the body has "
                f"{n_body} {axis}(s), so {axis} positions are ambiguous; refusing a "
                "positioned insert (only an append is allowed on an inconsistent tablix)"
            )
        tail = insertion_point(tablix, axis, len(leaves))
        point = InsertionPoint(
            tail.anchor, tail.before, tail.members, n_body, n_body, None, (len(leaves), n_body)
        )
        member, header_names = static_member_like(point.anchor, header_name)
        insert_member(point, member)
        return point, header_names
    inside = None
    if inside_group is not None:
        inside = find_group_member(tablix, axis, inside_group)
        if inside is None:
            raise ElementNotFoundError(
                f"{axis}-axis group {inside_group!r} not found in tablix {tablix.get('Name')!r}"
            )
    index = n_body if position is None else position
    if not leaves:
        # Degenerate (no hierarchy members yet): only an append is meaningful.
        if index < 0 or index > n_body:
            raise IndexError(f"position {position} out of range; tablix has {n_body} {axis}(s)")
        point = InsertionPoint(None, True, hierarchy_members(tablix, axis), index, index)
    else:
        point = insertion_point(tablix, axis, index, inside)
    member, header_names = static_member_like(point.anchor, header_name)
    insert_member(point, member)
    return point, header_names


def placement_warnings(point: InsertionPoint, axis: str) -> list[str]:
    """Human-readable notes for an :class:`InsertionPoint`: an index moved
    out of a group's interior, or an already-inconsistent hierarchy."""
    out: list[str] = []
    if point.mismatch is not None:
        n_leaves, n_body = point.mismatch
        out.append(
            f"the {axis} hierarchy had {n_leaves} leaf member(s) for {n_body} body "
            f"{axis}(s) before this call (an existing inconsistency Report Builder "
            "rejects); the new one was appended at the end and the mismatch remains"
        )
    if point.relocated_from is not None:
        out.append(_relocation_text(point, axis))
    return out


def _relocation_text(point: InsertionPoint, axis: str) -> str:
    return (
        f"position {point.requested} falls inside group {point.relocated_from!r}; a "
        f"static {axis} there would repeat per group instance, so it was inserted at "
        f"{axis} {point.index}, just before the group. Pass inside_group="
        f"{point.relocated_from!r} to insert inside the group instead."
    )


__all__ = [
    "NO_DETAIL_GROUP",
    "InsertionPoint",
    "body_count",
    "place_static_leaf",
    "placement_warnings",
    "removal_member",
    "axis_leaves",
    "column_leaf",
    "column_leaves",
    "data_leaf",
    "data_row_index",
    "detach",
    "detail_member",
    "detail_row_index",
    "find_group_member",
    "find_row_group_member",
    "header_sizes",
    "hierarchy_members",
    "insert_member",
    "insert_nth_child",
    "insertion_point",
    "is_detail_group",
    "is_dynamic_group",
    "is_leaf_member",
    "leaf_column_index",
    "leaf_row_index",
    "leaf_span",
    "owner_member",
    "row_leaf",
    "row_leaves",
    "static_member_like",
    "subtree_leaves",
]
