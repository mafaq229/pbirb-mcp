"""RDL expression reference + aggregate emitters (Phase 9).

Two related surfaces:

* :func:`get_expression_reference` — static cheat-sheet of common RDL
  expression patterns shaped ``{category: [{name, syntax, example,
  description}]}``. The intent is that an LLM authoring a textbox
  value or filter expression calls this once instead of guessing —
  and the catalogue explicitly calls out the encoding gotcha (``&``
  not ``&amp;`` for the concat operator).

* :func:`count_where` / :func:`sum_where` / :func:`iif_format` — pure
  string-building emitters for the recurring ``Count(IIf(cond, 1,
  Nothing))`` / ``Sum(IIf(cond, X, Nothing))`` / ``IIf(cond, T, F)``
  patterns. Each returns a complete top-level RDL expression with
  leading ``=``; one leading ``=`` on an argument is dropped (#96). No
  I/O — the user can paste the result into ``set_textbox_value`` or
  pass it to ``add_dataset_filter`` directly.
"""

from __future__ import annotations

from typing import Any, Optional

_REFERENCE: dict[str, list[dict[str, str]]] = {
    "globals": [
        {
            "name": "PageNumber",
            "syntax": "Globals!PageNumber",
            "example": '=Globals!PageNumber & " of " & Globals!TotalPages',
            "description": (
                "Current page number. Only resolves inside <PageHeader> / "
                "<PageFooter> — placing it in the body raises the "
                "page-number-out-of-chrome lint rule."
            ),
        },
        {
            "name": "TotalPages",
            "syntax": "Globals!TotalPages",
            "example": "=Globals!TotalPages",
            "description": "Total page count. Header/footer scope only.",
        },
        {
            "name": "ReportName",
            "syntax": "Globals!ReportName",
            "example": "=Globals!ReportName",
            "description": "Report's display name as set in <Report>.",
        },
        {
            "name": "ExecutionTime",
            "syntax": "Globals!ExecutionTime",
            "example": '=Format(Globals!ExecutionTime, "yyyy-MM-dd HH:mm")',
            "description": "When the report was rendered (DateTime).",
        },
        {
            "name": "UserID",
            "syntax": "User!UserID",
            "example": "=User!UserID",
            "description": "Current user's identity (DOMAIN\\user on AD).",
        },
    ],
    "parameters": [
        {
            "name": "Value",
            "syntax": "Parameters!<Name>.Value",
            "example": "=Parameters!DateFrom.Value",
            "description": (
                "Single value, or first value if multi-value. For "
                "multi-value comparisons use the IN operator or "
                'Join(Parameters!X.Value, ", "); using = directly fires '
                "the multi-value-eq lint rule."
            ),
        },
        {
            "name": "Label",
            "syntax": "Parameters!<Name>.Label",
            "example": "=Parameters!Region.Label",
            "description": (
                "Display label of the selected value: the label of its "
                "available-values entry (a ValidValues <Label>, or the "
                "LabelField of a dataset-backed list). Same as Value when "
                "the parameter has no labels; an array for multi-value."
            ),
        },
        {
            "name": "IsMultiValue",
            "syntax": "Parameters!<Name>.IsMultiValue",
            "example": (
                '=IIf(Parameters!Region.IsMultiValue, Join(Parameters!Region.Value, ", "), '
                "Parameters!Region.Value)"
            ),
            "description": "True when the parameter is declared multi-value.",
        },
        {
            "name": "Count",
            "syntax": "Parameters!<Name>.Count",
            "example": "=Parameters!Region.Count",
            "description": "Number of values selected (multi-value).",
        },
        {
            "name": "Multi-value Join",
            "syntax": 'Join(Parameters!<Name>.Value, ", ")',
            "example": '=Join(Parameters!Region.Value, ", ")',
            "description": "Comma-separated string of selected values.",
        },
    ],
    "fields": [
        {
            "name": "Value",
            "syntax": "Fields!<Name>.Value",
            "example": "=Fields!Amount.Value",
            "description": (
                "Field value in the current scope. Inside aggregates "
                "(Sum, Count, …) the scope is the containing tablix or "
                "group; in textboxes the scope is the row."
            ),
        },
        {
            "name": "IsMissing",
            "syntax": "Fields!<Name>.IsMissing",
            "example": '=IIf(Fields!Email.IsMissing, "n/a", Fields!Email.Value)',
            "description": (
                "True when the field is absent from the dataset's result set "
                "(the query no longer returns that column). It is not a null "
                "test — for a null value use IsNothing(Fields!X.Value)."
            ),
        },
        {
            "name": "IsNothing",
            "syntax": "IsNothing(<expr>)",
            "example": '=IIf(IsNothing(Fields!Email.Value), "n/a", Fields!Email.Value)',
            "description": "True when the value is null (Nothing).",
        },
    ],
    "aggregates": [
        {
            "name": "Sum",
            "syntax": "Sum(Fields!<Name>.Value)",
            "example": "=Sum(Fields!Amount.Value)",
            "description": "Total over the current scope.",
        },
        {
            "name": "Count",
            "syntax": "Count(Fields!<Name>.Value)",
            "example": "=Count(Fields!OrderID.Value)",
            "description": "Count of non-null values in scope.",
        },
        {
            "name": "CountDistinct",
            "syntax": "CountDistinct(Fields!<Name>.Value)",
            "example": "=CountDistinct(Fields!CustomerID.Value)",
            "description": "Number of unique values in scope.",
        },
        {
            "name": "Avg / Min / Max",
            "syntax": "Avg(Fields!<Name>.Value)",
            "example": "=Avg(Fields!Amount.Value)",
            "description": "Standard aggregations; same scope rules as Sum/Count.",
        },
        {
            "name": "First / Last",
            "syntax": "First(Fields!<Name>.Value)",
            "example": "=First(Fields!StatusCode.Value)",
            "description": "First/last value in document order within scope.",
        },
        {
            "name": "Scope argument",
            "syntax": 'Sum(<expr>, "<scope>")',
            "example": '=Sum(Fields!Amount.Value, "MainDataset")',
            "description": (
                "Every aggregate takes an optional scope: a dataset, data "
                "region or group name (in quotes). Required outside a data "
                "region (a body or header textbox) to say which dataset to "
                "aggregate; inside one, a group or region name widens the "
                "scope (e.g. percent of total)."
            ),
        },
        {
            "name": "CountRows",
            "syntax": 'CountRows(["<scope>"])',
            "example": '=CountRows("MainDataset")',
            "description": "Number of rows in scope (nulls included).",
        },
        {
            "name": "RowNumber",
            "syntax": "RowNumber(<scope> | Nothing)",
            "example": "=RowNumber(Nothing)",
            "description": (
                "Running row count within a scope; Nothing means the "
                "outermost data region. Common for alternating row colours."
            ),
        },
        {
            "name": "RunningValue",
            "syntax": 'RunningValue(<expr>, <aggregate>, "<scope>" | Nothing)',
            "example": "=RunningValue(Fields!Amount.Value, Sum, Nothing)",
            "description": "Running aggregate (Sum, Count, Max, …) up to the current row.",
        },
        {
            "name": "InScope",
            "syntax": 'InScope("<scope>")',
            "example": '=IIf(InScope("ProductGroup"), Sum(Fields!Amount.Value), Nothing)',
            "description": (
                "True when the current cell is inside the named group or "
                "data region — tells detail / subtotal cells of a matrix apart."
            ),
        },
        {
            "name": "Conditional Count",
            "syntax": "Count(IIf(<condition>, 1, Nothing))",
            "example": '=Count(IIf(Fields!Status.Value = "Active", 1, Nothing))',
            "description": (
                "Count rows matching a condition: Count skips Nothing, so "
                "non-matching rows don't count (0 when none match). The "
                "count_where emitter builds this."
            ),
        },
        {
            "name": "Conditional Sum",
            "syntax": "Sum(IIf(<condition>, Fields!<X>.Value, Nothing))",
            "example": ('=Sum(IIf(Fields!Status.Value = "Active", Fields!Amount.Value, Nothing))'),
            "description": (
                "Sum X where a condition holds. Use Nothing, not 0, for the "
                "false branch: an Int32 0 mixed with Double / Decimal values "
                "fails ('aggregate function on data of varying data types'); "
                "Sum ignores Nothing (blank when no row matches). The "
                "sum_where emitter builds this."
            ),
        },
    ],
    "lookups": [
        {
            "name": "Lookup",
            "syntax": 'Lookup(<source_expr>, <dest_expr>, <result_expr>, "<DataSet>")',
            "example": (
                '=Lookup(Fields!ProductID.Value, Fields!ID.Value, Fields!Name.Value, "Products")'
            ),
            "description": (
                "Value from another dataset: the first row of <DataSet> "
                "whose dest_expr equals source_expr (evaluated in the "
                "current scope); result_expr is evaluated in <DataSet>. "
                "Nothing when no row matches."
            ),
        },
        {
            "name": "LookupSet",
            "syntax": 'LookupSet(<source_expr>, <dest_expr>, <result_expr>, "<DataSet>")',
            "example": (
                "=Join(LookupSet(Fields!OrderID.Value, Fields!OrderID.Value, "
                'Fields!Item.Value, "Lines"), ", ")'
            ),
            "description": "Like Lookup but returns every matching row's result (an array).",
        },
        {
            "name": "MultiLookup",
            "syntax": 'MultiLookup(<source_array>, <dest_expr>, <result_expr>, "<DataSet>")',
            "example": (
                '=Join(MultiLookup(Split(Fields!Codes.Value, ","), Fields!Code.Value, '
                'Fields!Name.Value, "Codes"), ", ")'
            ),
            "description": "Lookup for each value of an array (e.g. a multi-value parameter).",
        },
    ],
    "conditionals": [
        {
            "name": "IIf",
            "syntax": "IIf(<condition>, <true_value>, <false_value>)",
            "example": '=IIf(Fields!Amount.Value > 0, "Credit", "Debit")',
            "description": (
                "Inline if. Both branches ALWAYS evaluate (VB semantics) "
                "— don't use to guard against div-by-zero etc.; use "
                "IIf(denom = 0, 0, num / denom) plus a wrapper if needed."
            ),
        },
        {
            "name": "Switch",
            "syntax": "Switch(<case1>, <value1>, <case2>, <value2>, true, <default>)",
            "example": (
                '=Switch(Fields!Score.Value >= 90, "A", Fields!Score.Value >= 80, "B", true, "C")'
            ),
            "description": (
                "Multi-branch conditional. Pair the final 'true' with a "
                "default value — Switch returns Nothing if no case "
                "matches, which usually displays as blank."
            ),
        },
    ],
    "strings": [
        {
            "name": "Concatenation",
            "syntax": "<expr1> & <expr2>",
            "example": '="Total: " & Format(Sum(Fields!Amount.Value), "C2")',
            "description": (
                "VB.NET string concatenation. IMPORTANT: pass as `&` in "
                "tool calls — the writer XML-encodes once to `&amp;` on "
                "save, which SSRS decodes back to `&` for the VB parser. "
                "Pre-encoding to `&amp;` produces `&amp;amp;` on disk and "
                "fails with BC30451 ''amp' is not declared'."
            ),
        },
        {
            "name": "Format",
            "syntax": 'Format(<value>, "<fmt>")',
            "example": '=Format(Sum(Fields!Amount.Value), "C2")',
            "description": (
                'Format strings: numbers ("N2", "C2", "P0"), dates '
                '("MMM, yyyy", "yyyy-MM-dd", "d"), custom .NET formats.'
            ),
        },
        {
            "name": "Trim / UCase / LCase",
            "syntax": "Trim(<s>) / UCase(<s>) / LCase(<s>)",
            "example": "=UCase(Trim(Fields!Name.Value))",
            "description": "Whitespace + case helpers.",
        },
        {
            "name": "Mid / Left / Right",
            "syntax": "Mid(<s>, <start>, <length>)",
            "example": "=Mid(Fields!SKU.Value, 1, 3)",
            "description": "Substring (1-based start in VB.NET, not 0-based).",
        },
        {
            "name": "Replace",
            "syntax": "Replace(<s>, <find>, <replace>)",
            "example": '=Replace(Fields!Phone.Value, "-", "")',
            "description": "Literal string replace.",
        },
        {
            "name": "Len",
            "syntax": "Len(<s>)",
            "example": "=Len(Fields!Notes.Value)",
            "description": "String length.",
        },
    ],
    "dates": [
        {
            "name": "Today / Now",
            "syntax": "Today() / Now()",
            "example": '=Format(Today(), "yyyy-MM-dd")',
            "description": "Today() is Date (no time); Now() is DateTime.",
        },
        {
            "name": "DateAdd",
            "syntax": 'DateAdd("<interval>", <number>, <date>)',
            "example": '=DateAdd("d", -7, Today())',
            "description": (
                'Add N units to a date. Intervals: "yyyy" (year), "q" '
                '(quarter), "m" (month), "d" (day), "h" (hour), '
                '"n" (minute), "s" (second).'
            ),
        },
        {
            "name": "DateDiff",
            "syntax": 'DateDiff("<interval>", <date1>, <date2>)',
            "example": '=DateDiff("d", Fields!StartDate.Value, Today())',
            "description": "Difference between dates in interval units.",
        },
        {
            "name": "Year / Month / Day",
            "syntax": "Year(<date>) / Month(<date>) / Day(<date>)",
            "example": "=Year(Fields!OrderDate.Value)",
            "description": "Date-component accessors.",
        },
    ],
}


def get_expression_reference() -> dict[str, Any]:
    """Return a static catalogue of common RDL expression patterns.

    Categories: globals, parameters, fields, aggregates, lookups,
    conditionals, strings, dates. Each entry is ``{name, syntax, example,
    description}``. Use this as the first lookup when authoring a
    textbox value or filter expression — saves a round-trip through
    "what's the right RDL syntax for X?" and explicitly calls out the
    encoding gotcha for the ``&`` concat operator.
    """
    # Defensive copy so callers can't mutate the catalogue.
    return {cat: [dict(entry) for entry in entries] for cat, entries in _REFERENCE.items()}


# ---- aggregate-expression emitters ------------------------------------


def _body(label: str, value: object) -> str:
    """An argument as an expression body: one leading ``=`` (after
    whitespace) is dropped (#96) — callers routinely pass ``=Fields!…``,
    which pasted into ``IIf(`` gave an invalid ``IIf(=Fields!…``."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("="):
            text = text[1:].strip()
        if text:
            return text
    raise ValueError(f"{label} must be a non-empty expression body")


def _scope_arg(scope: Optional[str]) -> str:
    """``, "<scope>"`` (quotes doubled) for an aggregate, or ``""``."""
    if scope is None:
        return ""
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope must be a non-empty dataset, data region or group name")
    return ', "' + scope.strip().replace('"', '""') + '"'


def count_where(condition: str, scope: Optional[str] = None) -> str:
    """Emit ``=Count(IIf(<condition>, 1, Nothing))`` — count the rows
    matching a condition.

    ``Count`` skips ``Nothing``, so non-matching rows don't count, and
    the result is 0 (not blank) when none match. (The older
    ``Sum(IIf(cond, 1, 0))`` idiom is equivalent; ``Count`` keeps the
    two emitters on the same Nothing-based pattern.)

    ``condition`` is an RDL expression body; one leading ``=`` is
    dropped. ``scope`` (a dataset, data region or group name) is passed
    as the aggregate's scope argument — needed outside a data region.

    Returns a complete top-level RDL expression with leading ``=``
    suitable for ``set_textbox_value``, a tablix cell ``<Value>``,
    or any other RDL expression sink.
    """
    cond = _body("condition", condition)
    return f"=Count(IIf({cond}, 1, Nothing){_scope_arg(scope)})"


def sum_where(field_expression: str, condition: str, scope: Optional[str] = None) -> str:
    """Emit ``=Sum(IIf(<condition>, <field_expression>, Nothing))`` — sum
    a value where a condition holds.

    The false branch is ``Nothing``, not ``0`` (#96): an Int32 ``0``
    mixed with ``System.Double`` / ``Decimal`` values (every Power BI
    numeric column) fails with "aggregate function on data of varying
    data types". ``Sum`` ignores Nothing; it is blank when no row
    matches.

    ``field_expression`` is the value to sum (typically
    ``Fields!X.Value``) and ``condition`` any RDL expression body; one
    leading ``=`` on either is dropped. ``scope`` is the aggregate's
    scope argument, as in :func:`count_where`.
    """
    value = _body("field_expression", field_expression)
    cond = _body("condition", condition)
    return f"=Sum(IIf({cond}, {value}, Nothing){_scope_arg(scope)})"


def iif_format(condition: str, true_value: str, false_value: str) -> str:
    """Emit ``=IIf(<condition>, <true_value>, <false_value>)``.

    All three arguments are RDL expression bodies; one leading ``=`` on
    each is dropped. String literals must already be quoted, e.g.
    ``iif_format("Fields!Active.Value", '"Yes"', '"No"')``.
    """
    cond = _body("condition", condition)
    t = _body("true_value", true_value)
    f = _body("false_value", false_value)
    return f"=IIf({cond}, {t}, {f})"


__all__ = [
    "get_expression_reference",
    "count_where",
    "sum_where",
    "iif_format",
]
