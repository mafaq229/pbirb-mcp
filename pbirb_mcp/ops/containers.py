"""Report-item containers: Rectangles, List cells and page regions (#83).

RDL nests report items: the body, page header and page footer each hold
``<ReportItems>``, and so does every ``<Rectangle>`` — including the one a
List (a one-cell Tablix) keeps in its cell. An item's ``<Top>`` /
``<Left>`` are relative to whatever holds it. This module

* resolves a Rectangle to add items into (``container=`` on the add tools)
  and checks the new item against the rectangle's size;
* computes where a container's content origin sits on the page, so an item
  can change container without moving on the page;
* implements :func:`move_report_item` (reparent an item between the body,
  page header, page footer and Rectangles, keeping its whole subtree —
  style, actions, visibility) and :func:`ungroup_rectangle` (the inverse of
  ``add_rectangle(contained_items=...)``).

Page coordinates are the design-time layout: the header starts at the top
margin, the body below the header, the footer above the bottom margin; a
List cell sits at its tablix's position plus the row-header width / column
header height and the sizes of the cells before it (the first repetition).
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.geometry import (
    REPORT_ITEM_TAGS,
    TOLERANCE_IN,
    Box,
    child_items,
    item_box,
    length_in,
    local_name,
    section_geometry,
    size_unit,
    tablix_row_header_width,
)
from pbirb_mcp.core.ids import (
    REGION_OF_TAG,
    ElementNotFoundError,
    item_container,
    resolve_report_item,
)
from pbirb_mcp.core.xpath import find_child, q
from pbirb_mcp.ops import expr_walk
from pbirb_mcp.ops.header_footer import (
    DEFAULT_BAND_HEIGHT,
    FOOTER,
    HEADER,
    _ensure_page_section,
    _ensure_report_items,
    _set_or_create_text_in_order,
    detach_item,
    fit_band,
)

# Report items the page header / footer can't hold (RDL: no data regions
# or subreports in a page section).
DATA_REGION_TAGS = ("Tablix", "Chart", "GaugePanel", "Map", "CustomReportItem")
_NOT_IN_PAGE_SECTIONS = DATA_REGION_TAGS + ("Subreport",)
_REGIONS = {"body": "Body", "header": "PageHeader", "footer": "PageFooter"}
_PROSE = {"body": "the body", "header": "the page header", "footer": "the page footer"}


def container_label(container: etree._Element) -> str:
    """``body`` / ``header`` / ``footer`` / ``rectangle:<Name>``."""
    local = local_name(container)
    if local == "Rectangle":
        return f"rectangle:{container.get('Name')}"
    return REGION_OF_TAG.get(local, local)


def ensure_rectangle_items(rect: etree._Element) -> etree._Element:
    """``rect``'s ``<ReportItems>``, created first in the rectangle when absent
    (the Rectangle content model is an unordered choice)."""
    items = find_child(rect, "ReportItems")
    if items is None:
        items = etree.Element(q("ReportItems"))
        rect.insert(0, items)
    return items


# ---- geometry ---------------------------------------------------------------


def _column_header_height(tablix: etree._Element) -> float:
    """Height of a tablix's column-header area: the tallest root-to-leaf sum
    of ``TablixHeader/Size`` in the column hierarchy (0 when it has none)."""

    def tallest(members_root: Optional[etree._Element]) -> float:
        if members_root is None:
            return 0.0
        best = 0.0
        for member in members_root:
            if local_name(member) != "TablixMember":
                continue
            own = length_in(find_child(member, "TablixHeader"), "Size") or 0.0
            best = max(best, own + tallest(find_child(member, "TablixMembers")))
        return best

    hierarchy = find_child(tablix, "TablixColumnHierarchy")
    return tallest(find_child(hierarchy, "TablixMembers")) if hierarchy is not None else 0.0


def _span(cell: etree._Element) -> int:
    node = find_child(cell, "ColSpan")
    try:
        return max(1, int((node.text or "1").strip())) if node is not None else 1
    except ValueError:
        return 1


def cell_box(cell_contents: etree._Element) -> Optional[tuple[etree._Element, Box]]:
    """``(tablix, box)`` of the ``TablixBody`` cell whose ``<CellContents>``
    this is, the box relative to the tablix's top-left (design time: the
    first repetition). ``None`` for header / corner cells or unknown sizes."""
    cell = cell_contents.getparent()
    if cell is None or local_name(cell) != "TablixCell":
        return None
    row = cell.getparent().getparent()  # TablixCells -> TablixRow
    rows_root = row.getparent()
    body = rows_root.getparent()
    tablix = body.getparent()
    if local_name(tablix) != "Tablix":
        return None
    top = _column_header_height(tablix)
    for other in rows_root:
        if other is row:
            break
        if local_name(other) == "TablixRow":
            height = length_in(other, "Height")
            if height is None:
                return None
            top += height
    row_height = length_in(row, "Height")
    column = 0
    for other in cell.getparent():
        if other is cell:
            break
        if local_name(other) == "TablixCell":
            column += _span(other)
    cols_root = find_child(body, "TablixColumns")
    if cols_root is None or row_height is None:
        return None
    widths = [length_in(c, "Width") for c in cols_root.iterchildren(q("TablixColumn"))]
    end = column + _span(cell)
    if end > len(widths) or any(w is None for w in widths[:end]):
        return None
    known = [w for w in widths[:end] if w is not None]
    left = tablix_row_header_width(tablix) + sum(known[:column])
    return tablix, Box(left, top, sum(known[column:]), row_height)


def container_size(rect: etree._Element) -> Optional[tuple[float, float]]:
    """``(width, height)`` of a Rectangle in inches — its own box, or its
    tablix cell's for a rectangle filling a cell (a List's). ``None`` when
    unknown."""
    box = item_box(rect)
    if box is not None:
        return box.width, box.height
    parent = rect.getparent()
    if parent is not None and local_name(parent) == "CellContents":
        found = cell_box(parent)
        if found is not None:
            return found[1].width, found[1].height
    return None


def fit_warnings(item: etree._Element) -> list[str]:
    """A warning when ``item`` extends past the Rectangle holding it."""
    container = item_container(item)
    if container is None or local_name(container) != "Rectangle":
        return []
    size, box = container_size(container), item_box(item)
    if size is None or box is None:
        return []
    fmt = units.format_size
    edges = []
    if box.right > size[0] + TOLERANCE_IN:
        edges.append(f"right edge {fmt(box.right)} > width {fmt(size[0])}")
    if box.bottom > size[1] + TOLERANCE_IN:
        edges.append(f"bottom edge {fmt(box.bottom)} > height {fmt(size[1])}")
    if not edges:
        return []
    return [
        f"{local_name(item)} {item.get('Name')!r} extends outside rectangle "
        f"{container.get('Name')!r} ({'; '.join(edges)}); resize the rectangle or the item."
    ]


def _section_of(elem: etree._Element) -> Optional[etree._Element]:
    for anc in elem.iterancestors(q("ReportSection")):
        return anc
    return None


def _band_height(section: etree._Element, local: str) -> float:
    page = find_child(section, "Page")
    band = find_child(page, local) if page is not None else None
    return (length_in(band, "Height") or 0.0) if band is not None else 0.0


def _region_origin(section: etree._Element, region: str) -> tuple[float, float]:
    """Page position (top, left) in inches of a region's content origin."""
    geom = section_geometry(section)
    if region == "header":
        return geom.margin_top, geom.margin_left
    if region == "footer":
        top = geom.page_height - geom.margin_bottom - _band_height(section, "PageFooter")
        return top, geom.margin_left
    return geom.margin_top + _band_height(section, "PageHeader"), geom.margin_left


def _offset(item: etree._Element) -> Optional[tuple[float, float]]:
    """``item``'s (Top, Left) in inches — 0 when absent, ``None`` when not a
    literal size."""
    out = []
    for local in ("Top", "Left"):
        node = find_child(item, local)
        if node is None or not (node.text or "").strip():
            out.append(0.0)
            continue
        value = length_in(item, local)
        if value is None:
            return None
        out.append(value)
    return out[0], out[1]


def content_origin(container: etree._Element) -> Optional[tuple[float, float]]:
    """Page position (top, left) in inches of the origin ``container``'s
    items are positioned from, or ``None`` when it can't be worked out (a
    non-literal position, a tablix header cell, ...)."""
    local = local_name(container)
    section = _section_of(container)
    if section is None:
        return None
    if local in REGION_OF_TAG:
        return _region_origin(section, REGION_OF_TAG[local])
    if local == "Rectangle":
        own = _offset(container)
        base = content_origin(item_container(container))
        if own is None or base is None:
            return None
        return base[0] + own[0], base[1] + own[1]
    if local == "CellContents":
        found = cell_box(container)
        if found is None:
            return None
        tablix, box = found
        own = _offset(tablix)
        base = content_origin(item_container(tablix))
        if own is None or base is None:
            return None
        return base[0] + own[0] + box.top, base[1] + own[1] + box.left
    return None


# ---- container argument of the add tools -------------------------------------


def resolve_rectangle(
    doc: RDLDocument, name: str, *, dataset_name: Optional[str] = None
) -> etree._Element:
    """The Rectangle called ``name`` in the body (at any depth, including a
    List's ``<name>_Rect``) for the ``container`` argument of the add tools.
    Refuses a missing name, a non-Rectangle, a rectangle in the page header
    / footer, and — for a data region bound to ``dataset_name`` — a
    rectangle inside a data region bound to another dataset."""
    try:
        rect, _holder, region = resolve_report_item(doc, name)
    except ElementNotFoundError:
        raise ElementNotFoundError(f"container rectangle {name!r} not found") from None
    kind = local_name(rect)
    if kind != "Rectangle":
        raise ValueError(f"container {name!r} is a {kind}, not a Rectangle")
    if region != "body":
        raise ValueError(
            f"container {name!r} is in {_PROSE.get(region or '', 'no page region')}; "
            "this tool adds to the body — add the item there, then move_report_item it"
        )
    if dataset_name is not None:
        scope = expr_walk.dataset_scope(rect)
        if scope is not None and scope != dataset_name:
            raise ValueError(
                f"container {name!r} is inside a data region bound to dataset {scope!r}; "
                f"a data region nested there can't use dataset {dataset_name!r}"
            )
    return rect


def target_report_items(
    doc: RDLDocument, container: Optional[str], *, dataset_name: Optional[str] = None
) -> etree._Element:
    """The ``<ReportItems>`` a new item goes in: the body's, or with
    ``container`` that Rectangle's (see :func:`resolve_rectangle`)."""
    if container is None:
        from pbirb_mcp.ops.body import _ensure_body_report_items, _resolve_body

        return _ensure_body_report_items(_resolve_body(doc))
    return ensure_rectangle_items(resolve_rectangle(doc, container, dataset_name=dataset_name))


def place(
    doc: RDLDocument,
    item: etree._Element,
    container: Optional[str],
    result: dict[str, Any],
    *,
    dataset_name: Optional[str] = None,
) -> dict[str, Any]:
    """Append a new ``item`` to the body or the ``container`` Rectangle and
    save. Adds ``container`` (and ``warnings`` when it overflows the
    rectangle) to ``result``."""
    target_report_items(doc, container, dataset_name=dataset_name).append(item)
    doc.save()
    if container is None:
        return result
    out = {**result, "container": container}
    warnings = fit_warnings(item)
    if warnings:
        out["warnings"] = list(out.get("warnings", [])) + warnings
    return out


# ---- move_report_item --------------------------------------------------------


def _unscoped_field_refs(item: etree._Element) -> list[str]:
    """``Fields!`` references in ``item``'s subtree whose dataset comes from
    where the item sits — not inside an aggregate with an explicit scope and
    not inside a data region of the subtree itself."""
    found: list[str] = []
    for elem, text in expr_walk.iter_expressions(item):
        if "fields" not in text.lower():
            continue
        region = expr_walk.scope_region(elem)
        if region is not None and (region is item or item in region.iterancestors()):
            continue
        sig = expr_walk.expression_tokens(text)
        if not sig:
            continue
        calls = expr_walk.aggregate_calls(sig)
        for ref in expr_walk.references(sig, "Fields"):
            enclosing = expr_walk.enclosing_calls(calls, ref.first)
            if any(c.scope is not None or c.kind == expr_walk.LOOKUP for c in enclosing):
                continue
            ref_text = text[ref.start : ref.end]
            if ref_text not in found:
                found.append(ref_text)
    return found


def _subtree_items(item: etree._Element) -> list[etree._Element]:
    """``item`` and every report item inside it."""
    return list(item.iter(*[q(t) for t in REPORT_ITEM_TAGS]))


def _check_scope(item: etree._Element, dest_region: str, rect: Optional[etree._Element]) -> None:
    """Refuse a move that changes the dataset ``item``'s ``Fields!``
    references are evaluated against (see :func:`move_report_item`)."""
    kind, name = local_name(item), item.get("Name")
    if dest_region in ("header", "footer"):
        blocked = [el for el in _subtree_items(item) if local_name(el) in _NOT_IN_PAGE_SECTIONS]
        if blocked:
            what = ", ".join(f"{local_name(el)} {el.get('Name')!r}" for el in blocked)
            raise ValueError(
                f"can't move {kind} {name!r} into {_PROSE[dest_region]}: it is or holds "
                f"{what}; the page header / footer can't hold data regions or subreports"
            )
    src_scope = expr_walk.dataset_scope(item)
    dest_scope = expr_walk.dataset_scope(rect) if rect is not None else None
    if dest_region in ("header", "footer"):
        why = "the page header / footer takes fields only inside an aggregate with a dataset scope"
    elif src_scope is not None and dest_scope is None:
        why = f"outside a data region they lose dataset {src_scope!r}"
    elif src_scope is not None and dest_scope != src_scope:
        why = f"they would be evaluated against dataset {dest_scope!r} instead of {src_scope!r}"
    else:
        return  # same scope, or gaining one (e.g. a body textbox moved into a List)
    refs = _unscoped_field_refs(item)
    if refs:
        raise ValueError(
            f"can't move {kind} {name!r} into {_prose(dest_region, rect)}: "
            f"{', '.join(refs)} — {why}. Wrap each in an aggregate with a dataset scope, "
            'e.g. First(Fields!X.Value, "DataSet1").'
        )


def _prose(region: str, rect: Optional[etree._Element]) -> str:
    return f"rectangle {rect.get('Name')!r}" if rect is not None else _PROSE[region]


def _unit_of(item: etree._Element, local: str) -> str:
    node = find_child(item, local)
    return size_unit(node.text if node is not None else None)


def _destination(
    doc: RDLDocument, item: etree._Element, to: str
) -> tuple[Optional[etree._Element], str, Optional[etree._Element]]:
    """``(container_or_None, region, rect_or_None)``. The container is
    ``None`` for a page header / footer that doesn't exist yet."""
    section = _section_of(item)
    if section is None:
        raise ValueError(f"{item.get('Name')!r} is not in a report section")
    if to in _REGIONS:
        if to == "body":
            body = find_child(section, "Body")
            return body, "body", None
        page = find_child(section, "Page")
        band = find_child(page, _REGIONS[to]) if page is not None else None
        return band, to, None
    try:
        rect, _holder, region = resolve_report_item(doc, to)
    except ElementNotFoundError:
        raise ElementNotFoundError(
            f"move destination {to!r} is neither 'body' / 'header' / 'footer' nor a "
            "Rectangle in the report"
        ) from None
    if local_name(rect) != "Rectangle":
        raise ValueError(f"move destination {to!r} is a {local_name(rect)}, not a Rectangle")
    if rect is item or item in rect.iterancestors():
        raise ValueError(f"can't move {item.get('Name')!r} into itself or its own contents")
    if _section_of(rect) is not section:
        raise ValueError(f"rectangle {to!r} is in another report section")
    if region is None:
        raise ValueError(f"rectangle {to!r} is not in the body, page header or page footer")
    return rect, region, rect


def move_report_item(
    path: str,
    name: str,
    to: str,
    top: Optional[str] = None,
    left: Optional[str] = None,
) -> dict[str, Any]:
    """Move the report item ``name`` (from anywhere: body, page header,
    page footer, a Rectangle, a List's rectangle) into ``to`` — ``"body"``,
    ``"header"``, ``"footer"`` or the name of a Rectangle — keeping its
    whole subtree (style, actions, visibility, nested items) and its name.

    Without ``top`` / ``left`` the item keeps its on-page position: its
    coordinates are converted to the destination's (refused when that
    would be negative — pass ``top`` / ``left``); either one given
    overrides that axis. A missing page header / footer is created and a
    band grows to hold the item (``band_height``, ``warnings``).

    Refuses moving a data region (Tablix, Chart, ...) or Subreport into the
    page header / footer, and an item whose ``Fields!`` references would
    change dataset scope (header / footer: any reference not inside an
    aggregate with a dataset scope, e.g. ``First(Fields!X.Value, "DS")``).
    A tablix cell's own content can't be moved. Idempotent: an item already
    in ``to`` at the requested position returns ``changed: false``.

    Returns ``{name, kind, from, to, top, left, changed}`` (+ ``band_height``,
    ``warnings``).
    """
    top = units.position(top, "top")
    left = units.position(left, "left")
    doc = RDLDocument.open(path)
    item, src, _region = resolve_report_item(doc, name)
    kind = local_name(item)
    if local_name(src) == "CellContents":
        raise ValueError(
            f"{kind} {name!r} fills a tablix cell, which must hold an item; move the "
            "items inside it instead"
        )
    dest, dest_region, rect = _destination(doc, item, to)
    section = _section_of(item)
    same = dest is src
    if not same:
        _check_scope(item, dest_region, rect)

    # ---- coordinates: keep the on-page position unless given ----
    new = {"Top": top, "Left": left}
    if not same and (top is None or left is None):
        own = _offset(item)
        src_origin = content_origin(src)
        # A missing header / footer is created below; its origin is known.
        dest_origin = (
            content_origin(dest) if dest is not None else _region_origin(section, dest_region)
        )
        if own is None or src_origin is None or dest_origin is None:
            raise ValueError(
                f"can't work out where {kind} {name!r} sits on the page (a non-literal "
                "position or a tablix header cell); pass top and left"
            )
        for idx, local, side in ((0, "Top", "above"), (1, "Left", "left of")):
            if new[local] is not None:
                continue
            value = own[idx] + src_origin[idx] - dest_origin[idx]
            if value < -TOLERANCE_IN:
                raise ValueError(
                    f"keeping {kind} {name!r} where it is on the page would put it "
                    f"{units.format_size(-value)} {side} {_prose(dest_region, rect)}; pass "
                    f"{local.lower()} explicitly"
                )
            new[local] = units.format_size(max(0.0, value), _unit_of(item, local))

    from pbirb_mcp.ops.positioning import _set_layout_value

    if same:
        changed = False
        for local, value in new.items():
            if value is not None and _set_layout_value(item, local, value):
                changed = True
        if changed:
            doc.save()
        return _move_result(item, name, kind, src, src, changed, {})

    # ---- move ----
    warnings: list[str] = []
    extras: dict[str, Any] = {}
    if dest is None:
        page = find_child(section, "Page")
        dest = _ensure_page_section(page, HEADER if dest_region == "header" else FOOTER)
        if dest_region == "header":
            warnings.append(
                "created the page header; the body now starts below it, so body items "
                "move down the page by the header's height"
            )
    detach_item(item)
    if rect is not None:
        ensure_rectangle_items(rect).append(item)
    elif dest_region == "body":
        from pbirb_mcp.ops.body import _ensure_body_report_items

        _ensure_body_report_items(dest).append(item)
    else:
        _ensure_report_items(dest).append(item)
    for local, value in new.items():
        if value is not None:
            _set_layout_value(item, local, value)
    if dest_region in ("header", "footer"):
        band = dest if rect is None else _band_of(rect)
        band_item = _top_level(item, band)
        page = find_child(section, "Page")
        old = find_child(band, "Height")
        old_text = old.text if old is not None else None
        band_warnings, grown = fit_band(page, band, band_item, grow=True)
        if find_child(band, "Height") is None:
            _set_or_create_text_in_order(band, "Height", DEFAULT_BAND_HEIGHT)
        if grown is not None and old_text is not None:
            warnings.append(
                f"<{local_name(band)}> Height grown from {old_text!r} to {grown!r} to fit {name!r}."
            )
        warnings.extend(band_warnings)
        extras["band_height"] = find_child(band, "Height").text
    warnings.extend(fit_warnings(item))
    doc.save()
    if warnings:
        extras["warnings"] = warnings
    return _move_result(item, name, kind, src, dest, True, extras)


def _band_of(elem: etree._Element) -> etree._Element:
    for anc in elem.iterancestors():
        if local_name(anc) in ("PageHeader", "PageFooter"):
            return anc
    raise ValueError("not in a page header / footer")


def _top_level(item: etree._Element, band: etree._Element) -> etree._Element:
    top = item
    for anc in item.iterancestors():
        if anc is band:
            break
        if local_name(anc) in REPORT_ITEM_TAGS:
            top = anc
    return top


def _move_result(
    item: etree._Element,
    name: str,
    kind: str,
    src: etree._Element,
    dest: etree._Element,
    changed: bool,
    extras: dict[str, Any],
) -> dict[str, Any]:
    top = find_child(item, "Top")
    left = find_child(item, "Left")
    return {
        "name": name,
        "kind": kind,
        "from": container_label(src),
        "to": container_label(dest),
        "top": top.text if top is not None else "0in",
        "left": left.text if left is not None else "0in",
        "changed": changed,
        **extras,
    }


# ---- ungroup_rectangle -------------------------------------------------------

# Rectangle properties that stop applying once its children leave it.
_DROPPED = ("Visibility", "ActionInfo", "ToolTip", "DocumentMapLabel", "Bookmark", "PageBreak")


def _visible_style(rect: etree._Element) -> bool:
    style = find_child(rect, "Style")
    if style is None:
        return False
    if any(
        find_child(style, local) is not None for local in ("BackgroundColor", "BackgroundImage")
    ):
        return True
    for border in ("Border", "TopBorder", "BottomBorder", "LeftBorder", "RightBorder"):
        node = find_child(style, border)
        if node is None:
            continue
        line = find_child(node, "Style")
        if line is None or (line.text or "").strip() != "None":
            return True
    return False


def ungroup_rectangle(path: str, name: str) -> dict[str, Any]:
    """Remove the Rectangle ``name`` and move its items out to the
    rectangle's own container, where the rectangle was, keeping their
    on-page position (``Top`` / ``Left`` += the rectangle's, each in the
    item's unit) — the inverse of ``add_rectangle(contained_items=...)``.

    Warns about what the rectangle carried that its items don't inherit:
    a ``Visibility`` (they are no longer hidden with it), a border or
    background, an action, tooltip, bookmark, document-map label or page
    break. Refuses a rectangle that fills a tablix cell (a List's): the
    cell must hold one item.

    Returns ``{name, moved: [names], to, changed: true}`` (+ ``warnings``).
    """
    doc = RDLDocument.open(path)
    rect, holder, _region = resolve_report_item(doc, name)
    if local_name(rect) != "Rectangle":
        raise ValueError(f"{name!r} is a {local_name(rect)}, not a Rectangle")
    if local_name(holder) == "CellContents":
        raise ValueError(
            f"rectangle {name!r} fills a tablix cell, which must hold an item; move its "
            "items out with move_report_item instead"
        )
    offset = _offset(rect)
    if offset is None:
        raise ValueError(f"rectangle {name!r} has a non-literal Top / Left")
    children = child_items(rect)
    offsets = []
    for child in children:
        own = _offset(child)
        if own is None:
            raise ValueError(
                f"{local_name(child)} {child.get('Name')!r} has a non-literal Top / Left"
            )
        offsets.append(own)
    from pbirb_mcp.ops.positioning import _set_layout_value

    warnings: list[str] = []
    dropped = [local for local in _DROPPED if find_child(rect, local) is not None]
    if _visible_style(rect):
        dropped.append("border / background")
    if dropped and children:
        warnings.append(
            f"rectangle {name!r} had {', '.join(dropped)}, which no longer applies to "
            f"its items: {', '.join(repr(c.get('Name')) for c in children)}"
        )
    for child, own in zip(children, offsets):
        for idx, local in ((0, "Top"), (1, "Left")):
            unit = _unit_of(child, local)
            _set_layout_value(child, local, units.format_size(own[idx] + offset[idx], unit))
        rect.addprevious(child)
    detach_item(rect)
    doc.save()
    result: dict[str, Any] = {
        "name": name,
        "moved": [c.get("Name") for c in children],
        "to": container_label(holder),
        "changed": True,
    }
    if warnings:
        result["warnings"] = warnings
    return result


__all__ = [
    "DATA_REGION_TAGS",
    "cell_box",
    "container_label",
    "container_size",
    "content_origin",
    "ensure_rectangle_items",
    "fit_warnings",
    "move_report_item",
    "place",
    "resolve_rectangle",
    "target_report_items",
    "ungroup_rectangle",
]
