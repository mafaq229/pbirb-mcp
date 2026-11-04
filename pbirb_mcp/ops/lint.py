"""Static-analysis lint rules (Phase 7 commit 31; v0.3.1 added rule 16; #20 rules 17-18; #42 rule 19).

Core rules driven by the v0.2/v0.3 sweep feedback live here; further
rule families live in :mod:`pbirb_mcp.ops.lint_rules` (one module each,
auto-registered). Each rule is a
small pure function that takes the open :class:`RDLDocument` and
returns a list of issue dicts:

    {"severity": "error"|"warning",
     "rule": "<rule-name>",
     "location": "<XPath-ish locator>",
     "message": "<human-readable>",
     "suggestion": "<optional fix hint>"}

The shape mirrors :mod:`pbirb_mcp.ops.validate` so :func:`verify_report`
(commit 33) can union the two streams without reshaping.

Expression rules walk every expression in the report (any element whose
text starts with ``=``) via :func:`pbirb_mcp.ops.expr_walk.iter_expressions`
and tokenize it with :mod:`pbirb_mcp.ops.vbexpr` (#46).

Core rules (19):

* ``multi-value-eq`` (warn) — multi-value param compared with ``=``, or
  the FilterValue of an ``Equal``/``NotEqual`` ``<Filter>`` (#46).
* ``unused-data-source`` (warn) — DataSource never referenced.
* ``unused-data-set`` (warn) — DataSet never referenced.
* ``date-param-as-string`` (warn) — ``*date*``-named param typed as
  String.
* ``missing-field-reference`` (error) — ``Fields!X.<member>`` /
  ``Fields("X")`` for X not in any dataset's ``<Fields>`` (a field of
  another dataset than the one in scope is ``field-out-of-scope``, in
  :mod:`pbirb_mcp.ops.lint_rules.expressions`).
* ``page-number-out-of-chrome`` (error) — ``Globals!(Overall)PageNumber``
  / ``Globals!(Overall)TotalPages`` / ``Globals!PageName`` outside
  header/footer.
* ``expression-syntax`` (error) — expression with an unterminated
  string literal or mismatched parens/brackets/braces (tokenized via
  :mod:`pbirb_mcp.ops.vbexpr`, so literals and comments don't count).
* ``dangling-embedded-image`` (error) — ``Image`` or (#47)
  ``Style/BackgroundImage`` with ``Source=Embedded`` whose ``Value``
  doesn't name an ``<EmbeddedImage>``.
* ``dangling-data-source-reference`` (error) —
  ``<DataSet>/<Query>/<DataSourceName>`` doesn't match a real
  ``<DataSource>``.
* ``dangling-action`` (warn) — drillthrough action with empty
  ``<ReportName>``.
* ``pbidataset-at-prefix`` (error) — PBIDATASET dataset has a
  ``<QueryParameter Name="@…">`` (the bare ``Name`` is what works; the
  ``@`` belongs only in the DAX text).
* ``parameter-layout-out-of-sync`` (error) — ``<ReportParameters>`` /
  ``<CellDefinitions>`` count mismatch.
* ``double-encoded-entities`` (error) — element text contains
  ``&amp;``/``&lt;``/``&gt;``/``&quot;`` (the post-decode signature of a
  double-encoding regression). Skips ``<Code>``, ``<CommandText>``,
  ``<ImageData>``, ``rd:*`` and HTML-markup text runs.
* ``stale-designer-state`` (warn) — ``<rd:DesignerState>/<Statement>``
  diverges from ``<CommandText>``.
* ``tablix-span-misplaced`` (error) — ``<ColSpan>``/``<RowSpan>``
  outside ``<CellContents>``.
* ``dataset-fields-out-of-sync`` (warn, v0.3.1; both directions #51) —
  compares each data-bound ``<Field>``'s ``<DataField>`` with the columns
  the dataset's DAX ``<CommandText>`` returns
  (:func:`pbirb_mcp.ops.dax.result_columns`, matched with
  :func:`pbirb_mcp.ops.dax.data_field_matches`). Issue ``kind`` is
  ``field-not-returned`` (a DataField the DAX doesn't return) or
  ``column-not-bound`` (a returned column no Field binds).
* ``duplicate-name`` (error, #20) — two report items / groups share a
  ``Name`` (one report-wide namespace across body, header, footer and
  every nested container), or two data sources / datasets / parameters /
  embedded images / fields of one dataset do.
* ``invalid-identifier`` (error, #20) — a report item, group, data
  source, dataset, parameter, embedded image or field ``Name`` is not a
  CLS-compliant identifier (:func:`pbirb_mcp.core.names.validate_identifier`).
* ``dangling-toggle-item`` (error, #42) — a ``<Visibility>/<ToggleItem>``
  that doesn't name a Textbox, names the item itself or something inside
  it, sits in another page region, or is a tablix cell toggling an item
  outside that tablix (:func:`pbirb_mcp.ops.visibility.toggle_item_problem`).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from lxml import etree

from pbirb_mcp.core import names
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RD_NS, find_child, find_children, q
from pbirb_mcp.ops import expr_walk, vbexpr
from pbirb_mcp.ops.dataset import _is_pbidataset_dataset
from pbirb_mcp.ops.dax import data_field_matches, normalize_data_field, result_columns

Issue = dict[str, Any]


# ---- locator helpers ----------------------------------------------------


def _named_locator(elem: etree._Element) -> str:
    """Build a short ``Type[Name='X']`` locator. Falls back to local-name
    only when the element has no ``Name``.
    """
    tag = etree.QName(elem.tag).localname
    name = elem.get("Name")
    return f"{tag}[Name={name!r}]" if name else tag


def _ancestor_chain(elem: etree._Element, max_named: int = 3) -> str:
    """Locator for ``elem``: its nearest ``max_named`` named ancestors (the
    element itself included), outermost first, then the element's own tag
    when it isn't named — e.g. ``Tablix[Name='MainTable']>Textbox[Name='Amount']>Value``.

    Walks all the way to the root (#48): a textbox value sits six levels
    below its Textbox, so a fixed depth cut-off lost the name. Returns
    ``"(unnamed)"`` only when nothing up the chain has a Name.
    """
    if not isinstance(elem.tag, str):
        elem = elem.getparent()
        if elem is None:
            return "(unnamed)"
    named: list[str] = []
    cur: Optional[etree._Element] = elem
    while cur is not None and len(named) < max_named:
        if isinstance(cur.tag, str) and cur.get("Name"):
            named.append(_named_locator(cur))
        cur = cur.getparent()
    if not named:
        return "(unnamed)"
    parts = list(reversed(named))
    if not elem.get("Name"):
        parts.append(etree.QName(elem.tag).localname)
    return ">".join(parts)


# ---- expression-bearing nodes -------------------------------------------
#
# Every rule that inspects expressions walks them with
# :func:`pbirb_mcp.ops.expr_walk.iter_expressions` (#46): any element whose
# text starts with ``=``, except ``<CommandText>``, ``<Code>`` and
# non-RDL-namespace (``rd:``/``am:`` …) elements.


# ---- rule 1: multi-value-eq --------------------------------------------


def _multi_value_param_names(root: etree._Element) -> set[str]:
    out: set[str] = set()
    for rp in root.iter(q("ReportParameter")):
        mv = find_child(rp, "MultiValue")
        if mv is not None and (mv.text or "").strip().lower() == "true":
            name = rp.get("Name")
            if name:
                out.add(name)
    return out


def _is_eq(tok: vbexpr.Token) -> bool:
    return tok.kind == vbexpr.OPERATOR and tok.text == "="


def _rule_multi_value_eq(doc: RDLDocument) -> list[Issue]:
    """Flag ``Parameters!X.Value = ...`` / ``... = Parameters!X.Value`` for
    multi-value ``X``. Works on VB tokens, so references inside string
    literals or comments don't count, and ``<=`` / ``>=`` / ``<>`` are
    distinct operators rather than a bare ``=``. Indexed access
    (``Parameters!X.Value(0) = ...``) is a scalar and isn't flagged.
    """
    multi = _multi_value_param_names(doc.root)
    if not multi:
        return []
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for elem, text in expr_walk.iter_expressions(doc.root):
        if "parameters!" not in text.lower():
            continue
        tokens = vbexpr.tokenize_expression(text)
        if tokens is None:
            continue
        sig = vbexpr.significant(tokens)
        for ref in vbexpr.bang_references(sig, "Parameters"):
            if ref.name not in multi or (ref.member or "").lower() != "value":
                continue
            before = sig[ref.first - 1] if ref.first > 0 else None
            after = sig[ref.last + 1] if ref.last + 1 < len(sig) else None
            if after is not None and after.kind == vbexpr.DELIMITER and after.text == "(":
                continue  # Parameters!X.Value(0) — indexed, a scalar
            if not (
                (before is not None and _is_eq(before)) or (after is not None and _is_eq(after))
            ):
                continue
            key = (_ancestor_chain(elem), ref.name)
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                {
                    "severity": "warning",
                    "rule": "multi-value-eq",
                    "location": _ancestor_chain(elem),
                    "message": (
                        f"multi-value parameter {ref.name!r} compared with '='; Value is an array."
                    ),
                    "suggestion": (
                        f"use IN: ``Fields!X.Value IN (Parameters!{ref.name}.Value)`` "
                        f'or JOIN(Parameters!{ref.name}.Value, ",")'
                    ),
                }
            )
    issues.extend(_multi_value_filter_issues(doc, multi))
    return issues


def _bare_reference(sig: list[vbexpr.Token]) -> Optional[expr_walk.Ref]:
    """The single ``Parameters!X.Value`` an expression consists of
    (optionally wrapped in parentheses), else ``None``."""
    while (
        len(sig) >= 2
        and sig[0].text == "("
        and sig[-1].text == ")"
        and vbexpr.first_unbalanced_delimiter(sig[1:-1]) is None
    ):
        sig = sig[1:-1]
    refs = expr_walk.references(sig, "Parameters")
    if len(refs) == 1 and refs[0].first == 0 and refs[0].last == len(sig) - 1:
        return refs[0]
    return None


def _multi_value_filter_issues(doc: RDLDocument, multi: set[str]) -> list[Issue]:
    """``<Filter>`` with ``Operator`` ``Equal``/``NotEqual`` whose
    ``FilterValue`` is a multi-value parameter — the commonest form of
    the bug (#46): the filter compares each row against an array and
    Report Builder rejects it at run time. ``In`` is the operator for a
    multi-value list."""
    issues: list[Issue] = []
    for flt in doc.root.iter(q("Filter")):
        op = find_child(flt, "Operator")
        op_text = (op.text or "").strip() if op is not None else ""
        if op_text not in ("Equal", "NotEqual"):
            continue
        values = find_child(flt, "FilterValues")
        if values is None:
            continue
        for fv in find_children(values, "FilterValue"):
            sig = expr_walk.expression_tokens(fv.text)
            if not sig:
                continue
            ref = _bare_reference(sig)
            if ref is None or ref.name not in multi or (ref.member or "").lower() != "value":
                continue
            issues.append(
                {
                    "severity": "warning",
                    "rule": "multi-value-eq",
                    "location": _ancestor_chain(fv),
                    "message": (
                        f"filter Operator {op_text} compares against multi-value parameter "
                        f"{ref.name!r}; its Value is an array"
                    ),
                    "suggestion": (
                        "use Operator In (add_tablix_filter / add_dataset_filter with "
                        "operator='In'), or index a single value: "
                        f"Parameters!{ref.name}.Value(0)"
                    ),
                }
            )
    return issues


# ---- rule 2: unused-data-source ----------------------------------------


def _rule_unused_data_source(doc: RDLDocument) -> list[Issue]:
    sources = {ds.get("Name"): ds for ds in doc.root.iter(q("DataSource")) if ds.get("Name")}
    if not sources:
        return []
    referenced: set[str] = set()
    for ref in doc.root.iter(q("DataSourceName")):
        if ref.text:
            referenced.add(ref.text)
    return [
        {
            "severity": "warning",
            "rule": "unused-data-source",
            "location": f"DataSource[Name={name!r}]",
            "message": f"DataSource {name!r} has no DataSet that references it",
            "suggestion": "remove the unused DataSource or wire a DataSet to it",
        }
        for name in sources
        if name not in referenced
    ]


# ---- rule 3: unused-data-set -------------------------------------------


def _rule_unused_data_set(doc: RDLDocument) -> list[Issue]:
    datasets_block = find_child(doc.root, "DataSets")
    if datasets_block is None:
        return []
    datasets = {d.get("Name"): d for d in find_children(datasets_block, "DataSet") if d.get("Name")}
    if not datasets:
        return []
    referenced: set[str] = set()
    for ref in doc.root.iter(q("DataSetName")):
        if ref.text:
            referenced.add(ref.text)
    return [
        {
            "severity": "warning",
            "rule": "unused-data-set",
            "location": f"DataSet[Name={name!r}]",
            "message": f"DataSet {name!r} is never referenced from a tablix or chart",
        }
        for name in datasets
        if name not in referenced
    ]


# ---- rule 4: date-param-as-string --------------------------------------


_DATE_NAME_RE = re.compile(r"date", re.IGNORECASE)


def _rule_date_param_as_string(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    for rp in doc.root.iter(q("ReportParameter")):
        name = rp.get("Name") or ""
        if not _DATE_NAME_RE.search(name):
            continue
        dt = find_child(rp, "DataType")
        if dt is None or (dt.text or "").strip() != "String":
            continue
        issues.append(
            {
                "severity": "warning",
                "rule": "date-param-as-string",
                "location": f"ReportParameter[Name={name!r}]",
                "message": (
                    f"parameter {name!r} looks date-like but is typed as String; "
                    "string equality with a Date column will mismatch at runtime"
                ),
                "suggestion": "set DataType to DateTime",
            }
        )
    return issues


# ---- rule 5: missing-field-reference -----------------------------------


def _rule_missing_field_reference(doc: RDLDocument) -> list[Issue]:
    """A field reference — ``Fields!X.Value`` / ``.Label`` / ``.IsMissing``
    / any other member, or ``Fields("X")`` — to a field that no dataset
    declares, in any expression of the report (#46). A field that exists
    but in another dataset than the one in scope is ``field-out-of-scope``
    (:mod:`pbirb_mcp.ops.lint_rules.expressions`)."""
    valid = expr_walk.ScopeIndex(doc.root).all_fields
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()  # de-dupe by (location, field)
    for elem, text in expr_walk.iter_expressions(doc.root):
        if "fields" not in text.lower():
            continue
        # Only real code counts: ``Fields!X.Value`` inside a string
        # literal or comment is text.
        sig = expr_walk.expression_tokens(text)
        if not sig:
            continue
        for ref in expr_walk.references(sig, "Fields"):
            field = ref.name
            if field in valid:
                continue
            loc = _ancestor_chain(elem)
            if (loc, field) in seen:
                continue
            seen.add((loc, field))
            issues.append(
                {
                    "severity": "error",
                    "rule": "missing-field-reference",
                    "location": loc,
                    "message": (
                        f"{text[ref.start : ref.end]} referenced but field {field!r} "
                        "is not declared in any dataset"
                    ),
                    "suggestion": (
                        "add the field to the relevant dataset's <Fields> "
                        "(see add_dataset_field / refresh_dataset_fields)"
                    ),
                }
            )
    return issues


# ---- rule 6: page-number-out-of-chrome ---------------------------------


# Globals that only resolve in the page header/footer (#46): the issue's
# ``Globals!(Overall)?(PageNumber|TotalPages)|Globals!PageName``, matched
# on VB tokens so a mention inside a string literal doesn't count.
_PAGE_GLOBALS = frozenset(
    {"pagenumber", "totalpages", "overallpagenumber", "overalltotalpages", "pagename"}
)


def _under_chrome(elem: etree._Element) -> bool:
    """True iff elem has a <PageHeader> or <PageFooter> ancestor."""
    cur = elem.getparent()
    while cur is not None:
        local = etree.QName(cur.tag).localname
        if local in ("PageHeader", "PageFooter"):
            return True
        cur = cur.getparent()
    return False


def _rule_page_number_out_of_chrome(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    for elem, text in expr_walk.iter_expressions(doc.root):
        if "globals" not in text.lower():
            continue
        sig = expr_walk.expression_tokens(text)
        hits: list[str] = []
        for r in expr_walk.references(sig or [], "Globals"):
            if r.name.lower() in _PAGE_GLOBALS and r.name not in hits:
                hits.append(r.name)
        if not hits:
            continue
        if _under_chrome(elem):
            continue
        listed = ", ".join(f"Globals!{h}" for h in hits)
        issues.append(
            {
                "severity": "error",
                "rule": "page-number-out-of-chrome",
                "location": _ancestor_chain(elem),
                "message": f"{listed} only resolves inside <PageHeader>/<PageFooter>",
                "suggestion": "move the textbox to the page header/footer",
            }
        )
    return issues


# ---- rule 7: expression-syntax -----------------------------------------


def _rule_expression_syntax(doc: RDLDocument) -> list[Issue]:
    """Tokenize each ``=``-expression and report (a) an unterminated
    string literal, else (b) the first ``()`` / ``[]`` / ``{}`` that
    doesn't nest. Delimiters inside string/date literals and comments
    are ignored, so ``=Fields!A.Value & ") "`` is fine.
    """
    issues: list[Issue] = []
    for elem, text in expr_walk.iter_expressions(doc.root):
        tokens = vbexpr.tokenize_expression(text)
        if tokens is None:
            continue
        bad_string = vbexpr.unterminated_string(tokens)
        if bad_string is not None:
            issues.append(
                {
                    "severity": "error",
                    "rule": "expression-syntax",
                    "location": _ancestor_chain(elem),
                    "message": f"unterminated string literal in expression: {text[:80]!r}",
                    "suggestion": (
                        'close the string with a double quote; write a literal quote as ""'
                    ),
                }
            )
            continue
        bad_delim = vbexpr.first_unbalanced_delimiter(tokens)
        if bad_delim is not None:
            family = vbexpr.delimiter_family(bad_delim)
            issues.append(
                {
                    "severity": "error",
                    "rule": "expression-syntax",
                    "location": _ancestor_chain(elem),
                    "message": f"unbalanced {family} in expression: {text[:80]!r}",
                }
            )
    return issues


# ---- rule 8: dangling-embedded-image -----------------------------------


def _rule_dangling_embedded_image(doc: RDLDocument) -> list[Issue]:
    block = find_child(doc.root, "EmbeddedImages")
    declared: set[str] = set()
    if block is not None:
        for entry in find_children(block, "EmbeddedImage"):
            if entry.get("Name"):
                declared.add(entry.get("Name"))
    issues: list[Issue] = []
    # <Image> report items and Style/<BackgroundImage> (#47) share the
    # Source/Value shape.
    for img in doc.root.iter(q("Image"), q("BackgroundImage")):
        source = find_child(img, "Source")
        if source is None or (source.text or "").strip() != "Embedded":
            continue
        value = find_child(img, "Value")
        ref = (value.text or "").strip() if value is not None else ""
        # The Value element holds either a literal name (e.g. "Logo") or an
        # expression (e.g. "=Fields!X.Value"). Skip expressions — we can't
        # statically resolve them.
        if not ref or ref.startswith("="):
            continue
        if ref in declared:
            continue
        if etree.QName(img).localname == "BackgroundImage":
            location = _ancestor_chain(img)
            what = "BackgroundImage"
        else:
            location = f"Image[Name={img.get('Name')!r}]" if img.get("Name") else "Image"
            what = "Image"
        issues.append(
            {
                "severity": "error",
                "rule": "dangling-embedded-image",
                "location": location,
                "message": (
                    f"{what} Source=Embedded references {ref!r}, which isn't in <EmbeddedImages>"
                ),
                "suggestion": "add the image via add_embedded_image or fix the Value reference",
            }
        )
    return issues


# ---- rule 9: dangling-data-source-reference ----------------------------


def _rule_dangling_data_source_reference(doc: RDLDocument) -> list[Issue]:
    sources = {ds.get("Name") for ds in doc.root.iter(q("DataSource")) if ds.get("Name")}
    issues: list[Issue] = []
    # Restrict to <DataSet>/<Query>/<DataSourceName> — that's where the
    # reference actually has to resolve. <DataSource>'s own children
    # (e.g. nested DataSourceReference shapes) are different beasts.
    datasets_block = find_child(doc.root, "DataSets")
    if datasets_block is None:
        return []
    for ds in find_children(datasets_block, "DataSet"):
        query = find_child(ds, "Query")
        if query is None:
            continue
        ref = find_child(query, "DataSourceName")
        if ref is None or not ref.text:
            continue
        if ref.text in sources:
            continue
        issues.append(
            {
                "severity": "error",
                "rule": "dangling-data-source-reference",
                "location": f"DataSet[Name={ds.get('Name')!r}]/Query/DataSourceName",
                "message": (
                    f"DataSet {ds.get('Name')!r} references DataSource {ref.text!r}, "
                    "which isn't declared"
                ),
                "suggestion": ("add the DataSource via add_data_source or update the reference"),
            }
        )
    return issues


# ---- rule 10: dangling-action ------------------------------------------


def _rule_dangling_action(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    for action in doc.root.iter(q("Action")):
        drill = find_child(action, "Drillthrough")
        if drill is None:
            continue
        report_name = find_child(drill, "ReportName")
        text = (report_name.text or "").strip() if report_name is not None else ""
        if text:
            continue
        issues.append(
            {
                "severity": "warning",
                "rule": "dangling-action",
                "location": _ancestor_chain(action),
                "message": "drillthrough action has empty <ReportName>",
                "suggestion": "set the target report path/name",
            }
        )
    return issues


# ---- rule 11: pbidataset-at-prefix -------------------------------------


def _rule_pbidataset_at_prefix(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    datasets_block = find_child(doc.root, "DataSets")
    if datasets_block is None:
        return []
    for ds in find_children(datasets_block, "DataSet"):
        if not _is_pbidataset_dataset(doc, ds):
            continue
        query = find_child(ds, "Query")
        if query is None:
            continue
        params_block = find_child(query, "QueryParameters")
        if params_block is None:
            continue
        for qp in find_children(params_block, "QueryParameter"):
            name = qp.get("Name") or ""
            if not name.startswith("@"):
                continue
            issues.append(
                {
                    "severity": "error",
                    "rule": "pbidataset-at-prefix",
                    "location": (
                        f"DataSet[Name={ds.get('Name')!r}]/Query/"
                        f"QueryParameters/QueryParameter[Name={name!r}]"
                    ),
                    "message": (
                        f"PBIDATASET QueryParameter Name {name!r} has a leading '@'; "
                        "the bare name is what binds. The '@' belongs only in the DAX text."
                    ),
                    "suggestion": f"rename to {name.lstrip('@')!r}",
                }
            )
    return issues


# ---- rule 12: parameter-layout-out-of-sync -----------------------------


def _rule_parameter_layout_out_of_sync(doc: RDLDocument) -> list[Issue]:
    params_block = find_child(doc.root, "ReportParameters")
    layout_block = find_child(doc.root, "ReportParametersLayout")
    if params_block is None or layout_block is None:
        return []
    grid = find_child(layout_block, "GridLayoutDefinition")
    if grid is None:
        return []
    cells = find_child(grid, "CellDefinitions")
    if cells is None:
        return []
    param_count = len(find_children(params_block, "ReportParameter"))
    cell_count = len(find_children(cells, "CellDefinition"))
    if param_count == cell_count:
        return []
    return [
        {
            "severity": "error",
            "rule": "parameter-layout-out-of-sync",
            "location": "ReportParametersLayout/GridLayoutDefinition/CellDefinitions",
            "message": (
                f"<ReportParameters> declares {param_count} parameter(s) but "
                f"<CellDefinitions> has {cell_count} cell(s)"
            ),
            "suggestion": (
                "call sync_parameter_layout(path) to fill gaps, or "
                "set_parameter_layout(...) to author the grid explicitly"
            ),
        }
    ]


# ---- rule 13: double-encoded-entities ----------------------------------


_DOUBLE_ENCODED_RE = re.compile(r"&(?:amp|lt|gt|quot);")


# Elements whose text legitimately contains entity-like sequences: VB
# code, query text (DAX/SQL/MDX string literals), and base64 payloads.
# Anything outside the report's own namespace (``rd:*`` designer state,
# ``am:``/``df:`` metadata, ...) is skipped too — it's never rendered.
_DOUBLE_ENCODED_SKIP_TAGS = frozenset({"Code", "CommandText", "ImageData"})


def _is_html_markup_run_value(elem: etree._Element) -> bool:
    """True for ``<TextRun>/<Value>`` when the run has
    ``<MarkupType>HTML</MarkupType>`` — entities are the markup there.
    """
    parent = elem.getparent()
    if parent is None or etree.QName(parent.tag).localname != "TextRun":
        return False
    markup = find_child(parent, "MarkupType")
    return markup is not None and (markup.text or "").strip().upper() == "HTML"


def _rule_double_encoded_entities(doc: RDLDocument) -> list[Issue]:
    """Scan element text for the post-decode signature of double-encoding.

    XML decodes ``&amp;amp;`` to literal ``&amp;`` in ``.text``. So if a
    parsed text contains the four entity literals (``&amp;``, ``&lt;``,
    ``&gt;``, ``&quot;``), the source was double-encoded.

    Only report-content elements are scanned: ``<Code>``,
    ``<CommandText>``, ``<ImageData>``, non-RDL-namespace elements
    (``rd:*`` etc.) and HTML-markup text runs are skipped, because a
    literal ``&amp;`` is legitimate there.
    """
    report_ns = etree.QName(doc.root.tag).namespace
    issues: list[Issue] = []
    for elem in doc.root.iter(etree.Element):
        text = elem.text
        if not text or "&" not in text:
            continue
        qname = etree.QName(elem.tag)
        if qname.namespace != report_ns or qname.localname in _DOUBLE_ENCODED_SKIP_TAGS:
            continue
        m = _DOUBLE_ENCODED_RE.search(text)
        if not m:
            continue
        if _is_html_markup_run_value(elem):
            continue
        issues.append(
            {
                "severity": "error",
                "rule": "double-encoded-entities",
                "location": _ancestor_chain(elem),
                "message": (
                    f"text contains a double-encoded entity {m.group(0)!r}; "
                    "the writer fed pre-encoded text through XML serialisation"
                ),
                "suggestion": "decode the text once, then let the writer encode it",
            }
        )
    return issues


# ---- rule 14: stale-designer-state -------------------------------------


def _rule_stale_designer_state(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    for ds in doc.root.iter(q("DataSet")):
        query = find_child(ds, "Query")
        if query is None:
            continue
        cmd = find_child(query, "CommandText")
        if cmd is None or cmd.text is None:
            continue
        # find_child takes a local name only, so reach into rd: directly.
        designer = query.find(f"{{{RD_NS}}}DesignerState")
        if designer is None:
            continue
        statement = designer.find(f"{{{RD_NS}}}Statement")
        if statement is None or statement.text is None:
            continue
        if statement.text.strip() == cmd.text.strip():
            continue
        issues.append(
            {
                "severity": "warning",
                "rule": "stale-designer-state",
                "location": f"DataSet[Name={ds.get('Name')!r}]/Query/rd:DesignerState/Statement",
                "message": (
                    "<rd:DesignerState>/<Statement> diverges from <CommandText>; "
                    "the Query Designer GUI will display stale DAX"
                ),
                "suggestion": "call update_dataset_query() to sync both at once",
            }
        )
    return issues


# ---- rule 15: tablix-span-misplaced ------------------------------------


def _rule_tablix_span_misplaced(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    for cell in doc.root.iter(q("TablixCell")):
        for tag in ("ColSpan", "RowSpan"):
            direct = find_child(cell, tag)
            if direct is None:
                continue
            issues.append(
                {
                    "severity": "error",
                    "rule": "tablix-span-misplaced",
                    "location": _ancestor_chain(cell),
                    "message": (
                        f"<{tag}> is a direct child of <TablixCell>; "
                        "RDL 2016 requires it inside <CellContents>"
                    ),
                    "suggestion": (
                        "move <ColSpan>/<RowSpan> inside <CellContents>; "
                        "set_cell_span() does this automatically"
                    ),
                }
            )
    return issues


# ---- rule 16: dataset-fields-out-of-sync (v0.3.1) ----------------------


def _rule_dataset_fields_out_of_sync(doc: RDLDocument) -> list[Issue]:
    """Compare a dataset's data-bound ``<Field>`` declarations with the
    columns its DAX ``<CommandText>`` returns, in both directions (#51).

    Each Field's ``<DataField>`` is matched (never its ``Name`` — Report
    Builder names ``Sales[Amount (USD)]`` ``Amount__USD_``) against
    :func:`pbirb_mcp.ops.dax.result_columns` with
    :func:`pbirb_mcp.ops.dax.data_field_matches` (quotes stripped,
    case-insensitive — the same rule ``refresh_dataset_fields`` uses).
    Two issue kinds, both warnings, told apart by the issue's ``kind``:

    * ``field-not-returned`` — a declared DataField the DAX doesn't
      return (e.g. a column that only appears in a FILTER, or one a query
      rewrite dropped).
    * ``column-not-bound`` — a column the DAX returns that no Field binds
      (the case that breaks a report after a query rewrite).

    Calculated fields (``<Value>``, no ``<DataField>``) are skipped. When
    the extractor returns warnings (the DAX shape isn't recognisable —
    e.g. bare ``EVALUATE 'Table'``), the dataset is skipped rather than
    false-flagged.
    """
    issues: list[Issue] = []
    datasets_root = find_child(doc.root, "DataSets")
    if datasets_root is None:
        return issues
    for ds in find_children(datasets_root, "DataSet"):
        ds_name = ds.get("Name") or "(unnamed)"
        query = find_child(ds, "Query")
        if query is None:
            continue
        cmd = find_child(query, "CommandText")
        if cmd is None or not (cmd.text or "").strip():
            continue
        columns, warnings = result_columns(cmd.text)
        if warnings:
            # Unparseable shape — skip rather than false-flag.
            continue
        if not columns:
            continue
        fields_root = find_child(ds, "Fields")
        bound: list[str] = []
        fields = find_children(fields_root, "Field") if fields_root is not None else []
        for f in fields:
            name = f.get("Name")
            if name is None:
                continue
            data_field = find_child(f, "DataField")
            # Calculated fields (have <Value>, not <DataField>) are
            # author-defined and never expected to come back from DAX.
            if data_field is None and find_child(f, "Value") is not None:
                continue
            df_text = data_field.text if data_field is not None else None
            if df_text and df_text.strip():
                bound.append(df_text)
            if df_text is not None and any(data_field_matches(df_text, c) for c in columns):
                continue
            shown = (df_text or "").strip() or "(none)"
            issues.append(
                {
                    "severity": "warning",
                    "rule": "dataset-fields-out-of-sync",
                    "kind": "field-not-returned",
                    "location": (f"DataSet[Name={ds_name!r}]/Fields/Field[Name={name!r}]"),
                    "message": (
                        f"field-not-returned: <Field Name={name!r}> binds DataField "
                        f"{shown!r}, which the DAX <CommandText> does not return"
                    ),
                    "suggestion": (
                        "rewrite the dataset query (update_dataset_query) "
                        "or remove the orphan field (remove_dataset_field)"
                    ),
                }
            )
        seen: set[str] = set()
        for c in columns:
            key = normalize_data_field(c.data_field)
            if key in seen:
                continue
            seen.add(key)
            if any(data_field_matches(df, c) for df in bound):
                continue
            issues.append(
                {
                    "severity": "warning",
                    "rule": "dataset-fields-out-of-sync",
                    "kind": "column-not-bound",
                    "location": f"DataSet[Name={ds_name!r}]/Fields",
                    "message": (
                        f"column-not-bound: the DAX <CommandText> returns {c.data_field} "
                        "but no <Field> binds it"
                    ),
                    "suggestion": (
                        "add the field (refresh_dataset_fields / add_dataset_field) "
                        "or drop the column from the query"
                    ),
                }
            )
    return issues


# ---- rules 17-18: duplicate-name / invalid-identifier (#20) -------------


def _per_kind_namespaces(doc: RDLDocument):
    """Yield ``(label, elements)`` for each RDL namespace that keeps its own
    uniqueness: data sources, datasets, parameters, embedded images, and
    the fields of each dataset."""
    root = doc.root
    for container, child, label in (
        ("DataSources", "DataSource", "DataSource"),
        ("DataSets", "DataSet", "DataSet"),
        ("ReportParameters", "ReportParameter", "ReportParameter"),
        ("EmbeddedImages", "EmbeddedImage", "EmbeddedImage"),
    ):
        block = find_child(root, container)
        if block is not None:
            yield label, find_children(block, child)
    datasets_root = find_child(root, "DataSets")
    if datasets_root is not None:
        for ds in find_children(datasets_root, "DataSet"):
            fields_root = find_child(ds, "Fields")
            if fields_root is not None:
                yield (
                    f"DataSet[Name={ds.get('Name')!r}]/Field",
                    find_children(fields_root, "Field"),
                )


def _rule_duplicate_name(doc: RDLDocument) -> list[Issue]:
    """Two report items / groups (one report-wide namespace), or two
    entries of a per-kind namespace, share a ``Name``. Report Builder
    refuses such a file and name-addressed tools raise
    ``AmbiguousElementError`` on it."""
    issues: list[Issue] = []
    scope: dict[str, list[etree._Element]] = {}
    for el in names.iter_scope_elements(doc):
        scope.setdefault(el.get("Name"), []).append(el)
    for name, els in scope.items():
        if len(els) < 2:
            continue
        where = "; ".join(names.describe_location(e) for e in els)
        issues.append(
            {
                "severity": "error",
                "rule": "duplicate-name",
                "location": _named_locator(els[1]),
                "message": (
                    f"name {name!r} is used {len(els)} times ({where}); report item "
                    "and group names must be unique report-wide"
                ),
                "suggestion": "rename all but one so every report item and group name is unique",
            }
        )
    for label, els in _per_kind_namespaces(doc):
        counts: dict[str, int] = {}
        for el in els:
            n = el.get("Name")
            if n is not None:
                counts[n] = counts.get(n, 0) + 1
        for name, count in counts.items():
            if count < 2:
                continue
            issues.append(
                {
                    "severity": "error",
                    "rule": "duplicate-name",
                    "location": f"{label}[Name={name!r}]",
                    "message": f"{count} {label} entries are named {name!r}; names must be unique",
                    "suggestion": "rename or remove the duplicates",
                }
            )
    return issues


def _rule_invalid_identifier(doc: RDLDocument) -> list[Issue]:
    """A report item, group, data source, dataset, parameter, embedded
    image or field ``Name`` is not a valid RDL (CLS-compliant)
    identifier — Report Builder refuses to load it."""
    issues: list[Issue] = []

    def check(el: etree._Element, location: str) -> None:
        n = el.get("Name")
        if n is None or names.is_identifier(n):
            return
        issues.append(
            {
                "severity": "error",
                "rule": "invalid-identifier",
                "location": location,
                "message": (
                    f"{etree.QName(el).localname} name {n!r} is not a valid RDL identifier "
                    "(must start with a letter; letters, digits and underscores only; "
                    f"at most {names.MAX_IDENTIFIER_LENGTH} characters)"
                ),
                "suggestion": f"rename it, e.g. to {names.sanitize_identifier(n)!r}",
            }
        )

    for el in names.iter_scope_elements(doc):
        check(el, _named_locator(el))
    for label, els in _per_kind_namespaces(doc):
        for el in els:
            check(el, f"{label}[Name={el.get('Name')!r}]")
    return issues


def _rule_dangling_toggle_item(doc: RDLDocument) -> list[Issue]:
    """A ``<ToggleItem>`` Report Builder rejects at preview/publish (#42).
    Same checks ``set_element_visibility`` applies before writing one."""
    from pbirb_mcp.ops.visibility import toggle_item_problem

    issues: list[Issue] = []
    for toggle in doc.root.iter(q("ToggleItem")):
        vis = toggle.getparent()
        owner = vis.getparent() if vis is not None else None
        if owner is None or etree.QName(vis).localname != "Visibility":
            continue
        problem = toggle_item_problem(doc.root, owner, (toggle.text or "").strip())
        if problem is None:
            continue
        issues.append(
            {
                "severity": "error",
                "rule": "dangling-toggle-item",
                "location": _ancestor_chain(toggle),
                "message": problem,
                "suggestion": "point ToggleItem at a valid textbox, or remove it",
            }
        )
    return issues


# ---- registry -----------------------------------------------------------


_RULES: dict[str, Callable[[RDLDocument], list[Issue]]] = {
    "multi-value-eq": _rule_multi_value_eq,
    "unused-data-source": _rule_unused_data_source,
    "unused-data-set": _rule_unused_data_set,
    "date-param-as-string": _rule_date_param_as_string,
    "missing-field-reference": _rule_missing_field_reference,
    "page-number-out-of-chrome": _rule_page_number_out_of_chrome,
    "expression-syntax": _rule_expression_syntax,
    "dangling-embedded-image": _rule_dangling_embedded_image,
    "dangling-data-source-reference": _rule_dangling_data_source_reference,
    "dangling-action": _rule_dangling_action,
    "pbidataset-at-prefix": _rule_pbidataset_at_prefix,
    "parameter-layout-out-of-sync": _rule_parameter_layout_out_of_sync,
    "double-encoded-entities": _rule_double_encoded_entities,
    "stale-designer-state": _rule_stale_designer_state,
    "tablix-span-misplaced": _rule_tablix_span_misplaced,
    "dataset-fields-out-of-sync": _rule_dataset_fields_out_of_sync,
    "duplicate-name": _rule_duplicate_name,
    "invalid-identifier": _rule_invalid_identifier,
    "dangling-toggle-item": _rule_dangling_toggle_item,
}

# Rule families in pbirb_mcp/ops/lint_rules/ (auto-discovered). Imported
# here, after every helper above is defined, because those modules import
# helpers from this one.
from pbirb_mcp.ops import lint_rules as _lint_rules  # noqa: E402

for _name, _fn in _lint_rules.load().items():
    if _name in _RULES:
        raise ValueError(f"lint rule {_name!r} defined twice")
    _RULES[_name] = _fn

ALL_RULES = tuple(_RULES.keys())


def _resolve_rules(rules: list[str] | None) -> list[str]:
    """Validate and expand a ``rules`` selector. ``None`` or an empty list
    means every rule (#48 — an empty selection used to run nothing and
    report a clean file). Unknown names raise."""
    if not rules:
        return list(_RULES.keys())
    unknown = [r for r in rules if r not in _RULES]
    if unknown:
        raise ValueError(f"unknown lint rule(s): {unknown}; known rules: {ALL_RULES}")
    return list(rules)


def _lint_doc(doc: RDLDocument, rules: list[str] | None = None) -> dict[str, Any]:
    """Run lint rules against an already-open document.

    The transaction commit path (v0.4 commit 10) and ``apply_edits``
    (v0.4 commit 11) call this directly so they can lint the in-memory
    tree without writing to a tempfile and re-parsing — important
    because the on-disk file is intentionally stale during a
    transaction.

    Behaves identically to :func:`lint_report` but skips the
    ``RDLDocument.open(path)`` step. Returns the same shape:
    ``{issues, rules_run}``.
    """
    run = _resolve_rules(rules)
    issues: list[Issue] = []
    for name in run:
        issues.extend(_RULES[name](doc))
    return {"issues": issues, "rules_run": run}


def lint_report(path: str, rules: list[str] | None = None) -> dict[str, Any]:
    """Run lint rules against an RDL.

    ``rules`` selects a subset by name; ``None`` runs all of them. Unknown
    rule names are rejected with ``ValueError`` so a typo doesn't quietly
    skip checks.

    Returns ``{issues, rules_run}`` where ``issues`` is a list of
    ``{severity, rule, location, message, suggestion?}`` dicts.
    """
    doc = RDLDocument.open(path)
    return _lint_doc(doc, rules)


__all__ = ["_lint_doc", "lint_report", "ALL_RULES"]
