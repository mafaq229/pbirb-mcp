"""Page-chrome lint rules (#50): what may and must appear in
``<PageHeader>`` / ``<PageFooter>``.

Page globals (``PageNumber``, ``TotalPages``, ``OverallPageNumber``,
``OverallTotalPages``, ``PageName``) outside the page header/footer are
reported by the core ``page-number-out-of-chrome`` rule (#46).

* ``field-in-page-chrome`` (error) — a ``Fields!X`` reference in a page
  header/footer that isn't inside an aggregate call with an explicit
  dataset-scope string argument (``=First(Fields!X.Value, "DataSet1")``
  is fine; ``=Fields!X.Value`` is rejected by Report Builder, as is an
  aggregate whose scope names no dataset).
* ``page-section-missing-height`` (error) — a ``<PageHeader>`` /
  ``<PageFooter>`` with no ``<Height>``. RDL requires it; the bundled
  XSD's choice model lets it through.

Expressions are read with :mod:`pbirb_mcp.ops.vbexpr`, so references in
string literals and comments don't count. Every element whose text is an
``=`` expression is checked (style properties included), except
``<Code>`` and ``rd:`` designer elements.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RD_NS, find_child, find_children, q
from pbirb_mcp.ops import vbexpr
from pbirb_mcp.ops.lint import Issue, _ancestor_chain

_CHROME = ("PageHeader", "PageFooter")

# Page-level globals the core page-number-out-of-chrome rule doesn't
# cover (it handles PageNumber / TotalPages).

# Aggregate functions that take a scope argument. In a page header/footer
# the scope must be a dataset name given as a string literal.
_SCOPED_AGGREGATES = frozenset(
    name.lower()
    for name in (
        "Sum",
        "Avg",
        "Count",
        "CountDistinct",
        "CountRows",
        "Max",
        "Min",
        "First",
        "Last",
        "StDev",
        "StDevP",
        "Var",
        "VarP",
        "Aggregate",
        "RunningValue",
        "Union",
        "Merge",
    )
)

_SKIP_TAGS = frozenset({"Code"})


def _region(elem: etree._Element) -> Optional[str]:
    """``PageHeader`` / ``PageFooter`` when ``elem`` sits inside one."""
    cur = elem.getparent()
    while cur is not None:
        if isinstance(cur.tag, str):
            local = etree.QName(cur).localname
            if local in _CHROME:
                return local
        cur = cur.getparent()
    return None


def _expressions(root: etree._Element) -> Iterator[tuple[etree._Element, list[vbexpr.Token]]]:
    """Yield ``(element, significant tokens)`` for every ``=`` expression."""
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        qn = etree.QName(elem)
        if qn.namespace == RD_NS or qn.localname in _SKIP_TAGS:
            continue
        if not elem.text or "!" not in elem.text:
            continue
        tokens = vbexpr.tokenize_expression(elem.text)
        if tokens is None:
            continue
        yield elem, vbexpr.significant(tokens)


# ---- rule: field-in-page-chrome -------------------------------------------


class _Call(NamedTuple):
    name: str  # lower-cased function name
    open: int  # index of "(" in the significant token list
    close: int  # index of the matching ")" (len(sig) when unclosed)
    args: list[tuple[int, int]]  # [start, end) token ranges per argument


def _calls(sig: list[vbexpr.Token]) -> list[_Call]:
    """Every ``Name(...)`` call in ``sig`` with its argument ranges."""
    calls: list[_Call] = []
    stack: list[tuple[Optional[str], int, list[int]]] = []  # (name, open, commas)
    for i, tok in enumerate(sig):
        if tok.kind != vbexpr.DELIMITER:
            continue
        if tok.text in "([{":
            name = None
            if tok.text == "(" and i > 0 and sig[i - 1].kind == vbexpr.IDENTIFIER:
                name = sig[i - 1].text.lower()
            stack.append((name if tok.text == "(" else None, i, []))
        elif tok.text == "," and stack:
            stack[-1][2].append(i)
        elif tok.text in ")]}" and stack:
            name, start, commas = stack.pop()
            if name is not None:
                bounds = [start, *commas, i]
                args = [(bounds[k] + 1, bounds[k + 1]) for k in range(len(bounds) - 1)]
                calls.append(_Call(name, start, i, args))
    return calls


def _scope_strings(sig: list[vbexpr.Token], call: _Call) -> list[str]:
    """String-literal arguments after the first one (scope candidates)."""
    out = []
    for start, end in call.args[1:]:
        if end - start == 1 and sig[start].kind == vbexpr.STRING:
            out.append(vbexpr.string_value(sig[start]))
    return out


def _dataset_names(doc: RDLDocument) -> list[str]:
    block = find_child(doc.root, "DataSets")
    if block is None:
        return []
    return [ds.get("Name") for ds in find_children(block, "DataSet") if ds.get("Name")]


def _field_problem(
    sig: list[vbexpr.Token], index: int, calls: list[_Call], datasets: set[str]
) -> Optional[str]:
    """Why the ``Fields!`` reference at significant index ``index`` is not
    allowed in page chrome, or ``None`` when an enclosing aggregate scopes
    it to a dataset."""
    bad_scope: Optional[str] = None
    for call in calls:
        if call.name not in _SCOPED_AGGREGATES or not (call.open < index < call.close):
            continue
        for scope in _scope_strings(sig, call):
            if scope.lower() in datasets:
                return None
            bad_scope = scope
    if bad_scope is not None:
        return f"its aggregate's scope {bad_scope!r} is not a dataset name"
    return "it is not inside an aggregate with an explicit dataset scope"


def _rule_field_in_page_chrome(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    names = _dataset_names(doc)
    datasets = {n.lower() for n in names}
    example = names[0] if names else "DataSet1"
    for elem, sig in _expressions(doc.root):
        refs = vbexpr.bang_references(sig, "Fields")
        if not refs:
            continue
        region = _region(elem)
        if region is None:
            continue
        calls = _calls(sig)
        seen: set[str] = set()
        for ref in refs:
            problem = _field_problem(sig, ref.first, calls, datasets)
            if problem is None or ref.name in seen:
                continue
            seen.add(ref.name)
            issues.append(
                {
                    "severity": "error",
                    "rule": "field-in-page-chrome",
                    "location": _ancestor_chain(elem),
                    "message": (
                        f"Fields!{ref.name} is referenced in <{region}>, but {problem}; "
                        "Report Builder rejects field references in page headers/footers"
                    ),
                    "suggestion": (
                        "wrap it in an aggregate with a dataset scope, e.g. "
                        f'=First(Fields!{ref.name}.Value, "{example}"), or reference '
                        "a body textbox with ReportItems!Name.Value"
                    ),
                }
            )
    return issues


# ---- rule: page-section-missing-height ------------------------------------


def _rule_page_section_missing_height(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    for local in _CHROME:
        for section in doc.root.iter(q(local)):
            if find_child(section, "Height") is not None:
                continue
            issues.append(
                {
                    "severity": "error",
                    "rule": "page-section-missing-height",
                    "location": f"Page>{local}",
                    "message": f"<{local}> has no <Height>; RDL requires one",
                    "suggestion": (
                        "set it with set_page_header / set_page_footer (height='0.5in')"
                    ),
                }
            )
    return issues


RULES = {
    "field-in-page-chrome": _rule_field_in_page_chrome,
    "page-section-missing-height": _rule_page_section_missing_height,
}
