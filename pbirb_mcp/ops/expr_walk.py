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
    iter_field_expression_references(root, dataset, field)  # … with tokens, to rewrite (#98)
    field_dataset_reference_slots(root, dataset, field)     # DataSetReference Value/LabelField
    dataset_scope_references(root, dataset)            # Sum(…, "DS") / Lookup(…, "DS") (#98)
    locator(elem) -> str                               # Type[Name='X']/Sub/Path label
    parameter_references(tokens, name=None)            # Parameters!X / ("X") / Report.Parameters…
    iter_parameter_references(root, name=None)         # (elem, text, tokens, refs), incl. <Code>
    rename_parameter_references(text, tokens, refs, new)
    parameter_dependency_edges(root) / parameter_dependencies(root)
    forward_dependencies(order, edges)                 # dependants declared before a dependency
    find_inbound_references(root, names)               # ReportItems!X, ToggleItem, scopes … (#105)
    rewrite_references(refs, mapping)                  # rename what those point at (#111)

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
    hits: list[tuple[etree._Element, FieldReference]] = []
    for elem, text, _sig, refs in iter_field_expression_references(root, dataset_name, field_name):
        for ref, _resolved, ds in refs:
            hits.append(
                (
                    elem,
                    FieldReference(
                        elem, locator(elem), "expression", text[ref.start : ref.end], ds
                    ),
                )
            )

    for el in field_dataset_reference_slots(root, dataset_name, field_name):
        hits.append(
            (el, FieldReference(el, locator(el), "dataset_reference", field_name, dataset_name))
        )

    order = {el: i for i, el in enumerate(root.iter())}
    hits.sort(key=lambda h: order.get(h[0], 0))
    return [h[1] for h in hits]


def iter_field_expression_references(
    root: etree._Element, dataset_name: str, field_name: str
) -> Iterator[tuple[etree._Element, str, list, list]]:
    """``(element, text, tokens, [(ref, resolved, dataset)])`` for every
    expression holding a ``Fields`` reference to ``field_name`` whose scope
    is ``dataset_name`` or can't be resolved statically (``resolved`` is
    then False, ``dataset`` None) — the expression half of
    :func:`find_field_references`, with the tokens (offsets index
    ``text``) so a caller can rewrite the name token
    (``tokens[ref.first + 2]``, as :func:`rename_parameter_references`
    does). The field's own ``<Field>/<Value>`` is skipped."""

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

    index = ScopeIndex(root)
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
        kept = []
        for ref in refs:
            resolved, ds = _resolve_ref_dataset(ref, calls, region_ds, index)
            if resolved and ds != dataset_name:
                continue
            kept.append((ref, resolved, ds))
        if kept:
            yield elem, text, sig, kept


def field_dataset_reference_slots(
    root: etree._Element, dataset_name: str, field_name: str
) -> list[etree._Element]:
    """The ``DataSetReference/ValueField|LabelField`` elements (a
    parameter's ValidValues / DefaultValue query) of dataset
    ``dataset_name`` that name ``field_name``."""
    ns = etree.QName(root).namespace
    out: list[etree._Element] = []
    for dsref in root.iter(f"{{{ns}}}DataSetReference"):
        names = _children(dsref, ns, "DataSetName")
        if not names or (names[0].text or "").strip() != dataset_name:
            continue
        for slot in ("ValueField", "LabelField"):
            for el in _children(dsref, ns, slot):
                if (el.text or "").strip() == field_name:
                    out.append(el)
    return out


def dataset_scope_references(
    root: etree._Element, dataset_name: str
) -> list[tuple[etree._Element, str, list, list]]:
    """``(element, text, tokens, calls)`` for every expression whose
    aggregate / lookup / scope-function calls name ``dataset_name`` in a
    literal scope or dataset argument (``Sum(…, "DS")``,
    ``Lookup(…, "DS")``, ``CountRows("DS")``); ``calls`` are those
    :class:`Call` s (their ``scope_token`` indexes ``text``)."""
    out = []
    for elem, text in iter_expressions(root):
        if dataset_name not in text:
            continue
        sig = expression_tokens(text)
        if not sig:
            continue
        calls = [c for c in aggregate_calls(sig) if c.scope == dataset_name]
        if calls:
            out.append((elem, text, sig, calls))
    return out


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


def parameter_dependency_edges(root: etree._Element) -> dict[str, list[tuple[str, str]]]:
    """For each ``<ReportParameter>`` (declaration order), the parameters
    it depends on as ``(dependency, via)`` pairs, in document order (may
    repeat). Sources: expressions in its ``<DefaultValue>`` /
    ``<ValidValues>``, and — for a ``<DataSetReference>`` there — every
    ``Parameters!`` reference in that dataset (its query parameters,
    filters, calculated fields). ``via`` says which (``"DefaultValue"``,
    ``"ValidValues dataset 'X'"`` …). Report Builder evaluates
    parameters in declaration order, so a dependency must be declared
    earlier."""
    ns = etree.QName(root).namespace

    def kids(elem: etree._Element, local: str) -> list:
        return _children(elem, ns, local)

    def names_in(elem: etree._Element) -> list[str]:
        return [r.name for _e, _t, _s, refs in iter_parameter_references(elem) for r in refs]

    datasets: dict[str, etree._Element] = {}
    for block in kids(root, "DataSets"):
        for ds in kids(block, "DataSet"):
            if ds.get("Name"):
                datasets.setdefault(ds.get("Name"), ds)
    out: dict[str, list[tuple[str, str]]] = {}
    for block in kids(root, "ReportParameters"):
        for param in kids(block, "ReportParameter"):
            pname = param.get("Name")
            if not pname:
                continue
            deps: list[tuple[str, str]] = []
            for tag in ("DefaultValue", "ValidValues"):
                for section in kids(param, tag):
                    deps.extend((dep, tag) for dep in names_in(section))
                    for dsref in section.iter(f"{{{ns}}}DataSetReference"):
                        ds_name = ""
                        for n in kids(dsref, "DataSetName"):
                            ds_name = (n.text or "").strip()
                        ds = datasets.get(ds_name)
                        if ds is not None:
                            via = f"{tag} dataset {ds_name!r}"
                            deps.extend((dep, via) for dep in names_in(ds))
            out.setdefault(pname, deps)
    return out


def parameter_dependencies(root: etree._Element) -> dict[str, set]:
    """``{parameter: {parameters it depends on}}`` (see
    :func:`parameter_dependency_edges`)."""
    return {p: {d for d, _via in edges} for p, edges in parameter_dependency_edges(root).items()}


def forward_dependencies(
    order: Sequence[str], edges: dict[str, list[tuple[str, str]]]
) -> list[tuple[str, str, str]]:
    """``(parameter, dependency, via)`` for each dependency declared at or
    after its dependant in ``order`` (``dependency == parameter`` is a
    self-dependency). Unknown names are ignored; each pair once."""
    pos = {n: i for i, n in enumerate(order)}
    out = []
    for name in order:
        seen: set = set()
        for dep, via in edges.get(name, ()):
            j = pos.get(dep)
            if j is None or j < pos[name] or dep in seen:
                continue
            seen.add(dep)
            out.append((name, dep, via))
    return out


# ---- inbound references to report items and groups (#105, #111) -------------

# Elements whose whole text is the name of a report item, data region or
# group (plain strings in the XSD, never expressions) -> reference kind.
ELEMENT_NAME_REFERENCES = {
    "ToggleItem": "toggle_item",  # a Textbox
    "RepeatWith": "repeat_with",  # a data region
    "SortExpressionScope": "sort_scope",  # UserSort: a data region or group
    "SortTarget": "sort_target",  # UserSort: a data region or group
    "DomainScope": "domain_scope",  # a group's domain scope
    "TransformationScope": "transformation_scope",  # a chart formula's group
}


class InboundReference(NamedTuple):
    """One reference to a report item or group, found by
    :func:`find_inbound_references`.

    ``element`` holds it; ``locator`` labels it (see :func:`locator`);
    ``kind`` is ``"report_items"`` (``ReportItems!X`` / ``ReportItems("X")``
    in an expression), ``"scope"`` (the literal scope argument of an
    aggregate, ``RowNumber``, ``RunningValue``, ``Previous``, ``InScope``,
    ``Level`` …) or one of :data:`ELEMENT_NAME_REFERENCES`' kinds; ``name``
    is the referenced name; ``text`` the matched source text; ``span`` the
    ``(start, end)`` offsets of the name token in ``element.text`` for an
    expression reference, ``None`` when the element's whole text is the name.
    """

    element: etree._Element
    locator: str
    kind: str
    name: str
    text: str
    span: Optional[tuple[int, int]]


def find_inbound_references(root: etree._Element, names) -> list[InboundReference]:
    """Every reference to the report items / groups called ``names`` (one
    name or an iterable of names; case-sensitive), in document order.

    1. ``ReportItems!N`` / ``ReportItems("N")`` in any ``=``-expression
       (:func:`iter_expressions`; VB tokens, so names in string literals or
       comments don't count).
    2. The literal scope argument of a scope-taking call (see
       :func:`aggregate_calls`; not ``Lookup``'s dataset argument) — unless
       a dataset has that name too, which the scope string then means.
    3. Elements whose whole text is the name (:data:`ELEMENT_NAME_REFERENCES`):
       ``<ToggleItem>``, ``<RepeatWith>``, ``UserSort``'s
       ``<SortExpressionScope>`` / ``<SortTarget>``, ``<DomainScope>``,
       ``<TransformationScope>``.

    Bookmarks are values rather than names (``BookmarkLink`` points at a
    ``<Bookmark>`` text) and ``DataElementName`` is output naming, so neither
    counts. Callers decide which hits matter (e.g. only those outside a
    subtree being removed).
    """
    wanted = {names} if isinstance(names, str) else set(names)
    if not wanted:
        return []
    ns = etree.QName(root).namespace
    datasets = {
        ds.get("Name")
        for block in _children(root, ns, "DataSets")
        for ds in _children(block, ns, "DataSet")
        if ds.get("Name")
    }
    hits: list[InboundReference] = []
    for elem, text in iter_expressions(root):
        if not any(n in text for n in wanted):
            continue
        sig = expression_tokens(text)
        if not sig:
            continue
        loc: Optional[str] = None
        for ref in references(sig, "ReportItems"):
            if ref.name not in wanted:
                continue
            tok = sig[ref.first + 2]
            loc = loc or locator(elem)
            hits.append(
                InboundReference(
                    elem,
                    loc,
                    "report_items",
                    ref.name,
                    text[ref.start : ref.end],
                    (tok.start, tok.end),
                )
            )
        for call in aggregate_calls(sig):
            if call.kind == LOOKUP or call.scope not in wanted or call.scope in datasets:
                continue
            tok = call.scope_token
            end = sig[call.close_index].end if call.close_index < len(sig) else len(text)
            loc = loc or locator(elem)
            hits.append(
                InboundReference(
                    elem,
                    loc,
                    "scope",
                    call.scope,
                    text[sig[call.name_index].start : end],
                    (tok.start, tok.end),
                )
            )
    for local, kind in ELEMENT_NAME_REFERENCES.items():
        for el in root.iter(f"{{{ns}}}{local}" if ns else local):
            value = (el.text or "").strip()
            if value in wanted:
                hits.append(InboundReference(el, locator(el), kind, value, value, None))
    order = {el: i for i, el in enumerate(root.iter())}
    hits.sort(key=lambda h: (order.get(h.element, 0), h.span or (0, 0)))
    return hits


def _name_token(original: str, new_name: str) -> str:
    """``new_name`` spelt like the token it replaces: a string literal stays
    quoted, a bracketed identifier stays bracketed."""
    if original.startswith('"'):
        return '"' + new_name.replace('"', '""') + '"'
    if original.startswith("["):
        return f"[{new_name}]"
    return new_name


def rewrite_references(
    refs: Sequence[InboundReference], mapping: dict
) -> list[tuple[etree._Element, str, str]]:
    """Point each of ``refs`` (from :func:`find_inbound_references`) at
    ``mapping[ref.name]`` instead, editing the elements in place; refs whose
    name isn't in ``mapping`` are left alone. Only the name changes — the
    rest of an expression, and the whitespace around a plain name, are kept.
    Returns ``(element, before, after)`` per element rewritten."""
    by_elem: dict[etree._Element, list[InboundReference]] = {}
    for ref in refs:
        if ref.name in mapping:
            by_elem.setdefault(ref.element, []).append(ref)
    out = []
    for elem, group in by_elem.items():
        before = elem.text or ""
        spans = sorted({r.span for r in group if r.span is not None})
        if spans:
            names_at = {r.span: r.name for r in group if r.span is not None}
            pieces, pos = [], 0
            for start, end in spans:
                pieces.append(before[pos:start])
                pieces.append(_name_token(before[start:end], mapping[names_at[(start, end)]]))
                pos = end
            pieces.append(before[pos:])
            after = "".join(pieces)
        else:
            name = group[0].name
            lead = before[: len(before) - len(before.lstrip())]
            trail = before[len(before.rstrip()) :]
            after = f"{lead}{mapping[name]}{trail}"
        if after != before:
            elem.text = after
            out.append((elem, before, after))
    return out


__all__ = [
    "AGGREGATE",
    "Call",
    "ELEMENT_NAME_REFERENCES",
    "EXCLUDED_TAGS",
    "FieldReference",
    "InboundReference",
    "find_inbound_references",
    "rewrite_references",
    "LOOKUP",
    "Ref",
    "SCOPE_ONLY",
    "ScopeIndex",
    "aggregate_calls",
    "dataset_scope",
    "enclosing_calls",
    "expression_tokens",
    "dataset_scope_references",
    "field_dataset_reference_slots",
    "find_field_references",
    "iter_expressions",
    "iter_field_expression_references",
    "locator",
    "references",
    "scope_region",
    # report-parameter references / dependencies (#73, #74)
    "forward_dependencies",
    "iter_parameter_references",
    "parameter_dependencies",
    "parameter_dependency_edges",
    "parameter_references",
    "rename_parameter_references",
]
