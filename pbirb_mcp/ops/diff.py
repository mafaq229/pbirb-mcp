"""Semantic diff of two report trees (issues #84, #87).

Text diffs of RDL are dominated by layout, attribute order, BOM/newline
form and element order. This module compares two ``<Report>`` trees at the
RDL level instead:

* **Named nodes** are matched by identity, not position: report items and
  groups (one report-wide namespace), datasets, data sources, report
  parameters and embedded images by ``(element type, Name)``; any other
  element with a ``Name`` attribute (fields, query parameters, chart
  series/areas/axes, variables, ...) by ``(element type, Name)`` *within*
  its nearest named ancestor.
* Everything else is compared below its **owner** — the nearest named
  ancestor, or one of the anchors ``Report`` / ``ReportSection`` / ``Body``
  / ``Page`` / ``PageHeader`` / ``PageFooter`` — by path. Repeated
  unnamed siblings (columns, rows, cells, members, paragraphs, ...) are
  aligned by content, so an inserted column shows up as one added
  ``TablixColumn`` rather than as every later column "changing".

The result has five lists:

* ``added`` / ``removed`` — ``{kind, name, parent}`` for named nodes;
  ``{kind, name: null, parent, path}`` for unnamed collection members
  (``TablixColumn``, ``TablixCell``, ``TablixMember``, ``Paragraph``, ...;
  the element types the XSD lets repeat) — one entry per subtree.
* ``changed`` — ``{kind, name, property, from, to}``: a leaf value or an
  attribute (``.../@Attr``) of the owner ``kind``/``name`` (``name`` is
  null for an anchor) at ``property``, its path below the owner.
  ``from``/``to`` is null when the property is absent on that side.
* ``moved`` — ``{kind, name, from, to}``: a named node whose owner changed
  (e.g. a textbox moved from ``Body`` into a rectangle).
* ``reordered`` — ``{kind, name, path, from, to}``: the named children of
  ``path`` below the owner kept but in a different order (``from``/``to``
  list their names).

Ignored: layout whitespace, attribute order, comments, the byte form
(BOM, newlines, encoding) and — with ``ignore_ids`` (the default) — the
designer's ``rd:*ID`` elements (``rd:ReportID``, ``rd:DataSourceID``),
which Report Builder regenerates. Text is compared after
:func:`pbirb_mcp.core.encoding.encode_text` normalisation, so ``&amp;``
written literally and ``&`` compare equal.
"""

from __future__ import annotations

import difflib
import functools
import hashlib
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.names import SCOPE_KINDS
from pbirb_mcp.core.xpath import RD_NS

# Named kinds whose names are unique report-wide (per kind): matched by
# (kind, Name) wherever they sit, so a move between containers is a move.
_GLOBAL_KINDS = frozenset(SCOPE_KINDS) | {
    "DataSet",
    "DataSource",
    "ReportParameter",
    "EmbeddedImage",
}

# Unnamed singletons that own the properties below them.
_ANCHORS = frozenset({"ReportSection", "Body", "Page", "PageHeader", "PageFooter"})

LISTS = ("added", "removed", "changed", "moved", "reordered")


@functools.lru_cache(maxsize=1)
def _member_tags() -> frozenset:
    """Local names the bundled XSD lets repeat (``maxOccurs`` > 1):
    collection members such as ``TablixColumn`` / ``TablixCell`` /
    ``Paragraph``. Empty when the XSD isn't bundled."""
    from pbirb_mcp.core.schema import _bundled_xsd_path

    try:
        xsd = etree.parse(str(_bundled_xsd_path()))
    except (OSError, etree.XMLSyntaxError):
        return frozenset()
    tag = "{http://www.w3.org/2001/XMLSchema}element"
    return frozenset(
        e.get("name") for e in xsd.iter(tag) if e.get("name") and e.get("maxOccurs", "1") != "1"
    )


def _norm(text: Optional[str]) -> str:
    if not text:
        return ""
    return encode_text(text).replace("\r\n", "\n").replace("\r", "\n")


def _leaf_text(text: Optional[str]) -> str:
    # A leaf's text is compared exactly, except pretty-printing whitespace
    # (`<Style>\n  </Style>` is `<Style />`).
    if text and not text.strip() and "\n" in text:
        return ""
    return _norm(text)


def _local(elem: etree._Element) -> str:
    return etree.QName(elem).localname


def _label(elem: etree._Element) -> str:
    """``Value``, ``rd:DefaultName``, ``am:CreatedBy``: the tag as written."""
    local = _local(elem)
    return f"{elem.prefix}:{local}" if elem.prefix else local


def _attr_label(elem: etree._Element, name: str) -> str:
    if not name.startswith("{"):
        return name
    q = etree.QName(name)
    for prefix, uri in (elem.nsmap or {}).items():
        if uri == q.namespace and prefix:
            return f"{prefix}:{q.localname}"
    return q.localname


def _join(base: str, seg: str) -> str:
    return f"{base}/{seg}" if base else seg


class _Side:
    """One report tree, indexed for the diff."""

    def __init__(self, root: etree._Element, ignore_ids: bool) -> None:
        self.root = root
        self.ignore_ids = ignore_ids
        self.key: dict[int, tuple] = {}  # id(elem) -> identity
        self.by_key: dict[tuple, etree._Element] = {}
        self._refs: list = []  # keep proxies alive so ids stay unique
        self._sig: dict[int, str] = {}
        for elem in root.iter():
            if self.ignored(elem) or elem.get("Name") is None:
                continue
            local = _local(elem)
            if local in _GLOBAL_KINDS:
                key: tuple = (local, elem.get("Name"))
            else:
                key = (self._owner_key(elem.getparent()), local, elem.get("Name"))
            n = 1
            base = key
            while key in self.by_key:  # duplicate names: keep both apart
                n += 1
                key = base + (f"#{n}",)
            self.key[id(elem)] = key
            self.by_key[key] = elem
            self._refs.append(elem)

    def ignored(self, elem: Any) -> bool:
        if not isinstance(elem.tag, str):
            return True  # comment / processing instruction
        if self.ignore_ids:
            q = etree.QName(elem)
            return q.namespace == RD_NS and q.localname.endswith("ID")
        return False

    def children(self, elem: etree._Element) -> list:
        return [c for c in elem if not self.ignored(c)]

    def _owner_key(self, elem: Optional[etree._Element]) -> Optional[tuple]:
        while elem is not None:
            k = self.key.get(id(elem))
            if k is not None:
                return k
            elem = elem.getparent()
        return None

    def owner(self, elem: Optional[etree._Element]) -> tuple:
        """``(kind, name, identity)`` of the nearest owner at or above ``elem``."""
        while elem is not None:
            k = self.key.get(id(elem))
            if k is not None:
                return (_local(elem), elem.get("Name"), k)
            local = _local(elem)
            if local in _ANCHORS or elem is self.root:
                return (local, None, (local, self.path(elem)))
            elem = elem.getparent()
        return ("Report", None, ("Report", ""))

    def path(self, elem: etree._Element) -> str:
        """Positional path of ``elem`` from the root (anchor identity)."""
        segs = []
        while elem is not None and elem is not self.root:
            segs.append(self.seg(elem))
            elem = elem.getparent()
        return "/".join(reversed(segs))

    def seg(self, elem: etree._Element) -> str:
        parent = elem.getparent()
        label = _label(elem)
        if parent is None:
            return label
        same = [c for c in parent if c.tag == elem.tag and not self.ignored(c)]
        if len(same) > 1:
            return f"{label}[{same.index(elem) + 1}]"
        return label

    def chain(self, elem: Optional[etree._Element]) -> set:
        """Names and anchor kinds of every owner at or above ``elem``."""
        out = set()
        while elem is not None:
            if id(elem) in self.key:
                out.add(elem.get("Name"))
            elif _local(elem) in _ANCHORS:
                out.add(_local(elem))
            elem = elem.getparent()
        return out

    def sig(self, elem: etree._Element) -> str:
        """Digest of ``elem``'s normalised content (for aligning siblings)."""
        cached = self._sig.get(id(elem))
        if cached is not None:
            return cached
        kids = self.children(elem)
        h = hashlib.blake2b(digest_size=16)
        h.update(str(elem.tag).encode())
        for k, v in sorted(elem.attrib.items()):
            h.update(b"\0@" + k.encode() + b"=" + _norm(v).encode())
        text = _leaf_text(elem.text) if not kids else ""
        h.update(b"\0#" + text.encode())
        for c in kids:
            h.update(b"\0>" + self.sig(c).encode())
        digest = h.hexdigest()
        self._sig[id(elem)] = digest
        self._refs.append(elem)
        return digest


class _Differ:
    def __init__(self, a: _Side, b: _Side) -> None:
        self.a = a
        self.b = b
        self.out: dict[str, list] = {k: [] for k in LISTS}

    # ---- recording -----------------------------------------------------------

    def _emit(self, kind: str, entry: dict, chain: set, where: Optional[str] = None) -> None:
        entry["_chain"] = chain
        if where is not None:
            entry["_where"] = where
        self.out[kind].append(entry)

    def _changed(self, owner: tuple, prop: str, old: Any, new: Any, chain: set) -> None:
        self._emit(
            "changed",
            {"kind": owner[0], "name": owner[1], "property": prop or ".", "from": old, "to": new},
            chain,
        )

    # ---- walk ----------------------------------------------------------------

    def run(self) -> dict[str, list]:
        a, b = self.a, self.b
        self._cmp(a.root, b.root, a.owner(a.root), "")
        for key, eb in b.by_key.items():
            kind, name = _local(eb), eb.get("Name")
            ea = a.by_key.get(key)
            if ea is None:
                parent = b.owner(eb.getparent())
                self._emit(
                    "added",
                    {"kind": kind, "name": name, "parent": parent[1] or parent[0]},
                    b.chain(eb),
                )
                continue
            pa, pb = a.owner(ea.getparent()), b.owner(eb.getparent())
            if pa[2] != pb[2]:
                self._emit(
                    "moved",
                    {"kind": kind, "name": name, "from": pa[1] or pa[0], "to": pb[1] or pb[0]},
                    a.chain(ea) | b.chain(eb),
                )
            self._cmp(ea, eb, (kind, name, key), "")
        for key, ea in a.by_key.items():
            if key not in b.by_key:
                parent = a.owner(ea.getparent())
                self._emit(
                    "removed",
                    {"kind": _local(ea), "name": ea.get("Name"), "parent": parent[1] or parent[0]},
                    a.chain(ea),
                )
        return self.out

    def _cmp(self, ea: etree._Element, eb: etree._Element, owner: tuple, rel: str) -> None:
        """Compare two matched elements; ``rel`` is their path below ``owner``."""
        a, b = self.a, self.b
        if a.sig(ea) == b.sig(eb):
            return
        chain = b.chain(eb)
        named = id(eb) in b.key
        for attr in sorted(set(ea.attrib) | set(eb.attrib)):
            if named and attr == "Name":
                continue
            va, vb = ea.get(attr), eb.get(attr)
            if (va is None) != (vb is None) or _norm(va) != _norm(vb):
                self._changed(owner, _join(rel, "@" + _attr_label(eb, attr)), va, vb, chain)
        ka, kb = a.children(ea), b.children(eb)
        if not ka and not kb:
            ta, tb = _leaf_text(ea.text), _leaf_text(eb.text)
            if ta != tb:
                self._changed(owner, rel, ta, tb, chain)
            return
        # Named children are compared by identity (in run); here only
        # their order matters.
        na = [a.key[id(c)] for c in ka if id(c) in a.key]
        nb = [b.key[id(c)] for c in kb if id(c) in b.key]
        common = set(na) & set(nb)
        fa = [k for k in na if k in common]
        fb = [k for k in nb if k in common]
        if fa != fb:
            self._emit(
                "reordered",
                {
                    "kind": owner[0],
                    "name": owner[1],
                    "path": rel or ".",
                    "from": [a.by_key[k].get("Name") for k in fa],
                    "to": [b.by_key[k].get("Name") for k in fb],
                },
                chain,
            )
        ua = [c for c in ka if id(c) not in a.key]
        ub = [c for c in kb if id(c) not in b.key]
        matcher = difflib.SequenceMatcher(
            None, [a.sig(c) for c in ua], [b.sig(c) for c in ub], autojunk=False
        )
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "equal":
                continue
            right = ub[j1:j2]
            used = [False] * len(right)
            for x in ua[i1:i2]:
                j = next(
                    (j for j, y in enumerate(right) if not used[j] and y.tag == x.tag),
                    None,
                )
                if j is None:
                    self._one_side(x, a, owner, rel, "removed")
                else:
                    used[j] = True
                    self._descend(x, right[j], owner, rel)
            for j, y in enumerate(right):
                if not used[j]:
                    self._one_side(y, b, owner, rel, "added")

    def _descend(self, x: etree._Element, y: etree._Element, owner: tuple, rel: str) -> None:
        if _local(y) in _ANCHORS:
            self._cmp(x, y, self.b.owner(y), "")
        else:
            self._cmp(x, y, owner, _join(rel, self.b.seg(y)))

    def _one_side(self, elem: etree._Element, side: _Side, owner: tuple, rel: str, which: str):
        """An unnamed element present on one side only (``which``)."""
        path = _join(rel, side.seg(elem))
        chain = side.chain(elem)
        kids = side.children(elem)
        local = _local(elem)
        members = _member_tags()
        member = local in members or local in _ANCHORS or (not members and bool(kids))
        # A member with content is one entry; a repeated leaf value
        # (<Values><Value>x</Value>) reads better as a changed value.
        if member and (kids or not _leaf_text(elem.text)):
            self._emit(
                which,
                {"kind": local, "name": None, "parent": owner[1] or owner[0], "path": path},
                chain,
                where=_owner_label(owner[0], owner[1]),
            )
            return
        for attr in sorted(elem.attrib):
            value = elem.get(attr)
            prop = _join(path, "@" + _attr_label(elem, attr))
            if which == "added":
                self._changed(owner, prop, None, value, chain)
            else:
                self._changed(owner, prop, value, None, chain)
        if not kids:
            text = _leaf_text(elem.text)
            if which == "added":
                self._changed(owner, path, None, text, chain)
            else:
                self._changed(owner, path, text, None, chain)
            return
        for child in kids:
            if id(child) not in side.key:  # named ones are reported by identity
                self._one_side(child, side, owner, path, which)


def semantic_diff(
    root_a: etree._Element, root_b: etree._Element, *, ignore_ids: bool = True
) -> dict[str, list]:
    """The changes from ``root_a`` to ``root_b`` (see the module docstring).

    Entries carry private ``_chain`` (the owner names / anchor kinds
    around them, for scope filtering) and ``_where`` (the owner's label,
    for :func:`summary_paths`) fields; :func:`public_entries` drops them.
    """
    return _Differ(_Side(root_a, ignore_ids), _Side(root_b, ignore_ids)).run()


def public_entries(result: dict[str, list]) -> dict[str, list]:
    return {
        k: [{f: v for f, v in e.items() if not f.startswith("_")} for e in result[k]] for k in LISTS
    }


def _owner_label(kind: str, name: Optional[str]) -> str:
    return f"{kind}[{name}]" if name is not None else kind


def summary_paths(result: dict[str, list]) -> list[str]:
    """One ``"<change> <path>"`` line per touched element, e.g.
    ``changed Textbox[Title]/Paragraphs/Paragraph/TextRuns/TextRun/Value``."""
    lines: list[str] = []
    for which in ("added", "removed"):
        for e in result[which]:
            if e["name"] is not None:
                lines.append(f"{which} {_owner_label(e['kind'], e['name'])}")
            else:
                lines.append(f"{which} {e.get('_where', e['parent'])}/{e['path']}")
    for e in result["changed"]:
        prop = "" if e["property"] == "." else "/" + e["property"]
        lines.append(f"changed {_owner_label(e['kind'], e['name'])}{prop}")
    for e in result["moved"]:
        lines.append(f"moved {_owner_label(e['kind'], e['name'])}")
    for e in result["reordered"]:
        path = "" if e["path"] == "." else "/" + e["path"]
        lines.append(f"reordered {_owner_label(e['kind'], e['name'])}{path}")
    seen: set = set()
    return [x for x in lines if not (x in seen or seen.add(x))]


__all__ = ["LISTS", "public_entries", "semantic_diff", "summary_paths"]
