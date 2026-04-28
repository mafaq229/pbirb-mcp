"""Body-composition tools.

Adds and removes named items in ``<ReportSection>/<Body>/<ReportItems>``.
The fixture's body already holds ``MainTable``; new textboxes and images
coexist with it. Builders are reused from :mod:`pbirb_mcp.ops.header_footer`
so a body textbox emits the same shape as a header textbox.

``remove_body_item`` is willing to remove tablixes too — that's the
explicit "redesign" workflow. It raises a clear error on unknown names so
typos don't silently delete the wrong item.
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.header_footer import (
    _VALID_IMAGE_SOURCES,
    _build_image,
    _build_textbox,
)


def _resolve_body(doc: RDLDocument) -> etree._Element:
    body = doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body")
    if body is None:
        raise ValueError("report has no <ReportSection>/<Body>")
    return body


def _ensure_body_report_items(body: etree._Element) -> etree._Element:
    items = find_child(body, "ReportItems")
    if items is not None:
        return items
    # Body's child order is ReportItems, Height, Style — insert before
    # whichever of those exists first.
    items = etree.Element(q("ReportItems"))
    height = find_child(body, "Height")
    if height is not None:
        height.addprevious(items)
        return items
    style = find_child(body, "Style")
    if style is not None:
        style.addprevious(items)
        return items
    body.insert(0, items)
    return items


def _names_in(items: etree._Element) -> set[str]:
    return {el.get("Name") for el in list(items) if el.get("Name") is not None}


# ---- add_body_textbox -----------------------------------------------------


def add_body_textbox(
    path: str,
    name: str,
    text: str,
    top: str,
    left: str,
    width: str,
    height: str,
) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    body = _resolve_body(doc)
    items = _ensure_body_report_items(body)
    if name in _names_in(items):
        raise ValueError(f"body item named {name!r} already exists")
    items.append(_build_textbox(name, text, top, left, width, height))
    doc.save()
    return {"name": name, "kind": "Textbox"}


# ---- add_body_image -------------------------------------------------------


def add_body_image(
    path: str,
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
    body = _resolve_body(doc)
    items = _ensure_body_report_items(body)
    if name in _names_in(items):
        raise ValueError(f"body item named {name!r} already exists")
    items.append(_build_image(name, image_source, value, top, left, width, height))
    doc.save()
    return {"name": name, "kind": "Image"}


# ---- remove_body_item -----------------------------------------------------


def remove_body_item(path: str, name: str) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    body = _resolve_body(doc)
    items = find_child(body, "ReportItems")
    target = None
    if items is not None:
        for el in list(items):
            if el.get("Name") == name:
                target = el
                break
    if target is None:
        raise ElementNotFoundError(f"no body item named {name!r}")
    kind = etree.QName(target).localname
    items.remove(target)
    if len(list(items)) == 0:
        body.remove(items)
    doc.save()
    return {"removed": name, "kind": kind}


__all__ = ["add_body_image", "add_body_textbox", "remove_body_item"]
