"""Dataset-scoped expression rules (#46; #45 items 1-2).

* ``field-out-of-scope`` (error) — a field reference (``Fields!X.<any
  member>`` or ``Fields("X")``) in any expression — tablix cells, chart
  ``<Y>``/``<X>``/labels, styles, actions, filters, … — that does not
  resolve in the dataset the expression is evaluated against:

  - inside a data region (Tablix, Chart, GaugePanel, Map data region,
    CustomReportItem) the region's ``DataSetName``; inside a
    ``<DataSet>`` (calculated fields, dataset filters) that dataset;
  - inside an aggregate with an explicit scope (``Sum(…, "DS")``, a
    group or data-region name) the dataset that scope reads; inside
    ``Lookup``/``LookupSet``/``MultiLookup`` the destination and result
    expressions use the lookup's dataset argument;
  - outside any data region (body, page header/footer, parameters) a
    field may only appear inside an aggregate — with an explicit scope,
    or without one when the report has exactly one dataset.

  Fields no dataset declares are ``missing-field-reference``'s job;
  unknown scope names are ``unknown-aggregate-scope``'s, dangling
  ``DataSetName`` values ``dangling-dataset-reference``'s — this rule
  stays silent on those so each defect is reported once.
"""

from __future__ import annotations

from pbirb_mcp.ops import expr_walk
from pbirb_mcp.ops.lint import Issue, _ancestor_chain


def _rule_field_out_of_scope(doc) -> list[Issue]:
    index = expr_walk.ScopeIndex(doc.root)
    if not index.datasets:
        return []
    only_dataset = next(iter(index.datasets)) if len(index.datasets) == 1 else None
    issues: list[Issue] = []
    seen: set = set()
    for elem, text in expr_walk.iter_expressions(doc.root):
        if "fields" not in text.lower():
            continue
        sig = expr_walk.expression_tokens(text)
        if not sig:
            continue
        refs = [r for r in expr_walk.references(sig, "Fields") if r.name in index.all_fields]
        if not refs:
            continue
        calls = expr_walk.aggregate_calls(sig)
        region_ds = expr_walk.dataset_scope(elem)
        for ref in refs:
            enclosing = expr_walk.enclosing_calls(calls, ref.first)
            explicit = None
            unresolvable = False
            for call in enclosing:  # innermost first
                if call.scope is not None:
                    explicit = call
                    break
                if call.kind == expr_walk.LOOKUP:
                    # A Lookup whose dataset isn't a string literal can't
                    # be resolved statically.
                    unresolvable = True
                    break
            if unresolvable:
                continue
            problem = None
            if explicit is not None:
                ds = index.resolve(explicit.scope)
                if ds is None or ds not in index.datasets:
                    continue  # unknown scope / dangling region dataset
                if ref.name not in index.datasets[ds]:
                    problem = (
                        f"{text[ref.start : ref.end]} is evaluated in scope "
                        f"{explicit.scope!r} (dataset {ds!r}), which has no field {ref.name!r}"
                    )
            elif region_ds is not None:
                if region_ds not in index.datasets:
                    continue  # dangling-dataset-reference reports it
                if ref.name not in index.datasets[region_ds]:
                    problem = (
                        f"{text[ref.start : ref.end]} is not a field of dataset "
                        f"{region_ds!r}, which this expression is evaluated against"
                    )
            elif enclosing:
                if only_dataset is None:
                    problem = (
                        f"{text[ref.start : ref.end]} is aggregated outside any data region "
                        f"without a scope, and the report has {len(index.datasets)} datasets"
                    )
                elif ref.name not in index.datasets[only_dataset]:
                    problem = (
                        f"{text[ref.start : ref.end]} is not a field of dataset {only_dataset!r}"
                    )
            else:
                problem = (
                    f"{text[ref.start : ref.end]} is used outside any data region; a field "
                    "there must be inside an aggregate with a dataset scope"
                )
            if problem is None:
                continue
            loc = _ancestor_chain(elem)
            if (loc, ref.name) in seen:
                continue
            seen.add((loc, ref.name))
            issues.append(
                {
                    "severity": "error",
                    "rule": "field-out-of-scope",
                    "location": loc,
                    "message": problem,
                    "suggestion": (
                        "reference a field of the dataset in scope, wrap it in an aggregate "
                        'with an explicit dataset scope (e.g. First(Fields!X.Value, "DataSet")) '
                        "or Lookup(), or bind the data region to the dataset that has it"
                    ),
                }
            )
    return issues


RULES = {"field-out-of-scope": _rule_field_out_of_scope}
