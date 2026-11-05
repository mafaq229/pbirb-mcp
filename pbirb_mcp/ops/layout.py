"""Pagination + layout-container tools (Phases 10–11).

Phase 10 ships group-level pagination controls:

* :func:`set_group_page_break` — write
  ``<TablixMember>/<Group>/<PageBreak>/<BreakLocation>``. Controls how
  a tablix group breaks across pages (Start / End / StartAndEnd /
  Between / None). Setting ``location='None'`` removes the
  ``<PageBreak>`` element entirely (canonical "no page break" shape).
* :func:`set_repeat_on_new_page` — write
  ``<TablixMember>/<RepeatOnNewPage>``. Controls whether a member
  (typically a header row) repeats when the data spans pages.

``set_group_page_break`` targets a ``Group`` resolved via
``resolve_group(tablix_name, group_name)``. The member tools
(``set_repeat_on_new_page``, ``set_keep_with_group``) take either
``group_name`` (the group's member) or ``member`` — a row index or a
``style_tablix_row`` role, which reaches static rows (no ``<Group>``),
#67; ``set_repeat_header_rows`` flags every static row above the data.

Phase 11 will add the layout-container constructors (``add_rectangle``,
``add_list``, ``add_line``) in this same module.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names, tablix_spans, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_dataset, resolve_group, resolve_tablix
from pbirb_mcp.core.tablix_headers import COLUMN, header_depth
from pbirb_mcp.core.tablix_shape import leaf_row_index, owner_member, row_leaf, subtree_leaves
from pbirb_mcp.core.xpath import XPATH_NS, find_child, find_children, q
from pbirb_mcp.ops.body import _ensure_body_report_items, _resolve_body
from pbirb_mcp.ops.containers import fit_warnings, target_report_items
from pbirb_mcp.ops.styling import _resolve_row_index
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
            f"unknown BreakLocation {location!r}; valid values: {list(_VALID_BREAK_LOCATIONS)}"
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


# ---- member addressing (#67) --------------------------------------------


def _static_header_members(tablix: etree._Element) -> list[etree._Element]:
    """Top-level row members before the first one holding a ``<Group>``
    (dynamic or detail) — the static rows above the data, e.g. the
    column-header row (and a title row above it)."""
    hierarchy = find_child(tablix, "TablixRowHierarchy")
    members_root = find_child(hierarchy, "TablixMembers") if hierarchy is not None else None
    out: list[etree._Element] = []
    for member in find_children(members_root, "TablixMember") if members_root is not None else []:
        if next(member.iter(q("Group")), None) is not None:
            break
        out.append(member)
    return out


def _row_member(tablix: etree._Element, member: Any) -> tuple[etree._Element, int]:
    """The row-hierarchy ``TablixMember`` a ``member`` selector names and
    its body row: a row index (int or digit string) or a
    ``style_tablix_row`` role (``header``, ``details``, ``<group>_header``,
    ``<group>_footer``). ``header`` falls back to the first static row
    above the data when no row is marked ``KeepWithGroup=After`` yet.

    From the row's leaf it climbs through static wrappers that hold only
    that row, so the flag lands on the member next to the group."""
    try:
        index = _resolve_row_index(tablix, member)
    except ElementNotFoundError:
        headers = _static_header_members(tablix) if member == "header" else []
        if not headers:
            raise
        index = leaf_row_index(tablix, subtree_leaves(headers[0])[0])
    leaf = row_leaf(tablix, index)
    if leaf is None:
        raise ElementNotFoundError(
            f"body row {index} of tablix {tablix.get('Name')!r} has no row-hierarchy member"
        )
    while True:
        owner = owner_member(leaf)
        if (
            owner is None
            or find_child(owner, "Group") is not None
            or len(find_children(leaf.getparent(), "TablixMember")) > 1
        ):
            break
        leaf = owner
    return leaf, index


def _target_member(
    doc: RDLDocument,
    tablix_name: str,
    group_name: Optional[str],
    member: Any,
) -> tuple[etree._Element, dict[str, Any]]:
    """Resolve exactly one of ``group_name`` / ``member`` to a
    ``TablixMember``; also returns the result keys naming it."""
    if (group_name is None) == (member is None):
        raise ValueError("pass exactly one of group_name (a group's member) or member (a row)")
    if member is not None:
        tablix = resolve_tablix(doc, tablix_name)
        target, index = _row_member(tablix, member)
        group = find_child(target, "Group")
        return target, {
            "group": group.get("Name") if group is not None else None,
            "member": member,
            "row_index": index,
        }
    group = resolve_group(doc, tablix_name, group_name)
    target = group.getparent()
    # resolve_group returns a Group; its parent is always a TablixMember.
    if target is None or etree.QName(target.tag).localname != "TablixMember":
        raise ValueError(f"Group {group_name!r} is not enclosed in a <TablixMember>")
    return target, {"group": group_name}


def _set_member_flag(member: etree._Element, local: str, value: Optional[str]) -> bool:
    """Write ``<local>value</local>`` on ``member`` in schema order
    (``None`` removes it); True iff anything changed."""
    existing = find_child(member, local)
    if value is None:
        if existing is None:
            return False
        tablix_spans.detach(existing)
        return True
    current = (existing.text or "").strip() if existing is not None else None
    # xsd:boolean flags compare case-insensitively ("True" == "true").
    if current == value or (value == "true" and (current or "").lower() == "true"):
        return False
    node = etree.Element(q(local))
    node.text = value
    _insert_member_child(member, node)
    return True


# ---- set_repeat_on_new_page ---------------------------------------------


def set_repeat_on_new_page(
    path: str,
    tablix_name: str,
    group_name: Optional[str] = None,
    repeat: bool = True,
    member: Any = None,
) -> dict[str, Any]:
    """Set ``<TablixMember>/<RepeatOnNewPage>`` on one hierarchy member.

    The member is named by exactly one of:

    * ``group_name`` — the member that wraps that group;
    * ``member`` — a row: a body row index or a ``style_tablix_row`` role
      (``"header"``, ``"details"``, ``"<group>_header"``,
      ``"<group>_footer"``). Static rows have no group, so this is how the
      column-header row (``member="header"``) or a group's header row
      (``"<group>_header"``) is reached.

    Most common use: repeat the column-header row at the top of every
    page — ``member="header"`` (see also :func:`set_repeat_header_rows`,
    which also sets ``KeepWithGroup=After``). ``repeat=False`` removes the
    element (False is the default; explicit removal keeps round-trip
    clean).

    Returns ``{tablix, group, kind: 'TablixMember', repeat, changed: bool}``
    plus ``member`` / ``row_index`` for a ``member`` selector. Idempotent.
    """
    doc = RDLDocument.open(path)
    target, label = _target_member(doc, tablix_name, group_name, member)
    changed = _set_member_flag(target, "RepeatOnNewPage", "true" if repeat else None)
    if changed:
        doc.save()
    return {
        "tablix": tablix_name,
        **label,
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
    group_name: Optional[str] = None,
    value: Optional[str] = None,
    member: Any = None,
) -> dict[str, Any]:
    """Set ``<TablixMember>/<KeepWithGroup>`` on a static row's member.

    ``value`` ∈ ``{None, Before, After}``; ``None`` removes the element
    (the canonical "no preference" representation). The typical use is
    the column-header row — ``member="header"``, ``value="After"`` — so
    the header stays glued to the data rows that follow it (``Before``
    for a total row that follows them).

    The member is named by ``member`` (a body row index or a
    ``style_tablix_row`` role, as in :func:`set_repeat_on_new_page`) or
    ``group_name``. RDL defines KeepWithGroup for static members only
    (the ``tablix-structure`` lint rule warns on a grouped one), so a
    member carrying a ``<Group>`` — any ``group_name``, ``"details"`` —
    raises ``ValueError``; only ``value="None"`` is accepted there, to
    clean such a misplaced element up.

    Returns ``{tablix, group, kind: 'TablixMember', value, changed}`` plus
    ``member`` / ``row_index`` for a ``member`` selector. Idempotent.
    """
    if value not in _VALID_KEEP_WITH_GROUP:
        raise ValueError(
            f"unknown KeepWithGroup value {value!r}; valid values: {list(_VALID_KEEP_WITH_GROUP)}"
        )

    doc = RDLDocument.open(path)
    target, label = _target_member(doc, tablix_name, group_name, member)
    group = find_child(target, "Group")
    if group is not None and value != "None":
        raise ValueError(
            f"KeepWithGroup belongs on a static member (a header or total row next to a "
            f"group), not on the member of group {group.get('Name')!r}. Target the row "
            "instead: member='header' (the column-header row), '<group>_header', "
            "'<group>_footer' or a body row index."
        )
    changed = _set_member_flag(target, "KeepWithGroup", None if value == "None" else value)
    if changed:
        doc.save()
    return {
        "tablix": tablix_name,
        **label,
        "kind": "TablixMember",
        "value": value,
        "changed": changed,
    }


# ---- set_repeat_header_rows (#67) ---------------------------------------

# Tablix children in Report Builder's order around the header flags (the
# XSD itself is an unordered choice).
_TABLIX_FLAG_ORDER = (
    "TablixCorner",
    "TablixBody",
    "TablixColumnHierarchy",
    "TablixRowHierarchy",
    "LayoutDirection",
    "GroupsBeforeRowHeaders",
    "RepeatColumnHeaders",
    "RepeatRowHeaders",
    "FixedColumnHeaders",
    "FixedRowHeaders",
)


def _set_tablix_flag(tablix: etree._Element, local: str, value: Optional[bool]) -> bool:
    """Write ``<local>true</local>`` (``value`` True) or remove it
    (False) on the tablix; ``None`` leaves it. True iff changed."""
    if value is None:
        return False
    existing = find_child(tablix, local)
    if not value:
        if existing is None:
            return False
        tablix_spans.detach(existing)
        return True
    if existing is not None:
        if (existing.text or "").strip().lower() == "true":
            return False
        existing.text = "true"
        return True
    node = etree.Element(q(local))
    node.text = "true"
    before = _TABLIX_FLAG_ORDER[: _TABLIX_FLAG_ORDER.index(local)]
    anchor = None
    for child in tablix:
        if isinstance(child.tag, str) and etree.QName(child).localname in before:
            anchor = child
    if anchor is not None:
        anchor.addnext(node)
    else:
        tablix.insert(0, node)
    return True


def set_repeat_header_rows(
    path: str,
    tablix_name: str,
    repeat: bool = True,
    fixed: Optional[bool] = None,
) -> dict[str, Any]:
    """Repeat a tablix's header rows at the top of every page.

    The header rows are the static row members before the first member
    holding a group (the column-header row, and e.g. a title row above
    it). ``repeat=True`` gives each ``RepeatOnNewPage=true`` and
    ``KeepWithGroup=After`` — Report Builder's "repeat header rows" for a
    table; ``repeat=False`` removes ``RepeatOnNewPage`` (KeepWithGroup
    stays). When the tablix has column-header cells (a matrix's
    ``TablixHeader`` rows) its ``RepeatColumnHeaders`` is set / removed
    too.

    ``fixed`` (default: leave alone) also keeps them visible while
    scrolling in the web viewer: ``FixedData`` on the header members and,
    for a matrix, ``FixedColumnHeaders``; ``False`` removes those.

    Refuses (``ValueError``) a tablix with neither static header rows nor
    column-header cells. Returns ``{tablix, kind: 'Tablix', repeat,
    fixed, header_rows, changed}`` — ``header_rows`` the body row
    indices, ``changed`` the elements written or removed. Idempotent.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    headers = _static_header_members(tablix)
    column_headers = header_depth(tablix, COLUMN) > 0
    if not headers and not column_headers:
        raise ValueError(
            f"tablix {tablix_name!r} has no static header rows above its first group "
            "and no column-header cells; nothing to repeat"
        )
    changed: list[str] = []

    def note(flag: bool, what: str) -> None:
        if flag and what not in changed:
            changed.append(what)

    rows: list[int] = []
    for header in headers:
        rows += [leaf_row_index(tablix, leaf) for leaf in subtree_leaves(header)]
        note(
            _set_member_flag(header, "RepeatOnNewPage", "true" if repeat else None),
            "RepeatOnNewPage",
        )
        if repeat:
            note(_set_member_flag(header, "KeepWithGroup", "After"), "KeepWithGroup")
        if fixed is not None:
            note(_set_member_flag(header, "FixedData", "true" if fixed else None), "FixedData")
    if column_headers:
        note(_set_tablix_flag(tablix, "RepeatColumnHeaders", repeat), "RepeatColumnHeaders")
        note(_set_tablix_flag(tablix, "FixedColumnHeaders", fixed), "FixedColumnHeaders")
    if changed:
        doc.save()
    return {
        "tablix": tablix_name,
        "kind": "Tablix",
        "repeat": repeat,
        "fixed": fixed,
        "header_rows": [r for r in rows if r is not None],
        "changed": changed,
    }


# ---- add_rectangle (Phase 11 commit 41) ---------------------------------


def _coord_subtract(child_coord: str, container_coord: str) -> str:
    """Return ``child - container`` as an RDL size string in the
    container's unit (which is also the child's unit when they match)."""
    unit = units.parse_size(container_coord).unit
    return units.format_size(units.to_inches(child_coord) - units.to_inches(container_coord), unit)


# Slack for float round-off when comparing positions in inches (~0.00003mm).
_CONTAIN_EPS_IN = 1e-6


def _item_size_in(item: etree._Element, local: str) -> float:
    """``item``'s ``<local>`` size in inches; a missing value counts as 0."""
    node = find_child(item, local)
    if node is None or not (node.text or "").strip():
        return 0.0
    try:
        return units.to_inches(node.text)
    except ValueError:
        raise ValueError(
            f"cannot place {item.get('Name')!r} in a rectangle: its <{local}> "
            f"{node.text!r} is not a valid RDL size"
        ) from None


def _item_bbox_in(item: etree._Element) -> tuple[float, float, float, float]:
    """``(top, left, bottom, right)`` of a body item in inches.

    A missing ``Top`` / ``Left`` counts as 0 (the RDL default). A
    ``<Line>``'s ``Width`` / ``Height`` are the vector from its start
    point and may be negative, so its box is the span of both ends.
    """
    top = _item_size_in(item, "Top")
    left = _item_size_in(item, "Left")
    bottom = top + _item_size_in(item, "Height")
    right = left + _item_size_in(item, "Width")
    return (min(top, bottom), min(left, right), max(top, bottom), max(left, right))


def _fmt_in(inches: float) -> str:
    return units.format_size(inches, "in")


def add_rectangle(
    path: str,
    name: str,
    top: str,
    left: str,
    width: str,
    height: str,
    contained_items: list[str] | None = None,
    fit: bool = False,
    container: Optional[str] = None,
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
    body-coords − rectangle-coords, written in the rectangle's unit).
    Repeated names are moved once.

    Every moved item must lie inside the rectangle — otherwise its
    rectangle-local ``Top`` / ``Left`` would be negative, or it would
    overflow the rectangle, and Report Builder rejects the file. A
    misfit is refused, naming each offending item and by how much it
    overflows. With ``fit=True`` the rectangle instead grows to the
    union of the requested box and its children's boxes.

    Refuses if any named item isn't found in ``<Body>/<ReportItems>``,
    or if a name collision exists at the body level. Nothing is
    written on refusal.

    With ``container`` (a Rectangle in the body, a List's ``<name>_Rect``
    included) the rectangle is added inside that one instead: top / left
    are relative to it and ``contained_items`` must be its items (#83).

    Returns ``{name, kind: 'Rectangle', moved: list[str]}``; with
    ``fit=True`` also ``{fitted: bool, top, left, width, height}`` —
    the rectangle's final box and whether it had to grow.
    """
    names.validate_identifier(name, "rectangle")
    top, left, width, height = units.box(top, left, width, height)
    # De-duplicate while keeping the caller's order.
    contained_items = list(dict.fromkeys(contained_items or []))

    doc = RDLDocument.open(path)
    names.ensure_unique(doc, name)
    items = target_report_items(doc, container)

    # Resolve every contained item up front — fail before mutating the tree.
    children: list[etree._Element] = []
    if contained_items:
        by_name = {el.get("Name"): el for el in list(items) if el.get("Name") is not None}
        missing = [n for n in contained_items if n not in by_name]
        if missing:
            where = f"rectangle {container!r}" if container else "<Body>/<ReportItems>"
            raise ElementNotFoundError(
                f"cannot move into rectangle — items not in {where}: {missing}"
            )
        children = [by_name[n] for n in contained_items]

    # ---- containment (#43) ---------------------------------------------
    r_top, r_left = units.to_inches(top), units.to_inches(left)
    r_bottom = r_top + units.to_inches(height)
    r_right = r_left + units.to_inches(width)
    boxes = [(child.get("Name"), _item_bbox_in(child)) for child in children]
    fitted = False
    if boxes:
        u_top = min([r_top] + [b[0] for _, b in boxes])
        u_left = min([r_left] + [b[1] for _, b in boxes])
        u_bottom = max([r_bottom] + [b[2] for _, b in boxes])
        u_right = max([r_right] + [b[3] for _, b in boxes])
        grows = (
            u_top < r_top - _CONTAIN_EPS_IN
            or u_left < r_left - _CONTAIN_EPS_IN
            or u_bottom > r_bottom + _CONTAIN_EPS_IN
            or u_right > r_right + _CONTAIN_EPS_IN
        )
        if grows and not fit:
            problems = []
            for item_name, (b_top, b_left, b_bottom, b_right) in boxes:
                over = []
                for amount, side in (
                    (r_top - b_top, "above"),
                    (r_left - b_left, "left of"),
                    (b_bottom - r_bottom, "below"),
                    (b_right - r_right, "right of"),
                ):
                    if amount > _CONTAIN_EPS_IN:
                        over.append(f"{_fmt_in(amount)} {side}")
                if over:
                    problems.append(f"{item_name!r} extends {', '.join(over)} the rectangle")
            raise ValueError(
                f"rectangle {name!r} (top {top}, left {left}, {width} x {height}) does "
                f"not contain every item: {'; '.join(problems)}. Enlarge or move the "
                "rectangle, or pass fit=true to grow it to its children."
            )
        if grows:
            fitted = True
            # Re-render only the edges that moved, in the caller's units.
            if u_top < r_top - _CONTAIN_EPS_IN:
                top = units.format_size(u_top, units.parse_size(top).unit)
            if u_left < r_left - _CONTAIN_EPS_IN:
                left = units.format_size(u_left, units.parse_size(left).unit)
            r_top, r_left = units.to_inches(top), units.to_inches(left)
            if u_bottom - r_top > units.to_inches(height) + _CONTAIN_EPS_IN:
                height = units.format_size(u_bottom - r_top, units.parse_size(height).unit)
            if u_right - r_left > units.to_inches(width) + _CONTAIN_EPS_IN:
                width = units.format_size(u_right - r_left, units.parse_size(width).unit)

    rect = etree.Element(q("Rectangle"), Name=name)

    if children:
        rect_items = etree.SubElement(rect, q("ReportItems"))
        for child in children:
            child_top = find_child(child, "Top")
            child_left = find_child(child, "Left")
            if child_top is not None and child_top.text:
                child_top.text = _coord_subtract(child_top.text, top)
            if child_left is not None and child_left.text:
                child_left.text = _coord_subtract(child_left.text, left)
            items.remove(child)
            rect_items.append(child)

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
    result: dict[str, Any] = {
        "name": name,
        "kind": "Rectangle",
        "moved": list(contained_items),
    }
    if fit:
        result.update(fitted=fitted, top=top, left=left, width=width, height=height)
    return _in_container(result, rect, container)


def _in_container(
    result: dict[str, Any], item: etree._Element, container: Optional[str]
) -> dict[str, Any]:
    """Add ``container`` (and overflow ``warnings``) to an add tool's result."""
    if container is None:
        return result
    result["container"] = container
    warnings = fit_warnings(item)
    if warnings:
        result["warnings"] = warnings
    return result


# ---- add_list (Phase 11 commit 42) --------------------------------------


def add_list(
    path: str,
    name: str,
    dataset_name: str,
    top: str,
    left: str,
    width: str,
    height: str,
) -> dict[str, Any]:
    """Add a List (single-cell repeating Tablix) bound to ``dataset_name``.

    RDL doesn't have a distinct ``<List>`` element — what Report Builder
    surfaces as a List is a Tablix with one column, one detail row, and
    a Rectangle inside the single cell. Items placed inside the rectangle
    repeat once per dataset row: fill it with ``container=<rectangle>`` on
    ``add_body_textbox`` / ``add_body_image`` / ``add_line`` /
    ``add_rectangle`` / ``insert_*_from_template``, or move existing items
    in with ``move_report_item(name, to=<rectangle>)`` (#83). Their top /
    left are relative to the cell, which is ``width`` x ``height``.

    Returns ``{name, kind: 'Tablix', dataset, rectangle, details_group}``.
    The inner Rectangle is named ``<name>_Rect`` and the detail group
    ``<name>_Details`` — each suffixed (``<name>_Rect1``, …) when that
    name is already taken report-wide, so read the actual names from the
    payload.

    Refuses on duplicate name. Refuses if ``dataset_name`` isn't a real
    dataset.
    """
    names.validate_identifier(name, "list")
    top, left, width, height = units.box(top, left, width, height)
    doc = RDLDocument.open(path)
    resolve_dataset(doc, dataset_name)  # raises if absent
    registry = names.NameRegistry(doc)
    registry.ensure_unique(name)
    registry.claim(name)
    # Generated names: suffixed (X_Rect1, ...) if a report item or group
    # already uses them (e.g. an earlier add_list('X_Rect') tablix).
    rect_name = registry.unique(f"{name}_Rect")
    details_name = registry.unique(f"{name}_Details")
    body = _resolve_body(doc)
    items = _ensure_body_report_items(body)

    tablix = etree.Element(q("Tablix"), Name=name)

    # ---- TablixBody ----
    tbody = etree.SubElement(tablix, q("TablixBody"))
    cols = etree.SubElement(tbody, q("TablixColumns"))
    col = etree.SubElement(cols, q("TablixColumn"))
    etree.SubElement(col, q("Width")).text = width
    rows = etree.SubElement(tbody, q("TablixRows"))
    row = etree.SubElement(rows, q("TablixRow"))
    etree.SubElement(row, q("Height")).text = height
    cells = etree.SubElement(row, q("TablixCells"))
    cell = etree.SubElement(cells, q("TablixCell"))
    contents = etree.SubElement(cell, q("CellContents"))
    inner = etree.SubElement(contents, q("Rectangle"), Name=rect_name)
    etree.SubElement(inner, q("KeepTogether")).text = "true"
    inner_style = etree.SubElement(inner, q("Style"))
    inner_border = etree.SubElement(inner_style, q("Border"))
    etree.SubElement(inner_border, q("Style")).text = "None"

    # ---- TablixColumnHierarchy ----
    col_h = etree.SubElement(tablix, q("TablixColumnHierarchy"))
    col_members = etree.SubElement(col_h, q("TablixMembers"))
    etree.SubElement(col_members, q("TablixMember"))

    # ---- TablixRowHierarchy ----
    row_h = etree.SubElement(tablix, q("TablixRowHierarchy"))
    row_members = etree.SubElement(row_h, q("TablixMembers"))
    details_member = etree.SubElement(row_members, q("TablixMember"))
    # Group Name uses the same convention insert_tablix_from_template uses
    # ("Details") but suffixed with the list name to avoid collisions when
    # multiple lists exist in one report.
    etree.SubElement(details_member, q("Group"), Name=details_name)

    # ---- footer fields ----
    etree.SubElement(tablix, q("DataSetName")).text = dataset_name
    etree.SubElement(tablix, q("Top")).text = top
    etree.SubElement(tablix, q("Left")).text = left
    etree.SubElement(tablix, q("Height")).text = height
    etree.SubElement(tablix, q("Width")).text = width
    style = etree.SubElement(tablix, q("Style"))
    border = etree.SubElement(style, q("Border"))
    etree.SubElement(border, q("Style")).text = "None"

    items.append(tablix)
    doc.save()
    return {
        "name": name,
        "kind": "Tablix",
        "dataset": dataset_name,
        "rectangle": rect_name,
        "details_group": details_name,
    }


# ---- add_line (Phase 11 commit 43) --------------------------------------


# RDL 2016 BorderStyle enumeration.
_VALID_LINE_STYLES = (
    "None",
    "Dotted",
    "Dashed",
    "Solid",
    "Double",
    "Groove",
    "Ridge",
    "Inset",
    "WindowInset",
    "Outset",
)


def add_line(
    path: str,
    name: str,
    top: str,
    left: str,
    width: str,
    height: str,
    color: str = "#000000",
    line_thickness: str = "1pt",
    line_style: str = "Solid",
    container: Optional[str] = None,
) -> dict[str, Any]:
    """Add a ``<Line>`` to ``<Body>/<ReportItems>`` — or, with
    ``container``, inside that body Rectangle (a List's included; top /
    left relative to it, #83).

    RDL Line semantics: ``<Top>`` / ``<Left>`` is the start point;
    ``<Width>`` / ``<Height>`` is the offset (vector) to the end point.
    A horizontal line has ``height='0in'``; a vertical line has
    ``width='0in'``; a diagonal has both nonzero.

    Styling lives on ``<Style>/<Border>``: ``color`` is the stroke
    colour, ``line_thickness`` is the stroke width (``'1pt'``,
    ``'2pt'``, etc.), and ``line_style`` ∈
    {None, Dotted, Dashed, Solid, Double, Groove, Ridge, Inset,
    WindowInset, Outset}.

    Refuses on duplicate name or unknown ``line_style``. Returns
    ``{name, kind: 'Line'}``.
    """
    names.validate_identifier(name, "line")
    if line_style not in _VALID_LINE_STYLES:
        raise ValueError(
            f"unknown line_style {line_style!r}; valid values: {list(_VALID_LINE_STYLES)}"
        )
    top = units.position(top, "top")
    left = units.position(left, "left")
    # Line Width / Height are the vector to the end point: may be negative.
    width = units.validate_size(width, field="width", allow_negative=True)
    height = units.validate_size(height, field="height", allow_negative=True)
    line_thickness = units.style_size(line_thickness, "line_thickness", positive=True)

    doc = RDLDocument.open(path)
    names.ensure_unique(doc, name)
    items = target_report_items(doc, container)

    line = etree.Element(q("Line"), Name=name)
    # Per RDL XSD Line child order: Style, Top, Left, Height, Width,
    # ZIndex, Visibility, ToolTip, Bookmark, ...
    style = etree.SubElement(line, q("Style"))
    border = etree.SubElement(style, q("Border"))
    etree.SubElement(border, q("Color")).text = color
    etree.SubElement(border, q("Style")).text = line_style
    etree.SubElement(border, q("Width")).text = line_thickness
    etree.SubElement(line, q("Top")).text = top
    etree.SubElement(line, q("Left")).text = left
    etree.SubElement(line, q("Height")).text = height
    etree.SubElement(line, q("Width")).text = width

    items.append(line)
    doc.save()
    return _in_container({"name": name, "kind": "Line"}, line, container)


__all__ = [
    "add_line",
    "add_list",
    "add_rectangle",
    "set_group_page_break",
    "set_keep_together",
    "set_keep_with_group",
    "set_repeat_on_new_page",
]
