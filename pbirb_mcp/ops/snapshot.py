"""Snapshot tools (v0.2 commit 14).

``backup_report`` makes a copy of the RDL at ``<path>.bak.<timestamp>``
without touching the original. Cheap explicit checkpoint to call before
a destructive batch (remove_*, rename_parameter, etc.). Retention is the
caller's problem — we don't rotate or delete old backups.

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
import shutil
import time
from pathlib import Path
from typing import Any

_TRUTHY = frozenset({"1", "true", "yes", "y", "on"})


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


def maybe_auto_backup(path: str) -> dict[str, Any] | None:
    """Convenience entry point for auto-backup before destructive ops.

    Returns the backup descriptor when ``PBIRB_MCP_AUTO_BACKUP`` is on,
    or ``None`` when it's off. Other ops modules can call this without
    branching on the env var themselves.
    """
    if not _is_auto_backup_enabled():
        return None
    return backup_report(path)


__all__ = ["backup_report", "maybe_auto_backup"]
