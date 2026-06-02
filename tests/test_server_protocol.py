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
        resp = server.handle_request(_request("tools/call", {"name": "explode", "arguments": {}}))
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


class TestTransactionIdDispatch:
    """v0.4 commit 9 — dispatcher honours `transaction_id` in args.

    When a tool call's arguments contain ``transaction_id``:
      1. The dispatcher pops it (handlers never see it — their
         signature stays the same).
      2. Looks up the registered transaction; on miss → isError with
         error_type="TransactionError".
      3. Substitutes ``arguments["path"]`` with the registered abspath.
      4. Sweeps orphans before the lookup so expired entries are
         cleaned up lazily.
      5. Skips the auto-verify branch — intermediate trees aren't on
         disk, so verifying would inspect stale bytes.
    """

    def _make_path_tool(self, server, captured):
        def handler(path=None, **kwargs):
            captured["path"] = path
            captured["kwargs"] = kwargs
            return {"ok": True, "received_path": path}

        server.register_tool(
            name="set_thing",  # `set_` prefix → mutating per _MUTATING_PREFIXES
            description="captures what it got",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": True,
            },
            handler=handler,
        )

    def test_unknown_transaction_id_returns_iserror(self, server):
        self._make_path_tool(server, {})
        resp = server.handle_request(
            _request(
                "tools/call",
                {
                    "name": "set_thing",
                    "arguments": {"transaction_id": "not-a-real-id"},
                },
            )
        )
        assert "error" not in resp
        assert resp["result"]["isError"] is True
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["error_type"] == "TransactionError"
        # Don't echo the id back — that's the leakage guard.
        assert "not-a-real-id" not in payload["message"]

    def test_transaction_id_substitutes_path_and_strips_kwarg(self, server, tmp_path):
        import shutil

        from pbirb_mcp.core import transactions
        from pbirb_mcp.core.document import RDLDocument

        transactions._reset_for_tests()
        try:
            fixture = (
                __import__("pathlib").Path(__file__).parent
                / "fixtures"
                / "pbi_paginated_minimal.rdl"
            )
            rdl = tmp_path / "report.rdl"
            shutil.copy(fixture, rdl)
            doc = RDLDocument.open(rdl)
            tx_id = transactions.register(doc)

            captured = {}
            self._make_path_tool(server, captured)
            resp = server.handle_request(
                _request(
                    "tools/call",
                    {
                        "name": "set_thing",
                        "arguments": {"transaction_id": tx_id, "path": "/should-be-ignored"},
                    },
                )
            )
            assert "error" not in resp
            assert resp["result"].get("isError") is not True, resp
            # Path was substituted from the registry, not from the
            # caller's "/should-be-ignored" placeholder.
            assert captured["path"] == str(rdl.resolve())
            # transaction_id was stripped — handler doesn't see it.
            assert "transaction_id" not in captured["kwargs"]
        finally:
            transactions._reset_for_tests()

    def test_expired_transaction_id_returns_iserror(self, server, tmp_path):
        import shutil

        from pbirb_mcp.core import transactions
        from pbirb_mcp.core.document import RDLDocument

        transactions._reset_for_tests()
        try:
            fixture = (
                __import__("pathlib").Path(__file__).parent
                / "fixtures"
                / "pbi_paginated_minimal.rdl"
            )
            rdl = tmp_path / "report.rdl"
            shutil.copy(fixture, rdl)
            doc = RDLDocument.open(rdl)
            # Register at an arbitrary anchor time, then expire manually.
            tx_id = transactions.register(doc, now=0.0)
            # Force the entry's expires_at into the past so the
            # dispatcher's lazy sweep_orphans on the next call clears it.
            tx = transactions.lookup_by_id(tx_id)
            tx.expires_at = -1.0

            self._make_path_tool(server, {})
            resp = server.handle_request(
                _request(
                    "tools/call",
                    {"name": "set_thing", "arguments": {"transaction_id": tx_id}},
                )
            )
            assert resp["result"]["isError"] is True
            payload = json.loads(resp["result"]["content"][0]["text"])
            assert payload["error_type"] == "TransactionError"
        finally:
            transactions._reset_for_tests()

    def test_no_transaction_id_path_unchanged(self, server):
        captured = {}
        self._make_path_tool(server, captured)
        resp = server.handle_request(
            _request(
                "tools/call",
                {"name": "set_thing", "arguments": {"path": "/explicit/path.rdl"}},
            )
        )
        assert "error" not in resp
        assert resp["result"].get("isError") is not True, resp
        # No tx → caller-supplied path used verbatim.
        assert captured["path"] == "/explicit/path.rdl"

    def test_auto_verify_skipped_when_in_transaction(self, server, tmp_path, monkeypatch):
        """Inside a transaction, intermediate trees aren't on disk;
        running verify_report against the disk file would inspect
        stale bytes. The dispatcher must skip the auto-verify branch."""
        import shutil

        from pbirb_mcp.core import transactions
        from pbirb_mcp.core.document import RDLDocument

        transactions._reset_for_tests()
        verify_calls = {"n": 0}
        try:
            fixture = (
                __import__("pathlib").Path(__file__).parent
                / "fixtures"
                / "pbi_paginated_minimal.rdl"
            )
            rdl = tmp_path / "report.rdl"
            shutil.copy(fixture, rdl)
            doc = RDLDocument.open(rdl)
            tx_id = transactions.register(doc)

            # Force auto-verify ON.
            monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")

            def fake_verify(*args, **kwargs):
                verify_calls["n"] += 1
                return {"valid": True, "issues": [], "xsd_used": True}

            monkeypatch.setattr("pbirb_mcp.ops.validate.verify_report", fake_verify)

            self._make_path_tool(server, {})
            resp = server.handle_request(
                _request(
                    "tools/call",
                    {"name": "set_thing", "arguments": {"transaction_id": tx_id}},
                )
            )
            assert "error" not in resp
            # No auto-verify wrapping when transaction_id was set —
            # response has the raw shape, not {result, verify}.
            payload = json.loads(resp["result"]["content"][0]["text"])
            assert "result" not in payload  # raw handler return, not wrapped
            assert "verify" not in payload
            assert verify_calls["n"] == 0
        finally:
            transactions._reset_for_tests()

    def test_auto_verify_still_runs_outside_transaction(self, server, monkeypatch):
        """Regression canary for the v0.3.0 auto-verify behaviour:
        when there's no transaction_id, the auto-verify branch fires
        exactly as before."""
        verify_calls = {"n": 0}

        def fake_verify(path, *args, **kwargs):
            verify_calls["n"] += 1
            return {"valid": True, "issues": [], "xsd_used": True}

        monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")
        monkeypatch.setattr("pbirb_mcp.ops.validate.verify_report", fake_verify)

        self._make_path_tool(server, {})
        resp = server.handle_request(
            _request(
                "tools/call",
                {"name": "set_thing", "arguments": {"path": "/tmp/anything.rdl"}},
            )
        )
        assert "error" not in resp
        payload = json.loads(resp["result"]["content"][0]["text"])
        # Auto-verify wrapped the response.
        assert "result" in payload
        assert "verify" in payload
        assert verify_calls["n"] == 1


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
