"""Issue #53: the protocol layer is the official MCP SDK.

In-process checks through ``MCPServer.handle_request`` (the adapter the
test suite, apply_edits / dry_run_edit and the eval harness dispatch
through). ``test_protocol_conformance.py`` repeats the essentials over a
real stdio pipe.
"""

import json
from pathlib import Path

import pytest

from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"

_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
}


def _request(method, params=None, request_id=1):
    req = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        req["params"] = params
    return req


def _modern(method, params=None, request_id=1, version="2026-07-28"):
    meta = {**_MODERN_META, "io.modelcontextprotocol/protocolVersion": version}
    return _request(method, {**(params or {}), "_meta": meta}, request_id)


def _init(version):
    return _request(
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    )


@pytest.fixture
def server():
    return MCPServer()


@pytest.fixture(scope="module")
def full_server():
    srv = MCPServer()
    register_all_tools(srv)
    return srv


class TestVersionNegotiation:
    @pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
    def test_initialize_echoes_supported_handshake_version(self, server, version):
        resp = server.handle_request(_init(version))
        assert resp["result"]["protocolVersion"] == version

    def test_initialize_counter_offers_latest_handshake_version(self, server):
        resp = server.handle_request(_init("1999-01-01"))
        assert resp["result"]["protocolVersion"] == "2025-11-25"

    def test_malformed_initialize_is_invalid_params(self, server):
        resp = server.handle_request(_request("initialize", {}))
        assert resp["error"]["code"] == -32602

    def test_ping_on_handshake_era(self, server):
        resp = server.handle_request(_request("ping"))
        assert resp == {"jsonrpc": "2.0", "id": 1, "result": {}}

    def test_ping_removed_in_2026_07_28(self, server):
        resp = server.handle_request(_modern("ping"))
        assert resp["error"]["code"] == -32601

    def test_server_discover_2026_07_28(self, server):
        result = server.handle_request(_modern("server/discover"))["result"]
        assert "2026-07-28" in result["supportedVersions"]
        assert "tools" in result["capabilities"]
        assert result["resultType"] == "complete"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "pbirb-mcp"

    def test_server_discover_not_on_handshake_era(self, server):
        resp = server.handle_request(_request("server/discover"))
        assert resp["error"]["code"] == -32601

    def test_unsupported_modern_version(self, server):
        resp = server.handle_request(_modern("tools/list", version="2099-01-01"))
        assert resp["error"]["code"] == -32022  # UnsupportedProtocolVersionError
        assert resp["error"]["data"]["requested"] == "2099-01-01"
        assert "2026-07-28" in resp["error"]["data"]["supported"]

    def test_envelope_missing_capabilities_is_invalid_params(self, server):
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
        resp = server.handle_request(_request("tools/list", {"_meta": meta}))
        assert resp["error"]["code"] == -32602


class TestResultShapes:
    def test_tools_list_2026_carries_result_type_and_cache_hints(self, full_server):
        result = full_server.handle_request(_modern("tools/list"))["result"]
        assert result["resultType"] == "complete"
        assert isinstance(result["ttlMs"], int)
        assert result["cacheScope"] in ("public", "private")

    def test_tools_list_order_is_deterministic(self, full_server):
        other = MCPServer()
        register_all_tools(other)
        first = full_server.handle_request(_modern("tools/list"))["result"]["tools"]
        again = other.handle_request(_modern("tools/list"))["result"]["tools"]
        names = [t["name"] for t in first]
        assert names == [t["name"] for t in again]
        assert len(names) == len(set(names))

    def test_stateless_tools_call(self, full_server):
        args = {"path": str(FIXTURE)}
        resp = full_server.handle_request(
            _modern("tools/call", {"name": "describe_report", "arguments": args})
        )
        result = resp["result"]
        assert result["resultType"] == "complete"
        assert result.get("isError") in (None, False)
        assert "datasets" in json.loads(result["content"][0]["text"])

    def test_handshake_era_results_have_no_2026_fields(self, full_server):
        result = full_server.handle_request(_request("tools/list"))["result"]
        assert "resultType" not in result
        assert "ttlMs" not in result

    def test_unknown_tool_is_invalid_params_on_2026(self, full_server):
        resp = full_server.handle_request(_modern("tools/call", {"name": "nope"}))
        assert resp["error"]["code"] == -32602

    def test_non_object_arguments_is_invalid_params(self, full_server):
        params = {"name": "describe_report", "arguments": ["x"]}
        resp = full_server.handle_request(_request("tools/call", params))
        assert resp["error"]["code"] == -32602


class TestNotificationsNeverAnswered:
    @pytest.mark.parametrize("method", ["tools/list", "tools/call", "ping", "initialize", "nope"])
    def test_request_without_id_gets_no_response(self, full_server, method):
        assert full_server.handle_request({"jsonrpc": "2.0", "method": method}) is None

    def test_tools_call_without_id_does_not_run(self, server):
        calls = []
        server.register_tool(
            name="count",
            description="counts",
            input_schema={"type": "object", "additionalProperties": True},
            handler=lambda **_: calls.append(1) or {},
        )
        notif = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "count"}}
        assert server.handle_request(notif) is None
        assert calls == []


class TestInProcessAdapter:
    def test_handle_request_from_inside_a_running_event_loop(self, full_server):
        """An async caller (the eval harness) may call handle_request
        synchronously while its own event loop is running."""
        import asyncio

        async def main():
            return full_server.handle_request(_request("tools/list"))

        assert len(asyncio.run(main())["result"]["tools"]) > 100

    def test_initialize_sets_version_for_later_calls(self, server):
        server.register_tool(
            name="ok",
            description="ok",
            input_schema={"type": "object", "additionalProperties": True},
            handler=lambda **_: {"a": 1},
        )
        server.handle_request(_init("2024-11-05"))
        resp = server.handle_request(_request("tools/call", {"name": "ok", "arguments": {}}))
        assert json.loads(resp["result"]["content"][0]["text"]) == {"a": 1}
