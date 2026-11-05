"""Toolsets and read-only mode (issue #56).

``--toolsets`` / ``PBIRB_MCP_TOOLSETS`` and ``--read-only`` /
``PBIRB_MCP_READ_ONLY`` limit what a server lists *and* what it runs: a
call outside the policy is refused at dispatch with an actionable
``isError``. With no setting the server lists every tool, as before.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from evals.tool_selection.cases import CASES as EVAL_CASES
from pbirb_mcp.core import toolsets
from pbirb_mcp.core.toolsets import (
    ALWAYS_ON,
    READ_ONLY_ENV,
    TOOLSETS,
    TOOLSETS_ENV,
    ToolsetPolicy,
    parse_toolsets,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(TOOLSETS_ENV, raising=False)
    monkeypatch.delenv(READ_ONLY_ENV, raising=False)


def _server(**kwargs) -> MCPServer:
    srv = MCPServer(**kwargs)
    register_all_tools(srv)
    return srv


def _names(srv) -> list[str]:
    resp = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return [t["name"] for t in resp["result"]["tools"]]


def _call(srv, name, arguments):
    resp = srv.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    result = resp["result"]
    return result, json.loads(result["content"][0]["text"])


@pytest.fixture
def rdl(tmp_path):
    path = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, path)
    return path


# ---- the toolset table --------------------------------------------------------


class TestTable:
    def test_every_registered_tool_is_in_exactly_one_toolset(self):
        srv = _server()
        listed = [t for tools in TOOLSETS.values() for t in tools]
        assert len(listed) == len(set(listed)), "a tool is in two toolsets"
        assert set(listed) == set(srv._tools), (
            f"unlisted: {sorted(set(srv._tools) - set(listed))}; "
            f"stale: {sorted(set(listed) - set(srv._tools))}"
        )
        for name, tool in srv._tools.items():
            assert tool.toolset in TOOLSETS and name in TOOLSETS[tool.toolset]

    def test_toolsets_match_the_eval_categories(self):
        # The eval's "subset" arm groups cases by these categories; each
        # case's reference solution uses only its category's toolset + core.
        for case in EVAL_CASES:
            if case.category in ("question", "decline"):
                continue
            assert case.category in TOOLSETS, case.category
            for tool, _args in case.oracle:
                assert toolsets.toolset_of(tool) in (case.category, ALWAYS_ON), (case.id, tool)

    def test_core_holds_what_batches_and_planning_need(self):
        assert {
            "describe_report",
            "apply_edits",
            "dry_run_edit",
            "start_editing_transaction",
            "commit_editing_transaction",
            "cancel_editing_transaction",
            "verify_report",
        } <= TOOLSETS[ALWAYS_ON]


class TestParse:
    def test_default_is_every_toolset(self):
        assert parse_toolsets(None) == frozenset(TOOLSETS)
        assert parse_toolsets("") == frozenset(TOOLSETS)
        assert parse_toolsets(" , ") == frozenset(TOOLSETS)

    def test_names_plus_core(self):
        assert parse_toolsets("data, Style") == {"core", "data", "style"}
        assert parse_toolsets(["chart"]) == {"core", "chart"}

    def test_all_and_default_keywords(self):
        assert parse_toolsets("all") == frozenset(TOOLSETS)
        assert parse_toolsets("default") == frozenset(TOOLSETS) - toolsets.OPT_IN_TOOLSETS

    def test_unknown_name_lists_the_valid_ones(self):
        with pytest.raises(ValueError, match="unknown toolset 'tables'.*tablix"):
            parse_toolsets("data,tables")


# ---- tools/list -----------------------------------------------------------------


class TestListing:
    def test_no_setting_lists_every_tool_in_registration_order(self):
        srv = _server()
        assert _names(srv) == list(srv._tools)

    def test_toolsets_limit_the_listing(self):
        srv = _server(toolsets="style,chart")
        names = set(_names(srv))
        assert names == TOOLSETS["core"] | TOOLSETS["style"] | TOOLSETS["chart"]

    def test_read_only_lists_exactly_the_read_only_hint_tools(self):
        srv = _server(read_only=True)
        listed = _names(srv)
        expected = [n for n, t in srv._tools.items() if t.annotations["readOnlyHint"]]
        assert listed == expected
        assert "dry_run_edit" in listed and "describe_report" in listed
        assert "apply_edits" not in listed and "set_textbox_value" not in listed

    def test_read_only_and_toolsets_combine(self):
        srv = _server(toolsets="data", read_only=True)
        names = set(_names(srv))
        assert "get_dataset" in names and "describe_report" in names
        assert "update_dataset_query" not in names and "get_textbox" not in names

    def test_environment_variables(self, monkeypatch):
        monkeypatch.setenv(TOOLSETS_ENV, "parameters")
        monkeypatch.setenv(READ_ONLY_ENV, "1")
        names = set(_names(_server()))
        assert "get_parameters" in names and "add_parameter" not in names
        assert "get_tablixes" not in names

    def test_explicit_settings_override_the_environment(self, monkeypatch):
        monkeypatch.setenv(TOOLSETS_ENV, "parameters")
        monkeypatch.setenv(READ_ONLY_ENV, "1")
        names = set(_names(_server(toolsets="chart", read_only=False)))
        assert "add_chart_series" in names and "get_parameters" not in names

    def test_bad_environment_value_fails_loudly(self, monkeypatch):
        monkeypatch.setenv(TOOLSETS_ENV, "everything")
        with pytest.raises(ValueError, match="unknown toolset"):
            MCPServer()


# ---- dispatch -------------------------------------------------------------------


class TestDispatch:
    def test_tool_outside_the_toolsets_is_refused(self, rdl):
        srv = _server(toolsets="style")
        before = rdl.read_bytes()
        result, payload = _call(srv, "add_parameter", {"path": str(rdl), "name": "Region"})
        assert result["isError"] is True
        assert payload["error_type"] == "ToolNotEnabled"
        assert payload["toolset"] == "parameters"
        assert payload["setting"] == TOOLSETS_ENV
        assert "parameters" in payload["message"] and TOOLSETS_ENV in payload["message"]
        assert rdl.read_bytes() == before

    def test_refusal_does_not_depend_on_arguments(self):
        # Refused before argument validation or transaction routing.
        srv = _server(toolsets="style")
        result, payload = _call(srv, "add_parameter", {"transaction_id": "nope"})
        assert payload["error_type"] == "ToolNotEnabled"
        assert "nope" not in json.dumps(payload)

    def test_enabled_tool_runs(self, rdl):
        srv = _server(toolsets="style")
        result, payload = _call(
            srv,
            "set_textbox_value",
            {"path": str(rdl), "textbox_name": "HeaderAmount", "value": "X"},
        )
        assert not result.get("isError"), payload

    def test_mutating_tool_is_refused_in_read_only_mode(self, rdl):
        srv = _server(read_only=True)
        before = rdl.read_bytes()
        result, payload = _call(
            srv,
            "set_textbox_value",
            {"path": str(rdl), "textbox_name": "HeaderAmount", "value": "X"},
        )
        assert result["isError"] is True
        assert payload["error_type"] == "ReadOnlyMode"
        assert payload["setting"] == READ_ONLY_ENV
        assert rdl.read_bytes() == before
        for name in ("apply_edits", "start_editing_transaction", "backup_report"):
            _result, payload = _call(srv, name, {"path": str(rdl), "ops": []})
            assert payload["error_type"] == "ReadOnlyMode", name

    def test_disabled_destructive_tool_is_refused_without_asking_the_user(self, rdl, monkeypatch):
        # A 2026-07-28 client that can elicit is not asked to confirm a call
        # the policy refuses anyway.
        monkeypatch.setenv("PBIRB_MCP_REQUIRE_CONFIRM", "1")
        srv = _server(read_only=True)
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        }
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "remove_parameter",
                    "arguments": {"path": str(rdl), "name": "DateTo"},
                    "_meta": meta,
                },
            }
        )
        result = resp["result"]
        assert result["isError"] is True, result
        assert json.loads(result["content"][0]["text"])["error_type"] == "ReadOnlyMode"

    def test_read_tool_runs_in_read_only_mode(self, rdl):
        srv = _server(read_only=True)
        result, payload = _call(srv, "describe_report", {"path": str(rdl)})
        assert not result.get("isError"), payload


class TestBatches:
    OP = {"tool": "set_textbox_value", "args": {"textbox_name": "HeaderAmount", "value": "X"}}
    OFF = {"tool": "add_parameter", "args": {"name": "Region", "type": "String"}}

    def test_apply_edits_runs_allowed_ops_with_its_internal_transaction(self, rdl):
        # start/commit/cancel_editing_transaction are core, but apply_edits
        # must work even when its ops' toolset is the only one enabled.
        srv = _server(toolsets="style")
        result, payload = _call(srv, "apply_edits", {"path": str(rdl), "ops": [self.OP]})
        assert not result.get("isError"), payload
        assert payload["committed"] is True
        assert b"<Value>X</Value>" in rdl.read_bytes()

    def test_apply_edits_refuses_an_op_outside_the_toolsets(self, rdl):
        srv = _server(toolsets="style")
        before = rdl.read_bytes()
        result, payload = _call(srv, "apply_edits", {"path": str(rdl), "ops": [self.OP, self.OFF]})
        assert result["isError"] is True
        assert payload["error_type"] == "ToolNotEnabled"
        assert payload["op_index"] == 1 and payload["tool"] == "add_parameter"
        assert rdl.read_bytes() == before

    def test_dry_run_refuses_an_op_outside_the_toolsets(self, rdl):
        srv = _server(toolsets="style")
        result, payload = _call(srv, "dry_run_edit", {"path": str(rdl), "ops": [self.OFF]})
        assert result["isError"] is True
        assert payload["error_type"] == "ToolNotEnabled"

    def test_dry_run_previews_edits_in_read_only_mode(self, rdl):
        srv = _server(read_only=True)
        before = (rdl.read_bytes(), os.stat(rdl).st_mtime_ns)
        result, payload = _call(srv, "dry_run_edit", {"path": str(rdl), "ops": [self.OP]})
        assert not result.get("isError"), payload
        assert payload["applied"][0]["ok"] is True, payload
        assert "X" in payload["diff"]
        assert (rdl.read_bytes(), os.stat(rdl).st_mtime_ns) == before

    def test_nested_write_to_a_real_file_is_refused_in_read_only_mode(self):
        policy = ToolsetPolicy(read_only=True)
        assert policy.refusal("style", "set_textbox_value", read_only_tool=False, nested=True)
        assert (
            policy.refusal(
                "style", "set_textbox_value", read_only_tool=False, nested=True, preview_only=True
            )
            is None
        )

    def test_no_policy_inside_plain_library_calls(self, rdl):
        # Calling the op function directly (no dispatcher) is unrestricted.
        from pbirb_mcp.ops.transactions import apply_edits

        assert toolsets.active_policy() is None
        out = apply_edits(str(rdl), [self.OFF])
        assert out["committed"] is True


# ---- the console entry point ----------------------------------------------------


class TestCli:
    def test_list_toolsets(self, capsys):
        import pbirb_mcp_server

        assert pbirb_mcp_server.main(["--list-toolsets"]) == 0
        out = capsys.readouterr().out
        for name in TOOLSETS:
            assert name in out
        assert "core (always on)" in out

    def test_unknown_toolset_exits_2(self, capsys):
        import pbirb_mcp_server

        assert pbirb_mcp_server.main(["--toolsets", "bogus"]) == 2
        assert "unknown toolset 'bogus'" in capsys.readouterr().err

    def test_flags_over_a_real_pipe(self, tmp_path):
        from tests.test_protocol_conformance import StdioClient

        env = {k: v for k, v in os.environ.items() if k not in (TOOLSETS_ENV, READ_ONLY_ENV)}
        env["PYTHONPATH"] = str(ROOT)
        shutil.copy(FIXTURE, tmp_path / "r.rdl")
        client = StdioClient(cwd=tmp_path, env=env, args=("--toolsets", "chart", "--read-only"))
        try:
            client.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            )
            names = {t["name"] for t in client.request("tools/list")["result"]["tools"]}
            assert "get_chart" in names and "describe_report" in names
            assert "add_chart_series" not in names and "get_parameters" not in names
            resp = client.request(
                "tools/call",
                {
                    "name": "set_chart_title",
                    "arguments": {"path": str(tmp_path / "r.rdl"), "chart_name": "C", "text": "T"},
                },
            )
            payload = json.loads(resp["result"]["content"][0]["text"])
            assert payload["error_type"] == "ReadOnlyMode"
        finally:
            client.close()
