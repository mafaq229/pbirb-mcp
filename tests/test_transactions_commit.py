"""Safe transaction commits (issue #26): per-op rollback, external-change
detection, hardlink-aware locking, failed-save recovery."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core import transactions
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture(autouse=True)
def _isolate_registry():
    transactions._reset_for_tests()
    yield
    transactions._reset_for_tests()


@pytest.fixture
def server() -> MCPServer:
    s = MCPServer()
    register_all_tools(s)
    return s


@pytest.fixture
def rdl(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


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
    return json.loads(result["content"][0]["text"]), bool(result.get("isError"))


def _start(server, path) -> str:
    payload, is_error = _call(server, "start_editing_transaction", path=str(path))
    assert not is_error, payload
    return payload["transaction_id"]


def test_op_that_raises_after_partial_mutation_is_rolled_back(server, rdl):
    before = rdl.read_bytes()
    tx = _start(server, rdl)

    # A handler that mutates the shared tree, then raises.
    def boom(path):
        doc = RDLDocument.open(path)
        doc.root.find(".//{*}PageHeight").text = "5in"
        raise ValueError("late failure")

    server.register_tool(
        "mutate_then_fail",
        "test-only",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        boom,
    )
    payload, is_error = _call(server, "mutate_then_fail", path=str(rdl), transaction_id=tx)
    assert is_error and payload["message"] == "late failure"
    committed, _ = _call(server, "commit_editing_transaction", transaction_id=tx)
    assert committed["saved"] is True
    assert rdl.read_bytes() == before


def test_rollback_keeps_earlier_successful_ops(server, rdl):
    tx = _start(server, rdl)
    _call(
        server,
        "set_textbox_value",
        path=str(rdl),
        textbox_name="HeaderAmount",
        value="KEEP",
        transaction_id=tx,
    )
    _, is_error = _call(
        server,
        "set_textbox_value",
        path=str(rdl),
        textbox_name="Nope",
        value="x",
        transaction_id=tx,
    )
    assert is_error
    _call(server, "commit_editing_transaction", transaction_id=tx)
    assert b"KEEP" in rdl.read_bytes()


def test_external_change_blocks_commit(server, rdl):
    tx = _start(server, rdl)
    _call(
        server,
        "set_textbox_value",
        path=str(rdl),
        textbox_name="HeaderAmount",
        value="MINE",
        transaction_id=tx,
    )
    with open(rdl, "ab") as fh:
        fh.write(b"<!-- saved by Report Builder meanwhile -->\n")
    external = rdl.read_bytes()
    payload, is_error = _call(server, "commit_editing_transaction", transaction_id=tx)
    assert not is_error
    assert payload["saved"] is False
    assert "changed on disk" in payload["error"]
    assert rdl.read_bytes() == external
    # Transaction is still open; force=true overwrites deliberately.
    assert tx in transactions.active_transactions()
    payload, _ = _call(server, "commit_editing_transaction", transaction_id=tx, force=True)
    assert payload["saved"] is True
    assert b"MINE" in rdl.read_bytes()


def test_hardlink_cannot_open_second_transaction(server, rdl, tmp_path):
    hard = tmp_path / "hard.rdl"
    os.link(rdl, hard)
    _start(server, rdl)
    payload, is_error = _call(server, "start_editing_transaction", path=str(hard))
    assert is_error
    assert payload["error_type"] == "TransactionConflictError"


def test_hardlink_write_without_id_is_a_conflict(server, rdl, tmp_path):
    hard = tmp_path / "hard.rdl"
    os.link(rdl, hard)
    before = rdl.read_bytes()
    _start(server, rdl)
    payload, is_error = _call(
        server, "set_textbox_value", path=str(hard), textbox_name="HeaderAmount", value="x"
    )
    assert is_error
    assert payload["error_type"] == "TransactionConflictError"
    assert rdl.read_bytes() == before


def test_failed_save_keeps_transaction_open_and_retryable(server, rdl, monkeypatch):
    before = rdl.read_bytes()
    tx = _start(server, rdl)
    _call(
        server,
        "set_textbox_value",
        path=str(rdl),
        textbox_name="HeaderAmount",
        value="FIRST",
        transaction_id=tx,
    )
    real_replace = os.replace

    def deny(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(os, "replace", deny)
    payload, is_error = _call(server, "commit_editing_transaction", transaction_id=tx)
    assert is_error and payload["error_type"] == "PermissionError"
    assert rdl.read_bytes() == before
    assert tx in transactions.active_transactions()

    # Edits after the failed commit stay in memory — never on disk.
    monkeypatch.setattr(os, "replace", real_replace)
    _call(
        server,
        "set_textbox_value",
        path=str(rdl),
        textbox_name="HeaderAmount",
        value="LEAK",
        transaction_id=tx,
    )
    assert b"LEAK" not in rdl.read_bytes()

    # Retry succeeds once the cause is fixed.
    payload, _ = _call(server, "commit_editing_transaction", transaction_id=tx)
    assert payload["saved"] is True
    assert b"LEAK" in rdl.read_bytes()


def test_cancel_after_failed_commit_leaves_disk_untouched(server, rdl, monkeypatch):
    before = rdl.read_bytes()
    tx = _start(server, rdl)

    def deny(src, dst):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(os, "replace", deny)
    _call(server, "commit_editing_transaction", transaction_id=tx)
    monkeypatch.undo()
    _call(
        server,
        "set_textbox_value",
        path=str(rdl),
        textbox_name="HeaderAmount",
        value="LEAK",
        transaction_id=tx,
    )
    payload, _ = _call(server, "cancel_editing_transaction", transaction_id=tx)
    assert payload["discarded"] is True
    assert rdl.read_bytes() == before


def test_set_page_setup_partial_write_not_committed(server, rdl):
    # The issue's case 1: set_page_setup used to write PageHeight before
    # rejecting columns=0; the per-op snapshot discards that.
    tx = _start(server, rdl)
    _, is_error = _call(
        server, "set_page_setup", path=str(rdl), page_height="5in", columns=0, transaction_id=tx
    )
    assert is_error
    _call(server, "commit_editing_transaction", transaction_id=tx)
    assert b"<PageHeight>5in</PageHeight>" not in rdl.read_bytes()
