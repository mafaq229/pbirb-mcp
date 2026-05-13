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


__all__ = [
    "cancel_editing_transaction",
    "commit_editing_transaction",
    "start_editing_transaction",
]
