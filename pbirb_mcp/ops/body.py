"""Body-composition tools.

Adds and removes named items in ``<ReportSection>/<Body>/<ReportItems>``.
The fixture's body already holds ``MainTable``; new textboxes and images
coexist with it. Builders are reused from :mod:`pbirb_mcp.ops.header_footer`
so a body textbox emits the same shape as a header textbox.

``remove_body_item`` is willing to remove tablixes too — that's the
explicit "redesign" workflow. It raises a clear error on unknown names so
typos don't silently delete the wrong item, reports what went with the
item, and refuses while other items still reference it (#105).

The add tools take ``container`` — a Rectangle in the body, a List's
``<name>_Rect`` included — to add inside it instead (#83,
:mod:`pbirb_mcp.ops.containers`); ``remove_body_item`` reaches items
nested in Rectangles and List cells.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.containers import place
from pbirb_mcp.ops.header_footer import (
    _build_image,
    _build_textbox,
    detach_item,
    removal_impact,
    resolve_removable_item,
)
from pbirb_mcp.ops.images import check_image_source


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
    container: Optional[str] = None,
) -> dict[str, Any]:
    """Add a Textbox to the body, or with ``container`` inside that body
    Rectangle (a List's ``<name>_Rect`` included); top / left are then
    relative to the rectangle (#83)."""
    names.validate_identifier(name, "textbox")
    top, left, width, height = units.box(top, left, width, height)
    doc = RDLDocument.open(path)
    names.ensure_unique(doc, name)
    textbox = _build_textbox(name, text, top, left, width, height)
    return place(doc, textbox, container, {"name": name, "kind": "Textbox"})


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
    container: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> dict[str, Any]:
    """Add an Image to the body, or inside the body Rectangle ``container``
    (see :func:`add_body_textbox`)."""
    # Source / Value / MIMEType rules shared with set_image_source (#79):
    # Database needs an =expression and a mime_type, the others refuse one.
    mime_type = check_image_source(image_source, value, mime_type)
    names.validate_identifier(name, "image")
    top, left, width, height = units.box(top, left, width, height)
    doc = RDLDocument.open(path)
    names.ensure_unique(doc, name)
    image = _build_image(name, image_source, value, top, left, width, height, mime_type)
    return place(doc, image, container, {"name": name, "kind": "Image"})


# ---- remove_body_item -----------------------------------------------------


def remove_body_item(path: str, name: str, force: bool = False) -> dict[str, Any]:
    """Remove the body item ``name`` — at the body's top level or nested in a
    Rectangle / List cell (#83) — with everything inside it.

    Returns ``{removed, kind, also_removed, dangling_refs}`` plus ``parent``
    (the Rectangle it left) for a nested item: ``also_removed`` names the
    report items and groups that went with it (a Rectangle's contents, a
    tablix's cell textboxes and groups). Refuses while anything outside the
    removed subtree still references it or them (``ReportItems!X``,
    ``ToggleItem``, a scope string, ``RepeatWith``, a UserSort target), listing
    each, unless ``force``: the references are then left in place and listed
    in ``dangling_refs`` (#105, :func:`header_footer.removal_impact`)."""
    doc = RDLDocument.open(path)
    target = resolve_removable_item(doc, "body", name)
    kind = etree.QName(target).localname
    impact = removal_impact(doc, target, force=force)
    holder = detach_item(target)
    doc.save()
    result: dict[str, Any] = {"removed": name, "kind": kind}
    if etree.QName(holder).localname == "Rectangle":
        result["parent"] = holder.get("Name")
    result.update(impact)
    return result


__all__ = ["add_body_image", "add_body_textbox", "remove_body_item"]
