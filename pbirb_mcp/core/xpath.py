"""Namespace-aware XPath helpers.

Centralises the RDL namespace constants and provides small helpers so feature
modules don't litter f-strings with `{ns}` everywhere. lxml requires a prefix
map for XPath — we always use the prefix ``r`` for the default RDL namespace
because lxml does not support an empty default-namespace prefix in XPath.
"""

from __future__ import annotations

from typing import Iterable, Optional

from lxml import etree

RDL_NS = "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"
RD_NS = "http://schemas.microsoft.com/SQLServer/reporting/reportdesigner"

XPATH_NS = {"r": RDL_NS, "rd": RD_NS}


def q(local_name: str) -> str:
    """Return a Clark-notation tag for the default RDL namespace."""
    return f"{{{RDL_NS}}}{local_name}"


def qrd(local_name: str) -> str:
    """Return a Clark-notation tag for the rd: namespace."""
    return f"{{{RD_NS}}}{local_name}"


def xfind(node: etree._Element, xpath: str) -> Optional[etree._Element]:
    results = node.xpath(xpath, namespaces=XPATH_NS)
    return results[0] if results else None


def xfindall(node: etree._Element, xpath: str) -> list[etree._Element]:
    return list(node.xpath(xpath, namespaces=XPATH_NS))


def find_child(parent: etree._Element, local_name: str) -> Optional[etree._Element]:
    return parent.find(q(local_name))


def find_children(parent: etree._Element, local_name: str) -> list[etree._Element]:
    return parent.findall(q(local_name))


def iter_local(parent: etree._Element, local_name: str) -> Iterable[etree._Element]:
    yield from parent.iter(q(local_name))
