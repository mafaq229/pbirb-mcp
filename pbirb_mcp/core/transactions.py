"""Transaction registry for v0.4 editing transactions.

Pure-data module: a process-wide registry of in-memory
:class:`RDLDocument` trees that the dispatcher and the
``start/commit/cancel_editing_transaction`` tools (v0.4 commit 10)
share. No dependency on :mod:`pbirb_mcp.server` — kept pure-unit-
testable so the registry's lookup/expire/cancel semantics can be
verified without spinning up the JSON-RPC dispatch path.

Lifecycle (full picture documented in ``docs/TRANSACTIONS.md`` —
v0.4 commit 24):

* ``start_editing_transaction(path)`` opens the doc, marks it
  ``_in_transaction = True``, calls :func:`register`, returns the
  ``transaction_id``.
* Subsequent edit-tool calls that carry ``transaction_id`` get
  routed through the dispatcher (v0.4 commit 9): the dispatcher
  pops ``transaction_id`` from the JSON-RPC ``arguments``, looks
  up the transaction via :func:`lookup_by_id`, substitutes
  ``arguments["path"]`` with the registered abspath, and dispatches
  the handler unchanged. Because :meth:`RDLDocument.open` is
  intercepted (v0.4 commit 8) to consult :func:`lookup_by_path`,
  the handler reuses the live in-memory tree instead of parsing
  from disk; :meth:`RDLDocument.save_as` is a no-op while the
  ``_in_transaction`` flag is set, so intermediate edits never hit
  disk.
* ``commit_editing_transaction`` runs lint, clears the flag, calls
  ``doc.save()`` once, and :func:`commit` removes the entry.
* ``cancel_editing_transaction`` calls :func:`cancel` and lets the
  in-memory tree be garbage-collected.
* Orphans (no commit / cancel within ``PBIRB_MCP_TRANSACTION_TIMEOUT_S``
  seconds — default 600) are removed lazily by :func:`sweep_orphans`
  on every dispatcher call. No background thread.

Concurrency: the stdio MCP server processes one request at a time
by protocol, so the registry is a plain dict — no locks. If anyone
ever runs handlers in threads, the in-memory tree shared inside a
transaction would need its own synchronisation; we'd cross that
bridge when it comes.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pbirb_mcp.core.document import RDLDocument


_DEFAULT_TIMEOUT_S = 600
_TIMEOUT_ENV = "PBIRB_MCP_TRANSACTION_TIMEOUT_S"


@dataclass
class _Transaction:
    transaction_id: str
    abspath: str  # the canonical resolve()d string — the registry key
    doc: RDLDocument
    expires_at: float  # unix timestamp


_BY_ID: dict[str, _Transaction] = {}
_BY_PATH: dict[str, str] = {}  # abspath → transaction_id


def _timeout_seconds() -> int:
    raw = os.environ.get(_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_TIMEOUT_S
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def register(doc: RDLDocument, *, now: Optional[float] = None) -> str:
    """Open a transaction for ``doc``. Returns the new ``transaction_id``.

    Refuses with :class:`ValueError` when an active transaction already
    owns the same abspath. Callers (``start_editing_transaction``)
    surface that as a structured ``TransactionConflictError``.
    """
    from pbirb_mcp.core.document import RDLDocument  # local import: circular at module load.

    if not isinstance(doc, RDLDocument):  # defensive — handler-driven
        raise TypeError("register() requires an RDLDocument")

    abspath = str(doc.path.resolve())
    if abspath in _BY_PATH:
        existing_id = _BY_PATH[abspath]
        raise ValueError(
            f"an active transaction ({existing_id!r}) already owns "
            f"{abspath!r}; commit or cancel it before starting a new one"
        )

    tx_id = uuid.uuid4().hex
    start = now if now is not None else time.time()
    tx = _Transaction(
        transaction_id=tx_id,
        abspath=abspath,
        doc=doc,
        expires_at=start + _timeout_seconds(),
    )
    _BY_ID[tx_id] = tx
    _BY_PATH[abspath] = tx_id
    # Mark the document as in-transaction so save_as no-ops until commit.
    doc._in_transaction = True
    return tx_id


def lookup_by_id(transaction_id: str) -> Optional[_Transaction]:
    return _BY_ID.get(transaction_id)


def lookup_by_path(abspath: str) -> Optional[_Transaction]:
    tx_id = _BY_PATH.get(abspath)
    if tx_id is None:
        return None
    return _BY_ID.get(tx_id)


def cancel(transaction_id: str) -> Optional[_Transaction]:
    """Remove a transaction without saving. Returns the removed
    :class:`_Transaction` (with its in-memory doc) if it existed,
    otherwise ``None``. Clears the ``_in_transaction`` flag so the
    document — if held elsewhere — can save normally again.
    """
    tx = _BY_ID.pop(transaction_id, None)
    if tx is None:
        return None
    _BY_PATH.pop(tx.abspath, None)
    # Defensive: clear the flag so any stray reference to the doc can
    # save normally again.
    with contextlib.suppress(AttributeError):
        tx.doc._in_transaction = False
    return tx


def commit(transaction_id: str) -> Optional[_Transaction]:
    """Same as :func:`cancel` from the registry's perspective —
    deregister + clear the flag. The caller is responsible for
    calling ``doc.save()`` between clearing the flag and deregistering;
    we don't save here so the registry stays pure-data and unit-testable
    without filesystem side effects.
    """
    return cancel(transaction_id)


def sweep_orphans(now: Optional[float] = None) -> list[str]:
    """Expire transactions whose ``expires_at`` is in the past.
    Returns the list of expired transaction ids (for tests / logging).

    O(active-transactions). Cheap to call from the dispatcher on every
    transaction-aware tool call.
    """
    cutoff = now if now is not None else time.time()
    expired = [tx_id for tx_id, tx in _BY_ID.items() if tx.expires_at <= cutoff]
    for tx_id in expired:
        cancel(tx_id)
    return expired


def active_transactions() -> list[str]:
    """Read-only inventory — convenience for tests and `describe_report`
    extensions. Returns ids of currently-active transactions."""
    return list(_BY_ID.keys())


def _reset_for_tests() -> None:
    """Test-only escape hatch. The registry is process-wide module
    state; tests must reset it between runs or earlier failures leak
    across test cases."""
    _BY_ID.clear()
    _BY_PATH.clear()


__all__ = [
    "_Transaction",
    "active_transactions",
    "cancel",
    "commit",
    "lookup_by_id",
    "lookup_by_path",
    "register",
    "sweep_orphans",
]
