"""Shared guards for the batch tools (``apply_edits`` / ``dry_run_edit``).

Both tools replay ``{tool, args}`` ops through the JSON-RPC dispatcher.
Only tools registered as *batchable* (see
:meth:`pbirb_mcp.server.MCPServer.register_tool`) may appear as ops:
transaction control, nested batch tools, and tools that write outside the
batch's tree (backup / restore / duplicate / create) are refused up front,
before anything is opened, copied or registered. So is an op whose tool is
outside the toolsets the server enables (issue #56).
"""

from __future__ import annotations

from typing import Any


class OpNotAllowed(ValueError):
    """A batch op names a tool that may not run inside a batch."""

    def __init__(self, tool: str, index: int) -> None:
        super().__init__(
            f"op #{index}: tool {tool!r} can't run inside a batch (transaction "
            "control, nested batches and tools that write outside the batch's "
            "tree — backup/restore/duplicate/create — are not allowed); call "
            "it on its own instead"
        )
        self.payload = {"tool": tool, "op_index": index}


class OpNotEnabled(ValueError):
    """A batch op names a tool outside the enabled toolsets (issue #56)."""

    def __init__(self, refusal: dict, index: int) -> None:
        super().__init__(f"op #{index}: {refusal['message']}")
        self.payload = {**refusal, "op_index": index}


def check_ops(server: Any, ops: Any) -> None:
    """Validate the op list before a batch does anything.

    Raises :class:`ValueError` for a malformed list and
    :class:`OpNotAllowed` for a known, non-batchable tool and
    :class:`OpNotEnabled` for one outside the enabled toolsets. Unknown tool
    names and ops without a string ``tool`` are left to fail at dispatch
    (the batch reports them per op).
    """
    from pbirb_mcp.core import toolsets

    if not isinstance(ops, list):
        raise ValueError("ops must be a list of {tool, args} entries")
    policy = toolsets.active_policy()
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ValueError(f"op #{i} must be an object {{tool, args}}")
        args = op.get("args")
        if args is not None and not isinstance(args, dict):
            raise ValueError(f"op #{i} `args` must be an object")
        tool = op.get("tool")
        if isinstance(tool, str) and server.has_tool(tool) and not server.is_batchable(tool):
            raise OpNotAllowed(tool, i)
        if policy is not None and isinstance(tool, str) and server.has_tool(tool):
            # Toolset membership only: read-only mode is enforced per op at
            # dispatch (dry_run_edit's ops may edit its temp copy).
            refusal = policy.refusal(
                server.toolset_of(tool), tool, read_only_tool=True, nested=True
            )
            if refusal is not None:
                raise OpNotEnabled(refusal, i)


def payload_reports_failure(payload: Any) -> bool:
    """True when a tool's (non-isError) result says it didn't do its job —
    e.g. ``{committed: false}`` / ``{saved: false}``."""
    return isinstance(payload, dict) and (
        payload.get("committed") is False or payload.get("saved") is False
    )


__all__ = ["OpNotAllowed", "OpNotEnabled", "check_ops", "payload_reports_failure"]
