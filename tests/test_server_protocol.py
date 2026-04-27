"""JSON-RPC protocol tests for the bare MCP server (no tools registered yet)."""

import json

import pytest

from pbirb_mcp.server import MCPServer


@pytest.fixture
def server():
    return MCPServer()


def _request(method, params=None, request_id=1):
    req = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        req["params"] = params
    return req


class TestInitialize:
    def test_initialize_returns_capabilities(self, server):
        resp = server.handle_request(_request("initialize", {"protocolVersion": "2024-11-05"}))
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert "result" in resp
        result = resp["result"]
        assert "protocolVersion" in result
        assert "capabilities" in result
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "pbirb-mcp"

    def test_initialize_advertises_tools_capability(self, server):
        resp = server.handle_request(_request("initialize", {}))
        assert "tools" in resp["result"]["capabilities"]


class TestToolsList:
    def test_tools_list_empty_at_bootstrap(self, server):
        resp = server.handle_request(_request("tools/list"))
        assert "result" in resp
        assert resp["result"]["tools"] == []

    def test_tools_list_response_shape(self, server):
        resp = server.handle_request(_request("tools/list", request_id=42))
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 42
        assert "tools" in resp["result"]


class TestToolsCall:
    def test_unknown_tool_returns_jsonrpc_error(self, server):
        resp = server.handle_request(
            _request("tools/call", {"name": "no_such_tool", "arguments": {}})
        )
        assert "error" in resp
        assert resp["error"]["code"] == -32601
        assert "no_such_tool" in resp["error"]["message"]


class TestNotifications:
    def test_initialized_notification_returns_none(self, server):
        # JSON-RPC notifications have no id and expect no response.
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert server.handle_request(notif) is None


class TestUnknownMethod:
    def test_unknown_method_returns_method_not_found(self, server):
        resp = server.handle_request(_request("not_a_method"))
        assert "error" in resp
        assert resp["error"]["code"] == -32601


class TestMalformedRequest:
    def test_request_without_method_returns_invalid_request(self, server):
        resp = server.handle_request({"jsonrpc": "2.0", "id": 1})
        assert "error" in resp
        assert resp["error"]["code"] == -32600


class TestSerialization:
    def test_response_is_json_serialisable(self, server):
        resp = server.handle_request(_request("initialize", {}))
        json.dumps(resp)
