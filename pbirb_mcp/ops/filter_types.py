"""Filter expression helpers (Phase 8 commit 36).

Two helpers shared between :func:`pbirb_mcp.ops.tablix.add_tablix_filter`
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

The helpers are intentionally small: regex-based detection (no DAX
parser), one-pass scan, and only the type pairings that recur in real
session feedback (date-vs-string is the only documented case in v0.2).
"""

from __future__ import annotations

import re
from typing import Optional

from lxml import etree

from pbirb_mcp.core.xpath import RD_NS, find_child, find_children

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


# RDL field reference inside an expression: Fields!<name>.Value
_FIELD_REF_RE = re.compile(r"Fields!(\w+)\.Value")
# RDL parameter reference: Parameters!<name>.Value (or .Label)
_PARAM_REF_RE = re.compile(r"Parameters!(\w+)\.(?:Value|Label)")


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


def type_mismatch_warnings(
    root: etree._Element,
    dataset: etree._Element,
    expression: str,
    values: list[str],
) -> list[str]:
    """Cross-check field types vs parameter types referenced in a
    filter expression and its values. One warning per detected
    mismatch. Skips silently when either side is unreadable.

    Heuristic: every ``Fields!X.Value`` in ``expression`` is paired
    with every ``Parameters!Y.Value`` in ``values``. This is loose by
    design — a single value is the common case, and false positives
    here would be quieter than a hard rule.
    """
    field_refs = _FIELD_REF_RE.findall(expression or "")
    if not field_refs:
        return []
    # Collapse the values list to a single haystack for parameter ref
    # discovery — order doesn't matter for the warning text.
    values_text = " ".join(v for v in values if v)
    param_refs = _PARAM_REF_RE.findall(values_text)
    if not param_refs:
        return []

    warnings: list[str] = []
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


__all__ = [
    "wrap_with_format",
    "type_mismatch_warnings",
]
