"""MCP server core: the tool registry and the tool dispatcher.

The wire protocol (version negotiation, JSON-RPC framing, stdio) is the
official MCP SDK, wired up in :mod:`pbirb_mcp.protocol` (issue #53).

Tools are registered via :meth:`MCPServer.register_tool`. Each registered tool
declares an input JSON Schema and a handler callable. The bootstrap server
exposes no tools — feature commits add them.

Optional :envvar:`PBIRB_MCP_AUTO_VERIFY` (Phase 7 commit 34): when set to a
truthy value (``1``, ``true``, ``yes``, ``on``, case-insensitive) the server
runs :func:`pbirb_mcp.ops.validate.verify_report` after every successful
call of a tool registered with ``mutating=True`` and merges the result into
the response: ``{result, verify}``. The verified file is the tool's
``path``, else its ``dst_path`` (duplicate_report), else the
``restored_to`` it returned (restore_from_backup). Default off — v0.2
callers see no shape change.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

SERVER_NAME = "pbirb-mcp"
SERVER_VERSION = "0.4.1"


def _auto_verify_enabled() -> bool:
    val = os.environ.get("PBIRB_MCP_AUTO_VERIFY", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _verify_target(arguments: dict, result: Any) -> Optional[str]:
    """The file a mutating call wrote, for auto-verify."""
    if isinstance(result, dict) and isinstance(result.get("restored_to"), str):
        return result["restored_to"]
    for key in ("dst_path", "path"):
        if isinstance(arguments.get(key), str):
            return arguments[key]
    return None


ToolHandler = Callable[..., Any]

# Tools that may never run as an op inside apply_edits / dry_run_edit:
# transaction control (would commit/cancel mid-batch), the batch tools
# themselves (nesting), and tools that write to disk outside the batch's
# tree (backup/restore/duplicate/create) or don't take a `path`.
NON_BATCHABLE_TOOLS = frozenset(
    {
        "start_editing_transaction",
        "commit_editing_transaction",
        "cancel_editing_transaction",
        "apply_edits",
        "dry_run_edit",
        "create_report",
        "duplicate_report",
        "restore_from_backup",
        "backup_report",
    }
)


# Tools that manage transactions themselves: never routed through one,
# and never given an injected `transaction_id` parameter.
TRANSACTION_CONTROL_TOOLS = frozenset(
    {"start_editing_transaction", "commit_editing_transaction", "cancel_editing_transaction"}
)

_TRANSACTION_ID_PARAM = {
    "type": "string",
    "description": (
        "Optional. Run this call inside the editing transaction with this id "
        "(from start_editing_transaction): reads and edits use its in-memory "
        "tree; nothing reaches disk until commit_editing_transaction."
    ),
}


@dataclass
class Tool:
    name: str
    description: str
    # The schema arguments are validated against (may use nullable type
    # lists / oneOf).
    input_schema: dict
    handler: ToolHandler
    # May this tool run as an op inside apply_edits / dry_run_edit?
    batchable: bool = False
    # Does this tool write an RDL file? (drives PBIRB_MCP_AUTO_VERIFY)
    mutating: bool = False
    # May a call carry `transaction_id` to run inside a transaction?
    routable: bool = False
    # The client-safe schema published in tools/list.
    published_schema: Optional[dict] = None


class MCPServer:
    def __init__(self) -> None:
        from pbirb_mcp.protocol import AdapterState

        self._tools: dict[str, Tool] = {}
        self._sdk_server = None
        self._adapter_state = AdapterState()

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: ToolHandler,
        *,
        batchable: Optional[bool] = None,
        mutating: bool = False,
    ) -> None:
        """Register a tool.

        ``batchable`` — may the tool run as an op in ``apply_edits`` /
        ``dry_run_edit``. Defaults to: the schema declares ``path`` and the
        tool isn't in :data:`NON_BATCHABLE_TOOLS`.

        ``mutating`` — the tool writes an RDL file; with
        ``PBIRB_MCP_AUTO_VERIFY`` on, the written file is verified after
        every successful call.
        """
        from pbirb_mcp.core.argcheck import client_schema

        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        schema = dict(input_schema or {"type": "object", "properties": {}})
        props = dict(schema.get("properties", {}) or {})
        if batchable is None:
            batchable = "path" in props and name not in NON_BATCHABLE_TOOLS
        # Every tool that works on a report file can run inside a
        # transaction; declare `transaction_id` so schema-conformant clients
        # may send it (issue #19).
        routable = name not in TRANSACTION_CONTROL_TOOLS and (
            "path" in props or "src_path" in props
        )
        if routable and "transaction_id" not in props:
            props["transaction_id"] = _TRANSACTION_ID_PARAM
            schema["properties"] = props
        self._tools[name] = Tool(
            name,
            description,
            schema,
            handler,
            batchable=batchable,
            mutating=mutating,
            routable=routable,
            published_schema=client_schema(schema),
        )

    def is_batchable(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.batchable

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    # ---- protocol -----------------------------------------------------------

    @property
    def sdk_server(self):
        """The MCP SDK low-level server that serves this registry (issue #53).

        Built on first use, after ``register_all_tools`` has populated the
        registry; ``tools/list`` reads the registry live.
        """
        if self._sdk_server is None:
            from pbirb_mcp.protocol import build_sdk_server

            self._sdk_server = build_sdk_server(self, name=SERVER_NAME, version=SERVER_VERSION)
        return self._sdk_server

    def handle_request(self, request: dict) -> Optional[dict]:
        """Serve one JSON-RPC request in-process; ``None`` for a notification.

        A thin synchronous adapter over the SDK server (see
        :mod:`pbirb_mcp.protocol`): same methods, version negotiation, error
        codes and result shapes as the stdio transport. A request carrying
        the 2026-07-28 ``_meta`` envelope is served at that revision; any
        other at the version the last ``initialize`` negotiated (default
        2025-11-25).
        """
        from pbirb_mcp.protocol import handle_message

        return handle_message(self.sdk_server, self._adapter_state, request)

    # ---- handlers -----------------------------------------------------------

    def _tools_list(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.published_schema or tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    def _tools_call(self, params: dict) -> dict:
        """Run one tool call; returns the ``CallToolResult`` dict.

        The protocol layer (:mod:`pbirb_mcp.protocol`) has already checked
        that ``params["name"]`` is a registered tool and that
        ``params["arguments"]`` is an object (or absent).
        """
        name = params["name"]
        arguments = params.get("arguments") or {}
        tool = self._tools[name]

        # v0.4 commit 9 — transaction routing. When `transaction_id` is in
        # arguments AND the tool is an EDIT tool (i.e. its schema declares
        # `path` but NOT `transaction_id`), swap arguments["path"] with
        # the registered abspath and strip the id from kwargs. The
        # transaction-control tools (commit/cancel_editing_transaction)
        # declare `transaction_id` as their first-class argument; the
        # dispatcher leaves those alone and lets the handler look up the
        # transaction itself.
        # On miss → structured isError that does NOT echo the opaque id
        # back (leakage guard). Orphans are swept lazily before every
        # transaction-aware lookup.
        arguments = dict(arguments)

        in_transaction = False
        tx_id = None
        schema_props = (tool.input_schema or {}).get("properties", {})
        if "transaction_id" in arguments and tool.routable:
            from pbirb_mcp.core import transactions as _tx

            tx_id = arguments.pop("transaction_id")
            # lookup_by_id sweeps expired transactions first.
            tx = _tx.lookup_by_id(tx_id) if isinstance(tx_id, str) else None
            if tx is None:
                payload = {
                    "error_type": "TransactionError",
                    "message": (
                        "transaction is unknown, expired, or already "
                        "committed; call start_editing_transaction to "
                        "open a fresh one"
                    ),
                }
                return {
                    "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
                    "isError": True,
                }
            if "path" in schema_props:
                arguments["path"] = tx.abspath
            in_transaction = True

        # Validate arguments against the tool's schema before dispatch
        # (issue #19): wrong types used to reach the ops ("false" is
        # truthy) or surface as Python TypeError text. Runs after routing
        # so a transaction-routed call may leave `path` to the dispatcher.
        from pbirb_mcp.core.argcheck import InvalidArguments, validate_arguments

        try:
            validate_arguments(tool.input_schema, arguments)
        except InvalidArguments as exc:
            payload = {"error_type": "InvalidArguments", "message": str(exc), **exc.payload}
            return {
                "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
                "isError": True,
            }

        # Per MCP spec, tool-handler exceptions are surfaced in the result
        # content with `isError: true`, NOT as JSON-RPC errors. JSON-RPC errors
        # are reserved for protocol issues (bad envelope, unknown method, bad
        # params). Returning handler errors as JSON-RPC -32603 caused MCP
        # clients to render them as opaque "Tool execution failed" strings,
        # hiding the rich messages tools already raise (locators in
        # remove_parameter, missing-column hints in add_subtotal_row, etc.).
        try:
            if in_transaction:
                from pbirb_mcp.core import transactions as _tx

                # Snapshot first: an op that mutates the shared tree and
                # then raises must not leave its partial mutation for the
                # next commit to save.
                snapshot = tx.doc.snapshot()
                try:
                    # RDLDocument.open hands out the transaction's live
                    # tree only to calls running under its id.
                    with _tx.activate(tx_id):
                        result = tool.handler(**arguments)
                except Exception:
                    tx.doc.restore(snapshot)
                    raise
            else:
                from pbirb_mcp.core import transactions as _tx

                # Not routed: run outside any transaction, even when this
                # call is nested inside a routed one (batch tools).
                with _tx.activate(None):
                    result = tool.handler(**arguments)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s raised", name)
            payload = {
                "error_type": type(exc).__name__,
                "message": str(exc) or repr(exc),
            }
            # Exceptions may carry structured fields (e.g. OpNotAllowed's
            # `tool`); merge them into the payload.
            extra = getattr(exc, "payload", None)
            if isinstance(extra, dict):
                payload.update(extra)
            return {
                "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
                "isError": True,
            }

        # Auto-verify (Phase 7 commit 34): when PBIRB_MCP_AUTO_VERIFY is on
        # and the tool is registered mutating=True, run verify_report
        # against the file it wrote and merge into the response. Off by
        # default — v0.2 callers see no shape change.
        # v0.4: skip when the request was inside a transaction —
        # intermediate trees aren't on disk, so verifying would inspect
        # stale bytes. commit_editing_transaction runs its own verify
        # against the post-commit on-disk file.
        target = _verify_target(arguments, result) if tool.mutating else None
        if not in_transaction and _auto_verify_enabled() and target is not None:
            from pbirb_mcp.ops.validate import verify_report

            try:
                verify = verify_report(target)
            except Exception as exc:  # noqa: BLE001
                # Don't fail the mutating call because verify barfed.
                logger.warning("auto-verify failed for %s: %s", name, exc)
                verify = {
                    "valid": None,
                    "issues": [],
                    "xsd_used": False,
                    "error": {"error_type": type(exc).__name__, "message": str(exc)},
                }
            wrapped = {"result": result, "verify": verify}
            return {"content": [{"type": "text", "text": json.dumps(wrapped, default=str)}]}

        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}

    # ---- transport ----------------------------------------------------------

    def run_stdio(self) -> None:
        """Serve MCP over stdin/stdout (all supported protocol revisions)."""
        from pbirb_mcp.protocol import serve_stdio

        serve_stdio(self.sdk_server)
