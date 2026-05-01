# pbirb-mcp

[![CI](https://github.com/mafaq229/pbirb-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mafaq229/pbirb-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pbirb-mcp.svg)](https://pypi.org/project/pbirb-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/pbirb-mcp.svg)](https://pypi.org/project/pbirb-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An MCP server for editing **Power BI Report Builder paginated reports** (`.rdl`)
through Claude (Desktop, CLI, or any MCP client). Forty-plus tools cover the
gaps that otherwise force hand-written XML: dataset filters, headers and
footers, body composition, groupings, sorting, row visibility, conditional
expressions, styling, page setup, advanced parameters, and embedded images.

The server speaks JSON-RPC 2.0 over stdio. It opens an `.rdl` from disk,
mutates it in place via lxml, validates structure, and writes atomically — a
failed save never leaves a half-written report or scrubs the original.

## Stability

Pre-1.0. The tool surface — tool names, `inputSchema`, output shapes, error
semantics — is the contract. While on `0.x`, MINOR releases may include a
small breaking change with a migration note in
[CHANGELOG.md](CHANGELOG.md); after v1.0, breaking changes require MAJOR.
See [CONTRIBUTING.md](CONTRIBUTING.md#versioning) for the full bump rules
adapted from SemVer for an MCP tool surface.

Pin to a MINOR while on `0.x` (e.g. `pbirb-mcp~=0.1`) if your prompts
depend on specific tool names or schemas.

---

## Quick start

### 1. Install

The simplest path is [uv](https://docs.astral.sh/uv/) + PyPI — no clone, no
venv, no install step:

```bash
uvx pbirb-mcp
```

uvx fetches the package into a throwaway environment, runs the
`pbirb-mcp` console script, and exits. The MCP server speaks JSON-RPC
over stdio, so any MCP client (Claude Desktop, Claude Code, etc.) can
spawn it directly.

For local development against this codebase instead:

```bash
git clone https://github.com/mafaq229/pbirb-mcp
cd pbirb-mcp
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

Verify the binary works:

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n' \
  | .venv/bin/pbirb-mcp
```

You should see a single JSON-RPC response with `protocolVersion`,
`capabilities.tools`, and `serverInfo.name = "pbirb-mcp"`.

### 2. Wire into Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "pbirb": {
      "command": "uvx",
      "args": ["pbirb-mcp"],
      "env": {
        "PBIRB_MCP_LOG_LEVEL": "INFO",
        "PBIRB_MCP_LOG_FILE": "/tmp/pbirb-mcp.log"
      }
    }
  }
}
```

Restart Claude Desktop. The hammer icon should show `pbirb` and the 40+ tools
listed below.

For development against a local checkout, swap the `args` for
`["--from", "/absolute/path/to/pbirb-mcp", "pbirb-mcp"]` so uvx runs
your working tree instead of the published version.

### 3. Wire into Claude Code

```bash
claude mcp add pbirb -- uvx pbirb-mcp
```

Or add to `.mcp.json` at the workspace root:

```json
{
  "mcpServers": {
    "pbirb": {
      "command": "uvx",
      "args": ["pbirb-mcp"]
    }
  }
}
```

### 4. Smoke test

In any chat, ask the model:

> Open `~/path/to/some-report.rdl` and tell me what datasets and parameters it has.

The model should call `describe_report`, `get_datasets`, and `get_parameters`
and summarise the result. If it doesn't, your client probably hasn't loaded
the server — check the client's MCP debug log.

---

## Working with a report

The recommended flow is **read first, then edit by name**. Every mutating
tool addresses elements by their RDL `Name` attribute (Tablix name,
Textbox name, ReportParameter name, DataSet name) — never by index.
Indices break across multi-step edits.

A typical session:

```text
You:    Bind the existing report at ~/Reports/sales.rdl to the Finance
        workspace's "FY26 Sales" dataset, change the body DAX to a date-
        ranged query, and add a header with a logo and a parameter line.

Claude: → describe_report(path=…)
        → set_datasource_connection(name="PBIDataset",
              workspace_url="Finance", dataset_name="FY26 Sales")
        → update_dataset_query(dataset_name="MainDataset",
              dax_body="EVALUATE FILTER('Sales',
                'Sales'[OrderDate] >= @DateFrom &&
                'Sales'[OrderDate] <= @DateTo)")
        → add_query_parameter(dataset_name="MainDataset",
              name="DateFrom",
              value_expression="=Parameters!DateFrom.Value")
        → add_query_parameter(dataset_name="MainDataset",
              name="DateTo",
              value_expression="=Parameters!DateTo.Value")
        → add_embedded_image(name="Logo",
              mime_type="image/png", image_path="~/brand/logo.png")
        → set_page_header(height="0.75in",
              print_on_first_page=True, print_on_last_page=True)
        → add_header_image(name="HeaderLogo",
              image_source="Embedded", value="Logo",
              top="0in", left="0in", width="1in", height="0.5in")
        → add_header_textbox(name="DateLine",
              text='="From: " & Parameters!DateFrom.Value & "  To: "
                    & Parameters!DateTo.Value',
              top="0.1in", left="1.2in", width="4in", height="0.25in")
```

Open the resulting `.rdl` in Report Builder; the change is visible in the
designer and renders in Preview against the bound dataset.

---

## Tool reference

42 tools, grouped by RDL concern. Every tool takes a `path` argument
(absolute path to the `.rdl`); the rest of the schema is in `tools/list`
output and visible to the LLM at registration time.

### Read-only inventory

The "what's in this report?" tools. Always the first calls when planning a
multi-step edit.

| Tool | Returns |
|------|---------|
| `describe_report` | Top-level inventory: data sources, datasets, parameters, tablixes, page setup |
| `get_datasets` | Full DAX command text, fields, query parameters, dataset filters |
| `get_parameters` | Report parameters with data type, prompt, and flags (multi-value, hidden, nullable, allow-blank) |
| `get_tablixes` | Tablix layout: columns, row/column groups, sort expressions, filters, visibility |
| `list_tablix_filters` | Filters on a tablix in document order with stable indices |
| `list_embedded_images` | Embedded image names + MIME types |

### Datasource & dataset

| Tool | What it edits |
|------|---------------|
| `set_datasource_connection` | Repoint a `<DataSource>` at a Power BI XMLA endpoint. `DataProvider=SQL` (the AS provider id). |
| `update_dataset_query` | Replace `<DataSet>/<Query>/<CommandText>` with a DAX expression |
| `add_query_parameter` | Append `<QueryParameter>` (e.g. `=Parameters!DateFrom.Value`) |
| `update_query_parameter` | Change the value expression of an existing query parameter |
| `remove_query_parameter` | Drop a query parameter (and clean up empty `<QueryParameters>`) |

### Tablix

| Tool | What it edits |
|------|---------------|
| `add_tablix_filter` | Append a `<Filter>`. Operators: Equal, NotEqual, GreaterThan, In, Between, Like, TopN, ... |
| `remove_tablix_filter` | Remove by ordinal index from `list_tablix_filters` |
| `add_row_group` | Wrap the current row hierarchy in a new outer group + insert a header row |
| `remove_row_group` | Inverse of `add_row_group` |
| `set_group_sort` | Replace `<SortExpressions>` on a group |
| `set_group_visibility` | Set `<Visibility>` on a group's TablixMember |
| `set_detail_row_visibility` | Set `<Visibility>` on the Details group |
| `set_row_height` | Set `<Height>` on the Nth body row |

### Page

| Tool | What it edits |
|------|---------------|
| `set_page_setup` | Page dimensions, margins, columns. All fields optional. |
| `set_page_orientation` | Swap PageHeight/PageWidth to match `Portrait` or `Landscape`. Idempotent. |

### Page header & footer

Same set of operations for each region, each accepts named items so
follow-up edits don't drift on indices.

| Tool | What it edits |
|------|---------------|
| `set_page_header` / `set_page_footer` | Section height + `PrintOnFirstPage` / `PrintOnLastPage` |
| `add_header_textbox` / `add_footer_textbox` | Append a Textbox (static text or `=expression`) |
| `add_header_image` / `add_footer_image` | Append an Image (External URL, Embedded name, or Database expression) |
| `remove_header_item` / `remove_footer_item` | Remove by name; tidies empty `<ReportItems>` |

### Body composition

| Tool | What it edits |
|------|---------------|
| `add_body_textbox` | Append a Textbox to `<Body>/<ReportItems>` |
| `add_body_image` | Append an Image to the body |
| `remove_body_item` | Remove a named Textbox / Image / Tablix from the body |

### Snippet templates

Single-call inserts of common report items, programmatically built and
appended to the body.

| Tool | What it builds |
|------|----------------|
| `insert_tablix_from_template` | A basic Tablix mirroring the fixture's shape — header row with the column name as a static label, detail row binding to `=Fields!<column>.Value`. One column per requested field. |
| `insert_chart_from_template` | A basic Column chart: single category axis grouped by `category_field`, single Y series `=Sum(Fields!<value_field>.Value)`. Change `<Type>` post-insert (Bar / Line / Pie / etc.). |

### Styling

| Tool | What it edits |
|------|---------------|
| `set_textbox_style` | Routes properties to the right nested `<Style>` node automatically: box-level (BackgroundColor, Border, VerticalAlign), paragraph-level (TextAlign), run-level (FontFamily, FontSize, FontWeight, Color, Format) |
| `set_alternating_row_color` | Zebra-stripe a tablix's detail row with `BackgroundColor=IIf(RowNumber(Nothing) Mod 2, "<a>", "<b>")` |

### Visibility

| Tool | What it edits |
|------|---------------|
| `set_element_visibility` | Set `<Visibility>` on any named ReportItem (Tablix, Textbox, Image, Rectangle, Subreport, Chart). Group / detail-row visibility have their own tools. |

### Parameters (advanced)

| Tool | What it edits |
|------|---------------|
| `set_parameter_available_values` | Static `<ParameterValues>` list (strings or `{value, label}` dicts) **or** `<DataSetReference>` to a lookup dataset |
| `set_parameter_default_values` | Static `<Values>` list **or** `<DataSetReference>` (defaults take ValueField only — defaults are values, not display strings) |
| `update_parameter_advanced` | Toggle the four boolean flags: `multi_value`, `hidden`, `allow_null` (writes `<Nullable>`), `allow_blank` |

#### Cascading parameters

RDL has no `<DependsOn>` element — cascading is inferred from
`=Parameters!X.Value` references in a lookup dataset's `<QueryParameters>`.
To wire parameter `B` to depend on parameter `A`:

1. `set_parameter_available_values(name="B", source="query", query_dataset="LookupB", ...)`
2. `add_query_parameter(dataset_name="LookupB", name="@A", value_expression="=Parameters!A.Value")`

Report Builder figures out the dependency graph by parsing those expressions.

### Embedded images

| Tool | What it edits |
|------|---------------|
| `add_embedded_image` | Read a real file off disk, base64-encode it, store under `<EmbeddedImages>` |
| `list_embedded_images` | Names + MIME types |
| `remove_embedded_image` | Remove by name; tidies empty `<EmbeddedImages>` |

Reference an embedded image with
`add_*_image(image_source="Embedded", value="<image-name>")`.

---

## Power BI specifics

### XMLA connection strings

`set_datasource_connection` writes the canonical form:

```
Data Source=powerbi://api.powerbi.com/v1.0/myorg/<workspace>;Initial Catalog=<dataset>
```

`workspace_url` accepts a bare workspace name (`Finance`) or a full
`powerbi://` URL — the tool detects the latter and avoids double-prefixing.
`DataProvider` is set to `SQL` (the Analysis Services provider id RDL uses
for PBI XMLA, despite the misleading name).

### DAX queries

DAX bodies are accepted verbatim — `pbirb-mcp` doesn't parse DAX, so the
user (or Report Builder at preview time) is the source of truth for syntax.
Empty bodies are rejected up front because Report Builder loads them but
errors at preview, which is a worse signal than a clear `ValueError` here.

PBI paginated reports do **not** carry `<CommandType>` for DAX (unlike
SSRS where you'd set `CommandType=StoredProcedure`); these tools never
emit it.

### Pre-commit hooks (contributors)

`pre-commit` is in the `[dev]` extras. After a fresh checkout:

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pre-commit install                # one-time — installs the git hook
.venv/bin/pre-commit run --all-files        # one-time — clean any drift
```

After the hook is installed, every `git commit` runs ruff format +
ruff check (with `--fix`) + the fast pytest suite. If lint or tests
fail the commit is aborted; fix and re-stage before retrying.

To run individual hooks ad-hoc:

```bash
.venv/bin/pre-commit run ruff --all-files
.venv/bin/pre-commit run ruff-format --all-files
.venv/bin/pre-commit run pytest-fast --all-files
```

The full config lives in `.pre-commit-config.yaml`. Ruff settings
(line length, rule selection, per-file ignores) live under
`[tool.ruff*]` in `pyproject.toml`.

### Report Builder install

Power BI Report Builder is a free Microsoft-distributed Windows app:

> https://www.microsoft.com/en-us/download/details.aspx?id=105942

Open any `.rdl` produced by `pbirb-mcp` directly in Report Builder. The
"opens cleanly with no upgrade prompt" check is the actual integration
test — the unit tests verify schema correctness, but lxml will
round-trip XML that Report Builder's deserialiser still rejects (we hit
this twice in the chart-template work; both fixes are documented in the
git history).

---

## Architecture

```
pbirb-mcp/
├── pbirb_mcp_server.py         # Entry point (logging + main())
├── pbirb_mcp/
│   ├── server.py               # JSON-RPC stdio dispatch
│   ├── tools.py                # Tool registry — wires ops into the server
│   ├── core/
│   │   ├── document.py         # RDLDocument: open/save (lxml), atomic write
│   │   ├── xpath.py            # Namespace-aware XPath helpers
│   │   ├── ids.py              # Stable element addressing
│   │   └── schema.py           # Structural + opt-in XSD validation
│   ├── ops/
│   │   ├── reader.py           # describe / get_datasets / get_params / get_tablixes
│   │   ├── datasource.py       # PBI XMLA connection authoring
│   │   ├── dataset.py          # DAX body + query parameters
│   │   ├── tablix.py           # Filters, groupings, sort, visibility, height
│   │   ├── page.py             # Page setup + orientation
│   │   ├── header_footer.py    # Page header / footer authoring
│   │   ├── body.py             # Body textboxes / images / removal
│   │   ├── templates.py        # Chart + tablix snippet builders
│   │   ├── styling.py          # set_textbox_style + alternating row color
│   │   ├── visibility.py       # Element-level visibility
│   │   ├── parameters.py       # Available / default values, advanced flags
│   │   └── embedded_images.py  # Base64 image embedding
│   └── schemas/                # (Empty — drop the official RDL XSD here for opt-in XSD validation)
└── tests/
    ├── fixtures/
    │   └── pbi_paginated_minimal.rdl  # Hand-tuned to match Report Builder's emitted style
    └── test_*.py               # 270+ tests — every commit's tool plus round-trip invariants
```

### Hard rules (enforced by tests)

- **Tests first.** Every commit writes failing tests, then makes them pass.
- **lxml, not stdlib `xml.etree`.** Round-trip fidelity is a feature, not
  polish. Report Builder reads what's on disk; formatting drift causes
  silent corruption.
- **Stable IDs, never indices.** Tools take `tablix_name` + `group_name`,
  not `column_index: 2`. Indices break across multi-step edits.
- **Atomic save.** `RDLDocument.save_as` writes to `<path>.tmp` then renames.
  A failure mid-write never leaves a half-written report.
- **Round-trip byte-identity is enforced** by
  `tests/test_document.py`'s `test_round_trip_byte_identical_to_fixture`.
  A no-op open → save → reopen produces a byte-identical file.

### RDL gotchas learned the hard way

- `<?xml version="1.0" encoding="utf-8"?>` uses **double quotes**, not
  lxml's default single quotes. Fixed in `RDLDocument.save_as` via a
  manual declaration.
- Self-closing tags use `<Tag />` with a space, not `<Tag/>`. Fixed via
  a post-process regex.
- The `rd:` prefix
  (`http://schemas.microsoft.com/SQLServer/reporting/reportdesigner`)
  carries designer metadata Report Builder relies on. Don't strip it;
  preserve prefixes.
- Do **not** put `MustUnderstand="df"` on `<Report>` unless you also
  declare `xmlns:df=...`.
- `<ChartCategoryAxes>` / `<ChartValueAxes>` hold `<ChartAxis>`
  children **directly** — there is no `<ChartCategoryAxis>` /
  `<ChartValueAxis>` wrapper.
- `<ChartMember>` requires a `<Label>` child, even an empty one.
- DAX queries live in
  `<DataSet><Query><CommandText>EVALUATE ...</CommandText></Query></DataSet>`.
  No `<CommandType>` element for DAX (unlike SSRS).

---

## Logging

Two environment variables control the logger:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PBIRB_MCP_LOG_LEVEL` | `WARNING` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `PBIRB_MCP_LOG_FILE` | stderr | Path to a log file; otherwise logs go to stderr (where Claude Desktop captures them in its MCP debug pane) |

```bash
PBIRB_MCP_LOG_LEVEL=DEBUG PBIRB_MCP_LOG_FILE=/tmp/pbirb-mcp.log pbirb-mcp
```

---

## Development

### Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

The suite is fast (~0.3s for 270+ tests) so re-run on every change.

### Smoke testing the live binary

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | .venv/bin/pbirb-mcp
```

For an end-to-end sanity check, drive an actual mutation against a copy of
the bundled fixture:

```bash
SCRATCH=$(mktemp -d)/r.rdl
cp tests/fixtures/pbi_paginated_minimal.rdl "$SCRATCH"
.venv/bin/python -c "
from pbirb_mcp.ops.dataset import update_dataset_query
update_dataset_query(path='$SCRATCH', dataset_name='MainDataset',
    dax_body=\"EVALUATE TOPN(10, 'Sales')\")
print('Wrote', '$SCRATCH')
"
```

Open the resulting file in Power BI Report Builder. **Manual verification
that an `.rdl` opens cleanly is the actual integration test** — the unit
tests catch schema-level mistakes, but only Report Builder catches
deserialiser nits.

### Adding a new tool

1. Write tests first under `tests/test_*.py`.
2. Implement in the appropriate `pbirb_mcp/ops/*.py` module (or create a
   new one).
3. Register in `pbirb_mcp/tools.py` with a clear `description` and
   strict `inputSchema`.
4. Run the full suite and a JSON-RPC smoke against the fixture.
5. Open the modified RDL in Report Builder.

The commit-by-commit history shows the pattern in practice.

---

## Releases

[CHANGELOG.md](CHANGELOG.md) tracks every release in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. Releases
are also published as
[GitHub Releases](https://github.com/mafaq229/pbirb-mcp/releases) and to
[PyPI](https://pypi.org/project/pbirb-mcp/).

Versions follow SemVer adapted for an MCP tool surface — see
[CONTRIBUTING.md § Versioning](CONTRIBUTING.md#versioning).

## Contributing

PRs welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers dev setup, the
hard rules (tests-first, lxml only, stable IDs, atomic save,
byte-identity round-trip, smoke in Report Builder), the SemVer-for-MCP
bump table, and the PR review checklist.

Bug reports and tool proposals: please use the
[issue templates](https://github.com/mafaq229/pbirb-mcp/issues/new/choose).
For security issues, see [SECURITY.md](SECURITY.md). All participants
are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Acknowledgments

The existing [bethmaloney/rdl-mcp](https://github.com/bethmaloney/rdl-mcp)
server pioneered the MCP-over-RDL pattern but is scoped to SSRS: column
metadata, basic parameter management, stored-procedure swap. Power BI
paginated reports use the same RDL 2016 schema as SSRS, but:

1. Data sources are **Power BI XMLA endpoints**, not SQL Server.
2. Queries are **DAX**, and the upstream tool exposes no body-edit (only
   stored-procedure name swap, useless here).
3. Report Builder is **picky about XML round-tripping** — formatting drift,
   namespace prefix loss, or unrecognised `MustUnderstand` attributes cause
   silent corruption or "this report needs to be upgraded" prompts.

`pbirb-mcp` is built around lxml so a no-op edit produces a byte-identical
file, addresses every element by stable name (never index), and treats
"opens cleanly in Report Builder" as the actual integration test.
