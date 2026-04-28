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

    def test_handler_exception_returns_iserror_content(self, server):
        # MCP spec: tool-handler exceptions belong in result content with
        # isError: true, not as JSON-RPC errors. Verify the rich exception
        # message survives instead of being flattened to a generic string.
        def boom(**_kwargs):
            raise ValueError("specific reason with locator [Tablix2/Cell3]")

        server.register_tool(
            name="explode",
            description="raises",
            input_schema={"type": "object", "additionalProperties": True},
            handler=boom,
        )
        resp = server.handle_request(
            _request("tools/call", {"name": "explode", "arguments": {}})
        )
        assert "error" not in resp
        assert resp["result"]["isError"] is True
        text = resp["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["error_type"] == "ValueError"
        assert "specific reason" in payload["message"]
        assert "Tablix2/Cell3" in payload["message"]

    def test_handler_lookup_error_preserves_type(self, server):
        # ElementNotFoundError (a LookupError) should carry through with its
        # type name so callers can branch on it.
        from pbirb_mcp.core.ids import ElementNotFoundError

        def missing(**_kwargs):
            raise ElementNotFoundError("no Textbox named 'DoesNotExist'")

        server.register_tool(
            name="lookup_fail",
            description="raises",
            input_schema={"type": "object", "additionalProperties": True},
            handler=missing,
        )
        resp = server.handle_request(
            _request("tools/call", {"name": "lookup_fail", "arguments": {}})
        )
        assert resp["result"]["isError"] is True
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["error_type"] == "ElementNotFoundError"
        assert "DoesNotExist" in payload["message"]

    def test_handler_success_has_no_iserror(self, server):
        def ok(**_kwargs):
            return {"hello": "world"}

        server.register_tool(
            name="ok",
            description="ok",
            input_schema={"type": "object", "additionalProperties": True},
            handler=ok,
        )
        resp = server.handle_request(_request("tools/call", {"name": "ok", "arguments": {}}))
        assert "error" not in resp
        # Per MCP, successful results don't carry isError; absence implies false.
        assert resp["result"].get("isError") in (None, False)
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload == {"hello": "world"}


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
