"""Tool registry — wires ops modules into the JSON-RPC server.

Each ops module exposes plain Python functions; this module describes their
JSON Schema and registers them with an :class:`MCPServer`. Adding a new tool
means appending one entry here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pbirb_mcp.core import chart_types as _chart_types
from pbirb_mcp.core import columns as _columns
from pbirb_mcp.ops import (
    actions,
    body,
    chart,
    clone,
    dataset,
    datasource,
    embedded_images,
    header_footer,
    images,
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
    validate,
    visibility,
)
from pbirb_mcp.ops import containers as _containers
from pbirb_mcp.ops import dry_run as _dry_run
from pbirb_mcp.ops import escape as _escape
from pbirb_mcp.ops import expressions as _expressions
from pbirb_mcp.ops import layout as _layout
from pbirb_mcp.ops import layout_helpers as _layout_helpers
from pbirb_mcp.ops import lint as _lint
from pbirb_mcp.ops import rename as _rename
from pbirb_mcp.ops import scratch as _scratch
from pbirb_mcp.ops import transactions as _transactions
from pbirb_mcp.ops.response_format import RESPONSE_FORMAT_PARAM, with_response_format

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


# describe_report / get_datasets / get_parameters / get_tablixes (#55).
_READER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Absolute path to the .rdl file to read.",
        },
        "response_format": RESPONSE_FORMAT_PARAM,
    },
    "required": ["path"],
    "additionalProperties": False,
}


# list_body_items / list_header_items / list_footer_items (#52).
_LIST_ITEMS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Absolute path to the .rdl file to read.",
        },
        "recursive": {
            "type": "boolean",
            "default": False,
            "description": "Also list the items inside Rectangles (with parent set).",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


# set_group_sort / set_column_group_sort (#66): an expression string, or
# {value, direction}. A string re-sent for an existing key keeps its Direction.
_SORT_EXPRESSIONS_SCHEMA: dict = {
    "type": "array",
    "items": {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "minLength": 1},
                    "direction": {"type": "string", "enum": ["Ascending", "Descending"]},
                },
                "required": ["value"],
                "additionalProperties": False,
            },
            {"type": "string", "minLength": 1},
        ]
    },
    "description": (
        "Sort keys in order: '=expr' or {value, direction: Ascending|Descending}. "
        "A plain string for an existing key keeps its direction; [] removes the sort."
    ),
}


# `container` of the body add tools (#83).
_CONTAINER_PARAM: dict = {
    "type": ["string", "null"],
    "description": (
        "Rectangle in the body to add inside (a List's <name>_Rect too); "
        "top/left are then relative to it. Omit for the body."
    ),
}


# Image/MIMEType for Database images (#79): add_*_image, set_image_source.
_IMAGE_MIME_TYPE_SCHEMA: dict = {
    "type": "string",
    "description": (
        "Database images only (required there): image/png, image/jpeg, image/gif, "
        "image/bmp, image/x-png, or an =expression."
    ),
}


def register_all_tools(server: MCPServer) -> None:
    server.register_tool(
        name="describe_report",
        description=(
            "Inventory of a report: data sources, datasets, parameters, tablixes "
            "(rows, columns, groups, column_details with each column's header "
            "text, cell textbox names and bound fields), charts, subreports, "
            "nested items, body and page setup (with defaults and printable "
            "width). Call it first when planning edits."
        ),
        input_schema=_READER_SCHEMA,
        handler=with_response_format("describe_report", reader.describe_report),
    )
    server.register_tool(
        name="get_datasets",
        description=(
            "Read every dataset: DAX command text, fields, query parameters and dataset filters."
        ),
        input_schema=_READER_SCHEMA,
        handler=with_response_format("get_datasets", reader.get_datasets),
    )
    server.register_tool(
        name="get_parameters",
        description=(
            "Read the report parameters: name, data type, prompt, flags, default "
            "values, valid values (static or query) and parameter-pane cell."
        ),
        input_schema=_READER_SCHEMA,
        handler=with_response_format("get_parameters", reader.get_parameters),
    )
    server.register_tool(
        name="get_tablixes",
        description=(
            "Read every tablix (table/matrix): columns, rows, cells with spans, "
            "header and corner cells, the row/column member tree (groups, sorts, "
            "visibility) with row roles, filters and visibility. Read it before "
            "editing a tablix."
        ),
        input_schema=_READER_SCHEMA,
        handler=with_response_format("get_tablixes", reader.get_tablixes),
    )

    server.register_tool(
        name="update_dataset_query",
        description=(
            "Replace a dataset's DAX query (CommandText); the DAX is taken "
            "verbatim, empty is refused. alias_strategy='preserve_field_names' "
            "keeps every Field name and re-aims DataFields at the new columns "
            "where unambiguous (the rest are reported in warnings). dry_run=true "
            "returns the result without saving."
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
                "alias_strategy": {
                    "type": ["string", "null"],
                    "enum": [None, "preserve_field_names"],
                    "description": (
                        "When 'preserve_field_names', re-aim <DataField> "
                        "cells at the new DAX columns (identity first, "
                        "then old-DAX slot position)."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Compute the result (and mapped list) without saving.",
                },
                "designer_state": {
                    "type": "string",
                    "enum": ["sync", "drop"],
                    "default": "sync",
                    "description": (
                        "Report Builder's Query Designer state: 'sync' rewrites its "
                        "Statement to the new DAX; 'drop' removes rd:DesignerState so "
                        "Report Builder opens the query in its text editor."
                    ),
                },
            },
            "required": ["path", "dataset_name", "dax_body"],
            "additionalProperties": False,
        },
        handler=dataset.update_dataset_query,
        mutating=True,
    )
    server.register_tool(
        name="add_query_parameter",
        description=(
            "Add a query parameter to a dataset, e.g. to pass "
            "=Parameters!DateFrom.Value into the DAX. For Power BI (PBIDATASET) "
            "datasets write @Name in the DAX but Name here: a leading @ is "
            "stripped (normalised: true); force_at_prefix=true keeps it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "name": {"type": "string"},
                "value_expression": {"type": "string"},
                "force_at_prefix": {"type": "boolean", "default": False},
            },
            "required": ["path", "dataset_name", "name", "value_expression"],
            "additionalProperties": False,
        },
        handler=dataset.add_query_parameter,
        mutating=True,
    )
    server.register_tool(
        name="update_query_parameter",
        description=(
            "Change the value expression of an existing query parameter. Finds it "
            "by Name with or without a leading @ (same PBIDATASET rule as "
            "add_query_parameter)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "name": {"type": "string"},
                "value_expression": {"type": "string"},
                "force_at_prefix": {"type": "boolean", "default": False},
            },
            "required": ["path", "dataset_name", "name", "value_expression"],
            "additionalProperties": False,
        },
        handler=dataset.update_query_parameter,
        mutating=True,
    )
    server.register_tool(
        name="set_datasource_connection",
        description=(
            "Point a data source at another Power BI workspace/semantic model. "
            "Keeps the source's provider (PBIDATASET stays PBIDATASET, SQL stays "
            "XMLA) and updates its workspace/model labels; provider converts. "
            "catalog_id sets a PBIDATASET source's Initial Catalog (the model id). "
            "Returns before/after."
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
                    "description": "PBI semantic model display name.",
                },
                "integrated_security": {
                    "type": "boolean",
                    "description": "Default true. False omits the element.",
                },
                "provider": {
                    "type": ["string", "null"],
                    "enum": ["sql", "pbidataset", None],
                    "default": None,
                    "description": "Omit to keep the source's current provider.",
                },
                "catalog_id": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Initial Catalog; PBIDATASET: the model's internal id.",
                },
                "identity_provider": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "PBIDATASET Identity Provider value.",
                },
            },
            "required": ["path", "name", "workspace_url", "dataset_name"],
            "additionalProperties": False,
        },
        handler=datasource.set_datasource_connection,
        mutating=True,
    )
    server.register_tool(
        name="list_data_sources",
        description=(
            "List every data source with its connection details: provider, "
            "connect string, integrated security, shared reference, security "
            "type, id."
        ),
        input_schema=_PATH_ONLY_SCHEMA,
        handler=datasource.list_data_sources,
    )
    server.register_tool(
        name="get_data_source",
        description=(
            "Read one data source by name: provider, connect string, integrated "
            "security, shared reference, security type, id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "response_format": RESPONSE_FORMAT_PARAM,
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=with_response_format("get_data_source", datasource.get_data_source),
    )
    server.register_tool(
        name="add_data_source",
        description=(
            "Add a data source for a Power BI workspace/semantic model. "
            "workspace_url takes a workspace name or powerbi:// URL; gets a fresh "
            "DataSourceID; refuses a duplicate name. provider='sql' (default) "
            "writes the XMLA (DataProvider=SQL) shape, 'pbidataset' the Report "
            "Builder (PBIDATASET) shape; catalog_id is its Initial Catalog (the "
            "model id)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "workspace_url": {"type": "string"},
                "dataset_name": {"type": "string"},
                "integrated_security": {"type": "boolean", "default": True},
                "provider": {
                    "type": "string",
                    "enum": ["sql", "pbidataset"],
                    "default": "sql",
                },
                "catalog_id": {"type": ["string", "null"], "default": None},
                "identity_provider": {"type": ["string", "null"], "default": None},
            },
            "required": ["path", "name", "workspace_url", "dataset_name"],
            "additionalProperties": False,
        },
        handler=datasource.add_data_source,
        mutating=True,
    )
    server.register_tool(
        name="remove_data_source",
        description=(
            "Remove a data source by name. Refuses while a dataset still uses it "
            "(lists them); force=true removes anyway. Never removes the last one."
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
        handler=datasource.remove_data_source,
        mutating=True,
    )
    server.register_tool(
        name="rename_data_source",
        description=(
            "Rename a data source and update every dataset query that names it. "
            "Refuses if new_name is taken."
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
        handler=datasource.rename_data_source,
        mutating=True,
    )
    server.register_tool(
        name="list_dataset_filters",
        description=(
            "List a dataset's filters in order: expression, operator, values. The "
            "list index is the handle for remove_dataset_filter. Dataset filters "
            "apply to every item bound to the dataset (see list_tablix_filters "
            "for one tablix)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
            },
            "required": ["path", "dataset_name"],
            "additionalProperties": False,
        },
        handler=dataset.list_dataset_filters,
    )
    server.register_tool(
        name="add_dataset_filter",
        description=(
            "Add a filter to a dataset (applies to every tablix/chart using it). "
            "Operators: Equal, NotEqual, GreaterThan, LessThan, "
            "GreaterThanOrEqual, LessThanOrEqual, Like, In, Between, TopN, "
            "BottomN, TopPercent, BottomPercent; values is a non-empty list. "
            "field_format wraps the expression in Format() for string parameters. "
            "Returns the filter's index."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "expression": {
                    "type": "string",
                    "description": "FilterExpression — usually a Fields! reference.",
                },
                "operator": {"type": "string"},
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "field_format": {
                    "type": "string",
                    "description": (
                        "Optional format string (e.g. 'MMM, yyyy') wrapping "
                        "the expression as Format(<body>, '<fmt>')."
                    ),
                },
            },
            "required": [
                "path",
                "dataset_name",
                "expression",
                "operator",
                "values",
            ],
            "additionalProperties": False,
        },
        handler=dataset.add_dataset_filter,
        mutating=True,
    )
    server.register_tool(
        name="remove_dataset_filter",
        description=("Remove a dataset filter by its index from list_dataset_filters."),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "filter_index": {"type": "integer", "minimum": 0},
            },
            "required": ["path", "dataset_name", "filter_index"],
            "additionalProperties": False,
        },
        handler=dataset.remove_dataset_filter,
        mutating=True,
    )
    server.register_tool(
        name="get_dataset",
        description=(
            "Read one dataset by name: data source, DAX command text, fields "
            "(data_field / value), query parameters and filters."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "response_format": RESPONSE_FORMAT_PARAM,
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=with_response_format("get_dataset", dataset.get_dataset),
    )
    server.register_tool(
        name="add_calculated_field",
        description=(
            "Add a calculated field (an expression, e.g. =Fields!Amount.Value * "
            "Fields!Qty.Value) to a dataset, usable as Fields!Name.Value. Refuses "
            "a duplicate name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "field_name": {"type": "string"},
                "expression": {
                    "type": "string",
                    "description": "RDL expression, e.g. '=Fields!Amount.Value * Fields!Quantity.Value'.",
                },
            },
            "required": ["path", "dataset_name", "field_name", "expression"],
            "additionalProperties": False,
        },
        handler=dataset.add_calculated_field,
        mutating=True,
    )
    server.register_tool(
        name="remove_calculated_field",
        description=(
            "Remove a calculated field from a dataset. Refuses a query-bound "
            "field (use remove_dataset_field) and a field still referenced "
            "(expressions in its dataset's scope, parameter ValueField/LabelField) "
            "unless force=true."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "field_name": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "description": (
                        "Default false: refuse if the field is still "
                        "referenced. true: delete anyway "
                        "(prefer fixing the references first)."
                    ),
                },
            },
            "required": ["path", "dataset_name", "field_name"],
            "additionalProperties": False,
        },
        handler=dataset.remove_calculated_field,
        mutating=True,
    )
    server.register_tool(
        name="remove_dataset_field",
        description=(
            "Remove a query-bound field (one with a DataField) from a dataset. "
            "Refuses a calculated field (use remove_calculated_field) and a field "
            "still referenced (expressions in its dataset's scope, parameter "
            "ValueField/LabelField) unless force=true. Use it to drop orphans "
            "refresh_dataset_fields reports."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "field_name": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "description": (
                        "Default false: refuse if the field is still "
                        "referenced anywhere. true: delete anyway "
                        "(prefer fixing the references first)."
                    ),
                },
            },
            "required": ["path", "dataset_name", "field_name"],
            "additionalProperties": False,
        },
        handler=dataset.remove_dataset_field,
        mutating=True,
    )
    server.register_tool(
        name="add_dataset_field",
        description=(
            "Declare a query-bound field on a dataset (DataField = the DAX "
            "column, optional type_name), e.g. after the query returns a new "
            "column. Refuses a duplicate name. For an expression field use "
            "add_calculated_field."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
                "field_name": {"type": "string"},
                "data_field": {
                    "type": "string",
                    "description": (
                        "Source column reference, e.g. 'Sales[ProductID]' "
                        "or '[Region]' (the form depends on DAX shape)."
                    ),
                },
                "type_name": {
                    "type": ["string", "null"],
                    "description": (
                        ".NET type, e.g. 'System.String', "
                        "'System.DateTime', 'System.Decimal'. Optional."
                    ),
                },
            },
            "required": ["path", "dataset_name", "field_name", "data_field"],
            "additionalProperties": False,
        },
        handler=dataset.add_dataset_field,
        mutating=True,
    )
    server.register_tool(
        name="refresh_dataset_fields",
        description=(
            "Refresh a dataset's fields from its DAX query, like Report Builder's "
            "Refresh Fields: adds a field per new result column (Report Builder "
            "naming) and lists orphans without removing them. Matches by "
            "DataField, not Name. When the DAX can't be analysed nothing is added "
            "and warnings say why."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dataset_name": {"type": "string"},
            },
            "required": ["path", "dataset_name"],
            "additionalProperties": False,
        },
        handler=dataset.refresh_dataset_fields,
        mutating=True,
    )
    server.register_tool(
        name="set_image_sizing",
        description=(
            "Set how an image fits its box: AutoSize (native size, box grows), "
            "Fit (stretch), FitProportional (keep aspect ratio) or Clip."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "image_name": {"type": "string"},
                "sizing": {
                    "type": "string",
                    "enum": ["AutoSize", "Fit", "FitProportional", "Clip"],
                },
            },
            "required": ["path", "image_name", "sizing"],
            "additionalProperties": False,
        },
        handler=images.set_image_sizing,
        mutating=True,
    )
    server.register_tool(
        name="set_image_source",
        description=(
            "Repoint an existing image without re-adding it: source Embedded "
            "(value = an embedded image's name; refused if missing), External "
            "(URL) or Database (=Fields! expression + required mime_type). "
            "MIMEType is kept only for Database."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "image_name": {"type": "string"},
                "source": {"type": "string", "enum": ["External", "Embedded", "Database"]},
                "value": {
                    "type": "string",
                    "description": (
                        "Embedded image name, URL, or =expression (required for Database)."
                    ),
                },
                "mime_type": _IMAGE_MIME_TYPE_SCHEMA,
                "embedded_name": {
                    "type": "string",
                    "description": "Deprecated: same as source='Embedded', value=<name>.",
                },
            },
            "required": ["path", "image_name"],
            "additionalProperties": False,
        },
        handler=images.set_image_source,
        mutating=True,
    )
    server.register_tool(
        name="set_column_width",
        description=(
            "Set the width of a tablix column. column is a 0-based index or the "
            "name of a textbox in the column; or pick it with column_ref {header "
            "| field}. width is an RDL size ('1.5in', '4cm')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "column": {
                    "oneOf": [
                        {"type": "integer", "minimum": 0},
                        {"type": "string"},
                    ],
                    "description": "0-based column index or textbox name in any column cell.",
                },
                "column_ref": _columns.COLUMN_REF_SCHEMA,
                "width": {"type": "string"},
            },
            "required": ["path", "tablix_name", "width"],
            "additionalProperties": False,
        },
        handler=tablix_columns.set_column_width,
        mutating=True,
    )
    server.register_tool(
        name="set_tablix_size",
        description=(
            "Set a tablix's overall Width and/or Height (RDL sizes), e.g. after "
            "adding rows. A width that disagrees with the column widths is refused "
            "unless distribute=true scales the columns to fit. Column widths are "
            "set_column_width."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "height": {"type": "string"},
                "width": {"type": "string"},
                "distribute": {"type": "boolean", "default": False},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=tablix.set_tablix_size,
        mutating=True,
    )
    server.register_tool(
        name="reorder_parameters",
        description=(
            "Reorder the report parameters (declaration order). names must list "
            "every parameter exactly once; refuses an order that puts a parameter "
            "before one its defaults / available values depend on. The pane keeps "
            "its cell positions unless reflow_layout=true."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "reflow_layout": {"type": "boolean", "default": False},
            },
            "required": ["path", "names"],
            "additionalProperties": False,
        },
        handler=parameters.reorder_parameters,
        mutating=True,
    )
    server.register_tool(
        name="set_parameter_layout",
        description=(
            "Lay out the parameter pane: a rows x columns grid with the "
            "parameters placed in parameter_order (each exactly once). Creates "
            "the layout if absent. See reorder_parameters (declaration order) and "
            "sync_parameter_layout (repair)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "rows": {"type": "integer", "minimum": 1},
                "columns": {"type": "integer", "minimum": 1},
                "parameter_order": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "required": ["path", "rows", "columns", "parameter_order"],
            "additionalProperties": False,
        },
        handler=parameters.set_parameter_layout,
        mutating=True,
    )
    server.register_tool(
        name="duplicate_report",
        description=(
            "Copy a report to a new file (save as). regenerate_ids=true (default) "
            "gives the copy fresh DataSourceID / ReportID GUIDs so Report Builder "
            "accepts both. Refuses an existing dst_path; the write is atomic."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "src_path": {"type": "string"},
                "dst_path": {"type": "string"},
                "regenerate_ids": {"type": "boolean", "default": True},
            },
            "required": ["src_path", "dst_path"],
            "additionalProperties": False,
        },
        handler=clone.duplicate_report,
        mutating=True,
    )
    server.register_tool(
        name="get_embedded_image_data",
        description=(
            "Read an embedded image's base64 data, MIME type and decoded size, "
            "e.g. to copy it into another report."
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
        handler=embedded_images.get_embedded_image_data,
    )

    # ---- validation (Phase 7) -------------------------------------------

    server.register_tool(
        name="validate_report",
        description=(
            "Validate a report: structural checks plus the bundled RDL 2016 XSD "
            "(xsd_used). Returns {valid, errors}; valid is false on any "
            "error-severity issue. For XSD + lint in one call use verify_report."
        ),
        input_schema=_PATH_ONLY_SCHEMA,
        handler=validate.validate_report,
    )

    server.register_tool(
        name="verify_report",
        description=(
            "Check a report is OK: validate_report (XSD) + lint_report in one "
            "call. valid is false on any error. With baseline_path (e.g. a "
            "backup) or inside a transaction, each issue is marked new or "
            "pre-existing. PBIRB_MCP_AUTO_VERIFY=1 runs it after every edit "
            "outside a transaction."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "baseline_path": {
                    "type": "string",
                    "description": (
                        "Optional earlier version of the report; issues "
                        "already present there are marked new: false."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=validate.verify_report,
    )

    server.register_tool(
        name="lint_report",
        description=(
            "Lint a report for the bugs Report Builder rejects or renders wrong "
            "(broken field references, multi-value comparisons, tablix/chart "
            f"structure, duplicate names, layout overflow, ... {len(_lint.ALL_RULES)} "
            "rules). Each issue has severity, rule, location and message; `rules` "
            "runs a subset."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "rules": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional subset of rule names; omitted or empty runs all rules."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_lint.lint_report,
    )

    server.register_tool(
        name="dry_run_edit",
        description=(
            "Preview edits without changing the report: runs {tool, args} ops on "
            "a temp copy and returns per-op results, a unified diff and a verify "
            "report. Ops omit `path`. Stops at the first failing op. summary=true "
            "leaves out the diff; max_diff_bytes caps it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["tool"],
                        "additionalProperties": False,
                    },
                    "description": "Sequence of tool calls to dispatch against the tempfile.",
                },
                "summary": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Omit the diff and op result_text; verify becomes "
                        "{valid, error_count, issue_count, issues_by_rule}."
                    ),
                },
                "max_diff_bytes": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 40000,
                    "description": "Cap on the returned diff's UTF-8 size (0 = no cap).",
                },
            },
            "required": ["path", "ops"],
            "additionalProperties": False,
        },
        handler=_dry_run.dry_run_edit,
    )

    # ---- v0.4 transactions (commit 10) -----------------------------------
    server.register_tool(
        name="start_editing_transaction",
        description=(
            "Start an editing transaction on a report: later calls with its "
            "transaction_id edit an in-memory copy, and nothing is saved until "
            "commit_editing_transaction (or discarded by cancel). Expires after "
            "PBIRB_MCP_TRANSACTION_TIMEOUT_S (default 600 s). One transaction per "
            "file."
        ),
        input_schema=_PATH_ONLY_SCHEMA,
        handler=_transactions.start_editing_transaction,
    )
    server.register_tool(
        name="commit_editing_transaction",
        description=(
            "Save a transaction's edits in one atomic write after verifying them "
            "(XSD + lint, new errors only). On errors nothing is saved and the "
            "transaction stays open to fix. Refuses if the file changed on disk "
            "meanwhile unless force=true."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "description": (
                        "Overwrite the file even if it changed on disk since "
                        "the transaction started. Default false."
                    ),
                },
            },
            "required": ["transaction_id"],
            "additionalProperties": False,
        },
        handler=_transactions.commit_editing_transaction,
    )
    server.register_tool(
        name="cancel_editing_transaction",
        description=("Discard a transaction's edits; the file on disk stays as it was."),
        input_schema={
            "type": "object",
            "properties": {"transaction_id": {"type": "string"}},
            "required": ["transaction_id"],
            "additionalProperties": False,
        },
        handler=_transactions.cancel_editing_transaction,
    )
    server.register_tool(
        name="apply_edits",
        description=(
            "Apply several edits atomically: runs {tool, args} ops in one "
            "transaction, verifies, and saves once. If any op fails or verify "
            "finds new errors nothing is saved. Ops omit `path`. Preview the same "
            "ops with dry_run_edit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["tool"],
                        "additionalProperties": False,
                    },
                    "description": "Ordered list of tool calls to apply atomically.",
                },
            },
            "required": ["path", "ops"],
            "additionalProperties": False,
        },
        handler=_transactions.apply_edits,
        mutating=True,
    )

    # ---- v0.4 scratch creation (commits 13 + 14) -------------------------
    server.register_tool(
        name="create_report",
        description=(
            "Create a new, valid, empty report at path (refuses an existing "
            "file). Page defaults to US Letter portrait, 1in margins; page_setup "
            "overrides sizes. datasource {name, workspace_url, dataset_name, ...} "
            "connects it to Power BI; omitted, a placeholder DataSource1 + "
            "DataSet1 is written. fields declares DataSet1's fields."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "page_setup": {
                    "type": ["object", "null"],
                    "additionalProperties": {"type": "string"},
                    "default": None,
                },
                "datasource": {
                    "type": ["object", "null"],
                    "default": None,
                },
                "fields": {
                    "type": ["array", "null"],
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "data_field": {"type": "string"},
                                    "type_name": {"type": "string"},
                                },
                                "required": ["name"],
                                "additionalProperties": False,
                            },
                        ]
                    },
                    "default": None,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_scratch.create_report,
        mutating=True,
    )

    server.register_tool(
        name="get_expression_reference",
        description=(
            "Cheat sheet of RDL expression patterns (globals, parameters, fields, "
            "aggregates, conditionals, strings, dates) with syntax and examples. "
            "Use it when writing an expression instead of guessing."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_expressions.get_expression_reference,
    )

    server.register_tool(
        name="count_where",
        description=(
            "Build a conditional count expression: =Sum(IIf(condition, 1, 0)). "
            "condition is an expression body without the leading '='."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "condition": {
                    "type": "string",
                    "description": (
                        "RDL expression body, e.g. 'Fields!Status.Value = \"Active\"'."
                    ),
                },
            },
            "required": ["condition"],
            "additionalProperties": False,
        },
        handler=_expressions.count_where,
    )

    server.register_tool(
        name="sum_where",
        description=(
            "Build a conditional sum expression: =Sum(IIf(condition, "
            "field_expression, 0)). Both arguments are expression bodies without "
            "'='."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "field_expression": {
                    "type": "string",
                    "description": ("Value to sum, e.g. 'Fields!Amount.Value'."),
                },
                "condition": {
                    "type": "string",
                    "description": (
                        "RDL expression body, e.g. 'Fields!Status.Value = \"Active\"'."
                    ),
                },
            },
            "required": ["field_expression", "condition"],
            "additionalProperties": False,
        },
        handler=_expressions.sum_where,
    )

    server.register_tool(
        name="iif_format",
        description=(
            "Build an =IIf(condition, true_value, false_value) expression, e.g. "
            "for conditional text or colours. Arguments are expression bodies "
            "without '='; quote string literals ('\"Yes\"')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "condition": {"type": "string"},
                "true_value": {"type": "string"},
                "false_value": {"type": "string"},
            },
            "required": ["condition", "true_value", "false_value"],
            "additionalProperties": False,
        },
        handler=_expressions.iif_format,
    )

    # ---- pagination (Phase 10) ------------------------------------------

    server.register_tool(
        name="set_group_page_break",
        description=(
            "Set a page break on a tablix group: None (remove), Start, End, StartAndEnd or Between."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "location": {
                    "type": "string",
                    "enum": list(_layout._VALID_BREAK_LOCATIONS),
                },
            },
            "required": ["path", "tablix_name", "group_name", "location"],
            "additionalProperties": False,
        },
        handler=_layout.set_group_page_break,
        mutating=True,
    )

    server.register_tool(
        name="set_repeat_on_new_page",
        description=(
            "Repeat a row on every page (RepeatOnNewPage on its member): "
            "member='header' for the column-header row, '<group>_header' for a "
            "group's header row, or group_name for a group's member; "
            "repeat=false removes it. For a table's header rows prefer "
            "set_repeat_header_rows."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "member": {
                    "oneOf": [
                        {"type": "integer", "minimum": 0},
                        {"type": "string"},
                    ],
                    "description": (
                        "A row instead of a group: body row index, or 'header', "
                        "'details', '<group>_header', '<group>_footer'."
                    ),
                },
                "repeat": {"type": "boolean"},
            },
            "required": ["path", "tablix_name", "repeat"],
            "additionalProperties": False,
        },
        handler=_layout.set_repeat_on_new_page,
        mutating=True,
    )

    server.register_tool(
        name="set_repeat_header_rows",
        description=(
            "Repeat a table's header rows at the top of every page: "
            "RepeatOnNewPage + KeepWithGroup=After on the static rows above the "
            "first group (plus RepeatColumnHeaders on a matrix); fixed=true also "
            "freezes them while scrolling. repeat=false undoes it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "repeat": {"type": "boolean", "default": True},
                "fixed": {"type": ["boolean", "null"], "default": None},
            },
            "required": ["path", "tablix_name"],
            "additionalProperties": False,
        },
        handler=_layout.set_repeat_header_rows,
        mutating=True,
    )

    server.register_tool(
        name="set_keep_together",
        description=(
            "Keep a tablix, rectangle, chart, textbox, map or gauge on one page "
            "when possible (KeepTogether); keep=false removes it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "keep": {"type": "boolean"},
            },
            "required": ["path", "name", "keep"],
            "additionalProperties": False,
        },
        handler=_layout.set_keep_together,
        mutating=True,
    )

    server.register_tool(
        name="set_keep_with_group",
        description=(
            "Keep a static row with the group rows after or before it "
            "(KeepWithGroup: None, Before, After), e.g. member='header', "
            "value='After' glues the column-header row to the data. Refuses a "
            "member that has a group (value='None' still removes it there)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "member": {
                    "oneOf": [
                        {"type": "integer", "minimum": 0},
                        {"type": "string"},
                    ],
                    "description": (
                        "A row instead of a group: body row index, or 'header', "
                        "'details', '<group>_header', '<group>_footer'."
                    ),
                },
                "value": {
                    "type": "string",
                    "enum": list(_layout._VALID_KEEP_WITH_GROUP),
                },
            },
            "required": ["path", "tablix_name", "value"],
            "additionalProperties": False,
        },
        handler=_layout.set_keep_with_group,
        mutating=True,
    )

    # ---- layout containers (Phase 11) -----------------------------------

    server.register_tool(
        name="add_rectangle",
        description=(
            "Add a rectangle to the body (or a container rectangle): an empty frame, or a container for "
            "contained_items (moved in with their on-page position kept). Items "
            "that don't fit are refused unless fit=true grows the rectangle."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "top": {"type": "string"},
                "left": {"type": "string"},
                "width": {"type": "string"},
                "height": {"type": "string"},
                "contained_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 0,
                    "default": [],
                    "description": (
                        "Optional names of existing body items to move "
                        "into the rectangle. Omit or pass [] for an "
                        "empty rectangle (visual frame)."
                    ),
                },
                "fit": {
                    "type": "boolean",
                    "description": (
                        "Grow the rectangle to the union of its box and "
                        "the contained items' boxes instead of refusing "
                        "a misfit (default false)."
                    ),
                },
                "container": _CONTAINER_PARAM,
            },
            "required": ["path", "name", "top", "left", "width", "height"],
            "additionalProperties": False,
        },
        handler=_layout.add_rectangle,
        mutating=True,
    )

    server.register_tool(
        name="add_list",
        description=(
            "Add a list (a one-cell repeating tablix with a rectangle inside, "
            "named '<name>_Rect') bound to a dataset; items placed in the "
            "rectangle repeat once per row. Fill it with container='<name>_Rect' "
            "on the add tools, or move_report_item."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "dataset_name": {"type": "string"},
                "top": {"type": "string"},
                "left": {"type": "string"},
                "width": {"type": "string"},
                "height": {"type": "string"},
            },
            "required": ["path", "name", "dataset_name", "top", "left", "width", "height"],
            "additionalProperties": False,
        },
        handler=_layout.add_list,
        mutating=True,
    )

    server.register_tool(
        name="add_line",
        description=(
            "Add a line to the body. top/left is the start point and width/height "
            "the offset to the end: height='0in' is horizontal, width='0in' "
            "vertical. Optional color, line_thickness, line_style."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "top": {"type": "string"},
                "left": {"type": "string"},
                "width": {"type": "string"},
                "height": {"type": "string"},
                "color": {"type": "string"},
                "line_thickness": {"type": "string"},
                "line_style": {
                    "type": "string",
                    "enum": list(_layout._VALID_LINE_STYLES),
                },
                "container": _CONTAINER_PARAM,
            },
            "required": ["path", "name", "top", "left", "width", "height"],
            "additionalProperties": False,
        },
        handler=_layout.add_line,
        mutating=True,
    )

    server.register_tool(
        name="move_report_item",
        description=(
            "Move a report item into the body, page header, page footer or a "
            "rectangle (to='body' / 'header' / 'footer' / a Rectangle name, a "
            "List's too), keeping its style, actions, visibility and on-page "
            "position (top/left override). Refuses data regions, and Fields! "
            "outside a scoped aggregate, in the header/footer."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "to": {
                    "type": "string",
                    "description": "'body', 'header', 'footer', or a Rectangle's name.",
                },
                "top": {
                    "type": ["string", "null"],
                    "description": "New Top in the destination; omit to keep the page position.",
                },
                "left": {
                    "type": ["string", "null"],
                    "description": "New Left in the destination; omit to keep the page position.",
                },
            },
            "required": ["path", "name", "to"],
            "additionalProperties": False,
        },
        handler=_containers.move_report_item,
        mutating=True,
    )

    server.register_tool(
        name="ungroup_rectangle",
        description=(
            "Remove a rectangle and move its items out to where it was, keeping "
            "their on-page position (undoes add_rectangle's contained_items). "
            "Warns when the rectangle's visibility, border or action no longer "
            "applies to them."
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
        handler=_containers.ungroup_rectangle,
        mutating=True,
    )
    server.register_tool(
        name="rename_report_item",
        description=(
            "Rename a textbox, rectangle, image, tablix, chart or group anywhere in the "
            "report and rewrite every reference to it (ReportItems!, ToggleItem, scope "
            "strings, RepeatWith, sort targets)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_name": {"type": "string"},
                "new_name": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": list(_rename.KINDS),
                    "description": "Only when old_name is ambiguous.",
                },
                "rename_inner": {
                    "type": "boolean",
                    "default": False,
                    "description": "List tablix: also rename <old>_Rect / <old>_Details.",
                },
            },
            "required": ["path", "old_name", "new_name"],
            "additionalProperties": False,
        },
        handler=_rename.rename_report_item,
        mutating=True,
    )

    # ---- xpath escape hatch (Phase 12 commit 45) ------------------------

    server.register_tool(
        name="raw_xml_view",
        description=(
            "Read the raw XML matching an XPath (context: the Report root; 'r:' = "
            "RDL namespace, 'rd:' = designer namespace), e.g. "
            "\".//r:Textbox[@Name='X']/r:Style\". Output is capped at max_bytes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "xpath": {"type": "string"},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 40000,
                    "description": (
                        "Cap on the total UTF-8 size of the returned XML (0 = no cap). "
                        "Over the cap, the crossing element is cut and ends in a "
                        "truncation marker; later matches are omitted and counted."
                    ),
                },
            },
            "required": ["path", "xpath"],
            "additionalProperties": False,
        },
        handler=_escape.raw_xml_view,
    )

    server.register_tool(
        name="raw_xml_replace",
        description=(
            "Last resort: replace the one element an XPath matches with new XML "
            "(RDL is the default namespace, 'rd:' is bound). Refuses zero or "
            "several matches and the Report root. Prefer a dedicated tool."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "xpath": {"type": "string"},
                "content": {
                    "type": "string",
                    "description": (
                        "XML for the replacement element. Exactly one top-level element."
                    ),
                },
            },
            "required": ["path", "xpath", "content"],
            "additionalProperties": False,
        },
        handler=_escape.raw_xml_replace,
        mutating=True,
    )

    # ---- actions / tooltip / document-map (Phase 5) ---------------------

    _DRILLTHROUGH_PARAM_ITEM = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "value": {"type": "string"},
            "omit": {"type": ["boolean", "string"]},
        },
        "required": ["name", "value"],
        "additionalProperties": False,
    }
    # Shared by set_textbox_action / set_image_action / set_item_action.
    _ACTION_PROPS = {
        "action_type": {
            "type": "string",
            "enum": ["Hyperlink", "Drillthrough", "BookmarkLink"],
        },
        "target_expression": {"type": "string"},
        "drillthrough_parameters": {
            "type": "array",
            "items": _DRILLTHROUGH_PARAM_ITEM,
        },
        "clear": {"type": "boolean"},
    }

    server.register_tool(
        name="set_textbox_action",
        description=(
            "Make a textbox clickable: a hyperlink (URL), a drillthrough to "
            "another report (with parameters) or a bookmark link; clear=true "
            "removes it. target_expression is text or an =expression. Or pass "
            "`cell` (tablix + column header/field/index + row) instead of textbox_name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "textbox_name": {"type": "string"},
                "cell": _columns.CELL_SCHEMA,
                **_ACTION_PROPS,
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_columns.with_cell_target(actions.set_textbox_action),
        mutating=True,
    )
    server.register_tool(
        name="set_image_action",
        description=(
            "Make an image clickable: hyperlink, drillthrough or bookmark link "
            "(as set_textbox_action)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "image_name": {"type": "string"},
                **_ACTION_PROPS,
            },
            "required": ["path", "image_name"],
            "additionalProperties": False,
        },
        handler=actions.set_image_action,
        mutating=True,
    )
    server.register_tool(
        name="set_item_action",
        description=(
            "Make a textbox, image, rectangle or line clickable (as "
            "set_textbox_action), by item name; clear=true removes it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                **_ACTION_PROPS,
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=actions.set_item_action,
        mutating=True,
    )
    server.register_tool(
        name="set_textbox_tooltip",
        description=(
            "Set or clear ('') a textbox's tooltip (text or =expression). Or pass "
            "`cell` (tablix + column header/field/index + row) instead of "
            "textbox_name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "textbox_name": {"type": "string"},
                "cell": _columns.CELL_SCHEMA,
                "text_or_expression": {"type": "string"},
            },
            "required": ["path", "text_or_expression"],
            "additionalProperties": False,
        },
        handler=_columns.with_cell_target(actions.set_textbox_tooltip),
        mutating=True,
    )
    server.register_tool(
        name="set_item_tooltip",
        description="Set or clear ('') the hover tooltip of any named report item.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "text_or_expression": {"type": "string"},
            },
            "required": ["path", "name", "text_or_expression"],
            "additionalProperties": False,
        },
        handler=actions.set_item_tooltip,
        mutating=True,
    )
    server.register_tool(
        name="set_bookmark",
        description=(
            "Set or clear ('') the bookmark of any named report item: the "
            "target a BookmarkLink action jumps to."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "bookmark_or_expression": {"type": "string"},
            },
            "required": ["path", "name", "bookmark_or_expression"],
            "additionalProperties": False,
        },
        handler=actions.set_bookmark,
        mutating=True,
    )
    server.register_tool(
        name="set_document_map_label",
        description=(
            "Set or clear ('') the document map (table of contents) label of any named report item."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "element_name": {"type": "string"},
                "label_or_expression": {"type": "string"},
            },
            "required": ["path", "element_name", "label_or_expression"],
            "additionalProperties": False,
        },
        handler=actions.set_document_map_label,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_series_action",
        description=(
            "Make a chart series clickable: hyperlink, drillthrough or bookmark "
            "link (as set_textbox_action), addressed by chart_name + series_name; "
            "action_type None removes it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "series_name": {"type": "string"},
                "action_type": {
                    "type": "string",
                    "enum": ["Hyperlink", "Drillthrough", "BookmarkLink", "None"],
                },
                "target_expression": {"type": ["string", "null"], "default": None},
                "drillthrough_parameters": {
                    "type": "array",
                    "items": _DRILLTHROUGH_PARAM_ITEM,
                    "description": "Drillthrough only.",
                },
            },
            "required": [
                "path",
                "chart_name",
                "series_name",
                "action_type",
            ],
            "additionalProperties": False,
        },
        handler=actions.set_chart_series_action,
        mutating=True,
    )

    server.register_tool(
        name="remove_query_parameter",
        description=("Remove a query parameter from a dataset."),
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
        mutating=True,
    )

    server.register_tool(
        name="list_tablix_filters",
        description=(
            "List a tablix's filters in order: expression, operator, values. The "
            "list index is the handle for remove_tablix_filter."
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
            "Add a filter to one tablix (Equal, NotEqual, GreaterThan, In, "
            "Between, Like, TopN, ...). field_format wraps the expression in "
            "Format() for string parameters; warnings flag field/parameter type "
            "mismatches. Returns the filter's index."
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
                "field_format": {
                    "type": "string",
                    "description": (
                        "Optional format string (e.g. 'MMM, yyyy') wrapping "
                        "the expression as Format(<body>, '<fmt>')."
                    ),
                },
            },
            "required": ["path", "tablix_name", "expression", "operator", "values"],
            "additionalProperties": False,
        },
        handler=tablix.add_tablix_filter,
        mutating=True,
    )
    server.register_tool(
        name="add_row_group",
        description=(
            "Group a tablix's rows (Report Builder's Add Parent Group): wraps the "
            "detail rows (or wrap='member:<group>' / 'all') in a new group, at the "
            "top level or inside parent_group; column-header and total rows stay "
            "outside. header: 'row' (group header row), 'column' (row-header "
            "cell), 'none'; 'auto' picks 'column' on a matrix, else 'row'."
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
                "parent_group": {
                    "type": ["string", "null"],
                    "description": "Existing row group to nest the new group inside "
                    "(default: top level).",
                },
                "wrap": {
                    "type": "string",
                    "default": "details",
                    "description": "'details' (the member holding the detail rows), "
                    "'member:<group>' (that group), or 'all' (every member at that level, "
                    "header/total rows included).",
                },
                "header": {
                    "type": "string",
                    "enum": ["auto", "row", "column", "none"],
                    "default": "auto",
                },
            },
            "required": ["path", "tablix_name", "group_name", "group_expression"],
            "additionalProperties": False,
        },
        handler=tablix.add_row_group,
        mutating=True,
    )
    server.register_tool(
        name="remove_row_group",
        description=(
            "Remove a row group (ungroup): its rows move up a level; its header "
            "row and subtotal rows are deleted (keep_subtotals=true keeps "
            "subtotals); a row-header cell goes with its blank padding. Refuses "
            "the detail group and a row-header group whose removal would drop "
            "other header content."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "keep_subtotals": {"type": "boolean", "default": False},
            },
            "required": ["path", "tablix_name", "group_name"],
            "additionalProperties": False,
        },
        handler=tablix.remove_row_group,
        mutating=True,
    )
    server.register_tool(
        name="convert_to_matrix",
        description=(
            "Turn a grouped table into a matrix (after add_row_group + "
            "add_column_group): detail cells become aggregates (default Sum), "
            "group captions become row/column headers, caption-only rows and "
            "columns are deleted, and a corner is created. Then "
            "set_tablix_corner. See docs/MATRIX-cookbook.md."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "row_group": {"type": "string"},
                "column_group": {"type": "string"},
                "aggregate": {
                    "type": "string",
                    "enum": list(tablix.MATRIX_AGGREGATES),
                    "default": "Sum",
                },
            },
            "required": ["path", "tablix_name", "row_group", "column_group"],
            "additionalProperties": False,
        },
        handler=tablix.convert_to_matrix,
        mutating=True,
    )
    server.register_tool(
        name="set_tablix_corner",
        description=(
            "Set the text (or =expression) of a matrix's top-left corner cell, "
            "e.g. the row-axis caption. Creates or resizes the corner to the "
            "header depths; row/col pick a cell. Needs row and column headers "
            "(convert_to_matrix first)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "text": {"type": ["string", "null"], "default": None},
                "expression": {"type": ["string", "null"], "default": None},
                "row": {"type": "integer", "minimum": 0, "default": 0},
                "col": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["path", "tablix_name"],
            "additionalProperties": False,
        },
        handler=tablix.set_tablix_corner,
        mutating=True,
    )
    server.register_tool(
        name="set_group_sort",
        description=(
            "Sort a row group: replaces its sort keys, e.g. ['=Fields!Region.Value'] or "
            "[{'value': '=Fields!Amount.Value', 'direction': 'Descending'}]; [] removes the sort."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "sort_expressions": _SORT_EXPRESSIONS_SCHEMA,
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
        mutating=True,
    )
    server.register_tool(
        name="set_group_visibility",
        description=(
            "Hide or show a row group (Hidden: true/false or =expression), "
            "optionally toggled by a textbox (drill-down) in the same "
            "region/tablix, outside the hidden rows; omit it to keep the current "
            "toggle, null or '' removes it."
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
        mutating=True,
    )

    # ---- column-axis groups (v0.2) ----------------------------------------
    server.register_tool(
        name="add_column_group",
        description=(
            "Group a tablix's columns (make it a matrix): wraps the column "
            "hierarchy in a new group whose TablixHeader shows the group "
            "expression; body columns are unchanged and the corner grows to match."
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
        mutating=True,
    )
    server.register_tool(
        name="remove_column_group",
        description=(
            "Remove a column group (ungroup): its columns move up a level, its "
            "header goes, and the corner shrinks; body columns are kept."
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
        mutating=True,
    )
    server.register_tool(
        name="set_column_group_sort",
        description=(
            "Sort a column group: replaces its sort keys ('=expr' or {value, direction}); "
            "set_group_sort is for row groups."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "group_name": {"type": "string"},
                "sort_expressions": _SORT_EXPRESSIONS_SCHEMA,
            },
            "required": ["path", "tablix_name", "group_name", "sort_expressions"],
            "additionalProperties": False,
        },
        handler=tablix_columns.set_column_group_sort,
        mutating=True,
    )
    server.register_tool(
        name="set_column_group_visibility",
        description=(
            "Hide or show a column group (Hidden: true/false or =expression), "
            "optionally toggled by a textbox outside the hidden columns; omit the "
            "toggle to keep it, null or '' removes it."
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
        mutating=True,
    )
    server.register_tool(
        name="add_tablix_column",
        description=(
            "Add a column to a tablix: header_text in the first row (default "
            "column_name), `expression` (e.g. =Fields!X.Value) in the data row, "
            "in a textbox named column_name. position is the 0-based body column "
            "(default: end); a position inside a column group moves outside it "
            "unless inside_group names it. New cells copy the left neighbour's style "
            "(copy_style_from)."
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
                "inside_group": {
                    "type": ["string", "null"],
                    "description": "Column group to insert inside on purpose (the column "
                    "then repeats per group instance); position must lie within that group's "
                    "columns or just past them. Default: the column stays outside every group.",
                },
                "header_name": {
                    "type": ["string", "null"],
                    "description": "Name of the header-row cell's textbox (must be unused). "
                    "Default: <column_name>_0, suffixed if taken.",
                },
                "copy_style_from": {
                    "type": ["string", "null"],
                    "default": "left",
                    "description": "Each new cell copies the style (fill, border, font, "
                    "alignment, number format) of the cell in the same row: 'left' "
                    "(default), 'right', or the column of the named textbox; null for "
                    "unstyled cells.",
                },
                "format": {
                    "type": ["string", "null"],
                    "description": "Number/date format for the data cell, e.g. '#,0.00' "
                    "('' clears an inherited one).",
                },
            },
            "required": ["path", "tablix_name", "column_name", "expression"],
            "additionalProperties": False,
        },
        handler=tablix_columns.add_tablix_column,
        mutating=True,
    )
    server.register_tool(
        name="remove_tablix_column",
        description=(
            "Remove a tablix column: by the name of a textbox in it, or "
            "column_ref {header | field | index}. Merged cells shrink instead. "
            "Refuses the last column, a group's only column (unless "
            "remove_group=true) and a toggle target (unless force=true)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "column_name": {"type": "string"},
                "column_ref": _columns.COLUMN_REF_SCHEMA,
                "remove_group": {"type": "boolean", "default": False},
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Also remove ToggleItems that point at a textbox this "
                        "removal deletes (otherwise such a removal is refused)."
                    ),
                },
            },
            "required": ["path", "tablix_name"],
            "additionalProperties": False,
        },
        handler=tablix_columns.remove_tablix_column,
        mutating=True,
    )
    server.register_tool(
        name="add_subtotal_row",
        description=(
            "Add a subtotal or grand-total row to a row group. aggregates is "
            "[{column, expression}] (column = the data-row textbox name, or "
            "column_ref) e.g. =Sum(Fields!X.Value). position footer|header; scope "
            "'group' (per group) or 'total' (once, outside the group = grand "
            "total)."
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
                            "column_ref": _columns.COLUMN_REF_SCHEMA,
                            "expression": {"type": "string"},
                        },
                        "required": ["expression"],
                        "additionalProperties": False,
                    },
                },
                "position": {
                    "type": "string",
                    "enum": ["header", "footer"],
                    "default": "footer",
                },
                "scope": {
                    "type": "string",
                    "enum": ["group", "total"],
                    "default": "group",
                },
            },
            "required": ["path", "tablix_name", "group_name", "aggregates"],
            "additionalProperties": False,
        },
        handler=tablix_subtotals.add_subtotal_row,
        mutating=True,
    )
    server.register_tool(
        name="add_subtotal_column",
        description=(
            "Add a subtotal or grand-total column to a column group. aggregates "
            "is [{row, expression}] by 0-based body row. position after|before; "
            "scope 'group' (per group) or 'total' (once, outside the group = "
            "grand total)."
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
                            "row": {"type": "integer"},
                            "expression": {"type": "string"},
                        },
                        "required": ["row", "expression"],
                        "additionalProperties": False,
                    },
                },
                "position": {
                    "type": "string",
                    "enum": ["before", "after"],
                    "default": "after",
                },
                "width": {"type": "string", "default": "1in"},
                "scope": {
                    "type": "string",
                    "enum": ["group", "total"],
                    "default": "group",
                },
            },
            "required": ["path", "tablix_name", "group_name", "aggregates"],
            "additionalProperties": False,
        },
        handler=tablix_subtotals.add_subtotal_column,
        mutating=True,
    )
    server.register_tool(
        name="set_cell_span",
        description=(
            "Merge a tablix body cell with the cells to its right (col_span; 1 "
            "resets). Address it by row_index + column_name (a textbox in the "
            "column) or column_ref. A covered cell with content is refused unless "
            "force=true deletes it. Body cells can't span rows."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "row_index": {"type": "integer", "minimum": 0},
                "column_name": {"type": "string"},
                "column_ref": _columns.COLUMN_REF_SCHEMA,
                "row_span": {"type": ["integer", "null"], "minimum": 1},
                "col_span": {"type": ["integer", "null"], "minimum": 1},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["path", "tablix_name", "row_index"],
            "additionalProperties": False,
        },
        handler=tablix_cells.set_cell_span,
        mutating=True,
    )
    server.register_tool(
        name="add_static_row",
        description=(
            "Add a static (ungrouped) row of literal text to a tablix: cells is "
            "one string per column. position is the 0-based body row (default: "
            "end); a position inside a group moves outside it unless inside_group "
            "names it. Textboxes are named row_name, row_name_<col>."
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
                "inside_group": {
                    "type": ["string", "null"],
                    "description": "Row group to insert inside on purpose (the row then "
                    "repeats per group instance); position must lie within that group's rows "
                    "or just past them. Default: the row stays outside every group.",
                },
            },
            "required": ["path", "tablix_name", "row_name"],
            "additionalProperties": False,
        },
        handler=tablix_static.add_static_row,
        mutating=True,
    )
    server.register_tool(
        name="add_static_column",
        description=(
            "Add a static (ungrouped) column of literal text to a tablix: cells "
            "is one string per row. position is the 0-based body column (default: "
            "end); a position inside a column group moves outside it unless "
            "inside_group names it."
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
                "inside_group": {
                    "type": ["string", "null"],
                    "description": "Column group to insert inside on purpose (the column "
                    "then repeats per group instance); position must lie within that group's "
                    "columns or just past them. Default: the column stays outside every group.",
                },
            },
            "required": ["path", "tablix_name", "column_name"],
            "additionalProperties": False,
        },
        handler=tablix_static.add_static_column,
        mutating=True,
    )

    # ---- positioning (v0.2 commits 6-8) -----------------------------------
    server.register_tool(
        name="set_body_item_position",
        description=(
            "Move a named item in the report body, or inside one of its rectangles / lists "
            "(coordinates relative to it), to (top, left); any RDL size ('2cm', '0.75in')."
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
        mutating=True,
    )
    server.register_tool(
        name="set_header_item_position",
        description=(
            "Move a named item in the page header (or a rectangle in it) to (top, left). Warns when it "
            "hangs below the band or past the printable width; grow_band=true "
            "grows the header instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "top": {"type": "string"},
                "left": {"type": "string"},
                "grow_band": {
                    "type": "boolean",
                    "description": (
                        "Grow the band's Height to fit the item instead of "
                        "returning a warning when it hangs below the band."
                    ),
                },
            },
            "required": ["path", "name", "top", "left"],
            "additionalProperties": False,
        },
        handler=positioning.set_header_item_position,
        mutating=True,
    )
    server.register_tool(
        name="set_footer_item_position",
        description=(
            "Move a named item in the page footer (or a rectangle in it) to (top, left). Warns when it "
            "hangs below the band or past the printable width; grow_band=true "
            "grows the footer instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "top": {"type": "string"},
                "left": {"type": "string"},
                "grow_band": {
                    "type": "boolean",
                    "description": (
                        "Grow the band's Height to fit the item instead of "
                        "returning a warning when it hangs below the band."
                    ),
                },
            },
            "required": ["path", "name", "top", "left"],
            "additionalProperties": False,
        },
        handler=positioning.set_footer_item_position,
        mutating=True,
    )
    server.register_tool(
        name="set_body_item_size",
        description=(
            "Resize a named item in the report body, or inside one of its rectangles / "
            "lists: width and/or height (RDL sizes)."
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
        mutating=True,
    )
    server.register_tool(
        name="set_header_item_size",
        description=(
            "Resize a named item in the page header (or a rectangle in it): width and/or height. Warns "
            "when it outgrows the band or the printable width; grow_band=true "
            "grows the header instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "width": {"type": ["string", "null"]},
                "height": {"type": ["string", "null"]},
                "grow_band": {
                    "type": "boolean",
                    "description": (
                        "Grow the band's Height to fit the item instead of "
                        "returning a warning when it hangs below the band."
                    ),
                },
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=positioning.set_header_item_size,
        mutating=True,
    )
    server.register_tool(
        name="set_footer_item_size",
        description=(
            "Resize a named item in the page footer (or a rectangle in it): width and/or height. Warns "
            "when it outgrows the band or the printable width; grow_band=true "
            "grows the footer instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "width": {"type": ["string", "null"]},
                "height": {"type": ["string", "null"]},
                "grow_band": {
                    "type": "boolean",
                    "description": (
                        "Grow the band's Height to fit the item instead of "
                        "returning a warning when it hangs below the band."
                    ),
                },
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=positioning.set_footer_item_size,
        mutating=True,
    )

    # ---- reader extensions (v0.2) -----------------------------------------
    server.register_tool(
        name="list_body_items",
        description=(
            "List the named items in the report body: name, type, top, left, "
            "width, height. recursive=true also lists items inside rectangles "
            "(with parent; positions relative to it)."
        ),
        input_schema=_LIST_ITEMS_SCHEMA,
        handler=reader.list_body_items,
    )
    server.register_tool(
        name="list_header_items",
        description=(
            "List the named items in the page header: name, type, position, size "
            "(like list_body_items)."
        ),
        input_schema=_LIST_ITEMS_SCHEMA,
        handler=reader.list_header_items,
    )
    server.register_tool(
        name="list_footer_items",
        description=(
            "List the named items in the page footer: name, type, position, size "
            "(like list_body_items)."
        ),
        input_schema=_LIST_ITEMS_SCHEMA,
        handler=reader.list_footer_items,
    )
    server.register_tool(
        name="get_textbox",
        description=(
            "Read a textbox anywhere in the report: value, position, size, "
            "visibility, CanGrow/CanShrink, style grouped as box / border / "
            "paragraph / run, runs[] (accepted as-is by set_textbox_runs), "
            "paragraphs[], action, tooltip, bookmark, map label. Tablix cells "
            "have no position."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "response_format": RESPONSE_FORMAT_PARAM,
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=with_response_format("get_textbox", reader.get_textbox),
    )
    server.register_tool(
        name="get_image",
        description=(
            "Read an image: position, size, source (External / Embedded / "
            "Database), value, sizing, MIME type, style, visibility, action, "
            "tooltip, bookmark, map label."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "response_format": RESPONSE_FORMAT_PARAM,
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=with_response_format("get_image", reader.get_image),
    )
    server.register_tool(
        name="get_rectangle",
        description=(
            "Read a rectangle: position, size, contained items, style, visibility, "
            "action, tooltip, bookmark, map label."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "response_format": RESPONSE_FORMAT_PARAM,
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=with_response_format("get_rectangle", reader.get_rectangle),
    )
    server.register_tool(
        name="get_chart",
        description=(
            "Read a chart: position, size, dataset, palette, custom colours, series "
            "(type, value, color, labels, axes, legend, action), series_groups, "
            "category_groups, derived_series, chart_areas/axes, legends, titles, style, "
            "visibility."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "response_format": RESPONSE_FORMAT_PARAM,
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=with_response_format("get_chart", reader.get_chart),
    )

    # ---- snapshot (v0.2 commit 14, v0.4 restore) --------------------------
    server.register_tool(
        name="backup_report",
        description=(
            "Back up a report to <path>.bak.<UTC timestamp>[.<label>] before a risky "
            "edit; returns the backup path. dest_dir picks another folder. Inside a "
            "transaction it saves the edited state. Undo with restore_from_backup."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the .rdl file."},
                "dest_dir": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Existing folder for the backup; default: the report's.",
                },
                "label": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Tag added to the name: 1-64 letters, digits, - or _.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=snapshot.backup_report,
    )
    server.register_tool(
        name="list_backups",
        description=(
            "List a report's backups, newest first, with label, time and whether each "
            "matches the current file. Pick one for restore_from_backup."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the .rdl file."},
                "dest_dir": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Folder the backups are in; default: the report's.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=snapshot.list_backups,
    )
    server.register_tool(
        name="restore_from_backup",
        description=(
            "Restore a backup over its report (undo). Backs the current state up "
            "first (safety_backup) and refuses a backup that isn't a valid report. "
            "target_path defaults to the report the backup was taken of."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "backup_path": {"type": "string"},
                "target_path": {"type": ["string", "null"], "default": None},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["backup_path"],
            "additionalProperties": False,
        },
        handler=snapshot.restore_from_backup,
        mutating=True,
    )

    # ---- parameter CRUD (v0.2 commits 15-19) ------------------------------
    server.register_tool(
        name="set_parameter_prompt",
        description=(
            "Set a report parameter's prompt text. '' removes the prompt; ' ' keeps a blank one."
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
        mutating=True,
    )
    server.register_tool(
        name="set_parameter_type",
        description=(
            "Set a report parameter's data type: Boolean, DateTime, Integer, "
            "Float or String. Refuses when an existing default or static valid "
            "value wouldn't fit (fix it first)."
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
        mutating=True,
    )
    server.register_tool(
        name="add_parameter",
        description=(
            "Add a report parameter (name, type, optional prompt and flags). Then "
            "set its values with set_parameter_available_values / "
            "set_parameter_default_values."
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
        mutating=True,
    )
    server.register_tool(
        name="remove_parameter",
        description=(
            "Remove a report parameter. Refuses while it is still referenced in "
            "any form (Parameters!<name>.Value/.Label/.Count/.IsMultiValue, "
            'Parameters("<name>"); lists where) unless force=true. '
            "compact_layout=true re-packs the parameter pane."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "force": {"type": "boolean", "default": False},
                "compact_layout": {"type": "boolean", "default": False},
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=parameters.remove_parameter,
        mutating=True,
    )
    server.register_tool(
        name="rename_parameter",
        description=(
            "Rename a report parameter and update every reference to it: "
            "Parameters!<old> with any member (.Value, .Label, .Count, "
            '.IsMultiValue), Parameters("<old>"), in every expression and the '
            "Code block, plus the pane layout. Refuses if new_name is taken."
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
        mutating=True,
    )
    server.register_tool(
        name="sync_parameter_layout",
        description=(
            "Repair the parameter pane layout: drop cells for removed parameters, "
            "add cells for missing ones, move cells that share a slot or sit "
            "outside the grid. No-op without a layout. add/remove/rename_parameter "
            "already do this."
        ),
        input_schema=_PATH_ONLY_SCHEMA,
        handler=parameters.sync_parameter_layout,
        mutating=True,
    )

    server.register_tool(
        name="set_detail_row_visibility",
        description=(
            "Hide or show a tablix's detail rows (Hidden: true/false or "
            "=expression), optionally toggled by a textbox outside them (e.g. a "
            "group header cell); omit the toggle to keep it, null or '' removes "
            "it."
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
        mutating=True,
    )
    server.register_tool(
        name="set_row_height",
        description=("Set the height of a tablix body row (0-based row_index); any RDL size."),
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
        mutating=True,
    )

    server.register_tool(
        name="remove_tablix_filter",
        description=("Remove a tablix filter by its index from list_tablix_filters."),
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
        mutating=True,
    )

    server.register_tool(
        name="set_page_setup",
        description=(
            "Set page size, margins and column count; only the fields passed are "
            "written. columns=1 removes multi-column layout. Warns when the body "
            "no longer fits the page width."
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
        mutating=True,
    )
    server.register_tool(
        name="set_page_orientation",
        description=(
            "Set page orientation to Portrait or Landscape (swaps page width and "
            "height if needed; a missing size is the 8.5x11in default). Warns when "
            "the body no longer fits the page width."
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
        mutating=True,
    )
    server.register_tool(
        name="set_body_size",
        description=(
            "Set the report body's height and/or width, e.g. widen it for a wide "
            "tablix that gets clipped. Paper size is set_page_setup; item sizes "
            "are set_body_item_size."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "width": {"type": ["string", "null"], "default": None},
                "height": {"type": ["string", "null"], "default": None},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=page.set_body_size,
        mutating=True,
    )
    # ---- geometry fix-ups (#49) ---------------------------------------------
    server.register_tool(
        name="fit_page_to_body",
        description=(
            "Fix a body wider than the page (blank pages): mode='page' (default) "
            "widens the page to fit; mode='body' shrinks the body to its "
            "rightmost item (errors if that still doesn't fit)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "mode": {"type": "string", "enum": ["page", "body"], "default": "page"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_layout_helpers.fit_page_to_body,
        mutating=True,
    )
    server.register_tool(
        name="center_item",
        description=(
            "Centre a named item in its container (the body, the printable width "
            "for header and footer items, or its rectangle): axis horizontal "
            "(default), vertical or both."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "axis": {
                    "type": "string",
                    "enum": ["horizontal", "vertical", "both"],
                    "default": "horizontal",
                },
            },
            "required": ["path", "name"],
            "additionalProperties": False,
        },
        handler=_layout_helpers.center_item,
        mutating=True,
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
    _IMAGE_SCHEMA = {  # add_header_image / add_footer_image
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
            "mime_type": _IMAGE_MIME_TYPE_SCHEMA,
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
    _REMOVE_FORCE = {
        "type": "boolean",
        "default": False,
        "description": (
            "Remove even if other items still reference it or what it holds "
            "(ReportItems!, ToggleItem, scopes); they are left dangling."
        ),
    }
    _NAMED_REMOVE_SCHEMA = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "name": {"type": "string"},
            "force": _REMOVE_FORCE,
        },
        "required": ["path", "name"],
        "additionalProperties": False,
    }

    server.register_tool(
        name="set_page_header",
        description=(
            "Add or update the page header: height and print on first/last page. "
            "Only the fields passed are written (a new header gets 0.5in). Warns "
            "when items no longer fit."
        ),
        input_schema=_SECTION_FLAGS_SCHEMA,
        handler=header_footer.set_page_header,
        mutating=True,
    )
    server.register_tool(
        name="set_page_footer",
        description=(
            "Add or update the page footer: height and print on first/last page "
            "(like set_page_header)."
        ),
        input_schema=_SECTION_FLAGS_SCHEMA,
        handler=header_footer.set_page_footer,
        mutating=True,
    )
    server.register_tool(
        name="add_header_textbox",
        description=(
            "Add a textbox to the page header (created if missing; grows to fit). "
            "text is literal or an =expression; Fields! needs an aggregate with a "
            'dataset scope here, e.g. =First(Fields!X.Value, "DataSet1"). Pass '
            "raw text: & not &amp;."
        ),
        input_schema=_TEXTBOX_SCHEMA,
        handler=header_footer.add_header_textbox,
        mutating=True,
    )
    server.register_tool(
        name="add_footer_textbox",
        description=(
            "Add a textbox to the page footer, e.g. a page number "
            "(=Globals!PageNumber). Same arguments as add_header_textbox. Pass "
            "raw text: & not &amp;."
        ),
        input_schema=_TEXTBOX_SCHEMA,
        handler=header_footer.add_footer_textbox,
        mutating=True,
    )
    server.register_tool(
        name="add_header_image",
        description=(
            "Add an image (logo) to the page header: image_source External (URL), "
            "Embedded (name of an embedded image) or Database (=Fields! "
            "expression, plus the required mime_type). The header grows to fit."
        ),
        input_schema=_IMAGE_SCHEMA,
        handler=header_footer.add_header_image,
        mutating=True,
    )
    server.register_tool(
        name="add_footer_image",
        description=("Add an image to the page footer (same arguments as add_header_image)."),
        input_schema=_IMAGE_SCHEMA,
        handler=header_footer.add_footer_image,
        mutating=True,
    )
    server.register_tool(
        name="remove_header_item",
        description=(
            "Remove a named item from the page header, also inside a rectangle. "
            "Refuses while other items reference it unless force."
        ),
        input_schema=_NAMED_REMOVE_SCHEMA,
        handler=header_footer.remove_header_item,
        mutating=True,
    )
    server.register_tool(
        name="remove_footer_item",
        description=(
            "Remove a named item from the page footer, also inside a rectangle. "
            "Refuses while other items reference it unless force."
        ),
        input_schema=_NAMED_REMOVE_SCHEMA,
        handler=header_footer.remove_footer_item,
        mutating=True,
    )

    server.register_tool(
        name="set_textbox_style",
        description=(
            "Format a textbox or tablix cell: font, size, weight, italic, color, "
            "underline, number format, alignment, background, borders, padding, "
            "can_grow/can_shrink. Only properties passed are written; font/color/"
            "format apply to every run, text_align to every paragraph; invalid "
            "literals are refused. A cell's textbox name works (e.g. "
            "'HeaderAmount'), or pass `cell` (tablix + column header/field/index "
            "+ row)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "textbox_name": {"type": "string"},
                "cell": _columns.CELL_SCHEMA,
                "font_family": {"type": ["string", "null"]},
                "font_size": {
                    "type": ["string", "null"],
                    "description": "RDL size, e.g. '11pt'.",
                },
                "font_weight": {
                    "type": ["string", "null"],
                    "description": "Thin | Light | Normal | Medium | SemiBold | Bold | Heavy | ... or 100-900.",
                },
                "font_style": {
                    "type": ["string", "null"],
                    "description": "Normal | Italic.",
                },
                "color": {
                    "type": ["string", "null"],
                    "description": "Text color — named, '#RRGGBB' or '#AARRGGBB'.",
                },
                "text_decoration": {
                    "type": ["string", "null"],
                    "description": "None | Underline | Overline | LineThrough.",
                },
                "background_color": {"type": ["string", "null"]},
                "border_style": {
                    "type": ["string", "null"],
                    "description": "None | Solid | Dotted | Dashed | Double | ...",
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
                "padding_top": {
                    "type": ["string", "null"],
                    "description": "RDL size (e.g. '2pt', '0.05in').",
                },
                "padding_bottom": {"type": ["string", "null"]},
                "padding_left": {"type": ["string", "null"]},
                "padding_right": {"type": ["string", "null"]},
                "writing_mode": {
                    "type": ["string", "null"],
                    "description": (
                        "Horizontal | Vertical | Rotate270 — text "
                        "orientation. Useful for narrow column headers."
                    ),
                },
                "can_grow": {
                    "type": ["boolean", "null"],
                    "description": (
                        "Allow textbox to grow vertically when content "
                        "exceeds the height. Direct Textbox child, not Style."
                    ),
                },
                "can_shrink": {
                    "type": ["boolean", "null"],
                    "description": (
                        "Allow textbox to shrink when content is shorter "
                        "than the height. Direct Textbox child, not Style."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_columns.with_cell_target(styling.set_textbox_style),
        mutating=True,
    )
    server.register_tool(
        name="set_textbox_runs",
        description=(
            "Set rich text in a textbox: several runs with their own style (e.g. "
            "a bold label + plain value) and/or several paragraphs. Edits runs in "
            "place, keeping what you don't pass; get_textbox runs[] feed back "
            "as-is. Refuses to drop paragraphs unless drop_extra_paragraphs=true. "
            "Pass raw text: & not &amp;. Or pass `cell` (tablix + column "
            "header/field/index + row) instead of textbox_name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "textbox_name": {"type": "string"},
                "cell": _columns.CELL_SCHEMA,
                "runs": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "value": {
                                "type": ["string", "null"],
                                "description": "Alias of text (the key get_textbox returns).",
                            },
                            "paragraph_index": {
                                "type": "integer",
                                "minimum": 0,
                                "description": "Paragraph this run belongs to (default 0).",
                            },
                            "font_family": {"type": ["string", "null"]},
                            "font_size": {"type": ["string", "null"]},
                            "font_weight": {"type": ["string", "null"]},
                            "font_style": {"type": ["string", "null"]},
                            "color": {"type": ["string", "null"]},
                            "format": {"type": ["string", "null"]},
                            "text_decoration": {"type": ["string", "null"]},
                            "markup_type": {
                                "type": ["string", "null"],
                                "enum": ["None", "HTML", None],
                                "description": "HTML renders the run's tags; null removes it.",
                            },
                            "style": {
                                "type": "object",
                                "description": (
                                    "Run style keyed by RDL name (get_textbox shape); "
                                    "null removes a key."
                                ),
                                "properties": {
                                    local: {"type": ["string", "null"]}
                                    for local in (
                                        "FontFamily",
                                        "FontSize",
                                        "FontWeight",
                                        "FontStyle",
                                        "Color",
                                        "Format",
                                        "TextDecoration",
                                    )
                                },
                                "additionalProperties": False,
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "drop_extra_paragraphs": {
                    "type": "boolean",
                    "description": (
                        "Delete existing trailing paragraphs that runs don't "
                        "address (default false: refuse instead)."
                    ),
                },
            },
            "required": ["path", "runs"],
            "additionalProperties": False,
        },
        handler=_columns.with_cell_target(styling.set_textbox_runs),
        mutating=True,
    )
    server.register_tool(
        name="set_textbox_value",
        description=(
            "Change a textbox's text or =expression (a label, a field, a broken "
            "aggregate). For a textbox with several runs or paragraphs use "
            "set_textbox_runs. Pass raw text: & not &amp;. Or pass `cell` (tablix "
            "+ column header/field/index + row) instead of textbox_name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "textbox_name": {"type": "string"},
                "cell": _columns.CELL_SCHEMA,
                "value": {"type": "string"},
            },
            "required": ["path", "value"],
            "additionalProperties": False,
        },
        handler=_columns.with_cell_target(styling.set_textbox_value),
        mutating=True,
    )
    server.register_tool(
        name="set_textbox_style_bulk",
        description=(
            "Format many textboxes at once with the same style arguments as "
            "set_textbox_style; unknown names are listed in skipped. `cells` adds "
            "tablix cells by column. Pair with find_textboxes_by_style."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "textbox_names": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "cells": {"type": "array", "items": _columns.CELL_SCHEMA},
                "font_family": {"type": ["string", "null"]},
                "font_size": {"type": ["string", "null"]},
                "font_weight": {"type": ["string", "null"]},
                "font_style": {"type": ["string", "null"]},
                "color": {"type": ["string", "null"]},
                "text_decoration": {"type": ["string", "null"]},
                "background_color": {"type": ["string", "null"]},
                "border_style": {"type": ["string", "null"]},
                "border_color": {"type": ["string", "null"]},
                "border_width": {"type": ["string", "null"]},
                "text_align": {"type": ["string", "null"]},
                "vertical_align": {"type": ["string", "null"]},
                "format": {"type": ["string", "null"]},
                "padding_top": {"type": ["string", "null"]},
                "padding_bottom": {"type": ["string", "null"]},
                "padding_left": {"type": ["string", "null"]},
                "padding_right": {"type": ["string", "null"]},
                "writing_mode": {"type": ["string", "null"]},
                "can_grow": {"type": ["boolean", "null"]},
                "can_shrink": {"type": ["boolean", "null"]},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_columns.with_cells_target(styling.set_textbox_style_bulk),
        mutating=True,
    )
    server.register_tool(
        name="find_textboxes_by_style",
        description=(
            "Find textboxes by current style (all filters must match), e.g. every "
            "red or bold one. Returns name, location and the matched fields; pair "
            "with set_textbox_style_bulk."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "background_color": {"type": "string"},
                "vertical_align": {"type": "string"},
                "writing_mode": {"type": "string"},
                "padding_top": {"type": "string"},
                "padding_bottom": {"type": "string"},
                "padding_left": {"type": "string"},
                "padding_right": {"type": "string"},
                "border_style": {"type": "string"},
                "border_color": {"type": "string"},
                "border_width": {"type": "string"},
                "text_align": {"type": "string"},
                "font_family": {"type": "string"},
                "font_size": {"type": "string"},
                "font_weight": {"type": "string"},
                "font_style": {"type": "string"},
                "color": {"type": "string"},
                "format": {"type": "string"},
                "text_decoration": {"type": "string"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=reader.find_textboxes_by_style,
    )
    server.register_tool(
        name="find_textbox_by_value",
        description=(
            "Find textboxes whose text or expression matches a regex, in the "
            "body, header and footer; e.g. stale =Parameters!Old.Value "
            "references."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {
                    "type": "string",
                    "description": "Python regex (re.search); not RDL/SQL glob.",
                },
            },
            "required": ["path", "pattern"],
            "additionalProperties": False,
        },
        handler=reader.find_textbox_by_value,
    )
    server.register_tool(
        name="style_tablix_row",
        description=(
            "Format every cell of a tablix row at once (same style arguments as "
            "set_textbox_style). row: a 0-based index, 'header', 'details', "
            "'<group>_header', '<group>_footer', '<group>_rowheader' or 'corner'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "tablix_name": {"type": "string"},
                "row": {
                    "oneOf": [
                        {"type": "integer", "minimum": 0},
                        {"type": "string"},
                    ],
                    "description": (
                        "Integer index, or one of: 'header', 'details', "
                        "'<group>_header', '<group>_footer', '<group>_rowheader', "
                        "'corner'."
                    ),
                },
                "font_family": {"type": ["string", "null"]},
                "font_size": {"type": ["string", "null"]},
                "font_weight": {"type": ["string", "null"]},
                "font_style": {"type": ["string", "null"]},
                "color": {"type": ["string", "null"]},
                "text_decoration": {"type": ["string", "null"]},
                "background_color": {"type": ["string", "null"]},
                "border_style": {"type": ["string", "null"]},
                "border_color": {"type": ["string", "null"]},
                "border_width": {"type": ["string", "null"]},
                "text_align": {"type": ["string", "null"]},
                "vertical_align": {"type": ["string", "null"]},
                "format": {"type": ["string", "null"]},
                "padding_top": {"type": ["string", "null"]},
                "padding_bottom": {"type": ["string", "null"]},
                "padding_left": {"type": ["string", "null"]},
                "padding_right": {"type": ["string", "null"]},
                "writing_mode": {"type": ["string", "null"]},
                "can_grow": {"type": ["boolean", "null"]},
                "can_shrink": {"type": ["boolean", "null"]},
            },
            "required": ["path", "tablix_name", "row"],
            "additionalProperties": False,
        },
        handler=styling.style_tablix_row,
        mutating=True,
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
            "container": _CONTAINER_PARAM,
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
            "mime_type": _IMAGE_MIME_TYPE_SCHEMA,
            "top": {"type": "string"},
            "left": {"type": "string"},
            "width": {"type": "string"},
            "height": {"type": "string"},
            "container": _CONTAINER_PARAM,
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
            "Add a textbox to the report body (literal text or an =expression, "
            "e.g. =Globals!ReportName). Refuses a name in use. Pass raw text: & "
            "not &amp;."
        ),
        input_schema=_BODY_TEXTBOX_SCHEMA,
        handler=body.add_body_textbox,
        mutating=True,
    )
    server.register_tool(
        name="add_body_image",
        description=(
            "Add an image to the report body: image_source External (URL), "
            "Embedded (name of an embedded image) or Database (=Fields! "
            "expression, plus the required mime_type)."
        ),
        input_schema=_BODY_IMAGE_SCHEMA,
        handler=body.add_body_image,
        mutating=True,
    )
    server.register_tool(
        name="remove_body_item",
        description=(
            "Remove a named item (textbox, image, tablix, chart, ...) from the report body, "
            "also inside a rectangle or list, with everything in it (listed in also_removed). "
            "Refuses while other items reference it unless force."
        ),
        input_schema=_NAMED_REMOVE_SCHEMA,
        handler=body.remove_body_item,
        mutating=True,
    )

    server.register_tool(
        name="insert_tablix_from_template",
        description=(
            "Add a table to the body bound to a dataset: one column per entry in "
            "`columns` (a field name, or {field, header} for a caption); a header "
            "row plus a detail row of =Fields!X.Value. Every field must exist in "
            "the dataset. width is split evenly over the columns (set_column_width "
            "after)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "dataset_name": {"type": "string"},
                "columns": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "field": {"type": "string"},
                                    "header": {"type": "string"},
                                },
                                "required": ["field"],
                                "additionalProperties": False,
                            },
                        ]
                    },
                    "minItems": 1,
                },
                "top": {"type": "string"},
                "left": {"type": "string"},
                "width": {"type": "string"},
                "height": {"type": "string"},
                "container": _CONTAINER_PARAM,
                "repeat_header": {
                    "type": "boolean",
                    "default": True,
                    "description": "Repeat the header row on every page (RepeatOnNewPage).",
                },
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
        mutating=True,
    )
    server.register_tool(
        name="insert_chart_from_template",
        description=(
            "Add a chart to the body bound to a dataset: a column chart of "
            "Sum(value_field) by category_field. Change its type with "
            "set_chart_series_type (bar, line, area, pie = Shape/Pie, doughnut, "
            "scatter, ...)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "name": {"type": "string"},
                "dataset_name": {"type": "string"},
                "category_field": {"type": ["string", "null"], "default": None},
                "value_field": {"type": ["string", "null"], "default": None},
                "top": {"type": "string"},
                "left": {"type": "string"},
                "width": {"type": "string"},
                "height": {"type": "string"},
                "container": _CONTAINER_PARAM,
                "category_expression": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "=expression instead of category_field.",
                },
                "value_expression": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Whole Y =expression instead of value_field.",
                },
            },
            "required": [
                "path",
                "name",
                "dataset_name",
                "top",
                "left",
                "width",
                "height",
            ],
            "additionalProperties": False,
        },
        handler=chart.insert_chart_from_template,
        mutating=True,
    )
    server.register_tool(
        name="add_chart_series",
        description=(
            "Add a series to a chart: =Sum(Fields!value_field.Value), type "
            "default Column (mix types for a combo chart, e.g. bars + a line). "
            "series_subtype must be a real RDL pair (see set_chart_series_type; "
            "shorthands like Pie are normalised). Refuses a name in use."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "series_name": {"type": "string"},
                "value_field": {"type": ["string", "null"], "default": None},
                "value_expression": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Whole Y =expression instead of value_field.",
                },
                "series_type": {
                    "type": "string",
                    "default": "Column",
                    "description": chart.SERIES_TYPE_DOC,
                },
                "series_subtype": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": chart.SERIES_SUBTYPE_DOC,
                },
            },
            "required": ["path", "chart_name", "series_name"],
            "additionalProperties": False,
        },
        handler=chart.add_chart_series,
        mutating=True,
    )
    server.register_tool(
        name="remove_chart_series",
        description=(
            "Remove a series from a chart (with its legend member). Refuses the "
            "last series (use remove_body_item for the chart) and a series a "
            "derived series uses unless cascade=true."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "series_name": {"type": "string"},
                "cascade": {"type": "boolean", "default": False},
            },
            "required": ["path", "chart_name", "series_name"],
            "additionalProperties": False,
        },
        handler=chart.remove_chart_series,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_series_type",
        description=(
            "Change a chart series' type: Column, Bar, Line, Area, Shape (pie, "
            "doughnut, funnel, pyramid), Scatter (bubble), Range, Polar (radar), "
            "with a subtype such as Stacked, PercentStacked or Smooth. Shorthands "
            "like Pie or SmoothLine are normalised. Set per series for a combo "
            "chart."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "series_name": {"type": "string"},
                "series_type": {
                    "type": "string",
                    "description": chart.SERIES_TYPE_DOC,
                },
                "series_subtype": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": chart.SERIES_SUBTYPE_DOC,
                },
            },
            "required": ["path", "chart_name", "series_name", "series_type"],
            "additionalProperties": False,
        },
        handler=chart.set_chart_series_type,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_series_grouping",
        description=(
            "Split a chart series into one series per value of a field (dynamic "
            "series), e.g. one line per Region: group_field or group_expression. "
            "The legend label becomes the group value (or label_expression), "
            "sorted by sort. replace=true changes an existing grouping's "
            "expression and keeps its group name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "series_name": {"type": "string"},
                "group_field": {"type": ["string", "null"], "default": None},
                "group_expression": {"type": ["string", "null"], "default": None},
                "replace": {"type": "boolean", "default": False},
                "label_expression": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Legend text per fanned-out series; default: the "
                    "group expression.",
                },
                "sort": {
                    "type": ["string", "null"],
                    "enum": ["Ascending", "Descending", "None", None],
                    "default": None,
                    "description": "Sort the series by the group expression. Default: "
                    "Ascending for a new group, unchanged on replace. 'None' removes "
                    "the sort.",
                },
                "group_name": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Name of a new group (default <series>_Group); "
                    "with replace=true, renames the existing group.",
                },
            },
            "required": ["path", "chart_name", "series_name"],
            "additionalProperties": False,
        },
        handler=chart.set_chart_series_grouping,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_axis",
        description=(
            "Format a chart axis (Category or Value): title, number/date format, "
            "min/max, interval, log scale, visible. '' clears a field. axis_name "
            "defaults to 'Primary'; chart_area to the first area."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "axis": {
                    "type": "string",
                    "description": "Category or Value",
                    "enum": ["Category", "Value"],
                },
                "axis_name": {"type": ["string", "null"], "default": None},
                "chart_area": {"type": ["string", "null"], "default": None},
                "title": {"type": "string"},
                "format": {
                    "type": "string",
                    "description": "Numeric/date format, e.g. '#,0.00' or 'MMM yyyy'.",
                },
                "min": {"type": "string", "description": "Number, NaN (auto) or =expression."},
                "max": {"type": "string"},
                "log_scale": {"type": "boolean"},
                "interval": {"type": "string"},
                "visible": {"oneOf": [{"type": "boolean"}, {"type": "string", "enum": ["Auto"]}]},
            },
            "required": ["path", "chart_name", "axis"],
            "additionalProperties": False,
        },
        handler=chart.set_chart_axis,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_legend",
        description=(
            "Show/hide a chart legend and set its position (TopLeft ... "
            "BottomRight, e.g. BottomCenter)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "legend_name": {"type": ["string", "null"], "default": None},
                "position": {"type": "string"},
                "visible": {"type": "boolean"},
            },
            "required": ["path", "chart_name"],
            "additionalProperties": False,
        },
        handler=chart.set_chart_legend,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_data_labels",
        description=(
            "Show/hide and format data labels on one series or all: visible (or "
            "an =expression), number format, position, use_value_as_label, font "
            "weight and color; '' clears. Written on the series' data point, "
            "where Report Builder keeps them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "series_name": {"type": "string"},
                "visible": {"type": "boolean"},
                "format": {"type": "string"},
                "visible_expression": {"type": "string"},
                "position": {
                    "type": "string",
                    "enum": list(chart.DATA_LABEL_POSITIONS),
                    "description": "Outside is for Shape series (pie, doughnut, funnel).",
                },
                "use_value_as_label": {"type": "boolean"},
                "font_weight": {"type": "string"},
                "color": {"type": "string"},
            },
            "required": ["path", "chart_name"],
            "additionalProperties": False,
        },
        handler=chart.set_chart_data_labels,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_palette",
        description=(
            "Set a chart's colour palette (a built-in such as Pastel or Pacific, "
            "or Custom with brand colours in colors) and hatching; '' clears."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "palette": {"type": "string", "enum": [*_chart_types.PALETTES, ""]},
                "colors": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "minItems": 1,
                    "default": None,
                    "description": "Custom palette colours, in order (name, #hex, =expr).",
                },
                "hatch_behavior": {
                    "type": ["string", "null"],
                    "enum": [*_chart_types.PALETTE_HATCH_BEHAVIORS, "", None],
                    "default": None,
                },
            },
            "required": ["path", "chart_name", "palette"],
            "additionalProperties": False,
        },
        handler=chart.set_chart_palette,
        mutating=True,
    )
    server.register_tool(
        name="set_series_color",
        description=(
            "Set one chart series' colour (name, #hex or =expression), overriding "
            "the palette; '' clears."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "series_name": {"type": "string"},
                "color": {"type": "string"},
                "target": {
                    "type": ["string", "null"],
                    "enum": [*chart.SERIES_COLOR_TARGETS, None],
                    "default": None,
                    "description": "Default: point if a data point has a colour, else series.",
                },
            },
            "required": ["path", "chart_name", "series_name", "color"],
            "additionalProperties": False,
        },
        handler=chart.set_series_color,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_title",
        description=("Set a chart's title text (or =expression)."),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "text": {"type": "string"},
                "title_name": {"type": ["string", "null"], "default": None},
            },
            "required": ["path", "chart_name", "text"],
            "additionalProperties": False,
        },
        handler=chart.set_chart_title,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_series_properties",
        description=(
            "Set a chart series' legend entry, value/category axis (a missing one, "
            "e.g. 'Secondary', is created opposite), chart area, hidden, order, and "
            "point tooltip, marker and empty points. '' clears."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "series_name": {"type": "string"},
                "legend_text": {"type": "string"},
                "hide_in_legend": {"type": "boolean"},
                "legend_name": {"type": "string"},
                "value_axis": {"type": "string"},
                "category_axis": {"type": "string"},
                "chart_area": {"type": "string"},
                "hidden": {"type": "boolean"},
                "position": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Move the series (and legend entry) to this index.",
                },
                "tooltip": {"type": "string"},
                "marker_type": {"type": "string", "enum": [*chart.chart_types.MARKER_TYPES, ""]},
                "marker_size": {"type": "string"},
                "empty_point_value": {
                    "type": "string",
                    "enum": [*chart.chart_types.EMPTY_POINT_VALUES, ""],
                },
                "empty_point_color": {"type": "string"},
            },
            "required": ["path", "chart_name", "series_name"],
            "additionalProperties": False,
        },
        handler=chart.set_chart_series_properties,
        mutating=True,
    )
    server.register_tool(
        name="set_chart_category_group",
        description=(
            "Set a chart category group's label (e.g. =Format(Fields!D.Value,"
            '"MMM")), sort or field; add_level=inner|outer adds a nested category '
            "level on group_field or group_expression."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "chart_name": {"type": "string"},
                "group_name": {"type": "string"},
                "label_expression": {"type": "string"},
                "sort": {"type": "string", "enum": ["Ascending", "Descending", "None"]},
                "sort_expression": {"type": "string"},
                "add_level": {"type": "string", "enum": ["inner", "outer"]},
                "group_field": {"type": "string"},
                "group_expression": {"type": "string"},
            },
            "required": ["path", "chart_name"],
            "additionalProperties": False,
        },
        handler=chart.set_chart_category_group,
        mutating=True,
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
            "Set a parameter's dropdown values: source='static' with values "
            "(strings or {value, label}), or source='query' from a lookup dataset "
            "(also how cascading parameters are wired). Static with no values "
            "clears the list."
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
        mutating=True,
    )
    server.register_tool(
        name="set_parameter_default_values",
        description=(
            "Set a parameter's default value(s): source='static' with values, or "
            "source='query' from a dataset field. Static with no values clears "
            "the default."
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
        mutating=True,
    )

    server.register_tool(
        name="update_parameter_advanced",
        description=(
            "Set a parameter's flags: multi_value, hidden, allow_null, "
            "allow_blank (each optional). Cascading parameters use "
            "set_parameter_available_values(source='query')."
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
        mutating=True,
    )

    server.register_tool(
        name="add_embedded_image",
        description=(
            "Embed an image file (logo) in the report from disk (bmp, gif, jpeg, "
            "png; must be a complete image of mime_type). Then use it with "
            "image_source='Embedded', value=name."
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
                "warn_above_bytes": {"type": "integer", "minimum": 0},
            },
            "required": ["path", "name", "mime_type", "image_path"],
            "additionalProperties": False,
        },
        handler=embedded_images.add_embedded_image,
        mutating=True,
    )
    server.register_tool(
        name="list_embedded_images",
        description=("List the report's embedded images: name and MIME type."),
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
            "Remove an embedded image. Refuses while an image, a background image or "
            "an image expression still uses it, unless force=true."
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
        mutating=True,
    )

    server.register_tool(
        name="set_alternating_row_color",
        description=(
            "Zebra-stripe a tablix's detail rows: alternating background colours "
            "color_a / color_b on every detail cell (replaces their background "
            "colour)."
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
        mutating=True,
    )
    server.register_tool(
        name="set_conditional_row_color",
        description=(
            "Colour a tablix's detail rows by a field's value: color_map {value: "
            "colour} (first match wins, else default_color, or base_color "
            "'zebra:<a>,<b>' to keep striping) becomes a Switch() "
            "background on every detail cell. Case-insensitive unless "
            "case_sensitive=true."
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
                "base_color": {
                    "type": "string",
                    "description": (
                        "Fallback instead of default_color: 'zebra:<a>,<b>' keeps "
                        "striping, or an =expression."
                    ),
                },
            },
            "required": ["path", "tablix_name", "value_expression", "color_map"],
            "additionalProperties": False,
        },
        handler=styling.set_conditional_row_color,
        mutating=True,
    )

    server.register_tool(
        name="set_element_visibility",
        description=(
            "Hide or show any named report item (tablix, textbox, image, "
            "rectangle, chart, ...): hidden_expression true/false or =expression, "
            "optional toggle_textbox (drill-down), clear=true removes visibility. "
            "Groups and detail rows have their own tools."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "element_name": {"type": "string"},
                "hidden_expression": {
                    "type": "string",
                    "description": "'true' / 'false' or an =expression.",
                },
                "toggle_textbox": {
                    "type": ["string", "null"],
                    "description": "Omit to keep the existing toggle; '' or null removes it.",
                },
                "clear": {
                    "type": "boolean",
                    "description": "Remove <Visibility> entirely (default false).",
                },
            },
            "required": ["path", "element_name"],
            "additionalProperties": False,
        },
        handler=visibility.set_element_visibility,
        mutating=True,
    )


__all__ = ["register_all_tools"]
