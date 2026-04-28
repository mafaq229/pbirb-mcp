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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import find_child, q, qrd
from pbirb_mcp.ops.page import _resolve_page  # type: ignore[attr-defined]

_VALID_IMAGE_SOURCES = ("External", "Embedded", "Database")


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


def _names_in_section(section: etree._Element) -> set[str]:
    items = find_child(section, "ReportItems")
    if items is None:
        return set()
    return {el.get("Name") for el in list(items) if el.get("Name") is not None}


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
    value.text = text
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
) -> etree._Element:
    img = etree.Element(q("Image"), Name=name)
    etree.SubElement(img, q("Source")).text = image_source
    etree.SubElement(img, q("Value")).text = value
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
    doc = RDLDocument.open(path)
    page = _resolve_page(doc)
    section = _ensure_page_section(page, region)
    changed: list[str] = []
    if height is not None and _set_or_create_text_in_order(section, "Height", height):
        changed.append("Height")
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
    return {"path": str(doc.path), "region": region.name, "changed": changed}


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
    doc = RDLDocument.open(path)
    page = _resolve_page(doc)
    section = _ensure_page_section(page, region)
    # Default Height keeps Report Builder from rendering a 0in band when the
    # caller adds an item without first calling set_page_header/footer.
    if find_child(section, "Height") is None:
        _set_or_create_text_in_order(section, "Height", "0.5in")
    items = _ensure_report_items(section)
    if name in _names_in_section(section):
        raise ValueError(f"item named {name!r} already exists in {region.section_local}")
    items.append(_build_textbox(name, text, top, left, width, height))
    doc.save()
    return {"region": region.name, "name": name}


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
) -> dict[str, Any]:
    if image_source not in _VALID_IMAGE_SOURCES:
        raise ValueError(
            f"image_source must be one of {_VALID_IMAGE_SOURCES!r}; got {image_source!r}"
        )
    doc = RDLDocument.open(path)
    page = _resolve_page(doc)
    section = _ensure_page_section(page, region)
    if find_child(section, "Height") is None:
        _set_or_create_text_in_order(section, "Height", "0.5in")
    items = _ensure_report_items(section)
    if name in _names_in_section(section):
        raise ValueError(f"item named {name!r} already exists in {region.section_local}")
    items.append(_build_image(name, image_source, value, top, left, width, height))
    doc.save()
    return {"region": region.name, "name": name}


def _remove_item(path: str, region: _Region, name: str) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    page = _resolve_page(doc)
    section = find_child(page, region.section_local)
    items = find_child(section, "ReportItems") if section is not None else None
    target = None
    if items is not None:
        for el in list(items):
            if el.get("Name") == name:
                target = el
                break
    if target is None:
        raise ElementNotFoundError(f"no item named {name!r} in {region.section_local}")
    items.remove(target)
    if len(list(items)) == 0:
        section.remove(items)
    doc.save()
    return {"region": region.name, "removed": name}


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
) -> dict[str, Any]:
    return _add_image(path, HEADER, name, image_source, value, top, left, width, height)


def add_footer_image(
    path: str,
    name: str,
    image_source: str,
    value: str,
    top: str,
    left: str,
    width: str,
    height: str,
) -> dict[str, Any]:
    return _add_image(path, FOOTER, name, image_source, value, top, left, width, height)


def remove_header_item(path: str, name: str) -> dict[str, Any]:
    return _remove_item(path, HEADER, name)


def remove_footer_item(path: str, name: str) -> dict[str, Any]:
    return _remove_item(path, FOOTER, name)


__all__ = [
    "add_footer_image",
    "add_footer_textbox",
    "add_header_image",
    "add_header_textbox",
    "remove_footer_item",
    "remove_header_item",
    "set_page_footer",
    "set_page_header",
]
