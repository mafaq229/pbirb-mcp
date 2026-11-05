"""Filter expression helpers (Phase 8 commit 36).

Helpers shared between :func:`pbirb_mcp.ops.tablix.add_tablix_filter`
and :func:`pbirb_mcp.ops.dataset.add_dataset_filter`:

* :func:`wrap_with_format` — wrap the body of a filter expression in
  ``Format(<body>, "<field_format>")``. Closes the date-vs-string
  mismatch class without forcing the user to hand-edit the
  ``<FilterExpression>`` after authoring.
* :func:`type_mismatch_warnings` — best-effort cross-check between a
  field's ``<rd:TypeName>`` (read from the bound dataset's ``<Field>``
  block) and a parameter's ``<DataType>`` (read from
  ``<ReportParameters>``). Emits a structured warning string per
  detected mismatch. Skips silently when either side isn't readable.
* :func:`encode_filter_texts` — the text actually written to
  ``<FilterExpression>``/``<FilterValue>``: literals verbatim,
  ``=``-expressions entity-decoded with a warning (issue #75).

The helpers are intentionally small: regex-based detection (no DAX
parser), one-pass scan, and only the type pairings that recur in real
session feedback (date-vs-string is the only documented case in v0.2).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.encoding import encode_value_text
from pbirb_mcp.core.xpath import RD_NS, find_child, find_children, q
from pbirb_mcp.ops import vbexpr

# Maps RDL parameter <DataType> to the canonical "shape group". An
# rd:TypeName is bucketed via :func:`_canonical_typename_group`, then
# the two groups are compared. Mismatch → warning.
_PARAM_TYPE_GROUP = {
    "DateTime": "datetime",
    "String": "string",
    "Integer": "integer",
    "Float": "number",
    "Boolean": "boolean",
}


def _canonical_typename_group(rd_typename: str) -> Optional[str]:
    """Map an rd:TypeName like ``System.DateTime`` to a shape group.

    Returns ``None`` for unrecognised types — silence is preferable to
    a noisy false positive.
    """
    t = (rd_typename or "").strip()
    if not t:
        return None
    # Strip the System. prefix; tolerate bare names too.
    bare = t.split(".")[-1]
    bare_l = bare.lower()
    if bare_l == "datetime":
        return "datetime"
    if bare_l == "string":
        return "string"
    if bare_l in (
        "int16",
        "int32",
        "int64",
        "byte",
        "sbyte",
        "uint16",
        "uint32",
        "uint64",
    ):
        return "integer"
    if bare_l in ("double", "single", "decimal"):
        return "number"
    if bare_l == "boolean":
        return "boolean"
    return None


# RDL parameter reference: Parameters!<name>.Value (or .Label)
_PARAM_REF_RE = re.compile(r"Parameters!(\w+)\.(?:Value|Label)")

# VB functions whose result is a String: a field inside one compares as
# a string, whatever its rd:TypeName.
_STRING_COERCIONS = frozenset(
    {
        "format",
        "format$",
        "cstr",
        "formatdatetime",
        "formatnumber",
        "formatcurrency",
        "formatpercent",
    }
)

# Field shape group → FilterValue DataType that makes a literal compare
# with the field's type (#94). Only numeric and date fields: a literal
# is already a String.
_SUGGESTED_DATA_TYPE = {
    "integer": "Integer",
    "number": "Float",
    "datetime": "DateTime",
}


# ---- operators, arity, FilterValue DataType (#94) ------------------------

# RDL 2016 FilterOperator enumeration, in the order the docs list it.
FILTER_OPERATORS: tuple[str, ...] = (
    "Equal",
    "NotEqual",
    "GreaterThan",
    "GreaterThanOrEqual",
    "LessThan",
    "LessThanOrEqual",
    "Like",
    "In",
    "Between",
    "TopN",
    "BottomN",
    "TopPercent",
    "BottomPercent",
)

# Operators whose single value is a count / percentage, not a comparand.
_COUNT_OPERATORS = frozenset({"TopN", "BottomN", "TopPercent", "BottomPercent"})

# ``<FilterValue DataType=...>`` (XSD StringWithDataTypeAttribute).
FILTER_VALUE_DATA_TYPES: tuple[str, ...] = ("Boolean", "DateTime", "Integer", "Float", "String")

_INT_LITERAL_RE = re.compile(r"\s*[+-]?\d+\s*")
_NUMBER_LITERAL_RE = re.compile(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*")


def _is_expression(value: str) -> bool:
    return value.lstrip().startswith("=")


def validate_filter(
    operator: str,
    values: list[str],
    value_data_type: Optional[str] = None,
) -> None:
    """Raise ``ValueError`` unless ``operator`` / ``values`` /
    ``value_data_type`` make a filter Report Builder can evaluate (#94).

    Arity: ``Between`` takes exactly 2 values, ``In`` one or more, every
    other operator exactly 1. ``TopN`` / ``BottomN`` take an integer
    literal or an ``=``-expression, ``TopPercent`` / ``BottomPercent`` a
    number or an expression. A ``value_data_type`` of Integer / Float /
    Boolean must match the literal values it types.
    """
    if operator not in FILTER_OPERATORS:
        hint = ""
        for op in FILTER_OPERATORS:
            if op.lower() == (operator or "").lower():
                hint = f" (did you mean {op!r}? operators are case-sensitive)"
                break
        raise ValueError(
            f"unknown filter operator {operator!r}{hint}; valid operators are: "
            f"{list(FILTER_OPERATORS)}"
        )
    if not values:
        raise ValueError("at least one filter value is required")
    n = len(values)
    if operator == "Between":
        if n != 2:
            raise ValueError(f"operator 'Between' takes exactly 2 values (low, high); got {n}")
    elif operator != "In" and n != 1:
        more = " — use 'In' to match any of several values" if operator == "Equal" else ""
        raise ValueError(f"operator {operator!r} takes exactly 1 value; got {n}{more}")
    if operator in _COUNT_OPERATORS and not _is_expression(values[0]):
        pattern = _INT_LITERAL_RE if operator in ("TopN", "BottomN") else _NUMBER_LITERAL_RE
        if not pattern.fullmatch(values[0]):
            kind = "an integer" if pattern is _INT_LITERAL_RE else "a number"
            raise ValueError(
                f"operator {operator!r} takes {kind} literal or an '=' expression "
                f"as its value; got {values[0]!r}"
            )
    if value_data_type is None:
        return
    if value_data_type not in FILTER_VALUE_DATA_TYPES:
        raise ValueError(
            f"unknown value_data_type {value_data_type!r}; valid: {list(FILTER_VALUE_DATA_TYPES)}"
        )
    for v in values:
        if _is_expression(v):
            continue
        ok = True
        if value_data_type == "Integer":
            ok = bool(_INT_LITERAL_RE.fullmatch(v))
        elif value_data_type == "Float":
            ok = bool(_NUMBER_LITERAL_RE.fullmatch(v))
        elif value_data_type == "Boolean":
            ok = v.strip().lower() in ("true", "false")
        if not ok:
            raise ValueError(f"filter value {v!r} is not a valid {value_data_type} literal")


def write_filter(
    filters_root: etree._Element,
    expression: str,
    operator: str,
    values: list[str],
    value_data_type: Optional[str] = None,
) -> etree._Element:
    """Append ``<Filter>`` (expression, operator, values) to a
    ``<Filters>`` block. ``value_data_type`` becomes the ``DataType``
    attribute of every ``<FilterValue>``. Texts are written as given —
    callers pass them through :func:`encode_filter_texts` first."""
    filter_node = etree.SubElement(filters_root, q("Filter"))
    etree.SubElement(filter_node, q("FilterExpression")).text = expression
    etree.SubElement(filter_node, q("Operator")).text = operator
    values_root = etree.SubElement(filter_node, q("FilterValues"))
    for v in values:
        v_node = etree.SubElement(values_root, q("FilterValue"))
        if value_data_type is not None:
            v_node.set("DataType", value_data_type)
        v_node.text = v
    return filter_node


def filter_to_dict(filter_node: etree._Element) -> dict[str, Any]:
    """Read-back shape for one ``<Filter>``: expression, operator, values
    and ``data_type`` (the ``<FilterValue DataType>``, ``None`` when
    unset; the first one found when values disagree)."""
    expr = find_child(filter_node, "FilterExpression")
    op = find_child(filter_node, "Operator")
    values_root = find_child(filter_node, "FilterValues")
    values: list[str] = []
    data_type: Optional[str] = None
    if values_root is not None:
        for v in find_children(values_root, "FilterValue"):
            values.append(v.text or "")
            if data_type is None and v.get("DataType"):
                data_type = v.get("DataType")
    return {
        "expression": expr.text if expr is not None else None,
        "operator": op.text if op is not None else None,
        "values": values,
        "data_type": data_type,
    }


def wrap_with_format(expression: str, field_format: str) -> str:
    """Wrap a filter expression's body in ``Format(<body>, "<format>")``.

    Preserves the leading ``=`` if present. Idempotent in spirit —
    callers shouldn't call this twice — but this helper doesn't try to
    detect double-wrap; that's the caller's contract.
    """
    body = expression.lstrip()
    leading_eq = ""
    if body.startswith("="):
        leading_eq = "="
        body = body[1:]
    # Escape embedded double quotes in the format string. RDL formats
    # rarely contain quotes, but be defensive.
    safe_format = field_format.replace('"', '""')
    return f'{leading_eq}Format({body}, "{safe_format}")'


def _resolve_field_typename(dataset: etree._Element, field_name: str) -> Optional[str]:
    """Return the ``rd:TypeName`` text for a named ``<Field>`` in the
    dataset, or ``None`` if not found / not declared."""
    fields_block = find_child(dataset, "Fields")
    if fields_block is None:
        return None
    for f in find_children(fields_block, "Field"):
        if f.get("Name") != field_name:
            continue
        tn = f.find(f"{{{RD_NS}}}TypeName")
        return tn.text if tn is not None else None
    return None


def _resolve_parameter_datatype(root: etree._Element, param_name: str) -> Optional[str]:
    """Return the ``<DataType>`` text for a named ``<ReportParameter>``,
    or ``None`` if not declared."""
    params_block = find_child(root, "ReportParameters")
    if params_block is None:
        return None
    for p in find_children(params_block, "ReportParameter"):
        if p.get("Name") != param_name:
            continue
        dt = find_child(p, "DataType")
        return dt.text if dt is not None else None
    return None


def _uncoerced_field_refs(expression: str) -> list[str]:
    """Names of the ``Fields!X.Value`` references in ``expression`` that
    reach the comparison with their own type.

    A reference inside ``Format(`` / ``CStr(`` (or another string
    conversion), or followed by ``.ToString``, compares as a String, so a
    field/value type check on it would be a false positive (#94 — the
    ``field_format`` wrapper itself produced that warning). References
    inside string literals and comments are never reported.
    """
    text = expression or ""
    tokens = vbexpr.tokenize_expression(text)
    if tokens is None:
        tokens = vbexpr.tokenize(text)
    sig = vbexpr.significant(tokens)
    # For each significant token index: is it inside a coercing call?
    coerced_at: list[bool] = []
    stack: list[bool] = []
    for i, tok in enumerate(sig):
        if tok.kind == vbexpr.DELIMITER and tok.text == "(":
            prev = sig[i - 1] if i > 0 else None
            stack.append(
                prev is not None
                and prev.kind == vbexpr.IDENTIFIER
                and prev.text.lower() in _STRING_COERCIONS
            )
        elif tok.kind == vbexpr.DELIMITER and tok.text == ")" and stack:
            stack.pop()
        coerced_at.append(any(stack))
    out: list[str] = []
    for ref in vbexpr.bang_references(sig, "Fields"):
        if (ref.member or "").lower() != "value":
            continue
        if coerced_at[ref.first]:
            continue
        nxt = ref.last + 1
        if (
            nxt + 1 < len(sig)
            and sig[nxt].text == "."
            and sig[nxt + 1].kind == vbexpr.IDENTIFIER
            and sig[nxt + 1].text.lower() == "tostring"
        ):
            continue
        out.append(ref.name)
    return out


def type_mismatch_warnings(
    root: etree._Element,
    dataset: etree._Element,
    expression: str,
    values: list[str],
    *,
    operator: Optional[str] = None,
    value_data_type: Optional[str] = None,
) -> list[str]:
    """Cross-check field types vs the filter values. One warning per
    detected mismatch. Skips silently when either side is unreadable.

    * every uncoerced ``Fields!X.Value`` in ``expression`` is paired
      with every ``Parameters!Y.Value`` in ``values``; differing type
      groups warn. Loose by design — a single value is the common case.
    * a numeric / date field compared with bare literal values (no
      leading ``=``) and no ``value_data_type`` warns too: a literal
      FilterValue is a String, so the comparison fails at run time
      (#94). The warning names the DataType to pass.

    Fields wrapped in ``Format(`` / ``CStr(`` / ``.ToString`` compare
    as strings and are skipped. TopN-style operators take a count, not
    a comparand, so they are never checked.
    """
    if operator in _COUNT_OPERATORS:
        return []
    field_refs = _uncoerced_field_refs(expression)
    if not field_refs:
        return []
    warnings: list[str] = []

    literals = [v for v in values if v and not v.lstrip().startswith("=")]
    if literals and value_data_type is None and operator != "Like":
        for field_name in dict.fromkeys(field_refs):
            rd_type = _resolve_field_typename(dataset, field_name)
            group = _canonical_typename_group(rd_type) if rd_type else None
            suggested = _SUGGESTED_DATA_TYPE.get(group or "")
            if suggested is None:
                continue
            warnings.append(
                f"filter value type: field {field_name!r} is {rd_type!r} but "
                f"{literals[0]!r} is a String literal, so the comparison fails "
                f"at run time. Pass value_data_type={suggested!r} (writes "
                f'<FilterValue DataType="{suggested}">) or use an expression value.'
            )

    # Collapse the values list to a single haystack for parameter ref
    # discovery — order doesn't matter for the warning text.
    values_text = " ".join(v for v in values if v)
    param_refs = _PARAM_REF_RE.findall(values_text)
    if not param_refs:
        return warnings

    for field_name in field_refs:
        rd_type = _resolve_field_typename(dataset, field_name)
        field_group = _canonical_typename_group(rd_type) if rd_type else None
        if field_group is None:
            continue
        for param_name in param_refs:
            param_type = _resolve_parameter_datatype(root, param_name)
            param_group = _PARAM_TYPE_GROUP.get(param_type or "")
            if param_group is None:
                continue
            if field_group == param_group:
                continue
            warnings.append(
                f"filter type mismatch: field {field_name!r} is {rd_type!r} "
                f"({field_group}) but parameter {param_name!r} is "
                f"{param_type!r} ({param_group}). Mismatch will fail at "
                "runtime; consider field_format='...' to coerce, or "
                f"retype the parameter."
            )
    return warnings


def encode_filter_texts(expression: str, values: list[str]) -> tuple[str, list[str], list[str]]:
    """Return ``(expression, values, warnings)`` as they should be written to
    ``<FilterExpression>`` / ``<FilterValue>`` (issue #75).

    Literals (no leading ``=``) are kept verbatim — ``R&amp;D`` is a valid
    filter value. ``=``-expressions are entity-decoded (the LLM
    double-encoding guard) with a warning whenever that changed the text.
    Callers echo the returned texts, i.e. what was actually written.
    """
    warnings: list[str] = []
    written_expression, w = encode_value_text(expression, "filter expression")
    if w:
        warnings.append(w)
    written_values: list[str] = []
    for v in values:
        text, w = encode_value_text(v, "filter value")
        written_values.append(text)
        if w:
            warnings.append(w)
    return written_expression, written_values, warnings


__all__ = [
    "FILTER_OPERATORS",
    "FILTER_VALUE_DATA_TYPES",
    "encode_filter_texts",
    "filter_to_dict",
    "type_mismatch_warnings",
    "validate_filter",
    "wrap_with_format",
    "write_filter",
]
