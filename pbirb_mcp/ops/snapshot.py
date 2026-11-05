"""Backups: ``backup_report``, ``list_backups``, ``restore_from_backup``
and the ``PBIRB_MCP_AUTO_BACKUP`` hook (#63).

``backup_report(path, dest_dir=None, label=None)`` writes a copy of the
report to ``<dest_dir or report dir>/<name>.bak.<UTC-ts>[-<n>][.<label>]``
plus a metadata sidecar ``<backup>.meta.json`` —
``{source, sha1, size_bytes, source_mtime_ns, label, created, reason,
in_transaction}``. Inside an editing transaction the backup holds the
transaction's in-memory tree (what the caller is editing), not the stale
bytes on disk.

``restore_from_backup`` copies a backup back over its report:

* the target is the sidecar's ``source`` (so a backup in a ``dest_dir``
  still finds its report), else the name the backup implies, else
  ``target_path`` must be given;
* the backup must parse as XML (always) and be a structurally valid
  report (unless ``force``) — junk is never copied over a report;
* a backup recorded for a *different* report is refused unless ``force``;
* the target's current state is backed up first (``safety_backup``), so a
  restore can itself be undone. That safety copy replaces the old "target
  is newer than the backup" guard, which refused the main use case
  (backup → bad edit → restore) every time;
* a path held by an editing transaction is refused
  (``TransactionConflictError``); the write goes through
  :func:`~pbirb_mcp.core.document.write_atomic` (symlinks, mode,
  read-only — #64).

Auto-backup: with ``PBIRB_MCP_AUTO_BACKUP`` truthy (``1`` / ``true`` /
``yes`` / ``y`` / ``on``) the server calls :func:`maybe_auto_backup`
before every tool in ``AUTO_BACKUP_TOOLS`` (removals, renames, query and
field rewrites, raw XML replace, convert_to_matrix — see
:mod:`pbirb_mcp.server`) and adds the backup path to the result as
``auto_backup``. An auto-backup is skipped when the newest backup of the
report already holds the same bytes, so repeats and every op of one
transaction (whose file on disk doesn't change until commit) share one
backup: the state before the edits.

Timestamps are UTC ``YYYYMMDD-HHMMSS``; a same-second collision gets a
``-<n>`` suffix. Backups keep the report's permission bits; their mtime
is the time of the backup. Retention is the caller's: nothing is rotated
or deleted.
"""

from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import security
from pbirb_mcp.core.document import refuse_read_only, write_atomic
from pbirb_mcp.core.schema import RDLValidationError, validate_structure

_TRUTHY = frozenset({"1", "true", "yes", "y", "on"})

# Backup file names: ``<target>.bak.<UTC-ts>`` plus ``-<n>`` for a
# same-second collision, plus ``.<label>``. ``target`` is the original
# report's file name.
_BACKUP_NAME = re.compile(
    r"^(?P<target>.+)\.bak\.(?P<ts>\d{8}-\d{6})(?:-(?P<n>\d+))?(?:\.(?P<label>[A-Za-z0-9_-]+))?$"
)
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_META_SUFFIX = ".meta.json"

_XML_PARSER = etree.XMLParser(
    remove_blank_text=False,
    resolve_entities=False,
    no_network=True,
    huge_tree=True,
)


def _is_auto_backup_enabled() -> bool:
    return os.environ.get("PBIRB_MCP_AUTO_BACKUP", "").strip().lower() in _TRUTHY


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()  # noqa: S324 — content identity, not security


def _meta_path(backup: Path) -> Path:
    return backup.with_name(backup.name + _META_SUFFIX)


def _read_meta(backup: Path) -> Optional[dict]:
    try:
        meta = json.loads(_meta_path(backup).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _validate_label(label: Optional[str]) -> None:
    if label is not None and not _LABEL.match(label):
        raise ValueError(
            f"label {label!r} must be 1-64 letters, digits, '-' or '_' "
            "(starting with a letter or digit)"
        )


def _next_backup_path(directory: Path, name: str, label: Optional[str]) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = f".{label}" if label else ""
    n = 0
    while True:
        base = f"{name}.bak.{stamp}" + (f"-{n}" if n else "")
        # A same-second backup with another label still collides in sort
        # order; give every backup of that second its own number.
        taken = (directory / base).exists() or any(directory.glob(glob.escape(base) + ".*"))
        if not taken:
            return directory / (base + suffix)
        n += 1


def _source_bytes(src: Path) -> tuple[bytes, bool]:
    """The report's bytes, and whether they come from the editing
    transaction this call runs under (its in-memory tree)."""
    from pbirb_mcp.core import transactions as _tx

    tx = _tx.owned_transaction(str(src.resolve()))
    if tx is not None:
        return tx.doc.to_bytes(), True
    return src.read_bytes(), False


def _write_backup(
    src: Path,
    data: bytes,
    *,
    dest_dir: Optional[str],
    label: Optional[str],
    reason: str,
    in_transaction: bool,
) -> dict[str, Any]:
    _validate_label(label)
    if dest_dir is not None:
        directory = Path(dest_dir)
        if not directory.is_dir():
            raise FileNotFoundError(f"dest_dir {dest_dir!r} is not an existing directory")
    else:
        directory = src.parent
    dst = _next_backup_path(directory, src.name, label)
    st = os.stat(src)
    # Same permission bits as the report (a private report stays private),
    # but always owner-writable so the backup can be managed.
    mode = stat.S_IMODE(st.st_mode) | stat.S_IWUSR
    created = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    meta = {
        "source": os.path.abspath(src),
        "sha1": _sha1(data),
        "size_bytes": len(data),
        "source_mtime_ns": st.st_mtime_ns,
        "label": label,
        "created": created,
        "reason": reason,
        "in_transaction": in_transaction,
    }
    write_atomic(dst, data, mode=mode)
    write_atomic(_meta_path(dst), json.dumps(meta, indent=2).encode("utf-8") + b"\n", mode=mode)
    return {
        "source": str(src),
        "backup": str(dst),
        "size_bytes": len(data),
        "label": label,
        "created": created,
        "in_transaction": in_transaction,
    }


def backup_report(
    path: str,
    dest_dir: Optional[str] = None,
    label: Optional[str] = None,
) -> dict[str, Any]:
    """Copy ``path`` to ``<dest_dir>/<name>.bak.<UTC-ts>[.<label>]``.

    ``dest_dir`` defaults to the report's directory and must exist.
    ``label`` (1-64 letters, digits, ``-``, ``_``) is appended to the
    name. Inside an editing transaction (a call routed with its
    ``transaction_id``) the backup holds the in-transaction tree.

    Returns ``{source, backup, size_bytes, label, created,
    in_transaction}``; the report is unchanged.
    """
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(path)
    data, in_tx = _source_bytes(src)
    return _write_backup(
        src, data, dest_dir=dest_dir, label=label, reason="manual", in_transaction=in_tx
    )


def _backups_of(src: Path, directory: Path) -> list[tuple[tuple, Path, Optional[dict]]]:
    """``(sort_key, backup, meta)`` for every backup of ``src`` in
    ``directory``, oldest first. A backup with a sidecar must name
    ``src`` as its source; one without counts only next to the report."""
    real_src = os.path.realpath(src)
    found = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        m = _BACKUP_NAME.match(entry.name)
        if m is None or m.group("target") != src.name or not entry.is_file():
            continue
        meta = _read_meta(entry)
        if meta is not None and isinstance(meta.get("source"), str):
            if os.path.realpath(meta["source"]) != real_src:
                continue
        elif os.path.realpath(directory) != os.path.dirname(real_src):
            continue
        key = (m.group("ts"), int(m.group("n") or 0))
        found.append((key, entry, meta))
    found.sort(key=lambda item: item[0])
    return found


def list_backups(path: str, dest_dir: Optional[str] = None) -> dict[str, Any]:
    """The backups of ``path`` (in its directory, or ``dest_dir``), newest
    first: ``{path, backups: [{backup, created, label, size_bytes, reason,
    same_as_report}]}``. ``same_as_report`` — the backup holds exactly the
    report's current bytes."""
    src = Path(path)
    directory = Path(dest_dir) if dest_dir is not None else src.parent
    if dest_dir is not None and not directory.is_dir():
        raise FileNotFoundError(f"dest_dir {dest_dir!r} is not an existing directory")
    current = _sha1(src.read_bytes()) if src.is_file() else None
    out = []
    for (ts, _n), backup, meta in reversed(_backups_of(src, directory)):
        meta = meta or {}
        data_sha = meta.get("sha1")
        if not isinstance(data_sha, str):
            data_sha = _sha1(backup.read_bytes())
        m = _BACKUP_NAME.match(backup.name)
        out.append(
            {
                "backup": str(backup),
                "created": meta.get("created") or _ts_to_iso(ts),
                "label": meta.get("label", m.group("label") if m else None),
                "size_bytes": backup.stat().st_size,
                "reason": meta.get("reason"),
                "same_as_report": data_sha == current,
            }
        )
    return {"path": str(src), "backups": out}


def _ts_to_iso(ts: str) -> str:
    moment = datetime.datetime.strptime(ts, "%Y%m%d-%H%M%S")
    return moment.replace(tzinfo=datetime.timezone.utc).isoformat(timespec="seconds")


def _derive_target_from_backup(backup: Path) -> Optional[Path]:
    """The report a backup belongs to: its sidecar's ``source``, else the
    path its ``<target>.bak.<UTC-ts>`` name implies next to it, else
    ``None`` (callers must pass ``target_path``)."""
    meta = _read_meta(backup)
    if meta is not None and isinstance(meta.get("source"), str):
        return Path(meta["source"])
    m = _BACKUP_NAME.match(backup.name)
    if m is None:
        return None
    return backup.with_name(m.group("target"))


def _check_backup_content(data: bytes, backup: Path, force: bool) -> None:
    try:
        tree = etree.ElementTree(etree.fromstring(data, _XML_PARSER))
    except etree.XMLSyntaxError as exc:
        raise ValueError(
            f"backup {str(backup)!r} is not well-formed XML ({exc}); refusing to restore it"
        ) from None
    if force:
        return
    try:
        validate_structure(tree)
    except RDLValidationError as exc:
        raise ValueError(
            f"backup {str(backup)!r} is not a valid report ({exc}); pass force=true to "
            "restore it anyway"
        ) from None


def restore_from_backup(
    backup_path: str,
    target_path: Optional[str] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Copy a backup back over its report.

    ``target_path`` defaults to the report the backup's sidecar records,
    else the path its ``<target>.bak.<UTC-ts>`` name implies; when
    neither is known it must be given.

    Refuses (``ValueError``) a backup that isn't well-formed XML; unless
    ``force``, also one that isn't a structurally valid report, or one
    recorded for a different report than the target. Before writing,
    backs up the target's current state (``safety_backup``; ``None`` when
    there was no target, or a backup already held those bytes). A target
    held by an editing transaction is refused (TransactionConflictError).

    Returns ``{source, restored_to, bytes_restored, safety_backup,
    changed}``; ``changed`` is ``False`` (nothing written) when the
    target already holds the backup's bytes.
    """
    src = Path(backup_path)
    if not src.is_file():
        raise FileNotFoundError(backup_path)

    if target_path is not None:
        dst = Path(target_path)
    else:
        derived = _derive_target_from_backup(src)
        if derived is None:
            raise ValueError(
                f"cannot derive target_path from backup filename {src.name!r}; "
                "pass target_path= explicitly"
            )
        dst = derived
        # The server sandboxed the arguments; a target read from the
        # sidecar must stay inside PBIRB_MCP_ALLOWED_ROOTS too (#57).
        security.check_paths({"target_path": str(dst)})

    # A held path may only change through its transaction's commit —
    # otherwise the commit would silently overwrite the restore.
    from pbirb_mcp.core import transactions as _tx

    _tx.check_writable(str(dst.resolve()), None)

    data = src.read_bytes()
    _check_backup_content(data, src, force)
    meta = _read_meta(src)
    recorded = meta.get("source") if meta else None
    if (
        not force
        and isinstance(recorded, str)
        and dst.exists()
        and os.path.realpath(recorded) != os.path.realpath(dst)
    ):
        raise ValueError(
            f"backup {str(src)!r} was taken of {recorded!r}, not {str(dst)!r}; pass "
            "force=true to restore it over a different report"
        )

    result = {
        "source": str(src),
        "restored_to": str(dst),
        "bytes_restored": len(data),
        "safety_backup": None,
        "changed": True,
    }
    if dst.is_file():
        current = dst.read_bytes()
        if current == data:
            result["changed"] = False
            return result
        refuse_read_only(dst)
        safety = _auto_backup(dst, current, reason="pre-restore")
        result["safety_backup"] = safety["backup"] if safety else None
    write_atomic(dst, data)
    return result


def _auto_backup(src: Path, data: bytes, *, reason: str) -> Optional[dict[str, Any]]:
    """Back up ``data`` (the bytes of ``src``) unless the newest backup of
    ``src`` next to it already holds them; returns that backup then."""
    existing = _backups_of(src, src.parent)
    if existing:
        _key, newest, meta = existing[-1]
        sha = meta.get("sha1") if meta else None
        if sha == _sha1(data) or (sha is None and newest.read_bytes() == data):
            return {"source": str(src), "backup": str(newest), "reused": True}
    out = _write_backup(src, data, dest_dir=None, label=None, reason=reason, in_transaction=False)
    out["reused"] = False
    return out


def maybe_auto_backup(path: str) -> Optional[dict[str, Any]]:
    """Back up ``path`` before a destructive edit when
    ``PBIRB_MCP_AUTO_BACKUP`` is on; ``None`` when it's off or there is
    no such file.

    Backs up the bytes on disk — inside a transaction that is the state
    before the transaction, since nothing reaches disk until commit — and
    reuses the newest backup when it already holds them (so a transaction
    or a repeated call makes one backup). The descriptor's ``reused``
    says which.
    """
    if not _is_auto_backup_enabled():
        return None
    src = Path(path)
    if not src.is_file():
        return None
    return _auto_backup(src, src.read_bytes(), reason="auto")


__all__ = [
    "backup_report",
    "list_backups",
    "maybe_auto_backup",
    "restore_from_backup",
]
