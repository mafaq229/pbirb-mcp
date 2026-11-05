"""Toolsets and read-only mode: which tools a server exposes (issue #56).

Every tool belongs to exactly one toolset. ``core`` (inventory, validation,
batches, transactions, expression helpers) is always on; the others are
enabled with ``--toolsets`` / :envvar:`PBIRB_MCP_TOOLSETS` (comma-separated
names, ``all``, or ``default``). With no setting every default toolset is on,
so the server lists every tool, as before.

``--read-only`` / :envvar:`PBIRB_MCP_READ_ONLY` further limits the server to
tools published ``readOnlyHint: true`` (#54).

The policy is enforced at dispatch, not only in ``tools/list``: a call to a
tool outside the enabled toolsets, or to a tool that writes while the server
is read-only, gets an ``isError`` result saying which setting to change.
Ops that ``apply_edits`` / ``dry_run_edit`` run are held to the policy of the
top-level call (their toolsets must be enabled); the transaction control
``apply_edits`` drives internally is always allowed, and ``dry_run_edit``'s
ops may edit its own temp copy in read-only mode.

``OPT_IN_TOOLSETS`` holds toolsets that ``default`` leaves off and only an
explicit name (or ``all``) enables — room for a future ``service`` toolset
(publishing to the Power BI service, #58).
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Optional, Union

__all__ = [
    "ALWAYS_ON",
    "OPT_IN_TOOLSETS",
    "READ_ONLY_ENV",
    "TOOLSETS",
    "TOOLSETS_ENV",
    "ToolsetPolicy",
    "active_policy",
    "activate",
    "parse_toolsets",
    "pop",
    "push",
    "resolve_policy",
    "toolset_of",
]

TOOLSETS_ENV = "PBIRB_MCP_TOOLSETS"
READ_ONLY_ENV = "PBIRB_MCP_READ_ONLY"

ALWAYS_ON = "core"

# Toolset -> tool names. The edit toolsets match the eval's categories
# (evals/tool_selection: data, parameters, tablix, style, layout, chart,
# interactivity). tests/test_toolsets.py checks every registered tool is in
# exactly one.
TOOLSETS: dict[str, frozenset[str]] = {
    # Always on: inventory, validation, batches, transactions, expression helpers.
    "core": frozenset(
        {
            "describe_report",
            "validate_report",
            "verify_report",
            "lint_report",
            "apply_edits",
            "dry_run_edit",
            "start_editing_transaction",
            "commit_editing_transaction",
            "cancel_editing_transaction",
            "get_expression_reference",
            "count_where",
            "sum_where",
            "iif_format",
        }
    ),
    # Data sources, datasets, DAX queries, query parameters, fields, dataset filters.
    "data": frozenset(
        {
            "get_datasets",
            "get_dataset",
            "update_dataset_query",
            "add_query_parameter",
            "update_query_parameter",
            "remove_query_parameter",
            "set_datasource_connection",
            "list_data_sources",
            "get_data_source",
            "add_data_source",
            "remove_data_source",
            "rename_data_source",
            "list_dataset_filters",
            "add_dataset_filter",
            "remove_dataset_filter",
            "add_calculated_field",
            "remove_calculated_field",
            "add_dataset_field",
            "remove_dataset_field",
            "refresh_dataset_fields",
        }
    ),
    # Report parameters: declarations, values, prompts, the parameter pane layout.
    "parameters": frozenset(
        {
            "get_parameters",
            "add_parameter",
            "remove_parameter",
            "rename_parameter",
            "set_parameter_prompt",
            "set_parameter_type",
            "set_parameter_available_values",
            "set_parameter_default_values",
            "update_parameter_advanced",
            "reorder_parameters",
            "set_parameter_layout",
            "sync_parameter_layout",
        }
    ),
    # Tables and matrices: groups, columns, rows, subtotals, spans, filters,
    # sorting, group visibility and pagination.
    "tablix": frozenset(
        {
            "get_tablixes",
            "insert_tablix_from_template",
            "list_tablix_filters",
            "add_tablix_filter",
            "remove_tablix_filter",
            "add_row_group",
            "remove_row_group",
            "add_column_group",
            "remove_column_group",
            "convert_to_matrix",
            "set_tablix_corner",
            "set_group_sort",
            "set_column_group_sort",
            "set_group_visibility",
            "set_column_group_visibility",
            "set_detail_row_visibility",
            "add_tablix_column",
            "remove_tablix_column",
            "add_static_row",
            "add_static_column",
            "add_subtotal_row",
            "add_subtotal_column",
            "set_cell_span",
            "set_column_width",
            "set_row_height",
            "set_tablix_size",
            "set_group_page_break",
            "set_repeat_on_new_page",
            "set_repeat_header_rows",
            "set_keep_together",
            "set_keep_with_group",
        }
    ),
    # Charts: series, axes, legend, labels, palette, colours, title.
    "chart": frozenset(
        {
            "get_chart",
            "insert_chart_from_template",
            "add_chart_series",
            "remove_chart_series",
            "set_chart_series_type",
            "set_chart_series_grouping",
            "set_chart_axis",
            "set_chart_legend",
            "set_chart_data_labels",
            "set_chart_palette",
            "set_series_color",
            "set_chart_title",
            "set_chart_series_properties",
            "set_chart_category_group",
        }
    ),
    # Textbox text and formatting, finding textboxes, row styling.
    "style": frozenset(
        {
            "get_textbox",
            "find_textbox_by_value",
            "find_textboxes_by_style",
            "set_textbox_value",
            "set_textbox_runs",
            "set_textbox_style",
            "set_textbox_style_bulk",
            "copy_textbox_style",
            "style_tablix_row",
            "set_alternating_row_color",
            "set_conditional_row_color",
        }
    ),
    # Page setup, body/header/footer items, positions and sizes, containers,
    # images and embedded images, element visibility.
    "layout": frozenset(
        {
            "set_page_setup",
            "set_page_orientation",
            "set_body_size",
            "fit_page_to_body",
            "center_item",
            "set_page_header",
            "set_page_footer",
            "list_body_items",
            "list_header_items",
            "list_footer_items",
            "add_body_textbox",
            "add_body_image",
            "remove_body_item",
            "add_header_textbox",
            "add_header_image",
            "remove_header_item",
            "add_footer_textbox",
            "add_footer_image",
            "remove_footer_item",
            "set_body_item_position",
            "set_header_item_position",
            "set_footer_item_position",
            "set_body_item_size",
            "set_header_item_size",
            "set_footer_item_size",
            "add_rectangle",
            "add_list",
            "add_line",
            "move_report_item",
            "ungroup_rectangle",
            "rename_report_item",
            "get_rectangle",
            "get_image",
            "set_image_sizing",
            "set_image_source",
            "add_embedded_image",
            "list_embedded_images",
            "get_embedded_image_data",
            "remove_embedded_image",
            "set_element_visibility",
        }
    ),
    # Actions (links, drill-through, bookmarks), tooltips, document map.
    "interactivity": frozenset(
        {
            "set_textbox_action",
            "set_image_action",
            "set_chart_series_action",
            "set_item_action",
            "set_textbox_tooltip",
            "set_item_tooltip",
            "set_bookmark",
            "set_document_map_label",
        }
    ),
    # Report files and the raw-XML escape hatch.
    "admin": frozenset(
        {
            "create_report",
            "duplicate_report",
            "backup_report",
            "list_backups",
            "restore_from_backup",
            "raw_xml_view",
            "raw_xml_replace",
        }
    ),
}

# Toolsets `default` leaves off; enabled only by name or `all`.
OPT_IN_TOOLSETS: frozenset[str] = frozenset()

DEFAULT_TOOLSETS: frozenset[str] = frozenset(TOOLSETS) - OPT_IN_TOOLSETS

_TOOLSET_OF: dict[str, str] = {tool: ts for ts, tools in TOOLSETS.items() for tool in tools}

# Transaction control: apply_edits drives these as nested calls, so they are
# allowed there whatever the policy says.
_INTERNAL_TOOLS = frozenset(
    {"start_editing_transaction", "commit_editing_transaction", "cancel_editing_transaction"}
)


def toolset_of(tool: str) -> str:
    """The toolset ``tool`` belongs to; an unlisted tool is in ``core``."""
    return _TOOLSET_OF.get(tool, ALWAYS_ON)


def parse_toolsets(spec: Union[str, Iterable[str], None]) -> frozenset[str]:
    """The toolsets a ``--toolsets`` value enables (``core`` always included).

    ``spec`` is comma-separated names (or an iterable of them); ``all`` is
    every toolset, ``default`` every non-opt-in one. Empty / ``None`` means
    ``default``. Raises :class:`ValueError` naming the valid toolsets for an
    unknown name.
    """
    if spec is None:
        names: list[str] = []
    elif isinstance(spec, str):
        names = [n.strip().lower() for n in spec.split(",") if n.strip()]
    else:
        names = [str(n).strip().lower() for n in spec if str(n).strip()]
    if not names:
        return DEFAULT_TOOLSETS
    enabled = {ALWAYS_ON}
    for name in names:
        if name == "all":
            enabled |= set(TOOLSETS)
        elif name == "default":
            enabled |= DEFAULT_TOOLSETS
        elif name in TOOLSETS:
            enabled.add(name)
        else:
            valid = ", ".join([*TOOLSETS, "all", "default"])
            raise ValueError(f"unknown toolset {name!r}; valid toolsets: {valid}")
    return frozenset(enabled)


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ToolsetPolicy:
    """Which tools a server exposes and accepts."""

    toolsets: frozenset[str] = DEFAULT_TOOLSETS
    read_only: bool = False

    def lists(self, toolset: str, *, read_only_tool: bool) -> bool:
        """Is a tool of ``toolset`` published in ``tools/list``?"""
        if toolset not in self.toolsets:
            return False
        return read_only_tool or not self.read_only

    def refusal(
        self,
        toolset: str,
        tool: str,
        *,
        read_only_tool: bool,
        nested: bool,
        preview_only: bool = False,
    ) -> Optional[dict[str, Any]]:
        """The ``isError`` payload refusing a call of ``tool`` (in
        ``toolset``), or ``None``.

        ``nested`` — the call is an op of a batch (apply_edits /
        dry_run_edit) whose top-level call this policy already allowed.
        ``preview_only`` — every file it writes is a dry-run temp copy.
        """
        if nested and tool in _INTERNAL_TOOLS:
            return None
        if toolset not in self.toolsets:
            enabled = sorted(self.toolsets)
            return {
                "error_type": "ToolNotEnabled",
                "message": (
                    f"{tool} is in the {toolset!r} toolset, which this server does not "
                    f"enable (enabled: {', '.join(enabled)}). Use a tool from an enabled "
                    f"toolset, or have the user add {toolset!r} to --toolsets / "
                    f"{TOOLSETS_ENV} in the MCP server configuration."
                ),
                "tool": tool,
                "toolset": toolset,
                "enabled_toolsets": enabled,
                "setting": TOOLSETS_ENV,
            }
        if self.read_only and not read_only_tool and not (nested and preview_only):
            return {
                "error_type": "ReadOnlyMode",
                "message": (
                    f"{tool} changes files, and this server runs read-only, so nothing "
                    "was changed. Read-only tools still work (dry_run_edit previews an "
                    "edit). To edit, the user must turn off --read-only / "
                    f"{READ_ONLY_ENV} in the MCP server configuration."
                ),
                "tool": tool,
                "setting": READ_ONLY_ENV,
            }
        return None


def resolve_policy(
    toolsets: Union[str, Iterable[str], None] = None, read_only: Optional[bool] = None
) -> ToolsetPolicy:
    """The policy for explicit settings, falling back to the environment.

    ``None`` means "not set here": read :envvar:`PBIRB_MCP_TOOLSETS` /
    :envvar:`PBIRB_MCP_READ_ONLY`; unset, every default toolset is on and
    the server is read-write.
    """
    if toolsets is None:
        toolsets = os.environ.get(TOOLSETS_ENV)
    if read_only is None:
        read_only = _truthy(os.environ.get(READ_ONLY_ENV, ""))
    return ToolsetPolicy(toolsets=parse_toolsets(toolsets), read_only=bool(read_only))


# The policy of the top-level call being dispatched; nested (batch op) calls
# are held to it rather than to the policy of the server that dispatches them.
_ACTIVE: contextvars.ContextVar[Optional[ToolsetPolicy]] = contextvars.ContextVar(
    "pbirb_mcp_toolset_policy", default=None
)


def active_policy() -> Optional[ToolsetPolicy]:
    """The policy of the enclosing top-level call, or ``None`` outside one."""
    return _ACTIVE.get()


def push(policy: ToolsetPolicy) -> contextvars.Token:
    """Make ``policy`` the active one (for the calls a handler dispatches);
    undo with :func:`pop`."""
    return _ACTIVE.set(policy)


def pop(token: contextvars.Token) -> None:
    _ACTIVE.reset(token)


@contextlib.contextmanager
def activate(policy: ToolsetPolicy) -> Iterator[None]:
    """Run the enclosed calls under ``policy``."""
    token = push(policy)
    try:
        yield
    finally:
        pop(token)
