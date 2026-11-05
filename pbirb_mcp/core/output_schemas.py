"""Output JSON Schemas for tool results (issue #55).

Every tool publishes an ``outputSchema`` in ``tools/list`` and returns the
same value as ``structuredContent`` next to the JSON text block. MCP
requires ``structuredContent`` to be an object, so a tool whose handler
returns a list or a string is wrapped as ``{"result": <value>}`` (the
convention the SDK's own high-level server uses); the text block keeps the
bare value, unchanged for existing clients.

The schemas are deliberately permissive — one per result family, with the
fields every member really returns typed and everything else open
(``additionalProperties`` allowed). A strict, hand-written schema per tool
would drift from 145 handlers, and a schema the output violates is worse
than none: SDK clients reject such a result outright. A handful of
high-traffic tools (``describe_report``, the validators, ``raw_xml_view``,
``dry_run_edit``) get specific schemas from :data:`SPECIFIC`.
``tests/test_output_schemas.py`` validates real outputs against them.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

__all__ = [
    "EDIT_RESULT",
    "OBJECT_RESULT",
    "SPECIFIC",
    "default_output_schema",
    "structured_content",
    "wrapped",
]


def wrapped(value_schema: dict) -> dict:
    """Schema for a non-object result, published as ``{"result": value}``."""
    return {
        "type": "object",
        "properties": {"result": value_schema},
        "required": ["result"],
    }


def structured_content(result: Any) -> dict:
    """The ``structuredContent`` object for a handler's (JSON) result."""
    return result if isinstance(result, dict) else {"result": result}


# Any read that returns an object.
OBJECT_RESULT: dict = {"type": "object"}

# A tool that writes the report. `changed` is a boolean, or the list of
# what changed; most writers also echo the path and the element they
# touched. With PBIRB_MCP_AUTO_VERIFY on, the outcome is nested as
# {result, verify}. (Documented once in docs/TOOLS.md rather than in the
# schema, which ~115 tools share — keeps tools/list small.)
EDIT_RESULT: dict = {
    "type": "object",
    "properties": {
        "changed": {"type": ["boolean", "array"]},
        "warnings": {"type": "array"},
        "verify": {"type": "object"},
    },
}

_LIST_OF_OBJECTS = wrapped({"type": "array", "items": {"type": "object"}})
_STRING = wrapped({"type": "string"})

_ISSUES = {"type": "array", "items": {"type": "object"}}

SPECIFIC: dict[str, dict] = {
    "describe_report": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "data_sources": {"type": "array", "items": {"type": "string"}},
            "datasets": {"type": "array", "items": {"type": "string"}},
            "parameters": {"type": "array", "items": {"type": "string"}},
            "tablixes": {"type": "array", "items": {"type": "object"}},
            "charts": {"type": "array", "items": {"type": "string"}},
            "page": {"type": "object"},
            "body": {"type": "object"},
        },
        "required": ["path", "data_sources", "datasets", "parameters", "tablixes", "charts"],
    },
    # Both views: `detailed` (every key) and `concise` (name, position,
    # counts, flat group names) — so only `name` is required.
    "get_tablixes": wrapped(
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "object"}},
                    "rows": {"type": "array", "items": {"type": "object"}},
                    "row_groups": {"type": "array"},
                    "column_groups": {"type": "array"},
                    "sort_expressions": {"type": "array", "items": {"type": "string"}},
                    "sort": {"type": "array", "items": {"type": "object"}},
                    "cells": {"type": "array", "items": {"type": "object"}},
                    "header_cells": {"type": "array", "items": {"type": "object"}},
                    "corner_cells": {"type": "array", "items": {"type": "object"}},
                    "row_members": {"type": "array", "items": {"type": "object"}},
                    "column_members": {"type": "array", "items": {"type": "object"}},
                    "row_leaves": {"type": "array", "items": {"type": "object"}},
                    "column_leaves": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["name"],
            },
        }
    ),
    "validate_report": {
        "type": "object",
        "properties": {
            "valid": {"type": "boolean"},
            "errors": _ISSUES,
            "xsd_used": {"type": "boolean"},
        },
        "required": ["valid", "errors"],
    },
    "verify_report": {
        "type": "object",
        "properties": {
            "valid": {"type": ["boolean", "null"]},
            "issues": _ISSUES,
            "xsd_used": {"type": "boolean"},
        },
        "required": ["valid", "issues"],
    },
    "lint_report": {
        "type": "object",
        "properties": {
            "issues": _ISSUES,
            "rules_run": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["issues"],
    },
    "raw_xml_view": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["nodes", "number", "string", "boolean"]},
            "count": {"type": "integer"},
            "matches": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Typed matches: {kind: element, xml} / {kind: attribute, name, value, "
                    "element} / {kind: text, value}. Past max_bytes the crossing match ends "
                    "in a '<!-- pbirb-mcp: truncated ... -->' marker."
                ),
            },
            "truncated": {"type": "boolean"},
            "value": {"type": ["number", "string", "boolean", "null"]},
        },
        "required": ["kind"],
    },
    "dry_run_edit": {
        "type": "object",
        "properties": {
            "applied": {"type": "array", "items": {"type": "object"}},
            "diff": {
                "type": ["string", "array", "object"],
                "items": {"type": "string"},
                "description": (
                    "Unified diff text, touched element paths (diff=summary) or "
                    "diff_report's lists (diff=semantic)."
                ),
            },
            "diff_stats": {"type": "object"},
            "diff_truncated": {"type": "boolean"},
            "verify": {"type": "object"},
        },
        "required": ["applied", "verify"],
    },
}


def _annotation_kind(handler: Callable[..., Any]) -> Optional[str]:
    """``"list"`` / ``"str"`` / ``"dict"`` from the handler's return annotation."""
    try:
        ann = inspect.signature(handler).return_annotation
    except (TypeError, ValueError):
        return None
    text = ann if isinstance(ann, str) else getattr(ann, "__name__", repr(ann))
    text = str(text).replace("typing.", "").lower()
    for kind in ("list", "str", "dict"):
        if text.startswith(kind):
            return kind
    return None


def default_output_schema(name: str, handler: Callable[..., Any], *, mutating: bool) -> dict:
    """The outputSchema a tool gets when its registration doesn't declare one."""
    if name in SPECIFIC:
        return SPECIFIC[name]
    kind = _annotation_kind(handler)
    if kind == "list":
        return _LIST_OF_OBJECTS
    if kind == "str":
        return _STRING
    return EDIT_RESULT if mutating else OBJECT_RESULT
