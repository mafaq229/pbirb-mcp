"""Pagination + layout-container tools (Phases 10–11).

Phase 10 ships group-level pagination controls:

* :func:`set_group_page_break` — write
  ``<TablixMember>/<Group>/<PageBreak>/<BreakLocation>``. Controls how
  a tablix group breaks across pages (Start / End / StartAndEnd /
  Between / None). Setting ``location='None'`` removes the
  ``<PageBreak>`` element entirely (canonical "no page break" shape).
* :func:`set_repeat_on_new_page` — write
  ``<TablixMember>/<RepeatOnNewPage>``. Controls whether a group's
  TablixMember (typically the header row) repeats when the group
  spans pages.

Both target a ``Group`` resolved via ``resolve_group(tablix_name,
group_name)`` and walk one level up to the ``TablixMember`` parent for
the repeat-on-new-page case.

Phase 11 will add the layout-container constructors (``add_rectangle``,
``add_list``, ``add_line``) in this same module.
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_group
from pbirb_mcp.core.xpath import XPATH_NS, find_child, q
from pbirb_mcp.ops.body import _ensure_body_report_items, _names_in, _resolve_body
from pbirb_mcp.ops.page import _SIZE_RE, _TO_INCHES
from pbirb_mcp.ops.tablix import _insert_member_child


# ---- set_group_page_break -----------------------------------------------


_VALID_BREAK_LOCATIONS = ("None", "Start", "End", "StartAndEnd", "Between")


# Per RDL 2016 XSD, Group child order (subset we care about):
#   GroupExpressions, PageBreak, Filters, SortExpressions, Parent,
#   DataElementName, ...
# PageBreak must come right after GroupExpressions and before Filters.
_GROUP_CHILD_ORDER = (
    "GroupExpressions",
    "PageBreak",
    "Filters",
    "SortExpressions",
    "Parent",
    "DataElementName",
    "DataElementOutput",
    "DataCollectionName",
)


def _insert_group_child(group: etree._Element, new_child: etree._Element) -> None:
    """Insert ``new_child`` into ``group`` respecting the schema-required
    sibling order. Replaces any existing element of the same local name.
    """
    new_local = etree.QName(new_child).localname
    existing = find_child(group, new_local)
    if existing is not None:
        group.replace(existing, new_child)
        return
    new_idx = _GROUP_CHILD_ORDER.index(new_local)
    for i, child in enumerate(list(group)):
        local = etree.QName(child).localname
        if local in _GROUP_CHILD_ORDER and _GROUP_CHILD_ORDER.index(local) > new_idx:
            group.insert(i, new_child)
            return
    group.append(new_child)


def set_group_page_break(
    path: str,
    tablix_name: str,
    group_name: str,
    location: str,
) -> dict[str, Any]:
    """Set the ``<BreakLocation>`` of a tablix group's page-break rule.

    ``location`` ∈ ``{None, Start, End, StartAndEnd, Between}``.
    Setting ``location='None'`` removes the ``<PageBreak>`` element
    entirely — that's the canonical RDL representation of "no page
    break", and keeps the file clean for round-trip identity.

    Returns ``{tablix, group, kind: 'Group', location, changed: bool}``.
    Idempotent: setting the same value twice is a no-op (no save).
    """
    if location not in _VALID_BREAK_LOCATIONS:
        raise ValueError(
            f"unknown BreakLocation {location!r}; valid values: "
            f"{list(_VALID_BREAK_LOCATIONS)}"
        )

    doc = RDLDocument.open(path)
    group = resolve_group(doc, tablix_name, group_name)

    existing = find_child(group, "PageBreak")

    if location == "None":
        # Canonical removal.
        if existing is None:
            return {
                "tablix": tablix_name,
                "group": group_name,
                "kind": "Group",
                "location": "None",
                "changed": False,
            }
        group.remove(existing)
        doc.save()
        return {
            "tablix": tablix_name,
            "group": group_name,
            "kind": "Group",
            "location": "None",
            "changed": True,
        }

    # Set or replace. Idempotent against current value.
    if existing is not None:
        bl = find_child(existing, "BreakLocation")
        if bl is not None and (bl.text or "").strip() == location:
            return {
                "tablix": tablix_name,
                "group": group_name,
                "kind": "Group",
                "location": location,
                "changed": False,
            }

    page_break = etree.Element(q("PageBreak"))
    etree.SubElement(page_break, q("BreakLocation")).text = location
    _insert_group_child(group, page_break)

    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "kind": "Group",
        "location": location,
        "changed": True,
    }


# ---- set_repeat_on_new_page ---------------------------------------------


def set_repeat_on_new_page(
    path: str,
    tablix_name: str,
    group_name: str,
    repeat: bool,
) -> dict[str, Any]:
    """Set ``<TablixMember>/<RepeatOnNewPage>`` on the member that wraps
    the named group.

    Most common use: keep a group header row visible at the top of every
    page the group spans. Setting ``repeat=False`` removes the element
    (False is the default; explicit removal keeps round-trip clean).

    Returns ``{tablix, group, kind: 'TablixMember', repeat, changed: bool}``.
    Idempotent.
    """
    doc = RDLDocument.open(path)
    group = resolve_group(doc, tablix_name, group_name)
    member = group.getparent()
    # resolve_group returns a Group; its parent is always a TablixMember.
    # Defensive sanity-check anyway.
    if member is None or etree.QName(member.tag).localname != "TablixMember":
        raise ValueError(
            f"Group {group_name!r} is not enclosed in a <TablixMember>"
        )

    existing = find_child(member, "RepeatOnNewPage")
    current = (existing.text or "").strip().lower() if existing is not None else ""
    desired = "true" if repeat else "false"

    if not repeat:
        if existing is None:
            return _result(tablix_name, group_name, repeat, changed=False)
        member.remove(existing)
        doc.save()
        return _result(tablix_name, group_name, repeat, changed=True)

    if current == desired:
        return _result(tablix_name, group_name, repeat, changed=False)

    new_node = etree.Element(q("RepeatOnNewPage"))
    new_node.text = desired
    _insert_member_child(member, new_node)

    doc.save()
    return _result(tablix_name, group_name, repeat, changed=True)


def _result(tablix_name: str, group_name: str, repeat: bool, changed: bool) -> dict:
    return {
        "tablix": tablix_name,
        "group": group_name,
        "kind": "TablixMember",
        "repeat": repeat,
        "changed": changed,
    }


# ---- set_keep_together (Phase 10 commit 40) -----------------------------


# Items that legitimately accept <KeepTogether> per RDL 2016 XSD.
# Image and Line don't; calling set_keep_together on them is a hard error.
_KEEP_TOGETHER_SUPPORTED = ("Tablix", "Rectangle", "Chart", "Textbox", "Map", "Gauge")

# Element local-names that come AFTER <KeepTogether> in a data region's
# child list per RDL 2016. Inserting immediately before the FIRST one of
# these places <KeepTogether> in the right slot. Used only for non-Textbox
# items — Textbox has its own ordered helper in styling.py.
_DATA_REGION_TAGS_AFTER_KEEP_TOGETHER = (
    "DataInstanceName",
    "DataInstanceElementOutput",
    "TablixCorner",
    "TablixBody",
    "TablixColumnHierarchy",
    "TablixRowHierarchy",
    "ChartCategoryHierarchy",
    "ChartSeriesHierarchy",
    "ChartArea",
    "ChartLegend",
    "ChartTitle",
    "ChartData",
    "ReportItems",
    "DataSetName",
    "Top",
    "Left",
    "Height",
    "Width",
    "ZIndex",
    "Style",
)


def _resolve_named_report_item(doc: RDLDocument, name: str) -> etree._Element:
    """Find any ``<ReportItems>/<*Name=name>`` in the document. Returns
    the first match (names are document-unique by RDL convention)."""
    matches = doc.root.xpath(
        ".//r:ReportItems/r:*[@Name=$n]",
        namespaces=XPATH_NS,
        n=name,
    )
    if not matches:
        raise ElementNotFoundError(f"named ReportItem {name!r} not found")
    return matches[0]


def set_keep_together(path: str, name: str, keep: bool) -> dict[str, Any]:
    """Set ``<KeepTogether>`` on a named Tablix / Rectangle / Chart /
    Textbox / Map / Gauge.

    Tells the renderer "don't split this across pages if you can help
    it". Note RDL semantics: ``KeepTogether`` is best-effort — when an
    item is genuinely larger than a page, it's still split. Setting
    ``keep=False`` removes the element (False is the default).

    Refuses for Image / Line / Subreport / other items where
    ``KeepTogether`` is not in the RDL XSD.

    Returns ``{name, kind, keep, changed}``. Idempotent.
    """
    doc = RDLDocument.open(path)
    target = _resolve_named_report_item(doc, name)
    local = etree.QName(target.tag).localname

    if local not in _KEEP_TOGETHER_SUPPORTED:
        raise ValueError(
            f"{local} {name!r} does not support <KeepTogether>; "
            f"valid kinds: {list(_KEEP_TOGETHER_SUPPORTED)}"
        )

    existing = find_child(target, "KeepTogether")
    desired = "true" if keep else "false"
    current = (existing.text or "").strip().lower() if existing is not None else ""

    if not keep:
        if existing is None:
            return {"name": name, "kind": local, "keep": keep, "changed": False}
        target.remove(existing)
        doc.save()
        return {"name": name, "kind": local, "keep": keep, "changed": True}

    if current == desired:
        return {"name": name, "kind": local, "keep": keep, "changed": False}

    if existing is not None:
        existing.text = desired
        doc.save()
        return {"name": name, "kind": local, "keep": keep, "changed": True}

    # Insert fresh node in the right schema position.
    if local == "Textbox":
        # Reuse the established Textbox-direct-child helper from styling.
        from pbirb_mcp.ops.styling import _set_or_create_textbox_direct_child

        _set_or_create_textbox_direct_child(target, "KeepTogether", desired)
    else:
        new_node = etree.Element(q("KeepTogether"))
        new_node.text = desired
        for child in target:
            child_local = etree.QName(child.tag).localname
            if child_local in _DATA_REGION_TAGS_AFTER_KEEP_TOGETHER:
                child.addprevious(new_node)
                break
        else:
            target.append(new_node)

    doc.save()
    return {"name": name, "kind": local, "keep": keep, "changed": True}


# ---- set_keep_with_group (Phase 10 commit 40) ---------------------------


_VALID_KEEP_WITH_GROUP = ("None", "Before", "After")


def set_keep_with_group(
    path: str,
    tablix_name: str,
    group_name: str,
    value: str,
) -> dict[str, Any]:
    """Set ``<TablixMember>/<KeepWithGroup>`` on the member that wraps
    the named group.

    ``value`` ∈ ``{None, Before, After}``. ``None`` removes the element
    (the canonical "no preference" representation). The typical use is
    a column-header row's TablixMember setting ``After`` so the header
    stays glued to the data rows that follow it.

    Returns ``{tablix, group, kind: 'TablixMember', value, changed}``.
    Idempotent.
    """
    if value not in _VALID_KEEP_WITH_GROUP:
        raise ValueError(
            f"unknown KeepWithGroup value {value!r}; valid values: "
            f"{list(_VALID_KEEP_WITH_GROUP)}"
        )

    doc = RDLDocument.open(path)
    group = resolve_group(doc, tablix_name, group_name)
    member = group.getparent()
    if member is None or etree.QName(member.tag).localname != "TablixMember":
        raise ValueError(
            f"Group {group_name!r} is not enclosed in a <TablixMember>"
        )

    existing = find_child(member, "KeepWithGroup")
    current = (existing.text or "").strip() if existing is not None else ""

    if value == "None":
        if existing is None:
            return _kwg_result(tablix_name, group_name, value, changed=False)
        member.remove(existing)
        doc.save()
        return _kwg_result(tablix_name, group_name, value, changed=True)

    if current == value:
        return _kwg_result(tablix_name, group_name, value, changed=False)

    new_node = etree.Element(q("KeepWithGroup"))
    new_node.text = value
    _insert_member_child(member, new_node)

    doc.save()
    return _kwg_result(tablix_name, group_name, value, changed=True)


def _kwg_result(tablix: str, group: str, value: str, changed: bool) -> dict:
    return {
        "tablix": tablix,
        "group": group,
        "kind": "TablixMember",
        "value": value,
        "changed": changed,
    }


# ---- add_rectangle (Phase 11 commit 41) ---------------------------------


def _parse_size(s: str) -> tuple[float, str]:
    """Parse an RDL size string into ``(value, unit)``. Reuses the regex
    + unit set from :mod:`pbirb_mcp.ops.page`."""
    m = _SIZE_RE.match(s or "")
    if not m:
        raise ValueError(
            f"invalid RDL size {s!r}; expected '<number><unit>' "
            f"with unit in (in, cm, mm, pt, pc)"
        )
    return float(m.group(1)), m.group(2)


def _format_size(value: float, unit: str) -> str:
    """Render ``value`` with ``unit``. Integer values render without a
    decimal point to match Report Builder's emitted shape."""
    if value == int(value):
        return f"{int(value)}{unit}"
    # Up to 5 decimal places, trailing zeros stripped — same convention
    # Report Builder uses for fractional inches.
    return f"{value:.5f}".rstrip("0").rstrip(".") + unit


def _coord_subtract(child_coord: str, container_coord: str) -> str:
    """Return ``child - container`` as an RDL size string. When both
    use the same unit, arithmetic stays in that unit; otherwise both
    are converted to the container's unit before subtracting.
    """
    cv, cu = _parse_size(child_coord)
    rv, ru = _parse_size(container_coord)
    if cu == ru:
        return _format_size(cv - rv, cu)
    cv_in = cv * _TO_INCHES[cu]
    rv_in = rv * _TO_INCHES[ru]
    return _format_size((cv_in - rv_in) / _TO_INCHES[ru], ru)


def add_rectangle(
    path: str,
    name: str,
    top: str,
    left: str,
    width: str,
    height: str,
    contained_items: list[str] | None = None,
) -> dict[str, Any]:
    """Add a ``<Rectangle Name=name>`` to ``<Body>/<ReportItems>``.

    When ``contained_items`` is empty (or not supplied), the rectangle
    is created empty — no ``<ReportItems>`` child. Callers can move
    items in later or use this as a visual frame.

    When ``contained_items`` lists one or more names of existing body
    items (Tablix / Textbox / Image / Chart / etc.), each is **moved**
    from ``<Body>/<ReportItems>`` into the rectangle's
    ``<ReportItems>``, and its ``<Top>`` / ``<Left>`` are recalculated
    so the on-screen position is preserved (rectangle-local coords =
    body-coords − rectangle-coords).

    Refuses if any named item isn't found in ``<Body>/<ReportItems>``,
    or if a name collision exists at the body level.

    Returns ``{name, kind: 'Rectangle', moved: list[str]}``.
    """
    contained_items = list(contained_items or [])

    doc = RDLDocument.open(path)
    body = _resolve_body(doc)
    items = _ensure_body_report_items(body)

    if name in _names_in(items):
        raise ValueError(f"body item named {name!r} already exists; pick a unique name")

    # Resolve every contained item up front — fail before mutating the tree.
    if contained_items:
        present = _names_in(items)
        missing = [n for n in contained_items if n not in present]
        if missing:
            raise ElementNotFoundError(
                f"cannot move into rectangle — items not in <Body>/<ReportItems>: {missing}"
            )

    rect = etree.Element(q("Rectangle"), Name=name)

    if contained_items:
        rect_items = etree.SubElement(rect, q("ReportItems"))
        # Find each child by name, recompute its position, move it.
        for item_name in contained_items:
            for child in list(items):
                if child.get("Name") != item_name:
                    continue
                child_top = find_child(child, "Top")
                child_left = find_child(child, "Left")
                if child_top is not None and child_top.text:
                    child_top.text = _coord_subtract(child_top.text, top)
                if child_left is not None and child_left.text:
                    child_left.text = _coord_subtract(child_left.text, left)
                items.remove(child)
                rect_items.append(child)
                break

    # Per RDL XSD Rectangle child order — positional/style at the end.
    etree.SubElement(rect, q("KeepTogether")).text = "true"
    etree.SubElement(rect, q("Top")).text = top
    etree.SubElement(rect, q("Left")).text = left
    etree.SubElement(rect, q("Height")).text = height
    etree.SubElement(rect, q("Width")).text = width
    style = etree.SubElement(rect, q("Style"))
    border = etree.SubElement(style, q("Border"))
    etree.SubElement(border, q("Style")).text = "None"

    items.append(rect)
    doc.save()
    return {
        "name": name,
        "kind": "Rectangle",
        "moved": list(contained_items),
    }


__all__ = [
    "add_rectangle",
    "set_group_page_break",
    "set_keep_together",
    "set_keep_with_group",
    "set_repeat_on_new_page",
]
