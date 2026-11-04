"""``tablix-structure`` — tablix shape errors the XSD cannot see (#44).

The RDL XSD declares ``Tablix``, ``TablixMember``, ``TablixRow`` and
``CellContents`` as unordered unbounded choices, so a tablix whose body,
hierarchies, spans or corner disagree passes ``validate_report`` while
Report Builder refuses to open it. This rule walks every ``<Tablix>`` —
nested ones (inside a cell or a rectangle) included — and reports:

* **error** — everything :func:`pbirb_mcp.core.tablix_headers.shape_findings`
  finds: column-hierarchy leaves ≠ ``TablixColumn`` count, row-hierarchy
  leaves ≠ ``TablixRow`` count, a body row whose cell count ≠ columns,
  a ColSpan past the right edge, a span-covered cell with contents, an
  empty ``<TablixCell/>`` no span covers, ``RowSpan>1`` in the body,
  unequal header depth along the leaf paths of an axis, and a corner that
  exists without both header depths or whose rows / cells don't match
  them.
* **warning** — both header depths are non-zero but there is no
  ``<TablixCorner>``; ``<KeepWithGroup>`` on a hierarchy member that has a
  ``<Group>`` (it only means something on a static member next to a
  group).
* **info** — the tablix ``<Width>`` differs from the row-header widths
  plus the body column widths (Report Builder recomputes it on open).

Each location is the tablix's ancestor chain plus the step inside it,
with 0-based indices, e.g.
``Tablix[Name='MainTable']>TablixBody>TablixRow[0]>TablixCell[2]``.

Report-wide name uniqueness and ``ToggleItem`` targets are separate
rules (``duplicate-name``, ``dangling-toggle-item``).
"""

from __future__ import annotations

from typing import Optional

from lxml import etree

from pbirb_mcp.core import tablix_headers, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import find_child, find_children, q
from pbirb_mcp.ops.lint import Issue, _ancestor_chain

RULE = "tablix-structure"

# Tolerance for the Width check: Report Builder rounds sizes to 5 decimals
# in mixed units, so tiny differences are noise.
_WIDTH_TOLERANCE_IN = 0.01

_SHAPE_SUGGESTION = (
    "fix the XML so hierarchy leaves, body columns/rows, cells and spans agree; the "
    "tablix tools (add_tablix_column / remove_tablix_column, add_static_row, "
    "set_cell_span, add_row_group / add_column_group, ...) keep them in step"
)
_CORNER_SUGGESTION = (
    "set_tablix_corner creates or resizes the corner to column-header depth x row-header depth"
)


def _location(tablix: etree._Element, path: str) -> str:
    base = _ancestor_chain(tablix)
    return f"{base}>{path}" if path else base


def _members(members_root: Optional[etree._Element]):
    """Every ``TablixMember`` under ``members_root``, depth-first, without
    descending into nested tablixes (their members belong to them)."""
    if members_root is None:
        return
    for member in find_children(members_root, "TablixMember"):
        yield member
        yield from _members(find_child(member, "TablixMembers"))


def _keep_with_group_issues(tablix: etree._Element) -> list[Issue]:
    issues: list[Issue] = []
    name = tablix.get("Name")
    for hierarchy_tag in ("TablixColumnHierarchy", "TablixRowHierarchy"):
        hierarchy = find_child(tablix, hierarchy_tag)
        if hierarchy is None:
            continue
        for member in _members(find_child(hierarchy, "TablixMembers")):
            keep = find_child(member, "KeepWithGroup")
            group = find_child(member, "Group")
            if keep is None or group is None:
                continue
            issues.append(
                {
                    "severity": "warning",
                    "rule": RULE,
                    "location": _ancestor_chain(group) + ">KeepWithGroup",
                    "message": (
                        f"tablix {name!r}: member of group {group.get('Name')!r} has "
                        f"KeepWithGroup={(keep.text or '').strip()!r}; KeepWithGroup "
                        "belongs on a static member (a group header / footer row) "
                        "next to a group, not on the group itself"
                    ),
                    "suggestion": "remove <KeepWithGroup> from the grouped member",
                }
            )
    return issues


def _size_in(node: Optional[etree._Element]) -> Optional[float]:
    if node is None:
        return None
    try:
        return units.to_inches((node.text or "").strip())
    except ValueError:
        return None


def _row_header_width(tablix: etree._Element) -> Optional[float]:
    """Width of the row-header columns: the widest sum of row-hierarchy
    ``TablixHeader/Size`` along a leaf path (0 without row headers).
    None when a size can't be read."""
    from pbirb_mcp.core.tablix_shape import row_leaves

    hierarchy = find_child(tablix, "TablixRowHierarchy")
    widest = 0.0
    for leaf in row_leaves(tablix):
        total = 0.0
        node: Optional[etree._Element] = leaf
        while node is not None and node is not hierarchy:
            if node.tag == q("TablixMember"):
                header = find_child(node, "TablixHeader")
                if header is not None:
                    size = _size_in(find_child(header, "Size"))
                    if size is None:
                        return None
                    total += size
            node = node.getparent()
        widest = max(widest, total)
    return widest


def _width_issue(tablix: etree._Element) -> Optional[Issue]:
    width_node = find_child(tablix, "Width")
    width = _size_in(width_node)
    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    if width is None or cols_root is None:
        return None
    total = 0.0
    for column in find_children(cols_root, "TablixColumn"):
        size = _size_in(find_child(column, "Width"))
        if size is None:
            return None
        total += size
    headers = _row_header_width(tablix)
    if headers is None:
        return None
    expected = total + headers
    if abs(expected - width) <= _WIDTH_TOLERANCE_IN:
        return None
    expected_text = units.format_size(expected)
    return {
        "severity": "info",
        "rule": RULE,
        "location": _location(tablix, "Width"),
        "message": (
            f"tablix {tablix.get('Name')!r}: Width is {(width_node.text or '').strip()} but its "
            f"row headers and columns add up to {expected_text}; Report Builder "
            "recomputes the width on open"
        ),
        "suggestion": f"set the tablix Width to {expected_text}",
    }


def _rule_tablix_structure(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    for tablix in doc.root.iter(q("Tablix")):
        name = tablix.get("Name")
        for problem in tablix_headers.shape_findings(tablix):
            corner = "Corner" in problem.message
            issues.append(
                {
                    "severity": problem.severity,
                    "rule": RULE,
                    "location": _location(tablix, problem.path),
                    "message": f"tablix {name!r}: {problem.message}",
                    "suggestion": _CORNER_SUGGESTION if corner else _SHAPE_SUGGESTION,
                }
            )
        issues.extend(_keep_with_group_issues(tablix))
        width = _width_issue(tablix)
        if width is not None:
            issues.append(width)
    return issues


RULES = {RULE: _rule_tablix_structure}
