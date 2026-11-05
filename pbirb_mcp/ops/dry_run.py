"""Dry-run edit harness (Phase 7 commit 32).

``dry_run_edit(path, ops)`` clones the source ``.rdl`` to a tempfile,
dispatches each ``{tool, args}`` op against it via the same JSON-RPC
surface real callers use, then returns:

* ``applied`` — per-op outcome ``[{tool, ok, result|error}]``.
* ``diff`` — unified diff of the source vs. the post-op tempfile.
* ``verify`` — validate + lint output for the post-op tempfile.

The original file is **never** touched: failures stop dispatch and the
tempfile is discarded. The caller does not need to know the tempfile
path ahead of time — the harness injects it as the ``path`` argument
of every op (overriding any caller-supplied ``path``).
"""

from __future__ import annotations

import contextlib
import difflib
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.validate import validate_report


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


# Default cap on the returned unified diff (issue #55): a dry run on a CRLF
# report produced a 303 KB diff (~76k tokens), past what MCP clients show.
DRY_RUN_MAX_DIFF_BYTES = 40_000


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


def _cap_diff(diff: str, max_bytes: int) -> tuple[str, bool]:
    raw = diff.encode("utf-8")
    if not max_bytes or len(raw) <= max_bytes:
        return diff, False
    head = raw[:max_bytes].decode("utf-8", errors="ignore")
    if "\n" in head:
        head = head[: head.rindex("\n") + 1]  # end on a whole line
    marker = (
        f"\\ pbirb-mcp: diff truncated: showing {len(head.encode('utf-8'))} of "
        f"{len(raw)} bytes; see diff_stats, pass summary=true, or raise "
        "max_diff_bytes (0 = no cap)\n"
    )
    return head + marker, True


def _verify_summary(verify: dict[str, Any]) -> dict[str, Any]:
    by_rule: dict[str, int] = {}
    for issue in verify["issues"]:
        rule = issue.get("rule", "?") if isinstance(issue, dict) else "?"
        by_rule[rule] = by_rule.get(rule, 0) + 1
    return {
        "valid": verify["valid"],
        "xsd_used": verify["xsd_used"],
        "error_count": len(verify["errors"]),
        "issue_count": len(verify["issues"]),
        "issues_by_rule": by_rule,
    }


def dry_run_edit(
    path: str,
    ops: list[dict[str, Any]],
    summary: bool = False,
    max_diff_bytes: int = DRY_RUN_MAX_DIFF_BYTES,
) -> dict[str, Any]:
    """Apply ``ops`` to a clone of ``path``; report diff + verify; discard.

    ``ops`` is a list of ``{"tool": <name>, "args": {<arg>: <value>...}}``
    entries. The harness injects ``path`` into each op's args (overriding
    any value the caller supplied).

    On op failure, dispatch stops and the failure is recorded in
    ``applied[-1]`` with ``ok=False``; the diff and verify are still
    computed against whatever state the tempfile reached.

    The original file is never modified.

    The result always carries ``diff_stats`` (lines added/removed, hunks).
    ``diff`` is capped at ``max_diff_bytes`` (``0`` = no cap): a capped diff
    ends on a whole line followed by an explicit ``\\ pbirb-mcp: diff
    truncated ...`` marker line, and ``diff_truncated`` is true.
    ``summary=True`` leaves the diff out, drops each op's ``result_text``
    and reduces ``verify`` to counts (``error_count``, ``issue_count``,
    ``issues_by_rule``).
    """
    if isinstance(max_diff_bytes, bool) or not isinstance(max_diff_bytes, int):
        raise ValueError("max_diff_bytes must be a non-negative integer (0 = no cap)")
    if max_diff_bytes < 0:
        raise ValueError("max_diff_bytes must be a non-negative integer (0 = no cap)")
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
                import json as _json

                applied.append({"tool": tool, "ok": False, "error": _json.loads(text)})
                break
            import json as _json

            try:
                parsed = _json.loads(text)
            except ValueError:
                parsed = None
            if payload_reports_failure(parsed):
                applied.append(
                    {
                        "tool": tool,
                        "ok": False,
                        "result_text": text,
                        "error": {
                            "error_type": "OpFailed",
                            "message": "op reported committed/saved = false",
                        },
                    }
                )
                break
            applied.append({"tool": tool, "ok": True, "result_text": text})

        # Always compute a diff + verify against the tempfile state. Even
        # on partial failure, the user gets to see how far the edit got.
        after_bytes = tmp_path.read_bytes()
        after_text = after_bytes.decode("utf-8", errors="replace")
        diff = _diff(original_text, after_text, str(src), "<dry-run-output>")

        validate_result = validate_report(str(tmp_path))
        lint_result = lint_report(str(tmp_path))
        verify = {
            "valid": validate_result["valid"] and not lint_result["issues"],
            "errors": validate_result["errors"],
            "issues": lint_result["issues"],
            "xsd_used": validate_result["xsd_used"],
        }

        stats = _diff_stats(diff)
        if summary:
            return {
                "applied": [{k: v for k, v in a.items() if k != "result_text"} for a in applied],
                "diff_stats": stats,
                "verify": _verify_summary(verify),
            }
        diff, truncated = _cap_diff(diff, max_diff_bytes)
        return {
            "applied": applied,
            "diff": diff,
            "diff_stats": stats,
            "diff_truncated": truncated,
            "verify": verify,
        }
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        # Defensive: nothing may outlive the preview (e.g. an auto-backup
        # an op took of the temp copy).
        for leftover in tmp_path.parent.glob(tmp_path.name + ".bak.*"):
            with contextlib.suppress(OSError):
                leftover.unlink()


__all__ = ["dry_run_edit"]
