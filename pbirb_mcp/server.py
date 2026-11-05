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

Optional :envvar:`PBIRB_MCP_AUTO_BACKUP` (issue #63): when truthy the
dispatcher backs the report up (:func:`pbirb_mcp.ops.snapshot.maybe_auto_backup`)
before every tool in :data:`AUTO_BACKUP_TOOLS` and adds the backup's path
to the result as ``auto_backup``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

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
        "remove_dataset",
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
        # Deletes the columns, rows and textboxes a matrix no longer needs.
        "convert_to_matrix",
        # Replaces an arbitrary element with caller-supplied XML.
        "raw_xml_replace",
        # Copies a backup over the report.
        "restore_from_backup",
    }
)

# Tools that PBIRB_MCP_AUTO_BACKUP backs the report up before (issue #63):
# the destructive ones, plus edits that rewrite references or a whole query
# and are hard to undo by hand. restore_from_backup isn't listed: it always
# backs the target up itself (its `safety_backup`).
AUTO_BACKUP_TOOLS = (DESTRUCTIVE_TOOLS - {"restore_from_backup"}) | {
    "rename_data_source",
    "rename_dataset",
    "rename_dataset_field",
    "rename_parameter",
    "rename_report_item",
    "update_dataset_query",
    "refresh_dataset_fields",
}

# Mutating tools whose second identical call is a no-op: it succeeds,
# reports nothing changed and leaves the file alone (issue #61). Published
# as idempotentHint (#54). tests/test_annotations.py keeps this equal to the
# "noop" cases of tests/test_idempotency.py; a tool whose repeat call
# refuses (creators, removers, most renames) or appends again is not listed.
IDEMPOTENT_TOOLS = frozenset(
    {
        "center_item",
        "copy_textbox_style",
        "fit_page_to_body",
        "move_report_item",
        "raw_xml_replace",
        "refresh_dataset_fields",
        "rename_report_item",
        "reorder_parameters",
        "set_alternating_row_color",
        "set_body_item_position",
        "set_body_item_size",
        "set_body_size",
        "set_bookmark",
        "set_cell_span",
        "set_chart_axis",
        "set_chart_category_group",
        "set_chart_data_labels",
        "set_chart_legend",
        "set_chart_palette",
        "set_chart_series_action",
        "set_chart_series_grouping",
        "set_chart_series_properties",
        "set_chart_series_type",
        "set_chart_title",
        "set_column_group_sort",
        "set_column_group_visibility",
        "set_column_width",
        "set_conditional_row_color",
        "set_dataset_data_source",
        "set_dataset_options",
        "set_datasource_connection",
        "set_detail_row_visibility",
        "set_document_map_label",
        "set_element_visibility",
        "set_field_type",
        "set_footer_item_position",
        "set_footer_item_size",
        "set_group_page_break",
        "set_group_sort",
        "set_group_visibility",
        "set_header_item_position",
        "set_header_item_size",
        "set_image_action",
        "set_image_sizing",
        "set_image_source",
        "set_item_action",
        "set_item_tooltip",
        "set_keep_together",
        "set_keep_with_group",
        "set_page_footer",
        "set_page_header",
        "set_page_orientation",
        "set_page_setup",
        "set_parameter_available_values",
        "set_parameter_default_values",
        "set_parameter_layout",
        "set_parameter_prompt",
        "set_parameter_type",
        "set_repeat_header_rows",
        "set_repeat_on_new_page",
        "set_row_height",
        "set_series_color",
        "set_tablix_corner",
        "set_tablix_size",
        "set_textbox_action",
        "set_textbox_runs",
        "set_textbox_style",
        "set_textbox_style_bulk",
        "set_textbox_tooltip",
        "set_textbox_value",
        "style_tablix_row",
        "sync_parameter_layout",
        "update_dataset_query",
        "update_parameter_advanced",
        "update_query_parameter",
    }
)

# Tools that write no report file (mutating=False) yet aren't read-only:
# transaction control holds edit state (commit saves the report) and
# backup_report writes a backup copy. Every other non-mutating tool only
# reads, and is published readOnlyHint=true.
_STATEFUL_TOOLS = TRANSACTION_CONTROL_TOOLS | {"backup_report"}

# Display titles where the name doesn't spell out well word by word.
_TITLE_OVERRIDES = {
    "count_where": "Build a conditional Count expression",
    "sum_where": "Build a conditional Sum expression",
    "iif_format": "Build an IIf expression",
    "dry_run_edit": "Preview edits (dry run)",
    "apply_edits": "Apply edits atomically",
    "raw_xml_view": "View raw XML",
    "raw_xml_replace": "Replace raw XML",
    "lint_report": "Lint report",
}
_TITLE_WORDS = {"xml": "XML", "rdl": "RDL", "dax": "DAX", "iif": "IIf"}


def tool_title(name: str) -> str:
    """Human-readable title for a tool name (``set_page_setup`` → ``Set page setup``)."""
    if name in _TITLE_OVERRIDES:
        return _TITLE_OVERRIDES[name]
    words = [_TITLE_WORDS.get(w, w) for w in name.split("_") if w]
    if not words:
        return name
    return " ".join([words[0][:1].upper() + words[0][1:], *words[1:]])


def tool_annotations(name: str, *, mutating: bool) -> dict:
    """The MCP ``ToolAnnotations`` hints for a tool (issue #54).

    The display title is published once, as the tool's top-level ``title``
    (2025-06-18+), not repeated as ``annotations.title``. The hints are
    derived from the classifications above, not declared per tool:
    ``readOnlyHint`` — writes nothing (not ``mutating``, not stateful);
    ``destructiveHint`` — in :data:`DESTRUCTIVE_TOOLS` (deletes report
    content or overwrites a file; ordinary in-place edits are not);
    ``idempotentHint`` — read-only, or in :data:`IDEMPOTENT_TOOLS`;
    ``openWorldHint`` — always false: every tool works on local files only.
    """
    read_only = not mutating and name not in _STATEFUL_TOOLS
    return {
        "readOnlyHint": read_only,
        "destructiveHint": name in DESTRUCTIVE_TOOLS,
        "idempotentHint": read_only or name in IDEMPOTENT_TOOLS,
        "openWorldHint": False,
    }


# Tools that take `confirm`: the destructive ones, plus apply_edits, which
# needs it when any of its ops is destructive.
_CONFIRMABLE_TOOLS = DESTRUCTIVE_TOOLS | {"apply_edits"}

_CONFIRM_PARAM = {
    "type": "boolean",
    "default": False,
    "description": (
        "true runs this destructive edit; required when PBIRB_MCP_REQUIRE_CONFIRM "
        "is on (the pbirb-mcp default). Ask the user first."
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
    "description": "Optional: run inside this start_editing_transaction id.",
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
    # Does this tool write an RDL file? (drives PBIRB_MCP_AUTO_VERIFY, the
    # audit log and readOnlyHint)
    mutating: bool = False
    # May a call carry `transaction_id` to run inside a transaction?
    routable: bool = False
    # The client-safe schema published in tools/list.
    published_schema: Optional[dict] = None
    # The JSON Schema of the result, published as `outputSchema`; every
    # successful call returns a matching `structuredContent` (issue #55).
    output_schema: Optional[dict] = None
    # Display name and MCP ToolAnnotations (issue #54); see tool_annotations.
    title: str = ""
    annotations: Optional[dict] = None
    # The toolset it belongs to (issue #56; pbirb_mcp.core.toolsets).
    toolset: str = "core"

    @property
    def read_only(self) -> bool:
        return bool(self.annotations and self.annotations.get("readOnlyHint"))


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
    def __init__(
        self,
        *,
        toolsets: Union[str, Iterable[str], None] = None,
        read_only: Optional[bool] = None,
    ) -> None:
        """``toolsets`` / ``read_only`` — which tools this server lists and
        accepts (issue #56; see :mod:`pbirb_mcp.core.toolsets`). ``None``
        reads ``PBIRB_MCP_TOOLSETS`` / ``PBIRB_MCP_READ_ONLY``; unset, every
        tool is on."""
        from pbirb_mcp.core.toolsets import resolve_policy
        from pbirb_mcp.protocol import AdapterState

        self._tools: dict[str, Tool] = {}
        self._sdk_server = None
        self._adapter_state = AdapterState()
        self.toolset_policy = resolve_policy(toolsets, read_only)

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
        title: Optional[str] = None,
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

        ``title`` — display name; defaults to :func:`tool_title`. The
        title and the read-only / destructive / idempotent / open-world
        hints are published as ``title`` and ``annotations``
        (:func:`tool_annotations`).
        """
        from pbirb_mcp.core.argcheck import client_schema
        from pbirb_mcp.core.output_schemas import default_output_schema
        from pbirb_mcp.core.toolsets import toolset_of

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
            title=title or tool_title(name),
            annotations=tool_annotations(name, mutating=mutating),
            toolset=toolset_of(name),
        )

    def is_batchable(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.batchable

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def toolset_of(self, name: str) -> str:
        """The toolset of a registered tool (issue #56)."""
        return self._tools[name].toolset

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

    def is_enabled(self, name: str) -> bool:
        """Is ``name`` listed and callable under this server's toolset /
        read-only policy (issue #56)?"""
        tool = self._tools.get(name)
        return tool is not None and self.toolset_policy.lists(
            tool.toolset, read_only_tool=tool.read_only
        )

    def _tools_list(self) -> list[dict]:
        """The ``tools/list`` entries: every tool the policy enables, in
        registration order."""
        return [
            {
                "name": tool.name,
                "title": tool.title,
                "description": tool.description,
                "inputSchema": tool.published_schema or tool.input_schema,
                **({"outputSchema": tool.output_schema} if tool.output_schema else {}),
                **({"annotations": tool.annotations} if tool.annotations else {}),
            }
            for tool in self._tools.values()
            if self.is_enabled(tool.name)
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

        # Issue #56: toolsets / read-only mode, enforced at dispatch (not
        # just hidden from tools/list). An op of a batch (apply_edits /
        # dry_run_edit) is held to the policy of the top-level call, not to
        # the policy of the server the batch dispatches it through.
        from pbirb_mcp.core import security
        from pbirb_mcp.core import toolsets as _toolsets

        outer_policy = _toolsets.active_policy()
        policy = outer_policy if outer_policy is not None else self.toolset_policy
        refusal = policy.refusal(
            tool.toolset,
            name,
            read_only_tool=tool.read_only,
            nested=outer_policy is not None,
            preview_only=security.is_preview_path(arguments.get("path")),
        )
        if refusal is not None:
            return _failure(refusal)

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

        # Issue #86: expand `~` in every path argument, once, here — the
        # sandbox check below and the handler both see the expanded path.
        arguments = security.expand_user_paths(arguments)
        # Issue #57: file-access sandbox, then the confirmation gate —
        # both before the handler, so a refused call never touches a file.
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
        policy_token = _toolsets.push(policy)
        auto_backup = None
        try:
            # Issue #63: PBIRB_MCP_AUTO_BACKUP — back the report up before a
            # destructive edit (not a dry-run's preview copy). Inside a
            # transaction this is the file as it was before the
            # transaction; repeats reuse the backup.
            report = arguments.get("path")
            if (
                name in AUTO_BACKUP_TOOLS
                and isinstance(report, str)
                and not security.is_preview_path(report)
            ):
                from pbirb_mcp.ops.snapshot import maybe_auto_backup

                auto_backup = maybe_auto_backup(report)
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
            _toolsets.pop(policy_token)
            _CALL_DEPTH.reset(depth)

        if auto_backup is not None and isinstance(result, dict):
            result = {**result, "auto_backup": auto_backup["backup"]}
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
