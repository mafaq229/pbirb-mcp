"""Page-setup tools.

Edits the first ``<ReportSection>/<Page>`` block. Multi-section reports
are vanishingly rare in PBI paginated and the plan deliberately scopes
us to one section.

``set_page_setup`` is partial: only fields the caller passes get written.
The ``columns`` argument writes ``<Columns>`` when > 1 and removes it
when = 1, matching Report Builder's convention (single-column is the
implicit default and Report Builder strips the element on save).

``set_page_orientation`` is a thin helper: it parses ``PageHeight`` and
``PageWidth`` into a common unit (inches), compares, and swaps when the
current orientation doesn't match the requested one. Idempotent when it
already matches.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, find_child, q

_VALID_ORIENTATIONS = ("Portrait", "Landscape")

# Per RDL XSD, the order of <Page> children is:
#   PageHeader?, PageFooter?, PageHeight?, PageWidth?, InteractiveHeight?,
#   InteractiveWidth?, LeftMargin?, RightMargin?, TopMargin?, BottomMargin?,
#   Columns?, ColumnSpacing?, Style?
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


# Regex for an RDL size string: a positive number plus an explicit unit.
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(in|cm|mm|pt|pc)\s*$")

# Conversion factors to inches.
_TO_INCHES = {
    "in": 1.0,
    "cm": 1.0 / 2.54,
    "mm": 1.0 / 25.4,
    "pt": 1.0 / 72.0,
    "pc": 1.0 / 6.0,
}


def _resolve_page(doc: RDLDocument) -> etree._Element:
    page = doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Page")
    if page is None:
        # Defensive — every PBI Report Builder export has one ReportSection.Page.
        raise ValueError("report has no <ReportSection>/<Page> block")
    return page


def _set_or_create_text_in_order(page: etree._Element, local: str, value: str) -> None:
    existing = find_child(page, local)
    if existing is not None:
        existing.text = value
        return

    new_node = etree.Element(q(local))
    new_node.text = value
    new_idx = _PAGE_CHILD_ORDER.index(local)
    for i, child in enumerate(list(page)):
        child_local = etree.QName(child).localname
        if child_local in _PAGE_CHILD_ORDER and _PAGE_CHILD_ORDER.index(child_local) > new_idx:
            page.insert(i, new_node)
            return
    page.append(new_node)


def _remove_if_present(page: etree._Element, local: str) -> None:
    existing = find_child(page, local)
    if existing is not None:
        page.remove(existing)


def _parse_size_to_inches(size: str) -> float:
    m = _SIZE_RE.match(size or "")
    if not m:
        raise ValueError(
            f"invalid RDL size {size!r}; expected '<number><unit>' "
            "with unit in (in, cm, mm, pt, pc)"
        )
    return float(m.group(1)) * _TO_INCHES[m.group(2)]


# ---- set_page_setup -------------------------------------------------------


def set_page_setup(
    path: str,
    page_height: Optional[str] = None,
    page_width: Optional[str] = None,
    margin_top: Optional[str] = None,
    margin_bottom: Optional[str] = None,
    margin_left: Optional[str] = None,
    margin_right: Optional[str] = None,
    columns: Optional[int] = None,
) -> dict[str, Any]:
    updates: list[tuple[str, Optional[str]]] = [
        ("PageHeight", page_height),
        ("PageWidth", page_width),
        ("TopMargin", margin_top),
        ("BottomMargin", margin_bottom),
        ("LeftMargin", margin_left),
        ("RightMargin", margin_right),
    ]
    if all(v is None for _, v in updates) and columns is None:
        # No-op: don't touch the file at all so the round-trip stays
        # byte-identical for callers that pass nothing.
        return {"path": path, "changed": []}

    doc = RDLDocument.open(path)
    page = _resolve_page(doc)

    changed: list[str] = []
    for local, value in updates:
        if value is None:
            continue
        _set_or_create_text_in_order(page, local, value)
        changed.append(local)

    if columns is not None:
        if columns < 1:
            raise ValueError("columns must be >= 1")
        if columns == 1:
            _remove_if_present(page, "Columns")
        else:
            _set_or_create_text_in_order(page, "Columns", str(columns))
        changed.append("Columns")

    doc.save()
    return {"path": str(doc.path), "changed": changed}


# ---- set_page_orientation -------------------------------------------------


def set_page_orientation(path: str, orientation: str) -> dict[str, Any]:
    if orientation not in _VALID_ORIENTATIONS:
        raise ValueError(f"orientation must be one of {_VALID_ORIENTATIONS!r}; got {orientation!r}")

    doc = RDLDocument.open(path)
    page = _resolve_page(doc)
    h_node = find_child(page, "PageHeight")
    w_node = find_child(page, "PageWidth")
    if h_node is None or w_node is None:
        raise ValueError("page has no PageHeight/PageWidth to orient")

    h_in = _parse_size_to_inches(h_node.text or "")
    w_in = _parse_size_to_inches(w_node.text or "")
    is_currently_landscape = w_in > h_in
    wants_landscape = orientation == "Landscape"

    if is_currently_landscape == wants_landscape:
        return {
            "path": str(doc.path),
            "orientation": orientation,
            "changed": False,
        }

    h_node.text, w_node.text = w_node.text, h_node.text
    doc.save()
    return {
        "path": str(doc.path),
        "orientation": orientation,
        "changed": True,
        "page_height": h_node.text,
        "page_width": w_node.text,
    }


# ---- set_body_size (v0.4 commit 20) --------------------------------------


# Per RDL XSD, <Body> contains <Height> (and <ReportItems> + <Style>).
# The body's <Width> is a SIBLING of <Body> inside <ReportSection> —
# the ReportSection itself has children {Body, Width, Page, ...}. Both
# control the rendering region; the asymmetry is a historical RDL quirk
# rather than something we can paper over.
_REPORT_SECTION_WIDTH_PRECEDED_BY = ("Body",)
_REPORT_SECTION_WIDTH_FOLLOWED_BY = ("Page",)


def _resolve_report_section(doc: RDLDocument) -> etree._Element:
    section = doc.root.find(f".//{{{RDL_NS}}}ReportSections/{{{RDL_NS}}}ReportSection")
    if section is None:
        raise ValueError("report has no <ReportSection>")
    return section


def _set_or_create_text_in_section(section: etree._Element, local: str, value: str) -> bool:
    """Set the text of a <ReportSection>/<local> element, creating it in
    the right slot if absent. Returns True iff the value changed."""
    node = find_child(section, local)
    if node is not None:
        if node.text == value:
            return False
        node.text = value
        return True
    node = etree.Element(q(local))
    # Place at the canonical RDL position. <Width> goes after <Body>,
    # before <Page>; we walk both lists to find the right anchor.
    for follower_local in _REPORT_SECTION_WIDTH_FOLLOWED_BY:
        anchor = find_child(section, follower_local)
        if anchor is not None:
            anchor.addprevious(node)
            node.text = value
            return True
    for preceder_local in _REPORT_SECTION_WIDTH_PRECEDED_BY:
        anchor = find_child(section, preceder_local)
        if anchor is not None:
            anchor.addnext(node)
            node.text = value
            return True
    section.append(node)
    node.text = value
    return True


def set_body_size(
    path: str,
    width: Optional[str] = None,
    height: Optional[str] = None,
) -> dict[str, Any]:
    """Set the body's rendering region — distinct from page chrome.

    ``<Body>/<Height>`` and ``<ReportSection>/<Width>`` (the body's
    width sibling) are the canonical RDL elements for the body's
    rendering bounds inside the page. ``set_page_setup`` touches
    ``<Page>/<PageWidth>`` and ``<Page>/<PageHeight>`` (the paper
    size); ``set_body_item_size`` touches the size of items INSIDE
    the body. ``set_body_size`` is the missing third tool that the
    2026-05-11 matrix-report session needed for a 16in landscape
    page where the default 5in body width clipped the matrix.

    Either or both kwargs must be supplied. Returns the canonical
    mutator shape ``{kind: 'Body', changed: list[str]}``. ``changed``
    lists ``'Width'`` / ``'Height'`` for whichever element was
    rewritten (idempotent: same value → empty ``changed``).
    """
    if width is None and height is None:
        raise ValueError("set_body_size requires at least one of width / height")

    doc = RDLDocument.open(path)
    section = _resolve_report_section(doc)

    changed: list[str] = []

    if width is not None and _set_or_create_text_in_section(section, "Width", width):
        changed.append("Width")

    if height is not None:
        body = find_child(section, "Body")
        if body is None:
            raise ValueError("ReportSection has no <Body> element")
        h_node = find_child(body, "Height")
        if h_node is None:
            # Per BodyType XSD, Height comes after ReportItems and
            # before Style. Use the existing helper from page.py
            # set_page_setup, which knows the canonical child-order.
            h_node = etree.Element(q("Height"))
            # Place before Style (the only sibling that comes after
            # Height in BodyType).
            style_anchor = find_child(body, "Style")
            if style_anchor is not None:
                style_anchor.addprevious(h_node)
            else:
                body.append(h_node)
        if (h_node.text or "") != height:
            h_node.text = height
            changed.append("Height")

    if changed:
        doc.save()

    return {"kind": "Body", "changed": changed}


__all__ = ["set_body_size", "set_page_orientation", "set_page_setup"]
