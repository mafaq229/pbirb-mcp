"""Tests for backup_report, list_backups, restore_from_backup and PBIRB_MCP_AUTO_BACKUP (#63)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from pbirb_mcp.ops.snapshot import (
    _BACKUP_NAME,
    _is_auto_backup_enabled,
    backup_report,
    list_backups,
    maybe_auto_backup,
    restore_from_backup,
)
from pbirb_mcp.ops.styling import set_textbox_value
from pbirb_mcp.ops.validate import validate_report
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

    def test_target_newer_than_backup_is_restored_after_a_safety_backup(self, rdl_path):
        # #63: the old "target is newer" guard refused the main use case
        # (backup -> bad edit -> restore). The current state is backed up
        # first instead, so nothing is lost.
        original_bytes = rdl_path.read_bytes()
        backup = Path(backup_report(path=str(rdl_path))["backup"])
        old = time.time() - 600
        os.utime(backup, (old, old))
        rdl_path.write_bytes(original_bytes.replace(b"</Report>", b"<!-- newer --></Report>"))
        newer = rdl_path.read_bytes()
        result = restore_from_backup(backup_path=str(backup))
        assert rdl_path.read_bytes() == original_bytes
        assert Path(result["safety_backup"]).read_bytes() == newer

    def test_force_restores_a_structurally_invalid_backup(self, rdl_path):
        original_bytes = rdl_path.read_bytes()
        odd = rdl_path.with_name(rdl_path.name + ".bak.20260101-000000")
        odd.write_bytes(b"<NotAReport/>")
        with pytest.raises(ValueError, match="not a valid report"):
            restore_from_backup(backup_path=str(odd))
        assert rdl_path.read_bytes() == original_bytes
        result = restore_from_backup(backup_path=str(odd), force=True)
        assert result["restored_to"] == str(rdl_path)
        assert rdl_path.read_bytes() == b"<NotAReport/>"

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
        assert set(result.keys()) == {
            "source",
            "restored_to",
            "bytes_restored",
            "safety_backup",
            "changed",
        }


class TestToolRegistration:
    def test_tool_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "backup_report" in server._tools

    def test_restore_tool_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "restore_from_backup" in server._tools


# ---- issue #63 ---------------------------------------------------------------


@pytest.fixture
def server():
    srv = MCPServer()
    register_all_tools(srv)
    return srv


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    from pbirb_mcp.core import security

    for var in ("PBIRB_MCP_AUTO_BACKUP", security.ALLOWED_ROOTS_ENV, security.REQUIRE_CONFIRM_ENV):
        monkeypatch.delenv(var, raising=False)
    security._reset_defaults_for_tests()
    yield
    security._reset_defaults_for_tests()


def _call(server, tool, **arguments):
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    result = resp["result"]
    payload = json.loads(result["content"][0]["text"])
    return bool(result.get("isError")), payload


def _backups(path: Path) -> list:
    """Backup files of ``path`` next to it (sidecars excluded)."""
    out = []
    for p in path.parent.iterdir():
        m = _BACKUP_NAME.match(p.name)
        if m and m.group("target") == path.name:
            out.append(p)
    return sorted(out)


class TestAutoBackup:
    def test_remove_creates_exactly_one_backup_and_reports_it(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        original = rdl_path.read_bytes()
        err, out = _call(server, "remove_body_item", path=str(rdl_path), name="MainTable")
        assert not err, out
        backups = _backups(rdl_path)
        assert [str(b) for b in backups] == [out["auto_backup"]]
        assert backups[0].read_bytes() == original
        meta = json.loads(Path(out["auto_backup"] + ".meta.json").read_text())
        assert meta["reason"] == "auto" and meta["sha1"]

    def test_off_by_default(self, server, rdl_path):
        err, out = _call(server, "remove_body_item", path=str(rdl_path), name="MainTable")
        assert not err and "auto_backup" not in out
        assert _backups(rdl_path) == []

    def test_non_destructive_edit_takes_no_backup(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        err, out = _call(
            server, "set_textbox_value", path=str(rdl_path), textbox_name="HeaderAmount", value="x"
        )
        assert not err and "auto_backup" not in out
        assert _backups(rdl_path) == []

    def test_each_destructive_change_backs_up_the_state_before_it(
        self, server, rdl_path, monkeypatch
    ):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        _call(server, "remove_parameter", path=str(rdl_path), name="DateTo")
        after_first = rdl_path.read_bytes()
        err, out = _call(server, "remove_body_item", path=str(rdl_path), name="MainTable")
        assert not err, out
        assert len(_backups(rdl_path)) == 2
        assert Path(out["auto_backup"]).read_bytes() == after_first

    def test_a_failed_or_repeated_call_reuses_the_backup(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        for _ in range(2):
            err, _out = _call(server, "remove_body_item", path=str(rdl_path), name="Nope")
            assert err
        assert len(_backups(rdl_path)) == 1

    def test_rename_backs_up(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        err, out = _call(
            server, "rename_parameter", path=str(rdl_path), old_name="DateTo", new_name="DateEnd"
        )
        assert not err, out
        assert out["auto_backup"]

    def test_transaction_backs_up_once_the_state_before_it(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        original = rdl_path.read_bytes()
        _, started = _call(server, "start_editing_transaction", path=str(rdl_path))
        tx = started["transaction_id"]
        _, first = _call(
            server, "remove_parameter", path=str(rdl_path), name="DateTo", transaction_id=tx
        )
        _, second = _call(
            server, "remove_body_item", path=str(rdl_path), name="MainTable", transaction_id=tx
        )
        err, committed = _call(server, "commit_editing_transaction", transaction_id=tx)
        assert not err and committed["saved"], committed
        assert first["auto_backup"] == second["auto_backup"]
        assert _backups(rdl_path) == [Path(first["auto_backup"])]
        assert Path(first["auto_backup"]).read_bytes() == original

    def test_apply_edits_backs_up_once(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        original = rdl_path.read_bytes()
        err, out = _call(
            server,
            "apply_edits",
            path=str(rdl_path),
            ops=[
                {"tool": "remove_parameter", "args": {"name": "DateTo"}},
                {"tool": "remove_body_item", "args": {"name": "MainTable"}},
            ],
        )
        assert not err and out["committed"], out
        backups = _backups(rdl_path)
        assert len(backups) == 1 and backups[0].read_bytes() == original

    def test_dry_run_takes_no_backup(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        before = sorted(p.name for p in rdl_path.parent.iterdir())
        err, out = _call(
            server,
            "dry_run_edit",
            path=str(rdl_path),
            ops=[{"tool": "remove_body_item", "args": {"name": "MainTable"}}],
        )
        assert not err, out
        assert sorted(p.name for p in rdl_path.parent.iterdir()) == before

    def test_maybe_auto_backup_of_a_missing_file_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        assert maybe_auto_backup(str(tmp_path / "ghost.rdl")) is None


class TestBackupThenRestore:
    def test_backup_edit_restore_succeeds_without_force(self, server, rdl_path):
        original = rdl_path.read_bytes()
        _, b = _call(server, "backup_report", path=str(rdl_path))
        _call(
            server,
            "set_textbox_value",
            path=str(rdl_path),
            textbox_name="HeaderAmount",
            value="BAD EDIT",
        )
        bad = rdl_path.read_bytes()
        err, out = _call(server, "restore_from_backup", backup_path=b["backup"])
        assert not err, out
        assert rdl_path.read_bytes() == original
        assert out["changed"] is True
        # The bad state was saved first, so the restore is undoable.
        assert Path(out["safety_backup"]).read_bytes() == bad
        assert validate_report(str(rdl_path))["valid"]

    def test_backup_mtime_is_the_backup_time(self, rdl_path):
        old = time.time() - 3600
        os.utime(rdl_path, (old, old))
        backup = Path(backup_report(str(rdl_path))["backup"])
        assert backup.stat().st_mtime > old + 1800

    def test_non_xml_backup_is_refused_and_target_unchanged(self, server, rdl_path):
        before = rdl_path.read_bytes()
        junk = rdl_path.with_name(rdl_path.name + ".bak.20990101-000000")
        junk.write_text("not xml")
        os.utime(junk, (4e9, 4e9))
        for force in (False, True):
            err, out = _call(server, "restore_from_backup", backup_path=str(junk), force=force)
            assert err and out["error_type"] == "ValueError"
            assert "well-formed" in out["message"]
            assert rdl_path.read_bytes() == before
        assert _backups(rdl_path) == [junk]  # no safety copy for a refused restore

    def test_restoring_identical_bytes_writes_nothing(self, rdl_path):
        backup = backup_report(str(rdl_path))["backup"]
        st = os.stat(rdl_path)
        result = restore_from_backup(backup)
        assert result["changed"] is False and result["safety_backup"] is None
        assert os.stat(rdl_path).st_ino == st.st_ino

    def test_safety_backup_reuses_a_backup_of_the_same_bytes(self, rdl_path):
        first = backup_report(str(rdl_path))["backup"]
        set_textbox_value(str(rdl_path), "HeaderAmount", "v2")
        second = backup_report(str(rdl_path))["backup"]
        result = restore_from_backup(first)
        assert result["safety_backup"] == second
        assert len(_backups(rdl_path)) == 2

    def test_restore_on_a_held_path_is_refused(self, server, rdl_path):
        backup = backup_report(str(rdl_path))["backup"]
        set_textbox_value(str(rdl_path), "HeaderAmount", "v2")
        _, started = _call(server, "start_editing_transaction", path=str(rdl_path))
        before = rdl_path.read_bytes()
        err, out = _call(server, "restore_from_backup", backup_path=backup)
        assert err and out["error_type"] == "TransactionConflictError"
        assert rdl_path.read_bytes() == before
        assert len(_backups(rdl_path)) == 1  # refused before any safety copy
        _call(server, "cancel_editing_transaction", transaction_id=started["transaction_id"])

    def test_backup_of_another_report_needs_force(self, rdl_path, tmp_path):
        other = tmp_path / "other.rdl"
        shutil.copy(FIXTURE, other)
        set_textbox_value(str(other), "HeaderAmount", "OTHER")
        backup = backup_report(str(other))["backup"]
        with pytest.raises(ValueError, match="different report"):
            restore_from_backup(backup, target_path=str(rdl_path))
        restore_from_backup(backup, target_path=str(rdl_path), force=True)
        assert "OTHER" in rdl_path.read_text(encoding="utf-8")


class TestBackupInTransaction:
    def test_backup_holds_the_in_transaction_edit(self, server, rdl_path):
        _, started = _call(server, "start_editing_transaction", path=str(rdl_path))
        tx = started["transaction_id"]
        _call(
            server,
            "set_textbox_value",
            path=str(rdl_path),
            textbox_name="HeaderAmount",
            value="IN-TX",
            transaction_id=tx,
        )
        err, out = _call(server, "backup_report", path=str(rdl_path), transaction_id=tx)
        assert not err, out
        assert out["in_transaction"] is True
        assert "IN-TX" in Path(out["backup"]).read_text(encoding="utf-8")
        assert "IN-TX" not in rdl_path.read_text(encoding="utf-8")
        # A call without the id backs up the file on disk.
        _, plain = _call(server, "backup_report", path=str(rdl_path))
        assert plain["in_transaction"] is False
        assert "IN-TX" not in Path(plain["backup"]).read_text(encoding="utf-8")
        _call(server, "cancel_editing_transaction", transaction_id=tx)


class TestDestAndLabel:
    def test_label_goes_into_the_name_and_parses(self, rdl_path):
        out = backup_report(str(rdl_path), label="before-cleanup")
        backup = Path(out["backup"])
        m = _BACKUP_NAME.match(backup.name)
        assert m and m.group("target") == rdl_path.name and m.group("label") == "before-cleanup"
        assert out["label"] == "before-cleanup"
        # Same-second second backup with the same label: numbered, still parses.
        again = Path(backup_report(str(rdl_path), label="before-cleanup")["backup"])
        m2 = _BACKUP_NAME.match(again.name)
        assert again != backup and m2.group("n") and m2.group("label") == "before-cleanup"
        # The sidecar is not itself a backup.
        assert not _BACKUP_NAME.match(backup.name + ".meta.json")

    @pytest.mark.parametrize("label", ["", "a.b", "../x", "-x", "x" * 65, "a b"])
    def test_bad_label_is_refused(self, rdl_path, label):
        with pytest.raises(ValueError, match="label"):
            backup_report(str(rdl_path), label=label)
        assert _backups(rdl_path) == []

    def test_dest_dir_is_honoured_and_restore_finds_the_report(self, rdl_path, tmp_path):
        dest = tmp_path / "backups"
        dest.mkdir()
        original = rdl_path.read_bytes()
        out = backup_report(str(rdl_path), dest_dir=str(dest), label="v1")
        backup = Path(out["backup"])
        assert backup.parent == dest and backup.read_bytes() == original
        assert _BACKUP_NAME.match(backup.name).group("target") == rdl_path.name
        set_textbox_value(str(rdl_path), "HeaderAmount", "BAD")
        result = restore_from_backup(str(backup))
        assert result["restored_to"] == str(rdl_path)
        assert rdl_path.read_bytes() == original
        # The safety copy goes next to the report.
        assert Path(result["safety_backup"]).parent == rdl_path.parent

    def test_missing_dest_dir_is_refused(self, rdl_path, tmp_path):
        with pytest.raises(FileNotFoundError, match="dest_dir"):
            backup_report(str(rdl_path), dest_dir=str(tmp_path / "nope"))

    def test_dest_dir_outside_the_allowed_roots_is_refused(
        self, server, rdl_path, tmp_path, monkeypatch
    ):
        from pbirb_mcp.core import security

        outside = tmp_path.parent / (tmp_path.name + "-outside")
        outside.mkdir()
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(tmp_path))
        err, out = _call(server, "backup_report", path=str(rdl_path), dest_dir=str(outside))
        assert err and out["error_type"] == "PathNotAllowed"
        assert list(outside.iterdir()) == []

    def test_sidecar_target_outside_the_allowed_roots_is_refused(
        self, server, rdl_path, tmp_path, monkeypatch
    ):
        from pbirb_mcp.core import security

        backup = Path(backup_report(str(rdl_path))["backup"])
        outside = tmp_path.parent / (tmp_path.name + "-elsewhere")
        outside.mkdir()
        victim = outside / rdl_path.name
        meta_path = Path(str(backup) + ".meta.json")
        meta = json.loads(meta_path.read_text())
        meta["source"] = str(victim)
        meta_path.write_text(json.dumps(meta))
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(tmp_path))
        err, out = _call(server, "restore_from_backup", backup_path=str(backup))
        assert err and out["error_type"] == "PathNotAllowed"
        assert not victim.exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
    def test_backup_keeps_a_private_mode(self, rdl_path):
        os.chmod(rdl_path, 0o600)
        backup = Path(backup_report(str(rdl_path))["backup"])
        assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600


class TestListBackups:
    def test_newest_first_with_labels_reasons_and_match(self, server, rdl_path, monkeypatch):
        first = backup_report(str(rdl_path), label="one")["backup"]
        set_textbox_value(str(rdl_path), "HeaderAmount", "v2")
        monkeypatch.setenv("PBIRB_MCP_AUTO_BACKUP", "1")
        _, removed = _call(server, "remove_parameter", path=str(rdl_path), name="DateTo")
        err, out = _call(server, "list_backups", path=str(rdl_path))
        assert not err, out
        rows = out["backups"]
        assert [r["backup"] for r in rows] == [removed["auto_backup"], first]
        assert [r["reason"] for r in rows] == ["auto", "manual"]
        assert [r["label"] for r in rows] == [None, "one"]
        assert [r["same_as_report"] for r in rows] == [False, False]
        restore_from_backup(first)
        rows = list_backups(str(rdl_path))["backups"]
        assert rows[0]["reason"] == "pre-restore"
        assert [r["same_as_report"] for r in rows if r["backup"] == first] == [True]

    def test_other_reports_and_sidecars_are_left_out(self, rdl_path, tmp_path):
        dest = tmp_path / "shared"
        dest.mkdir()
        other_dir = tmp_path / "elsewhere"
        other_dir.mkdir()
        other = other_dir / rdl_path.name  # same file name, different report
        shutil.copy(FIXTURE, other)
        mine = backup_report(str(rdl_path), dest_dir=str(dest))["backup"]
        backup_report(str(other), dest_dir=str(dest))
        rows = list_backups(str(rdl_path), dest_dir=str(dest))["backups"]
        assert [r["backup"] for r in rows] == [mine]

    def test_legacy_backup_without_sidecar_is_listed(self, rdl_path):
        legacy = rdl_path.with_name(rdl_path.name + ".bak.20200101-000000")
        shutil.copy(rdl_path, legacy)
        rows = list_backups(str(rdl_path))["backups"]
        assert [r["backup"] for r in rows] == [str(legacy)]
        assert rows[0]["created"] == "2020-01-01T00:00:00+00:00"
        assert rows[0]["same_as_report"] is True
