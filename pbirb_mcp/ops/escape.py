"""XPath escape hatch (Phase 12 commit 45).

Two tools for cases the structured surface doesn't yet cover. They exist
specifically so callers don't have to ``cp → sed → cp`` a file when the
right typed tool isn't shipped:

* :func:`raw_xml_view` — read-only. Run an XPath against the open
  document and return matched elements as serialised XML strings.
* :func:`raw_xml_replace` — surgical write. Replace a single matched
  element with new XML. Refuses when the XPath matches zero or more
  than one element (ambiguous), or when the match is the ``<Report>``
  root (replacing it would corrupt the document).

XPath context: expressions are evaluated against ``<Report>`` (the
document root). The ``r:`` and ``rd:`` namespace prefixes are
pre-bound to the RDL and rd namespaces respectively. Examples::

    raw_xml_view(path, "r:DataSources/r:DataSource[@Name='Foo']")
    raw_xml_view(path, ".//r:Textbox[@Name='X']/r:Style")

Replacement content for :func:`raw_xml_replace` is parsed with RDL set
as the default namespace, so callers write plain
``<Textbox><Value>x</Value></Textbox>`` without explicit
``xmlns=...`` declarations. The ``rd:`` prefix is also bound, so
``<rd:DefaultName>foo</rd:DefaultName>`` works too.
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, XPATH_NS


def _evaluate(doc: RDLDocument, xpath: str) -> list[etree._Element]:
    """Run ``xpath`` against the document root with the standard RDL
    namespace map. Wraps lxml's syntax errors into a ValueError so the
    server surfaces a clear message."""
    if not isinstance(xpath, str) or not xpath.strip():
        raise ValueError("xpath must be a non-empty string")
    try:
        return list(doc.root.xpath(xpath, namespaces=XPATH_NS))
    except etree.XPathEvalError as exc:
        raise ValueError(f"invalid xpath {xpath!r}: {exc}") from exc


def raw_xml_view(path: str, xpath: str) -> list[str]:
    """Return matched elements as serialised XML strings.

    Read-only: never saves. Returns ``[]`` when the xpath matches no
    elements; raises ``ValueError`` when the xpath itself is malformed.
    Each result is the element serialised verbatim — namespace
    declarations included so the string is round-trip parseable.
    """
    doc = RDLDocument.open(path)
    matches = _evaluate(doc, xpath)
    return [etree.tostring(m, encoding="unicode") for m in matches if isinstance(m, etree._Element)]


def raw_xml_replace(path: str, xpath: str, content: str) -> dict[str, Any]:
    """Replace the single element matched by ``xpath`` with ``content``.

    Refuses when:

    * The xpath matches zero elements (raises ``ElementNotFoundError``).
    * The xpath matches more than one element (raises ``ValueError`` —
      ambiguous replace is dangerous, list them with ``raw_xml_view``
      first then narrow the xpath).
    * The xpath matches the ``<Report>`` root (would corrupt the
      document).
    * ``content`` doesn't parse as XML, contains zero elements, or
      contains more than one top-level element.

    Returns ``{xpath, kind, changed: True}`` on success — ``kind`` is
    the local name of the new element so callers can confirm the swap
    landed where expected.
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty XML string")

    doc = RDLDocument.open(path)
    matches = _evaluate(doc, xpath)
    if not matches:
        raise ElementNotFoundError(f"xpath {xpath!r} matched no elements")
    if len(matches) > 1:
        raise ValueError(
            f"xpath {xpath!r} matched {len(matches)} elements; narrow it "
            "to exactly one (use raw_xml_view to list candidates first)"
        )
    target = matches[0]
    if not isinstance(target, etree._Element):
        raise ValueError(f"xpath {xpath!r} matched a non-element node")
    if target is doc.root:
        raise ValueError("refusing to replace the <Report> root — that would corrupt the document")

    # Parse content with RDL as the default namespace + rd: bound. Wrap
    # in a synthetic envelope so the user can write bare element names
    # without declaring xmlns themselves.
    wrapper_xml = f'<wrapper xmlns="{RDL_NS}" xmlns:rd="{RD_NS}">{content}</wrapper>'
    try:
        wrapper = etree.fromstring(wrapper_xml)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"content is not valid XML: {exc}") from exc

    children = list(wrapper)
    if not children:
        raise ValueError("content must contain exactly one XML element; got zero")
    if len(children) > 1:
        raise ValueError(f"content must contain exactly one XML element; got {len(children)}")
    new_node = children[0]

    parent = target.getparent()
    parent.replace(target, new_node)
    doc.save()
    return {
        "xpath": xpath,
        "kind": etree.QName(new_node.tag).localname,
        "changed": True,
    }


__all__ = ["raw_xml_view", "raw_xml_replace"]
