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

Each also reaches items nested in that region's Rectangles (including a
List's rectangle) — resolved by :func:`pbirb_mcp.core.ids.resolve_report_item`
(#83). Their coordinates stay relative to the Rectangle, as RDL defines
them; a nested item that now extends past its Rectangle returns a warning.

The header / footer variants check the result against the band (#50,
:func:`pbirb_mcp.ops.header_footer.fit_band`): an item whose bottom passes
the band's ``<Height>`` returns a ``warnings`` entry, or — with
``grow_band=True`` — grows the band to fit (``band_height`` in the
result). A right edge past the page's printable width always warns.

Position / size values are validated by :mod:`pbirb_mcp.core.units`
(``<number><unit>``; positions >= 0; sizes > 0 except a Line's
width / height, which may be zero or negative) and then written
**verbatim** to ``<Top>`` / ``<Left>`` / ``<Width>`` / ``<Height>`` — any
RDL unit (``"2cm"``, ``"0.75in"``, ``"108pt"``) is kept as given; we
don't convert.

The position elements are inserted in the layout-element order
RDL XSD requires (Top, Left, Height, Width, ZIndex, Visibility, ...) —
``_insert_layout_child`` picks the right neighbour to insert before.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from lxml import etree

from pbirb_mcp.core import units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.geometry import REPORT_ITEM_TAGS, local_name
from pbirb_mcp.core.ids import ElementNotFoundError, item_container, resolve_report_item
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.containers import fit_warnings
from pbirb_mcp.ops.header_footer import fit_band
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


def _set_layout_value(item: etree._Element, local_name: str, value: str) -> bool:
    """Write ``<local_name>value</local_name>`` into ``item``. Returns True
    if the file value actually changed (used by callers to surface a
    ``changed: bool`` flag for idempotency)."""
    existing = find_child(item, local_name)
    if existing is not None and existing.text == value:
        return False
    new = etree.Element(q(local_name))
    new.text = value
    _insert_layout_child(item, new)
    return True


def _locate_item(
    doc: RDLDocument,
    region_el: etree._Element,
    region: str,
    name: str,
) -> tuple[etree._Element, etree._Element]:
    """``(item, band_item)`` for the item called ``name`` anywhere in the
    region — at its top level or inside Rectangles / List cells (#83).
    ``band_item`` is the item's top-level ancestor in the region (the item
    itself when it isn't nested), the one the band check measures."""
    item, container, _region = resolve_report_item(doc, name, region=region)
    if local_name(container) == "CellContents":
        raise ValueError(
            f"{local_name(item)} {name!r} fills a tablix cell, which places and sizes "
            "it; change the cell's row height / column width instead"
        )
    band_item = item
    for anc in item.iterancestors():
        if anc is region_el:
            break
        if local_name(anc) in REPORT_ITEM_TAGS:
            band_item = anc
    return item, band_item


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


def _fit_chrome_band(
    doc: RDLDocument,
    container: etree._Element,
    item: etree._Element,
    grow_band: Optional[bool],
) -> dict[str, Any]:
    """Run the #50 band check for a header/footer item (``grow_band`` is
    ``None`` for body items: no check). Returns the extra result keys:
    ``band_height`` when the band grew, ``warnings`` when non-empty."""
    if grow_band is None:
        return {}
    warnings, grown = fit_band(_resolve_page(doc), container, item, grow=bool(grow_band))
    extras: dict[str, Any] = {}
    if grown is not None:
        extras["band_height"] = grown
    if warnings:
        extras["warnings"] = warnings
    return extras


def _move_named_item(
    path: str,
    resolve_container: Callable[[RDLDocument], etree._Element],
    container_label: str,
    name: str,
    top: str,
    left: str,
    grow_band: Optional[bool] = None,
) -> dict[str, Any]:
    # Validate before opening the document: a bad value never writes.
    top = units.validate_size(top, field="top")
    left = units.validate_size(left, field="left")
    doc = RDLDocument.open(path)
    container = resolve_container(doc)
    item, band_item = _locate_item(doc, container, container_label, name)
    changed_fields: list[str] = []
    if _set_layout_value(item, "Top", top):
        changed_fields.append("Top")
    if _set_layout_value(item, "Left", left):
        changed_fields.append("Left")
    extras = _fit_chrome_band(doc, container, band_item, grow_band)
    if "band_height" in extras:
        changed_fields.append("BandHeight")
    if changed_fields:
        doc.save()
    return _result(name, item, container_label, {"top": top, "left": left}, changed_fields, extras)


def _result(
    name: str,
    item: etree._Element,
    container_label: str,
    values: dict[str, Any],
    changed_fields: list[str],
    extras: dict[str, Any],
) -> dict[str, Any]:
    """The position / size tools' result. ``parent`` names the Rectangle
    holding a nested item (``None`` at the region's top level)."""
    parent = item_container(item)
    warnings = list(extras.pop("warnings", [])) + fit_warnings(item)
    result: dict[str, Any] = {
        "name": name,
        "container": container_label,
        "parent": parent.get("Name") if local_name(parent) == "Rectangle" else None,
        **values,
        "changed": bool(changed_fields),
        **extras,
    }
    if warnings:
        result["warnings"] = warnings
    return result


def set_body_item_position(
    path: str,
    name: str,
    top: str,
    left: str,
) -> dict[str, Any]:
    """Move an existing named body item to ``(top, left)`` without
    otherwise restructuring it. ``top`` / ``left`` are passed through
    verbatim — Report Builder accepts any RDL size unit. Returns
    ``changed: false`` when the item is already at the requested
    position (idempotent — file is not rewritten).
    """
    return _move_named_item(path, _resolve_body, "body", name, top, left)


# ---- public tools (commit 7) ---------------------------------------------


def set_header_item_position(
    path: str,
    name: str,
    top: str,
    left: str,
    grow_band: bool = False,
) -> dict[str, Any]:
    """Move a named item inside ``<PageHeader>``. Idempotent — see
    ``set_body_item_position`` for the ``changed`` semantics. Warns when
    the item no longer fits the band; ``grow_band=True`` grows the band
    instead (#50)."""
    return _move_named_item(path, _resolve_header, "header", name, top, left, grow_band)


def set_footer_item_position(
    path: str,
    name: str,
    top: str,
    left: str,
    grow_band: bool = False,
) -> dict[str, Any]:
    """Move a named item inside ``<PageFooter>``. Idempotent — see
    ``set_body_item_position`` for the ``changed`` semantics. Warns when
    the item no longer fits the band; ``grow_band=True`` grows the band
    instead (#50)."""
    return _move_named_item(path, _resolve_footer, "footer", name, top, left, grow_band)


# ---- public tools (commit 8) ---------------------------------------------


def _resize_named_item(
    path: str,
    resolve_container: Callable[[RDLDocument], etree._Element],
    container_label: str,
    name: str,
    width: Optional[str],
    height: Optional[str],
    grow_band: Optional[bool] = None,
) -> dict[str, Any]:
    """Shared resize helper used by set_body_item_size /
    set_header_item_size / set_footer_item_size. Either width or height
    (or both) must be supplied; missing fields are left untouched.
    """
    if width is None and height is None:
        raise ValueError("at least one of width or height must be supplied; both None is a no-op.")
    # Grammar first, before the lookup; the sign rule depends on the item
    # kind — a Line's Width/Height is the vector to its end point, so it
    # may be zero or negative. Everything else must be strictly positive.
    units.validate_optional_size(width, field="width", allow_negative=True)
    units.validate_optional_size(height, field="height", allow_negative=True)
    doc = RDLDocument.open(path)
    container = resolve_container(doc)
    item, band_item = _locate_item(doc, container, container_label, name)
    line = etree.QName(item).localname == "Line"
    width = units.validate_optional_size(width, field="width", allow_negative=line, allow_zero=line)
    height = units.validate_optional_size(
        height, field="height", allow_negative=line, allow_zero=line
    )
    changed_fields: list[str] = []
    if width is not None and _set_layout_value(item, "Width", width):
        changed_fields.append("Width")
    if height is not None and _set_layout_value(item, "Height", height):
        changed_fields.append("Height")
    extras = _fit_chrome_band(doc, container, band_item, grow_band)
    if "band_height" in extras:
        changed_fields.append("BandHeight")
    if changed_fields:
        doc.save()
    return _result(
        name, item, container_label, {"width": width, "height": height}, changed_fields, extras
    )


def set_body_item_size(
    path: str,
    name: str,
    width: Optional[str] = None,
    height: Optional[str] = None,
) -> dict[str, Any]:
    """Resize an existing named body item. Either ``width`` or ``height``
    (or both) must be supplied; missing fields are left untouched.
    Returns ``changed: false`` when neither dimension differs from the
    current value (idempotent — file is not rewritten)."""
    return _resize_named_item(path, _resolve_body, "body", name, width, height)


# ---- public tools (Phase 6 commit 26) — header/footer size parity ------


def set_header_item_size(
    path: str,
    name: str,
    width: Optional[str] = None,
    height: Optional[str] = None,
    grow_band: bool = False,
) -> dict[str, Any]:
    """Resize a named item inside ``<PageHeader>``. Mirrors the contract
    of :func:`set_body_item_size` — at least one of width / height; missing
    fields untouched; ``changed: false`` when nothing differs.

    Closes the v0.2 parity gap where only ``set_body_item_size`` existed
    and resizing a header logo / banner forced a manual ``str_replace``.

    Warns when the item no longer fits the band (or the page's printable
    width); ``grow_band=True`` grows the band instead (#50).
    """
    return _resize_named_item(path, _resolve_header, "header", name, width, height, grow_band)


def set_footer_item_size(
    path: str,
    name: str,
    width: Optional[str] = None,
    height: Optional[str] = None,
    grow_band: bool = False,
) -> dict[str, Any]:
    """Resize a named item inside ``<PageFooter>``. Same shape as
    :func:`set_header_item_size`."""
    return _resize_named_item(path, _resolve_footer, "footer", name, width, height, grow_band)


__all__ = [
    "set_body_item_position",
    "set_body_item_size",
    "set_footer_item_position",
    "set_footer_item_size",
    "set_header_item_position",
    "set_header_item_size",
]
