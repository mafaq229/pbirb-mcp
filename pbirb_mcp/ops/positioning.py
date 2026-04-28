"""Position / size tools for named ReportItems (v0.2 commits 6, 7, 8).

These tools change the ``<Top>`` / ``<Left>`` / ``<Width>`` / ``<Height>``
of an existing named item — Tablix, Textbox, Image, Rectangle, Subreport,
Chart, Line — without otherwise restructuring it. Any existing styling,
sort, filter, group structure, etc. is preserved.

Containers covered:

* ``set_body_item_position`` / ``set_body_item_size`` — items inside
  ``<ReportSection>/<Body>/<ReportItems>``.
* ``set_header_item_position`` — items inside ``<Page>/<PageHeader>/<ReportItems>``.
* ``set_footer_item_position`` — items inside ``<Page>/<PageFooter>/<ReportItems>``.

Position / size values are **passed through verbatim** to ``<Top>`` /
``<Left>`` / ``<Width>`` / ``<Height>``. Report Builder accepts any RDL
size string (``"2cm"``, ``"0.75in"``, ``"108pt"``); we don't convert.

The position elements are inserted in the layout-element order
RDL XSD requires (Top, Left, Height, Width, ZIndex, Visibility, ...) —
``_insert_layout_child`` picks the right neighbour to insert before.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.page import _resolve_page  # type: ignore[attr-defined]

# Layout-related ReportItem child order per RDL XSD. We only emit the
# first four; the rest are listed so we don't accidentally insert before
# a sibling that should come after.
_LAYOUT_CHILD_ORDER = (
    "Top",
    "Left",
    "Height",
    "Width",
    "ZIndex",
    "Visibility",
    "ToolTip",
    "DocumentMapLabel",
    "Bookmark",
    "RepeatWith",
    "CustomProperties",
    "DataElementName",
    "DataElementOutput",
    "Style",
)


def _insert_layout_child(item: etree._Element, new_child: etree._Element) -> None:
    """Insert ``new_child`` into ``item`` respecting layout sibling order.
    Replaces any existing element of the same local name."""
    new_local = etree.QName(new_child).localname
    existing = find_child(item, new_local)
    if existing is not None:
        item.replace(existing, new_child)
        return
    if new_local not in _LAYOUT_CHILD_ORDER:
        item.append(new_child)
        return
    new_idx = _LAYOUT_CHILD_ORDER.index(new_local)
    for i, child in enumerate(list(item)):
        local = etree.QName(child).localname
        if local in _LAYOUT_CHILD_ORDER and _LAYOUT_CHILD_ORDER.index(local) > new_idx:
            item.insert(i, new_child)
            return
    item.append(new_child)


def _set_layout_value(item: etree._Element, local_name: str, value: str) -> None:
    new = etree.Element(q(local_name))
    new.text = value
    _insert_layout_child(item, new)


# Item types we recognise as named ReportItems.
_REPORT_ITEM_TAGS = (
    "Tablix",
    "Textbox",
    "Image",
    "Rectangle",
    "Subreport",
    "Chart",
    "Map",
    "Gauge",
    "Line",
    "List",
)


def _find_named_item(container: etree._Element, name: str) -> Optional[etree._Element]:
    """Find a direct child of ``container/ReportItems`` whose Name matches.
    Returns None if absent."""
    items = find_child(container, "ReportItems")
    if items is None:
        return None
    for child in items:
        if child.get("Name") != name:
            continue
        local = etree.QName(child).localname
        if local in _REPORT_ITEM_TAGS:
            return child
    return None


# ---- container resolvers -------------------------------------------------


def _resolve_body(doc: RDLDocument) -> etree._Element:
    body = doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body")
    if body is None:
        raise ValueError("report has no <ReportSection>/<Body>")
    return body


def _resolve_header(doc: RDLDocument) -> etree._Element:
    page = _resolve_page(doc)
    header = find_child(page, "PageHeader")
    if header is None:
        raise ElementNotFoundError("report has no <PageHeader>; call set_page_header first.")
    return header


def _resolve_footer(doc: RDLDocument) -> etree._Element:
    page = _resolve_page(doc)
    footer = find_child(page, "PageFooter")
    if footer is None:
        raise ElementNotFoundError("report has no <PageFooter>; call set_page_footer first.")
    return footer


# ---- public tools (commit 6) ---------------------------------------------


def set_body_item_position(
    path: str,
    name: str,
    top: str,
    left: str,
) -> dict[str, Any]:
    """Move an existing named body item to ``(top, left)`` without
    otherwise restructuring it. ``top`` / ``left`` are passed through
    verbatim — Report Builder accepts any RDL size unit.
    """
    doc = RDLDocument.open(path)
    container = _resolve_body(doc)
    item = _find_named_item(container, name)
    if item is None:
        raise ElementNotFoundError(f"body has no named item {name!r}")
    _set_layout_value(item, "Top", top)
    _set_layout_value(item, "Left", left)
    doc.save()
    return {"name": name, "container": "body", "top": top, "left": left}


# ---- public tools (commit 7) ---------------------------------------------


def set_header_item_position(
    path: str,
    name: str,
    top: str,
    left: str,
) -> dict[str, Any]:
    """Move a named item inside ``<PageHeader>``."""
    doc = RDLDocument.open(path)
    container = _resolve_header(doc)
    item = _find_named_item(container, name)
    if item is None:
        raise ElementNotFoundError(f"page header has no named item {name!r}")
    _set_layout_value(item, "Top", top)
    _set_layout_value(item, "Left", left)
    doc.save()
    return {"name": name, "container": "header", "top": top, "left": left}


def set_footer_item_position(
    path: str,
    name: str,
    top: str,
    left: str,
) -> dict[str, Any]:
    """Move a named item inside ``<PageFooter>``."""
    doc = RDLDocument.open(path)
    container = _resolve_footer(doc)
    item = _find_named_item(container, name)
    if item is None:
        raise ElementNotFoundError(f"page footer has no named item {name!r}")
    _set_layout_value(item, "Top", top)
    _set_layout_value(item, "Left", left)
    doc.save()
    return {"name": name, "container": "footer", "top": top, "left": left}


# ---- public tools (commit 8) ---------------------------------------------


def set_body_item_size(
    path: str,
    name: str,
    width: Optional[str] = None,
    height: Optional[str] = None,
) -> dict[str, Any]:
    """Resize an existing named body item. Either ``width`` or ``height``
    (or both) must be supplied; missing fields are left untouched."""
    if width is None and height is None:
        raise ValueError("at least one of width or height must be supplied; both None is a no-op.")
    doc = RDLDocument.open(path)
    container = _resolve_body(doc)
    item = _find_named_item(container, name)
    if item is None:
        raise ElementNotFoundError(f"body has no named item {name!r}")
    if width is not None:
        _set_layout_value(item, "Width", width)
    if height is not None:
        _set_layout_value(item, "Height", height)
    doc.save()
    return {"name": name, "container": "body", "width": width, "height": height}


__all__ = [
    "set_body_item_position",
    "set_body_item_size",
    "set_footer_item_position",
    "set_header_item_position",
]
