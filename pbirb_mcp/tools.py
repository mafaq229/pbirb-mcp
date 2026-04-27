"""Tool registry — wires ops modules into the JSON-RPC server.

Each ops module exposes plain Python functions; this module describes their
JSON Schema and registers them with an :class:`MCPServer`. Adding a new tool
means appending one entry here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pbirb_mcp.ops import dataset, datasource, reader

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


def register_all_tools(server: "MCPServer") -> None:
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


__all__ = ["register_all_tools"]
