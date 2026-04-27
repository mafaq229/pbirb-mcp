# pbirb-mcp

MCP server for editing **Power BI Report Builder** paginated reports (`.rdl`).

Inspired by [bethmaloney/rdl-mcp](https://github.com/bethmaloney/rdl-mcp), but
scoped to Power BI paginated reports (XMLA-backed datasets, DAX queries) and
expanded to cover the gaps that require hand-written XML in the upstream
project: dataset/tablix filters, headers/footers, body content, groupings,
sorting, row visibility, conditional expressions, styling, page setup, advanced
parameters, and embedded images.

## Status

Bootstrap. The server speaks JSON-RPC 2.0 over stdio and registers no tools yet.
Feature commits land each gap as its own tool.

## Install (local dev)

```bash
uv pip install -e .
pbirb-mcp  # JSON-RPC stdio loop
```

Or via `uvx` against this checkout:

```bash
uvx --from . pbirb-mcp
```

## Test

```bash
python3 -m pytest tests/ -v
```

## Logging

- `PBIRB_MCP_LOG_LEVEL` — `DEBUG` | `INFO` | `WARNING` (default) | `ERROR`
- `PBIRB_MCP_LOG_FILE` — path to log file (default: stderr)
