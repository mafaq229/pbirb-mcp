"""apply_edits / dry_run_edit op allow-list and atomicity (issue #27)."""

from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from pbirb_mcp.core import transactions
from pbirb_mcp.server import NON_BATCHABLE_TOOLS, MCPServer
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


def _sv(value):
    return {"tool": "set_textbox_value", "args": {"textbox_name": "HeaderAmount", "value": value}}


def _dry_run_residue() -> set:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "pbirb-dry-run-*")))


@pytest.mark.parametrize("batch_tool", ["apply_edits", "dry_run_edit"])
@pytest.mark.parametrize("denied", sorted(NON_BATCHABLE_TOOLS))
def test_non_batchable_op_is_refused_up_front(server, rdl, batch_tool, denied):
    before = rdl.read_bytes()
    residue = _dry_run_residue()
    payload, is_error = _call(
        server, batch_tool, path=str(rdl), ops=[_sv("FIRST"), {"tool": denied, "args": {}}]
    )
    assert is_error
    assert payload["error_type"] == "OpNotAllowed"
    assert payload["tool"] == denied
    assert payload["op_index"] == 1
    assert rdl.read_bytes() == before
    assert transactions.active_transactions() == []
    assert _dry_run_residue() == residue


def test_mid_batch_commit_is_refused_and_nothing_lands(server, rdl):
    before = rdl.read_bytes()
    payload, is_error = _call(
        server,
        "apply_edits",
        path=str(rdl),
        ops=[
            _sv("PARTIAL"),
            {"tool": "commit_editing_transaction"},
            {"tool": "set_textbox_value", "args": {"textbox_name": "Nope", "value": "x"}},
        ],
    )
    assert is_error and payload["error_type"] == "OpNotAllowed"
    assert rdl.read_bytes() == before


def test_restore_from_backup_in_batch_is_refused(server, rdl):
    backup, _ = _call(server, "backup_report", path=str(rdl))
    _call(server, "set_textbox_value", path=str(rdl), textbox_name="HeaderAmount", value="NEWER")
    before = rdl.read_bytes()
    payload, is_error = _call(
        server,
        "apply_edits",
        path=str(rdl),
        ops=[{"tool": "restore_from_backup", "args": {"backup_path": backup["backup"]}}],
    )
    assert is_error and payload["error_type"] == "OpNotAllowed"
    assert rdl.read_bytes() == before


def test_nested_apply_edits_is_refused(server, rdl):
    payload, is_error = _call(
        server,
        "apply_edits",
        path=str(rdl),
        ops=[{"tool": "apply_edits", "args": {"ops": [_sv("INNER")]}}],
    )
    assert is_error and payload["error_type"] == "OpNotAllowed"
    assert b"INNER" not in rdl.read_bytes()


@pytest.mark.parametrize("batch_tool", ["apply_edits", "dry_run_edit"])
def test_read_and_edit_tools_still_allowed(server, rdl, batch_tool):
    payload, is_error = _call(
        server,
        batch_tool,
        path=str(rdl),
        ops=[
            _sv("OK"),
            {"tool": "describe_report"},
            {"tool": "get_textbox", "args": {"name": "HeaderAmount"}},
            {
                "tool": "set_textbox_style",
                "args": {"textbox_name": "HeaderAmount", "font_weight": "Bold"},
            },
        ],
    )
    assert not is_error, payload
    assert [a["ok"] for a in payload["applied"]] == [True, True, True, True]


def test_op_returning_failure_counts_as_failure(server, rdl):
    # A batchable test tool that returns {saved: false} instead of raising.
    server.register_tool(
        "soft_fail",
        "test-only",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        lambda path: {"saved": False},
    )
    from pbirb_mcp.ops import transactions as tx_ops

    # apply_edits builds its own server; route through ours instead.
    original = tx_ops._build_server
    tx_ops._build_server = lambda: server
    try:
        payload, is_error = _call(
            server, "apply_edits", path=str(rdl), ops=[_sv("X"), {"tool": "soft_fail"}]
        )
    finally:
        tx_ops._build_server = original
    assert not is_error
    assert payload["committed"] is False
    assert payload["applied"][-1]["ok"] is False
    assert b"X</Value>" not in rdl.read_bytes()


def test_dry_run_inside_transaction_previews_transaction_tree(server, rdl):
    start, _ = _call(server, "start_editing_transaction", path=str(rdl))
    tx = start["transaction_id"]
    _call(
        server,
        "set_textbox_value",
        path=str(rdl),
        textbox_name="HeaderAmount",
        value="IN-TX",
        transaction_id=tx,
    )
    payload, is_error = _call(
        server,
        "dry_run_edit",
        path=str(rdl),
        ops=[
            {
                "tool": "set_textbox_style",
                "args": {"textbox_name": "HeaderAmount", "font_weight": "Bold"},
            }
        ],
        transaction_id=tx,
    )
    assert not is_error, payload
    # The diff is against the transaction tree: IN-TX is not a change.
    changed = [
        line
        for line in payload["diff"].splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert any("Bold" in line for line in changed)
    assert not any("IN-TX" in line for line in changed)
    # And the transaction tree itself is untouched by the preview.
    got, _ = _call(server, "get_textbox", path=str(rdl), name="HeaderAmount", transaction_id=tx)
    assert "Bold" not in json.dumps(got)
