"""Tests for backup_report (v0.2 commit 14) and restore_from_backup (v0.4)."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from pbirb_mcp.ops.snapshot import (
    _is_auto_backup_enabled,
    backup_report,
    maybe_auto_backup,
    restore_from_backup,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


class TestBackupReport:
    def test_creates_a_copy_with_timestamp_suffix(self, rdl_path):
        result = backup_report(path=str(rdl_path))
        backup = Path(result["backup"])
        assert backup.exists()
        assert backup.name.startswith(rdl_path.name + ".bak.")
        # Original unchanged.
        assert rdl_path.exists()
        assert backup.read_bytes() == rdl_path.read_bytes()
        assert result["size_bytes"] == backup.stat().st_size

    def test_collision_disambiguates_with_suffix(self, rdl_path):
        first = Path(backup_report(path=str(rdl_path))["backup"])
        # Make the timestamp predictable by forcing a same-second second call.
        second = Path(backup_report(path=str(rdl_path))["backup"])
        assert first != second
        assert first.exists() and second.exists()

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            backup_report(path=str(tmp_path / "ghost.rdl"))


class TestMaybeAutoBackup:
    def test_disabled_by_default(self, rdl_path, monkeypatch):
        monkeypatch.delenv("PBIRB_MCP_AUTO_BACKUP", raising=False)
        assert maybe_auto_backup(path=str(rdl_path)) is None
        assert not _is_auto_backup_enabled()

    def test_enabled_when_env_truthy(self, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        assert _is_auto_backup_enabled()
        result = maybe_auto_backup(path=str(rdl_path))
        assert result is not None
        assert Path(result["backup"]).exists()

    def test_disabled_for_falsy_string(self, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "0")
        assert maybe_auto_backup(path=str(rdl_path)) is None

    @pytest.mark.parametrize("value", ["true", "TRUE", "yes", "Y", "ON"])
    def test_truthy_variants(self, rdl_path, monkeypatch, value):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", value)
        assert maybe_auto_backup(path=str(rdl_path)) is not None


class TestRestoreFromBackup:
    """v0.4 — closes the snapshot loop. backup_report writes
    ``<path>.bak.<UTC-ts>``; restore_from_backup is the symmetric
    operation. Refuses if the target mtime > backup mtime (staleness
    check — without force=True, you'd silently overwrite NEWER state
    with an OLDER snapshot)."""

    def test_round_trip_restores_byte_identical(self, rdl_path):
        original_bytes = rdl_path.read_bytes()
        backup = Path(backup_report(path=str(rdl_path))["backup"])
        # Mutate the target so restore has work to do.
        rdl_path.write_bytes(original_bytes + b"\n<!-- tampered -->")
        # Roll the target's mtime BACK so backup looks newer than target
        # (the legitimate restore case).
        target_old = time.time() - 600
        os.utime(rdl_path, (target_old, target_old))
        result = restore_from_backup(backup_path=str(backup))
        assert result["source"] == str(backup)
        assert result["restored_to"] == str(rdl_path)
        assert result["bytes_restored"] == len(original_bytes)
        # Bytes match the original.
        assert rdl_path.read_bytes() == original_bytes

    def test_target_path_derived_from_backup_filename(self, rdl_path):
        backup = Path(backup_report(path=str(rdl_path))["backup"])
        # Without explicit target_path, the tool peels .bak.<ts> off
        # the backup name to recover the target.
        rdl_path.unlink()
        result = restore_from_backup(backup_path=str(backup))
        assert result["restored_to"] == str(rdl_path)
        assert rdl_path.exists()

    def test_explicit_target_path_overrides_derivation(self, rdl_path, tmp_path):
        backup = Path(backup_report(path=str(rdl_path))["backup"])
        dst = tmp_path / "elsewhere.rdl"
        result = restore_from_backup(backup_path=str(backup), target_path=str(dst))
        assert result["restored_to"] == str(dst)
        assert dst.exists()
        # Original still in place.
        assert rdl_path.exists()

    def test_refuses_when_target_newer_than_backup(self, rdl_path):
        backup = Path(backup_report(path=str(rdl_path))["backup"])
        # Backup mtime in the past, target mtime now → staleness.
        old = time.time() - 600
        os.utime(backup, (old, old))
        with pytest.raises(ValueError, match="newer than backup"):
            restore_from_backup(backup_path=str(backup))

    def test_force_overrides_staleness_check(self, rdl_path):
        original_bytes = rdl_path.read_bytes()
        backup = Path(backup_report(path=str(rdl_path))["backup"])
        rdl_path.write_bytes(b"<tampered/>")
        old = time.time() - 600
        os.utime(backup, (old, old))
        result = restore_from_backup(backup_path=str(backup), force=True)
        assert result["restored_to"] == str(rdl_path)
        assert rdl_path.read_bytes() == original_bytes

    def test_missing_backup_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            restore_from_backup(backup_path=str(tmp_path / "ghost.bak.20260101-000000"))

    def test_undeducible_target_requires_explicit_target_path(self, tmp_path):
        # A backup filename without the .bak.<ts> shape can't yield a
        # target via derivation — refuse with a clear error rather than
        # silently overwriting something random.
        stray = tmp_path / "random-name.rdl"
        stray.write_bytes(b"x")
        with pytest.raises(ValueError, match="target_path"):
            restore_from_backup(backup_path=str(stray))

    def test_round_trip_returns_canonical_shape(self, rdl_path):
        backup = Path(backup_report(path=str(rdl_path))["backup"])
        # Make target older so the staleness guard doesn't fire.
        os.utime(rdl_path, (time.time() - 600,) * 2)
        result = restore_from_backup(backup_path=str(backup))
        assert set(result.keys()) == {"source", "restored_to", "bytes_restored"}


class TestToolRegistration:
    def test_tool_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "backup_report" in server._tools

    def test_restore_tool_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "restore_from_backup" in server._tools
