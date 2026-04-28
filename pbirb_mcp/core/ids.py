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

from typing import TYPE_CHECKING

from lxml import etree

from pbirb_mcp.core.xpath import XPATH_NS

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


__all__ = [
    "AmbiguousElementError",
    "ElementNotFoundError",
    "resolve_dataset",
    "resolve_group",
    "resolve_parameter",
    "resolve_tablix",
    "resolve_textbox",
]
