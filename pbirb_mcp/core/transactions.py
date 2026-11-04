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
import contextvars
import hashlib
import os
import time
import uuid
from collections.abc import Iterator
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
    # (st_dev, st_ino) of the file at start — a hardlink is the same file.
    inode: Optional[tuple] = None
    # File fingerprint at start; commit refuses when the file changed.
    fingerprint: Optional[tuple] = None


_BY_ID: dict[str, _Transaction] = {}
_BY_PATH: dict[str, str] = {}  # abspath → transaction_id
_BY_INODE: dict[tuple, str] = {}  # (st_dev, st_ino) → transaction_id


def _inode_of(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def file_fingerprint(path: str) -> Optional[tuple]:
    """``(st_dev, st_ino, st_mtime_ns, st_size, sha1)`` of ``path``, or
    ``None`` when it can't be read."""
    try:
        st = os.stat(path)
        with open(path, "rb") as fh:
            digest = hashlib.sha1(fh.read()).hexdigest()  # noqa: S324 — change detection only
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size, digest)


# The transaction the current tool call was routed through (set by the
# dispatcher only when the call carried a valid ``transaction_id``).
# :meth:`RDLDocument.open` hands out a transaction's live tree only to
# calls running under that transaction's id; everyone else reads the
# on-disk file and is refused on save (see :func:`check_writable`).
_ACTIVE_TX: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "pbirb_active_transaction", default=None
)


class TransactionConflictError(ValueError):
    """A write targeted a path held by an open transaction the call
    wasn't routed through."""


@contextlib.contextmanager
def activate(transaction_id: Optional[str]) -> Iterator[None]:
    """Run the enclosed block as part of ``transaction_id``."""
    token = _ACTIVE_TX.set(transaction_id)
    try:
        yield
    finally:
        _ACTIVE_TX.reset(token)


def active_transaction_id() -> Optional[str]:
    return _ACTIVE_TX.get()


def owned_transaction(abspath: str) -> Optional[_Transaction]:
    """The transaction holding ``abspath`` iff the current call runs
    under it; ``None`` otherwise (not held, or held by someone else)."""
    tx = lookup_by_path(abspath)
    if tx is not None and tx.transaction_id == _ACTIVE_TX.get():
        return tx
    return None


def check_writable(abspath: str, doc: object) -> None:
    """Raise :class:`TransactionConflictError` when ``abspath`` is held
    by an open transaction and ``doc`` isn't that transaction's tree."""
    tx = lookup_by_path(abspath)
    if tx is not None and tx.doc is not doc:
        raise TransactionConflictError(
            f"{abspath!r} is held by an open editing transaction; pass its "
            "transaction_id to edit it, or commit/cancel the transaction first"
        )


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
    inode = _inode_of(abspath)
    existing_id = _BY_PATH.get(abspath)
    if existing_id is None and inode is not None:
        other = _BY_INODE.get(inode)
        # Ignore a recycled inode whose held path is no longer that file.
        if other is not None and _inode_of(_BY_ID[other].abspath) == inode:
            existing_id = other
    if existing_id is not None:
        raise TransactionConflictError(
            f"an active transaction ({existing_id!r}) already owns "
            f"{abspath!r} (or a hardlink to the same file); commit or "
            "cancel it before starting a new one"
        )

    tx_id = uuid.uuid4().hex
    start = now if now is not None else time.time()
    tx = _Transaction(
        transaction_id=tx_id,
        abspath=abspath,
        doc=doc,
        expires_at=start + _timeout_seconds(),
        inode=inode,
        fingerprint=file_fingerprint(abspath),
    )
    _BY_ID[tx_id] = tx
    _BY_PATH[abspath] = tx_id
    if inode is not None:
        _BY_INODE[inode] = tx_id
    # Mark the document as in-transaction so save_as no-ops until commit.
    doc._in_transaction = True
    return tx_id


def lookup_by_id(transaction_id: str) -> Optional[_Transaction]:
    # Expiry is enforced on every lookup, so commit/cancel/edits all see
    # an expired transaction as gone.
    sweep_orphans()
    return _BY_ID.get(transaction_id)


def lookup_by_path(abspath: str) -> Optional[_Transaction]:
    sweep_orphans()
    tx_id = _BY_PATH.get(abspath)
    if tx_id is None and _BY_INODE:
        # A hardlink (or other alias) of a held file is the same file.
        inode = _inode_of(abspath)
        tx_id = _BY_INODE.get(inode) if inode is not None else None
        # Inodes are recycled: only trust the match while the held path
        # still IS that inode (it may have been deleted or replaced).
        if tx_id is not None and _inode_of(_BY_ID[tx_id].abspath) != inode:
            tx_id = None
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
    if tx.inode is not None and _BY_INODE.get(tx.inode) == transaction_id:
        _BY_INODE.pop(tx.inode, None)
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
    _BY_INODE.clear()


__all__ = [
    "TransactionConflictError",
    "_Transaction",
    "activate",
    "active_transaction_id",
    "active_transactions",
    "check_writable",
    "file_fingerprint",
    "cancel",
    "commit",
    "lookup_by_id",
    "lookup_by_path",
    "owned_transaction",
    "register",
    "sweep_orphans",
]
