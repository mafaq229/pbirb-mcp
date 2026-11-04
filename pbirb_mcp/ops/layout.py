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

from pbirb_mcp.core import names, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_dataset, resolve_group
from pbirb_mcp.core.xpath import XPATH_NS, find_child, q
from pbirb_mcp.ops.body import _ensure_body_report_items, _resolve_body
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
        raise ValueError(f"Group {group_name!r} is not enclosed in a <TablixMember>")

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
            f"unknown KeepWithGroup value {value!r}; valid values: {list(_VALID_KEEP_WITH_GROUP)}"
        )

    doc = RDLDocument.open(path)
    group = resolve_group(doc, tablix_name, group_name)
    member = group.getparent()
    if member is None or etree.QName(member.tag).localname != "TablixMember":
        raise ValueError(f"Group {group_name!r} is not enclosed in a <TablixMember>")

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
    body = _resolve_body(doc)
    items = _ensure_body_report_items(body)

    # Resolve every contained item up front — fail before mutating the tree.
    children: list[etree._Element] = []
    if contained_items:
        by_name = {el.get("Name"): el for el in list(items) if el.get("Name") is not None}
        missing = [n for n in contained_items if n not in by_name]
        if missing:
            raise ElementNotFoundError(
                f"cannot move into rectangle — items not in <Body>/<ReportItems>: {missing}"
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
    a Rectangle inside the single cell. Items the user later places
    inside the rectangle repeat once per dataset row.

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
) -> dict[str, Any]:
    """Add a ``<Line>`` to ``<Body>/<ReportItems>``.

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
    body = _resolve_body(doc)
    items = _ensure_body_report_items(body)

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
    return {"name": name, "kind": "Line"}


__all__ = [
    "add_line",
    "add_list",
    "add_rectangle",
    "set_group_page_break",
    "set_keep_together",
    "set_keep_with_group",
    "set_repeat_on_new_page",
]
