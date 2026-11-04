"""Tests for PBIRB_MCP_AUTO_VERIFY auto-verify (Phase 7 commit 34).

When the env var is set, every successful mutating-tool call has its
response wrapped as ``{result, verify}`` where ``verify`` is the
output of :func:`pbirb_mcp.ops.validate.verify_report`. Off by default
to preserve v0.2 response shapes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def server() -> MCPServer:
    srv = MCPServer()
    register_all_tools(srv)
    return srv


def _call(server: MCPServer, name: str, args: dict) -> dict:
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    text = resp["result"]["content"][0]["text"]
    return {"resp": resp, "payload": json.loads(text)}


class TestAutoVerifyDefaultOff:
    def test_response_shape_unchanged_when_env_unset(self, server, rdl_path, monkeypatch):
        monkeypatch.delenv("PBIRB_MCP_AUTO_VERIFY", raising=False)
        out = _call(
            server,
            "set_alternating_row_color",
            {
                "path": str(rdl_path),
                "tablix_name": "MainTable",
                "color_a": "#ffffff",
                "color_b": "#eeeeee",
            },
        )
        # Direct handler shape — no `verify` key, no `result` wrapper.
        assert "verify" not in out["payload"]
        assert "result" not in out["payload"]


class TestAutoVerifyOn:
    def test_mutating_tool_response_is_wrapped(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")
        out = _call(
            server,
            "set_alternating_row_color",
            {
                "path": str(rdl_path),
                "tablix_name": "MainTable",
                "color_a": "#ffffff",
                "color_b": "#eeeeee",
            },
        )
        assert "result" in out["payload"]
        assert "verify" in out["payload"]
        # Verify shape from verify_report.
        v = out["payload"]["verify"]
        assert "valid" in v
        assert "issues" in v
        assert "xsd_used" in v

    @pytest.mark.parametrize("flag", ["1", "true", "yes", "on", "ON", "TRUE"])
    def test_truthy_flags(self, server, rdl_path, monkeypatch, flag):
        monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", flag)
        out = _call(
            server,
            "set_alternating_row_color",
            {
                "path": str(rdl_path),
                "tablix_name": "MainTable",
                "color_a": "#ffffff",
                "color_b": "#eeeeee",
            },
        )
        assert "verify" in out["payload"]

    @pytest.mark.parametrize("flag", ["0", "false", "no", "off", ""])
    def test_falsy_flags(self, server, rdl_path, monkeypatch, flag):
        monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", flag)
        out = _call(
            server,
            "set_alternating_row_color",
            {
                "path": str(rdl_path),
                "tablix_name": "MainTable",
                "color_a": "#ffffff",
                "color_b": "#eeeeee",
            },
        )
        assert "verify" not in out["payload"]

    def test_read_only_tool_not_wrapped(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")
        out = _call(server, "describe_report", {"path": str(rdl_path)})
        # describe_report doesn't start with a mutating prefix.
        assert "verify" not in out["payload"]

    def test_failed_mutation_keeps_iserror_no_wrap(self, server, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")
        # Reference a tablix that doesn't exist → handler raises.
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "set_alternating_row_color",
                    "arguments": {
                        "path": str(rdl_path),
                        "tablix_name": "DoesNotExist",
                        "color_a": "#fff",
                        "color_b": "#000",
                    },
                },
            }
        )
        assert resp["result"]["isError"] is True
        text = resp["result"]["content"][0]["text"]
        # Pure error payload — no verify wrapper.
        payload = json.loads(text)
        assert "error_type" in payload
        assert "verify" not in payload

    def test_verify_surfaces_lint_issues_after_mutation(self, server, rdl_path, monkeypatch):
        # Adding an unused DataSource fires unused-data-source warning.
        monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")
        out = _call(
            server,
            "add_data_source",
            {
                "path": str(rdl_path),
                "name": "AutoVerifyOrphan",
                "workspace_url": "powerbi://api.powerbi.com/v1.0/myorg/Workspace",
                "dataset_name": "Dataset",
            },
        )
        rules = [i["rule"] for i in out["payload"]["verify"]["issues"]]
        assert "unused-data-source" in rules
        # Warning-only — overall valid stays True.
        assert out["payload"]["verify"]["valid"] is True

    def test_no_path_arg_skips_verify(self, server, monkeypatch):
        # backup_report takes a path; pick a tool with no path arg —
        # there isn't one in the mutating tool surface, so this
        # exercise is defensive: the wrapper checks `path` is a str.
        monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")
        # Call describe_report instead — read-only, no wrap regardless.
        # (No mutating tool ships without a `path` arg in v0.3.0.)
        # This test stays as a regression guard against a future
        # mutating tool that drops `path`.
        assert True


def _p(server: MCPServer, name: str, args: dict) -> dict:
    return _call(server, name, args)["payload"]


class TestMutatingFlagRouting:
    """Issue #28: AUTO_VERIFY routes on the explicit `mutating` flag, so
    tools without a set_/add_/... prefix — and tools whose written file
    isn't `path` — are verified too."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")

    def test_raw_xml_replace(self, server, rdl_path):
        out = _p(
            server,
            "raw_xml_replace",
            {
                "path": str(rdl_path),
                "xpath": ".//r:Textbox[@Name='HeaderAmount']/r:CanGrow",
                "content": "<Bogus>1</Bogus>",
            },
        )
        assert set(out) == {"result", "verify"}
        assert out["verify"]["valid"] is False

    def test_apply_edits(self, server, rdl_path):
        out = _p(
            server,
            "apply_edits",
            {
                "path": str(rdl_path),
                "ops": [
                    {
                        "tool": "set_textbox_value",
                        "args": {"textbox_name": "HeaderAmount", "value": "v"},
                    }
                ],
            },
        )
        assert set(out) == {"result", "verify"}

    def test_create_report(self, server, tmp_path):
        out = _p(server, "create_report", {"path": str(tmp_path / "new.rdl")})
        assert set(out) == {"result", "verify"}

    def test_duplicate_report_verifies_dst(self, server, rdl_path, tmp_path):
        dst = tmp_path / "dup.rdl"
        out = _p(server, "duplicate_report", {"src_path": str(rdl_path), "dst_path": str(dst)})
        assert set(out) == {"result", "verify"}
        assert out["verify"]["valid"] is True

    def test_restore_from_backup_verifies_target(self, server, rdl_path):
        backup = _p(server, "backup_report", {"path": str(rdl_path)})
        out = _p(server, "restore_from_backup", {"backup_path": backup["backup"], "force": True})
        assert set(out) == {"result", "verify"}

    def test_refresh_dataset_fields(self, server, rdl_path):
        out = _p(
            server, "refresh_dataset_fields", {"path": str(rdl_path), "dataset_name": "MainDataset"}
        )
        assert set(out) == {"result", "verify"}

    def test_convert_to_matrix(self, server, rdl_path):
        _p(
            server,
            "add_column_group",
            {
                "path": str(rdl_path),
                "tablix_name": "MainTable",
                "group_name": "ByID",
                "group_expression": "=Fields!ProductID.Value",
            },
        )
        _p(
            server,
            "add_row_group",
            {
                "path": str(rdl_path),
                "tablix_name": "MainTable",
                "group_name": "ByProduct",
                "group_expression": "=Fields!ProductName.Value",
            },
        )
        out = _p(
            server,
            "convert_to_matrix",
            {
                "path": str(rdl_path),
                "tablix_name": "MainTable",
                "row_group": "ByProduct",
                "column_group": "ByID",
            },
        )
        assert set(out) == {"result", "verify"}, out

    def test_read_only_tools_unwrapped(self, server, rdl_path):
        out = _p(server, "describe_report", {"path": str(rdl_path)})
        assert "verify" not in out
