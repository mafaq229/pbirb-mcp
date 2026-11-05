"""Page-setup tools.

Edits the first ``<ReportSection>/<Page>`` block. Multi-section reports
are vanishingly rare in PBI paginated and the plan deliberately scopes
us to one section.

``set_page_setup`` is partial: only fields the caller passes get written.
The ``columns`` argument writes ``<Columns>`` when > 1 and removes it
when = 1, matching Report Builder's convention (single-column is the
implicit default and Report Builder strips the element on save).

``set_page_orientation`` is a thin helper: it parses ``PageHeight`` and
``PageWidth`` into a common unit (inches; an absent element is the RDL
default, 11in x 8.5in), compares, and swaps when the current orientation
doesn't match the requested one. Idempotent when it already matches.

Both tools report ``warnings`` (``body-wider-than-page``) when the body
no longer fits the page width after the change (#80).
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.geometry import PAGE_DEFAULTS, TOLERANCE_IN, section_geometry
from pbirb_mcp.core.mutation import remove_child_if_present, set_text_if_different
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


def _resolve_page(doc: RDLDocument) -> etree._Element:
    page = doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Page")
    if page is None:
        # Defensive — every PBI Report Builder export has one ReportSection.Page.
        raise ValueError("report has no <ReportSection>/<Page> block")
    return page


def printable_width_in(page: etree._Element) -> Optional[float]:
    """The page's printable width in inches: ``PageWidth - LeftMargin -
    RightMargin`` (#50). Missing margins count as 0; ``None`` when there
    is no ``<PageWidth>`` or a value isn't a plain RDL size (an
    expression), so callers skip the check rather than guess.
    """
    total = 0.0
    for local, sign, required in (
        ("PageWidth", 1.0, True),
        ("LeftMargin", -1.0, False),
        ("RightMargin", -1.0, False),
    ):
        node = find_child(page, local)
        if node is None:
            if required:
                return None
            continue
        try:
            total += sign * units.to_inches((node.text or "").strip())
        except ValueError:
            return None
    return total


def _set_or_create_text_in_order(page: etree._Element, local: str, value: str) -> bool:
    """Write ``<local>`` into ``<Page>`` in XSD order; ``True`` iff changed."""
    return set_text_if_different(page, local, value, order=_PAGE_CHILD_ORDER)


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
    page_height = units.dimension(page_height, "page_height")
    page_width = units.dimension(page_width, "page_width")
    margin_top = units.position(margin_top, "margin_top")
    margin_bottom = units.position(margin_bottom, "margin_bottom")
    margin_left = units.position(margin_left, "margin_left")
    margin_right = units.position(margin_right, "margin_right")
    if columns is not None and columns < 1:
        raise ValueError("columns must be >= 1")
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
        if value is not None and _set_or_create_text_in_order(page, local, value):
            changed.append(local)

    if columns is not None:
        if columns == 1:
            columns_changed = remove_child_if_present(page, "Columns")
        else:
            columns_changed = _set_or_create_text_in_order(page, "Columns", str(columns))
        if columns_changed:
            changed.append("Columns")

    if changed:
        doc.save()
    result: dict[str, Any] = {"path": str(doc.path), "changed": changed}
    # A narrower page, wider margins or more columns can push the body
    # past the page edge (#80); say so instead of a bare `changed`.
    if set(changed) & {"PageWidth", "LeftMargin", "RightMargin", "Columns"}:
        warnings = body_overflow_warnings(page)
        if warnings:
            result["warnings"] = warnings
    return result


# ---- body-overflow warnings (#80) ------------------------------------------


def body_overflow_warnings(page: etree._Element) -> list[dict[str, Any]]:
    """``[{"rule": "body-wider-than-page", ...}]`` when the body of
    ``page``'s ReportSection no longer fits the page width, else ``[]``.

    Same arithmetic as the ``body-wider-than-page`` lint rule (#49, via
    :func:`pbirb_mcp.core.geometry.section_geometry`, absent page
    elements at their RDL defaults): the body needs ``columns x body +
    spacing x (columns - 1) + margins``. The entry carries
    ``body_width``, ``printable_width`` (the widest body that fits one
    page column), ``overflow`` (how much wider than the page that is)
    and a ``suggestion``; sizes are inch strings. A body that overflows
    renders a mostly blank extra page after every page.
    """
    section = page.getparent()
    if section is None:
        return []
    geom = section_geometry(section)
    required = geom.required_page_width
    if required is None or required <= geom.page_width + TOLERANCE_IN:
        return []
    assert geom.body_width is not None
    entry: dict[str, Any] = {
        "rule": "body-wider-than-page",
        "body_width": units.format_size(geom.body_width),
        "printable_width": units.format_size(geom.available_body_width),
        "overflow": units.format_size(required - geom.page_width),
        "suggestion": (
            "fit_page_to_body(mode='body') shrinks the body to its content; "
            "fit_page_to_body(mode='page') widens the page; or set_body_size / "
            "set_page_setup."
        ),
    }
    if geom.columns > 1:
        entry["columns"] = geom.columns
    return [entry]


# ---- set_page_orientation -------------------------------------------------


def set_page_orientation(path: str, orientation: str) -> dict[str, Any]:
    """Swap ``PageHeight`` / ``PageWidth`` when the page's orientation
    isn't ``orientation`` (idempotent when it already is).

    A missing ``PageHeight`` / ``PageWidth`` is the RDL default (11in x
    8.5in, US Letter portrait — Report Builder omits elements equal to
    their default); when swapping, both are written explicitly.
    ``InteractiveHeight`` / ``InteractiveWidth`` are swapped too when
    both are present.

    Returns ``{path, orientation, changed, page_height, page_width}``,
    plus ``warnings`` (see :func:`body_overflow_warnings`) when the body
    is wider than the re-oriented page.
    """
    if orientation not in _VALID_ORIENTATIONS:
        raise ValueError(f"orientation must be one of {_VALID_ORIENTATIONS!r}; got {orientation!r}")

    doc = RDLDocument.open(path)
    page = _resolve_page(doc)
    sizes: dict[str, str] = {}
    for local in ("PageHeight", "PageWidth"):
        node = find_child(page, local)
        text = (node.text or "").strip() if node is not None else ""
        sizes[local] = node.text if text else PAGE_DEFAULTS[local]  # type: ignore[assignment]
        try:
            units.to_inches(sizes[local])
        except ValueError:
            raise ValueError(
                f"<{local}> is {sizes[local]!r}, not an RDL size; set it with set_page_setup "
                "before changing the orientation"
            ) from None
    is_currently_landscape = units.to_inches(sizes["PageWidth"]) > units.to_inches(
        sizes["PageHeight"]
    )
    wants_landscape = orientation == "Landscape"

    if is_currently_landscape == wants_landscape:
        return {
            "path": str(doc.path),
            "orientation": orientation,
            "changed": False,
            "page_height": sizes["PageHeight"],
            "page_width": sizes["PageWidth"],
        }

    new_height, new_width = sizes["PageWidth"], sizes["PageHeight"]
    _set_or_create_text_in_order(page, "PageHeight", new_height)
    _set_or_create_text_in_order(page, "PageWidth", new_width)
    ih, iw = find_child(page, "InteractiveHeight"), find_child(page, "InteractiveWidth")
    if ih is not None and iw is not None:
        ih.text, iw.text = iw.text, ih.text
    doc.save()
    result: dict[str, Any] = {
        "path": str(doc.path),
        "orientation": orientation,
        "changed": True,
        "page_height": new_height,
        "page_width": new_width,
    }
    warnings = body_overflow_warnings(page)
    if warnings:
        result["warnings"] = warnings
    return result


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
    width = units.dimension(width, "width")
    height = units.dimension(height, "height")

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


__all__ = [
    "body_overflow_warnings",
    "printable_width_in",
    "set_body_size",
    "set_page_orientation",
    "set_page_setup",
]
