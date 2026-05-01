"""MCP JSON-RPC server core.

Tools are registered via :meth:`MCPServer.register_tool`. Each registered tool
declares an input JSON Schema and a handler callable. The bootstrap server
exposes no tools — feature commits add them.

Optional :envvar:`PBIRB_MCP_AUTO_VERIFY` (Phase 7 commit 34): when set to a
truthy value (``1``, ``true``, ``yes``, ``on``, case-insensitive) the server
runs :func:`pbirb_mcp.ops.validate.verify_report` after every successful
mutating-tool call and merges the result into the response:
``{result, verify}``. Default off — v0.2 callers see no shape change.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "pbirb-mcp"
SERVER_VERSION = "0.3.0"

# Tool-name prefixes treated as mutating for auto-verify routing. All RDL-
# state-changing tools in the codebase start with one of these. Read-only
# tools (get_*, list_*, describe_*, find_*, validate_*, lint_*, verify_*,
# dry_run_edit, backup_report) are explicitly out — they don't write.
_MUTATING_PREFIXES = (
    "set_",
    "add_",
    "remove_",
    "update_",
    "rename_",
    "reorder_",
    "insert_",
    "style_",
    "duplicate_",
    "sync_",
)


def _auto_verify_enabled() -> bool:
    val = os.environ.get("PBIRB_MCP_AUTO_VERIFY", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _is_mutating_tool(name: str) -> bool:
    return any(name.startswith(p) for p in _MUTATING_PREFIXES)

# JSON-RPC 2.0 standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


ToolHandler = Callable[..., Any]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: ToolHandler


class MCPServer:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: ToolHandler,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = Tool(name, description, input_schema, handler)

    # ---- protocol -----------------------------------------------------------

    def handle_request(self, request: dict) -> Optional[dict]:
        """Route a single JSON-RPC request. Returns None for notifications."""
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._error(None, INVALID_REQUEST, "invalid JSON-RPC envelope")

        method = request.get("method")
        if not isinstance(method, str):
            return self._error(request.get("id"), INVALID_REQUEST, "missing method")

        # Notifications: no `id`, no response.
        is_notification = "id" not in request
        request_id = request.get("id")
        params = request.get("params") or {}

        try:
            if method == "initialize":
                return self._ok(request_id, self._initialize_result())
            if method == "tools/list":
                return self._ok(request_id, {"tools": self._tools_list()})
            if method == "tools/call":
                return self._ok(request_id, self._tools_call(params))
            if method.startswith("notifications/"):
                # Spec: server may receive these and must not reply.
                return None
            return self._error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")
        except _MCPError as exc:
            return self._error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("internal error handling %s", method)
            if is_notification:
                return None
            return self._error(request_id, INTERNAL_ERROR, str(exc))

    # ---- handlers -----------------------------------------------------------

    def _initialize_result(self) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _tools_list(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    def _tools_call(self, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            raise _MCPError(INVALID_PARAMS, "tools/call requires a string `name`")
        tool = self._tools.get(name)
        if tool is None:
            raise _MCPError(METHOD_NOT_FOUND, f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise _MCPError(INVALID_PARAMS, "tools/call `arguments` must be an object")

        # Per MCP spec, tool-handler exceptions are surfaced in the result
        # content with `isError: true`, NOT as JSON-RPC errors. JSON-RPC errors
        # are reserved for protocol issues (bad envelope, unknown method, bad
        # params). Returning handler errors as JSON-RPC -32603 caused MCP
        # clients to render them as opaque "Tool execution failed" strings,
        # hiding the rich messages tools already raise (locators in
        # remove_parameter, missing-column hints in add_subtotal_row, etc.).
        try:
            result = tool.handler(**arguments)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s raised", name)
            payload = {
                "error_type": type(exc).__name__,
                "message": str(exc) or repr(exc),
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
                "isError": True,
            }

        # Auto-verify (Phase 7 commit 34): when PBIRB_MCP_AUTO_VERIFY is on
        # and the tool is mutating + has a `path` arg, run verify_report
        # against the post-mutation file and merge into the response. Off
        # by default — v0.2 callers see no shape change.
        if (
            _auto_verify_enabled()
            and _is_mutating_tool(name)
            and isinstance(arguments.get("path"), str)
        ):
            from pbirb_mcp.ops.validate import verify_report

            try:
                verify = verify_report(arguments["path"])
            except Exception as exc:  # noqa: BLE001
                # Don't fail the mutating call because verify barfed.
                logger.warning("auto-verify failed for %s: %s", name, exc)
                verify = {"valid": None, "issues": [], "xsd_used": False,
                          "error": {"error_type": type(exc).__name__,
                                    "message": str(exc)}}
            wrapped = {"result": result, "verify": verify}
            return {"content": [{"type": "text", "text": json.dumps(wrapped, default=str)}]}

        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}

    # ---- transport ----------------------------------------------------------

    def run_stdio(self) -> None:
        """Read newline-delimited JSON-RPC requests from stdin, write to stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                resp = self._error(None, PARSE_ERROR, f"invalid JSON: {exc}")
                self._write(resp)
                continue

            response = self.handle_request(request)
            if response is not None:
                self._write(response)

    @staticmethod
    def _write(response: dict) -> None:
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    # ---- response helpers ---------------------------------------------------

    @staticmethod
    def _ok(request_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(
        request_id: Any,
        code: int,
        message: str,
        data: Any = None,
    ) -> dict:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": err}


class _MCPError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
