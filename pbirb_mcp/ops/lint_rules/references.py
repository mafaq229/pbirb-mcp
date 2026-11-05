"""Reference-integrity rules (#47).

The XSD types every name reference as a plain string, so a report can be
schema-valid and still point at things that don't exist. Report Builder
rejects these on load or preview; the edit tools (rename/remove a
parameter, dataset or data source; retarget a data region) can introduce
them.

* ``undefined-parameter`` (error) — ``Parameters!X`` / ``Parameters("X")``
  for X not a ``<ReportParameter>``.
* ``undefined-report-item`` (error) — ``ReportItems!X`` /
  ``ReportItems("X")`` for X not a report item.
* ``dangling-bookmark-link`` (warning) — an ``Action/BookmarkLink`` that
  matches no literal ``<Bookmark>`` (skipped when a ``Bookmark`` is an
  expression — it could evaluate to anything).
* ``unknown-aggregate-scope`` (error) — the scope argument of an
  aggregate / ``RowNumber`` / ``RunningValue`` / ``InScope`` / ``Level``
  names no dataset, data region or group; the dataset argument of
  ``Lookup`` / ``LookupSet`` / ``MultiLookup`` names no dataset.
* ``dangling-dataset-reference`` (error) — a ``<DataSetName>`` (Tablix,
  Chart, GaugePanel, MapDataRegion, CustomReportItem, a parameter's
  ``DataSetReference``) that names no ``<DataSet>``.
* ``missing-data-source-name`` (error) — a ``<DataSet>/<Query>`` without
  a ``<DataSourceName>``.
* ``parameter-forward-dependency`` (error) — a parameter's default
  value, available values, or the query of the dataset those come from,
  references the parameter itself or one declared after it.
* ``empty-hyperlink`` (warning) — an ``Action/Hyperlink`` with no URL.

Expressions are found with :func:`pbirb_mcp.ops.expr_walk.iter_expressions`
and references on VB tokens, so names inside string literals and
comments never count. Duplicate names and invalid identifiers are
``duplicate-name`` / ``invalid-identifier`` (#20); ToggleItem targets are
``dangling-toggle-item`` (#42); embedded images, including
``Style/BackgroundImage``, are ``dangling-embedded-image``.
"""

from __future__ import annotations

from lxml import etree

from pbirb_mcp.core import names
from pbirb_mcp.core.xpath import find_child, find_children, q
from pbirb_mcp.ops import expr_walk
from pbirb_mcp.ops.lint import Issue, _ancestor_chain


def _parameter_names(root: etree._Element) -> list[str]:
    block = find_child(root, "ReportParameters")
    if block is None:
        return []
    return [p.get("Name") for p in find_children(block, "ReportParameter") if p.get("Name")]


def _reference_issues(doc, collection: str, valid: set, rule: str, what: str, fix: str):
    issues: list[Issue] = []
    seen: set = set()
    for elem, text in expr_walk.iter_expressions(doc.root):
        if collection.lower() not in text.lower():
            continue
        sig = expr_walk.expression_tokens(text)
        if not sig:
            continue
        for ref in expr_walk.references(sig, collection):
            if ref.name in valid:
                continue
            loc = _ancestor_chain(elem)
            if (loc, ref.name) in seen:
                continue
            seen.add((loc, ref.name))
            issues.append(
                {
                    "severity": "error",
                    "rule": rule,
                    "location": loc,
                    "message": f"{text[ref.start : ref.end]} references {what} {ref.name!r}, "
                    "which doesn't exist",
                    "suggestion": fix,
                }
            )
    return issues


def _rule_undefined_parameter(doc) -> list[Issue]:
    return _reference_issues(
        doc,
        "Parameters",
        set(_parameter_names(doc.root)),
        "undefined-parameter",
        "report parameter",
        "add the parameter (add_parameter) or fix the reference; names are case-sensitive",
    )


def _rule_undefined_report_item(doc) -> list[Issue]:
    items = {
        el.get("Name")
        for kind in names.REPORT_ITEM_KINDS
        for el in doc.root.iter(q(kind))
        if el.get("Name")
    }
    return _reference_issues(
        doc,
        "ReportItems",
        items,
        "undefined-report-item",
        "report item",
        "point the reference at an existing textbox (find_textboxes_by_style / "
        "list_body_items) or remove it; names are case-sensitive",
    )


def _is_literal(text) -> bool:
    return bool(text and text.strip()) and not text.lstrip().startswith("=")


def _rule_dangling_bookmark_link(doc) -> list[Issue]:
    literal: set[str] = set()
    for bm in doc.root.iter(q("Bookmark")):
        text = bm.text or ""
        if text.lstrip().startswith("="):
            return []  # an expression bookmark could be any target
        if text.strip():
            literal.add(text.strip())
    issues: list[Issue] = []
    for link in doc.root.iter(q("BookmarkLink")):
        if not _is_literal(link.text) or link.text.strip() in literal:
            continue
        issues.append(
            {
                "severity": "warning",
                "rule": "dangling-bookmark-link",
                "location": _ancestor_chain(link),
                "message": f"BookmarkLink {link.text.strip()!r} matches no <Bookmark> in the report",
                "suggestion": (
                    "give the target item that <Bookmark>, or re-point the action "
                    "(set_textbox_action / set_image_action)"
                ),
            }
        )
    return issues


def _rule_unknown_aggregate_scope(doc) -> list[Issue]:
    index = expr_walk.ScopeIndex(doc.root)
    issues: list[Issue] = []
    seen: set = set()
    for elem, text in expr_walk.iter_expressions(doc.root):
        if "(" not in text:
            continue
        sig = expr_walk.expression_tokens(text)
        if not sig:
            continue
        for call in expr_walk.aggregate_calls(sig):
            if call.scope is None:
                continue
            if call.kind == expr_walk.LOOKUP:
                if call.scope in index.datasets:
                    continue
                kind = "dataset"
            else:
                if index.known(call.scope):
                    continue
                kind = "dataset, data region or group"
            loc = _ancestor_chain(elem)
            if (loc, call.function, call.scope) in seen:
                continue
            seen.add((loc, call.function, call.scope))
            known = sorted(index.datasets)
            issues.append(
                {
                    "severity": "error",
                    "rule": "unknown-aggregate-scope",
                    "location": loc,
                    "message": (
                        f"{call.function}(…) scope {call.scope!r} names no {kind} in the report"
                    ),
                    "suggestion": (
                        f"use an existing {kind} name (datasets: {', '.join(known) or 'none'}); "
                        "scope names are case-sensitive"
                    ),
                }
            )
    return issues


def _rule_dangling_dataset_reference(doc) -> list[Issue]:
    datasets = set(expr_walk.ScopeIndex(doc.root).datasets)
    issues: list[Issue] = []
    for ref in doc.root.iter(q("DataSetName")):
        name = (ref.text or "").strip()
        if name in datasets:
            continue
        owner = ref.getparent()
        owner_tag = etree.QName(owner).localname if owner is not None else "?"
        issues.append(
            {
                "severity": "error",
                "rule": "dangling-dataset-reference",
                "location": _ancestor_chain(ref),
                "message": (
                    f"{owner_tag} DataSetName {name!r} names no <DataSet>"
                    if name
                    else f"{owner_tag} has an empty <DataSetName>"
                ),
                "suggestion": (
                    "point it at an existing dataset "
                    f"({', '.join(sorted(datasets)) or 'none declared'}) or add the dataset"
                ),
            }
        )
    return issues


def _rule_missing_data_source_name(doc) -> list[Issue]:
    block = find_child(doc.root, "DataSets")
    if block is None:
        return []
    issues: list[Issue] = []
    for ds in find_children(block, "DataSet"):
        query = find_child(ds, "Query")
        if query is None:
            continue
        ref = find_child(query, "DataSourceName")
        if ref is not None and (ref.text or "").strip():
            continue
        issues.append(
            {
                "severity": "error",
                "rule": "missing-data-source-name",
                "location": f"DataSet[Name={ds.get('Name')!r}]/Query",
                "message": (
                    f"DataSet {ds.get('Name')!r} has a <Query> without a <DataSourceName>; "
                    "Report Builder can't tell which data source runs it"
                ),
                "suggestion": (
                    "add <DataSourceName> naming a data source (list_data_sources / "
                    "add_data_source)"
                ),
            }
        )
    return issues


def _rule_parameter_forward_dependency(doc) -> list[Issue]:
    # The dependency graph is shared with reorder_parameters' guard (#74).
    edges = expr_walk.parameter_dependency_edges(doc.root)
    issues: list[Issue] = []
    for name, dep, via in expr_walk.forward_dependencies(list(edges), edges):
        self_dep = dep == name
        message = (
            f"parameter {name!r} depends on itself through its {via}"
            if self_dep
            else f"parameter {name!r} ({via}) references {dep!r}, which is declared "
            "after it; Report Builder rejects forward dependencies"
        )
        issues.append(
            {
                "severity": "error",
                "rule": "parameter-forward-dependency",
                "location": f"ReportParameter[Name={name!r}]",
                "message": message,
                "suggestion": (
                    "remove the self-reference from the default / available values"
                    if self_dep
                    else f"move {dep!r} before {name!r} (reorder_parameters)"
                ),
            }
        )
    return issues


def _rule_empty_hyperlink(doc) -> list[Issue]:
    issues: list[Issue] = []
    for link in doc.root.iter(q("Hyperlink")):
        parent = link.getparent()
        if parent is None or etree.QName(parent).localname != "Action":
            continue
        if (link.text or "").strip():
            continue
        issues.append(
            {
                "severity": "warning",
                "rule": "empty-hyperlink",
                "location": _ancestor_chain(link),
                "message": "hyperlink action has an empty <Hyperlink> URL",
                "suggestion": "set the URL (set_textbox_action / set_image_action) or remove the action",
            }
        )
    return issues


RULES = {
    "undefined-parameter": _rule_undefined_parameter,
    "undefined-report-item": _rule_undefined_report_item,
    "dangling-bookmark-link": _rule_dangling_bookmark_link,
    "unknown-aggregate-scope": _rule_unknown_aggregate_scope,
    "dangling-dataset-reference": _rule_dangling_dataset_reference,
    "missing-data-source-name": _rule_missing_data_source_name,
    "parameter-forward-dependency": _rule_parameter_forward_dependency,
    "empty-hyperlink": _rule_empty_hyperlink,
}
