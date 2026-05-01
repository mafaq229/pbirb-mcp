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


def dry_run_edit(path: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply ``ops`` to a clone of ``path``; report diff + verify; discard.

    ``ops`` is a list of ``{"tool": <name>, "args": {<arg>: <value>...}}``
    entries. The harness injects ``path`` into each op's args (overriding
    any value the caller supplied).

    On op failure, dispatch stops and the failure is recorded in
    ``applied[-1]`` with ``ok=False``; the diff and verify are still
    computed against whatever state the tempfile reached.

    The original file is never modified.
    """
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"path {path!r} is not a regular file")
    if not isinstance(ops, list):
        raise ValueError("ops must be a list of {tool, args} entries")

    server = _build_server()
    original_bytes = src.read_bytes()
    original_text = original_bytes.decode("utf-8", errors="replace")

    with tempfile.NamedTemporaryFile(suffix=".rdl", prefix="pbirb-dry-run-", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
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

        return {"applied": applied, "diff": diff, "verify": verify}
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


__all__ = ["dry_run_edit"]
