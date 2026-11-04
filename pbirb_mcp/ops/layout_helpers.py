"""Geometry fix-up helpers (#49): fit the page to the body, centre an item.

They pair with the geometry lint rules (:mod:`pbirb_mcp.ops.lint_rules.geometry`):

* ``fit_page_to_body(mode="page")`` clears ``body-wider-than-page`` by
  widening ``<PageWidth>`` to ``columns x body + column spacing +
  margins``; ``mode="body"`` instead shrinks the body width
  (``<ReportSection>/<Width>``) to the right edge of its rightmost item,
  and refuses when even that does not fit the printable width.
* ``center_item`` sets an item's ``<Left>`` (and/or ``<Top>``) so it sits
  in the middle of its container: the body width for body items, the
  printable width (page width minus margins) for page header / footer
  items, the Rectangle for items inside one.

All size math is in inches through :mod:`pbirb_mcp.core.units`; values
are written back in the unit of the element they replace (or of the
item's Width), so a centimetre report stays in centimetres.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.geometry import (
    TOLERANCE_IN,
    SectionGeometry,
    child_items,
    item_box,
    iter_containers,
    length_in,
    local_name,
    report_sections,
    section_geometry,
    size_unit,
    tablix_columns_width,
)
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.units import MAX_SIZE_IN, format_size, to_inches
from pbirb_mcp.core.xpath import find_child
from pbirb_mcp.ops import page as _page
from pbirb_mcp.ops.positioning import _set_layout_value

_FIT_MODES = ("page", "body")
_AXES = ("horizontal", "vertical", "both")


def _first_section(doc: RDLDocument) -> etree._Element:
    sections = report_sections(doc.root)
    if not sections:
        raise ValueError("report has no <ReportSection>")
    return sections[0]


def _text_of(parent: Optional[etree._Element], local: str) -> Optional[str]:
    node = find_child(parent, local) if parent is not None else None
    return node.text if node is not None else None


def _fmt(inches: float, unit: str) -> str:
    # Round away float noise (8.499999999 -> 8.5) before rendering.
    return format_size(round(inches, 6), unit)


def _summary(geom: SectionGeometry, section: etree._Element) -> dict[str, Any]:
    page = find_child(section, "Page")
    page_unit = size_unit(_text_of(page, "PageWidth"))
    body_unit = size_unit(_text_of(section, "Width"), page_unit)
    return {
        "page_width": _fmt(geom.page_width, page_unit),
        "body_width": None if geom.body_width is None else _fmt(geom.body_width, body_unit),
        "printable_width": _fmt(geom.printable_width, page_unit),
    }


# ---- fit_page_to_body -------------------------------------------------------


def fit_page_to_body(path: str, mode: str = "page") -> dict[str, Any]:
    """Make the body fit the page width (clears ``body-wider-than-page``).

    ``mode="page"`` widens ``<PageWidth>`` to the width the body needs
    (``columns x body width + column spacing x (columns - 1) + left +
    right margins``); a page that is already wide enough is left alone.

    ``mode="body"`` shrinks the body width to the right edge of its
    rightmost top-level item; raises ``ValueError`` when that edge is still
    wider than one page column of the printable width (move or resize the
    items, or use ``mode="page"``). A body that already fits is left alone.

    Returns ``{mode, changed, page_width, body_width, printable_width}``
    (the values after the call).
    """
    if mode not in _FIT_MODES:
        raise ValueError(f"mode must be one of {_FIT_MODES}, got {mode!r}")
    doc = RDLDocument.open(path)
    section = _first_section(doc)
    geom = section_geometry(section)
    if geom.body_width is None:
        raise ValueError("the report section has no valid <Width> (body width)")
    page = find_child(section, "Page")
    page_unit = size_unit(_text_of(page, "PageWidth"))

    changed = False
    if mode == "page":
        required = geom.required_page_width
        assert required is not None
        if required > geom.page_width + TOLERANCE_IN:
            if required > MAX_SIZE_IN:
                raise ValueError(
                    f"the body needs a {_fmt(required, 'in')} page; RDL caps page "
                    f"width at {MAX_SIZE_IN:g}in — use mode='body' or narrow the body"
                )
            _page.set_page_setup(path, page_width=_fmt(required, page_unit))
            changed = True
    else:
        available = geom.available_body_width
        if geom.body_width > available + TOLERANCE_IN:
            body = find_child(section, "Body")
            boxes = [b for b in (item_box(i) for i in child_items(body)) if b is not None]
            rightmost = max((b.right for b in boxes), default=available)
            if rightmost > available + TOLERANCE_IN:
                raise ValueError(
                    f"the body's items reach {_fmt(rightmost, 'in')} but only "
                    f"{_fmt(available, 'in')} fits the page "
                    f"(printable width {_fmt(geom.printable_width, 'in')}"
                    + (f", {geom.columns} columns" if geom.columns > 1 else "")
                    + "); move or resize the items, or use mode='page'"
                )
            body_unit = size_unit(_text_of(section, "Width"), page_unit)
            _page.set_body_size(path, width=_fmt(rightmost, body_unit))
            changed = True

    if changed:
        doc = RDLDocument.open(path)
        section = _first_section(doc)
        geom = section_geometry(section)
    return {"mode": mode, "changed": changed, **_summary(geom, section)}


# ---- center_item ------------------------------------------------------------


def _locate(
    doc: RDLDocument, name: str
) -> tuple[etree._Element, etree._Element, Optional[float], Optional[float]]:
    """``(item, container, container_width, container_height)`` for the
    positioned item called ``name`` anywhere in the body / header / footer
    (including inside Rectangles)."""
    for section in report_sections(doc.root):
        for container, width, height in iter_containers(section):
            for item in child_items(container):
                if item.get("Name") == name:
                    return item, container, width, height
    raise ElementNotFoundError(
        f"no positioned report item named {name!r} in the body, page header or "
        "page footer (tablix-cell contents are placed by their cell)"
    )


def _container_label(container: etree._Element) -> str:
    local = local_name(container)
    if local == "Rectangle":
        return f"rectangle:{container.get('Name')}"
    return {"Body": "body", "PageHeader": "header", "PageFooter": "footer"}[local]


def center_item(path: str, name: str, axis: str = "horizontal") -> dict[str, Any]:
    """Centre a named report item in its container.

    ``axis="horizontal"`` sets ``Left = (container width - item width) / 2``;
    ``"vertical"`` sets ``Top`` the same way from the heights; ``"both"``
    does both. The container is the body (section Width / Body Height) for
    body items, the printable width (page width minus margins) and band
    Height for page header / footer items, and the Rectangle for items
    inside one. A Tablix is centred on the width it renders at (its column
    widths plus row headers), which differs from its ``<Width>`` only when
    ``tablix-width-mismatch`` fires — ``warnings`` then says so. Raises
    ``ValueError`` when the item is larger than its
    container on that axis. Idempotent: ``changed: false`` (no save) when
    the item is already centred.

    Returns ``{name, container, axis, top, left, changed, warnings}``.
    """
    if axis not in _AXES:
        raise ValueError(f"axis must be one of {_AXES}, got {axis!r}")
    doc = RDLDocument.open(path)
    item, container, c_width, c_height = _locate(doc, name)
    box = item_box(item)
    if box is None:
        raise ValueError(f"item {name!r} has no valid Width / Height to centre with")
    label = _container_label(container)

    # (element, container size, item extent, item start offset in the box,
    #  unit) per axis. A Line's Width/Height is a vector: when negative,
    #  Left/Top is the box's far edge.
    plans: list[tuple[str, Optional[float], float, float, str]] = []
    warnings: list[str] = []
    if axis in ("horizontal", "both"):
        width_text = _text_of(item, "Width")
        shift = box.width if width_text is not None and width_text.lstrip().startswith("-") else 0
        extent = box.width
        if local_name(item) == "Tablix":
            # A tablix renders at the width of its columns (+ row headers),
            # whatever its <Width> says — centre what is drawn.
            rendered = tablix_columns_width(item)
            if rendered is not None:
                if abs(rendered - box.width) > 0.01:
                    warnings.append(
                        f"Tablix {name!r} Width {width_text} differs from its column "
                        f"widths ({_fmt(rendered, size_unit(width_text))}); centred on "
                        "the column widths it renders at (tablix-width-mismatch)"
                    )
                extent = rendered
        plans.append(("Left", c_width, extent, shift, size_unit(width_text)))
    if axis in ("vertical", "both"):
        height_text = _text_of(item, "Height")
        shift = (
            box.height if height_text is not None and height_text.lstrip().startswith("-") else 0
        )
        plans.append(("Top", c_height, box.height, shift, size_unit(height_text)))

    writes: list[tuple[str, str]] = []
    for local, size, extent, shift, unit in plans:
        if size is None:
            raise ValueError(f"the {label} container has no valid size to centre {name!r} in")
        if extent > size + TOLERANCE_IN:
            raise ValueError(
                f"item {name!r} ({_fmt(extent, 'in')}) is larger than its {label} "
                f"container ({_fmt(size, 'in')}) on the {local} axis"
            )
        writes.append((local, _fmt(max(0.0, (size - extent) / 2) + shift, unit)))

    changed = False
    for local, value in writes:
        current = length_in(item, local) or 0.0
        if abs(current - to_inches(value)) < 1e-6:
            continue  # already centred ("0.5in" vs "0.50in" is no change)
        changed = _set_layout_value(item, local, value) or changed
    if changed:
        doc.save()
    return {
        "name": name,
        "container": label,
        "axis": axis,
        "top": _text_of(item, "Top") or "0in",
        "left": _text_of(item, "Left") or "0in",
        "changed": changed,
        "warnings": warnings,
    }


__all__ = ["center_item", "fit_page_to_body"]
