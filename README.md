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





cd /Users/muhammadafaq/Documents/research/pbirb-mcp

# 1. Tests
.venv/bin/python -m pytest tests/ -v

# 2. JSON-RPC over stdio
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' | .venv/bin/pbirb-mcp

# 3. Same but via uvx (validates packaging works the same way users will install)
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n' | uvx --from . pbirb-mcp

# 4. Open the fixture in Power BI Report Builder — confirm it loads with no
#    "upgrade?" prompt and the table renders. This validates that my hand-
#    written fixture is actually a viable PBI paginated RDL before I build
#    feature tools on top of it.
open tests/fixtures/pbi_paginated_minimal.rdl

# 5. Round-trip identity check
.venv/bin/python -c "
from pathlib import Path; import shutil, tempfile
from pbirb_mcp.core.document import RDLDocument
src = Path('tests/fixtures/pbi_paginated_minimal.rdl')
tmp = Path(tempfile.mkdtemp())/'rt.rdl'; shutil.copy(src, tmp)
RDLDocument.open(tmp).save()
print('byte-identical:', tmp.read_bytes() == src.read_bytes())
"