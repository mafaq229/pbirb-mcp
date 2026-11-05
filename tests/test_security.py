"""Issue #57: file-access sandbox, destructive-edit confirmation, audit log."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from pbirb_mcp.core import security
from pbirb_mcp.server import DESTRUCTIVE_TOOLS, MCPServer
from pbirb_mcp.tools import register_all_tools

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "pbi_chart_rich.rdl"

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
}


@pytest.fixture(scope="module")
def server():
    srv = MCPServer()
    register_all_tools(srv)
    return srv


@pytest.fixture(autouse=True)
def _clean_policy(monkeypatch):
    for var in (
        security.ALLOWED_ROOTS_ENV,
        security.REQUIRE_CONFIRM_ENV,
        security.AUDIT_LOG_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    security._reset_defaults_for_tests()
    yield
    security._reset_defaults_for_tests()


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "project"
    proj.mkdir()
    shutil.copy(FIXTURE, proj / "report.rdl")
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy(FIXTURE, outside / "report.rdl")
    return proj, outside


def _call(server, tool, meta=None, extra=None, **arguments):
    params = {"name": tool, "arguments": arguments, **(extra or {})}
    if meta:
        params["_meta"] = meta
    resp = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    )
    return resp["result"]


def _payload(result):
    return json.loads(result["content"][0]["text"])


# ---- the destructive set -------------------------------------------------------


class TestDestructiveSet:
    def test_every_remove_tool_is_destructive(self, server):
        removers = {n for n in server._tools if n.startswith("remove_")}
        assert removers <= DESTRUCTIVE_TOOLS
        assert set(server._tools) >= DESTRUCTIVE_TOOLS  # no stale names

    def test_confirm_param_published(self, server):
        listing = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        schemas = {t["name"]: t["inputSchema"] for t in listing["result"]["tools"]}
        for name in DESTRUCTIVE_TOOLS | {"apply_edits"}:
            assert schemas[name]["properties"]["confirm"]["type"] == "boolean", name
        assert "confirm" not in schemas["set_textbox_style"]["properties"]


# ---- sandbox ---------------------------------------------------------------------


class TestAllowedRoots:
    def test_path_outside_roots_is_refused(self, server, project, monkeypatch):
        proj, outside = project
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(proj))
        result = _call(server, "describe_report", path=str(outside / "report.rdl"))
        assert result["isError"] is True
        payload = _payload(result)
        assert payload["error_type"] == "PathNotAllowed"
        assert payload["param"] == "path"
        assert payload["allowed_roots"] == [str(proj.resolve())]
        assert security.ALLOWED_ROOTS_ENV in payload["message"]

    def test_path_inside_roots_is_served(self, server, project, monkeypatch):
        proj, _ = project
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(proj))
        result = _call(server, "describe_report", path=str(proj / "report.rdl"))
        assert not result.get("isError")

    def test_dotdot_escape_is_refused(self, server, project, monkeypatch):
        proj, _ = project
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(proj))
        sneaky = proj / ".." / "outside" / "report.rdl"
        assert _payload(_call(server, "describe_report", path=str(sneaky)))["error_type"] == (
            "PathNotAllowed"
        )

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_symlink_escape_is_refused(self, server, project, monkeypatch):
        proj, outside = project
        (proj / "link.rdl").symlink_to(outside / "report.rdl")
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(proj))
        result = _call(server, "describe_report", path=str(proj / "link.rdl"))
        assert _payload(result)["error_type"] == "PathNotAllowed"

    def test_every_path_argument_is_checked(self, server, project, monkeypatch):
        proj, outside = project
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(proj))
        result = _call(
            server,
            "duplicate_report",
            src_path=str(proj / "report.rdl"),
            dst_path=str(outside / "copy.rdl"),
        )
        assert _payload(result)["param"] == "dst_path"
        assert not (outside / "copy.rdl").exists()

    def test_nested_batch_op_path_arguments_are_checked(self, server, project, monkeypatch):
        proj, outside = project
        before = (proj / "report.rdl").read_bytes()
        png = outside / "logo.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(proj))
        op = {
            "tool": "add_embedded_image",
            "args": {"name": "Logo", "mime_type": "image/png", "image_path": str(png)},
        }
        result = _call(server, "apply_edits", path=str(proj / "report.rdl"), ops=[op])
        assert result.get("isError") or "PathNotAllowed" in result["content"][0]["text"]
        assert (proj / "report.rdl").read_bytes() == before

    def test_dry_run_preview_copy_is_allowed(self, server, project, monkeypatch):
        proj, _ = project
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(proj))
        op = {"tool": "set_textbox_value", "args": {"textbox_name": "Amount", "value": "x"}}
        result = _call(server, "dry_run_edit", path=str(proj / "report.rdl"), ops=[op])
        assert _payload(result)["applied"][0]["ok"] is True

    def test_star_is_unrestricted(self, server, project, monkeypatch):
        _, outside = project
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, "*")
        assert not _call(server, "describe_report", path=str(outside / "report.rdl")).get("isError")

    def test_several_roots(self, server, project, monkeypatch):
        proj, outside = project
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, f"{proj}{os.pathsep}{outside}")
        assert not _call(server, "describe_report", path=str(outside / "report.rdl")).get("isError")

    def test_in_process_default_is_unrestricted(self, server, project):
        _, outside = project
        assert not _call(server, "describe_report", path=str(outside / "report.rdl")).get("isError")

    def test_server_default_is_the_working_directory(self, server, project, monkeypatch):
        proj, outside = project
        monkeypatch.chdir(proj)
        security.use_server_defaults()
        assert not _call(server, "describe_report", path="report.rdl").get("isError")
        result = _call(server, "describe_report", path=str(outside / "report.rdl"))
        assert _payload(result)["error_type"] == "PathNotAllowed"


# ---- confirmation ------------------------------------------------------------------


class TestConfirmation:
    def test_destructive_without_confirm_does_not_touch_the_file(
        self, server, project, monkeypatch
    ):
        proj, _ = project
        report = proj / "report.rdl"
        before = report.read_bytes()
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        result = _call(server, "remove_body_item", path=str(report), name="SalesByProduct")
        assert result["isError"] is True
        payload = _payload(result)
        assert payload["error_type"] == "ConfirmationRequired"
        assert "confirm=true" in payload["message"]
        assert report.read_bytes() == before

    def test_confirm_true_runs(self, server, project, monkeypatch):
        proj, _ = project
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        result = _call(
            server,
            "remove_body_item",
            path=str(proj / "report.rdl"),
            name="SalesByProduct",
            confirm=True,
        )
        assert _payload(result)["removed"] == "SalesByProduct"

    def test_apply_edits_with_destructive_op_needs_confirm(self, server, project, monkeypatch):
        proj, _ = project
        report = proj / "report.rdl"
        before = report.read_bytes()
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        ops = [{"tool": "remove_body_item", "args": {"name": "SalesByProduct"}}]
        refused = _call(server, "apply_edits", path=str(report), ops=ops)
        assert _payload(refused)["error_type"] == "ConfirmationRequired"
        assert report.read_bytes() == before
        done = _call(server, "apply_edits", path=str(report), ops=ops, confirm=True)
        assert not done.get("isError"), done
        assert report.read_bytes() != before

    def test_apply_edits_without_destructive_ops_needs_no_confirm(
        self, server, project, monkeypatch
    ):
        proj, _ = project
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        ops = [{"tool": "set_textbox_value", "args": {"textbox_name": "Amount", "value": "x"}}]
        assert not _call(server, "apply_edits", path=str(proj / "report.rdl"), ops=ops).get(
            "isError"
        )

    def test_dry_run_preview_of_destructive_op_needs_no_confirm(self, server, project, monkeypatch):
        proj, _ = project
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        ops = [{"tool": "remove_body_item", "args": {"name": "SalesByProduct"}}]
        out = _payload(_call(server, "dry_run_edit", path=str(proj / "report.rdl"), ops=ops))
        assert out["applied"][0]["ok"] is True

    def test_transaction_routed_destructive_call_needs_confirm(self, server, project, monkeypatch):
        proj, _ = project
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        tx = _payload(_call(server, "start_editing_transaction", path=str(proj / "report.rdl")))
        try:
            result = _call(
                server,
                "remove_body_item",
                name="SalesByProduct",
                transaction_id=tx["transaction_id"],
            )
            assert _payload(result)["error_type"] == "ConfirmationRequired"
        finally:
            _call(server, "cancel_editing_transaction", transaction_id=tx["transaction_id"])

    def test_in_process_default_needs_no_confirm(self, server, project):
        proj, _ = project
        result = _call(
            server, "remove_body_item", path=str(proj / "report.rdl"), name="SalesByProduct"
        )
        assert not result.get("isError")

    def test_env_can_turn_it_off(self, server, project, monkeypatch):
        proj, _ = project
        security.use_server_defaults(str(proj))
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "0")
        result = _call(
            server, "remove_body_item", path=str(proj / "report.rdl"), name="SalesByProduct"
        )
        assert not result.get("isError")


class TestConfirmationInputRequired2026:
    """2026-07-28 + elicitation-capable client: confirmation is an
    `input_required` round trip (MRTR) instead of an error."""

    def _remove(self, server, report, extra=None, meta=MODERN_META):
        return _call(
            server,
            "remove_body_item",
            meta=meta,
            extra=extra,
            path=str(report),
            name="SalesByProduct",
        )

    def test_first_round_asks_and_does_not_touch_the_file(self, server, project, monkeypatch):
        proj, _ = project
        report = proj / "report.rdl"
        before = report.read_bytes()
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        result = self._remove(server, report)
        assert result["resultType"] == "input_required"
        ask = result["inputRequests"]["confirm"]
        assert ask["method"] == "elicitation/create"
        assert ask["params"]["requestedSchema"]["properties"]["confirm"]["type"] == "boolean"
        assert result["requestState"]
        assert report.read_bytes() == before

    def test_accepted_retry_runs(self, server, project, monkeypatch):
        proj, _ = project
        report = proj / "report.rdl"
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        state = self._remove(server, report)["requestState"]
        retry = {
            "inputResponses": {"confirm": {"action": "accept", "content": {"confirm": True}}},
            "requestState": state,
        }
        result = self._remove(server, report, extra=retry)
        assert result["resultType"] == "complete"
        assert _payload(result)["removed"] == "SalesByProduct"

    def test_declined_retry_does_not_run(self, server, project, monkeypatch):
        proj, _ = project
        report = proj / "report.rdl"
        before = report.read_bytes()
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        state = self._remove(server, report)["requestState"]
        retry = {"inputResponses": {"confirm": {"action": "decline"}}, "requestState": state}
        result = self._remove(server, report, extra=retry)
        assert _payload(result)["error_type"] == "ConfirmationDeclined"
        assert report.read_bytes() == before

    def test_confirmation_is_bound_to_the_arguments(self, server, project, monkeypatch):
        proj, _ = project
        report = proj / "report.rdl"
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        state = self._remove(server, report)["requestState"]
        retry = {
            "inputResponses": {"confirm": {"action": "accept", "content": {"confirm": True}}},
            "requestState": state,
        }
        other = _call(
            server,
            "remove_body_item",
            meta=MODERN_META,
            extra=retry,
            path=str(report),
            name="MainTable",
        )
        assert other["resultType"] == "input_required"

    def test_client_without_elicitation_gets_the_error(self, server, project, monkeypatch):
        proj, _ = project
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        meta = {**MODERN_META, "io.modelcontextprotocol/clientCapabilities": {}}
        result = self._remove(server, proj / "report.rdl", meta=meta)
        assert _payload(result)["error_type"] == "ConfirmationRequired"


# ---- audit log -------------------------------------------------------------------------


class TestAuditLog:
    def _lines(self, log):
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_every_mutation_is_recorded(self, server, project, monkeypatch, tmp_path):
        proj, _ = project
        log = tmp_path / "audit.jsonl"
        monkeypatch.setenv(security.AUDIT_LOG_ENV, str(log))
        report = proj / "report.rdl"
        _call(server, "describe_report", path=str(report))  # read: not logged
        _call(server, "set_textbox_style", path=str(report), textbox_name="Amount", color="Red")
        _call(server, "remove_body_item", path=str(report), name="SalesByProduct")
        _call(server, "remove_body_item", path=str(report), name="NoSuchItem")  # fails
        entries = self._lines(log)
        assert [e["tool"] for e in entries] == [
            "set_textbox_style",
            "remove_body_item",
            "remove_body_item",
        ]
        first = entries[0]
        assert first["path"] == str(report.resolve())
        assert first["ok"] is True
        assert len(first["args_sha256"]) == 64
        assert first["ts"].endswith("Z")
        assert "changed" in first and first["in_transaction"] is False
        assert entries[2]["ok"] is False
        assert entries[2]["error_type"]

    def test_transaction_edits_and_commit_are_recorded(
        self, server, project, monkeypatch, tmp_path
    ):
        proj, _ = project
        log = tmp_path / "audit.jsonl"
        monkeypatch.setenv(security.AUDIT_LOG_ENV, str(log))
        report = proj / "report.rdl"
        tx = _payload(_call(server, "start_editing_transaction", path=str(report)))
        _call(
            server,
            "set_textbox_value",
            textbox_name="Amount",
            value="x",
            transaction_id=tx["transaction_id"],
        )
        _call(server, "commit_editing_transaction", transaction_id=tx["transaction_id"], force=True)
        entries = self._lines(log)
        assert [e["tool"] for e in entries] == ["set_textbox_value", "commit_editing_transaction"]
        assert entries[0]["in_transaction"] is True
        assert entries[1]["path"] == str(report.resolve())
        # The opaque transaction id never lands in the log.
        assert tx["transaction_id"] not in log.read_text()

    def test_dry_run_previews_are_not_recorded(self, server, project, monkeypatch, tmp_path):
        proj, _ = project
        log = tmp_path / "audit.jsonl"
        monkeypatch.setenv(security.AUDIT_LOG_ENV, str(log))
        op = {"tool": "set_textbox_value", "args": {"textbox_name": "Amount", "value": "x"}}
        _call(server, "dry_run_edit", path=str(proj / "report.rdl"), ops=[op])
        assert self._lines(log) == []

    def test_refused_calls_are_not_mutations(self, server, project, monkeypatch, tmp_path):
        proj, _ = project
        log = tmp_path / "audit.jsonl"
        monkeypatch.setenv(security.AUDIT_LOG_ENV, str(log))
        monkeypatch.setenv(security.REQUIRE_CONFIRM_ENV, "1")
        _call(server, "remove_body_item", path=str(proj / "report.rdl"), name="SalesByProduct")
        assert self._lines(log) == []

    def test_log_is_append_only(self, server, project, monkeypatch, tmp_path):
        proj, _ = project
        log = tmp_path / "audit.jsonl"
        log.write_text('{"earlier": true}\n')
        monkeypatch.setenv(security.AUDIT_LOG_ENV, str(log))
        _call(
            server,
            "set_textbox_style",
            path=str(proj / "report.rdl"),
            textbox_name="Amount",
            color="Red",
        )
        lines = log.read_text().splitlines()
        assert lines[0] == '{"earlier": true}'
        assert len(lines) == 2


# ---- the stdio server's defaults -------------------------------------------------------


def _server_env():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in (security.ALLOWED_ROOTS_ENV, security.REQUIRE_CONFIRM_ENV)
    }
    return {**env, "PYTHONPATH": str(ROOT)}


def test_stdio_server_defaults_cwd_root_and_confirmation(project):
    from tests.test_protocol_conformance import StdioClient

    proj, outside = project
    report = proj / "report.rdl"
    before = report.read_bytes()
    no_elicit = {"io.modelcontextprotocol/clientCapabilities": {}}
    remove = {
        "name": "remove_body_item",
        "arguments": {"path": "report.rdl", "name": "SalesByProduct"},
    }
    client = StdioClient(cwd=proj, env=_server_env())
    try:
        outside_call = client.modern(
            "tools/call",
            {"name": "describe_report", "arguments": {"path": str(outside / "report.rdl")}},
        )
        assert _payload(outside_call["result"])["error_type"] == "PathNotAllowed"
        inside_call = client.modern(
            "tools/call", {"name": "describe_report", "arguments": {"path": "report.rdl"}}
        )
        assert not inside_call["result"].get("isError")
        refused = client.modern("tools/call", remove, **no_elicit)
        assert _payload(refused["result"])["error_type"] == "ConfirmationRequired"
        asked = client.modern(
            "tools/call",
            remove,
            **{"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}},
        )
        assert asked["result"]["resultType"] == "input_required"
    finally:
        client.close()
    assert report.read_bytes() == before


def test_sdk_client_confirms_through_elicitation(project):
    """The SDK client's MRTR driver answers input_required with its
    elicitation callback and retries; the edit then runs."""
    import anyio
    import mcp_types as types
    from mcp import Client, StdioServerParameters

    proj, _ = project
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "pbirb_mcp_server.py")],
        cwd=str(proj),
        env=_server_env(),
    )
    asked = []

    async def on_elicit(context, request_params):
        asked.append(request_params.message)
        return types.ElicitResult(action="accept", content={"confirm": True})

    async def main():
        async with Client(params, mode="auto", elicitation_callback=on_elicit) as c:
            return await c.call_tool(
                "remove_body_item", {"path": "report.rdl", "name": "SalesByProduct"}
            )

    result = anyio.run(main)
    assert asked and "remove_body_item" in asked[0]
    assert not result.is_error
    assert json.loads(result.content[0].text)["removed"] == "SalesByProduct"
