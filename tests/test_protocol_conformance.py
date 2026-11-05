"""Issue #53: conformance smoke test over a real stdio pipe.

Spawns the ``pbirb-mcp`` entry point and speaks newline-delimited JSON-RPC
to it as a 2025-11-25 (handshake) client and as a 2026-07-28 (stateless)
client.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "pbi_paginated_minimal.rdl"

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "conformance", "version": "1"},
}


class StdioClient:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "pbirb_mcp_server.py")],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next_id = 0

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout: float = 20.0) -> dict:
        line = self._lines.get(timeout=timeout)
        assert line is not None, "server closed stdout: " + self.proc.stderr.read()
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        resp = self.read()
        assert resp.get("id") == self._next_id, resp
        return resp

    def modern(self, method: str, params: dict | None = None, **meta) -> dict:
        return self.request(method, {**(params or {}), "_meta": {**MODERN_META, **meta}})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.stdout.close()
            self.proc.stderr.close()


@pytest.fixture
def client():
    c = StdioClient()
    yield c
    c.close()


def test_2025_11_25_handshake_client(client):
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "conformance", "version": "1"},
        },
    )
    assert init["result"]["protocolVersion"] == "2025-11-25"
    assert init["result"]["serverInfo"]["name"] == "pbirb-mcp"
    assert "tools" in init["result"]["capabilities"]
    # A notification gets no response: the next line is the ping's.
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert client.request("ping")["result"] == {}

    tools = client.request("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert "describe_report" in names
    assert names == [t["name"] for t in client.request("tools/list")["result"]["tools"]]

    # A tools/call notification (no id) is neither answered nor run.
    client.send({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "describe_report"}})
    call = client.request(
        "tools/call", {"name": "describe_report", "arguments": {"path": str(FIXTURE)}}
    )
    assert not call["result"].get("isError")
    assert "datasets" in json.loads(call["result"]["content"][0]["text"])

    unknown = client.request("tools/call", {"name": "no_such_tool", "arguments": {}})
    assert unknown["error"]["code"] == -32602
    assert client.request("server/discover")["error"]["code"] == -32601


def test_2026_07_28_stateless_client(client):
    discover = client.modern("server/discover")["result"]
    assert "2026-07-28" in discover["supportedVersions"]
    assert "tools" in discover["capabilities"]
    assert discover["resultType"] == "complete"

    listing = client.modern("tools/list")["result"]
    assert listing["resultType"] == "complete"
    assert "ttlMs" in listing and "cacheScope" in listing
    assert any(t["name"] == "describe_report" for t in listing["tools"])

    call = client.modern(
        "tools/call", {"name": "describe_report", "arguments": {"path": str(FIXTURE)}}
    )["result"]
    assert call["resultType"] == "complete"
    assert "datasets" in json.loads(call["content"][0]["text"])

    assert client.modern("tools/call", {"name": "nope"})["error"]["code"] == -32602
    # 2026-07-28 removed ping and the initialize handshake.
    assert client.modern("ping")["error"]["code"] == -32601
    unsupported = client.modern(
        "tools/list", **{"io.modelcontextprotocol/protocolVersion": "2099-01-01"}
    )
    assert unsupported["error"]["code"] == -32022
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "conformance", "version": "1"},
        },
    )
    assert init["error"]["code"] == -32022


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_sdk_client_interop(mode):
    """The SDK's own client talks to the server in both eras ('auto'
    probes server/discover and adopts 2026-07-28)."""
    import anyio
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable, args=[str(ROOT / "pbirb_mcp_server.py")], cwd=str(ROOT)
    )

    async def main():
        async with Client(params, mode=mode) as c:
            listing = await c.list_tools()
            result = await c.call_tool("describe_report", {"path": str(FIXTURE)})
            return c.protocol_version, listing, result

    version, listing, result = anyio.run(main)
    assert version == ("2025-11-25" if mode == "legacy" else "2026-07-28")
    assert any(t.name == "describe_report" for t in listing.tools)
    assert not result.is_error
    assert "datasets" in json.loads(result.content[0].text)


def test_older_handshake_revision_negotiates_down(client):
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "conformance", "version": "1"},
        },
    )
    assert init["result"]["protocolVersion"] == "2024-11-05"
