"""RDL expression reference + aggregate emitters (Phase 9).

Two related surfaces:

* :func:`get_expression_reference` — static cheat-sheet of common RDL
  expression patterns shaped ``{category: [{name, syntax, example,
  description}]}``. The intent is that an LLM authoring a textbox
  value or filter expression calls this once instead of guessing —
  and the catalogue explicitly calls out the encoding gotcha (``&``
  not ``&amp;`` for the concat operator).

* :func:`count_where` / :func:`sum_where` / :func:`iif_format` — pure
  string-building emitters for the recurring ``Sum(IIf(cond, 1, 0))``
  / ``Sum(IIf(cond, X, 0))`` / ``IIf(cond, T, F)`` patterns. Each
  returns a complete top-level RDL expression with leading ``=``.
  No I/O, no validation — the user can paste the result into
  ``set_textbox_value`` or pass it to ``add_dataset_filter`` directly.
"""

from __future__ import annotations

from typing import Any


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
                "Join(Parameters!X.Value, \", \"); using = directly fires "
                "the multi-value-eq lint rule."
            ),
        },
        {
            "name": "Label",
            "syntax": "Parameters!<Name>.Label",
            "example": "=Parameters!Region.Label",
            "description": (
                "Human-readable label (when LabelExpression is set on "
                "the parameter). Falls back to Value otherwise."
            ),
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
            "description": "True when the dataset returned no value for the field.",
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
            "name": "Conditional Count",
            "syntax": "Sum(IIf(<condition>, 1, 0))",
            "example": '=Sum(IIf(Fields!Status.Value = "Active", 1, 0))',
            "description": (
                "SSRS idiom for 'count rows matching condition'. The "
                "count_where emitter (Phase 9 commit 38) builds this for you."
            ),
        },
        {
            "name": "Conditional Sum",
            "syntax": "Sum(IIf(<condition>, Fields!<X>.Value, 0))",
            "example": '=Sum(IIf(Fields!Status.Value = "Active", Fields!Amount.Value, 0))',
            "description": (
                "SSRS idiom for 'sum X where condition'. The sum_where "
                "emitter (Phase 9 commit 38) builds this for you."
            ),
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
                '=Switch('
                'Fields!Score.Value >= 90, "A", '
                'Fields!Score.Value >= 80, "B", '
                'true, "C")'
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

    Categories: globals, parameters, fields, aggregates, conditionals,
    strings, dates. Each entry is ``{name, syntax, example,
    description}``. Use this as the first lookup when authoring a
    textbox value or filter expression — saves a round-trip through
    "what's the right RDL syntax for X?" and explicitly calls out the
    encoding gotcha for the ``&`` concat operator.
    """
    # Defensive copy so callers can't mutate the catalogue.
    return {cat: [dict(entry) for entry in entries] for cat, entries in _REFERENCE.items()}


# ---- aggregate-expression emitters (Phase 9 commit 38) ------------------


def count_where(condition: str) -> str:
    """Emit ``=Sum(IIf(<condition>, 1, 0))`` — the SSRS conditional
    count idiom.

    No field argument: the count-rows-matching-condition pattern
    doesn't reference a field directly. ``condition`` is any RDL
    expression body (no leading ``=``).

    Returns a complete top-level RDL expression with leading ``=``
    suitable for ``set_textbox_value``, a tablix cell ``<Value>``,
    or any other RDL expression sink.
    """
    if not isinstance(condition, str) or not condition.strip():
        raise ValueError("condition must be a non-empty expression body")
    return f"=Sum(IIf({condition}, 1, 0))"


def sum_where(field_expression: str, condition: str) -> str:
    """Emit ``=Sum(IIf(<condition>, <field_expression>, 0))`` — the
    SSRS conditional sum idiom.

    ``field_expression`` is the value to sum (typically
    ``Fields!X.Value`` or an expression body). ``condition`` is any
    RDL expression body (no leading ``=``).
    """
    if not isinstance(field_expression, str) or not field_expression.strip():
        raise ValueError("field_expression must be a non-empty expression body")
    if not isinstance(condition, str) or not condition.strip():
        raise ValueError("condition must be a non-empty expression body")
    return f"=Sum(IIf({condition}, {field_expression}, 0))"


def iif_format(condition: str, true_value: str, false_value: str) -> str:
    """Emit ``=IIf(<condition>, <true_value>, <false_value>)``.

    All three arguments are RDL expression bodies (no leading ``=``).
    String literals must already be quoted, e.g.
    ``iif_format("Fields!Active.Value", '"Yes"', '"No"')``.
    """
    for label, val in (
        ("condition", condition),
        ("true_value", true_value),
        ("false_value", false_value),
    ):
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{label} must be a non-empty expression body")
    return f"=IIf({condition}, {true_value}, {false_value})"


__all__ = [
    "get_expression_reference",
    "count_where",
    "sum_where",
    "iif_format",
]
