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


__all__ = [
    "set_group_page_break",
    "set_keep_together",
    "set_keep_with_group",
    "set_repeat_on_new_page",
]
