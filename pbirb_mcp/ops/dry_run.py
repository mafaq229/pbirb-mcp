"""Dry-run edit harness (Phase 7 commit 32).

``dry_run_edit(path, ops)`` clones the source ``.rdl`` to a tempfile,
dispatches each ``{tool, args}`` op against it via the same JSON-RPC
surface real callers use, then returns:

* ``applied`` — per-op outcome ``[{tool, ok, result|error}]``, the same
  entries :func:`pbirb_mcp.ops.transactions.apply_edits` returns (plus the
  deprecated ``result_text``, the result as a JSON string; issue #84).
* ``diff`` — the change from the source to the post-op tempfile: a unified
  text diff, a list of touched element paths, or nothing (``diff=``).
* ``verify`` — XSD + structural + lint for the post-op tempfile, in the
  shape ``apply_edits`` / ``commit_editing_transaction`` use.

The original file is **never** touched: failures stop dispatch and the
tempfile is discarded. The caller does not need to know the tempfile
path ahead of time — the harness injects it as the ``path`` argument
of every op (overriding any caller-supplied ``path``).
"""

from __future__ import annotations

import contextlib
import difflib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.security import preview_path
from pbirb_mcp.ops.validate import baseline_of, mark_new_issues, validate_report, verify_doc


def _build_server():
    """Lazy import to avoid a circular dependency at module load time."""
    from pbirb_mcp.server import MCPServer
    from pbirb_mcp.tools import register_all_tools

    server = MCPServer()
    register_all_tools(server)
    return server


def _diff(before_text: str, after_text: str, label_before: str, label_after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=label_before,
            tofile=label_after,
        )
    )


# Default cap on the returned diff (issue #55): a dry run on a CRLF report
# produced a 303 KB diff (~76k tokens), past what MCP clients show.
DRY_RUN_MAX_DIFF_BYTES = 40_000

# `diff` modes (issue #84): the unified text diff, the touched element
# paths, or no diff (diff_stats only).
DIFF_MODES = ("unified", "summary", "none")


def _diff_stats(diff: str) -> dict[str, Any]:
    added = removed = hunks = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"changed": bool(diff), "lines_added": added, "lines_removed": removed, "hunks": hunks}


_SHORT_MARKER = "\\ pbirb-mcp: diff truncated\n"


def _cap_diff(diff: str, max_bytes: int) -> tuple[str, bool]:
    """``diff`` cut to at most ``max_bytes`` UTF-8 bytes, marker included.

    A cut diff ends on a whole line followed by a ``\\ pbirb-mcp: diff
    truncated ...`` marker line (the ``\\`` prefix is unified diff's
    comment-line form, which patch tools skip).
    """
    raw = diff.encode("utf-8")
    if not max_bytes or len(raw) <= max_bytes:
        return diff, False
    total = len(raw)

    def marker(shown: int) -> str:
        return (
            f"\\ pbirb-mcp: diff truncated: showing {shown} of {total} bytes; see "
            "diff_stats, use diff=summary, or raise max_diff_bytes (0 = no cap)\n"
        )

    # Room for the longest marker this cut can produce (shown <= total).
    reserve = len(marker(total).encode("utf-8"))
    if reserve > max_bytes:
        return _SHORT_MARKER.encode("utf-8")[:max_bytes].decode("utf-8", "ignore"), True
    head = raw[: max_bytes - reserve].decode("utf-8", errors="ignore")
    head = head[: head.rindex("\n") + 1] if "\n" in head else ""  # whole lines only
    return head + marker(len(head.encode("utf-8"))), True


def _cap_lines(lines: list[str], max_bytes: int) -> tuple[list[str], bool]:
    """The first ``lines`` whose JSON form fits ``max_bytes``, then a
    ``"... N more ..."`` line when any were left out."""
    if not max_bytes or len(json.dumps(lines).encode("utf-8")) <= max_bytes:
        return lines, False
    kept: list[str] = []
    for i, line in enumerate(lines):
        more = f"... {len(lines) - i} more; raise max_diff_bytes (0 = no cap)"
        if len(json.dumps([*kept, line, more]).encode("utf-8")) > max_bytes:
            return [*kept, more], True
        kept.append(line)
    return kept, False  # pragma: no cover — the whole list fitted above


def _summary_lines(before: bytes, after: bytes) -> list[str]:
    """``"<change> <element path>"`` for every element the ops touched."""
    from pbirb_mcp.ops.diff import semantic_diff, summary_paths

    parser = etree.XMLParser(resolve_entities=False)
    try:
        a = etree.fromstring(before, parser)
        b = etree.fromstring(after, parser)
    except etree.XMLSyntaxError as exc:
        return [f"unparseable result: {exc}"]
    return summary_paths(semantic_diff(a, b))


def _verify_file(path: Path, baseline: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """:func:`verify_doc` of the file at ``path``.

    ``{valid, issues, rules_run, xsd_used, introduced_count,
    preexisting_count, introduced_error_count}``: XSD / structural errors are
    among ``issues``, ``valid`` is false only for an error-severity issue (as
    in verify_report) and each issue is marked ``new`` against ``baseline``
    (another such result; ``None`` = everything is new).
    """
    try:
        verify = verify_doc(RDLDocument.open(path))
    except etree.XMLSyntaxError:
        v = validate_report(str(path))
        verify = {"valid": False, "issues": v["errors"], "rules_run": [], "xsd_used": False}
    return mark_new_issues(verify, baseline_of(baseline or {"issues": []}))


def _verify_summary(verify: dict[str, Any]) -> dict[str, Any]:
    by_rule: dict[str, int] = {}
    for issue in verify["issues"]:
        rule = issue.get("rule", "?") if isinstance(issue, dict) else "?"
        by_rule[rule] = by_rule.get(rule, 0) + 1
    return {
        "valid": verify["valid"],
        "xsd_used": verify["xsd_used"],
        "error_count": sum(1 for i in verify["issues"] if i.get("severity") == "error"),
        "issue_count": len(verify["issues"]),
        "issues_by_rule": by_rule,
    }


def dry_run_edit(
    path: str,
    ops: list[dict[str, Any]],
    summary: bool = False,
    max_diff_bytes: int = DRY_RUN_MAX_DIFF_BYTES,
    diff: Optional[str] = None,
) -> dict[str, Any]:
    """Apply ``ops`` to a clone of ``path``; report diff + verify; discard.

    ``ops`` is a list of ``{"tool": <name>, "args": {<arg>: <value>...}}``
    entries. The harness injects ``path`` into each op's args (overriding
    any value the caller supplied).

    On op failure, dispatch stops and the failure is recorded in
    ``applied[-1]`` with ``ok=False``; the diff and verify are still
    computed against whatever state the tempfile reached.

    The original file is never modified.

    ``applied`` entries are ``{tool, ok, result|error}`` as in apply_edits;
    ``result_text`` (the result as a JSON string) is kept for one release
    and deprecated. ``verify`` is ``{valid, issues, rules_run, xsd_used,
    introduced_count, preexisting_count, introduced_error_count}`` — the
    shape apply_edits and commit_editing_transaction return — with each
    issue marked ``new`` unless the source already had it; warnings don't
    make it invalid.

    ``diff`` selects the change report: ``"unified"`` (the default) — a
    unified text diff; ``"summary"`` — a list of ``"<change> <element
    path>"`` lines (e.g. ``changed Textbox[Title]/Paragraphs/Paragraph/
    TextRuns/TextRun/Value``); ``"none"`` — no diff. Either diff is capped
    at ``max_diff_bytes`` (``0`` = no cap), marker included: a capped
    unified diff ends on a whole line followed by an explicit ``\\
    pbirb-mcp: diff truncated ...`` line, a capped summary with a ``"...
    N more"`` line, and ``diff_truncated`` is true. ``diff_stats`` (lines
    added/removed, hunks of the text diff) is always present.

    ``summary=True`` drops each op's ``result``/``result_text``, reduces
    ``verify`` to counts (``error_count``, ``issue_count``,
    ``issues_by_rule``) and leaves the diff out unless ``diff`` asks for one.
    """
    if isinstance(max_diff_bytes, bool) or not isinstance(max_diff_bytes, int):
        raise ValueError("max_diff_bytes must be a non-negative integer (0 = no cap)")
    if max_diff_bytes < 0:
        raise ValueError("max_diff_bytes must be a non-negative integer (0 = no cap)")
    if diff is None:
        diff = "none" if summary else "unified"
    if diff not in DIFF_MODES:
        raise ValueError(f"diff must be one of {', '.join(DIFF_MODES)}; got {diff!r}")
    from pbirb_mcp.core import transactions as _tx
    from pbirb_mcp.ops.batch import check_ops, payload_reports_failure

    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"path {path!r} is not a regular file")
    if not isinstance(ops, list):
        raise ValueError("ops must be a list of {tool, args} entries")

    server = _build_server()
    # Refuse non-batchable ops before any temp file is created.
    check_ops(server, ops)

    # Called inside the transaction that holds `path`: preview against the
    # transaction's in-memory tree, not the (older) disk bytes.
    owner = _tx.owned_transaction(str(src.resolve()))
    original_bytes = owner.doc.to_bytes() if owner is not None else src.read_bytes()
    original_text = original_bytes.decode("utf-8", errors="replace")

    with tempfile.NamedTemporaryFile(suffix=".rdl", prefix="pbirb-dry-run-", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        if owner is not None:
            tmp_path.write_bytes(original_bytes)
        else:
            shutil.copy(str(src), str(tmp_path))
        # What's already wrong with the source: verify marks only the
        # issues the ops introduce as `new` (as commit does).
        baseline = _verify_file(tmp_path)

        applied: list[dict[str, Any]] = []
        for i, op in enumerate(ops):
            tool = op.get("tool")
            args = dict(op.get("args") or {})
            if not isinstance(tool, str):
                applied.append(
                    {
                        "tool": tool,
                        "ok": False,
                        "error": {
                            "error_type": "ValueError",
                            "message": f"op #{i} missing string `tool`",
                        },
                    }
                )
                break
            args["path"] = str(tmp_path)
            # The temp copy is the server's own file: allowed outside
            # PBIRB_MCP_ALLOWED_ROOTS and kept out of the audit log (#57).
            with preview_path(str(tmp_path)):
                resp = server.handle_request(
                    {
                        "jsonrpc": "2.0",
                        "id": i,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": args},
                    }
                )
            # Two failure shapes to handle:
            # 1. JSON-RPC error envelope (e.g. unknown tool) — top-level
            #    `error` key, no `result`.
            # 2. Tool-handler exception — `result.isError=True`, error
            #    payload JSON-encoded in `result.content[0].text`.
            if resp and "error" in resp:
                applied.append(
                    {
                        "tool": tool,
                        "ok": False,
                        "error": {
                            "error_type": "JSONRPCError",
                            "message": resp["error"].get("message", "unknown"),
                            "code": resp["error"].get("code"),
                        },
                    }
                )
                break
            result = resp.get("result", {}) if resp else {}
            is_error = result.get("isError", False)
            content = result.get("content", [])
            text = content[0]["text"] if content else "{}"
            if is_error:
                # server.py already JSON-encodes {error_type, message}
                # into content[0].text on handler exception.
                applied.append({"tool": tool, "ok": False, "error": json.loads(text)})
                break
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = text
            if payload_reports_failure(parsed):
                applied.append(
                    {
                        "tool": tool,
                        "ok": False,
                        "result": parsed,
                        "result_text": text,
                        "error": {
                            "error_type": "OpFailed",
                            "message": "op reported committed/saved = false",
                        },
                    }
                )
                break
            applied.append({"tool": tool, "ok": True, "result": parsed, "result_text": text})

        # Always compute a diff + verify against the tempfile state. Even
        # on partial failure, the user gets to see how far the edit got.
        after_bytes = tmp_path.read_bytes()
        after_text = after_bytes.decode("utf-8", errors="replace")
        text_diff = _diff(original_text, after_text, str(src), "<dry-run-output>")
        verify = _verify_file(tmp_path, baseline)

        out: dict[str, Any] = {
            "applied": (
                [
                    {k: v for k, v in a.items() if k not in ("result", "result_text")}
                    for a in applied
                ]
                if summary
                else applied
            ),
        }
        if diff == "unified":
            out["diff"], out["diff_truncated"] = _cap_diff(text_diff, max_diff_bytes)
        elif diff == "summary":
            lines = _summary_lines(original_bytes, after_bytes) if text_diff else []
            out["diff"], out["diff_truncated"] = _cap_lines(lines, max_diff_bytes)
        out["diff_stats"] = _diff_stats(text_diff)
        out["verify"] = _verify_summary(verify) if summary else verify
        return out
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        # Defensive: nothing may outlive the preview (e.g. an auto-backup
        # an op took of the temp copy).
        for leftover in tmp_path.parent.glob(tmp_path.name + ".bak.*"):
            with contextlib.suppress(OSError):
                leftover.unlink()


__all__ = ["DIFF_MODES", "dry_run_edit"]
