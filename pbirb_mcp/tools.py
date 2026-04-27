"""Tool registry — wires ops modules into the JSON-RPC server.

Each ops module exposes plain Python functions; this module describes their
JSON Schema and registers them with an :class:`MCPServer`. Adding a new tool
means appending one entry here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pbirb_mcp.ops import reader

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


__all__ = ["register_all_tools"]
