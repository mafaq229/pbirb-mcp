"""Tool registry — wires ops modules into the JSON-RPC server.

Each ops module exposes plain Python functions; this module describes their
JSON Schema and registers them with an :class:`MCPServer`. Adding a new tool
means appending one entry here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pbirb_mcp.ops import (
    body,
    dataset,
    datasource,
    embedded_images,
    header_footer,
    page,
    parameters,
    positioning,
    reader,
    snapshot,
    styling,
    tablix,
    tablix_cells,
    tablix_columns,
    tablix_static,
    tablix_subtotals,
    templates,
    visibility,
)

if TYPE_CHECKING:
    from pbirb_mcp.server import MCPServer


_PATH_ONLY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Absolute path to the .rdl file to read.",
        }
    },
    "required": ["path"],
    "additionalProperties": False,
}


def register_all_tools(server: MCPServer) -> None:
    server.register_tool(
        name="describe_report",
        description=(
            "Top-level inventory of an RDL: data sources, datasets, parameters, "
            "tablixes, and page setup. Always the first call when planning edits."
        ),
        input_schema=_PATH_ONLY_SCHEMA,
        handler=reader.describe_report,
    )
    server.register_tool(
        name="get_datasets",
        description=(
            "Full DAX command text, fields, query parameters, and dataset-level "
            "filters for every DataSet in the report."
        ),
        input_schema=_PATH_ONLY_SCHEMA,
        handler=reader.get_datasets,
    )
    server.register_tool(
        name="get_parameters",
        description="Report parameter declarations: name, data type, prompt, flags.",
        input_schema=_PATH_ONLY_SCHEMA,
        handler=reader.get_parameters,
    )
    server.register_tool(
        name="get_tablixes",
        description=(
            "Tablix layout with stable IDs: columns, row/column groups, sort "
            "expressions, filters, visibility. Required input for any tablix edit."
        ),
        input_schema=_PATH_ONLY_SCHEMA,
        handler=reader.get_tablixes,
    )

    server.register_tool(
        name="update_dataset_query",
        description=(
            "Replace the DAX command text of a named dataset. The full DAX "
            "expression is accepted verbatim; empty bodies are rejected."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "dax_body": {
                    "type": "string",
                    "description": "Full DAX (e.g. EVALUATE TOPN(10, 'Sales')).",
                },
            },
            "required": ["path", "dataset_name", "dax_body"],
            "additionalProperties": False,
        },
        handler=dataset.update_dataset_query,
    )
    server.register_tool(
        name="add_query_parameter",
        description=(
            "Add a <QueryParameter> binding to a dataset's query. Use to wire "
            "report parameters into DAX (e.g. =Parameters!DateFrom.Value)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "name": {"type": "string"},
                "value_expression": {"type": "string"},
            },
            "required": ["path", "dataset_name", "name", "value_expression"],
            "additionalProperties": False,
        },
        handler=dataset.add_query_parameter,
    )
    server.register_tool(
        name="update_query_parameter",
        description="Change the value expression of an existing query parameter.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "name": {"type": "string"},
                "value_expression": {"type": "string"},
            },
            "required": ["path", "dataset_name", "name", "value_expression"],
            "additionalProperties": False,
        },
        handler=dataset.update_query_parameter,
    )
    server.register_tool(
        name="set_datasource_connection",
        description=(
            "Repoint a DataSource at a Power BI XMLA endpoint. workspace_url "
            "accepts a bare workspace name or a full powerbi:// URL; "
            "DataProvider is set to SQL (the AS provider id in RDL)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {
                    "type": "string",
                    "description": "RDL DataSource Name attribute.",
                },
                "workspace_url": {
                    "type": "string",
                    "description": "Workspace name or full powerbi:// XMLA URL.",
                },
                "dataset_name": {
                    "type": "string",
                    "description": "PBI semantic model (Initial Catalog).",
                },
                "integrated_security": {
                    "type": "boolean",
                    "description": "Default true. False omits the element.",
                },
            },
            "required": ["path", "name", "workspace_url", "dataset_name"],
            "additionalProperties": False,
        },
        handler=datasource.set_datasource_connection,
    )

    server.register_tool(
        name="remove_query_parameter",
        description=(
            "Remove a query parameter from a dataset. Cleans up the empty "
            "<QueryParameters/> block when removing the last one."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["path", "dataset_name", "name"],
            "additionalProperties": False,
        },
        handler=dataset.remove_query_parameter,
    )

    server.register_tool(
        name="list_tablix_filters",
        description=(
            "List all filters on a named tablix in document order. Returns "
            "expression, operator, and values per filter; index in the list "
            "is the stable handle for remove_tablix_filter."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
            },
            "required": ["path", "tablix_name"],
            "additionalProperties": False,
        },
        handler=tablix.list_tablix_filters,
    )
    server.register_tool(
        name="add_tablix_filter",
        description=(
            "Append a <Filter> to a tablix. Operator must be one of the RDL "
            "2016 enumeration (Equal, NotEqual, GreaterThan, In, Between, ...). "
            "Returns the new filter's index for follow-up calls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "expression": {"type": "string"},
                "operator": {"type": "string"},
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "required": ["path", "tablix_name", "expression", "operator", "values"],
            "additionalProperties": False,
        },
        handler=tablix.add_tablix_filter,
    )
    server.register_tool(
        name="add_row_group",
        description=(
            "Add a row group that wraps the entire current top-level row "
            "hierarchy. Inserts a matching group-header row at body row 0 "
            "with the group expression in the first cell. parent_group "
            "(nesting under an existing group) is reserved for a future "
            "commit and currently raises NotImplementedError."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "group_expression": {
                    "type": "string",
                    "description": "RDL expression, e.g. =Fields!Region.Value",
                },
                "parent_group": {"type": ["string", "null"]},
            },
            "required": ["path", "tablix_name", "group_name", "group_expression"],
            "additionalProperties": False,
        },
        handler=tablix.add_row_group,
    )
    server.register_tool(
        name="remove_row_group",
        description=(
            "Inverse of add_row_group: unwraps a group's children back to "
            "its parent hierarchy and removes the matching header row at "
            "body row 0. Refuses to remove the conventional Details group."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
            },
            "required": ["path", "tablix_name", "group_name"],
            "additionalProperties": False,
        },
        handler=tablix.remove_row_group,
    )
    server.register_tool(
        name="set_group_sort",
        description=(
            "Replace a group's <SortExpressions> with a fresh list. Each "
            "entry is an RDL expression, e.g. =Fields!Region.Value."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "sort_expressions": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "path",
                "tablix_name",
                "group_name",
                "sort_expressions",
            ],
            "additionalProperties": False,
        },
        handler=tablix.set_group_sort,
    )
    server.register_tool(
        name="set_group_visibility",
        description=(
            "Set <Visibility> on a group's TablixMember. Accepts a Hidden "
            "expression and an optional ToggleItem (a textbox name that "
            "toggles the group expand/collapse)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "visibility_expression": {"type": "string"},
                "toggle_textbox": {"type": ["string", "null"]},
            },
            "required": [
                "path",
                "tablix_name",
                "group_name",
                "visibility_expression",
            ],
            "additionalProperties": False,
        },
        handler=tablix.set_group_visibility,
    )

    # ---- column-axis groups (v0.2) ----------------------------------------
    server.register_tool(
        name="add_column_group",
        description=(
            "Add a column group that wraps the current top-level column "
            "hierarchy. Inserts a matching column at body column 0 (default "
            "1in width) and a header cell at column 0 of every existing row "
            "with the group expression in the topmost cell. Mirrors "
            "add_row_group on the column axis. parent_group nesting is "
            "reserved for a future commit and currently raises "
            "NotImplementedError."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "group_expression": {
                    "type": "string",
                    "description": "RDL expression, e.g. =Fields!Region.Value",
                },
                "parent_group": {"type": ["string", "null"]},
            },
            "required": ["path", "tablix_name", "group_name", "group_expression"],
            "additionalProperties": False,
        },
        handler=tablix_columns.add_column_group,
    )
    server.register_tool(
        name="remove_column_group",
        description=(
            "Inverse of add_column_group: unwraps a column-axis group's "
            "children back to the top of the column hierarchy and removes "
            "the matching body column at position 0 (along with each row's "
            "first cell). Errors if group_name only exists on the row axis."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
            },
            "required": ["path", "tablix_name", "group_name"],
            "additionalProperties": False,
        },
        handler=tablix_columns.remove_column_group,
    )
    server.register_tool(
        name="set_column_group_sort",
        description=(
            "Replace a column-axis group's <SortExpressions> with a fresh "
            "list. Mirrors set_group_sort but refuses up front if "
            "group_name is on the row axis (use set_group_sort instead)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "sort_expressions": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["path", "tablix_name", "group_name", "sort_expressions"],
            "additionalProperties": False,
        },
        handler=tablix_columns.set_column_group_sort,
    )
    server.register_tool(
        name="set_column_group_visibility",
        description=(
            "Set <Visibility> on a column-axis group's TablixMember. Accepts "
            "a Hidden expression and an optional ToggleItem (textbox name "
            "that toggles expand/collapse). Mirrors set_group_visibility "
            "but refuses up front if group_name is on the row axis."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "visibility_expression": {"type": "string"},
                "toggle_textbox": {"type": ["string", "null"]},
            },
            "required": [
                "path",
                "tablix_name",
                "group_name",
                "visibility_expression",
            ],
            "additionalProperties": False,
        },
        handler=tablix_columns.set_column_group_visibility,
    )
    server.register_tool(
        name="add_tablix_column",
        description=(
            "Append (or insert) a column into a tablix. column_name is the "
            "textbox name placed in the data row's new cell — must be unique "
            "report-wide. expression goes inside that textbox's TextRun "
            "(typically =Fields!X.Value). For a tablix with >= 2 rows the "
            "first row gets header_text (default = column_name) as a literal, "
            "middle rows get blank cells, and the last row gets expression. "
            "position is 0-indexed; default appends at end. width defaults to "
            "1in. Inserts a matching top-level TablixMember in the column "
            "hierarchy at the same index."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "column_name": {"type": "string"},
                "expression": {"type": "string"},
                "position": {"type": ["integer", "null"], "minimum": 0},
                "width": {"type": ["string", "null"]},
                "header_text": {"type": ["string", "null"]},
            },
            "required": ["path", "tablix_name", "column_name", "expression"],
            "additionalProperties": False,
        },
        handler=tablix_columns.add_tablix_column,
    )
    server.register_tool(
        name="remove_tablix_column",
        description=(
            "Remove the tablix column whose data-row cell holds a textbox "
            "named column_name. Drops the matching TablixColumn, removes the "
            "top-level TablixMember at that column index (only if it's a "
            "leaf, never a column group wrapper), and removes the cell at "
            "that index from every TablixRow. Errors if no row contains a "
            "textbox with the given name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "column_name": {"type": "string"},
            },
            "required": ["path", "tablix_name", "column_name"],
            "additionalProperties": False,
        },
        handler=tablix_columns.remove_tablix_column,
    )
    server.register_tool(
        name="add_subtotal_row",
        description=(
            "Append a subtotal row to a row-axis group. aggregates is a list "
            "of {column, expression} entries; column matches against the "
            "Details row's textbox names (the same names add_tablix_column "
            "uses as column_name — NOT field names). expression is the "
            "aggregate (e.g. =Sum(Fields!X.Value)). Columns not listed get "
            "blank cells. position='footer' (default) appends; 'header' "
            "inserts at body row 1 (right after the group-header row). "
            "Group must have been added via add_row_group."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "aggregates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "expression": {"type": "string"},
                        },
                        "required": ["column", "expression"],
                        "additionalProperties": False,
                    },
                },
                "position": {
                    "type": "string",
                    "enum": ["header", "footer"],
                    "default": "footer",
                },
            },
            "required": ["path", "tablix_name", "group_name", "aggregates"],
            "additionalProperties": False,
        },
        handler=tablix_subtotals.add_subtotal_row,
    )
    server.register_tool(
        name="set_cell_span",
        description=(
            "Set <RowSpan> and/or <ColSpan> on a tablix cell. The cell is "
            "addressed by (row_index, column_name) where column_name is the "
            "textbox name inside the cell. At least one of row_span / "
            "col_span must be supplied; both must be >= 1. Pass 1 to "
            "explicitly reset a span. Replaces existing values if present."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "row_index": {"type": "integer", "minimum": 0},
                "column_name": {"type": "string"},
                "row_span": {"type": ["integer", "null"], "minimum": 1},
                "col_span": {"type": ["integer", "null"], "minimum": 1},
            },
            "required": ["path", "tablix_name", "row_index", "column_name"],
            "additionalProperties": False,
        },
        handler=tablix_cells.set_cell_span,
    )
    server.register_tool(
        name="add_static_row",
        description=(
            "Add a static (no-group) row to a tablix. Each cell holds "
            "literal text. cells is a list of strings, one per body column "
            "(left to right); shorter list = blank trailing cells, longer "
            "list errors. Cell textboxes are named row_name (col 0) and "
            "row_name_<col_index> (others) — unique report-wide so row_name "
            "must not clash with any existing textbox. position is "
            "0-indexed; default appends. height defaults to 0.25in."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "row_name": {"type": "string"},
                "cells": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "position": {"type": ["integer", "null"], "minimum": 0},
                "height": {"type": ["string", "null"]},
            },
            "required": ["path", "tablix_name", "row_name"],
            "additionalProperties": False,
        },
        handler=tablix_static.add_static_row,
    )
    server.register_tool(
        name="add_static_column",
        description=(
            "Add a static (no-group) column to a tablix. Each cell holds "
            "literal text. cells is a list of strings, one per body row "
            "(top to bottom); shorter list = blank trailing cells, longer "
            "list errors. Cell textboxes are named column_name (row 0) and "
            "column_name_<row_index> (others). position is 0-indexed; "
            "default appends. width defaults to 1in."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "column_name": {"type": "string"},
                "cells": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "position": {"type": ["integer", "null"], "minimum": 0},
                "width": {"type": ["string", "null"]},
            },
            "required": ["path", "tablix_name", "column_name"],
            "additionalProperties": False,
        },
        handler=tablix_static.add_static_column,
    )

    # ---- positioning (v0.2 commits 6-8) -----------------------------------
    server.register_tool(
        name="set_body_item_position",
        description=(
            "Move an existing named ReportItem inside <Body> to (top, left). "
            "Preserves all other properties (size, style, group structure). "
            "top and left are passed through verbatim — RDL accepts any size "
            "unit (2cm, 0.75in, 108pt). Errors if no body item by that name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "top": {"type": "string"},
                "left": {"type": "string"},
            },
            "required": ["path", "name", "top", "left"],
            "additionalProperties": False,
        },
        handler=positioning.set_body_item_position,
    )
    server.register_tool(
        name="set_header_item_position",
        description=(
            "Move an existing named ReportItem inside <PageHeader> to "
            "(top, left). Errors if there is no <PageHeader> (call "
            "set_page_header first) or no item by that name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "top": {"type": "string"},
                "left": {"type": "string"},
            },
            "required": ["path", "name", "top", "left"],
            "additionalProperties": False,
        },
        handler=positioning.set_header_item_position,
    )
    server.register_tool(
        name="set_footer_item_position",
        description=(
            "Move an existing named ReportItem inside <PageFooter> to "
            "(top, left). Errors if there is no <PageFooter> (call "
            "set_page_footer first) or no item by that name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "top": {"type": "string"},
                "left": {"type": "string"},
            },
            "required": ["path", "name", "top", "left"],
            "additionalProperties": False,
        },
        handler=positioning.set_footer_item_position,
    )
    server.register_tool(
        name="set_body_item_size",
        description=(
            "Resize an existing named ReportItem inside <Body>. At least "
            "one of width / height must be supplied; missing fields are "
            "left untouched. Same RDL size-string convention as the "
            "position tools."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "width": {"type": ["string", "null"]},
                "height": {"type": ["string", "null"]},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=positioning.set_body_item_size,
    )

    # ---- reader extensions (v0.2) -----------------------------------------
    server.register_tool(
        name="list_body_items",
        description=(
            "List every named ReportItem at the top level of <Body>. "
            "Returns name, type (Tablix / Textbox / Image / Rectangle / "
            "Subreport / Chart / etc.), top, left, width, height. Use "
            "before set_body_item_position / set_body_item_size when you "
            "don't already know what's in the body."
        ),
        input_schema=_PATH_ONLY_SCHEMA,
        handler=reader.list_body_items,
    )
    server.register_tool(
        name="list_header_items",
        description="Same shape as list_body_items but for <PageHeader>.",
        input_schema=_PATH_ONLY_SCHEMA,
        handler=reader.list_header_items,
    )
    server.register_tool(
        name="list_footer_items",
        description="Same shape as list_body_items but for <PageFooter>.",
        input_schema=_PATH_ONLY_SCHEMA,
        handler=reader.list_footer_items,
    )
    server.register_tool(
        name="get_textbox",
        description=(
            "Return effective state of a named Textbox: position, size, "
            "Visibility, CanGrow, CanShrink, plus a nested style dict that "
            "mirrors set_textbox_style's routing — "
            "{box: {BackgroundColor, VerticalAlign, padding, ...}, "
            "border: {Style, Color, Width}, paragraph: {TextAlign}, "
            "run: {FontFamily, FontSize, FontWeight, Color, Format, ...}}. "
            "Empty branches are dropped. runs[] entries each carry their own "
            "per-run style. Searches the entire report; tablix-cell textboxes "
            "have None for top/left/width/height. Top-level positioned items "
            "with a missing <Top> or <Left> coerce to '0in' (RDL default)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=reader.get_textbox,
    )
    server.register_tool(
        name="get_image",
        description=(
            "Return effective state of a named Image: position, size, "
            "Source (External / Embedded / Database), Value, Sizing "
            "(AutoSize / Fit / FitProportional / Clip), MIMEType, Style, "
            "Visibility."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=reader.get_image,
    )
    server.register_tool(
        name="get_rectangle",
        description=(
            "Return effective state of a named Rectangle: position, size, "
            "names of contained ReportItems, Style, Visibility."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=reader.get_rectangle,
    )

    # ---- snapshot (v0.2 commit 14) ----------------------------------------
    server.register_tool(
        name="backup_report",
        description=(
            "Copy the report to <path>.bak.<UTC-timestamp>. Original is "
            "untouched. Cheap explicit checkpoint to call before a "
            "destructive batch (remove_*, rename_parameter, etc.). Returns "
            "the backup path. Set PBIRB_MCP_AUTO_BACKUP=1 to opt into "
            "automatic backups before destructive ops (off by default)."
        ),
        input_schema=_PATH_ONLY_SCHEMA,
        handler=snapshot.backup_report,
    )

    # ---- parameter CRUD (v0.2 commits 15-19) ------------------------------
    server.register_tool(
        name="set_parameter_prompt",
        description=(
            "Write the <Prompt> text on a ReportParameter. Empty string "
            "clears the <Prompt> element entirely; pass a single space ' ' "
            "for blank-but-present."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["path", "name", "prompt"],
            "additionalProperties": False,
        },
        handler=parameters.set_parameter_prompt,
    )
    server.register_tool(
        name="set_parameter_type",
        description=(
            "Set <DataType> on a ReportParameter. type ∈ {Boolean, "
            "DateTime, Integer, Float, String}. Rejects with ValueError "
            "if any existing literal default value would be incompatible "
            "with the new type — fix defaults first via "
            "set_parameter_default_values, then retry."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": ["Boolean", "DateTime", "Integer", "Float", "String"],
                },
            },
            "required": ["path", "name", "type"],
            "additionalProperties": False,
        },
        handler=parameters.set_parameter_type,
    )
    server.register_tool(
        name="add_parameter",
        description=(
            "Create a new ReportParameter with a minimal valid declaration. "
            "Appends to <ReportParameters> (creating it if absent). Pair "
            "with set_parameter_available_values / "
            "set_parameter_default_values afterwards for value lists. "
            "Booleans are only emitted when an explicit value is supplied."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": ["Boolean", "DateTime", "Integer", "Float", "String"],
                },
                "prompt": {"type": ["string", "null"]},
                "allow_null": {"type": ["boolean", "null"]},
                "allow_blank": {"type": ["boolean", "null"]},
                "multi_value": {"type": ["boolean", "null"]},
                "hidden": {"type": ["boolean", "null"]},
            },
            "required": ["path", "name", "type"],
            "additionalProperties": False,
        },
        handler=parameters.add_parameter,
    )
    server.register_tool(
        name="remove_parameter",
        description=(
            "Remove a ReportParameter by name. Refuses (lists offending "
            "locators) if the parameter is still referenced anywhere in "
            "the report by Parameters!<name>.Value or .Label. Pass "
            "force=True to remove anyway."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=parameters.remove_parameter,
    )
    server.register_tool(
        name="rename_parameter",
        description=(
            "Rename a ReportParameter and rewrite every textual occurrence "
            "of Parameters!<old_name>.Value / .Label across the entire "
            "report. Case-sensitive. Atomic: collects all matches first, "
            "then commits. Errors if new_name already exists or equals "
            "old_name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_name": {"type": "string"},
                "new_name": {"type": "string"},
            },
            "required": ["path", "old_name", "new_name"],
            "additionalProperties": False,
        },
        handler=parameters.rename_parameter,
    )

    server.register_tool(
        name="set_detail_row_visibility",
        description=(
            "Set <Visibility> on the tablix's Details group, optionally with "
            "a ToggleItem textbox name. Use to hide detail rows by expression "
            "without restructuring the row hierarchy."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "expression": {"type": "string"},
                "toggle_textbox": {"type": ["string", "null"]},
            },
            "required": ["path", "tablix_name", "expression"],
            "additionalProperties": False,
        },
        handler=tablix.set_detail_row_visibility,
    )
    server.register_tool(
        name="set_row_height",
        description=(
            "Set the Height of the Nth body row (0-indexed) in a tablix. "
            "Accepts any RDL size string ('0.25in', '1cm', '12pt')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "row_index": {"type": "integer", "minimum": 0},
                "height": {"type": "string"},
            },
            "required": ["path", "tablix_name", "row_index", "height"],
            "additionalProperties": False,
        },
        handler=tablix.set_row_height,
    )

    server.register_tool(
        name="remove_tablix_filter",
        description=(
            "Remove a filter by index. Filters are anonymous in RDL, so use "
            "list_tablix_filters first to find the right index. Removing the "
            "last filter also drops the empty <Filters/> block."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "filter_index": {"type": "integer", "minimum": 0},
            },
            "required": ["path", "tablix_name", "filter_index"],
            "additionalProperties": False,
        },
        handler=tablix.remove_tablix_filter,
    )

    server.register_tool(
        name="set_page_setup",
        description=(
            "Update <Page> dimensions, margins, and column count on the "
            "first ReportSection. All fields are optional — only what's "
            "passed gets written. columns=1 strips the <Columns/> element."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "page_height": {"type": ["string", "null"]},
                "page_width": {"type": ["string", "null"]},
                "margin_top": {"type": ["string", "null"]},
                "margin_bottom": {"type": ["string", "null"]},
                "margin_left": {"type": ["string", "null"]},
                "margin_right": {"type": ["string", "null"]},
                "columns": {"type": ["integer", "null"], "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=page.set_page_setup,
    )
    server.register_tool(
        name="set_page_orientation",
        description=(
            "Set page orientation by swapping PageHeight and PageWidth when "
            "the current orientation doesn't match the requested one. "
            "Idempotent. Accepts 'Portrait' or 'Landscape'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "orientation": {
                    "type": "string",
                    "enum": ["Portrait", "Landscape"],
                },
            },
            "required": ["path", "orientation"],
            "additionalProperties": False,
        },
        handler=page.set_page_orientation,
    )

    _SECTION_FLAGS_SCHEMA = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "height": {"type": ["string", "null"]},
            "print_on_first_page": {"type": ["boolean", "null"]},
            "print_on_last_page": {"type": ["boolean", "null"]},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    _TEXTBOX_SCHEMA = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string"},
            "text": {
                "type": "string",
                "description": "Static text or RDL expression (=...).",
            },
            "top": {"type": "string"},
            "left": {"type": "string"},
            "width": {"type": "string"},
            "height": {"type": "string"},
        },
        "required": ["path", "name", "text", "top", "left", "width", "height"],
        "additionalProperties": False,
    }
    _IMAGE_SCHEMA = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string"},
            "image_source": {
                "type": "string",
                "enum": ["External", "Embedded", "Database"],
            },
            "value": {
                "type": "string",
                "description": (
                    "URL for External, embedded-image name for Embedded, "
                    "or =Fields!X.Value for Database."
                ),
            },
            "top": {"type": "string"},
            "left": {"type": "string"},
            "width": {"type": "string"},
            "height": {"type": "string"},
        },
        "required": [
            "path",
            "name",
            "image_source",
            "value",
            "top",
            "left",
            "width",
            "height",
        ],
        "additionalProperties": False,
    }
    _NAMED_REMOVE_SCHEMA = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["path", "name"],
        "additionalProperties": False,
    }

    server.register_tool(
        name="set_page_header",
        description=(
            "Create or update <PageHeader>: height plus PrintOnFirstPage / "
            "PrintOnLastPage flags. All fields optional — only what's "
            "passed gets written."
        ),
        input_schema=_SECTION_FLAGS_SCHEMA,
        handler=header_footer.set_page_header,
    )
    server.register_tool(
        name="set_page_footer",
        description="Same as set_page_header, for <PageFooter>.",
        input_schema=_SECTION_FLAGS_SCHEMA,
        handler=header_footer.set_page_footer,
    )
    server.register_tool(
        name="add_header_textbox",
        description=(
            "Add a Textbox to <PageHeader>/<ReportItems>. text accepts "
            "static strings or RDL expressions (=Parameters!DateFrom.Value)."
        ),
        input_schema=_TEXTBOX_SCHEMA,
        handler=header_footer.add_header_textbox,
    )
    server.register_tool(
        name="add_footer_textbox",
        description="Same as add_header_textbox, for <PageFooter>.",
        input_schema=_TEXTBOX_SCHEMA,
        handler=header_footer.add_footer_textbox,
    )
    server.register_tool(
        name="add_header_image",
        description=(
            "Add an Image to <PageHeader>/<ReportItems>. image_source is "
            "External (URL in value), Embedded (EmbeddedImage Name in value), "
            "or Database (=Fields!Photo.Value-style expression)."
        ),
        input_schema=_IMAGE_SCHEMA,
        handler=header_footer.add_header_image,
    )
    server.register_tool(
        name="add_footer_image",
        description="Same as add_header_image, for <PageFooter>.",
        input_schema=_IMAGE_SCHEMA,
        handler=header_footer.add_footer_image,
    )
    server.register_tool(
        name="remove_header_item",
        description=(
            "Remove a named Textbox or Image from <PageHeader>/<ReportItems>. "
            "Empties the ReportItems block when the last item leaves."
        ),
        input_schema=_NAMED_REMOVE_SCHEMA,
        handler=header_footer.remove_header_item,
    )
    server.register_tool(
        name="remove_footer_item",
        description="Same as remove_header_item, for <PageFooter>.",
        input_schema=_NAMED_REMOVE_SCHEMA,
        handler=header_footer.remove_footer_item,
    )

    server.register_tool(
        name="set_textbox_style",
        description=(
            "Set styling on a named Textbox. Properties route to the right "
            "nested Style node automatically: background_color, "
            "border_*, vertical_align go on Textbox/Style; text_align on "
            "Paragraph/Style; font_*, color, format on TextRun/Style. All "
            "fields optional — only what's passed gets written. Cell-level "
            "styling: every tablix cell is a Textbox with a unique name, so "
            "use this tool with the cell's textbox name (e.g. 'HeaderAmount')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "textbox_name": {"type": "string"},
                "font_family": {"type": ["string", "null"]},
                "font_size": {
                    "type": ["string", "null"],
                    "description": "RDL size, e.g. '11pt'.",
                },
                "font_weight": {
                    "type": ["string", "null"],
                    "description": "Normal | Bold | Lighter | ... or numeric.",
                },
                "color": {
                    "type": ["string", "null"],
                    "description": "Text color — '#RRGGBB' or named.",
                },
                "background_color": {"type": ["string", "null"]},
                "border_style": {
                    "type": ["string", "null"],
                    "description": "None | Solid | Dotted | Dashed | Double.",
                },
                "border_color": {"type": ["string", "null"]},
                "border_width": {"type": ["string", "null"]},
                "text_align": {
                    "type": ["string", "null"],
                    "description": "Left | Center | Right | Justify | General.",
                },
                "vertical_align": {
                    "type": ["string", "null"],
                    "description": "Top | Middle | Bottom.",
                },
                "format": {
                    "type": ["string", "null"],
                    "description": "Number/date format (e.g. '#,0.00', 'C2', 'd').",
                },
            },
            "required": ["path", "textbox_name"],
            "additionalProperties": False,
        },
        handler=styling.set_textbox_style,
    )

    _BODY_TEXTBOX_SCHEMA = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string"},
            "text": {
                "type": "string",
                "description": "Static text or RDL expression (=...).",
            },
            "top": {"type": "string"},
            "left": {"type": "string"},
            "width": {"type": "string"},
            "height": {"type": "string"},
        },
        "required": ["path", "name", "text", "top", "left", "width", "height"],
        "additionalProperties": False,
    }
    _BODY_IMAGE_SCHEMA = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string"},
            "image_source": {
                "type": "string",
                "enum": ["External", "Embedded", "Database"],
            },
            "value": {"type": "string"},
            "top": {"type": "string"},
            "left": {"type": "string"},
            "width": {"type": "string"},
            "height": {"type": "string"},
        },
        "required": [
            "path",
            "name",
            "image_source",
            "value",
            "top",
            "left",
            "width",
            "height",
        ],
        "additionalProperties": False,
    }
    server.register_tool(
        name="add_body_textbox",
        description=(
            "Add a Textbox to <Body>/<ReportItems>. text accepts static "
            "strings or RDL expressions (e.g. =Globals!ReportName). "
            "Coexists with the existing tablix; rejects names already in use."
        ),
        input_schema=_BODY_TEXTBOX_SCHEMA,
        handler=body.add_body_textbox,
    )
    server.register_tool(
        name="add_body_image",
        description=(
            "Add an Image to <Body>/<ReportItems>. image_source: "
            "External (URL), Embedded (EmbeddedImage Name), Database "
            "(=Fields!Photo.Value)."
        ),
        input_schema=_BODY_IMAGE_SCHEMA,
        handler=body.add_body_image,
    )
    server.register_tool(
        name="remove_body_item",
        description=(
            "Remove a named item (Textbox, Image, or Tablix) from "
            "<Body>/<ReportItems>. Destructive but explicit — raises if "
            "the name doesn't match anything."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=body.remove_body_item,
    )

    server.register_tool(
        name="insert_tablix_from_template",
        description=(
            "Build and append a basic Tablix to <Body>/<ReportItems>. One "
            "column per name in `columns`; header row gets the column "
            "name as a static label, detail row binds to "
            "=Fields!<column>.Value. dataset_name must already exist. "
            "`width` is the tablix outer width — each column defaults to "
            "1in regardless, so for a 3-column tablix the columns sum to "
            "3in even if you pass width=10cm. Resize columns afterwards "
            "via direct edits or future column-width tools."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "dataset_name": {"type": "string"},
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "top": {"type": "string"},
                "left": {"type": "string"},
                "width": {"type": "string"},
                "height": {"type": "string"},
            },
            "required": [
                "path",
                "name",
                "dataset_name",
                "columns",
                "top",
                "left",
                "width",
                "height",
            ],
            "additionalProperties": False,
        },
        handler=templates.insert_tablix_from_template,
    )
    server.register_tool(
        name="insert_chart_from_template",
        description=(
            "Build and append a basic Column chart to <Body>/<ReportItems>. "
            "Single category axis grouped by category_field; single Y series "
            "Sum(Fields!<value_field>.Value). dataset_name must already exist. "
            "Change <Type> post-insert (e.g. to Bar / Line / Pie) by "
            "editing the chart directly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "dataset_name": {"type": "string"},
                "category_field": {"type": "string"},
                "value_field": {"type": "string"},
                "top": {"type": "string"},
                "left": {"type": "string"},
                "width": {"type": "string"},
                "height": {"type": "string"},
            },
            "required": [
                "path",
                "name",
                "dataset_name",
                "category_field",
                "value_field",
                "top",
                "left",
                "width",
                "height",
            ],
            "additionalProperties": False,
        },
        handler=templates.insert_chart_from_template,
    )

    _STATIC_VALUE_ITEM = {
        "oneOf": [
            {"type": "string"},
            {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["value"],
                "additionalProperties": False,
            },
        ]
    }
    server.register_tool(
        name="set_parameter_available_values",
        description=(
            "Set <ValidValues> on a report parameter. source='static' "
            "writes a list of <ParameterValue> entries (each entry can "
            "be a string or {value, label} dict); source='query' writes "
            "a <DataSetReference> to a lookup dataset. Replaces any "
            "existing <ValidValues> block."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "source": {"type": "string", "enum": ["static", "query"]},
                "static_values": {
                    "type": ["array", "null"],
                    "items": _STATIC_VALUE_ITEM,
                },
                "query_dataset": {"type": ["string", "null"]},
                "query_value_field": {"type": ["string", "null"]},
                "query_label_field": {"type": ["string", "null"]},
            },
            "required": ["path", "name", "source"],
            "additionalProperties": False,
        },
        handler=parameters.set_parameter_available_values,
    )
    server.register_tool(
        name="set_parameter_default_values",
        description=(
            "Set <DefaultValue> on a report parameter. source='static' "
            "writes a <Values> list of expressions; source='query' writes "
            "a <DataSetReference> with ValueField only (no LabelField — "
            "defaults are values, not display strings). Replaces existing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "source": {"type": "string", "enum": ["static", "query"]},
                "static_values": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "query_dataset": {"type": ["string", "null"]},
                "query_value_field": {"type": ["string", "null"]},
            },
            "required": ["path", "name", "source"],
            "additionalProperties": False,
        },
        handler=parameters.set_parameter_default_values,
    )

    server.register_tool(
        name="update_parameter_advanced",
        description=(
            "Toggle the four boolean flags on a report parameter: "
            "multi_value, hidden, allow_null (writes <Nullable>), "
            "allow_blank. Each is independently optional. With no flags "
            "passed it's a no-op. Cascading parameters are NOT a flag — "
            "use set_parameter_available_values(source='query') + "
            "add_query_parameter on the lookup dataset to wire a "
            "dependency on another parameter."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "multi_value": {"type": ["boolean", "null"]},
                "hidden": {"type": ["boolean", "null"]},
                "allow_null": {"type": ["boolean", "null"]},
                "allow_blank": {"type": ["boolean", "null"]},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=parameters.update_parameter_advanced,
    )

    server.register_tool(
        name="add_embedded_image",
        description=(
            "Read a real image file off disk, base64-encode it, and store "
            "it under <EmbeddedImages>. Reference it later with "
            "image_source='Embedded' + value=<name>. Supported MIME types: "
            "image/bmp, image/gif, image/jpeg, image/png, image/x-png. "
            "The file's magic bytes are sniffed and must match mime_type — "
            "claiming PNG bytes as image/jpeg is rejected here rather than "
            "letting Report Builder fail at preview time."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "mime_type": {
                    "type": "string",
                    "enum": [
                        "image/bmp",
                        "image/gif",
                        "image/jpeg",
                        "image/png",
                        "image/x-png",
                    ],
                },
                "image_path": {
                    "type": "string",
                    "description": "Filesystem path to the source image.",
                },
            },
            "required": ["path", "name", "mime_type", "image_path"],
            "additionalProperties": False,
        },
        handler=embedded_images.add_embedded_image,
    )
    server.register_tool(
        name="list_embedded_images",
        description=(
            "List embedded images in the report by name and MIME type. "
            "Returns an empty list when <EmbeddedImages> is absent."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=embedded_images.list_embedded_images,
    )
    server.register_tool(
        name="remove_embedded_image",
        description=(
            "Remove a named embedded image. Refuses (lists offending Image "
            "elements) when any <Image Source=\"Embedded\"><Value>=name> "
            "still references it; pass force=True to remove anyway and "
            "accept the dangling references. Drops the empty "
            "<EmbeddedImages/> block when removing the last entry."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=embedded_images.remove_embedded_image,
    )

    server.register_tool(
        name="set_alternating_row_color",
        description=(
            "Zebra-stripe a tablix's detail row by writing "
            "BackgroundColor=IIf(RowNumber(Nothing) Mod 2, color_a, color_b) "
            "on every detail cell's Textbox. Walks the row hierarchy to "
            "find the Details leaf — works after add_row_group nests the "
            "structure deeper. Replaces any existing BackgroundColor."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "color_a": {
                    "type": "string",
                    "description": "Odd-row color, e.g. '#F2F2F2'.",
                },
                "color_b": {
                    "type": "string",
                    "description": "Even-row color, e.g. '#FFFFFF'.",
                },
            },
            "required": ["path", "tablix_name", "color_a", "color_b"],
            "additionalProperties": False,
        },
        handler=styling.set_alternating_row_color,
    )
    server.register_tool(
        name="set_conditional_row_color",
        description=(
            "Color every cell of a tablix's detail row based on the value "
            "of one of its fields. Builds a Switch(...) expression mapping "
            "field values to colors and writes it as BackgroundColor on "
            "every detail cell. value_expression is the field reference "
            "(e.g. 'Fields!Status.Value' — a leading '=' is accepted). "
            "color_map is an ordered dict of value->color (e.g. "
            "{\"Red\":\"#FF0000\",\"Yellow\":\"#FFFF00\"}); first match "
            "wins. Unmatched values fall back to default_color "
            "(default 'Transparent'). When case_sensitive is False "
            "(default), wraps the field reference in UCase() and uppercases "
            "the keys for case-insensitive matching. Walks the row "
            "hierarchy to find the Details leaf — works after add_row_group "
            "nests the structure. Replaces any existing BackgroundColor."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "value_expression": {
                    "type": "string",
                    "description": (
                        "Field reference to switch on, e.g. "
                        "'Fields!Status.Value'. Leading '=' optional."
                    ),
                },
                "color_map": {
                    "type": "object",
                    "description": (
                        "Ordered map of expected values to color strings. "
                        "First match in declaration order wins."
                    ),
                    "additionalProperties": {"type": "string"},
                    "minProperties": 1,
                },
                "default_color": {
                    "type": "string",
                    "default": "Transparent",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": ["path", "tablix_name", "value_expression", "color_map"],
            "additionalProperties": False,
        },
        handler=styling.set_conditional_row_color,
    )

    server.register_tool(
        name="set_element_visibility",
        description=(
            "Set <Visibility> on any named ReportItem (Tablix, Textbox, "
            "Image, Rectangle, Subreport, Chart). For group-level "
            "visibility use set_group_visibility; for detail-row use "
            "set_detail_row_visibility. toggle_textbox optionally points "
            "at a textbox name that toggles expand/collapse."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "element_name": {"type": "string"},
                "hidden_expression": {"type": "string"},
                "toggle_textbox": {"type": ["string", "null"]},
            },
            "required": ["path", "element_name", "hidden_expression"],
            "additionalProperties": False,
        },
        handler=visibility.set_element_visibility,
    )


__all__ = ["register_all_tools"]
