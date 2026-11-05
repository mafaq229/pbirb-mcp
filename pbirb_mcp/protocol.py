"""MCP wire protocol, hosted on the official MCP Python SDK (issue #53).

:class:`pbirb_mcp.server.MCPServer` owns the tool registry and the tool
dispatcher (argument validation, transaction routing, auto-verify). This
module puts that registry behind the SDK's low-level
:class:`mcp.server.lowlevel.Server`, which owns everything protocol-shaped:

* version negotiation — the ``initialize`` handshake for 2024-11-05 …
  2025-11-25 clients, the stateless per-request ``_meta`` envelope and
  ``server/discover`` for 2026-07-28 clients, and
  ``UnsupportedProtocolVersionError`` (-32022) for anything else;
* ``ping`` (handshake era only — 2026-07-28 removed it);
* per-version result shaping: ``resultType`` and the ``serverInfo`` stamp on
  2026-07-28 results, ``ttlMs``/``cacheScope`` on ``tools/list``, and fields a
  revision doesn't know (e.g. ``structuredContent`` before 2025-06-18)
  sieved out;
* JSON-RPC framing over stdio, including "a notification never gets a
  response".

Two entry points share the one SDK server:

* :func:`serve_stdio` — the real transport (``pbirb-mcp`` console script).
* :func:`handle_message` — a synchronous, in-process adapter for
  ``MCPServer.handle_request``: one JSON-RPC request dict in, one response
  dict (or ``None`` for a notification) out. Tests, ``apply_edits`` /
  ``dry_run_edit`` op dispatch and the eval harness use it. Each request is
  served by the SDK's request kernel exactly like a wire request; the only
  difference is that every request is treated as arriving on a ready
  connection (no init gate), so callers needn't replay a handshake.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

import anyio
import mcp_types as types
from mcp.server.caching import CacheHint
from mcp.server.connection import Connection
from mcp.server.lowlevel import Server
from mcp.server.runner import serve_one
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import InboundLadderRejection, classify_inbound_request
from mcp.shared.jsonrpc_dispatcher import handler_exception_to_error_data
from mcp.shared.transport_context import TransportContext
from mcp_types import CLIENT_CAPABILITIES_META_KEY, PROTOCOL_VERSION_META_KEY
from mcp_types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    LATEST_HANDSHAKE_VERSION,
    MODERN_PROTOCOL_VERSIONS,
)

if TYPE_CHECKING:
    from pbirb_mcp.server import MCPServer

logger = logging.getLogger(__name__)

__all__ = [
    "HANDSHAKE_PROTOCOL_VERSIONS",
    "MODERN_PROTOCOL_VERSIONS",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "build_sdk_server",
    "handle_message",
    "serve_stdio",
]

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    *HANDSHAKE_PROTOCOL_VERSIONS,
    *MODERN_PROTOCOL_VERSIONS,
)

# JSON-RPC 2.0 codes the adapter emits itself (the SDK emits the rest).
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# The tool list is fixed for the life of the process, so a 2026-07-28 client
# may cache it; it's the same for every caller ("public").
TOOLS_LIST_CACHE_HINT = CacheHint(ttl_ms=3_600_000, scope="public")

# Tool execution is serialized process-wide: handlers share the transaction
# registry and edit files on disk, and the hand-rolled loop this replaced ran
# one request at a time. A call dispatched from inside a running tool call
# (apply_edits / dry_run_edit ops) already holds the lock — the flag below
# lets it through instead of deadlocking.
_TOOL_LOCK = threading.Lock()
_IN_TOOL_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pbirb_mcp_in_tool_call", default=False
)


def build_sdk_server(owner: MCPServer, *, name: str, version: str) -> Server[Any]:
    """The SDK low-level server serving ``owner``'s tool registry."""

    async def on_list_tools(
        ctx: Any, params: Optional[types.PaginatedRequestParams]
    ) -> dict[str, Any]:
        # Registration order is fixed by tools.py, so the order is
        # deterministic; there is a single page.
        return {"resultType": "complete", "tools": owner._tools_list()}

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> Any:
        tool_name = params.name
        if not owner.has_tool(tool_name):
            # MCP spec: an unknown tool is Invalid params, not Method not found.
            raise MCPError(code=INVALID_PARAMS, message=f"unknown tool: {tool_name}")
        arguments = params.arguments
        if (
            ctx.protocol_version in MODERN_PROTOCOL_VERSIONS
            and _client_can_elicit(ctx)
            and owner.is_enabled(tool_name)  # else refused at dispatch (#56)
            and owner.requires_confirmation(tool_name, arguments)
        ):
            # Issue #57, 2026-07-28: ask the user through the client
            # (multi-round-trip `input_required`) instead of failing.
            confirmed, outcome = _confirmation_round(tool_name, dict(arguments or {}), params)
            if not confirmed:
                return outcome
            arguments = outcome
        call = {"name": tool_name, "arguments": arguments}
        try:
            result = await anyio.to_thread.run_sync(_run_tool_serialized, owner, call)
        except Exception as exc:  # noqa: BLE001
            # The dispatcher turns handler errors into isError results, so
            # this is a server bug. Map it explicitly: the SDK's stdio
            # dispatcher would otherwise answer with code 0.
            logger.exception("tools/call %s failed outside the tool handler", tool_name)
            raise MCPError(code=INTERNAL_ERROR, message=str(exc) or type(exc).__name__) from exc
        return {"resultType": "complete", **result}

    return Server(
        name,
        version=version,
        cache_hints={"tools/list": TOOLS_LIST_CACHE_HINT},
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


CONFIRM_INPUT_KEY = "confirm"


def _client_can_elicit(ctx: Any) -> bool:
    """Did the 2026-07-28 request envelope declare the elicitation capability?"""
    params = ctx.params if isinstance(ctx.params, Mapping) else {}
    meta = params.get("_meta")
    caps = meta.get(CLIENT_CAPABILITIES_META_KEY) if isinstance(meta, Mapping) else None
    return isinstance(caps, Mapping) and isinstance(caps.get("elicitation"), Mapping)


def _confirmation_round(
    tool_name: str, arguments: dict[str, Any], params: types.CallToolRequestParams
) -> tuple[bool, Any]:
    """The confirmation step of a destructive call on 2026-07-28.

    ``(True, arguments + confirm=True)`` when the client's retry carries
    the user's acceptance for this exact call; otherwise ``(False,
    result)`` with the ``input_required`` result to send (first round) or
    an ``isError`` result (the user declined).
    """
    from pbirb_mcp.core import security

    state = security.request_fingerprint(tool_name, arguments)
    answer = (params.input_responses or {}).get(CONFIRM_INPUT_KEY)
    if answer is not None and params.request_state == state:
        accepted = (
            isinstance(answer, types.ElicitResult)
            and answer.action == "accept"
            and (answer.content or {}).get("confirm") is True
        )
        if accepted:
            return True, {**arguments, "confirm": True}
        payload = {
            "error_type": "ConfirmationDeclined",
            "message": f"The user did not confirm {tool_name}; nothing was changed.",
            "tool": tool_name,
        }
        return False, {
            "resultType": "complete",
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": True,
        }
    target = arguments.get("path") or arguments.get("target_path") or "the report"
    request = types.ElicitRequest(
        params=types.ElicitRequestFormParams(
            message=f"Allow the destructive edit {tool_name} on {target}?",
            requested_schema={
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "title": f"Run {tool_name}",
                        "description": "This removes or overwrites report content.",
                    }
                },
                "required": ["confirm"],
            },
        )
    )
    return False, types.InputRequiredResult(
        input_requests={CONFIRM_INPUT_KEY: request}, request_state=state
    )


def _run_tool_serialized(owner: MCPServer, call: dict[str, Any]) -> dict[str, Any]:
    if _IN_TOOL_CALL.get():
        return owner._tools_call(call)
    with _TOOL_LOCK:
        token = _IN_TOOL_CALL.set(True)
        try:
            return owner._tools_call(call)
        finally:
            _IN_TOOL_CALL.reset(token)


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------


def serve_stdio(sdk_server: Server[Any]) -> None:
    """Serve MCP over this process's stdin/stdout until stdin closes.

    The SDK decides the protocol era from the client's first request: an
    ``initialize`` opens a handshake-era connection, a request carrying the
    2026-07-28 ``_meta`` envelope opens a stateless one.
    """
    from mcp.server.stdio import stdio_server

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await sdk_server.run(
                read_stream, write_stream, sdk_server.create_initialization_options()
            )

    anyio.run(_main)


# ---------------------------------------------------------------------------
# in-process adapter
# ---------------------------------------------------------------------------


@dataclass
class _InProcessDispatchContext:
    """Minimal SDK ``DispatchContext`` for a request served in-process.

    There is no client on the other end to receive server-initiated
    requests or notifications, so the back-channel refuses / drops them.
    """

    request_id: Any
    transport: TransportContext = field(
        default_factory=lambda: TransportContext(kind="in-process", can_send_request=False)
    )
    message_metadata: Any = None
    cancel_requested: anyio.Event = field(default_factory=anyio.Event)

    @property
    def can_send_request(self) -> bool:
        return False

    async def send_raw_request(self, method: str, params: Any, opts: Any = None) -> dict:
        from mcp.shared.exceptions import NoBackChannelError

        raise NoBackChannelError(method)

    async def notify(self, method: str, params: Any, opts: Any = None) -> None:
        return None

    async def progress(self, progress: float, total: Any = None, message: Any = None) -> None:
        return None


@dataclass
class AdapterState:
    """Per-``MCPServer`` state the in-process adapter keeps between calls."""

    # The version a handshake-era caller negotiated with `initialize`;
    # requests without the 2026-07-28 envelope are served at it.
    legacy_version: str = LATEST_HANDSHAKE_VERSION


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def _has_modern_envelope(params: Any) -> bool:
    if not isinstance(params, Mapping):
        return False
    meta = params.get("_meta")
    return isinstance(meta, Mapping) and PROTOCOL_VERSION_META_KEY in meta


def handle_message(
    sdk_server: Server[Any], state: AdapterState, request: Any
) -> Optional[dict[str, Any]]:
    """Serve one JSON-RPC message in-process; ``None`` for a notification."""
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "invalid JSON-RPC envelope")
    method = request.get("method")
    if not isinstance(method, str):
        return _error(request.get("id"), INVALID_REQUEST, "missing method")
    if "id" not in request:
        # JSON-RPC 2.0: a notification never gets a response. Client
        # notifications (initialized, cancelled, …) carry nothing this
        # stateless adapter acts on.
        return None
    request_id = request["id"]
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        return _error(None, INVALID_REQUEST, "request id must be a string or integer")
    params = request.get("params")
    if params is not None and not isinstance(params, dict):
        return _error(request_id, INVALID_REQUEST, "params must be an object")

    if method != "initialize" and _has_modern_envelope(params):
        route = classify_inbound_request({"method": method, "params": params})
        if isinstance(route, InboundLadderRejection):
            return _error(request_id, route.code, route.message, route.data)
        connection = Connection.from_envelope(
            route.protocol_version, route.client_info, route.client_capabilities
        )
    else:
        connection = Connection.from_envelope(state.legacy_version, None, None)

    async def _serve() -> dict[str, Any]:
        return await serve_one(
            sdk_server,
            _InProcessDispatchContext(request_id),
            method,
            params,
            connection=connection,
            lifespan_state={},
        )

    try:
        result = _run_sync(_serve)
    except Exception as exc:  # noqa: BLE001 — mapped to a JSON-RPC error below
        error = handler_exception_to_error_data(exc)
        if error is None:
            logger.exception("internal error handling %s", method)
            return _error(request_id, INTERNAL_ERROR, str(exc) or type(exc).__name__)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error.model_dump(by_alias=True, mode="json", exclude_none=True),
        }
    if method == "initialize" and result.get("protocolVersion") in HANDSHAKE_PROTOCOL_VERSIONS:
        state.legacy_version = result["protocolVersion"]
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = threading.Lock()


def _run_sync(fn: Callable[[], Any]) -> Any:
    """Run the async callable ``fn`` to completion from synchronous code.

    Normally that's a fresh event loop in this thread. When this thread is
    already running a loop (an async caller — e.g. the eval harness — calling
    ``handle_request`` synchronously), the loop runs in a helper thread under
    a copy of the caller's context, so context variables such as the active
    editing transaction carry over.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return anyio.run(fn)
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(thread_name_prefix="pbirb-mcp-inproc")
        executor = _EXECUTOR
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, anyio.run, fn).result()
