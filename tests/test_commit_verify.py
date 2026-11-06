"""Commit-time verify (issue #28): XSD runs at commit / apply_edits, and
only errors the transaction introduced block the save."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import transactions
from pbirb_mcp.core.xpath import XPATH_NS
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
BOGUS = {
    "xpath": ".//r:Textbox[@Name='HeaderAmount']/r:CanGrow",
    # Same element, XSD-invalid value: raw_xml_replace leaves the check to
    # the commit inside a transaction (#85).
    "content": "<CanGrow>maybe</CanGrow>",
}


def _corrupt(path: Path) -> None:
    """Write BOGUS's XSD error straight into the file: outside a transaction
    raw_xml_replace now refuses an edit that fails the XSD (#85)."""
    tree = etree.parse(str(path))
    tree.getroot().xpath(BOGUS["xpath"], namespaces=XPATH_NS)[0].text = "maybe"
    tree.write(str(path), xml_declaration=True, encoding="utf-8")


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


@pytest.fixture
def broken(tmp_path: Path) -> Path:
    """A report with one pre-existing lint error (missing-field-reference)."""
    dest = tmp_path / "broken.rdl"
    dest.write_text(FIXTURE.read_text().replace("=Fields!Amount.Value", "=Fields!Typo.Value"))
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


def test_apply_edits_introducing_xsd_error_does_not_commit(server, rdl):
    before = rdl.read_bytes()
    payload, _ = _call(
        server, "apply_edits", path=str(rdl), ops=[{"tool": "raw_xml_replace", "args": BOGUS}]
    )
    assert payload["committed"] is False
    assert payload["verify"]["valid"] is False
    assert payload["verify"]["xsd_used"] is True
    xsd = [i for i in payload["verify"]["issues"] if i["rule"] == "xsd"]
    assert xsd and all(i["new"] is True for i in xsd)
    assert rdl.read_bytes() == before


def test_commit_introducing_xsd_error_keeps_transaction_open(server, rdl):
    before = rdl.read_bytes()
    start, _ = _call(server, "start_editing_transaction", path=str(rdl))
    tx = start["transaction_id"]
    _call(server, "raw_xml_replace", path=str(rdl), transaction_id=tx, **BOGUS)
    payload, _ = _call(server, "commit_editing_transaction", transaction_id=tx)
    assert payload["saved"] is False
    assert payload["verify"]["introduced_error_count"] >= 1
    assert rdl.read_bytes() == before
    assert tx in transactions.active_transactions()


def test_preexisting_errors_do_not_block_unrelated_commit(server, broken):
    payload, _ = _call(
        server,
        "apply_edits",
        path=str(broken),
        ops=[
            {
                "tool": "set_textbox_value",
                "args": {"textbox_name": "HeaderAmount", "value": "Total"},
            }
        ],
    )
    assert payload["committed"] is True
    issues = payload["verify"]["issues"]
    old = [i for i in issues if i["rule"] == "missing-field-reference"]
    assert old and all(i["new"] is False for i in old)
    assert payload["verify"]["preexisting_count"] == len(issues)
    assert payload["verify"]["introduced_count"] == 0
    # Honest: the file still has an error.
    assert payload["verify"]["valid"] is False
    assert "Total" in broken.read_text()


def test_new_error_still_blocks_on_report_with_preexisting_errors(server, broken):
    before = broken.read_bytes()
    payload, _ = _call(
        server,
        "apply_edits",
        path=str(broken),
        ops=[{"tool": "raw_xml_replace", "args": BOGUS}],
    )
    assert payload["committed"] is False
    flags = {(i["rule"], i["new"]) for i in payload["verify"]["issues"]}
    assert ("missing-field-reference", False) in flags
    assert ("xsd", True) in flags
    assert broken.read_bytes() == before


def test_preexisting_xsd_error_does_not_block(server, rdl):
    # The file is already XSD-invalid before the transaction starts.
    _corrupt(rdl)
    payload, _ = _call(
        server,
        "apply_edits",
        path=str(rdl),
        ops=[
            {
                "tool": "set_textbox_value",
                "args": {"textbox_name": "HeaderAmount", "value": "Total"},
            }
        ],
    )
    assert payload["committed"] is True
    xsd = [i for i in payload["verify"]["issues"] if i["rule"] == "xsd"]
    assert xsd and all(i["new"] is False for i in xsd)


def test_verify_report_inside_transaction_marks_new(server, broken):
    start, _ = _call(server, "start_editing_transaction", path=str(broken))
    tx = start["transaction_id"]
    _call(server, "raw_xml_replace", path=str(broken), transaction_id=tx, **BOGUS)
    payload, _ = _call(server, "verify_report", path=str(broken), transaction_id=tx)
    flags = {(i["rule"], i["new"]) for i in payload["issues"]}
    assert ("missing-field-reference", False) in flags
    assert ("xsd", True) in flags
    assert payload["introduced_error_count"] >= 1


def test_verify_report_baseline_path(server, broken, tmp_path):
    backup = tmp_path / "before.rdl"
    shutil.copy(broken, backup)
    _corrupt(broken)
    payload, is_error = _call(server, "verify_report", path=str(broken), baseline_path=str(backup))
    assert not is_error, payload
    flags = {(i["rule"], i["new"]) for i in payload["issues"]}
    assert ("missing-field-reference", False) in flags
    assert ("xsd", True) in flags
    assert payload["preexisting_count"] == 1


def test_verify_report_without_baseline_has_no_new_flags(server, broken):
    payload, _ = _call(server, "verify_report", path=str(broken))
    assert all("new" not in i for i in payload["issues"])
    assert "introduced_count" not in payload
