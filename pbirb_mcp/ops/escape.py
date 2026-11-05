"""XPath escape hatch (Phase 12 commit 45; hardened in issue #85).

Tools for cases the structured surface doesn't yet cover. They exist
specifically so callers don't have to ``cp → sed → cp`` a file when the
right typed tool isn't shipped:

* :func:`raw_xml_view` — read-only. Run an XPath against the open
  document; node results come back typed (elements as XML, attributes,
  text), number / string / boolean XPaths as a scalar.
* :func:`raw_xml_replace` — replace a single matched element with new XML
  of the same kind.
* :func:`raw_xml_insert` — insert new elements into / next to a single
  matched element.
* :func:`raw_xml_remove` — remove the matched element(s).

XPath context: expressions are evaluated against ``<Report>`` (the
document root). ``r:`` (RDL) and ``rd:`` (report designer) are bound, as
are Report Builder's ``am:`` (authoring metadata), ``df:`` (default font
family) and ``cl:`` (component library) and every prefix the root
declares. Examples::

    raw_xml_view(path, "r:DataSources/r:DataSource[@Name='Foo']")
    raw_xml_view(path, ".//r:Textbox[@Name='X']/r:Style")
    raw_xml_view(path, "count(//r:Textbox)")

Content for the writers is parsed with RDL as the default namespace and
the same prefixes bound, so callers write plain
``<Textbox><Value>x</Value></Textbox>`` (or ``<rd:DefaultName>``) without
``xmlns=...`` declarations.

The writers refuse an edit that would leave the report with XSD /
structural errors it didn't have, except inside an editing transaction,
whose commit verifies the end state instead (so a multi-step raw edit can
pass through intermediate states). New report item / group names must be
unique report-wide.
"""

from __future__ import annotations

import copy
import math
from collections import Counter
from typing import Any

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.mutation import fingerprint
from pbirb_mcp.core.names import SCOPE_KINDS, NameRegistry
from pbirb_mcp.core.tablix_shape import detach
from pbirb_mcp.core.xpath import RDL_NS, XPATH_NS

# Report Builder's other namespaces (issue #85): authoring metadata is
# declared on <am:AuthoringMetadata> itself, not on the root.
AM_NS = "http://schemas.microsoft.com/sqlserver/reporting/authoringmetadata"
DF_NS = (
    "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition/defaultfontfamily"
)
CL_NS = "http://schemas.microsoft.com/sqlserver/reporting/2010/01/componentdefinition"
_EXTRA_NS = {"am": AM_NS, "df": DF_NS, "cl": CL_NS}

INSERT_POSITIONS = ("append", "prepend", "before", "after")


def _namespaces(doc: RDLDocument) -> dict[str, str]:
    """Prefix map for XPaths and content: am/df/cl, the root's own
    prefixes, then r/rd (which always win)."""
    root_prefixes = {p: u for p, u in (doc.root.nsmap or {}).items() if p and u}
    return {**_EXTRA_NS, **root_prefixes, **XPATH_NS}


def _evaluate(doc: RDLDocument, xpath: str) -> Any:
    """Run ``xpath`` against the document root. Returns lxml's result: a
    list for node-sets, else a float / bool / str. Wraps lxml's syntax
    errors into a ValueError so the server surfaces a clear message."""
    if not isinstance(xpath, str) or not xpath.strip():
        raise ValueError("xpath must be a non-empty string")
    try:
        return doc.root.xpath(xpath, namespaces=_namespaces(doc))
    except etree.XPathError as exc:
        raise ValueError(f"invalid xpath {xpath!r}: {exc}") from exc


def _elements(doc: RDLDocument, xpath: str, what: str) -> list[etree._Element]:
    """The elements ``xpath`` selects; refuses scalars and non-element
    nodes, raises :class:`ElementNotFoundError` on no match."""
    result = _evaluate(doc, xpath)
    if not isinstance(result, list):
        raise ValueError(f"xpath {xpath!r} evaluates to a {_scalar_kind(result)}, not elements")
    if not result:
        raise ElementNotFoundError(f"xpath {xpath!r} matched no elements")
    for node in result:
        if not isinstance(node, etree._Element) or not isinstance(node.tag, str):
            raise ValueError(
                f"xpath {xpath!r} matched a non-element node; {what} works on elements "
                "(select the element, e.g. drop a trailing /@attr or /text())"
            )
    return result


def _single(doc: RDLDocument, xpath: str, what: str) -> etree._Element:
    matches = _elements(doc, xpath, what)
    if len(matches) > 1:
        raise ValueError(
            f"xpath {xpath!r} matched {len(matches)} elements; narrow it "
            "to exactly one (use raw_xml_view to list candidates first)"
        )
    return matches[0]


# ---- raw_xml_view --------------------------------------------------------------

# Default output cap for raw_xml_view (issue #55): a whole-tablix view of a
# real report ran to ~150 KB, far past what an MCP client shows the model
# (Claude Code truncates tool output at ~25k tokens). 40 KB of XML is
# ~10-13k tokens.
RAW_XML_VIEW_MAX_BYTES = 40_000

TRUNCATION_MARKER_PREFIX = "<!-- pbirb-mcp: truncated"


def _scalar_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, float):
        return "number"
    return "string"


def _scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None  # NaN / Infinity aren't JSON
        return int(value) if value.is_integer() else value
    return str(value)


def _element_xml(elem: etree._Element) -> str:
    """``elem`` alone: no tail, and only the namespace declarations it uses."""
    clone = copy.deepcopy(elem)
    clone.tail = None
    etree.cleanup_namespaces(clone)
    return etree.tostring(clone, encoding="unicode")


def _qualified(name: str, nsmap: dict) -> str:
    if not name.startswith("{"):
        return name
    q = etree.QName(name)
    for prefix, uri in nsmap.items():
        if uri == q.namespace and prefix:
            return f"{prefix}:{q.localname}"
    return q.localname


def _describe(node: Any) -> tuple[dict[str, Any], str]:
    """``(match, field)``: the typed match and the key holding its text."""
    if isinstance(node, etree._Element):
        if isinstance(node, etree._Comment):
            return {"kind": "comment", "value": node.text or ""}, "value"
        if isinstance(node, etree._ProcessingInstruction):
            return {
                "kind": "processing-instruction",
                "name": node.target,
                "value": node.text or "",
            }, ("value")
        return {"kind": "element", "xml": _element_xml(node)}, "xml"
    if isinstance(node, tuple):  # namespace:: axis
        return {"kind": "namespace", "name": node[0], "value": node[1]}, "value"
    parent = getattr(node, "getparent", lambda: None)()
    if getattr(node, "is_attribute", False):
        match = {
            "kind": "attribute",
            "name": _qualified(node.attrname, parent.nsmap if parent is not None else {}),
            "value": str(node),
        }
        if parent is not None:
            match["element"] = etree.QName(parent).localname
        return match, "value"
    return {"kind": "text", "value": str(node)}, "value"


def raw_xml_view(path: str, xpath: str, max_bytes: int = RAW_XML_VIEW_MAX_BYTES) -> dict[str, Any]:
    """Run ``xpath`` against the report. Read-only: never saves.

    Node-set XPaths return ``{kind: "nodes", count, matches, truncated}``;
    each match is ``{kind: "element", xml}`` (serialised without its tail
    and with only the namespace declarations it uses), ``{kind:
    "attribute", name, value, element}``, ``{kind: "text", value}`` or
    ``{kind: "comment", value}``. Number / string / boolean XPaths
    (``count(...)``, ``string(...)``, ``boolean(...)``) return ``{kind:
    "number"|"string"|"boolean", value}``. Raises ``ValueError`` when the
    xpath itself is malformed.

    ``max_bytes`` caps the total UTF-8 size of the returned XML / values
    (``0`` = no cap). When the matches exceed it, the match that crosses
    the cap is cut and ends in an explicit ``<!-- pbirb-mcp: truncated
    ... -->`` marker naming how much was left out (that xml is no longer
    well-formed) and carries ``truncated: true``; later matches are
    dropped and counted in the marker; ``truncated`` is true.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer (0 = no cap)")
    doc = RDLDocument.open(path)
    result = _evaluate(doc, xpath)
    if not isinstance(result, list):
        return {"kind": _scalar_kind(result), "value": _scalar(result)}
    described = [_describe(node) for node in result]
    texts = [m[field] for m, field in described]
    kept = _cap(texts, max_bytes) if max_bytes else texts
    matches = []
    for (match, field), text in zip(described, kept):
        if text is not match[field]:
            match = {**match, field: text, "truncated": True}
        matches.append(match)
    truncated = len(kept) < len(texts) or any(m.get("truncated") for m in matches)
    return {"kind": "nodes", "count": len(result), "matches": matches, "truncated": truncated}


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


# ---- writers ----------------------------------------------------------------------


def _parse_content(doc: RDLDocument, content: Any) -> list[etree._Element]:
    """The top-level elements of ``content`` (RDL default namespace, the
    view's prefixes bound)."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty XML string")
    decls = " ".join(f'xmlns:{p}="{u}"' for p, u in _namespaces(doc).items() if p != "r")
    wrapper_xml = f'<wrapper xmlns="{RDL_NS}" {decls}>{content}</wrapper>'
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        wrapper = etree.fromstring(wrapper_xml, parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"content is not valid XML: {exc}") from exc
    nodes = [c for c in wrapper if isinstance(c.tag, str)]
    if not nodes:
        raise ValueError("content must contain an XML element; got zero")
    if (wrapper.text or "").strip() or any((c.tail or "").strip() for c in wrapper):
        raise ValueError("content must be XML elements only (text outside an element found)")
    return nodes


def _scope_names(nodes: list[etree._Element]) -> list[str]:
    tags = tuple(f"{{{RDL_NS}}}{k}" for k in SCOPE_KINDS)
    return [el.get("Name") for n in nodes for el in n.iter(*tags) if el.get("Name") is not None]


def _schema_errors(doc: RDLDocument) -> list[dict[str, Any]]:
    from pbirb_mcp.ops.validate import _validate_tree

    errors, _ = _validate_tree(doc.tree, str(doc.path))
    return [e for e in errors if e.get("severity") == "error"]


def _save_checked(doc: RDLDocument, before: list[dict[str, Any]]) -> None:
    """Save ``doc`` unless the edit introduced XSD / structural errors.

    ``before`` is :func:`_schema_errors` of the unedited tree. Inside an
    editing transaction nothing is checked here: the commit verifies the
    end state.
    """
    if not getattr(doc, "_in_transaction", False):
        from pbirb_mcp.ops.validate import issue_key

        remaining = Counter(issue_key(e) for e in before)
        new = []
        for err in _schema_errors(doc):
            key = issue_key(err)
            if remaining[key] > 0:
                remaining[key] -= 1
            else:
                new.append(err)
        if new:
            details = "; ".join(e["message"].replace(f"{{{RDL_NS}}}", "") for e in new[:3])
            more = f" (+{len(new) - 3} more)" if len(new) > 3 else ""
            raise ValueError(
                f"refusing: the edit would make the report invalid ({len(new)} new "
                f"XSD/structural error(s): {details}{more}); nothing was saved"
            )
    doc.save()


def _baseline(doc: RDLDocument) -> list[dict[str, Any]]:
    return [] if getattr(doc, "_in_transaction", False) else _schema_errors(doc)


def raw_xml_replace(
    path: str, xpath: str, content: str, allow_kind_change: bool = False
) -> dict[str, Any]:
    """Replace the single element matched by ``xpath`` with ``content``.

    Refuses when:

    * The xpath matches zero elements (raises ``ElementNotFoundError``).
    * The xpath matches more than one element (raises ``ValueError`` —
      ambiguous replace is dangerous, list them with ``raw_xml_view``
      first then narrow the xpath), or something other than an element.
    * The xpath matches the ``<Report>`` root (would corrupt the
      document).
    * ``content`` doesn't parse as XML, contains zero elements, or
      contains more than one top-level element.
    * ``content``'s element has a different namespace or local name than
      the target (a ``<PageWidth xmlns="urn:x">`` would pass the XSD as an
      extension element while the real PageWidth vanished) — unless
      ``allow_kind_change`` is true.
    * The result has XSD / structural errors the report didn't have
      (outside a transaction), or reuses a report item / group name.

    Returns ``{xpath, kind, changed}`` on success — ``kind`` is the
    local name of the new element so callers can confirm the swap landed
    where expected; ``changed`` is False (and the file is left alone)
    when ``content`` has the same content as the element it replaces
    (layout whitespace and attribute order are ignored).
    """
    doc = RDLDocument.open(path)
    nodes = _parse_content(doc, content)
    target = _single(doc, xpath, "raw_xml_replace")
    if target is doc.root:
        raise ValueError("refusing to replace the <Report> root — that would corrupt the document")
    if len(nodes) > 1:
        raise ValueError(f"content must contain exactly one XML element; got {len(nodes)}")
    new_node = nodes[0]
    if new_node.tag != target.tag and not allow_kind_change:
        raise ValueError(
            f"content is <{_clark_label(new_node.tag)}> but the target is "
            f"<{_clark_label(target.tag)}>; a replacement must be the same element "
            "(namespace and name) — pass allow_kind_change=true to swap in a "
            "different one"
        )

    # The same content (layout whitespace aside) is a no-op: keep the
    # original element and leave the file alone.
    changed = fingerprint(new_node) != fingerprint(target)
    if changed:
        NameRegistry(doc, exclude=[target]).ensure_unique(*_scope_names([new_node]))
        before = _baseline(doc)
        new_node.tail = target.tail
        target.getparent().replace(target, new_node)
        _save_checked(doc, before)
    return {
        "xpath": xpath,
        "kind": etree.QName(new_node.tag).localname,
        "changed": changed,
    }


def _clark_label(tag: str) -> str:
    q = etree.QName(tag)
    if q.namespace == RDL_NS:
        return q.localname
    return f"{q.localname} xmlns={q.namespace!r}" if q.namespace else q.localname


def raw_xml_insert(path: str, xpath: str, content: str, position: str = "append") -> dict[str, Any]:
    """Insert the element(s) in ``content`` relative to the single element
    ``xpath`` matches.

    ``position``: ``append`` (last child of the match, the default),
    ``prepend`` (first child), ``before`` / ``after`` (sibling of the match;
    refused for the ``<Report>`` root). Several top-level elements are
    inserted in order. The bundled RDL 2016 XSD constrains child order only
    inside collections (``<Fields>``, ``<TablixMembers>``, ...), which hold
    one element type, so any of these positions is XSD-correct wherever
    the element is allowed at all.

    Refuses (nothing saved) a match count other than one, a non-element
    match, content that isn't XML elements, a report item / group name
    already in the report, and a result with XSD / structural errors the
    report didn't have (outside a transaction). Not idempotent: the same
    call again inserts another copy (or refuses its duplicate names).

    Returns ``{xpath, position, inserted, changed: true}``; ``inserted``
    lists the local names of the new elements.
    """
    if position not in INSERT_POSITIONS:
        raise ValueError(f"position must be one of {', '.join(INSERT_POSITIONS)}; got {position!r}")
    doc = RDLDocument.open(path)
    nodes = _parse_content(doc, content)
    anchor = _single(doc, xpath, "raw_xml_insert")
    if position in ("before", "after") and anchor is doc.root:
        raise ValueError(f"refusing to insert {position} the <Report> root; use append or prepend")
    NameRegistry(doc).ensure_unique(*_scope_names(nodes))
    before = _baseline(doc)
    for node in nodes:
        node.tail = None
    if position == "append":
        for node in nodes:
            anchor.append(node)
    elif position == "prepend":
        for i, node in enumerate(nodes):
            anchor.insert(i, node)
    elif position == "before":
        for node in nodes:
            anchor.addprevious(node)
    else:
        for node in reversed(nodes):
            anchor.addnext(node)
    _save_checked(doc, before)
    return {
        "xpath": xpath,
        "position": position,
        "inserted": [etree.QName(n).localname for n in nodes],
        "changed": True,
    }


def raw_xml_remove(path: str, xpath: str, all: bool = False) -> dict[str, Any]:  # noqa: A002
    """Remove the element ``xpath`` matches (every match with ``all``).

    Refuses (nothing saved) no match (``ElementNotFoundError``), several
    matches unless ``all=true``, a non-element match, the ``<Report>``
    root, and a result with XSD / structural errors the report didn't have
    (e.g. removing a required element; outside a transaction). The
    surrounding layout is kept. Returns ``{xpath, removed, count, changed:
    true}``; ``removed`` lists the local names of the removed elements.
    """
    doc = RDLDocument.open(path)
    matches = _elements(doc, xpath, "raw_xml_remove")
    if any(m is doc.root for m in matches):
        raise ValueError("refusing to remove the <Report> root — that would corrupt the document")
    if len(matches) > 1 and not all:
        raise ValueError(
            f"xpath {xpath!r} matched {len(matches)} elements; narrow it to one, or pass "
            "all=true to remove every match (list them with raw_xml_view first)"
        )
    picked = {id(m) for m in matches}
    # A match inside another match goes with it.
    outer = [m for m in matches if not any(id(a) in picked for a in m.iterancestors())]
    before = _baseline(doc)
    for elem in outer:
        parent = elem.getparent()
        detach(elem)
        if len(parent) == 0 and parent.text is not None and not parent.text.strip():
            parent.text = None
    _save_checked(doc, before)
    return {
        "xpath": xpath,
        "removed": [etree.QName(m).localname for m in outer],
        "count": len(outer),
        "changed": True,
    }


__all__ = [
    "INSERT_POSITIONS",
    "raw_xml_insert",
    "raw_xml_remove",
    "raw_xml_replace",
    "raw_xml_view",
]
