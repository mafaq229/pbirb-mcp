"""Structural queries over a chart's series / category hierarchies.

RDL ties a chart's data to its hierarchies **by position**, the same way a
tablix ties body rows to row-hierarchy leaves:

* ``ChartData/ChartSeriesCollection/ChartSeries[i]`` belongs to leaf *i*
  of ``ChartSeriesHierarchy`` — the leaf ``<ChartMember>`` elements in
  depth-first document order. That member's ``<Label>`` is the series'
  legend text, and any ``<Group>`` on it (or on an ancestor member) is the
  dynamic series group that fans the series out at render time.
* ``ChartSeries[i]/ChartDataPoints/ChartDataPoint[j]`` belongs to leaf *j*
  of ``ChartCategoryHierarchy``.

So a well-formed chart has exactly as many ``ChartSeries`` as series leaf
members, and every series has exactly as many ``ChartDataPoint`` as there
are category leaf members. The bundled XSD checks neither (it types the
hierarchies as an unordered choice), so tools that add or remove series
must keep both counts in step themselves.

``ChartDerivedSeriesCollection/ChartDerivedSeries/ChartSeries`` (moving
averages, Bollinger bands, ...) are computed from a source series named by
``SourceChartSeriesName``; they have no hierarchy member of their own but
share the chart's series-name namespace.

All functions here are read-only. :func:`series_leaf_members` /
:func:`series_member` / :func:`member_series` translate between series and
hierarchy members; :func:`hierarchy_problems` reports invariant breaks.
"""

from __future__ import annotations

from typing import Optional, Union

from lxml import etree

from pbirb_mcp.core.xpath import find_child, find_children, q

_CHART_MEMBER = q("ChartMember")


def _leaves_of(members_root: Optional[etree._Element]) -> list[etree._Element]:
    """Leaf ``ChartMember`` elements under a ``<ChartMembers>``, depth-first
    in document order. A member is a leaf when it has no nested
    ``ChartMember`` children (an empty ``<ChartMembers/>`` counts as none).
    """
    if members_root is None:
        return []
    out: list[etree._Element] = []
    for member in find_children(members_root, "ChartMember"):
        out.extend(subtree_leaves(member))
    return out


def child_members(member: etree._Element) -> list[etree._Element]:
    """The ``ChartMember`` children of ``member``'s ``<ChartMembers>``."""
    members_root = find_child(member, "ChartMembers")
    if members_root is None:
        return []
    return find_children(members_root, "ChartMember")


def subtree_leaves(member: etree._Element) -> list[etree._Element]:
    """Leaf members of ``member``'s subtree (``[member]`` when it is a leaf)."""
    children = child_members(member)
    if not children:
        return [member]
    out: list[etree._Element] = []
    for child in children:
        out.extend(subtree_leaves(child))
    return out


def series_hierarchy_members(chart: etree._Element) -> Optional[etree._Element]:
    """The top-level ``ChartSeriesHierarchy/ChartMembers`` element, or None."""
    hierarchy = find_child(chart, "ChartSeriesHierarchy")
    if hierarchy is None:
        return None
    return find_child(hierarchy, "ChartMembers")


def series_leaf_members(chart: etree._Element) -> list[etree._Element]:
    """Leaf ``ChartMember`` elements of the series hierarchy, depth-first in
    document order. ``series_leaf_members(c)[i]`` belongs to series ``i``."""
    return _leaves_of(series_hierarchy_members(chart))


def category_leaf_members(chart: etree._Element) -> list[etree._Element]:
    """Leaf ``ChartMember`` elements of the category hierarchy, depth-first
    in document order. Leaf ``j`` owns every series' ``ChartDataPoint[j]``."""
    hierarchy = find_child(chart, "ChartCategoryHierarchy")
    if hierarchy is None:
        return []
    return _leaves_of(find_child(hierarchy, "ChartMembers"))


def series_list(chart: etree._Element) -> list[etree._Element]:
    """``ChartData/ChartSeriesCollection/ChartSeries`` in document order
    (derived series excluded)."""
    data = find_child(chart, "ChartData")
    if data is None:
        return []
    collection = find_child(data, "ChartSeriesCollection")
    if collection is None:
        return []
    return find_children(collection, "ChartSeries")


def derived_series_list(chart: etree._Element) -> list[etree._Element]:
    """``ChartData/ChartDerivedSeriesCollection/ChartDerivedSeries`` elements."""
    data = find_child(chart, "ChartData")
    if data is None:
        return []
    collection = find_child(data, "ChartDerivedSeriesCollection")
    if collection is None:
        return []
    return find_children(collection, "ChartDerivedSeries")


def derived_series_name(derived: etree._Element) -> Optional[str]:
    """The ``Name`` of a ``ChartDerivedSeries``' own ``<ChartSeries>``."""
    inner = find_child(derived, "ChartSeries")
    return inner.get("Name") if inner is not None else None


def derived_source_name(derived: etree._Element) -> Optional[str]:
    """The ``SourceChartSeriesName`` text of a ``ChartDerivedSeries``."""
    node = find_child(derived, "SourceChartSeriesName")
    return node.text.strip() if node is not None and node.text else None


def series_names(chart: etree._Element, include_derived: bool = True) -> list[str]:
    """Every series name in the chart. Regular and derived series share one
    namespace (``SourceChartSeriesName`` resolves against both)."""
    out = [s.get("Name") for s in series_list(chart)]
    if include_derived:
        out.extend(n for n in (derived_series_name(d) for d in derived_series_list(chart)) if n)
    return out


def derived_dependents(chart: etree._Element, source_name: str) -> list[etree._Element]:
    """``ChartDerivedSeries`` computed from ``source_name``, transitively
    (a derived series can itself be the source of another one), in
    document order."""
    derived = derived_series_list(chart)
    wanted = {source_name}
    found: list[etree._Element] = []
    changed = True
    while changed:
        changed = False
        for d in derived:
            if d in found:
                continue
            if derived_source_name(d) in wanted:
                found.append(d)
                name = derived_series_name(d)
                if name:
                    wanted.add(name)
                changed = True
    return [d for d in derived if d in found]


def series_index(chart: etree._Element, series: Union[str, etree._Element]) -> Optional[int]:
    """Position of ``series`` (element or name) in ``ChartSeriesCollection``,
    or None if it is not a regular series of this chart."""
    for i, s in enumerate(series_list(chart)):
        if s is series or s.get("Name") == series:
            return i
    return None


def series_member(
    chart: etree._Element, series: Union[int, str, etree._Element]
) -> Optional[etree._Element]:
    """The leaf series-hierarchy ``ChartMember`` that belongs to ``series``
    (an index, a name or a ``ChartSeries`` element). None when the series is
    unknown or the hierarchy has no leaf at that position."""
    idx = series if isinstance(series, int) else series_index(chart, series)
    if idx is None or idx < 0:
        return None
    leaves = series_leaf_members(chart)
    return leaves[idx] if idx < len(leaves) else None


def member_series(chart: etree._Element, member: etree._Element) -> Optional[etree._Element]:
    """The ``ChartSeries`` that belongs to leaf ``member``, or None."""
    for i, leaf in enumerate(series_leaf_members(chart)):
        if leaf is member:
            all_series = series_list(chart)
            return all_series[i] if i < len(all_series) else None
    return None


def member_ancestors(member: etree._Element) -> list[etree._Element]:
    """``member`` and its ancestor ``ChartMember`` elements, innermost first."""
    out: list[etree._Element] = []
    node: Optional[etree._Element] = member
    while node is not None and node.tag == _CHART_MEMBER:
        out.append(node)
        parent = node.getparent()  # <ChartMembers>
        node = parent.getparent() if parent is not None else None
    return out


def member_groups(member: etree._Element) -> list[etree._Element]:
    """The ``<Group>`` elements that apply to leaf ``member`` (its own and
    its ancestors'), innermost first."""
    out: list[etree._Element] = []
    for m in member_ancestors(member):
        group = find_child(m, "Group")
        if group is not None:
            out.append(group)
    return out


def data_point_count(series: etree._Element) -> int:
    """Number of ``ChartDataPoints/ChartDataPoint`` in a ``ChartSeries``."""
    points = find_child(series, "ChartDataPoints")
    if points is None:
        return 0
    return len(find_children(points, "ChartDataPoint"))


def hierarchy_problems(chart: etree._Element) -> list[str]:
    """Human-readable breaks of the positional series/category invariant;
    empty when the chart is consistent."""
    problems: list[str] = []
    all_series = series_list(chart)
    leaves = series_leaf_members(chart)
    if len(all_series) != len(leaves):
        problems.append(
            f"{len(all_series)} ChartSeries but {len(leaves)} leaf ChartMember(s) "
            "in ChartSeriesHierarchy"
        )
    categories = len(category_leaf_members(chart))
    for s in all_series:
        points = data_point_count(s)
        if points != categories:
            problems.append(
                f"ChartSeries {s.get('Name')!r} has {points} ChartDataPoint(s) but the "
                f"category hierarchy has {categories} leaf member(s)"
            )
    return problems


__all__ = [
    "category_leaf_members",
    "child_members",
    "data_point_count",
    "derived_dependents",
    "derived_series_list",
    "derived_series_name",
    "derived_source_name",
    "hierarchy_problems",
    "member_ancestors",
    "member_groups",
    "member_series",
    "series_hierarchy_members",
    "series_index",
    "series_leaf_members",
    "series_list",
    "series_member",
    "series_names",
    "subtree_leaves",
]
