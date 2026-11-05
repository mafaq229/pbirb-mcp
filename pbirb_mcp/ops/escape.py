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
from pbirb_mcp.core.mutation import fingerprint
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


# Default output cap for raw_xml_view (issue #55): a whole-tablix view of a
# real report ran to ~150 KB, far past what an MCP client shows the model
# (Claude Code truncates tool output at ~25k tokens). 40 KB of XML is
# ~10-13k tokens.
RAW_XML_VIEW_MAX_BYTES = 40_000

TRUNCATION_MARKER_PREFIX = "<!-- pbirb-mcp: truncated"


def raw_xml_view(path: str, xpath: str, max_bytes: int = RAW_XML_VIEW_MAX_BYTES) -> list[str]:
    """Return matched elements as serialised XML strings.

    Read-only: never saves. Returns ``[]`` when the xpath matches no
    elements; raises ``ValueError`` when the xpath itself is malformed.
    Each result is the element serialised verbatim — namespace
    declarations included so the string is round-trip parseable.

    ``max_bytes`` caps the total UTF-8 size of the returned strings
    (``0`` = no cap). When the matches exceed it, the element that
    crosses the cap is cut and ends in an explicit
    ``<!-- pbirb-mcp: truncated ... -->`` marker naming how much was
    left out (that string is no longer well-formed XML); later matches
    are dropped and counted in the marker.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer (0 = no cap)")
    doc = RDLDocument.open(path)
    matches = _evaluate(doc, xpath)
    out = [etree.tostring(m, encoding="unicode") for m in matches if isinstance(m, etree._Element)]
    return _cap(out, max_bytes) if max_bytes else out


def _cap(items: list[str], max_bytes: int) -> list[str]:
    encoded = [s.encode("utf-8") for s in items]
    total = sum(len(b) for b in encoded)
    if total <= max_bytes:
        return items
    kept: list[str] = []
    used = 0
    for i, b in enumerate(encoded):
        if used + len(b) <= max_bytes:
            kept.append(items[i])
            used += len(b)
            continue
        head = b[: max_bytes - used].decode("utf-8", errors="ignore")
        dropped = len(items) - i - 1
        marker = (
            f"{TRUNCATION_MARKER_PREFIX}: showing {used + len(head.encode('utf-8'))} of "
            f"{total} bytes; match {i + 1} of {len(items)} is cut"
            + (f" and {dropped} later match(es) omitted" if dropped else "")
            + "; narrow the xpath or raise max_bytes (0 = no cap) -->"
        )
        kept.append(head + marker)
        break
    return kept


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

    Returns ``{xpath, kind, changed}`` on success — ``kind`` is the
    local name of the new element so callers can confirm the swap landed
    where expected; ``changed`` is False (and the file is left alone)
    when ``content`` has the same content as the element it replaces
    (layout whitespace is ignored).
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

    # The same content (layout whitespace aside) is a no-op: keep the
    # original element and leave the file alone.
    changed = fingerprint(new_node) != fingerprint(target)
    if changed:
        new_node.tail = target.tail
        target.getparent().replace(target, new_node)
        doc.save()
    return {
        "xpath": xpath,
        "kind": etree.QName(new_node.tag).localname,
        "changed": changed,
    }


__all__ = ["raw_xml_view", "raw_xml_replace"]
