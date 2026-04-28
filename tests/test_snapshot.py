"""Tests for backup_report (v0.2 commit 14)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.ops.snapshot import (
    _is_auto_backup_enabled,
    backup_report,
    maybe_auto_backup,
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


class TestToolRegistration:
    def test_tool_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "backup_report" in server._tools
