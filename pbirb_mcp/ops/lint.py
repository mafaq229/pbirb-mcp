"""Static-analysis lint rules (Phase 7 commit 31; v0.3.1 added rule 16).

Sixteen rules driven by the v0.2/v0.3 sweep feedback. Each rule is a
small pure function that takes the open :class:`RDLDocument` and
returns a list of issue dicts:

    {"severity": "error"|"warning",
     "rule": "<rule-name>",
     "location": "<XPath-ish locator>",
     "message": "<human-readable>",
     "suggestion": "<optional fix hint>"}

The shape mirrors :mod:`pbirb_mcp.ops.validate` so :func:`verify_report`
(commit 33) can union the two streams without reshaping.

Rules (16):

* ``multi-value-eq`` (warn) — multi-value param compared with ``=``.
* ``unused-data-source`` (warn) — DataSource never referenced.
* ``unused-data-set`` (warn) — DataSet never referenced.
* ``date-param-as-string`` (warn) — ``*date*``-named param typed as
  String.
* ``missing-field-reference`` (error) — ``Fields!X.Value`` for X not in
  any dataset's ``<Fields>``.
* ``page-number-out-of-chrome`` (error) — ``Globals!PageNumber`` /
  ``Globals!TotalPages`` outside header/footer.
* ``expression-syntax`` (error) — expression with mismatched parens or
  brackets.
* ``dangling-embedded-image`` (error) — ``Image Source=Embedded`` whose
  ``Value`` doesn't name an ``<EmbeddedImage>``.
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
  double-encoding regression).
* ``stale-designer-state`` (warn) — ``<rd:DesignerState>/<Statement>``
  diverges from ``<CommandText>``.
* ``tablix-span-misplaced`` (error) — ``<ColSpan>``/``<RowSpan>``
  outside ``<CellContents>``.
* ``dataset-fields-out-of-sync`` (warn, v0.3.1) — ``<Field>`` declared
  but the dataset's DAX ``<CommandText>`` doesn't return that column.
  Reuses :func:`pbirb_mcp.ops.dataset._extract_dax_field_names`.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RD_NS, find_child, find_children, q
from pbirb_mcp.ops.dataset import _extract_dax_field_names, _is_pbidataset_dataset

Issue = dict[str, Any]


# ---- locator helpers ----------------------------------------------------


def _named_locator(elem: etree._Element) -> str:
    """Build a short ``Type[Name='X']`` locator. Falls back to local-name
    only when the element has no ``Name``.
    """
    tag = etree.QName(elem.tag).localname
    name = elem.get("Name")
    return f"{tag}[Name={name!r}]" if name else tag


def _ancestor_chain(elem: etree._Element, max_depth: int = 4) -> str:
    """Walk parents, collect named ancestors, return ``A>B>C``."""
    parts: list[str] = []
    cur = elem
    depth = 0
    while cur is not None and depth < max_depth:
        local = etree.QName(cur.tag).localname
        if cur.get("Name"):
            parts.append(f"{local}[Name={cur.get('Name')!r}]")
        cur = cur.getparent()
        depth += 1
    return ">".join(reversed(parts)) if parts else "(unnamed)"


# ---- expression-bearing nodes -------------------------------------------

# Elements whose text is treated as an expression in RDL. ``<Value>``
# inside ``<Textbox>/<Paragraphs>/<Paragraph>/<TextRuns>/<TextRun>`` is
# the canonical example; aggregates also live in tablix cells, query
# parameters, filter expressions, etc.
_EXPRESSION_TAGS = (
    "Value",
    "FilterExpression",
    "Filter",
    "GroupExpression",
    "SortExpression",
    "ToolTip",
    "Label",
    "Hidden",
    "ReportName",
    "BackgroundColor",
    "Color",
    "FontWeight",
    "FontStyle",
    "Action",
)


def _iter_expression_nodes(root: etree._Element):
    """Yield all elements whose text is treated as an expression. We
    over-approximate (every ``<Value>`` regardless of parent) — false
    positives are tolerable because the rule body filters by content
    (``=``-prefix, ``Fields!``, ``Parameters!``, etc.).
    """
    for tag in (
        "Value",
        "FilterExpression",
        "GroupExpression",
        "SortExpression",
        "ToolTip",
        "Label",
        "Hidden",
        "InitialToggleState",
    ):
        yield from root.iter(q(tag))


# ---- rule 1: multi-value-eq --------------------------------------------


_PARAM_REF_RE = re.compile(r"Parameters!(\w+)\.Value")


def _multi_value_param_names(root: etree._Element) -> set[str]:
    out: set[str] = set()
    for rp in root.iter(q("ReportParameter")):
        mv = find_child(rp, "MultiValue")
        if mv is not None and (mv.text or "").strip().lower() == "true":
            name = rp.get("Name")
            if name:
                out.add(name)
    return out


def _rule_multi_value_eq(doc: RDLDocument) -> list[Issue]:
    multi = _multi_value_param_names(doc.root)
    if not multi:
        return []
    names_alt = "|".join(re.escape(n) for n in multi)
    # Two directions: param on the LEFT (`Parameters!X.Value =`) or on
    # the RIGHT (`= Parameters!X.Value`). `=(?!=)` keeps `==` out, but
    # the LHS form must also reject the leading `=` that starts every
    # RDL expression — handled by requiring a non-`=`-anchored prefix.
    pattern_left = re.compile(r"Parameters!(" + names_alt + r")\.Value\s*=(?!=)")
    pattern_right = re.compile(r"(?<!=)=\s*Parameters!(" + names_alt + r")\.Value")
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for elem in _iter_expression_nodes(doc.root):
        if not elem.text or "Parameters!" not in elem.text:
            continue
        # Strip the leading `=` that starts every expression — it would
        # otherwise match pattern_right at the head and false-positive.
        body = elem.text.lstrip()
        if body.startswith("="):
            body = body[1:]
        for pattern in (pattern_left, pattern_right):
            for m in pattern.finditer(body):
                key = (_ancestor_chain(elem), m.group(1))
                if key in seen:
                    continue
                seen.add(key)
                issues.append(
                    {
                        "severity": "warning",
                        "rule": "multi-value-eq",
                        "location": _ancestor_chain(elem),
                        "message": (
                            f"multi-value parameter {m.group(1)!r} compared with '='; "
                            "Value is an array."
                        ),
                        "suggestion": (
                            f"use IN: ``Fields!X.Value IN (Parameters!{m.group(1)}.Value)`` "
                            f'or JOIN(Parameters!{m.group(1)}.Value, ",")'
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


_FIELD_REF_RE = re.compile(r"Fields!(\w+)\.Value")


def _all_field_names(doc: RDLDocument) -> set[str]:
    """Union of every ``<Field Name>`` in every dataset."""
    out: set[str] = set()
    for f in doc.root.iter(q("Field")):
        if f.get("Name"):
            out.add(f.get("Name"))
    return out


def _rule_missing_field_reference(doc: RDLDocument) -> list[Issue]:
    valid = _all_field_names(doc)
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()  # de-dupe by (location, field)
    for elem in _iter_expression_nodes(doc.root):
        if not elem.text or "Fields!" not in elem.text:
            continue
        for m in _FIELD_REF_RE.finditer(elem.text):
            field = m.group(1)
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
                    "message": f"Fields!{field}.Value referenced but not declared in any dataset",
                    "suggestion": (
                        "add the field to the relevant dataset's <Fields> "
                        "(see add_dataset_field / refresh_dataset_fields)"
                    ),
                }
            )
    return issues


# ---- rule 6: page-number-out-of-chrome ---------------------------------


_PAGE_GLOBAL_RE = re.compile(r"Globals!(PageNumber|TotalPages)")


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
    for elem in _iter_expression_nodes(doc.root):
        if not elem.text or "Globals!" not in elem.text:
            continue
        m = _PAGE_GLOBAL_RE.search(elem.text)
        if not m:
            continue
        if _under_chrome(elem):
            continue
        issues.append(
            {
                "severity": "error",
                "rule": "page-number-out-of-chrome",
                "location": _ancestor_chain(elem),
                "message": (f"Globals!{m.group(1)} only resolves inside <PageHeader>/<PageFooter>"),
                "suggestion": "move the textbox to the page header/footer",
            }
        )
    return issues


# ---- rule 7: expression-syntax -----------------------------------------


def _balanced(text: str, open_ch: str, close_ch: str) -> bool:
    depth = 0
    for ch in text:
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _rule_expression_syntax(doc: RDLDocument) -> list[Issue]:
    issues: list[Issue] = []
    for elem in _iter_expression_nodes(doc.root):
        text = elem.text
        if not text or not text.lstrip().startswith("="):
            continue
        if not _balanced(text, "(", ")"):
            issues.append(
                {
                    "severity": "error",
                    "rule": "expression-syntax",
                    "location": _ancestor_chain(elem),
                    "message": f"unbalanced parentheses in expression: {text[:80]!r}",
                }
            )
            continue
        if not _balanced(text, "[", "]"):
            issues.append(
                {
                    "severity": "error",
                    "rule": "expression-syntax",
                    "location": _ancestor_chain(elem),
                    "message": f"unbalanced brackets in expression: {text[:80]!r}",
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
    for img in doc.root.iter(q("Image")):
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
        issues.append(
            {
                "severity": "error",
                "rule": "dangling-embedded-image",
                "location": f"Image[Name={img.get('Name')!r}]" if img.get("Name") else "Image",
                "message": f"Image Source=Embedded references {ref!r}, which isn't in <EmbeddedImages>",
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


def _rule_double_encoded_entities(doc: RDLDocument) -> list[Issue]:
    """Scan element text for the post-decode signature of double-encoding.

    XML decodes ``&amp;amp;`` to literal ``&amp;`` in ``.text``. So if a
    parsed text contains the four entity literals (``&amp;``, ``&lt;``,
    ``&gt;``, ``&quot;``), the source was double-encoded.
    """
    issues: list[Issue] = []
    for elem in doc.root.iter():
        text = elem.text
        if not text:
            continue
        m = _DOUBLE_ENCODED_RE.search(text)
        if not m:
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
    """Detect ``<Field>`` declarations that no longer correspond to any
    column the DAX ``<CommandText>`` is expected to return.

    Same regression class as the v0.3.0 SELECTCOLUMNS over-match
    (commit ``9792281``), but caught at the static-lint layer instead
    of the runtime ``refresh_dataset_fields`` reporter — useful when
    nobody has called the refresh tool yet but the dataset query was
    rewritten in a way that drops columns.

    Reuses :func:`pbirb_mcp.ops.dataset._extract_dax_field_names`. When
    the extractor returns warnings (the DAX shape isn't recognisable —
    e.g. bare ``EVALUATE 'Table'``), the rule silently skips that
    dataset rather than false-flagging every declared field as orphan.
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
        extracted, warnings = _extract_dax_field_names(cmd.text)
        if warnings:
            # Unparseable shape — skip rather than false-flag.
            continue
        if not extracted:
            continue
        fields_root = find_child(ds, "Fields")
        if fields_root is None:
            continue
        extracted_set = set(extracted)
        for f in find_children(fields_root, "Field"):
            name = f.get("Name")
            if name is None:
                continue
            # Calculated fields (have <Value>, not <DataField>) are
            # author-defined and never expected to come back from DAX —
            # skip them.
            if find_child(f, "Value") is not None and find_child(f, "DataField") is None:
                continue
            if name in extracted_set:
                continue
            issues.append(
                {
                    "severity": "warning",
                    "rule": "dataset-fields-out-of-sync",
                    "location": (f"DataSet[Name={ds_name!r}]/Fields/Field[Name={name!r}]"),
                    "message": (
                        f"<Field Name={name!r}> is declared but the DAX "
                        f"<CommandText> does not appear to return that "
                        f"column"
                    ),
                    "suggestion": (
                        "rewrite the dataset query (update_dataset_query) "
                        "or remove the orphan field (remove_dataset_field)"
                    ),
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
}

ALL_RULES = tuple(_RULES.keys())


def lint_report(path: str, rules: list[str] | None = None) -> dict[str, Any]:
    """Run lint rules against an RDL.

    ``rules`` selects a subset by name; ``None`` runs all 16. Unknown
    rule names are rejected with ``ValueError`` so a typo doesn't quietly
    skip checks.

    Returns ``{issues, rules_run}`` where ``issues`` is a list of
    ``{severity, rule, location, message, suggestion?}`` dicts.
    """
    if rules is not None:
        unknown = [r for r in rules if r not in _RULES]
        if unknown:
            raise ValueError(f"unknown lint rule(s): {unknown}; known rules: {ALL_RULES}")
        run = list(rules)
    else:
        run = list(_RULES.keys())

    doc = RDLDocument.open(path)
    issues: list[Issue] = []
    for name in run:
        issues.extend(_RULES[name](doc))
    return {"issues": issues, "rules_run": run}


__all__ = ["lint_report", "ALL_RULES"]
