"""Tool-argument validation against a tool's ``inputSchema`` (issue #19).

A deliberately small JSON-Schema subset — exactly what ``tools.py``
uses — so we don't need a ``jsonschema`` dependency:

* ``type`` (``string`` / ``integer`` / ``number`` / ``boolean`` /
  ``array`` / ``object`` / ``null``, or a list of them);
* ``enum``, ``minimum``, ``maximum``, ``minLength``;
* ``required``, ``properties``, ``additionalProperties`` (``false``);
* array ``items`` and ``minItems`` / ``maxItems``;
* ``oneOf`` / ``anyOf`` (value must match at least one alternative).

No coercion: ``"false"`` is not a boolean and ``"1"`` is not an integer —
silently accepting them is how ``force="false"`` used to bypass a guard.
``bool`` is not an integer either (Python's ``True == 1`` notwithstanding).

:func:`client_schema` derives the schema published in ``tools/list``:
common MCP clients render list-valued ``type`` and ``oneOf`` as ``{}``,
so those are rewritten to a single type (see its docstring).
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional


class InvalidArguments(ValueError):
    """A tool call's arguments don't match its ``inputSchema``."""

    def __init__(self, param: str, expected: str, got: str, message: Optional[str] = None):
        self.param = param
        self.expected = expected
        self.got = got
        super().__init__(message or f"{param}: expected {expected}, got {got}")
        self.payload = {"param": param, "expected": expected, "got": got}


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean {json.dumps(value)}"
    if isinstance(value, int):
        return f"integer {value}"
    if isinstance(value, float):
        return f"number {value!r}"
    if isinstance(value, str):
        text = value if len(value) <= 40 else value[:37] + "..."
        return f"string {json.dumps(text)}"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_type(value: Any, name: str) -> bool:
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    if name == "null":
        return value is None
    return True  # unknown type keyword — don't block


def _check(value: Any, schema: dict, where: str) -> None:
    if not isinstance(schema, dict) or not schema:
        return
    for key in ("oneOf", "anyOf"):
        alternatives = schema.get(key)
        if alternatives:
            errors = []
            for alt in alternatives:
                try:
                    _check(value, alt, where)
                except InvalidArguments as exc:
                    errors.append(exc)
                else:
                    break
            else:
                expected = " or ".join(e.expected for e in errors) or "one of the alternatives"
                raise InvalidArguments(where, expected, _describe(value))

    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_is_type(value, t) for t in names):
            raise InvalidArguments(where, " or ".join(names), _describe(value))
    if value is None:
        return

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(json.dumps(v) for v in schema["enum"] if v is not None)
        raise InvalidArguments(where, f"one of [{allowed}]", _describe(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise InvalidArguments(where, f"number >= {schema['minimum']}", _describe(value))
        if "maximum" in schema and value > schema["maximum"]:
            raise InvalidArguments(where, f"number <= {schema['maximum']}", _describe(value))
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        raise InvalidArguments(
            where, f"string of length >= {schema['minLength']}", _describe(value)
        )
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise InvalidArguments(where, f"array with >= {schema['minItems']} items", "array")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise InvalidArguments(where, f"array with <= {schema['maxItems']} items", "array")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                _check(item, items, f"{where}[{i}]")
    if isinstance(value, dict) and ("properties" in schema or "required" in schema):
        _check_object(value, schema, prefix=f"{where}.")


def _check_object(value: dict, schema: dict, prefix: str = "") -> None:
    props = schema.get("properties", {}) or {}
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in props:
                known = ", ".join(sorted(props)) or "none"
                raise InvalidArguments(
                    f"{prefix}{key}",
                    "no such parameter",
                    _describe(value[key]),
                    message=f"unknown parameter {prefix}{key!s}; accepted parameters: {known}",
                )
    # Types of what was supplied first, so the error names the bad value
    # rather than some other missing parameter.
    for key, sub in props.items():
        if key in value:
            _check(value[key], sub, f"{prefix}{key}")
    for key in schema.get("required", []) or []:
        if key not in value:
            sub = props.get(key, {})
            raise InvalidArguments(
                f"{prefix}{key}",
                _expected_of(sub),
                "nothing",
                message=f"missing required parameter {prefix}{key}",
            )


def _expected_of(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):
        return " or ".join(t)
    if isinstance(t, str):
        return t
    if schema.get("oneOf") or schema.get("anyOf"):
        alts = schema.get("oneOf") or schema.get("anyOf")
        return " or ".join(_expected_of(a) for a in alts)
    return "a value"


def validate_arguments(schema: dict, arguments: dict) -> None:
    """Raise :class:`InvalidArguments` when ``arguments`` doesn't match
    the tool's ``schema``."""
    _check_object(arguments, schema or {})


# ---- published (client-safe) schemas ---------------------------------------

# When collapsing a oneOf, publish the most structured alternative.
_TYPE_RANK = ["object", "array", "string", "number", "integer", "boolean"]


def _alt_type(alt: dict) -> str:
    t = alt.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    return t or "string"


def _sanitize(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_sanitize(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _sanitize(v) for k, v in schema.items()}

    t = out.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        if len(non_null) == 1:
            out["type"] = non_null[0]
        else:
            best = min(non_null, key=lambda x: _TYPE_RANK.index(x) if x in _TYPE_RANK else 99)
            out["type"] = best
            _note(out, f"also accepts: {', '.join(x for x in non_null if x != best)}")
    if isinstance(out.get("enum"), list) and None in out["enum"]:
        out["enum"] = [v for v in out["enum"] if v is not None]

    for key in ("oneOf", "anyOf"):
        alts = out.pop(key, None)
        if alts:
            alts = [a for a in alts if _alt_type(a) != "null"] or alts
            best = min(
                alts,
                key=lambda a: _TYPE_RANK.index(_alt_type(a)) if _alt_type(a) in _TYPE_RANK else 99,
            )
            others = [_alt_type(a) for a in alts if a is not best]
            merged = dict(best)
            merged.update({k: v for k, v in out.items() if k not in merged or k == "description"})
            out = merged
            if others:
                _note(out, f"also accepts: {', '.join(others)}")
    return out


def _note(schema: dict, text: str) -> None:
    desc = schema.get("description")
    schema["description"] = f"{desc} ({text})" if desc else text.capitalize()


def client_schema(schema: dict) -> dict:
    """The schema to publish in ``tools/list``.

    * ``{"type": [T, "null"]}`` → ``{"type": T}`` (a parameter left out of
      ``required`` is already optional; ``null`` is still accepted);
    * ``None`` dropped from ``enum``;
    * ``oneOf`` / ``anyOf`` / multi-type ``type`` → the most structured
      alternative, with the other accepted types noted in the description.

    The server keeps validating against the original schema, so every
    value it accepted before is still accepted.
    """
    return _sanitize(copy.deepcopy(schema))


__all__ = ["InvalidArguments", "client_schema", "validate_arguments"]
