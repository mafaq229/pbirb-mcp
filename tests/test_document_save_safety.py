"""Issue #64: save safely — write through symlinks, keep the file mode,
honour read-only files.

Every writer that replaces a report goes through
:func:`pbirb_mcp.core.document.write_atomic`: ``RDLDocument.save_as`` (all
edit tools, transaction commit, create_report), ``duplicate_report`` and
``restore_from_backup``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument, write_atomic
from pbirb_mcp.ops.clone import duplicate_report
from pbirb_mcp.ops.snapshot import backup_report, restore_from_backup
from pbirb_mcp.ops.styling import set_textbox_value
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX modes / symlinks")


@pytest.fixture(scope="module")
def server():
    srv = MCPServer()
    register_all_tools(srv)
    return srv


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
    return result, json.loads(result["content"][0]["text"])


@pytest.fixture
def target(tmp_path: Path) -> Path:
    dest = tmp_path / "target.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def link(tmp_path: Path, target: Path) -> Path:
    lnk = tmp_path / "link.rdl"
    lnk.symlink_to(target)
    return lnk


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _tmp_leftovers(directory: Path) -> list:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


@posix_only
class TestSymlinks:
    def test_edit_through_a_symlink_updates_the_target(self, link, target):
        set_textbox_value(str(link), "HeaderAmount", "VIA-LINK")
        assert link.is_symlink()
        assert os.readlink(link) == str(target)
        assert "VIA-LINK" in target.read_text(encoding="utf-8")
        assert validate_report(str(target))["valid"]

    def test_relative_symlink_in_another_directory(self, tmp_path, target):
        other = tmp_path / "linked"
        other.mkdir()
        lnk = other / "report.rdl"
        lnk.symlink_to(os.path.join("..", target.name))
        set_textbox_value(str(lnk), "HeaderAmount", "REL")
        assert lnk.is_symlink()
        assert "REL" in target.read_text(encoding="utf-8")
        # The temp file lived next to the real file, and is gone.
        assert _tmp_leftovers(other) == [] and _tmp_leftovers(tmp_path) == []

    def test_symlink_chain(self, tmp_path, link, target):
        second = tmp_path / "link2.rdl"
        second.symlink_to(link)
        set_textbox_value(str(second), "HeaderAmount", "CHAIN")
        assert second.is_symlink() and link.is_symlink()
        assert "CHAIN" in target.read_text(encoding="utf-8")

    def test_tool_call_through_a_symlink(self, server, link, target):
        result, payload = _call(
            server, "set_textbox_value", path=str(link), textbox_name="HeaderAmount", value="T"
        )
        assert not result.get("isError"), payload
        assert link.is_symlink()
        assert ">T<" in target.read_text(encoding="utf-8")

    def test_transaction_commit_through_a_symlink(self, server, link, target):
        _, started = _call(server, "start_editing_transaction", path=str(link))
        tx = started["transaction_id"]
        _call(
            server,
            "set_textbox_value",
            path=str(link),
            textbox_name="HeaderAmount",
            value="IN-TX",
            transaction_id=tx,
        )
        result, committed = _call(server, "commit_editing_transaction", transaction_id=tx)
        assert not result.get("isError"), committed
        assert committed["saved"] is True
        assert link.is_symlink()
        assert "IN-TX" in target.read_text(encoding="utf-8")

    def test_no_op_save_through_a_symlink_keeps_the_file(self, link, target):
        before = os.stat(target)
        doc = RDLDocument.open(link)
        assert doc.save() is False
        after = os.stat(target)
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    def test_restore_through_a_symlink(self, link, target):
        backup = backup_report(str(link))["backup"]
        set_textbox_value(str(link), "HeaderAmount", "BAD")
        restore_from_backup(backup, target_path=str(link), force=True)
        assert link.is_symlink()
        assert "BAD" not in target.read_text(encoding="utf-8")


@posix_only
class TestFileMode:
    @pytest.mark.parametrize("mode", [0o600, 0o640, 0o664])
    def test_edit_keeps_the_mode(self, target, mode):
        os.chmod(target, mode)
        set_textbox_value(str(target), "HeaderAmount", "x")
        assert _mode(target) == mode
        assert ">x<" in target.read_text(encoding="utf-8")

    def test_mode_of_a_symlinked_target_is_kept(self, link, target):
        os.chmod(target, 0o600)
        set_textbox_value(str(link), "HeaderAmount", "x")
        assert _mode(target) == 0o600

    def test_new_file_gets_the_umask_default(self, target, tmp_path):
        umask = os.umask(0o022)
        try:
            out = tmp_path / "new.rdl"
            RDLDocument.open(target).save_as(out)
        finally:
            os.umask(umask)
        assert _mode(out) == 0o644

    def test_restore_keeps_the_report_mode(self, target):
        os.chmod(target, 0o600)
        backup = Path(backup_report(str(target))["backup"])
        os.chmod(backup, 0o644)
        set_textbox_value(str(target), "HeaderAmount", "BAD")
        restore_from_backup(str(backup), force=True)
        assert _mode(target) == 0o600

    def test_write_atomic_keeps_owner_and_group(self, target):
        before = os.stat(target)
        write_atomic(target, target.read_bytes())
        after = os.stat(target)
        assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


@posix_only
class TestReadOnly:
    def test_editing_a_read_only_file_is_refused(self, target):
        os.chmod(target, 0o444)
        before = target.read_bytes()
        with pytest.raises(PermissionError, match="read-only"):
            set_textbox_value(str(target), "HeaderAmount", "y")
        assert target.read_bytes() == before
        assert _mode(target) == 0o444
        assert _tmp_leftovers(target.parent) == []

    def test_tool_call_on_a_read_only_file_is_an_error(self, server, target):
        os.chmod(target, 0o444)
        before = target.read_bytes()
        result, payload = _call(
            server, "set_textbox_value", path=str(target), textbox_name="HeaderAmount", value="y"
        )
        assert result.get("isError") is True
        assert payload["error_type"] == "PermissionError"
        assert target.read_bytes() == before

    def test_read_only_target_through_a_symlink(self, link, target):
        os.chmod(target, 0o444)
        before = target.read_bytes()
        with pytest.raises(PermissionError):
            set_textbox_value(str(link), "HeaderAmount", "y")
        assert target.read_bytes() == before and link.is_symlink()

    def test_identical_repeat_on_a_read_only_file_writes_nothing(self, target):
        # The no-op save (#61) never writes, so there is nothing to refuse.
        set_textbox_value(str(target), "HeaderAmount", "same")
        os.chmod(target, 0o444)
        result = set_textbox_value(str(target), "HeaderAmount", "same")
        assert result["changed"] is False

    def test_commit_on_a_read_only_file_fails_and_keeps_the_transaction(self, server, target):
        _, started = _call(server, "start_editing_transaction", path=str(target))
        tx = started["transaction_id"]
        _call(
            server,
            "set_textbox_value",
            path=str(target),
            textbox_name="HeaderAmount",
            value="TX",
            transaction_id=tx,
        )
        os.chmod(target, 0o444)
        before = target.read_bytes()
        result, payload = _call(server, "commit_editing_transaction", transaction_id=tx)
        assert result.get("isError") is True and payload["error_type"] == "PermissionError"
        assert target.read_bytes() == before
        # Still open: cancel succeeds.
        result, _ = _call(server, "cancel_editing_transaction", transaction_id=tx)
        assert not result.get("isError")

    def test_restore_over_a_read_only_report_is_refused(self, target):
        backup = backup_report(str(target))["backup"]
        set_textbox_value(str(target), "HeaderAmount", "KEEP")
        os.chmod(target, 0o444)
        with pytest.raises(PermissionError):
            restore_from_backup(backup, force=True)
        assert "KEEP" in target.read_text(encoding="utf-8")


class TestAtomicity:
    def test_failed_write_leaves_original_and_no_temp_file(self, target, monkeypatch):
        before = target.read_bytes()

        def boom(*_a, **_kw):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("pbirb_mcp.core.document.os.replace", boom)
        with pytest.raises(OSError, match="simulated"):
            set_textbox_value(str(target), "HeaderAmount", "x")
        assert target.read_bytes() == before
        assert _tmp_leftovers(target.parent) == []

    def test_write_atomic_returns_the_real_path(self, target):
        assert write_atomic(target, b"<x/>") == Path(os.path.realpath(target))
        assert target.read_bytes() == b"<x/>"

    @posix_only
    def test_write_atomic_through_a_dangling_symlink_creates_the_target(self, tmp_path):
        lnk = tmp_path / "dangling.rdl"
        real = tmp_path / "real.rdl"
        lnk.symlink_to(real)
        write_atomic(lnk, b"<x/>")
        assert lnk.is_symlink() and real.read_bytes() == b"<x/>"


class TestDuplicateReport:
    @pytest.mark.parametrize("regenerate", [True, False])
    def test_duplicate_writes_a_regular_file_without_leftovers(self, target, tmp_path, regenerate):
        dst = tmp_path / "copy.rdl"
        duplicate_report(str(target), str(dst), regenerate_ids=regenerate)
        assert dst.is_file() and not dst.is_symlink()
        assert _tmp_leftovers(tmp_path) == []
        if not regenerate:
            assert dst.read_bytes() == target.read_bytes()
