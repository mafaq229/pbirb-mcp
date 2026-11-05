"""Report-item and page geometry shared by the readers, lint and layout tools.

RDL positions every report item by ``<Top>`` / ``<Left>`` / ``<Width>`` /
``<Height>`` relative to its container (``<Body>``, ``<PageHeader>``,
``<PageFooter>`` or a ``<Rectangle>``). This module names the report-item
element tags once, holds the RDL defaults Report Builder applies when a
``<Page>`` size element is absent, and turns a ``<ReportSection>`` into
inch-based page / body geometry (#49) — all size math goes through
:mod:`pbirb_mcp.core.units`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Optional

from lxml import etree

from pbirb_mcp.core.units import format_size, parse_size, to_inches
from pbirb_mcp.core.xpath import find_child, q

# Every RDL 2016 ReportItem element name. A "List" or "Matrix" in the
# Report Builder UI is a <Tablix>; a gauge is a <GaugePanel>.
REPORT_ITEM_TAGS = (
    "Tablix",
    "Textbox",
    "Image",
    "Rectangle",
    "Subreport",
    "Chart",
    "Map",
    "GaugePanel",
    "Line",
    "CustomReportItem",
)

# RDL defaults for <Page> size elements that are absent (what Report
# Builder renders): US Letter portrait, zero margins, one column with
# 0.5in spacing.
PAGE_DEFAULTS = {
    "PageHeight": "11in",
    "PageWidth": "8.5in",
    "TopMargin": "0in",
    "BottomMargin": "0in",
    "LeftMargin": "0in",
    "RightMargin": "0in",
    "ColumnSpacing": "0.5in",
}

# Geometry comparisons tolerate this much (inches) — Report Builder
# itself writes values like 6.49999in.
TOLERANCE_IN = 0.001


def local_name(elem: etree._Element) -> str:
    return etree.QName(elem).localname if isinstance(elem.tag, str) else ""


def length_in(parent: Optional[etree._Element], local: str) -> Optional[float]:
    """``parent/<local>`` as inches; ``None`` when absent or not a valid
    RDL size (validation reports those)."""
    if parent is None:
        return None
    node = find_child(parent, local)
    if node is None or node.text is None:
        return None
    try:
        return to_inches(node.text)
    except ValueError:
        return None


def size_unit(text: Optional[str], fallback: str = "in") -> str:
    """The unit designator of an RDL size string (``fallback`` if unparsable)."""
    try:
        return parse_size(text).unit  # type: ignore[arg-type]
    except ValueError:
        return fallback


@dataclass(frozen=True)
class Box:
    """An item's box in its container, in inches. Lines (whose Width /
    Height are a vector) are normalized to a non-negative extent."""

    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    def overlaps(self, other: Box, tol: float = TOLERANCE_IN) -> bool:
        """True when the two boxes share an area (touching edges don't count)."""
        return (
            min(self.right, other.right) - max(self.left, other.left) > tol
            and min(self.bottom, other.bottom) - max(self.top, other.top) > tol
        )


def item_box(item: etree._Element) -> Optional[Box]:
    """The box of a positioned report item, or ``None`` when its Width or
    Height is absent / invalid (tablix-cell contents carry neither — the
    cell sizes them). A missing Top / Left means 0 (RDL default)."""
    width = length_in(item, "Width")
    height = length_in(item, "Height")
    if width is None or height is None:
        return None
    top = length_in(item, "Top") or 0.0
    left = length_in(item, "Left") or 0.0
    if width < 0:
        left, width = left + width, -width
    if height < 0:
        top, height = top + height, -height
    return Box(left, top, width, height)


def child_items(container: Optional[etree._Element]) -> list[etree._Element]:
    """Report items directly inside ``container/<ReportItems>``."""
    if container is None:
        return []
    items_root = find_child(container, "ReportItems")
    if items_root is None:
        return []
    return [c for c in items_root if local_name(c) in REPORT_ITEM_TAGS]


@dataclass(frozen=True)
class SectionGeometry:
    """Page and body geometry of one ``<ReportSection>``, in inches. Absent
    page elements take their RDL default; ``body_width`` / ``body_height``
    are ``None`` when absent or invalid."""

    page_width: float
    page_height: float
    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float
    columns: int
    column_spacing: float
    body_width: Optional[float]
    body_height: Optional[float]

    @property
    def printable_width(self) -> float:
        """Page width minus the left and right margins."""
        return self.page_width - self.margin_left - self.margin_right

    @property
    def available_body_width(self) -> float:
        """The widest body that fits one page column:
        ``(printable - spacing x (columns - 1)) / columns``."""
        return (self.printable_width - self.column_spacing * (self.columns - 1)) / self.columns

    @property
    def required_page_width(self) -> Optional[float]:
        """The page width the body needs:
        ``columns x body + spacing x (columns - 1) + margins``."""
        if self.body_width is None:
            return None
        return (
            self.columns * self.body_width
            + self.column_spacing * (self.columns - 1)
            + self.margin_left
            + self.margin_right
        )


def _page_length(page: Optional[etree._Element], local: str) -> float:
    value = length_in(page, local)
    return to_inches(PAGE_DEFAULTS[local]) if value is None else value


def section_geometry(section: etree._Element) -> SectionGeometry:
    page = find_child(section, "Page")
    body = find_child(section, "Body")
    columns = 1
    columns_node = find_child(page, "Columns") if page is not None else None
    if columns_node is not None and columns_node.text:
        try:
            columns = max(1, int(columns_node.text.strip()))
        except ValueError:
            columns = 1
    return SectionGeometry(
        page_width=_page_length(page, "PageWidth"),
        page_height=_page_length(page, "PageHeight"),
        margin_left=_page_length(page, "LeftMargin"),
        margin_right=_page_length(page, "RightMargin"),
        margin_top=_page_length(page, "TopMargin"),
        margin_bottom=_page_length(page, "BottomMargin"),
        columns=columns,
        column_spacing=_page_length(page, "ColumnSpacing"),
        body_width=length_in(section, "Width"),
        body_height=length_in(body, "Height"),
    )


def report_sections(root: etree._Element) -> list[etree._Element]:
    sections = find_child(root, "ReportSections")
    if sections is None:
        return []
    return [s for s in sections if local_name(s) == "ReportSection"]


def iter_containers(
    section: etree._Element,
) -> Iterator[tuple[etree._Element, Optional[float], Optional[float]]]:
    """Every positioning container of a section with its inner size in
    inches: the Body (section Width x Body Height), the PageHeader /
    PageFooter (printable width x their Height) and every Rectangle
    nested in those (its own Width x Height). A ``None`` size means
    unknown — skip that axis."""
    geom = section_geometry(section)
    body = find_child(section, "Body")
    page = find_child(section, "Page")
    roots: list[tuple[etree._Element, Optional[float], Optional[float]]] = []
    if body is not None:
        roots.append((body, geom.body_width, geom.body_height))
    for local in ("PageHeader", "PageFooter"):
        band = find_child(page, local) if page is not None else None
        if band is not None:
            roots.append((band, geom.printable_width, length_in(band, "Height")))
    stack = list(reversed(roots))
    while stack:
        container, width, height = stack.pop()
        yield container, width, height
        nested = []
        for item in child_items(container):
            if local_name(item) == "Rectangle":
                box = item_box(item)
                nested.append(
                    (item, box.width if box else None, box.height if box else None),
                )
        stack.extend(reversed(nested))


def tablix_row_header_width(tablix: etree._Element) -> float:
    """Width of a tablix's row-header area: the widest root-to-leaf sum of
    ``TablixHeader/Size`` in the row hierarchy (0 when it has none)."""

    def widest(members_root: Optional[etree._Element]) -> float:
        if members_root is None:
            return 0.0
        best = 0.0
        for member in members_root:
            if local_name(member) != "TablixMember":
                continue
            own = length_in(find_child(member, "TablixHeader"), "Size") or 0.0
            best = max(best, own + widest(find_child(member, "TablixMembers")))
        return best

    hierarchy = find_child(tablix, "TablixRowHierarchy")
    return widest(find_child(hierarchy, "TablixMembers")) if hierarchy is not None else 0.0


def tablix_columns_width(tablix: etree._Element) -> Optional[float]:
    """Σ ``TablixBody/TablixColumns/TablixColumn/Width`` plus the row-header
    width — what the tablix actually renders as. ``None`` when a column
    width is missing or invalid."""
    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    if cols_root is None:
        return None
    total = 0.0
    for col in cols_root.iterchildren(q("TablixColumn")):
        width = length_in(col, "Width")
        if width is None:
            return None
        total += width
    return total + tablix_row_header_width(tablix)


def sync_tablix_width(tablix: etree._Element) -> Optional[str]:
    """Set the tablix's ``<Width>`` to :func:`tablix_columns_width` — what
    it renders at — in the unit ``<Width>`` already uses (#89). Every tool
    that adds, removes or resizes a column or a row header calls this, so
    ``tablix-width-mismatch`` never fires on their output.

    Returns the new ``<Width>`` text, or ``None`` when nothing changed: the
    widths already agree (within :data:`TOLERANCE_IN`), the tablix has no
    ``<Width>``, or a column width is missing / invalid."""
    node = find_child(tablix, "Width")
    actual = tablix_columns_width(tablix)
    if node is None or actual is None or actual <= 0:
        return None
    declared = length_in(tablix, "Width")
    if declared is not None and abs(declared - actual) <= TOLERANCE_IN:
        return None
    # Round away float noise (4.499999 -> 4.5) before rendering.
    node.text = format_size(round(actual, 6), size_unit(node.text))
    return node.text


__all__ = [
    "PAGE_DEFAULTS",
    "REPORT_ITEM_TAGS",
    "TOLERANCE_IN",
    "Box",
    "SectionGeometry",
    "child_items",
    "item_box",
    "iter_containers",
    "length_in",
    "local_name",
    "report_sections",
    "section_geometry",
    "size_unit",
    "sync_tablix_width",
    "tablix_columns_width",
    "tablix_row_header_width",
]
