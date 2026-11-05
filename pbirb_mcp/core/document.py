"""RDLDocument — open / save / validate with round-trip fidelity.

Power BI Report Builder is sensitive to formatting drift: re-serialised
documents that don't preserve namespace prefixes, attribute order, and
declaration style can fail to open or silently lose metadata. This module
parses with lxml configured to preserve the original tree exactly, and
serialises with the same encoding and declaration style as the source.

Key behaviours:

* **Atomic save** — write to ``<path>.tmp`` then rename. A failure mid-write
  never leaves a half-written report or scrubs the original.
* **Structural validation** — runs on demand via :meth:`RDLDocument.validate`.
  XSD validation is opt-in: drop the Microsoft RDL 2016 XSD into
  ``pbirb_mcp/schemas/reportdefinition.xsd`` and it picks up automatically.
* **Byte-faithful round-trip** — :meth:`RDLDocument.open` records the
  source's byte form (UTF-8 BOM, CRLF vs LF line ends, trailing newline,
  the XML declaration, the exact root start tag, ``<X></X>`` vs ``<X />``
  empty elements) and :meth:`RDLDocument.save_as` re-emits it, so a no-op
  open → save is byte-identical and a one-value edit changes one line.
  Elements inserted since open are indented to their parent's depth.
* **No needless writes** (#61) — :meth:`RDLDocument.save` skips the write
  when the document would serialise to the bytes already on disk, or to a
  tree canonically equal (C14N) to the one parsed, and the file hasn't
  been touched since. A no-op edit keeps the file's inode and mtime.
"""

from __future__ import annotations

import contextlib
import copy
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from lxml import etree

from pbirb_mcp.core.schema import (
    RDLValidationError,
    validate_against_xsd,
    validate_structure,
)
from pbirb_mcp.core.xpath import RD_NS, RDL_NS

# Report Builder writes self-closing tags as `<Tag />` with a space before the
# slash; lxml emits `<Tag/>`. Attribute values are always quoted in XML, so a
# `/>` byte sequence only ever appears as a self-closing tag terminator —
# making this substitution safe without a real XML parse.
_SELF_CLOSING_NORMALISE = re.compile(rb"(?<=[^ /])/>")

# Markup tokens, in document order. Used to recover per-element byte facts
# lxml's tree doesn't carry (self-closing vs `<X></X>`, the root start tag
# bytes). Attribute values may contain `>` but never `<`, so a start tag is
# `<name` up to the first `>` outside quotes.
_MARKUP_TOKEN = re.compile(
    rb"<!--.*?-->"
    rb"|<!\[CDATA\[.*?\]\]>"
    rb"|<\?.*?\?>"
    rb"|<!DOCTYPE(?:[^\[>]|\[.*?\])*>"
    rb"|</[^>]*>"
    rb"|<(?P<start>[^!?/>\s][^>\"']*(?:(?:\"[^\"]*\"|'[^']*')[^>\"']*)*)>",
    re.DOTALL,
)
_DECLARATION = re.compile(rb"<\?xml\s[^?]*\?>")
_BOM = b"\xef\xbb\xbf"
# Encodings in which `\n`, `<` and `>` are single ASCII bytes, so the
# byte-level fix-ups below are safe. Anything else (UTF-16, …) falls back to
# plain serialisation.
_ASCII_COMPATIBLE = {"utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "latin-1", "cp1252"}

PathLike = Union[str, os.PathLike]


@dataclass
class _SourceForm:
    """Byte-level facts about the file a document was parsed from."""

    has_bom: bool = False
    newline: bytes = b"\n"
    # Whitespace after the last markup (b"" when the file ends at `>`).
    trailing: bytes = b"\n"
    # Original `<?xml …?>` bytes; None when the source had no declaration.
    declaration: Optional[bytes] = None
    # Bytes between the declaration and the next markup (usually a newline).
    after_declaration: bytes = b"\n"
    # Original root start tag, plus the root identity it was valid for.
    root_start_tag: Optional[bytes] = None
    root_signature: Optional[tuple] = None
    # True when the source writes self-closing tags as `<X />` (Report
    # Builder style); False for `<X/>`.
    spaced_self_close: bool = True
    # False for documents built in memory (no source file).
    from_source: bool = False


def _root_signature(root: etree._Element) -> tuple:
    return (
        root.tag,
        tuple(root.attrib.items()),
        tuple(sorted((root.nsmap or {}).items(), key=str)),
    )


def _scan_source(raw: bytes, tree: etree._ElementTree) -> tuple:
    """Return ``(form, expanded)``: the source's byte form, and the elements
    written as ``<X></X>`` in the source (lxml can't tell them apart from
    ``<X />`` once parsed)."""
    form = _SourceForm(from_source=True)
    data = raw
    if data.startswith(_BOM):
        form.has_bom = True
        data = data[len(_BOM) :]
    crlf = data.count(b"\r\n")
    lone_lf = data.count(b"\n") - crlf
    form.newline = b"\r\n" if crlf > lone_lf else b"\n"
    stripped = data.rstrip(b" \t\r\n")
    form.trailing = data[len(stripped) :]
    m = _DECLARATION.match(data)
    if m:
        form.declaration = m.group(0)
        rest = data[m.end() :]
        form.after_declaration = rest[: len(rest) - len(rest.lstrip(b" \t\r\n"))]
    else:
        form.declaration = None

    elements = [e for e in tree.iter() if isinstance(e.tag, str)]
    starts = []
    spaced = unspaced = 0
    for tok in _MARKUP_TOKEN.finditer(data):
        if tok.group("start") is None:
            continue
        body = tok.group("start")
        self_closing = body.endswith(b"/")
        if self_closing:
            if body.endswith(b" /"):
                spaced += 1
            else:
                unspaced += 1
        # `<X …></X>` — a start tag immediately followed by its end tag.
        expanded_empty = not self_closing and data.startswith(b"</", tok.end())
        starts.append((tok, expanded_empty))
    form.spaced_self_close = spaced >= unspaced

    expanded = []
    if starts and len(starts) == len(elements):
        form.root_start_tag = starts[0][0].group(0)
        form.root_signature = _root_signature(tree.getroot())
        expanded = [
            elem
            for elem, (_tok, is_expanded) in zip(elements, starts)
            if is_expanded and elem.text is None and len(elem) == 0
        ]
    return form, expanded


def _find_root_start_tag(body: bytes) -> Optional[re.Match]:
    for tok in _MARKUP_TOKEN.finditer(body):
        if tok.group("start") is not None:
            return tok
    return None


def _indent_unit(root: etree._Element) -> str:
    text = root.text or ""
    if "\n" in text:
        unit = text.rsplit("\n", 1)[1]
        if unit and not unit.strip():
            return unit
    return "  "


def _is_ws(text: Optional[str]) -> bool:
    return text is None or not text.strip()


def indent_inserted(node: etree._Element, unit: str = "  ") -> None:
    """Indent a freshly inserted subtree to its position in a pretty-printed tree.

    Sets the whitespace before ``node`` (the previous sibling's tail, or the
    parent's text), ``node``'s own tail, and the subtree's internal
    indentation. Only whitespace-only text/tails are touched, so element
    content is never altered. No-op when the parent is laid out on one line
    (its existing children carry no newlines).
    """
    parent = node.getparent()
    if parent is None:
        return
    siblings = [c for c in parent if c is not node]
    holders = [parent.text] + [c.tail for c in siblings]
    if siblings and not any(h and "\n" in h for h in holders):
        return  # compact parent — keep it compact
    depth = sum(1 for _ in node.iterancestors())
    child_ws = "\n" + unit * depth
    close_ws = "\n" + unit * (depth - 1)
    prev = node.getprevious()
    if prev is not None:
        if _is_ws(prev.tail):
            prev.tail = child_ws
    elif _is_ws(parent.text):
        parent.text = child_ws
    if _is_ws(node.tail):
        node.tail = child_ws if node.getnext() is not None else close_ws
    if len(node):
        tail = node.tail
        etree.indent(node, space=unit, level=depth)
        node.tail = tail


def _stat_signature(path: Path) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


@dataclass
class _DiskState:
    """The file a document was read from or last written to (#61)."""

    target: str  # resolve()d path
    data: bytes
    stat: Optional[tuple]


def _canonical(tree: etree._ElementTree) -> Optional[bytes]:
    try:
        return etree.tostring(tree, method="c14n", with_comments=True)
    except Exception:  # noqa: BLE001 — no canonical form, no shortcut
        return None


@dataclass
class RDLDocument:
    """A loaded RDL document with namespace-aware tree access."""

    path: Path
    tree: etree._ElementTree
    encoding: str
    # Byte form of the source file (BOM, newlines, …). Documents built in
    # memory (create_report) get Report Builder's defaults.
    source_form: _SourceForm = field(default_factory=_SourceForm, repr=False)
    # ids of the element proxies present at open/last save. Elements not in
    # this set were inserted since and get indented on save. None disables
    # the pass (documents built in memory).
    _known: Optional[set] = field(default=None, repr=False, compare=False)
    # Keeps the proxies behind `_known` alive so their ids stay unique.
    _known_refs: Optional[list] = field(default=None, repr=False, compare=False)
    # Elements the source wrote as `<X></X>`; re-emitted that way while
    # they stay empty.
    _expanded: list = field(default_factory=list, repr=False, compare=False)
    # What this document last read from / wrote to disk: the file it
    # belongs to, its bytes and its stat signature. save() skips the
    # write when nothing would change (#61). None for documents built in
    # memory or copied (copy_to) — those always write.
    _on_disk: Optional[_DiskState] = field(default=None, repr=False, compare=False)

    # ---- construction --------------------------------------------------------

    @classmethod
    def open(cls, path: PathLike) -> RDLDocument:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        # v0.4 transaction interception: when the current call runs
        # under the editing transaction that owns this path, return the
        # live in-memory tree instead of re-parsing. Identity-equal
        # across repeated open() calls — handlers that do
        # `doc = RDLDocument.open(path)` reuse the same in-memory tree
        # all calls of that transaction share. Calls without the
        # transaction's id read the on-disk file; saving that copy back
        # over the held path is refused (see save_as). Local import
        # dodges the module-load circular dep.
        from pbirb_mcp.core import transactions as _tx

        existing = _tx.owned_transaction(str(path.resolve()))
        if existing is not None:
            return existing.doc
        # Preserve everything: comments, CDATA, whitespace.
        parser = etree.XMLParser(
            remove_blank_text=False,
            remove_comments=False,
            strip_cdata=False,
            resolve_entities=False,
        )
        raw = path.read_bytes()
        tree = etree.parse(io.BytesIO(raw), parser, base_url=str(path))
        encoding = tree.docinfo.encoding or "utf-8"
        form, expanded = _SourceForm(), []
        if encoding.lower() in _ASCII_COMPATIBLE:
            form, expanded = _scan_source(raw, tree)
        doc = cls(path=path, tree=tree, encoding=encoding, source_form=form)
        doc._expanded = expanded
        doc._remember_elements()
        doc._on_disk = _DiskState(str(path.resolve()), raw, _stat_signature(path))
        return doc

    def _remember_elements(self) -> None:
        refs = list(self.tree.iter())
        self._known_refs = refs
        self._known = {id(e) for e in refs}

    def snapshot(self) -> tuple:
        """Deep copy of the tree plus the bookkeeping needed to restore it."""
        tree_copy = copy.deepcopy(self.tree)
        mapping = {id(a): b for a, b in zip(self.tree.iter(), tree_copy.iter())}
        known = None
        if self._known is not None:
            known = [mapping[i] for i in self._known if i in mapping]
        expanded = [mapping[id(e)] for e in self._expanded if id(e) in mapping]
        return (tree_copy, known, expanded)

    def copy_to(self, path: PathLike) -> RDLDocument:
        """An independent document (deep-copied tree, same byte form)
        that saves to ``path``. Never part of a transaction."""
        tree_copy, known, expanded = self.snapshot()
        doc = RDLDocument(
            path=Path(path),
            tree=tree_copy,
            encoding=self.encoding,
            source_form=self.source_form,
        )
        doc._expanded = expanded
        if known is not None:
            doc._known_refs = known
            doc._known = {id(e) for e in known}
        return doc

    def restore(self, snap: tuple) -> None:
        """Put back a tree captured by :meth:`snapshot`.

        The snapshot stays reusable: the tree is copied again on restore.
        """
        tree_copy, known, expanded = snap
        fresh = copy.deepcopy(tree_copy)
        mapping = {id(a): b for a, b in zip(tree_copy.iter(), fresh.iter())}
        self.tree = fresh
        self._expanded = [mapping[id(e)] for e in expanded if id(e) in mapping]
        if known is not None:
            known = [mapping[id(e)] for e in known if id(e) in mapping]
        if known is None:
            self._known = None
            self._known_refs = None
        else:
            self._known_refs = known
            self._known = {id(e) for e in known}

    def _indent_new_elements(self) -> None:
        if self._known is None:
            return
        root = self.root
        if "\n" not in (root.text or ""):
            return  # source isn't pretty-printed — don't impose a layout
        unit = _indent_unit(root)
        known = self._known
        for elem in list(root.iter()):
            if id(elem) in known:
                continue
            parent = elem.getparent()
            if parent is not None and id(parent) in known:
                indent_inserted(elem, unit)

    # ---- accessors -----------------------------------------------------------

    @property
    def root(self) -> etree._Element:
        return self.tree.getroot()

    @property
    def nsmap(self) -> dict:
        # Always expose at least the canonical RDL + rd prefixes, even if a
        # source file omits one.
        merged = {None: RDL_NS, "rd": RD_NS}
        merged.update({k: v for k, v in self.root.nsmap.items() if v})
        return merged

    # ---- mutation lifecycle --------------------------------------------------

    def validate(self, *, with_xsd: bool = True) -> None:
        validate_structure(self.tree)
        if with_xsd:
            validate_against_xsd(self.tree)

    def save(self) -> bool:
        """Write the document back to its own path.

        Returns ``False`` (and leaves the file alone: same inode, same
        mtime) when nothing would change — see :meth:`save_as`.
        """
        return self.save_as(self.path)

    @classmethod
    @contextlib.contextmanager
    def batch(cls, path: PathLike):
        """Open once / mutate many / save once on clean exit.

        Building block for ``create_report`` and the transaction commit
        path. Independent of the transaction registry (v0.4 commit 7) —
        this is just the "open once / save once on clean exit / no save
        on exception" wrapper.

        Semantics:

        * Yields a live :class:`RDLDocument` parsed from ``path``.
        * Calls :meth:`save` exactly once when the ``with``-block exits
          cleanly — even when no mutations happened (an empty batch is
          a valid round-trip check; the file itself is only rewritten
          when the tree changed, see :meth:`save_as`).
        * On exception inside the ``with``-block, propagates the
          exception and does NOT call :meth:`save`. The file on disk
          stays byte-identical to its pre-call state.

        Use this whenever a caller plans more than one mutation against
        the same RDL — a 5-edit sequence inside one ``batch`` does one
        parse + one serialize + one atomic rename, instead of 5.
        """
        doc = cls.open(path)
        try:
            yield doc
        except Exception:
            # Re-raise without saving. Disk file untouched.
            raise
        else:
            doc.save()

    def save_as(self, path: PathLike) -> bool:
        """Atomically write the document to ``path``; ``True`` when written.

        Saving back to the file the document was read from (or last
        written to) is skipped — returning ``False`` — when that file is
        untouched since (same stat signature) and the document would
        serialise to the same bytes, or to a tree C14N-equal to the one
        on disk (#61). Saving to any other path always writes.
        """
        # v0.4 transaction interception: while this document is inside
        # an active editing transaction, saving it back to its own path
        # silently no-ops. commit_editing_transaction clears the flag and
        # re-calls save_as so the actual on-disk write happens exactly
        # once, at commit time. Saving to any OTHER path writes normally.
        # A document that isn't the holding transaction's tree may not be
        # saved over a held path at all (TransactionConflictError).
        from pbirb_mcp.core import transactions as _tx

        path = Path(path)
        target = str(path.resolve())
        if getattr(self, "_in_transaction", False) and target == str(self.path.resolve()):
            return False
        _tx.check_writable(target, self)
        data = self.to_bytes()
        if self._unchanged_on_disk(target, data):
            self.path = path
            return False
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except Exception:
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()
            raise
        self.path = path
        self._on_disk = _DiskState(target, data, _stat_signature(path))
        if self._known is not None:
            self._remember_elements()
        return True

    def _unchanged_on_disk(self, target: str, data: bytes) -> bool:
        """Would writing ``data`` to ``target`` leave the file as it is?"""
        state = self._on_disk
        if state is None or state.target != target or state.stat is None:
            return False
        if _stat_signature(Path(target)) != state.stat:
            return False  # touched since we read/wrote it — write ours
        if data == state.data:
            return True
        # Final guard: the bytes differ only in form (a source the
        # byte-faithful writer doesn't reproduce exactly) but the tree is
        # canonically the one on disk — no edit happened.
        try:
            parser = etree.XMLParser(
                remove_blank_text=False,
                remove_comments=False,
                strip_cdata=False,
                resolve_entities=False,
            )
            before = etree.parse(io.BytesIO(state.data), parser)
        except etree.XMLSyntaxError:
            return False
        after = _canonical(self.tree)
        return after is not None and after == _canonical(before)

    def to_bytes(self) -> bytes:
        """Serialise the tree in the source file's byte form."""
        self._indent_new_elements()
        form = self.source_form
        # Serialize body without the XML declaration so we can write the
        # declaration ourselves with double quotes — Report Builder always
        # uses <?xml version="1.0" encoding="utf-8"?> and tooling that
        # diffs by string sees single quotes (lxml's default) as drift.
        # lxml writes `<X/>` for text None and `<X></X>` for text "" —
        # flip the source's expanded-empty elements for the duration.
        flipped = [e for e in self._expanded if e.text is None and len(e) == 0]
        for e in flipped:
            e.text = ""
        try:
            body = etree.tostring(self.tree, xml_declaration=False, encoding=self.encoding)
        finally:
            for e in flipped:
                e.text = None
        if self.encoding.lower() not in _ASCII_COMPATIBLE:
            declaration = f'<?xml version="1.0" encoding="{self.encoding}"?>\n'.encode(
                self.encoding
            )
            return declaration + body + (b"" if body.endswith(b"\n") else b"\n")
        if form.spaced_self_close:
            body = _SELF_CLOSING_NORMALISE.sub(b" />", body)
        body = body.rstrip(b" \t\r\n")
        if form.newline != b"\n":
            # The parser normalised every literal CR in text to LF and lxml
            # escapes a real CR as `&#13;`, so every `\n` here is a line end.
            body = body.replace(b"\n", form.newline)
        if form.root_start_tag is not None and form.root_signature == _root_signature(self.root):
            m = _find_root_start_tag(body)
            if m is not None:
                body = body[: m.start()] + form.root_start_tag + body[m.end() :]
        if form.declaration is not None:
            prolog = form.declaration + form.after_declaration
        elif not form.from_source:
            # Built in memory (no source) — Report Builder's declaration.
            prolog = f'<?xml version="1.0" encoding="{self.encoding}"?>'.encode() + form.newline
        else:
            prolog = b""
        return (_BOM if form.has_bom else b"") + prolog + body + form.trailing


__all__ = [
    "RDLDocument",
    "RDLValidationError",
    "RDL_NS",
    "RD_NS",
]
