"""Walk every RDL expression and resolve its dataset scope (#46).

Lint rules used to look for expressions in a hard-coded list of element
names (``Value``, ``FilterExpression``, ``Label`` …) and so never saw
chart ``<Y>``/``<X>``, ``Style/Color``, ``Format``, ``FilterValue``,
``Hyperlink``, ``Bookmark`` and dozens of other expression-capable
properties. This module replaces that list with the RDL rule itself:
*any* element whose text starts with ``=`` is an expression.

Public API::

    iter_expressions(root) -> Iterator[(element, text)]
    expression_tokens(text) -> list[Token] | None      # significant tokens
    scope_region(elem) -> element | None               # data region / DataSet in scope
    dataset_scope(elem) -> str | None                  # its dataset name
    references(tokens, collection) -> list[Ref]        # Fields!X / Fields("X") …
    aggregate_calls(tokens) -> list[Call]              # Sum(…, "Scope"), Lookup(…) …
    enclosing_calls(calls, index) -> list[Call]        # innermost first
    ScopeIndex(root)                                   # datasets / regions / groups
    find_field_references(root, dataset, field)        # consumers of one field (#72)
    locator(elem) -> str                               # Type[Name='X']/Sub/Path label
    parameter_references(tokens, name=None)            # Parameters!X / ("X") / Report.Parameters…
    iter_parameter_references(root, name=None)         # (elem, text, tokens, refs), incl. <Code>
    rename_parameter_references(text, tokens, refs, new)

All token indices refer to the *significant* token list (whitespace and
comments dropped) — what :func:`expression_tokens` returns and what
:func:`pbirb_mcp.ops.vbexpr.significant` produces.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import NamedTuple, Optional

from lxml import etree

from pbirb_mcp.ops import vbexpr

# Elements whose text is never a VB expression even when it starts with
# ``=``: query text and the report's embedded code block.
EXCLUDED_TAGS = frozenset({"CommandText", "Code"})


def _local(elem: etree._Element) -> str:
    return etree.QName(elem).localname


def _children(elem: etree._Element, ns: Optional[str], local: str) -> list:
    tag = f"{{{ns}}}{local}" if ns else local
    return [c for c in elem if c.tag == tag]


def iter_expressions(root: etree._Element) -> Iterator[tuple[etree._Element, str]]:
    """Yield ``(element, text)`` for every element whose stripped text
    starts with ``=``, in document order.

    Skips ``<CommandText>``, ``<Code>`` and every element outside the
    report's own namespace (``rd:*`` designer state, ``am:``/``df:``
    metadata …) together with everything beneath it.
    """
    report_ns = etree.QName(root).namespace

    def walk(elem: etree._Element) -> Iterator[tuple[etree._Element, str]]:
        for child in elem:
            if not isinstance(child.tag, str):
                continue
            qn = etree.QName(child)
            if qn.namespace != report_ns or qn.localname in EXCLUDED_TAGS:
                continue
            text = child.text
            if text and text.lstrip().startswith("="):
                yield child, text
            if len(child):
                yield from walk(child)

    text = root.text
    if (
        isinstance(root.tag, str)
        and etree.QName(root).localname not in EXCLUDED_TAGS
        and text
        and text.lstrip().startswith("=")
    ):
        yield root, text
    yield from walk(root)


def expression_tokens(text: Optional[str]) -> Optional[list[vbexpr.Token]]:
    """Significant VB tokens of an ``=``-expression; ``None`` when ``text``
    isn't an expression."""
    tokens = vbexpr.tokenize_expression(text)
    if tokens is None:
        return None
    return vbexpr.significant(tokens)


# ---- dataset scope -----------------------------------------------------------


def _dataset_name_child(elem: etree._Element) -> Optional[str]:
    ns = etree.QName(elem).namespace
    for c in _children(elem, ns, "DataSetName"):
        if c.text and c.text.strip():
            return c.text.strip()
    return None


def _map_data_region(elem: etree._Element) -> Optional[etree._Element]:
    """For a map layer (``<MapDataRegionName>X``), the ``<MapDataRegion
    Name="X">`` of the same map."""
    ns = etree.QName(elem).namespace
    names = _children(elem, ns, "MapDataRegionName")
    if not names or not (names[0].text or "").strip():
        return None
    wanted = names[0].text.strip()
    cur = elem.getparent()
    while cur is not None and _local(cur) != "Map":
        cur = cur.getparent()
    if cur is None:
        return None
    for region in cur.iter(f"{{{ns}}}MapDataRegion"):
        if region.get("Name") == wanted:
            return region
    return None


def scope_region(elem: etree._Element) -> Optional[etree._Element]:
    """The element that gives ``elem`` its dataset scope, or ``None``.

    Climbs from ``elem`` to the nearest ancestor that either has a
    ``<DataSetName>`` child (Tablix, Chart, GaugePanel, MapDataRegion,
    CustomReportItem) or *is* a ``<DataSet>`` (calculated fields and
    dataset filters are evaluated against their own dataset). A map
    layer resolves through its ``<MapDataRegionName>``. Expressions in
    the page header/footer, in a body item outside any data region or
    in report parameters have no dataset scope.
    """
    cur = elem.getparent()
    while cur is not None:
        if isinstance(cur.tag, str):
            local = _local(cur)
            if local == "DataSet" and cur.get("Name"):
                return cur
            if local == "DataSetReference":
                # A parameter's ValidValues/DefaultValue query reference —
                # its children are field names, not scoped expressions.
                return None
            if _dataset_name_child(cur) is not None:
                return cur
            region = _map_data_region(cur)
            if region is not None:
                return region
        cur = cur.getparent()
    return None


def dataset_scope(elem: etree._Element) -> Optional[str]:
    """Name of the dataset ``elem``'s expressions are evaluated against
    (see :func:`scope_region`), or ``None`` when it has no scope."""
    region = scope_region(elem)
    if region is None:
        return None
    if _local(region) == "DataSet":
        return region.get("Name")
    return _dataset_name_child(region)


# ---- collection references -------------------------------------------------


class Ref(NamedTuple):
    """A ``Collection!Name[.Member]`` or ``Collection("Name")[.Member]``
    reference. ``first``/``last`` index its first/last significant token."""

    collection: str
    name: str
    member: Optional[str]
    start: int
    end: int
    first: int
    last: int


def _after_member_access(tokens: Sequence[vbexpr.Token], i: int) -> bool:
    return i > 0 and tokens[i - 1].kind == vbexpr.OPERATOR and tokens[i - 1].text in (".", "!")


def references(tokens: Sequence[vbexpr.Token], collection: str) -> list[Ref]:
    """``Fields!X.Value``-style and ``Fields("X").Value``-style references
    to ``collection`` (matched case-insensitively), in token order.
    ``tokens`` must be significant tokens (see :func:`expression_tokens`).
    """
    out = [
        Ref(r.collection, r.name, r.member, r.start, r.end, r.first, r.last)
        for r in vbexpr.bang_references(tokens, collection)
    ]
    want = collection.lower()
    n = len(tokens)
    for i in range(n - 3):
        a, op, s, cl = tokens[i], tokens[i + 1], tokens[i + 2], tokens[i + 3]
        if not (
            a.kind == vbexpr.IDENTIFIER
            and a.text.lower() == want
            and op.kind == vbexpr.DELIMITER
            and op.text == "("
            and s.kind == vbexpr.STRING
            and cl.kind == vbexpr.DELIMITER
            and cl.text == ")"
            and not _after_member_access(tokens, i)
        ):
            continue
        member: Optional[str] = None
        last = i + 3
        if i + 5 < n:
            dot, mem = tokens[i + 4], tokens[i + 5]
            if dot.kind == vbexpr.OPERATOR and dot.text == "." and mem.kind == vbexpr.IDENTIFIER:
                member = mem.text
                last = i + 5
        out.append(Ref(a.text, vbexpr.string_value(s), member, a.start, tokens[last].end, i, last))
    out.sort(key=lambda r: r.first)
    return out


# ---- aggregate / scope-taking calls ----------------------------------------

# Function (lower-cased) -> index of its scope argument. The expression
# argument (index 0, when there is one) is evaluated in that scope.
_AGGREGATE_SCOPE_ARG = {
    "sum": 1,
    "avg": 1,
    "count": 1,
    "countdistinct": 1,
    "first": 1,
    "last": 1,
    "max": 1,
    "min": 1,
    "stdev": 1,
    "stdevp": 1,
    "var": 1,
    "varp": 1,
    "aggregate": 1,
    "union": 1,
    "previous": 1,
    "runningvalue": 2,
    "countrows": 0,
    "rownumber": 0,
}
# Scope-name functions that aggregate nothing.
_SCOPE_ONLY = {"inscope": 0, "level": 0}
# Lookup(source, destination, result, "DataSet"): destination and result
# are evaluated in the named dataset; source in the current scope.
_LOOKUP_DATASET_ARG = {"lookup": 3, "lookupset": 3, "multilookup": 3}

AGGREGATE = "aggregate"
SCOPE_ONLY = "scope"
LOOKUP = "lookup"


class Call(NamedTuple):
    """A call to an aggregate (``Sum``…), a lookup (``Lookup``…) or a
    scope function (``InScope``/``Level``).

    ``args`` are half-open ``(start, stop)`` significant-token ranges,
    one per argument; ``scope`` is the decoded string literal in the
    scope/dataset argument (``None`` when omitted or not a plain string,
    e.g. ``Nothing``); ``evaluated`` are the argument ranges evaluated in
    that scope (the aggregated expression; a lookup's destination and
    result).
    """

    function: str
    kind: str
    name_index: int
    open_index: int
    close_index: int
    args: tuple
    scope: Optional[str]
    scope_token: Optional[vbexpr.Token]
    evaluated: tuple


def _split_args(tokens: Sequence[vbexpr.Token], open_index: int) -> tuple[tuple, int]:
    """Arguments of the call whose ``(`` is at ``open_index``; returns
    ``(args, close_index)`` (close = len(tokens) when unclosed)."""
    depth = 0
    args = []
    start = open_index + 1
    i = open_index
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t.kind == vbexpr.DELIMITER:
            if t.text in "([{":
                depth += 1
            elif t.text in ")]}":
                depth -= 1
                if depth == 0:
                    if i > start or args:
                        args.append((start, i))
                    return tuple(args), i
            elif t.text == "," and depth == 1:
                args.append((start, i))
                start = i + 1
        i += 1
    if n > start or args:
        args.append((start, n))
    return tuple(args), n


def aggregate_calls(tokens: Sequence[vbexpr.Token]) -> list[Call]:
    """Every aggregate / lookup / scope-function call in ``tokens``
    (significant tokens), outermost first."""
    out: list[Call] = []
    n = len(tokens)
    for i in range(n - 1):
        t = tokens[i]
        if t.kind != vbexpr.IDENTIFIER or _after_member_access(tokens, i):
            continue
        nxt = tokens[i + 1]
        if nxt.kind != vbexpr.DELIMITER or nxt.text != "(":
            continue
        fn = t.text.lower()
        if fn in _AGGREGATE_SCOPE_ARG:
            kind, scope_arg = AGGREGATE, _AGGREGATE_SCOPE_ARG[fn]
        elif fn in _SCOPE_ONLY:
            kind, scope_arg = SCOPE_ONLY, _SCOPE_ONLY[fn]
        elif fn in _LOOKUP_DATASET_ARG:
            kind, scope_arg = LOOKUP, _LOOKUP_DATASET_ARG[fn]
        else:
            continue
        args, close = _split_args(tokens, i + 1)
        scope: Optional[str] = None
        scope_token: Optional[vbexpr.Token] = None
        if scope_arg < len(args):
            a, b = args[scope_arg]
            if b - a == 1 and tokens[a].kind == vbexpr.STRING:
                scope_token = tokens[a]
                scope = vbexpr.string_value(scope_token)
        if kind == LOOKUP:
            evaluated = tuple(args[1:3])
        elif kind == AGGREGATE and scope_arg > 0 and args:
            evaluated = (args[0],)
        else:
            evaluated = ()
        out.append(Call(t.text, kind, i, i + 1, close, args, scope, scope_token, evaluated))
    return out


def enclosing_calls(calls: Sequence[Call], index: int) -> list[Call]:
    """The calls whose evaluated arguments contain token ``index``,
    innermost first."""
    hits = [c for c in calls if any(a <= index < b for a, b in c.evaluated)]
    hits.sort(key=lambda c: c.open_index, reverse=True)
    return hits


# ---- report-wide scope names -----------------------------------------------


class ScopeIndex:
    """Datasets (with their field names), data regions and groups of a
    report, for resolving scope names.

    ``resolve(name)`` maps a scope name — a dataset, a data region or a
    group — to the dataset it reads, or ``None`` when no such scope
    exists. Names are case-sensitive.
    """

    def __init__(self, root: etree._Element) -> None:
        ns = etree.QName(root).namespace
        self.datasets: dict[str, frozenset] = {}
        for block in _children(root, ns, "DataSets"):
            for ds in _children(block, ns, "DataSet"):
                name = ds.get("Name")
                if not name:
                    continue
                fields: set[str] = set()
                for fblock in _children(ds, ns, "Fields"):
                    for f in _children(fblock, ns, "Field"):
                        if f.get("Name"):
                            fields.add(f.get("Name"))
                self.datasets.setdefault(name, frozenset(fields))
        self.all_fields: frozenset = frozenset().union(*self.datasets.values())
        self.regions: dict[str, Optional[str]] = {}
        self.groups: dict[str, Optional[str]] = {}
        for el in root.iter(f"{{{ns}}}DataSetName"):
            region = el.getparent()
            if region is not None and region.get("Name") and _local(region) != "DataSetReference":
                self.regions.setdefault(region.get("Name"), _dataset_name_child(region))
        for g in root.iter(f"{{{ns}}}Group"):
            if g.get("Name"):
                self.groups.setdefault(g.get("Name"), dataset_scope(g))

    def known(self, name: str) -> bool:
        return name in self.datasets or name in self.regions or name in self.groups

    def resolve(self, name: str) -> Optional[str]:
        if name in self.datasets:
            return name
        if name in self.regions:
            return self.regions[name]
        if name in self.groups:
            return self.groups[name]
        return None


# ---- field consumers (#72) -------------------------------------------------


def locator(elem: etree._Element, max_named: int = 3) -> str:
    """Human-readable locator for ``elem``: its nearest ``max_named`` named
    ancestors (``Type[Name='X']``, outermost first, joined by ``>``), then
    the unnamed path below the nearest one (joined by ``/``) — e.g.
    ``Tablix[Name='T']>Textbox[Name='Amount']/Paragraphs/Paragraph/TextRuns/TextRun/Value``
    or ``ReportParameter[Name='P']/ValidValues/DataSetReference/ValueField``.
    Walks to the root, so a named ancestor is never missed.
    """
    path: list[str] = []
    named: list[str] = []
    cur: Optional[etree._Element] = elem
    while cur is not None and len(named) < max_named:
        if isinstance(cur.tag, str):
            local = _local(cur)
            if cur.get("Name"):
                named.append(f"{local}[Name={cur.get('Name')!r}]")
            elif not named:
                path.append(local)
        cur = cur.getparent()
    head = ">".join(reversed(named))
    tail = "/".join(reversed(path))
    if head and tail:
        return f"{head}/{tail}"
    return head or tail or "(unnamed)"


class FieldReference(NamedTuple):
    """One consumer of a dataset field, found by :func:`find_field_references`.

    ``element`` holds the reference; ``locator`` labels it (see
    :func:`locator`); ``kind`` is ``"expression"`` (``Fields!X…`` /
    ``Fields("X")`` in an ``=``-expression) or ``"dataset_reference"``
    (a parameter's ``DataSetReference/ValueField|LabelField``); ``text``
    is the matched source text (the reference, or the bare field name);
    ``scope`` is the dataset the reference was resolved to, ``None`` when
    it could not be resolved statically (counted as a reference anyway).
    """

    element: etree._Element
    locator: str
    kind: str
    text: str
    scope: Optional[str]


def _resolve_ref_dataset(
    ref: Ref,
    calls: Sequence[Call],
    region_ds: Optional[str],
    index: ScopeIndex,
) -> tuple[bool, Optional[str]]:
    """``(resolved, dataset)`` for one ``Fields`` reference: the innermost
    enclosing call with a literal scope wins (a group / data region name
    resolves to the dataset it reads); a ``Lookup`` with a non-literal
    dataset, or an unknown scope name, is unresolved; otherwise the data
    region's / DataSet's dataset; outside any region, the report's only
    dataset (unresolved when there are several)."""
    for call in enclosing_calls(calls, ref.first):  # innermost first
        if call.scope is not None:
            ds = index.resolve(call.scope)
            return (ds is not None), ds
        if call.kind == LOOKUP:
            return False, None
    if region_ds is not None:
        return True, region_ds
    if len(index.datasets) == 1:
        return True, next(iter(index.datasets))
    return False, None


def find_field_references(
    root: etree._Element, dataset_name: str, field_name: str
) -> list[FieldReference]:
    """Every consumer of field ``field_name`` of dataset ``dataset_name``, in
    document order (#72). Public so other guards can reuse it.

    1. ``Fields!N.<member>`` and ``Fields("N")[.<member>]`` in any
       ``=``-expression (:func:`iter_expressions`, VB tokens — names in
       string literals or comments don't count), kept when the reference's
       dataset scope is ``dataset_name``: an aggregate's / lookup's literal
       scope argument, else the nearest data region's ``DataSetName``
       (Tablix, Chart, GaugePanel, map data region, CustomReportItem), else
       the enclosing ``<DataSet>`` (calculated fields, dataset filters),
       else the report's only dataset. A reference whose scope can't be
       resolved statically is kept too — a guard should err on the side
       of refusing.
    2. ``DataSetReference`` elements (a parameter's ValidValues /
       DefaultValue query) with ``DataSetName == dataset_name`` whose
       ``ValueField`` or ``LabelField`` is ``field_name`` — bare names, not
       expressions.

    The field's own ``<Field>`` definition is not a consumer and is skipped.
    """
    ns = etree.QName(root).namespace
    index = ScopeIndex(root)

    def own_definition(elem: etree._Element) -> bool:
        # <DataSet Name=dataset_name>/<Fields>/<Field Name=field_name>/<Value>
        field = elem.getparent()
        fields = field.getparent() if field is not None else None
        ds = fields.getparent() if fields is not None else None
        return (
            ds is not None
            and _local(field) == "Field"
            and field.get("Name") == field_name
            and _local(ds) == "DataSet"
            and ds.get("Name") == dataset_name
        )

    hits: list[tuple[etree._Element, FieldReference]] = []
    for elem, text in iter_expressions(root):
        if field_name not in text:
            continue
        sig = expression_tokens(text)
        if not sig:
            continue
        refs = [r for r in references(sig, "Fields") if r.name == field_name]
        if not refs or own_definition(elem):
            continue
        calls = aggregate_calls(sig)
        region_ds = dataset_scope(elem)
        for ref in refs:
            resolved, ds = _resolve_ref_dataset(ref, calls, region_ds, index)
            if resolved and ds != dataset_name:
                continue
            hits.append(
                (
                    elem,
                    FieldReference(
                        elem, locator(elem), "expression", text[ref.start : ref.end], ds
                    ),
                )
            )

    for dsref in root.iter(f"{{{ns}}}DataSetReference"):
        names = _children(dsref, ns, "DataSetName")
        if not names or (names[0].text or "").strip() != dataset_name:
            continue
        for slot in ("ValueField", "LabelField"):
            for el in _children(dsref, ns, slot):
                if (el.text or "").strip() == field_name:
                    hits.append(
                        (
                            el,
                            FieldReference(
                                el, locator(el), "dataset_reference", field_name, dataset_name
                            ),
                        )
                    )

    order = {el: i for i, el in enumerate(root.iter())}
    hits.sort(key=lambda h: order.get(h[0], 0))
    return [h[1] for h in hits]


# ---- report-parameter references (#73) and dependencies (#74) ---------------


def _is_report_qualifier(tokens: Sequence[vbexpr.Token], i: int) -> bool:
    """``tokens[i-2:i]`` is ``Report .`` (custom code's ``Report.Parameters``)."""
    return (
        i >= 2
        and tokens[i - 1].kind == vbexpr.OPERATOR
        and tokens[i - 1].text == "."
        and tokens[i - 2].kind == vbexpr.IDENTIFIER
        and tokens[i - 2].text.lower() == "report"
        and not _after_member_access(tokens, i - 2)
    )


def parameter_references(
    tokens: Sequence[vbexpr.Token], name: Optional[str] = None, *, qualified: bool = True
) -> list[Ref]:
    """Every report-parameter reference in ``tokens`` (significant tokens):
    ``Parameters!X`` / ``Parameters("X")`` with any member (``.Value``,
    ``.Label``, ``.Count``, ``.IsMultiValue`` …) or none. With
    ``qualified`` (default) also ``Report.Parameters!X`` /
    ``Report.Parameters("X")``, the form custom code uses. ``name``
    filters on the parameter name (case-sensitive, like RDL names).

    In every form the parameter-name token is ``tokens[ref.first + 2]``:
    an identifier for ``!X``, a string literal for ``("X")``.
    """
    drop: set = set()
    if qualified:
        for i in range(2, len(tokens)):
            t = tokens[i]
            if (
                t.kind == vbexpr.IDENTIFIER
                and t.text.lower() == "parameters"
                and _is_report_qualifier(tokens, i)
            ):
                drop.update((i - 2, i - 1))
    keep = [i for i in range(len(tokens)) if i not in drop]
    view = [tokens[i] for i in keep]
    out = []
    for r in references(view, "Parameters"):
        if name is not None and r.name != name:
            continue
        out.append(r._replace(first=keep[r.first], last=keep[r.last]))
    return out


def rename_parameter_references(
    text: str, tokens: Sequence[vbexpr.Token], refs: Sequence[Ref], new_name: str
) -> str:
    """``text`` with the parameter name of each of ``refs`` (from
    :func:`parameter_references` over ``tokens``, whose offsets index
    ``text``) replaced by ``new_name``; nothing else changes."""
    pieces = []
    pos = 0
    for ref in sorted(refs, key=lambda r: r.first):
        tok = tokens[ref.first + 2]
        if tok.kind == vbexpr.STRING:
            replacement = '"' + new_name.replace('"', '""') + '"'
        else:
            replacement = new_name
        pieces.append(text[pos : tok.start])
        pieces.append(replacement)
        pos = tok.end
    pieces.append(text[pos:])
    return "".join(pieces)


def iter_parameter_references(
    root: etree._Element, name: Optional[str] = None
) -> Iterator[tuple[etree._Element, str, list, list]]:
    """``(element, text, tokens, refs)`` for every element holding a
    report-parameter reference (see :func:`parameter_references`): every
    expression (:func:`iter_expressions`) plus the report's ``<Code>``
    block (``Report.Parameters!X``). ``tokens`` are the significant tokens
    whose offsets index ``text``; ``refs`` is never empty."""
    for elem, text in iter_expressions(root):
        if "parameters" not in text.lower():
            continue
        sig = expression_tokens(text)
        if not sig:
            continue
        refs = parameter_references(sig, name)
        if refs:
            yield elem, text, sig, refs
    ns = etree.QName(root).namespace
    for code in root.iter(f"{{{ns}}}Code" if ns else "Code"):
        text = code.text
        if not text or "parameters" not in text.lower():
            continue
        sig = vbexpr.significant(vbexpr.tokenize(text))
        refs = [r for r in parameter_references(sig, name) if _is_report_qualifier(sig, r.first)]
        if refs:
            yield code, text, sig, refs


__all__ = [
    "AGGREGATE",
    "Call",
    "EXCLUDED_TAGS",
    "FieldReference",
    "LOOKUP",
    "Ref",
    "SCOPE_ONLY",
    "ScopeIndex",
    "aggregate_calls",
    "dataset_scope",
    "enclosing_calls",
    "expression_tokens",
    "find_field_references",
    "iter_expressions",
    "locator",
    "references",
    "scope_region",
    # report-parameter references (#73)
    "iter_parameter_references",
    "parameter_references",
    "rename_parameter_references",
]
