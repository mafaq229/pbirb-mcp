"""Snapshot tools (v0.2 commit 14 + v0.4 restore_from_backup).

``backup_report`` makes a copy of the RDL at ``<path>.bak.<timestamp>``
without touching the original. Cheap explicit checkpoint to call before
a destructive batch (remove_*, rename_parameter, etc.). Retention is the
caller's problem — we don't rotate or delete old backups.

``restore_from_backup`` (v0.4) is the symmetric op: copies a
``<path>.bak.<ts>`` file back over the original. Refuses by default if
the target on disk is NEWER than the backup (staleness guard — without
``force=True``, you would silently overwrite fresher state with an
older snapshot).

Implementation notes:

* Uses ``shutil.copy2`` so file metadata (mtime, perms) is preserved.
* Timestamp format is ``YYYYMMDD-HHMMSS`` in UTC for sortability and
  cross-timezone safety.
* Two backups in the same second get a ``-<n>`` suffix so they don't
  collide.

Auto-backup hook: when the env var ``PBIRB_MCP_AUTO_BACKUP`` is set to
a truthy value (``1`` / ``true`` / ``yes``), other ops can call
``maybe_auto_backup(path)`` before destructive work and a snapshot is
taken if the env is on. Default behaviour stays unchanged.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

_TRUTHY = frozenset({"1", "true", "yes", "y", "on"})

# Filenames produced by ``backup_report`` end in ``.bak.<UTC-ts>`` plus
# optionally ``-<n>`` for same-second collisions. The capture group is
# the original target name (everything BEFORE that suffix).
_BACKUP_NAME = re.compile(r"^(?P<target>.+)\.bak\.\d{8}-\d{6}(?:-\d+)?$")


def _is_auto_backup_enabled() -> bool:
    return os.environ.get("PBIRB_MCP_AUTO_BACKUP", "").lower() in _TRUTHY


def _next_backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    candidate = path.with_name(f"{path.name}.bak.{stamp}")
    if not candidate.exists():
        return candidate
    # Same-second collision — disambiguate.
    n = 1
    while True:
        alt = path.with_name(f"{path.name}.bak.{stamp}-{n}")
        if not alt.exists():
            return alt
        n += 1


def backup_report(path: str) -> dict[str, Any]:
    """Copy ``path`` to ``<path>.bak.<UTC-timestamp>``.

    Preserves file metadata via :func:`shutil.copy2`. Returns the
    backup path; the original is unchanged.
    """
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(path)
    dst = _next_backup_path(src)
    shutil.copy2(src, dst)
    return {
        "source": str(src),
        "backup": str(dst),
        "size_bytes": dst.stat().st_size,
    }


def _derive_target_from_backup(backup: Path) -> Optional[Path]:
    """Return the original target path implied by ``backup``'s filename,
    or ``None`` when the backup name doesn't match the canonical
    ``<target>.bak.<UTC-ts>`` shape (callers must pass ``target_path``
    explicitly in that case)."""
    m = _BACKUP_NAME.match(backup.name)
    if m is None:
        return None
    return backup.with_name(m.group("target"))


def restore_from_backup(
    backup_path: str,
    target_path: Optional[str] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Copy a backup file back over its original target.

    ``target_path`` defaults to the path implied by the backup's
    canonical ``<target>.bak.<UTC-ts>`` filename. If the backup name
    doesn't match that shape (e.g. a backup that's been hand-renamed),
    ``target_path`` MUST be supplied explicitly — otherwise we refuse
    rather than guess.

    Refuses with ``ValueError`` when ``target_path`` exists and its
    mtime is NEWER than the backup's: without that guard, restoring an
    older snapshot over fresher state would silently lose work. Pass
    ``force=True`` to override (you're saying "yes I really mean it").

    Returns ``{source, restored_to, bytes_restored}``. ``backup_report``
    returned ``{source, backup, size_bytes}`` — same conceptual shape,
    field names mirrored for the inverse direction.
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

    if not force and dst.exists():
        target_mtime = dst.stat().st_mtime
        backup_mtime = src.stat().st_mtime
        if target_mtime > backup_mtime:
            raise ValueError(
                f"target {str(dst)!r} mtime is newer than backup — refusing to "
                "overwrite newer state with an older snapshot. Pass force=True "
                "to restore anyway."
            )

    shutil.copy2(src, dst)
    return {
        "source": str(src),
        "restored_to": str(dst),
        "bytes_restored": dst.stat().st_size,
    }


def maybe_auto_backup(path: str) -> dict[str, Any] | None:
    """Convenience entry point for auto-backup before destructive ops.

    Returns the backup descriptor when ``PBIRB_MCP_AUTO_BACKUP`` is on,
    or ``None`` when it's off. Other ops modules can call this without
    branching on the env var themselves.
    """
    if not _is_auto_backup_enabled():
        return None
    return backup_report(path)


__all__ = ["backup_report", "maybe_auto_backup", "restore_from_backup"]
