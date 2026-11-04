"""Geometry lint rules (#49): the layout defects that pass the XSD.

All four are warnings: Report Builder opens and renders such a file — it
just lays it out wrong (a blank page after every page, clipped or
shifted content, overlapping text) and itself only warns at preview
time. Sizes are compared in inches via :mod:`pbirb_mcp.core.geometry`
with a 0.001in tolerance; a size that does not parse is skipped (the
size validators report those).

* ``body-wider-than-page`` — ``columns x body Width + column spacing +
  left/right margins`` exceeds ``PageWidth`` (absent page values take
  their RDL defaults).
* ``item-out-of-bounds`` — an item's right or bottom edge lies outside
  its container: the Body's width (the section Width — past it the body
  silently widens, often past the page; the body grows downward to fit
  its items, so its Height is not a bound), the PageHeader / PageFooter
  (printable width x Height) or its Rectangle (Width x Height).
* ``overlapping-items`` — two positioned siblings in one container whose
  boxes intersect. Lines, and items with a Visibility (hidden, toggled
  or conditional — the usual show-one-of-two design), are ignored.
* ``tablix-width-mismatch`` — ``Tablix/Width`` differs from the width
  its columns render (Σ body column widths + row-header sizes) by more
  than 0.01in.
"""

from __future__ import annotations

from typing import Callable, Optional

from lxml import etree

from pbirb_mcp.core.geometry import (
    TOLERANCE_IN,
    child_items,
    item_box,
    iter_containers,
    length_in,
    local_name,
    report_sections,
    section_geometry,
    tablix_columns_width,
)
from pbirb_mcp.core.units import format_size
from pbirb_mcp.core.xpath import find_child, q
from pbirb_mcp.ops.lint import Issue, _ancestor_chain

# Tablix Width vs Σ column widths tolerance (the issue's threshold).
_TABLIX_WIDTH_TOLERANCE_IN = 0.01


def _fmt(inches: float) -> str:
    return format_size(round(inches, 4))


def _section_locator(index: int, total: int) -> str:
    return "ReportSection" if total == 1 else f"ReportSection[{index + 1}]"


def _container_label(container: etree._Element) -> str:
    local = local_name(container)
    if local == "Rectangle":
        return f"Rectangle {container.get('Name')!r}"
    return {"Body": "the body", "PageHeader": "the page header", "PageFooter": "the page footer"}[
        local
    ]


# ---- body-wider-than-page ---------------------------------------------------


def _rule_body_wider_than_page(doc) -> list[Issue]:
    issues: list[Issue] = []
    sections = report_sections(doc.root)
    for i, section in enumerate(sections):
        geom = section_geometry(section)
        required = geom.required_page_width
        if required is None or required <= geom.page_width + TOLERANCE_IN:
            continue
        assert geom.body_width is not None
        columns = (
            f"{geom.columns} columns x {_fmt(geom.body_width)} + "
            f"{geom.columns - 1} x {_fmt(geom.column_spacing)} column spacing"
            if geom.columns > 1
            else f"body width {_fmt(geom.body_width)}"
        )
        issues.append(
            {
                "severity": "warning",
                "rule": "body-wider-than-page",
                "location": _section_locator(i, len(sections)),
                "message": (
                    f"{columns} + margins {_fmt(geom.margin_left)} + "
                    f"{_fmt(geom.margin_right)} = {_fmt(required)} exceeds the page "
                    f"width {_fmt(geom.page_width)}; Report Builder renders the "
                    "overflow on extra (mostly blank) pages."
                ),
                "suggestion": (
                    "fit_page_to_body(mode='page') widens the page; "
                    "fit_page_to_body(mode='body') shrinks the body to its content; "
                    "or set_body_size / set_page_setup."
                ),
            }
        )
    return issues


# ---- item-out-of-bounds -----------------------------------------------------


def _rule_item_out_of_bounds(doc) -> list[Issue]:
    issues: list[Issue] = []
    for section in report_sections(doc.root):
        for container, width, height in iter_containers(section):
            if local_name(container) == "Body":
                # The body grows downward to hold its items (Report Builder
                # re-sizes <Body>/<Height> on open), so only its right edge
                # is a hard bound — past it the body silently widens.
                height = None
            for item in child_items(container):
                box = item_box(item)
                if box is None:
                    continue
                edges: list[str] = []
                if width is not None and box.right > width + TOLERANCE_IN:
                    edges.append(f"right edge {_fmt(box.right)} > width {_fmt(width)}")
                if height is not None and box.bottom > height + TOLERANCE_IN:
                    edges.append(f"bottom edge {_fmt(box.bottom)} > height {_fmt(height)}")
                if not edges:
                    continue
                issues.append(
                    {
                        "severity": "warning",
                        "rule": "item-out-of-bounds",
                        "location": _ancestor_chain(item),
                        "message": (
                            f"{local_name(item)} {item.get('Name')!r} extends outside "
                            f"{_container_label(container)} ({'; '.join(edges)})."
                        ),
                        "suggestion": (
                            "Move or resize the item (set_*_item_position / "
                            "set_*_item_size), or enlarge its container "
                            "(set_body_size, set_page_header / set_page_footer height)."
                        ),
                    }
                )
    return issues


# ---- overlapping-items ------------------------------------------------------


def _has_visibility(item: etree._Element) -> bool:
    """Hidden / toggled / conditionally visible — overlapping such an item
    is the usual show-one-of-two design, not a defect."""
    vis = find_child(item, "Visibility")
    if vis is None:
        return False
    if find_child(vis, "ToggleItem") is not None:
        return True
    hidden = find_child(vis, "Hidden")
    text = (hidden.text or "").strip() if hidden is not None else ""
    return bool(text) and text.lower() != "false"


def _rule_overlapping_items(doc) -> list[Issue]:
    issues: list[Issue] = []
    for section in report_sections(doc.root):
        for container, _w, _h in iter_containers(section):
            boxed = []
            for item in child_items(container):
                if local_name(item) == "Line" or _has_visibility(item):
                    continue
                box = item_box(item)
                if box is not None and box.width > 0 and box.height > 0:
                    boxed.append((item, box))
            for a in range(len(boxed)):
                item_a, box_a = boxed[a]
                for b in range(a + 1, len(boxed)):
                    item_b, box_b = boxed[b]
                    if not box_a.overlaps(box_b):
                        continue
                    issues.append(
                        {
                            "severity": "warning",
                            "rule": "overlapping-items",
                            "location": _ancestor_chain(item_b),
                            "message": (
                                f"{local_name(item_a)} {item_a.get('Name')!r} and "
                                f"{local_name(item_b)} {item_b.get('Name')!r} overlap in "
                                f"{_container_label(container)}; Report Builder renders "
                                "overlapping items unpredictably (and may push one aside)."
                            ),
                            "suggestion": (
                                "Move one of them (set_*_item_position), or give one a "
                                "Visibility if only one shows at a time."
                            ),
                        }
                    )
    return issues


# ---- tablix-width-mismatch --------------------------------------------------


def _rule_tablix_width_mismatch(doc) -> list[Issue]:
    issues: list[Issue] = []
    for tablix in doc.root.iter(q("Tablix")):
        declared: Optional[float] = length_in(tablix, "Width")
        if declared is None:
            continue
        actual = tablix_columns_width(tablix)
        if actual is None or abs(declared - actual) <= _TABLIX_WIDTH_TOLERANCE_IN:
            continue
        issues.append(
            {
                "severity": "warning",
                "rule": "tablix-width-mismatch",
                "location": _ancestor_chain(tablix),
                "message": (
                    f"Tablix {tablix.get('Name')!r} declares Width {_fmt(declared)} but "
                    f"its columns (and row headers) add up to {_fmt(actual)}; it renders "
                    "at the column width, so layout and centring computed from Width "
                    "are off."
                ),
                "suggestion": (
                    f"set_tablix_size(width='{_fmt(actual)}'), or adjust the columns "
                    "with set_column_width."
                ),
            }
        )
    return issues


RULES: dict[str, Callable[..., list[Issue]]] = {
    "body-wider-than-page": _rule_body_wider_than_page,
    "item-out-of-bounds": _rule_item_out_of_bounds,
    "overlapping-items": _rule_overlapping_items,
    "tablix-width-mismatch": _rule_tablix_width_mismatch,
}
