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

All functions are read-only and return ``None`` (rather than raising) when
the requested shape is absent; callers decide how to report that, using
:data:`NO_DETAIL_GROUP` for the detail-group case.
"""

from __future__ import annotations

from typing import Optional, Union

from lxml import etree

from pbirb_mcp.core.xpath import find_child, find_children, q

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


__all__ = [
    "NO_DETAIL_GROUP",
    "data_leaf",
    "data_row_index",
    "detail_member",
    "detail_row_index",
    "find_row_group_member",
    "is_detail_group",
    "is_dynamic_group",
    "is_leaf_member",
    "leaf_row_index",
    "row_leaf",
    "row_leaves",
]
