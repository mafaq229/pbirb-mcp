"""User-facing transaction tools (v0.4 commits 10 + 11).

* :func:`start_editing_transaction` — open a document, register it,
  return a ``transaction_id``. Subsequent edit tools that include
  ``transaction_id`` in their arguments operate against the same
  in-memory tree without touching disk.
* :func:`commit_editing_transaction` — lint the in-memory tree;
  abort on any error-severity issue; clear the in-transaction flag;
  call ``doc.save()`` once (atomic .tmp + rename); deregister.
* :func:`cancel_editing_transaction` — deregister without saving.
  The in-memory tree is GC'd; the on-disk file is unchanged.

These are thin shims over :mod:`pbirb_mcp.core.transactions` (the
pure-data registry) and :func:`pbirb_mcp.ops.lint._lint_doc` (the
already-open-doc lint entry point added in v0.4 commit 12).

See also :func:`apply_edits` (v0.4 commit 11) for the single-call
atomic-batch flavour.
"""

from __future__ import annotations

from typing import Any

from pbirb_mcp.core import transactions as _registry
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.ops.lint import _lint_doc


def start_editing_transaction(path: str) -> dict[str, Any]:
    """Open ``path`` and start an editing transaction.

    Returns ``{transaction_id, path, expires_at}``. ``expires_at`` is
    a unix timestamp (controlled by ``PBIRB_MCP_TRANSACTION_TIMEOUT_S``,
    default 600s). Pass ``transaction_id`` to subsequent edit tools to
    operate against the live in-memory tree.

    Refuses with :class:`ValueError` when an active transaction
    already owns the same canonical abspath.
    """
    doc = RDLDocument.open(path)
    tx_id = _registry.register(doc)
    tx = _registry.lookup_by_id(tx_id)
    assert tx is not None  # noqa: S101 — defensive sanity
    return {
        "transaction_id": tx_id,
        "path": tx.abspath,
        "expires_at": tx.expires_at,
    }


def commit_editing_transaction(transaction_id: str) -> dict[str, Any]:
    """Lint the in-memory tree, save once, deregister.

    Aborts (without saving) if lint surfaces any ``severity=='error'``
    issue — the caller can fix the offending state via additional
    transaction-aware edits and re-commit. Warnings do not abort.

    Returns ``{transaction_id, path, saved, verify}``. ``verify``
    mirrors the shape :func:`pbirb_mcp.ops.validate.verify_report`
    returns: ``{valid, issues, rules_run}``. On lint-error abort,
    ``saved`` is ``False`` and ``verify.valid`` is ``False``.

    Raises :class:`ValueError` ``TransactionError``-style when the
    transaction id is unknown or already expired.
    """
    tx = _registry.lookup_by_id(transaction_id)
    if tx is None:
        raise ValueError(
            "transaction is unknown, expired, or already committed; "
            "call start_editing_transaction to open a fresh one"
        )

    lint_result = _lint_doc(tx.doc)
    has_error = any(i.get("severity") == "error" for i in lint_result["issues"])
    if has_error:
        # Do NOT save. Leave the transaction open so the caller can fix
        # the offending state via additional transaction-aware calls
        # and re-commit. Return shape matches the success path so the
        # LLM can reason about it identically.
        return {
            "transaction_id": transaction_id,
            "path": tx.abspath,
            "saved": False,
            "verify": {"valid": False, **lint_result},
        }

    # Clear the flag so save_as actually writes; commit() then
    # deregisters from the registry.
    tx.doc._in_transaction = False
    tx.doc.save()
    _registry.commit(transaction_id)
    return {
        "transaction_id": transaction_id,
        "path": tx.abspath,
        "saved": True,
        "verify": {"valid": True, **lint_result},
    }


def cancel_editing_transaction(transaction_id: str) -> dict[str, Any]:
    """Discard a transaction. Disk file untouched; in-memory tree GC'd.

    Returns ``{transaction_id, path, discarded}``. ``path`` is included
    so the caller can confirm which file was being edited.

    Raises :class:`ValueError` when the transaction id is unknown.
    """
    tx = _registry.lookup_by_id(transaction_id)
    if tx is None:
        raise ValueError("transaction is unknown, expired, or already committed")
    abspath = tx.abspath
    _registry.cancel(transaction_id)
    return {
        "transaction_id": transaction_id,
        "path": abspath,
        "discarded": True,
    }


def _build_server():
    """Lazy import to avoid a circular dep at module load time. Same
    pattern as :func:`pbirb_mcp.ops.dry_run._build_server`."""
    from pbirb_mcp.server import MCPServer
    from pbirb_mcp.tools import register_all_tools

    server = MCPServer()
    register_all_tools(server)
    return server


def apply_edits(path: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Atomic batch: open once → apply ops → lint → save once.

    Opens an internal transaction against ``path``, dispatches each
    ``{tool, args}`` op through the JSON-RPC ``tools/call`` path with
    ``transaction_id`` injected, and commits at the end. On any op
    failure, cancels the transaction — the disk file is untouched
    because no save happened. On lint-error at commit time, also
    cancels (same outcome: zero disk mutation).

    Compared with :func:`pbirb_mcp.ops.dry_run.dry_run_edit`:

    * ``dry_run_edit`` clones to a tempfile, dispatches against it,
      computes a diff, and DISCARDS — the real file is never touched
      either way. Use it to preview a plan.
    * ``apply_edits`` opens a transaction against the REAL file and
      commits on success. Same atomicity guarantee (zero disk
      mutation on failure), but the success path lands the changes.

    Returns ``{applied: [...], verify: {...}, committed: bool}`` where
    ``applied`` mirrors ``dry_run_edit``'s shape (per-op
    ``{tool, ok, result|error}``) so an LLM can read both side by side.
    """
    if not isinstance(ops, list):
        raise ValueError("ops must be a list of {tool, args} entries")
    from pathlib import Path as _Path  # noqa: N814 — local alias

    src = _Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"path {path!r} is not a regular file")

    server = _build_server()

    # Start the transaction by driving the public tool — exercises the
    # same code path the LLM does.
    start_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "tools/call",
            "params": {
                "name": "start_editing_transaction",
                "arguments": {"path": str(src)},
            },
        }
    )
    if "error" in start_resp or start_resp["result"].get("isError"):
        # Surface the failure with the canonical apply_edits shape.
        return {
            "applied": [],
            "committed": False,
            "verify": {"valid": False, "issues": [], "rules_run": []},
            "error": _extract_payload(start_resp),
        }
    tx_state = _extract_payload(start_resp)
    tx_id = tx_state["transaction_id"]

    applied: list[dict[str, Any]] = []
    failed = False

    for i, op in enumerate(ops):
        tool_name = op.get("tool")
        args = dict(op.get("args") or {})
        if not isinstance(tool_name, str):
            applied.append(
                {
                    "tool": tool_name,
                    "ok": False,
                    "error": {
                        "error_type": "ValueError",
                        "message": f"op #{i} missing string `tool`",
                    },
                }
            )
            failed = True
            break
        # Strip caller-supplied path / transaction_id — the dispatcher
        # injects the registered abspath from the transaction, and
        # transaction_id is what tells it to do so.
        args.pop("path", None)
        args["transaction_id"] = tx_id

        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": i + 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            }
        )
        if "error" in resp:
            applied.append(
                {
                    "tool": tool_name,
                    "ok": False,
                    "error": {
                        "error_type": "JSONRPCError",
                        "message": resp["error"].get("message", "unknown"),
                        "code": resp["error"].get("code"),
                    },
                }
            )
            failed = True
            break
        result = resp.get("result", {}) or {}
        if result.get("isError"):
            applied.append(
                {
                    "tool": tool_name,
                    "ok": False,
                    "error": _extract_payload(resp),
                }
            )
            failed = True
            break
        applied.append({"tool": tool_name, "ok": True, "result": _extract_payload(resp)})

    if failed:
        # Roll back via the public cancel tool. Disk untouched because
        # no save happened during the transaction.
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 9999,
                "method": "tools/call",
                "params": {
                    "name": "cancel_editing_transaction",
                    "arguments": {"transaction_id": tx_id},
                },
            }
        )
        return {
            "applied": applied,
            "committed": False,
            "verify": {"valid": False, "issues": [], "rules_run": []},
        }

    # All ops succeeded — commit. The commit tool itself runs lint;
    # if it returns saved=False (lint error), it leaves the tx open,
    # but we cancel here to honour the atomicity contract: a single
    # apply_edits call either commits everything or nothing.
    commit_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 10_000,
            "method": "tools/call",
            "params": {
                "name": "commit_editing_transaction",
                "arguments": {"transaction_id": tx_id},
            },
        }
    )
    commit_payload = _extract_payload(commit_resp)
    if commit_resp["result"].get("isError") or not commit_payload.get("saved"):
        # Lint failed at commit time — cancel and report.
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 10_001,
                "method": "tools/call",
                "params": {
                    "name": "cancel_editing_transaction",
                    "arguments": {"transaction_id": tx_id},
                },
            }
        )
        return {
            "applied": applied,
            "committed": False,
            "verify": commit_payload.get("verify", {"valid": False, "issues": [], "rules_run": []}),
        }

    return {
        "applied": applied,
        "committed": True,
        "verify": commit_payload["verify"],
    }


def _extract_payload(resp: dict[str, Any]) -> dict[str, Any]:
    """Pull the JSON-decoded text payload out of a tools/call response.
    Falls back to ``{}`` on unexpected shapes."""
    try:
        text = resp["result"]["content"][0]["text"]
        import json as _json

        return _json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


__all__ = [
    "apply_edits",
    "cancel_editing_transaction",
    "commit_editing_transaction",
    "start_editing_transaction",
]
