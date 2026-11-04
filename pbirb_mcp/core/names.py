"""One report-wide name registry + RDL identifier validation.

RDL needs two things of every ``Name`` attribute that the bundled XSD
does not check (it types ``Name`` as a plain ``normalizedString``):

1. **Report-scope uniqueness.** Report items (Textbox, Rectangle, Image,
   Line, Subreport, Tablix, Chart, GaugePanel, Map, CustomReportItem) and
   groups (``<Group>`` in a tablix / chart / gauge / map / custom item)
   share ONE namespace across the whole report — body, page header, page
   footer and every nested container. Report Builder refuses a file with
   two report items or groups of the same name, and this server's own
   name-based resolvers raise ``AmbiguousElementError`` on them. Checking
   only the container being written into (Body, one section, one
   tablix) is not enough.
2. **CLS-compliant identifiers.** Report Builder accepts only names that
   start with a letter and continue with letters, digits or underscores
   (``^[^\\W\\d_]\\w*$``) for report items, groups, datasets, fields,
   parameters, data sources and embedded images.

Per-kind namespaces (datasets, parameters, data sources, embedded
images, the fields of one dataset) keep their own uniqueness checks in
their modules; they only use :func:`validate_identifier` from here.

Public helpers:

* :func:`report_scope_names` — every report item + group name.
* :func:`ensure_unique` — raise ``ValueError`` naming the clash location.
* :func:`unique_name` — Report Builder-style numeric suffixing for
  generated names (``Details`` → ``Details1`` → ``Details2``).
* :func:`validate_identifier` — identifier rule + length cap.
* :class:`NameRegistry` — the same operations over one snapshot of the
  report, for creators that emit several names in one call (it also
  remembers names generated earlier in the same call, before they are in
  the tree).

Length cap: neither the RDL 2016 specification nor Report Builder
documents a maximum identifier length, so :data:`MAX_IDENTIFIER_LENGTH`
is a deliberately generous 256 characters — long enough never to bite a
real name, short enough to stop a runaway generated one.

Case: names are compared case-sensitively, matching the resolvers in
:mod:`pbirb_mcp.core.ids` (and RDL's XML attribute semantics).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.xpath import RDL_NS

# Every element type that can sit in a <ReportItems> / <CellContents>
# slot (RDL 2016 ReportItemsType choice). Each name is unique in the
# XSD — no other RDL element shares these local names — so a plain tag
# walk is exact.
REPORT_ITEM_KINDS = (
    "Textbox",
    "Rectangle",
    "Image",
    "Line",
    "Subreport",
    "Tablix",
    "Chart",
    "GaugePanel",
    "Map",
    "CustomReportItem",
)

# Report-scope = report items + groups (GroupType is only ever emitted
# as an element named <Group>).
SCOPE_KINDS = REPORT_ITEM_KINDS + ("Group",)

MAX_IDENTIFIER_LENGTH = 256

_IDENTIFIER_RE = re.compile(r"[^\W\d_]\w*")
_NON_WORD_RE = re.compile(r"\W+")
_SCOPE_TAGS = tuple(f"{{{RDL_NS}}}{k}" for k in SCOPE_KINDS)
_SECTION_KINDS = ("Body", "PageHeader", "PageFooter")


def _root(doc: Any) -> etree._Element:
    """Accept an :class:`~pbirb_mcp.core.document.RDLDocument`, an
    ``ElementTree`` or a bare element."""
    root = getattr(doc, "root", None)
    if root is not None:
        return root
    if isinstance(doc, etree._ElementTree):
        return doc.getroot()
    return doc


def _local(el: etree._Element) -> str:
    return etree.QName(el).localname


def iter_scope_elements(doc: Any) -> Iterator[etree._Element]:
    """Yield every named report item and ``<Group>`` in document order."""
    for el in _root(doc).iter(*_SCOPE_TAGS):
        if el.get("Name") is not None:
            yield el


def describe_location(el: etree._Element) -> str:
    """Human-readable location of a named element, e.g.
    ``"Textbox 'Amount' in Tablix 'MainTable' in Body"``."""
    parts = [f"{_local(el)} {el.get('Name')!r}"]
    cur = el.getparent()
    while cur is not None:
        if not isinstance(cur.tag, str):
            cur = cur.getparent()
            continue
        local = _local(cur)
        if local in REPORT_ITEM_KINDS and cur.get("Name") is not None:
            parts.append(f"{local} {cur.get('Name')!r}")
        elif local in _SECTION_KINDS:
            parts.append(local)
            break
        cur = cur.getparent()
    return " in ".join(parts)


# ---- identifiers ----------------------------------------------------------


def is_identifier(name: object) -> bool:
    """True when ``name`` is a valid RDL (CLS-compliant) identifier
    within :data:`MAX_IDENTIFIER_LENGTH`."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_IDENTIFIER_LENGTH
        and _IDENTIFIER_RE.fullmatch(name) is not None
    )


def sanitize_identifier(base: str, fallback: str = "Item") -> str:
    """Turn arbitrary text into a valid identifier: runs of non-word
    characters become ``_``, a non-letter start gets ``fallback`` as a
    prefix, and the result is clipped to the length cap."""
    text = _NON_WORD_RE.sub("_", base if isinstance(base, str) else "").strip("_")
    if not text:
        text = fallback
    elif _IDENTIFIER_RE.match(text) is None:
        text = f"{fallback}{text}" if text[0].isdigit() else f"{fallback}_{text.lstrip('_')}"
    return text[:MAX_IDENTIFIER_LENGTH]


def validate_identifier(name: object, kind: str = "item") -> str:
    """Return ``name`` unchanged if it is a valid RDL identifier, else
    raise ``ValueError``.

    The rule (Report Builder's, a CLS-compliant identifier): non-empty,
    at most :data:`MAX_IDENTIFIER_LENGTH` characters, starts with a
    letter, continues with letters, digits or underscores only. ``kind``
    ("textbox", "parameter", "data source", …) only shapes the message.
    """
    if not isinstance(name, str):
        raise ValueError(f"{kind} name must be a string; got {type(name).__name__}")
    if name == "":
        raise ValueError(f"{kind} name must not be empty")
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"{kind} name is {len(name)} characters long; the limit is {MAX_IDENTIFIER_LENGTH}"
        )
    if _IDENTIFIER_RE.fullmatch(name) is None:
        raise ValueError(
            f"invalid {kind} name {name!r}: RDL names must start with a letter and "
            "contain only letters, digits and underscores (no spaces or punctuation); "
            f"e.g. {sanitize_identifier(name)!r}"
        )
    return name


# ---- report-scope registry ------------------------------------------------


class NameRegistry:
    """Snapshot of a report's report-scope names (report items + groups)
    plus the names claimed so far by the current call.

    Typical creator flow::

        reg = NameRegistry(doc)
        reg.ensure_unique(name)          # user-supplied: raise on clash
        reg.claim(name)
        tb = reg.unique(f"{name}_Rect")  # generated: suffix on clash
    """

    def __init__(self, doc: Any, exclude: Iterable[etree._Element] = ()) -> None:
        skip: set[int] = set()
        for sub in exclude:
            for el in sub.iter(*_SCOPE_TAGS):
                skip.add(id(el))
        self._where: dict[str, str] = {}
        for el in iter_scope_elements(doc):
            if id(el) in skip:
                continue
            self._where.setdefault(el.get("Name"), describe_location(el))

    def __contains__(self, name: object) -> bool:
        return name in self._where

    def names(self) -> set[str]:
        return set(self._where)

    def location(self, name: str) -> Optional[str]:
        return self._where.get(name)

    def ensure_unique(self, *names: str) -> None:
        """Raise ``ValueError`` if any of ``names`` is already taken (in the
        report or by an earlier claim in this call) or repeats within
        ``names`` itself."""
        seen: set[str] = set()
        for n in names:
            if n in seen:
                raise ValueError(
                    f"name {n!r} would be emitted twice by this call; report item and "
                    "group names must be unique report-wide"
                )
            seen.add(n)
            where = self._where.get(n)
            if where is not None:
                raise ValueError(
                    f"name {n!r} already exists in this report ({where}); report item and "
                    "group names must be unique report-wide — pick a different name"
                )

    def claim(self, name: str, where: str = "(this call)") -> str:
        """Record ``name`` as taken without checking (call
        :meth:`ensure_unique` first for user-supplied names)."""
        self._where.setdefault(name, where)
        return name

    def unique(self, base: str) -> str:
        """Claim and return a free name derived from ``base``.

        ``base`` itself when free; otherwise Report Builder-style numeric
        suffixes ``base1``, ``base2``, … When ``base`` already ends in a
        digit (``Region_Header_0``) the suffix is joined with ``_``
        (``Region_Header_0_1``) so the counter stays readable. A ``base``
        that is not a valid identifier is sanitized first.
        """
        stem = base if is_identifier(base) else sanitize_identifier(base)
        if stem not in self._where:
            return self.claim(stem)
        sep = "_" if stem[-1].isdigit() else ""
        i = 1
        while True:
            suffix = f"{sep}{i}"
            candidate = stem[: MAX_IDENTIFIER_LENGTH - len(suffix)] + suffix
            if candidate not in self._where:
                return self.claim(candidate)
            i += 1


def report_scope_names(doc: Any) -> set[str]:
    """Every report item name (body, header, footer, nested at any depth)
    and every ``<Group>`` name (tablix, chart, gauge, map, custom item)."""
    return {el.get("Name") for el in iter_scope_elements(doc)}


def ensure_unique(doc: Any, *names: str) -> None:
    """Raise ``ValueError`` (naming where the clash is) if any of ``names``
    already exists in the report scope or repeats within ``names``."""
    NameRegistry(doc).ensure_unique(*names)


def unique_name(doc: Any, base: str, taken: Optional[set[str]] = None) -> str:
    """Return a report-scope-free name derived from ``base`` (see
    :meth:`NameRegistry.unique`).

    ``taken`` holds names already generated by the same call that are not
    in the tree yet; the returned name is added to it, so a loop can call
    ``unique_name(doc, base, taken)`` repeatedly and never get a repeat.
    """
    reg = NameRegistry(doc)
    for n in taken or ():
        reg.claim(n)
    result = reg.unique(base)
    if taken is not None:
        taken.add(result)
    return result


__all__ = [
    "MAX_IDENTIFIER_LENGTH",
    "NameRegistry",
    "REPORT_ITEM_KINDS",
    "SCOPE_KINDS",
    "describe_location",
    "ensure_unique",
    "is_identifier",
    "iter_scope_elements",
    "report_scope_names",
    "sanitize_identifier",
    "unique_name",
    "validate_identifier",
]
