"""Issue #86: the server survives a bad log path, expands ``~`` in path
arguments, and refuses non-RDL XML in readers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pbirb_mcp.core import security
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "pbi_paginated_minimal.rdl"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "startup-test", "version": "1"},
    },
}


def _run_server(env_overrides: dict, cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PBIRB_MCP_")}
    env.update(env_overrides)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, str(ROOT / "pbirb_mcp_server.py")],
        input=json.dumps(INITIALIZE) + "\n",
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def _initialize_result(proc: subprocess.CompletedProcess) -> dict:
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, f"no response on stdout; stderr: {proc.stderr}"
    resp = json.loads(lines[0])
    assert resp.get("id") == 1, resp
    return resp["result"]


class TestLogFile:
    def test_missing_log_directory_falls_back_to_stderr(self, tmp_path):
        bad = tmp_path / "no-such-dir" / "pbirb.log"
        proc = _run_server({"PBIRB_MCP_LOG_FILE": str(bad)}, tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert _initialize_result(proc)["serverInfo"]["name"] == "pbirb-mcp"
        assert "cannot open PBIRB_MCP_LOG_FILE" in proc.stderr
        assert str(bad) in proc.stderr
        assert not bad.exists()

    def test_log_file_tilde_is_expanded(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        proc = _run_server(
            {
                "HOME": str(home),
                "PBIRB_MCP_LOG_FILE": "~/pbirb.log",
                "PBIRB_MCP_LOG_LEVEL": "INFO",
            },
            tmp_path,
        )
        _initialize_result(proc)
        assert "pbirb-mcp starting" in (home / "pbirb.log").read_text(encoding="utf-8")
        assert not (tmp_path / "~").exists()


# ---- `~` in path arguments ---------------------------------------------------------


@pytest.fixture(scope="module")
def server():
    srv = MCPServer()
    register_all_tools(srv)
    return srv


@pytest.fixture(autouse=True)
def _clean_policy(monkeypatch):
    for var in (security.ALLOWED_ROOTS_ENV, security.REQUIRE_CONFIRM_ENV, security.AUDIT_LOG_ENV):
        monkeypatch.delenv(var, raising=False)
    security._reset_defaults_for_tests()
    yield
    security._reset_defaults_for_tests()


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "reports").mkdir(parents=True)
    shutil.copy(FIXTURE, home / "reports" / "report.rdl")
    monkeypatch.setenv("HOME", str(home))
    return home


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


class TestTildePaths:
    def test_expand_user_paths_helper(self, home):
        args = {"path": "~/a.rdl", "dst_path": "~/b.rdl", "name": "~x", "dest_dir": None}
        out = security.expand_user_paths(args)
        assert out["path"] == str(home / "a.rdl")
        assert out["dst_path"] == str(home / "b.rdl")
        assert out["name"] == "~x"  # not a path argument
        assert args["path"] == "~/a.rdl"  # input untouched
        plain = {"path": "/x.rdl"}
        assert security.expand_user_paths(plain) is plain

    def test_reader_accepts_tilde(self, server, home):
        result, payload = _call(server, "describe_report", path="~/reports/report.rdl")
        assert not result.get("isError"), payload
        assert payload["datasets"]

    def test_lint_and_validate_accept_tilde(self, server, home):
        result, payload = _call(server, "lint_report", path="~/reports/report.rdl")
        assert not result.get("isError"), payload
        result, payload = _call(server, "validate_report", path="~/reports/report.rdl")
        assert not result.get("isError"), payload
        assert payload["valid"] is True

    def test_mutating_tool_writes_expanded_path(self, server, home):
        report = home / "reports" / "report.rdl"
        before = report.read_bytes()
        result, payload = _call(
            server, "set_page_setup", path="~/reports/report.rdl", page_width="8in"
        )
        assert not result.get("isError"), payload
        assert report.read_bytes() != before
        assert not Path("~").exists()

    def test_star_path_arguments_expanded(self, server, home):
        result, payload = _call(
            server,
            "duplicate_report",
            src_path="~/reports/report.rdl",
            dst_path="~/reports/copy.rdl",
        )
        assert not result.get("isError"), payload
        assert (home / "reports" / "copy.rdl").is_file()

    def test_sandbox_checks_expanded_path(self, server, home, tmp_path, monkeypatch):
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(home))
        result, payload = _call(server, "describe_report", path="~/reports/report.rdl")
        assert not result.get("isError"), payload
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv(security.ALLOWED_ROOTS_ENV, str(elsewhere))
        result, payload = _call(server, "describe_report", path="~/reports/report.rdl")
        assert result.get("isError")
        assert payload["error_type"] == "PathNotAllowed"
        assert payload["resolved_path"] == str((home / "reports" / "report.rdl").resolve())


# ---- non-RDL XML ---------------------------------------------------------------------


class TestNonRdl:
    @pytest.fixture
    def html(self, tmp_path):
        p = tmp_path / "page.rdl"
        p.write_text("<html><body/></html>", encoding="utf-8")
        return p

    @pytest.mark.parametrize("tool", ["describe_report", "lint_report", "get_datasets"])
    def test_readers_refuse_non_rdl(self, server, html, tool):
        result, payload = _call(server, tool, path=str(html))
        assert result.get("isError"), payload
        assert payload["error_type"] == "NotAnRDLReport"
        assert "<html>" in payload["message"]
        assert payload["root_element"] == "html"

    def test_editor_refuses_non_rdl_and_leaves_file(self, server, html):
        before = html.read_bytes()
        result, payload = _call(server, "set_page_setup", path=str(html), page_width="8in")
        assert result.get("isError")
        assert payload["error_type"] == "NotAnRDLReport"
        assert html.read_bytes() == before

    def test_validate_report_still_reports_structurally(self, server, html):
        result, payload = _call(server, "validate_report", path=str(html))
        assert not result.get("isError"), payload
        assert payload["valid"] is False
        assert any(e["rule"] == "structural" for e in payload["errors"])
