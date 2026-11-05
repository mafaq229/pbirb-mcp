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

import contextvars
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

# Tools that delete report content or overwrite a file (issue #57). The one
# place this is defined: confirmation (`confirm=true`, see
# pbirb_mcp.core.security) keys off it, and tool annotations
# (destructiveHint, #54) should too. Every `remove_*` tool must be listed
# (tests/test_security.py checks).
DESTRUCTIVE_TOOLS = frozenset(
    {
        "remove_body_item",
        "remove_calculated_field",
        "remove_chart_series",
        "remove_column_group",
        "remove_data_source",
        "remove_dataset_field",
        "remove_dataset_filter",
        "remove_embedded_image",
        "remove_footer_item",
        "remove_header_item",
        "remove_parameter",
        "remove_query_parameter",
        "remove_row_group",
        "remove_tablix_column",
        "remove_tablix_filter",
        # Replaces an arbitrary element with caller-supplied XML.
        "raw_xml_replace",
        # Copies a backup over the report.
        "restore_from_backup",
    }
)

# Tools that take `confirm`: the destructive ones, plus apply_edits, which
# needs it when any of its ops is destructive.
_CONFIRMABLE_TOOLS = DESTRUCTIVE_TOOLS | {"apply_edits"}

_CONFIRM_PARAM = {
    "type": "boolean",
    "default": False,
    "description": (
        "Set true to run this destructive edit. Without it the server may refuse "
        "and ask for confirmation (PBIRB_MCP_REQUIRE_CONFIRM; on by default for "
        "the pbirb-mcp server). Confirm with the user first."
    ),
}

# Tools that write report files without being registered mutating=True
# (commit saves the transaction's tree); audited too.
_AUDITED_EXTRA_TOOLS = frozenset({"commit_editing_transaction"})

# Nesting depth of tool calls in this context: apply_edits / dry_run_edit
# dispatch their ops as nested calls (depth >= 1), which skip the
# confirmation gate — the top-level batch call is gated instead.
_CALL_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("pbirb_mcp_call_depth", default=0)

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
    # The JSON Schema of the result, published as `outputSchema`; every
    # successful call returns a matching `structuredContent` (issue #55).
    output_schema: Optional[dict] = None


def _success(result: Any) -> dict:
    """A successful ``CallToolResult``: the JSON text block plus the same
    value as ``structuredContent`` (wrapped as ``{"result": ...}`` when it
    isn't an object)."""
    from pbirb_mcp.core.output_schemas import structured_content

    text = json.dumps(result, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured_content(json.loads(text)),
    }


def _failure(payload: dict) -> dict:
    """An ``isError`` ``CallToolResult`` carrying ``payload`` as JSON text."""
    return {
        "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
        "isError": True,
    }


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
        output_schema: Optional[dict] = None,
    ) -> None:
        """Register a tool.

        ``batchable`` — may the tool run as an op in ``apply_edits`` /
        ``dry_run_edit``. Defaults to: the schema declares ``path`` and the
        tool isn't in :data:`NON_BATCHABLE_TOOLS`.

        ``mutating`` — the tool writes an RDL file; with
        ``PBIRB_MCP_AUTO_VERIFY`` on, the written file is verified after
        every successful call.

        ``output_schema`` — JSON Schema of the result (``outputSchema``).
        Defaults to the family schema from
        :func:`pbirb_mcp.core.output_schemas.default_output_schema`. A
        non-object result is published as ``{"result": value}``.
        """
        from pbirb_mcp.core.argcheck import client_schema
        from pbirb_mcp.core.output_schemas import default_output_schema

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
        # Destructive tools (and apply_edits) take `confirm` (issue #57).
        if name in _CONFIRMABLE_TOOLS and "confirm" not in props:
            props["confirm"] = _CONFIRM_PARAM
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
            output_schema=output_schema or default_output_schema(name, handler, mutating=mutating),
        )

    def is_batchable(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.batchable

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def requires_confirmation(self, name: str, arguments: Any) -> bool:
        """Would this call be refused for want of ``confirm=true`` (issue #57)?

        True for a top-level call (not an op nested inside apply_edits /
        dry_run_edit) of a destructive tool — or of apply_edits with a
        destructive op — while confirmation is required
        (``PBIRB_MCP_REQUIRE_CONFIRM``), and ``confirm`` isn't true.
        """
        from pbirb_mcp.core import security

        if _CALL_DEPTH.get() > 0 or not security.confirmation_required():
            return False
        args = arguments if isinstance(arguments, dict) else {}
        if args.get("confirm") is True:
            return False
        if name in DESTRUCTIVE_TOOLS:
            return True
        if name == "apply_edits" and isinstance(args.get("ops"), list):
            return any(
                isinstance(op, dict) and op.get("tool") in DESTRUCTIVE_TOOLS for op in args["ops"]
            )
        return False

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
                **({"outputSchema": tool.output_schema} if tool.output_schema else {}),
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
            return _failure(payload)

        # Issue #57: file-access sandbox, then the confirmation gate —
        # both before the handler, so a refused call never touches a file.
        from pbirb_mcp.core import security

        try:
            security.check_paths(arguments)
        except security.PathNotAllowed as exc:
            return _failure({"error_type": "PathNotAllowed", "message": str(exc), **exc.payload})
        if self.requires_confirmation(name, arguments):
            return _failure(security.confirmation_payload(name, arguments))
        if name in _CONFIRMABLE_TOOLS:
            arguments.pop("confirm", None)  # the gate's, not the handler's
        audited = tool.mutating or name in _AUDITED_EXTRA_TOOLS

        # Per MCP spec, tool-handler exceptions are surfaced in the result
        # content with `isError: true`, NOT as JSON-RPC errors. JSON-RPC errors
        # are reserved for protocol issues (bad envelope, unknown method, bad
        # params). Returning handler errors as JSON-RPC -32603 caused MCP
        # clients to render them as opaque "Tool execution failed" strings,
        # hiding the rich messages tools already raise (locators in
        # remove_parameter, missing-column hints in add_subtotal_row, etc.).
        depth = _CALL_DEPTH.set(_CALL_DEPTH.get() + 1)
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
            if audited:
                security.audit(
                    name,
                    arguments,
                    ok=False,
                    error_type=type(exc).__name__,
                    in_transaction=in_transaction,
                )
            payload = {
                "error_type": type(exc).__name__,
                "message": str(exc) or repr(exc),
            }
            # Exceptions may carry structured fields (e.g. OpNotAllowed's
            # `tool`); merge them into the payload.
            extra = getattr(exc, "payload", None)
            if isinstance(extra, dict):
                payload.update(extra)
            return _failure(payload)
        finally:
            _CALL_DEPTH.reset(depth)

        if audited:
            security.audit(name, arguments, ok=True, result=result, in_transaction=in_transaction)

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
            return _success({"result": result, "verify": verify})

        return _success(result)

    # ---- transport ----------------------------------------------------------

    def run_stdio(self) -> None:
        """Serve MCP over stdin/stdout (all supported protocol revisions)."""
        from pbirb_mcp.protocol import serve_stdio

        serve_stdio(self.sdk_server)
