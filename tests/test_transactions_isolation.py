"""Transaction isolation (issue #25).

A transaction's in-memory tree belongs to calls routed through its
``transaction_id``. Everyone else reads the on-disk file and may not write
over the held path; expiry is enforced on every lookup; saving a held
tree to ANOTHER path really writes; validate sees the transaction tree.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core import transactions
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


def test_edit_without_id_on_held_path_is_a_conflict(server, rdl):
    before = rdl.read_bytes()
    tx = _start(server, rdl)
    payload, is_error = _call(
        server, "set_textbox_value", path=str(rdl), textbox_name="HeaderAmount", value="NO-TX"
    )
    assert is_error
    assert payload["error_type"] == "TransactionConflictError"
    assert rdl.read_bytes() == before
    # The transaction's tree didn't get the edit either.
    _call(server, "commit_editing_transaction", transaction_id=tx)
    assert b"NO-TX" not in rdl.read_bytes()


def test_read_without_id_sees_committed_state(server, rdl):
    tx = _start(server, rdl)
    _call(
        server,
        "set_textbox_value",
        path=str(rdl),
        textbox_name="HeaderAmount",
        value="IN-FLIGHT",
        transaction_id=tx,
    )
    inside, _ = _call(server, "get_textbox", path=str(rdl), name="HeaderAmount", transaction_id=tx)
    outside, _ = _call(server, "get_textbox", path=str(rdl), name="HeaderAmount")
    assert "IN-FLIGHT" in json.dumps(inside)
    assert "IN-FLIGHT" not in json.dumps(outside)


def test_expired_transaction_is_gone_and_edits_reach_disk(server, rdl, monkeypatch):
    tx = _start(server, rdl)
    transactions._BY_ID[tx].expires_at = 0.0  # expire it
    payload, is_error = _call(
        server, "set_textbox_value", path=str(rdl), textbox_name="HeaderAmount", value="AFTER"
    )
    assert not is_error, payload
    assert transactions.active_transactions() == []
    assert b"AFTER" in rdl.read_bytes()
    payload, is_error = _call(server, "commit_editing_transaction", transaction_id=tx)
    assert is_error
    assert "unknown or expired" in payload["message"] or "expired" in payload["message"]


def test_expired_transaction_cannot_be_cancelled_or_used(server, rdl):
    tx = _start(server, rdl)
    transactions._BY_ID[tx].expires_at = 0.0
    _, is_error = _call(server, "cancel_editing_transaction", transaction_id=tx)
    assert is_error
    payload, is_error = _call(
        server,
        "set_textbox_value",
        path=str(rdl),
        textbox_name="HeaderAmount",
        value="x",
        transaction_id=tx,
    )
    assert is_error
    assert payload["error_type"] == "TransactionError"


def _report_ids(path: Path) -> list[str]:
    return re.findall(r"<rd:ReportID>(.*?)<", path.read_text())


@pytest.mark.parametrize("inside", [True, False], ids=["inside", "alongside"])
def test_duplicate_report_writes_dst_and_keeps_source_ids(server, rdl, tmp_path, inside):
    ids_before = _report_ids(rdl)
    ds_before = re.findall(r"<rd:DataSourceID>(.*?)<", rdl.read_text())
    tx = _start(server, rdl)
    dst = tmp_path / "copy.rdl"
    extra = {"transaction_id": tx} if inside else {}
    payload, is_error = _call(
        server, "duplicate_report", src_path=str(rdl), dst_path=str(dst), **extra
    )
    assert not is_error, payload
    assert dst.is_file()
    assert _report_ids(dst) != ids_before
    _call(server, "commit_editing_transaction", transaction_id=tx)
    assert _report_ids(rdl) == ids_before
    assert re.findall(r"<rd:DataSourceID>(.*?)<", rdl.read_text()) == ds_before


def test_validate_inside_transaction_sees_transaction_tree(server, rdl):
    tx = _start(server, rdl)
    _, is_error = _call(
        server,
        "raw_xml_replace",
        path=str(rdl),
        xpath=".//r:Textbox[@Name='HeaderAmount']/r:CanGrow",
        content="<CanGrow>maybe</CanGrow>",
        transaction_id=tx,
    )
    assert not is_error
    inside, _ = _call(server, "validate_report", path=str(rdl), transaction_id=tx)
    assert inside["valid"] is False
    assert any(e["rule"] == "xsd" for e in inside["errors"])
    outside, _ = _call(server, "validate_report", path=str(rdl))
    assert outside["valid"] is True


def test_verify_report_inside_transaction_reports_xsd_error(server, rdl):
    tx = _start(server, rdl)
    _call(
        server,
        "raw_xml_replace",
        path=str(rdl),
        xpath=".//r:Textbox[@Name='HeaderAmount']/r:CanGrow",
        content="<CanGrow>maybe</CanGrow>",
        transaction_id=tx,
    )
    payload, _ = _call(server, "verify_report", path=str(rdl), transaction_id=tx)
    assert payload["valid"] is False
    assert any(i["rule"] == "xsd" for i in payload["issues"])


def test_restore_from_backup_onto_held_path_is_a_conflict(server, rdl):
    backup, _ = _call(server, "backup_report", path=str(rdl))
    _call(server, "set_textbox_value", path=str(rdl), textbox_name="HeaderAmount", value="NEW")
    before = rdl.read_bytes()
    _start(server, rdl)
    payload, is_error = _call(
        server, "restore_from_backup", backup_path=backup["backup"], force=True
    )
    assert is_error
    assert payload["error_type"] == "TransactionConflictError"
    assert rdl.read_bytes() == before


def test_second_start_on_held_path_still_refused(server, rdl):
    _start(server, rdl)
    payload, is_error = _call(server, "start_editing_transaction", path=str(rdl))
    assert is_error
