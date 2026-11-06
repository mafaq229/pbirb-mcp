"""Page-header and page-footer tools.

``<PageHeader>`` and ``<PageFooter>`` share the same ``PageSection``
schema, so the implementation is parametrised over a region kind. The
public surface keeps separate ``add_header_*`` / ``add_footer_*`` tools
so the LLM-facing tool catalog stays explicit — same set of operations,
just two regions.

Per RDL XSD, the child order inside a ``PageSection`` is:
  Height, PrintOnFirstPage, PrintOnLastPage, ReportItems, Style.

Insertions respect that order via :func:`_set_or_create_text_in_order`,
mirroring the ordering helper in :mod:`pbirb_mcp.ops.page`.

Band sizing (#50): RDL requires ``<Height>`` on a page section, so every
tool that creates one writes it (the given height, else ``0.5in``). The
``add_*`` tools grow the band to ``max(0.5in, top + height)`` when the new
item would hang below it; :func:`fit_band` is the shared check the
positioning tools use to warn (or, with ``grow_band``, grow) when a moved
or resized item no longer fits. An item whose right edge passes the
page's printable width only warns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import resolve_report_item
from pbirb_mcp.core.xpath import find_child, q, qrd
from pbirb_mcp.ops import expr_walk
from pbirb_mcp.ops.images import check_image_source
from pbirb_mcp.ops.page import _resolve_page, printable_width_in  # type: ignore[attr-defined]

_PAGE_SECTION_CHILD_ORDER = (
    "Height",
    "PrintOnFirstPage",
    "PrintOnLastPage",
    "ReportItems",
    "Style",
)

# Where the section sits inside <Page>. Per RDL XSD, PageHeader and
# PageFooter are the FIRST two children of <Page>, before PageHeight.
_PAGE_CHILD_ORDER = (
    "PageHeader",
    "PageFooter",
    "PageHeight",
    "PageWidth",
    "InteractiveHeight",
    "InteractiveWidth",
    "LeftMargin",
    "RightMargin",
    "TopMargin",
    "BottomMargin",
    "Columns",
    "ColumnSpacing",
    "Style",
)


@dataclass(frozen=True)
class _Region:
    name: str
    section_local: str  # "PageHeader" or "PageFooter"


HEADER = _Region("header", "PageHeader")
FOOTER = _Region("footer", "PageFooter")

# Height written for a band created without an explicit one.
DEFAULT_BAND_HEIGHT = "0.5in"
_EPS = 1e-6


# ---- section + ReportItems plumbing --------------------------------------


def _ensure_page_section(page: etree._Element, region: _Region) -> etree._Element:
    sec = find_child(page, region.section_local)
    if sec is not None:
        return sec
    sec = etree.Element(q(region.section_local))
    new_idx = _PAGE_CHILD_ORDER.index(region.section_local)
    for i, child in enumerate(list(page)):
        local = etree.QName(child).localname
        if local in _PAGE_CHILD_ORDER and _PAGE_CHILD_ORDER.index(local) > new_idx:
            page.insert(i, sec)
            return sec
    page.append(sec)
    return sec


def _set_or_create_text_in_order(parent: etree._Element, local: str, value: str) -> bool:
    """Write/replace ``<local>value</local>`` under ``parent`` respecting
    schema sibling order. Returns True iff the value actually changed
    (used by callers that surface a ``changed`` list/bool)."""
    existing = find_child(parent, local)
    if existing is not None:
        if existing.text == value:
            return False
        existing.text = value
        return True
    new_node = etree.Element(q(local))
    new_node.text = value
    new_idx = _PAGE_SECTION_CHILD_ORDER.index(local)
    for i, child in enumerate(list(parent)):
        child_local = etree.QName(child).localname
        if (
            child_local in _PAGE_SECTION_CHILD_ORDER
            and _PAGE_SECTION_CHILD_ORDER.index(child_local) > new_idx
        ):
            parent.insert(i, new_node)
            return True
    parent.append(new_node)
    return True


def _ensure_report_items(section: etree._Element) -> etree._Element:
    items = find_child(section, "ReportItems")
    if items is not None:
        return items
    items = etree.Element(q("ReportItems"))
    new_idx = _PAGE_SECTION_CHILD_ORDER.index("ReportItems")
    for i, child in enumerate(list(section)):
        child_local = etree.QName(child).localname
        if (
            child_local in _PAGE_SECTION_CHILD_ORDER
            and _PAGE_SECTION_CHILD_ORDER.index(child_local) > new_idx
        ):
            section.insert(i, items)
            return items
    section.append(items)
    return items


# ---- band sizing (#50) ----------------------------------------------------


def _inches(node: Optional[etree._Element]) -> Optional[float]:
    """``node``'s text in inches, or ``None`` when absent / not a plain size."""
    if node is None:
        return None
    try:
        return units.to_inches((node.text or "").strip())
    except ValueError:
        return None


def _unit(node: Optional[etree._Element]) -> Optional[str]:
    if node is None:
        return None
    try:
        return units.parse_size((node.text or "").strip()).unit
    except ValueError:
        return None


def item_extent(item: etree._Element) -> tuple[Optional[float], Optional[float]]:
    """``(bottom, right)`` of a report item in inches — ``Top + Height`` and
    ``Left + Width`` (a Line's negative Height/Width points up/left, so it
    doesn't extend the item). A missing Top/Left counts as 0; a missing or
    non-literal Height/Width (or Top/Left expression) gives ``None`` for
    that edge.
    """

    def edge(origin: str, extent: str) -> Optional[float]:
        o_node = find_child(item, origin)
        o = 0.0 if o_node is None else _inches(o_node)
        e = _inches(find_child(item, extent))
        if o is None or e is None:
            return None
        return o + max(0.0, e)

    return edge("Top", "Height"), edge("Left", "Width")


def fit_band(
    page: etree._Element,
    section: etree._Element,
    item: etree._Element,
    *,
    grow: bool,
) -> tuple[list[str], Optional[str]]:
    """Check that ``item`` fits inside its page section ``section``.

    When the item's bottom passes the band's ``<Height>`` (or the band has
    none), ``grow=True`` rewrites Height to ``max(0.5in, bottom)`` — in the
    band's existing unit, else the item's Height unit — and returns it as
    the second element; ``grow=False`` returns a warning instead. A right
    edge past the page's printable width (``PageWidth`` minus margins)
    always just warns. Returns ``(warnings, new_height_or_None)``.
    """
    warnings: list[str] = []
    new_height: Optional[str] = None
    section_local = etree.QName(section).localname
    label = f"{etree.QName(item).localname} {item.get('Name')!r}"
    bottom, right = item_extent(item)
    height_node = find_child(section, "Height")
    band = _inches(height_node)
    if bottom is not None and (band is None or bottom > band + _EPS):
        if grow:
            floor = units.to_inches(DEFAULT_BAND_HEIGHT)
            if bottom <= floor + _EPS:
                new_height = DEFAULT_BAND_HEIGHT
            else:
                unit = _unit(height_node) or _unit(find_child(item, "Height")) or "in"
                new_height = units.format_size(bottom, unit)
            _set_or_create_text_in_order(section, "Height", new_height)
        else:
            have = (
                f"Height {(height_node.text or '').strip()!r}"
                if band is not None and height_node is not None
                else "no usable Height"
            )
            warnings.append(
                f"{label} ends at {units.format_size(bottom)} but <{section_local}> has "
                f"{have}, so the item is clipped. Pass grow_band=true, or set the band "
                "height with set_page_header / set_page_footer."
            )
    printable = printable_width_in(page)
    if right is not None and printable is not None and right > printable + _EPS:
        warnings.append(
            f"{label} ends at {units.format_size(right)} from the left, past the page's "
            f"printable width of {units.format_size(printable)} (PageWidth minus margins)."
        )
    return warnings, new_height


def _lowest_item_bottom(section: etree._Element) -> Optional[float]:
    items = find_child(section, "ReportItems")
    if items is None:
        return None
    extents = (item_extent(el) for el in items if isinstance(el.tag, str))
    bottoms = [b for b, _ in extents if b is not None]
    return max(bottoms) if bottoms else None


# ---- builders -------------------------------------------------------------


def _build_textbox(
    name: str,
    text: str,
    top: str,
    left: str,
    width: str,
    height: str,
) -> etree._Element:
    tb = etree.Element(q("Textbox"), Name=name)
    etree.SubElement(tb, q("CanGrow")).text = "true"
    etree.SubElement(tb, q("KeepTogether")).text = "true"
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    paragraph = etree.SubElement(paragraphs, q("Paragraph"))
    textruns = etree.SubElement(paragraph, q("TextRuns"))
    textrun = etree.SubElement(textruns, q("TextRun"))
    value = etree.SubElement(textrun, q("Value"))
    value.text = encode_text(text)
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    default_name = etree.SubElement(tb, qrd("DefaultName"))
    default_name.text = name
    etree.SubElement(tb, q("Top")).text = top
    etree.SubElement(tb, q("Left")).text = left
    etree.SubElement(tb, q("Height")).text = height
    etree.SubElement(tb, q("Width")).text = width
    etree.SubElement(tb, q("Style"))
    return tb


def _build_image(
    name: str,
    image_source: str,
    value: str,
    top: str,
    left: str,
    width: str,
    height: str,
    mime_type: Optional[str] = None,
) -> etree._Element:
    img = etree.Element(q("Image"), Name=name)
    etree.SubElement(img, q("Source")).text = image_source
    etree.SubElement(img, q("Value")).text = encode_text(value)
    if mime_type is not None:
        # Required for Source=Database (#79); sits right after Value.
        etree.SubElement(img, q("MIMEType")).text = encode_text(mime_type)
    etree.SubElement(img, q("Sizing")).text = "FitProportional"
    etree.SubElement(img, q("Top")).text = top
    etree.SubElement(img, q("Left")).text = left
    etree.SubElement(img, q("Height")).text = height
    etree.SubElement(img, q("Width")).text = width
    etree.SubElement(img, q("Style"))
    return img


# ---- region-specific public tools ----------------------------------------


def _set_section(
    path: str,
    region: _Region,
    height: Optional[str],
    print_on_first_page: Optional[bool],
    print_on_last_page: Optional[bool],
) -> dict[str, Any]:
    height = units.dimension(height, "height")
    doc = RDLDocument.open(path)
    page = _resolve_page(doc)
    section = _ensure_page_section(page, region)
    changed: list[str] = []
    warnings: list[str] = []
    if height is not None and _set_or_create_text_in_order(section, "Height", height):
        changed.append("Height")
    if find_child(section, "Height") is None:
        # RDL requires Height on a page section (#50); the XSD's choice
        # model lets a flags-only section through, Report Builder doesn't.
        _set_or_create_text_in_order(section, "Height", DEFAULT_BAND_HEIGHT)
        changed.append("Height")
    if height is not None:
        lowest = _lowest_item_bottom(section)
        if lowest is not None and lowest > units.to_inches(height) + _EPS:
            warnings.append(
                f"<{region.section_local}> Height {height!r} is shorter than its items, "
                f"which reach {units.format_size(lowest)}; they will be clipped."
            )
    if print_on_first_page is not None:
        text = "true" if print_on_first_page else "false"
        if _set_or_create_text_in_order(section, "PrintOnFirstPage", text):
            changed.append("PrintOnFirstPage")
    if print_on_last_page is not None:
        text = "true" if print_on_last_page else "false"
        if _set_or_create_text_in_order(section, "PrintOnLastPage", text):
            changed.append("PrintOnLastPage")
    if changed:
        doc.save()
    result: dict[str, Any] = {"path": str(doc.path), "region": region.name, "changed": changed}
    if warnings:
        result["warnings"] = warnings
    return result


def _place_item(
    doc: RDLDocument, region: _Region, item: etree._Element, name: str
) -> dict[str, Any]:
    """Append ``item`` to the region's band, growing the band to fit it
    (``max(0.5in, top + height)``), and save. Shared by the add tools."""
    page = _resolve_page(doc)
    section = _ensure_page_section(page, region)
    existing = find_child(section, "Height")
    old_height = existing.text if existing is not None else None
    _ensure_report_items(section).append(item)
    warnings, grown = fit_band(page, section, item, grow=True)
    height_node = find_child(section, "Height")
    if height_node is None:
        # Item size not a plain literal: still never leave a band without Height.
        _set_or_create_text_in_order(section, "Height", DEFAULT_BAND_HEIGHT)
        height_node = find_child(section, "Height")
    if grown is not None and old_height is not None:
        warnings.insert(
            0,
            f"<{region.section_local}> Height grown from {old_height!r} to {grown!r} "
            f"to fit {name!r}.",
        )
    doc.save()
    result: dict[str, Any] = {
        "region": region.name,
        "name": name,
        "band_height": height_node.text,
    }
    if warnings:
        result["warnings"] = warnings
    return result


def _add_textbox(
    path: str,
    region: _Region,
    name: str,
    text: str,
    top: str,
    left: str,
    width: str,
    height: str,
) -> dict[str, Any]:
    names.validate_identifier(name, "textbox")
    top, left, width, height = units.box(top, left, width, height)
    doc = RDLDocument.open(path)
    names.ensure_unique(doc, name)
    return _place_item(doc, region, _build_textbox(name, text, top, left, width, height), name)


def _add_image(
    path: str,
    region: _Region,
    name: str,
    image_source: str,
    value: str,
    top: str,
    left: str,
    width: str,
    height: str,
    mime_type: Optional[str] = None,
) -> dict[str, Any]:
    # Source / Value / MIMEType rules shared with set_image_source (#79).
    mime_type = check_image_source(image_source, value, mime_type)
    names.validate_identifier(name, "image")
    top, left, width, height = units.box(top, left, width, height)
    doc = RDLDocument.open(path)
    names.ensure_unique(doc, name)
    img = _build_image(name, image_source, value, top, left, width, height, mime_type)
    return _place_item(doc, region, img, name)


def resolve_removable_item(doc: RDLDocument, region: str, name: str) -> etree._Element:
    """The item called ``name`` in ``region`` — at its top level or nested in
    a Rectangle / List cell (#83) — for the ``remove_*_item`` tools. A
    tablix cell's own content is refused: a cell must hold one item."""
    item, container, _region = resolve_report_item(doc, name, region=region)
    if etree.QName(container).localname == "CellContents":
        raise ValueError(
            f"{etree.QName(item).localname} {name!r} fills a tablix cell, which must hold "
            "an item; remove or rebuild the tablix (or its column) instead"
        )
    return item


def detach_item(item: etree._Element) -> etree._Element:
    """Remove ``item`` from its ``<ReportItems>``, dropping that element when
    it is left empty. Returns the element that held it (Body, PageHeader,
    PageFooter or Rectangle)."""
    items = item.getparent()
    holder = items.getparent()
    items.remove(item)
    if len(list(items)) == 0:
        holder.remove(items)
    return holder


def _reference_label(ref: expr_walk.InboundReference) -> str:
    if ref.span is not None:
        return f"{ref.text} in {ref.locator}"
    return f"{ref.locator} = {ref.text!r}"


def removal_impact(doc: RDLDocument, target: etree._Element, *, force: bool) -> dict[str, Any]:
    """What removing ``target`` takes with it and leaves dangling (#105):
    ``{also_removed, dangling_refs}``. ``also_removed`` names the report
    items and groups inside it; ``dangling_refs`` lists every reference from
    outside the subtree to it or one of those (``ReportItems!X``,
    ``ToggleItem``, scope strings, ``RepeatWith``, UserSort targets — see
    :func:`pbirb_mcp.ops.expr_walk.find_inbound_references`) as
    ``{kind, name, location, text}``. Raises ``ValueError`` listing them
    unless ``force``. Call before mutating."""
    subtree = [el.get("Name") for el in names.iter_scope_elements(target)]
    inside = set(target.iter())
    refs = [
        r
        for r in expr_walk.find_inbound_references(doc.root, set(subtree))
        if r.element not in inside
    ]
    if refs and not force:
        listed = "; ".join(_reference_label(r) for r in refs)
        inner = " (or an item or group inside it)" if len(subtree) > 1 else ""
        raise ValueError(
            f"{etree.QName(target).localname} {target.get('Name')!r}{inner} is still "
            f"referenced: {listed}. Re-point or remove those references "
            "first, or pass force=true to remove it anyway and leave them dangling "
            "(lint_report then flags them)."
        )
    return {
        "also_removed": [n for n in subtree[1:] if n],
        "dangling_refs": [
            {"kind": r.kind, "name": r.name, "location": r.locator, "text": r.text} for r in refs
        ],
    }


def _remove_item(path: str, region: _Region, name: str, force: bool = False) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    target = resolve_removable_item(doc, region.name, name)
    kind = etree.QName(target).localname
    impact = removal_impact(doc, target, force=force)
    holder = detach_item(target)
    doc.save()
    result: dict[str, Any] = {"region": region.name, "removed": name, "kind": kind}
    if etree.QName(holder).localname == "Rectangle":
        result["parent"] = holder.get("Name")
    result.update(impact)
    return result


# ---- public per-region wrappers (the actual tools) ------------------------


def set_page_header(
    path: str,
    height: Optional[str] = None,
    print_on_first_page: Optional[bool] = None,
    print_on_last_page: Optional[bool] = None,
) -> dict[str, Any]:
    return _set_section(path, HEADER, height, print_on_first_page, print_on_last_page)


def set_page_footer(
    path: str,
    height: Optional[str] = None,
    print_on_first_page: Optional[bool] = None,
    print_on_last_page: Optional[bool] = None,
) -> dict[str, Any]:
    return _set_section(path, FOOTER, height, print_on_first_page, print_on_last_page)


def add_header_textbox(
    path: str,
    name: str,
    text: str,
    top: str,
    left: str,
    width: str,
    height: str,
) -> dict[str, Any]:
    return _add_textbox(path, HEADER, name, text, top, left, width, height)


def add_footer_textbox(
    path: str,
    name: str,
    text: str,
    top: str,
    left: str,
    width: str,
    height: str,
) -> dict[str, Any]:
    return _add_textbox(path, FOOTER, name, text, top, left, width, height)


def add_header_image(
    path: str,
    name: str,
    image_source: str,
    value: str,
    top: str,
    left: str,
    width: str,
    height: str,
    mime_type: Optional[str] = None,
) -> dict[str, Any]:
    return _add_image(path, HEADER, name, image_source, value, top, left, width, height, mime_type)


def add_footer_image(
    path: str,
    name: str,
    image_source: str,
    value: str,
    top: str,
    left: str,
    width: str,
    height: str,
    mime_type: Optional[str] = None,
) -> dict[str, Any]:
    return _add_image(path, FOOTER, name, image_source, value, top, left, width, height, mime_type)


def remove_header_item(path: str, name: str, force: bool = False) -> dict[str, Any]:
    return _remove_item(path, HEADER, name, force)


def remove_footer_item(path: str, name: str, force: bool = False) -> dict[str, Any]:
    return _remove_item(path, FOOTER, name, force)


__all__ = [
    "DEFAULT_BAND_HEIGHT",
    "add_footer_image",
    "add_footer_textbox",
    "add_header_image",
    "add_header_textbox",
    "fit_band",
    "item_extent",
    "removal_impact",
    "remove_footer_item",
    "remove_header_item",
    "set_page_footer",
    "set_page_header",
]
