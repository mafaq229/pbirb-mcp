"""Schema/structural validation tool (Phase 7 commit 30).

Wraps :mod:`pbirb_mcp.core.schema` for the JSON-RPC surface. The
``validate_report`` tool runs:

1. **Parse** — lxml load. Failures are reported as ``rule="parse"``.
2. **Structural** — root-element + required top-level sections check.
   Failures: ``rule="structural"``.
3. **XSD** — bundled by default since v0.3.1. The Microsoft RDL 2016/01
   XSD ships under ``pbirb_mcp/schemas/reportdefinition.xsd`` (see
   ``schemas/NOTICE.md`` for the redistribution permission granted by the
   MS-RDL Open Specifications IP Rights Notice). Failures:
   ``rule="xsd"``. If the schema file is missing (e.g. a source-build
   that didn't copy package-data), ``validate_report`` emits a
   ``severity="warning"``, ``rule="xsd-not-bundled"`` issue rather than
   silently skipping — the silent skip masked four schema-conformance
   bugs in the v0.3.0 live-MCP sweep.

Issue shape mirrors :mod:`pbirb_mcp.ops.lint` (``severity``, ``rule``,
``location``, ``message``) so :func:`verify_report` (commit 33) can union
results with no reshaping.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Optional

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
    # Inside the owning transaction, validate the live in-memory tree —
    # the on-disk file doesn't have the transaction's edits yet.
    from pbirb_mcp.core import transactions as _tx

    if p.is_file():
        tx = _tx.owned_transaction(str(p.resolve()))
        if tx is not None:
            return tx.doc.tree, None
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


def _validate_tree(tree: etree._ElementTree, location: str) -> tuple[list[dict[str, Any]], bool]:
    """Structural + XSD checks on a parsed tree. Returns ``(errors, xsd_used)``."""
    errors: list[dict[str, Any]] = []

    # Structural — cheap, always runs.
    try:
        validate_structure(tree)
    except RDLValidationError as exc:
        errors.append(
            {
                "severity": "error",
                "rule": "structural",
                "location": location,
                "message": str(exc),
            }
        )

    # XSD — bundled by default since v0.3.1. If the file is missing
    # (e.g. a source-build that didn't copy package-data) emit a loud
    # warning rather than silently skipping. Silent skip masked four
    # schema-conformance bugs in the v0.3.0 live-MCP sweep.
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
                            "location": f"{location}:{entry.line}",
                            "message": entry.message,
                        }
                    )
    else:
        errors.append(
            {
                "severity": "warning",
                "rule": "xsd-not-bundled",
                "location": "pbirb_mcp/schemas/reportdefinition.xsd",
                "message": (
                    "bundled RDL 2016 XSD not found; XSD validation skipped. "
                    "This masks the schema-conformance bug class that "
                    "Power BI Report Builder rejects on load."
                ),
                "suggestion": (
                    "reinstall pbirb-mcp (the package ships the XSD as "
                    "package-data) or restore the file from the source "
                    "tree per pbirb_mcp/schemas/NOTICE.md"
                ),
            }
        )
    return errors, xsd_used


def validate_report(path: str) -> dict[str, Any]:
    """Run XSD + structural validation against an RDL file.

    Returns ``{valid, errors, xsd_used}``:

    * ``valid`` — ``True`` only when ``errors`` is empty.
    * ``errors`` — list of ``{severity, rule, location, message}`` dicts.
    * ``xsd_used`` — ``True`` iff the bundled XSD ran. Always ``False``
      when the optional XSD file isn't present.

    Inside the transaction that holds ``path`` it validates the
    transaction's in-memory tree.
    """
    tree, parse_issue = _load_tree(path)
    if parse_issue is not None:
        return {"valid": False, "errors": [parse_issue], "xsd_used": False}
    errors, xsd_used = _validate_tree(tree, path)
    # `valid` reflects ERROR-severity issues only — warnings (e.g.
    # xsd-not-bundled) don't flip it. Mirrors verify_report's
    # severity-aware computation.
    has_error = any(e.get("severity") == "error" for e in errors)
    return {"valid": not has_error, "errors": errors, "xsd_used": xsd_used}


def verify_doc(doc: Any) -> dict[str, Any]:
    """XSD + structural + lint on an already-open document's in-memory tree.

    Used at transaction start (baseline) and commit, so ``apply_edits``
    can't commit a file Report Builder would reject. Returns
    ``{valid, issues, rules_run, xsd_used}``.
    """
    from pbirb_mcp.ops.lint import _lint_doc

    errors, xsd_used = _validate_tree(doc.tree, str(doc.path))
    lint = _lint_doc(doc)
    issues: list[dict[str, Any]] = [*errors, *lint["issues"]]
    has_error = any(i.get("severity") == "error" for i in issues)
    return {
        "valid": not has_error,
        "issues": issues,
        "rules_run": lint["rules_run"],
        "xsd_used": xsd_used,
    }


# Rules whose location is just "<file path>[:<line>]".
_FILE_LEVEL_RULES = frozenset({"parse", "structural", "xsd", "xsd-not-bundled"})


def issue_key(issue: dict[str, Any]) -> tuple:
    """Identity of an issue for baseline comparison. File-level issues
    (XSD etc.) are located by "<path>:<line>"; the line shifts with
    unrelated edits and the path differs between a file and its backup, so
    they're keyed on rule + message only."""
    rule = issue.get("rule")
    location = "" if rule in _FILE_LEVEL_RULES else str(issue.get("location", ""))
    return (rule, location, issue.get("message"))


def baseline_of(verify: dict[str, Any]) -> Counter:
    """A multiset of :func:`issue_key` for every issue in a verify result."""
    return Counter(issue_key(i) for i in verify.get("issues", []))


def mark_new_issues(verify: dict[str, Any], baseline: Counter) -> dict[str, Any]:
    """Annotate each issue with ``new`` (not in ``baseline``) and add
    ``introduced_count`` / ``preexisting_count`` / ``introduced_error_count``.

    Duplicate keys are matched as a multiset: if the baseline had one copy
    of an issue and the file now has two, one of them is new.
    """
    remaining = Counter(baseline)
    introduced = preexisting = introduced_errors = 0
    marked = []
    for issue in verify.get("issues", []):
        key = issue_key(issue)
        is_new = remaining[key] <= 0
        if not is_new:
            remaining[key] -= 1
            preexisting += 1
        else:
            introduced += 1
            if issue.get("severity") == "error":
                introduced_errors += 1
        marked.append({**issue, "new": is_new})
    return {
        **verify,
        "issues": marked,
        "introduced_count": introduced,
        "preexisting_count": preexisting,
        "introduced_error_count": introduced_errors,
    }


def verify_report(path: str, baseline_path: Optional[str] = None) -> dict[str, Any]:
    """Single-call composite: union of :func:`validate_report` and
    :func:`pbirb_mcp.ops.lint.lint_report`.

    Returns ``{valid, issues, xsd_used}``:

    * ``issues`` — every ``{severity, rule, location, message,
      suggestion?}`` from validate (rule ∈ {parse, structural, xsd}) and
      lint (rules from :mod:`pbirb_mcp.ops.lint`).
    * ``valid`` — ``True`` iff no issue carries ``severity == "error"``.
      Warnings don't invalidate the report.
    * ``xsd_used`` — passed through from validate.

    With ``baseline_path`` (e.g. a backup of the file before an edit), or
    when called inside the transaction holding ``path`` (whose start-state
    is the baseline), each issue also carries ``new`` and the result adds
    ``introduced_count`` / ``preexisting_count`` /
    ``introduced_error_count``.

    The unified shape is what :envvar:`PBIRB_MCP_AUTO_VERIFY` (commit 34)
    splices into mutating-tool responses.
    """
    # Local import dodges a load-time circular: lint imports validate
    # would chain via ops.dataset → ops.parameters in some configs.
    from pbirb_mcp.core import transactions as _tx
    from pbirb_mcp.ops.lint import lint_report

    v = validate_report(path)
    if not v["valid"] and any(e["rule"] == "parse" for e in v["errors"]):
        # Parse failed — lint can't do anything sane.
        return {"valid": False, "issues": v["errors"], "xsd_used": v["xsd_used"]}

    lint = lint_report(path)
    issues: list[dict[str, Any]] = []
    issues.extend(v["errors"])
    issues.extend(lint["issues"])
    has_error = any(i.get("severity") == "error" for i in issues)
    result = {"valid": not has_error, "issues": issues, "xsd_used": v["xsd_used"]}

    baseline: Optional[Counter] = None
    if baseline_path is not None:
        baseline = baseline_of(verify_report(baseline_path))
    else:
        p = Path(path)
        tx = _tx.owned_transaction(str(p.resolve())) if p.is_file() else None
        if tx is not None and tx.baseline is not None:
            baseline = tx.baseline
    if baseline is not None:
        result = mark_new_issues(result, baseline)
    return result


__all__ = [
    "baseline_of",
    "issue_key",
    "mark_new_issues",
    "validate_report",
    "verify_doc",
    "verify_report",
]
