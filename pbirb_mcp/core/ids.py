"""Stable element addressing.

Tools must never address RDL elements by index. A column at position 2 today
can be at position 3 after one ``add_column`` call, so any multi-step edit
plan that hard-codes positions silently corrupts the report. Instead, every
public tool takes user-facing identifiers — ``tablix_name``, ``group_name``,
``dataset_name``, ``parameter_name``, ``textbox_name`` — and this module is
the single chokepoint that turns those identifiers into live lxml elements.

If a name doesn't resolve, raise :class:`ElementNotFoundError`. If a name is
ambiguous (Report Builder enforces uniqueness, but malformed reports happen)
raise :class:`AmbiguousElementError` instead of returning the first match.
Tools then surface these as JSON-RPC errors with names — never indices — so
the LLM caller can self-correct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from lxml import etree

from pbirb_mcp.core.geometry import REPORT_ITEM_TAGS
from pbirb_mcp.core.xpath import RDL_NS, XPATH_NS

if TYPE_CHECKING:
    from pbirb_mcp.core.document import RDLDocument


class ElementNotFoundError(LookupError):
    """No element with the given name exists in the document."""


class AmbiguousElementError(LookupError):
    """More than one element with the given name exists — caller must disambiguate."""


def _one(matches: list[etree._Element], kind: str, name: str) -> etree._Element:
    if not matches:
        raise ElementNotFoundError(f"{kind} {name!r} not found")
    if len(matches) > 1:
        raise AmbiguousElementError(f"{kind} {name!r} is ambiguous: {len(matches)} matches")
    return matches[0]


def resolve_tablix(doc: RDLDocument, name: str) -> etree._Element:
    matches = list(doc.root.xpath(".//r:Tablix[@Name=$n]", namespaces=XPATH_NS, n=name))
    return _one(matches, "Tablix", name)


def resolve_dataset(doc: RDLDocument, name: str) -> etree._Element:
    matches = list(doc.root.xpath(".//r:DataSets/r:DataSet[@Name=$n]", namespaces=XPATH_NS, n=name))
    return _one(matches, "DataSet", name)


def resolve_parameter(doc: RDLDocument, name: str) -> etree._Element:
    matches = list(
        doc.root.xpath(
            ".//r:ReportParameters/r:ReportParameter[@Name=$n]",
            namespaces=XPATH_NS,
            n=name,
        )
    )
    return _one(matches, "ReportParameter", name)


def resolve_textbox(doc: RDLDocument, name: str) -> etree._Element:
    matches = list(doc.root.xpath(".//r:Textbox[@Name=$n]", namespaces=XPATH_NS, n=name))
    return _one(matches, "Textbox", name)


def resolve_group(doc: RDLDocument, tablix_name: str, group_name: str) -> etree._Element:
    """Resolve a ``Group`` element by name within a named tablix.

    Searches both ``TablixRowHierarchy`` and ``TablixColumnHierarchy`` so the
    same call works for row groups (the common case) and column groups.
    """
    tablix = resolve_tablix(doc, tablix_name)
    matches = list(tablix.xpath(".//r:Group[@Name=$n]", namespaces=XPATH_NS, n=group_name))
    return _one(matches, f"Group in tablix {tablix_name!r}", group_name)


# Page regions a report item can sit in, by their element's local name.
REGION_OF_TAG = {"Body": "body", "PageHeader": "header", "PageFooter": "footer"}
_REGION_PROSE = {"body": "the body", "header": "the page header", "footer": "the page footer"}
_ITEM_PARENTS = ("ReportItems", "CellContents")


def report_item_region(item: etree._Element) -> Optional[str]:
    """``"body"`` / ``"header"`` / ``"footer"`` — the page region ``item``
    sits in, at any depth — or ``None`` outside all three."""
    for anc in item.iterancestors():
        if isinstance(anc.tag, str):
            region = REGION_OF_TAG.get(etree.QName(anc).localname)
            if region is not None:
                return region
    return None


def item_container(item: etree._Element) -> etree._Element:
    """The element that holds ``item``: the ``Body`` / ``PageHeader`` /
    ``PageFooter`` / ``Rectangle`` whose ``<ReportItems>`` lists it, or the
    ``<CellContents>`` of the tablix cell it fills."""
    parent = item.getparent()
    if parent is not None and etree.QName(parent).localname == "ReportItems":
        return parent.getparent()
    return parent


def resolve_report_item(
    doc: RDLDocument, name: str, region: Optional[str] = None
) -> tuple[etree._Element, etree._Element, Optional[str]]:
    """Resolve the report item (any ``REPORT_ITEM_TAGS`` element) named
    ``name`` anywhere in the report: the top level of the body / header /
    footer, inside Rectangles at any depth, inside tablix cells (a List's
    rectangle and what it holds).

    Returns ``(item, container, region)`` — see :func:`item_container` and
    :func:`report_item_region`. With ``region`` the search is limited to
    that page region, and a miss names the region the item is in, if any.
    Raises :class:`ElementNotFoundError` / :class:`AmbiguousElementError`.
    """
    tags = [f"{{{RDL_NS}}}{tag}" for tag in REPORT_ITEM_TAGS]
    matches = [
        el
        for el in doc.root.iter(*tags)
        if el.get("Name") == name
        and el.getparent() is not None
        and etree.QName(el.getparent()).localname in _ITEM_PARENTS
    ]
    if region is not None:
        elsewhere = [m for m in matches if report_item_region(m) != region]
        matches = [m for m in matches if report_item_region(m) == region]
        if not matches:
            hint = ""
            if elsewhere:
                where = report_item_region(elsewhere[0])
                hint = f" (it is in {_REGION_PROSE.get(where or '', 'no page region')})"
            raise ElementNotFoundError(f"{region} has no named item {name!r}{hint}")
    item = _one(matches, "report item", name)
    return item, item_container(item), report_item_region(item)


__all__ = [
    "REGION_OF_TAG",
    "AmbiguousElementError",
    "ElementNotFoundError",
    "item_container",
    "report_item_region",
    "resolve_dataset",
    "resolve_group",
    "resolve_parameter",
    "resolve_report_item",
    "resolve_tablix",
    "resolve_textbox",
]
