"""Tests for dry_run_edit (Phase 7 commit 32).

Round-trip discipline: the source file's bytes must be unchanged after
``dry_run_edit`` returns, regardless of whether the ops succeeded or
failed. Diff + verify are computed against the tempfile state.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.ops.dry_run import dry_run_edit
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


class TestDryRunEdit:
    def test_no_ops_returns_empty_diff_and_clean_verify(self, rdl_path):
        before = rdl_path.read_bytes()
        result = dry_run_edit(str(rdl_path), [])
        assert result["applied"] == []
        assert result["diff"] == ""
        assert result["verify"]["valid"] is True
        # Original untouched.
        assert rdl_path.read_bytes() == before

    def test_single_op_produces_diff(self, rdl_path):
        before = rdl_path.read_bytes()
        result = dry_run_edit(
            str(rdl_path),
            [
                {
                    "tool": "set_alternating_row_color",
                    "args": {
                        "tablix_name": "MainTable",
                        "color_a": "#ffffff",
                        "color_b": "#eeeeee",
                    },
                }
            ],
        )
        assert len(result["applied"]) == 1
        assert result["applied"][0]["ok"] is True
        assert result["diff"] != ""
        # Original file's bytes are unchanged.
        assert rdl_path.read_bytes() == before

    def test_failing_op_stops_dispatch(self, rdl_path):
        before = rdl_path.read_bytes()
        result = dry_run_edit(
            str(rdl_path),
            [
                {
                    "tool": "set_alternating_row_color",
                    "args": {
                        "tablix_name": "DoesNotExist",
                        "color_a": "#ffffff",
                        "color_b": "#eeeeee",
                    },
                },
                # Second op never runs because the first failed.
                {
                    "tool": "set_alternating_row_color",
                    "args": {
                        "tablix_name": "MainTable",
                        "color_a": "#ff0000",
                        "color_b": "#000000",
                    },
                },
            ],
        )
        assert len(result["applied"]) == 1
        assert result["applied"][0]["ok"] is False
        assert "error" in result["applied"][0]
        # Original file's bytes are unchanged.
        assert rdl_path.read_bytes() == before

    def test_path_arg_is_injected(self, rdl_path):
        # Caller did not supply a `path` arg; harness injects the tempfile.
        result = dry_run_edit(
            str(rdl_path),
            [
                {
                    "tool": "set_textbox_value",
                    "args": {"textbox_name": "HeaderProductID", "value": "ID"},
                }
            ],
        )
        assert result["applied"][0]["ok"] is True

    def test_unknown_tool_recorded_as_error(self, rdl_path):
        before = rdl_path.read_bytes()
        result = dry_run_edit(
            str(rdl_path),
            [{"tool": "no_such_tool", "args": {}}],
        )
        assert result["applied"][-1]["ok"] is False
        assert rdl_path.read_bytes() == before

    def test_verify_surfaces_lint_issues(self, rdl_path):
        # Apply an op that introduces a lint warning (unused-data-source
        # via add_data_source — adding a DataSource that no DataSet uses).
        result = dry_run_edit(
            str(rdl_path),
            [
                {
                    "tool": "add_data_source",
                    "args": {
                        "name": "UnusedDS",
                        "workspace_url": "powerbi://api.powerbi.com/v1.0/myorg/Workspace",
                        "dataset_name": "Dataset",
                    },
                }
            ],
        )
        assert result["applied"][0]["ok"] is True
        rules = [i["rule"] for i in result["verify"]["issues"]]
        assert "unused-data-source" in rules

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            dry_run_edit(str(tmp_path / "nope.rdl"), [])


class TestToolRegistration:
    def test_dry_run_edit_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "dry_run_edit" in names
