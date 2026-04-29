"""Schema/structural validation tool (Phase 7 commit 30).

Wraps :mod:`pbirb_mcp.core.schema` for the JSON-RPC surface. The
``validate_report`` tool runs:

1. **Parse** — lxml load. Failures are reported as ``rule="parse"``.
2. **Structural** — root-element + required top-level sections check.
   Failures: ``rule="structural"``.
3. **XSD** — opt-in. If ``pbirb_mcp/schemas/reportdefinition.xsd`` is
   bundled, the document is validated against it. Failures:
   ``rule="xsd"``. The Microsoft RDL XSD is not redistributable, so the
   shipped package returns ``xsd_used=False``; users who drop the
   official XSD into ``schemas/`` get full validation.

Issue shape mirrors :mod:`pbirb_mcp.ops.lint` (``severity``, ``rule``,
``location``, ``message``) so :func:`verify_report` (commit 33) can union
results with no reshaping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lxml import etree

from pbirb_mcp.core.schema import (
    RDLValidationError,
    _load_xsd,
    validate_structure,
    xsd_available,
)


def _load_tree(path: str) -> tuple[etree._ElementTree | None, dict | None]:
    """Parse the file. Return (tree, None) on success, (None, issue) on
    failure — issue uses the canonical lint shape.
    """
    p = Path(path)
    if not p.is_file():
        return None, {
            "severity": "error",
            "rule": "parse",
            "location": str(p),
            "message": f"file not found or not a regular file: {path}",
        }
    try:
        tree = etree.parse(str(p))
    except etree.XMLSyntaxError as exc:
        return None, {
            "severity": "error",
            "rule": "parse",
            "location": f"{path}:{exc.lineno}" if exc.lineno else str(p),
            "message": str(exc),
        }
    return tree, None


def validate_report(path: str) -> dict[str, Any]:
    """Run XSD + structural validation against an RDL file.

    Returns ``{valid, errors, xsd_used}``:

    * ``valid`` — ``True`` only when ``errors`` is empty.
    * ``errors`` — list of ``{severity, rule, location, message}`` dicts.
    * ``xsd_used`` — ``True`` iff the bundled XSD ran. Always ``False``
      when the optional XSD file isn't present.
    """
    tree, parse_issue = _load_tree(path)
    if parse_issue is not None:
        return {"valid": False, "errors": [parse_issue], "xsd_used": False}

    errors: list[dict[str, Any]] = []

    # Structural — cheap, always runs.
    try:
        validate_structure(tree)
    except RDLValidationError as exc:
        errors.append(
            {
                "severity": "error",
                "rule": "structural",
                "location": path,
                "message": str(exc),
            }
        )

    # XSD — opt-in. Skip when not bundled.
    xsd_used = False
    if xsd_available():
        schema = _load_xsd()
        if schema is not None:
            xsd_used = True
            if not schema.validate(tree):
                for entry in schema.error_log:
                    errors.append(
                        {
                            "severity": "error",
                            "rule": "xsd",
                            "location": f"{path}:{entry.line}",
                            "message": entry.message,
                        }
                    )

    return {"valid": not errors, "errors": errors, "xsd_used": xsd_used}


__all__ = ["validate_report"]
