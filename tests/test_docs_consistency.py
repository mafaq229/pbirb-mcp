"""Docs and tool descriptions stay true to the code (issue #62).

* every ``PBIRB_MCP_*`` variable the code reads is in the README's
  environment-variable table;
* rule counts written in the docs match the lint registry;
* every tool has a section in docs/TOOLS.md (the long-form notes the short
  ``tools/list`` descriptions point to);
* descriptions are short, purpose-first and free of release history, and
  ``tools/list`` stays inside its size budget.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pbirb_mcp.ops import lint
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
TOOLS_MD = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")

MAX_DESCRIPTION = 400
# Budgets for the default (all tools) tools/list response. Raising them
# needs a reason: every byte is sent to the model each session.
MAX_DESCRIPTION_BYTES = 28_000
MAX_TOOLS_LIST_BYTES = 175_000
HISTORY = re.compile(r"v0\.\d|commit \d|BREAKING|was bare|migrate with|Phase \d")


def _code_files():
    yield ROOT / "pbirb_mcp_server.py"
    yield from sorted((ROOT / "pbirb_mcp").rglob("*.py"))


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n#", start + len(heading))
    return text[start : nxt if nxt != -1 else len(text)]


@pytest.fixture(scope="module")
def tools_list():
    srv = MCPServer()
    register_all_tools(srv)
    return srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


class TestEnvironmentVariables:
    def test_every_env_var_in_the_code_is_in_the_readme_table(self):
        names = set()
        for path in _code_files():
            names |= set(re.findall(r"PBIRB_MCP_[A-Z0-9_]+", path.read_text(encoding="utf-8")))
        assert len(names) >= 10, names
        table = _section(README, "### Environment variables")
        missing = sorted(n for n in names if f"`{n}`" not in table)
        assert not missing, f"add to README § Environment variables: {missing}"

    def test_auto_backup_is_wired_up_and_documented(self):
        # #63: the dispatcher calls the hook, so the README must not call
        # it unimplemented any more.
        server_py = (ROOT / "pbirb_mcp" / "server.py").read_text(encoding="utf-8")
        assert "maybe_auto_backup(" in server_py
        row = next(
            line for line in README.splitlines() if line.startswith("| `PBIRB_MCP_AUTO_BACKUP`")
        )
        assert "Not implemented" not in row
        assert "auto_backup" in row
        assert "not implemented" not in _section(TOOLS_MD, "### `backup_report`").lower()

    def test_readme_shows_allowed_roots_in_a_client_config(self):
        assert '"PBIRB_MCP_ALLOWED_ROOTS":' in README


class TestRuleCounts:
    DOCS = [
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        *sorted((ROOT / "docs").glob("*.md")),
        ROOT / "pbirb_mcp" / "ops" / "validate.py",
        ROOT / "pbirb_mcp" / "ops" / "lint.py",
    ]

    @pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
    def test_written_rule_counts_match_the_registry(self, path):
        text = path.read_text(encoding="utf-8")
        counts = re.findall(r"\b(\d+)\s+(?:lint\s+)?rules\b", text)
        counts += re.findall(r"other\s+(\d+)\s+(?:lint\s+)?rules", text)
        wrong = [c for c in counts if int(c) != len(lint.ALL_RULES)]
        assert not wrong, f"{path.name} says {wrong} rules; there are {len(lint.ALL_RULES)}"


class TestToolDocs:
    def test_every_tool_has_a_tools_md_section(self, tools_list):
        names = {t["name"] for t in tools_list["result"]["tools"]}
        documented = set(re.findall(r"^### `([a-z_]+)`", TOOLS_MD, flags=re.M))
        assert names - documented == set(), "add to docs/TOOLS.md"
        assert documented - names == set(), "stale docs/TOOLS.md sections"

    def test_tools_md_has_no_release_history(self):
        assert not HISTORY.search(TOOLS_MD)


class TestDescriptions:
    def test_short_and_free_of_release_history(self, tools_list):
        for tool in tools_list["result"]["tools"]:
            desc = tool["description"]
            assert 20 <= len(desc) <= MAX_DESCRIPTION, (tool["name"], len(desc))
            assert not HISTORY.search(desc), (tool["name"], desc)

    def test_tools_list_size_budget(self, tools_list):
        tools = tools_list["result"]["tools"]
        assert sum(len(t["description"]) for t in tools) <= MAX_DESCRIPTION_BYTES
        assert len(json.dumps(tools_list)) <= MAX_TOOLS_LIST_BYTES
