"""Query-parameter binding rules (#95).

A Power BI (PBIDATASET / XMLA) dataset's DAX references parameters as
``@Name``; each needs a ``<QueryParameter Name="Name">`` binding that
supplies its value. Nothing relates the two when either side is edited,
and Report Builder only notices at preview time.

* ``query-parameter-mismatch`` — (error) an ``@Name`` in the DAX
  ``<CommandText>`` with no ``<QueryParameter>`` (Report Builder: "The
  query contains the 'Name' parameter, which is not declared"); (warning)
  a ``<QueryParameter>`` the DAX never uses. A ``Parameters!X`` in a
  binding's value that names no report parameter is
  ``undefined-parameter``'s job (:mod:`pbirb_mcp.ops.lint_rules.references`).
* ``multi-value-query-binding`` (warning) — a multi-value report
  parameter passed raw (``=Parameters!X.Value``) into a binding whose
  ``@Name`` the DAX uses outside ``RSCustomDaxFilter(...)``: a scalar
  comparison such as ``[col] = @X`` sees one value. Join the values
  (``=Join(Parameters!X.Value, "|")`` with ``PATHCONTAINS``) or filter
  with ``RSCustomDaxFilter``.

Only Power BI datasets are checked (SQL / MDX sources have their own
``@`` conventions and pass multi-value parameters to ``IN (@X)``), and
only literal command text: a ``=``-expression query is skipped. ``@Name``
tokens come from :func:`pbirb_mcp.ops.dax.parameter_uses`, so names in
strings and comments don't count; names compare case-insensitively and a
binding's leading ``@`` is ignored (that form is ``pbidataset-at-prefix``).

:func:`binding_warnings` gives the findings about one binding, as the
``warnings`` of add/update/remove_query_parameter.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from lxml import etree

from pbirb_mcp.core.xpath import find_child, find_children
from pbirb_mcp.ops import dax, expr_walk
from pbirb_mcp.ops.dataset import _is_pbidataset_dataset
from pbirb_mcp.ops.lint import Issue, _bare_reference, _multi_value_param_names

# Calls that take a multi-value parameter as a list.
_LIST_CALLS = frozenset({"RSCUSTOMDAXFILTER"})


class _Binding(NamedTuple):
    element: etree._Element
    name: str  # as written
    key: str  # bare, case-folded
    value: str


def _key(name: str) -> str:
    return name.lstrip("@").casefold()


def _dax_text(query: etree._Element) -> Optional[str]:
    cmd = find_child(query, "CommandText")
    text = cmd.text if cmd is not None else None
    if not text or not text.strip() or text.lstrip().startswith("="):
        return None
    return text


def _bindings(query: etree._Element) -> list[_Binding]:
    block = find_child(query, "QueryParameters")
    out: list[_Binding] = []
    for qp in find_children(block, "QueryParameter") if block is not None else []:
        name = qp.get("Name") or ""
        value = find_child(qp, "Value")
        out.append(_Binding(qp, name, _key(name), (value.text or "") if value is not None else ""))
    return out


def _checked_query(doc, dataset: etree._Element) -> Optional[tuple[etree._Element, str]]:
    query = find_child(dataset, "Query")
    if query is None or not _is_pbidataset_dataset(doc, dataset):
        return None
    text = _dax_text(query)
    return (query, text) if text is not None else None


def _qp_locator(ds_name: str, qp_name: str) -> str:
    return f"DataSet[Name={ds_name!r}]/Query/QueryParameters/QueryParameter[Name={qp_name!r}]"


def _raw_multi_value(binding: _Binding, multi: set[str]) -> Optional[str]:
    """The multi-value parameter a binding passes raw, else ``None``."""
    sig = expr_walk.expression_tokens(binding.value)
    if not sig:
        return None
    ref = _bare_reference(sig)
    if ref is None or ref.name not in multi or (ref.member or "").lower() != "value":
        return None
    return ref.name


def _scalar_uses(uses: list[dax.ParamUse], key: str) -> list[dax.ParamUse]:
    return [
        u for u in uses if u.name.casefold() == key and not any(c in _LIST_CALLS for c in u.calls)
    ]


def _unbound_issue(ds_name: str, name: str) -> Issue:
    return {
        "severity": "error",
        "rule": "query-parameter-mismatch",
        "location": f"DataSet[Name={ds_name!r}]/Query/CommandText",
        "message": (
            f"the DAX uses @{name} but dataset {ds_name!r} has no QueryParameter "
            f"{name!r}; Report Builder fails with 'The query contains the "
            f"'{name}' parameter, which is not declared'"
        ),
        "suggestion": (
            f"add_query_parameter(dataset_name={ds_name!r}, name={name!r}, "
            f"value_expression='=Parameters!{name}.Value'), or drop @{name} from the DAX"
        ),
    }


def _unused_issue(ds_name: str, b: _Binding) -> Issue:
    return {
        "severity": "warning",
        "rule": "query-parameter-mismatch",
        "location": _qp_locator(ds_name, b.name),
        "message": (
            f"QueryParameter {b.name!r} of dataset {ds_name!r} is not used by the DAX "
            f"(no @{b.name.lstrip('@')})"
        ),
        "suggestion": "remove it (remove_query_parameter) or reference it in the DAX",
    }


def _multi_value_issue(ds_name: str, b: _Binding, param: str) -> Issue:
    bare = b.name.lstrip("@")
    return {
        "severity": "warning",
        "rule": "multi-value-query-binding",
        "location": _qp_locator(ds_name, b.name),
        "message": (
            f"multi-value parameter {param!r} is passed raw into QueryParameter "
            f"{b.name!r}, and the DAX uses @{bare} as a scalar; only one value "
            "reaches the query"
        ),
        "suggestion": (
            f'bind =Join(Parameters!{param}.Value, "|") and test with '
            f"PATHCONTAINS(@{bare}, [Column]), or filter with RSCustomDaxFilter"
        ),
    }


def _dataset_findings(doc, dataset: etree._Element, multi: set[str]) -> list[tuple[str, Issue]]:
    """``(parameter key, issue)`` pairs for one dataset; the key (bare,
    case-folded name) says which binding / ``@name`` the issue is about."""
    checked = _checked_query(doc, dataset)
    if checked is None:
        return []
    query, text = checked
    ds_name = dataset.get("Name") or ""
    uses = dax.parameter_uses(text)
    used = {u.name.casefold() for u in uses}
    bindings = _bindings(query)
    bound = {b.key for b in bindings}
    out: list[tuple[str, Issue]] = [
        (name.casefold(), _unbound_issue(ds_name, name))
        for name in dax.parameter_names(text)
        if name.casefold() not in bound
    ]
    for b in bindings:
        if b.key and b.key not in used:
            out.append((b.key, _unused_issue(ds_name, b)))
            continue
        param = _raw_multi_value(b, multi)
        if param is not None and _scalar_uses(uses, b.key):
            out.append((b.key, _multi_value_issue(ds_name, b, param)))
    return out


def _datasets(doc) -> list[etree._Element]:
    block = find_child(doc.root, "DataSets")
    return list(find_children(block, "DataSet")) if block is not None else []


def _rule(rule: str):
    def run(doc) -> list[Issue]:
        multi = _multi_value_param_names(doc.root)
        return [
            issue
            for ds in _datasets(doc)
            for _k, issue in _dataset_findings(doc, ds, multi)
            if issue["rule"] == rule
        ]

    run.__name__ = "_rule_" + rule.replace("-", "_")
    return run


def binding_warnings(doc, dataset: etree._Element, name: str) -> list[str]:
    """The findings of both rules about binding / ``@name`` ``name`` of
    ``dataset``, as warning strings, plus ``Parameters!X`` references in
    that binding's value that name no report parameter — the ``warnings``
    of add/update/remove_query_parameter."""
    key = _key(name)
    multi = _multi_value_param_names(doc.root)
    out = [
        f"{issue['rule']}: {issue['message']}"
        for k, issue in _dataset_findings(doc, dataset, multi)
        if k == key
    ]
    block = find_child(doc.root, "ReportParameters")
    declared = (
        {p.get("Name") for p in find_children(block, "ReportParameter")}
        if block is not None
        else set()
    )
    query = find_child(dataset, "Query")
    for b in _bindings(query) if query is not None else []:
        if b.key != key:
            continue
        sig = expr_walk.expression_tokens(b.value) or []
        for ref in expr_walk.references(sig, "Parameters"):
            if ref.name not in declared:
                out.append(
                    f"undefined-parameter: QueryParameter {b.name!r} of dataset "
                    f"{dataset.get('Name')!r} references report parameter {ref.name!r}, "
                    "which doesn't exist"
                )
    return out


RULES = {
    "query-parameter-mismatch": _rule("query-parameter-mismatch"),
    "multi-value-query-binding": _rule("multi-value-query-binding"),
}
