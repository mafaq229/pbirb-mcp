# pbirb-mcp

[![CI](https://github.com/mafaq229/pbirb-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mafaq229/pbirb-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pbirb-mcp.svg)](https://pypi.org/project/pbirb-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/pbirb-mcp.svg)](https://pypi.org/project/pbirb-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An MCP server for editing **Power BI Report Builder paginated reports** (`.rdl`)
through Claude (Desktop, CLI, or any MCP client). 140+ tools cover the gaps
that otherwise force hand-written XML: report creation, data sources and
datasets, calculated fields, dataset and tablix filters, groupings (row,
column, matrix), sorting, charts, headers and footers, body composition,
layout containers, positioning, styling, page setup, pagination, advanced
parameters, embedded images, interactivity (actions, tooltips, document
map), transactions, and validation.

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
      "args": ["pbirb-mcp"]
    }
  }
}
```

Restart Claude Desktop. The hammer icon should show `pbirb` and the 140+ tools
listed below.

To enable file logging, add an `env` block — but keep it platform-appropriate.
`PBIRB_MCP_LOG_FILE` takes an OS-native path: a Unix path like
`/tmp/pbirb-mcp.log` only works on macOS/Linux. On Windows use a Windows path
(e.g. `%TEMP%\\pbirb-mcp.log`). See [Logging](#logging). When unset, logs go to
stderr, which Claude Desktop captures in its MCP debug pane on every platform.

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

**Or install it as a Claude Code plugin** — one command instead of editing
config by hand (it wires up the same `uvx pbirb-mcp` server for you):

```bash
/plugin marketplace add mafaq229/pbirb-mcp
/plugin install pbirb-mcp@pbirb
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

143 tools, grouped by RDL concern. The highlights of each group are tabled
below; the authoritative, always-current list with full schemas is the
server's `tools/list` output, visible to the LLM at registration time. Every
tool takes a `path` argument (absolute path to the `.rdl`).

Most mutating tools also accept an optional `transaction_id` so a multi-step
edit batches into a single atomic save — see [Transactions](#transactions--validation)
and [docs/TRANSACTIONS.md](docs/TRANSACTIONS.md).

### Read-only inventory

The "what's in this report?" tools. Always the first calls when planning a
multi-step edit.

| Tool | Returns |
|------|---------|
| `describe_report` | Top-level inventory: data sources, datasets, parameters, tablixes, page setup |
| `get_datasets` / `get_dataset` | Full DAX command text, fields, query parameters, dataset filters (all, or one by name) |
| `list_data_sources` / `get_data_source` | Data source inventory; one source's connection details |
| `get_parameters` | Report parameters with data type, prompt, and flags (multi-value, hidden, nullable, allow-blank) |
| `get_tablixes` | Tablix layout: columns, row/column groups, sort expressions, filters, visibility |
| `list_tablix_filters` / `list_dataset_filters` | Filters in document order with stable indices |
| `list_body_items` / `list_header_items` / `list_footer_items` | Named report items in each region |
| `get_textbox` / `get_image` / `get_rectangle` / `get_chart` | Full properties of a named report item |
| `list_embedded_images` / `get_embedded_image_data` | Embedded image names + MIME types; base64 bytes of one |
| `get_expression_reference` | Cheat-sheet of common RDL expression patterns (`count_where`, `sum_where`, `iif_format` helpers build these) |

### Datasource & dataset

| Tool | What it edits |
|------|---------------|
| `set_datasource_connection` | Repoint a `<DataSource>` at a Power BI XMLA endpoint. `DataProvider=SQL` (the AS provider id). |
| `add_data_source` / `remove_data_source` / `rename_data_source` | Manage `<DataSource>` elements |
| `update_dataset_query` | Replace `<DataSet>/<Query>/<CommandText>` with a DAX expression |
| `add_query_parameter` | Append `<QueryParameter>` (e.g. `=Parameters!DateFrom.Value`) |
| `update_query_parameter` | Change the value expression of an existing query parameter |
| `remove_query_parameter` | Drop a query parameter (and clean up empty `<QueryParameters>`) |
| `add_dataset_field` / `remove_dataset_field` | Manage `<Field>` entries on a dataset |
| `add_calculated_field` / `remove_calculated_field` | Manage `<Value>`-backed calculated fields |
| `refresh_dataset_fields` | Re-derive the `<Fields>` list from the query's column metadata |
| `add_dataset_filter` / `remove_dataset_filter` | Filters applied at the dataset level (vs. tablix) |

### Tablix

| Tool | What it edits |
|------|---------------|
| `add_tablix_filter` | Append a `<Filter>`. Operators: Equal, NotEqual, GreaterThan, In, Between, Like, TopN, ... |
| `remove_tablix_filter` | Remove by ordinal index from `list_tablix_filters` |
| `add_row_group` / `remove_row_group` | Wrap the row hierarchy in a new outer group + header row (and its inverse) |
| `add_column_group` / `remove_column_group` | Same, on the column axis |
| `convert_to_matrix` | Promote a table to a matrix (row + column groups) — see [docs/MATRIX-cookbook.md](docs/MATRIX-cookbook.md) |
| `set_tablix_corner` | Set the matrix corner cell text/expression |
| `set_group_sort` / `set_column_group_sort` | Replace `<SortExpressions>` on a group |
| `set_group_visibility` / `set_column_group_visibility` | Set `<Visibility>` on a group's TablixMember |
| `set_detail_row_visibility` | Set `<Visibility>` on the Details group |
| `add_tablix_column` / `remove_tablix_column` | Add/drop a column across the tablix grid |
| `add_static_row` / `add_static_column` | Insert a non-grouped row/column |
| `add_subtotal_row` / `add_subtotal_column` | Insert an aggregate row/column on a group |
| `set_cell_span` | Set `RowSpan` / `ColSpan` on a cell |
| `set_column_width` / `set_row_height` | Set `<Width>` / `<Height>` on the Nth column/row |
| `set_tablix_size` | Set the tablix's overall `<Width>` / `<Height>` |

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

### Charts

Refine a chart after `insert_chart_from_template` (or any existing `<Chart>`).

| Tool | What it edits |
|------|---------------|
| `add_chart_series` / `remove_chart_series` | Manage Y-axis `<ChartSeries>` |
| `set_chart_series_type` | Column / Bar / Line / Area / Pie / ... per series |
| `set_chart_series_grouping` | Category/series grouping expression |
| `set_chart_axis` | Category / value axis title, scale, format |
| `set_chart_legend` | Legend visibility and placement |
| `set_chart_data_labels` | Toggle and format data labels |
| `set_chart_title` | Chart title text/expression |
| `set_chart_palette` / `set_series_color` | Palette name; explicit per-series color |

### Styling

| Tool | What it edits |
|------|---------------|
| `set_textbox_style` | Routes properties to the right nested `<Style>` node automatically: box-level (BackgroundColor, Border, VerticalAlign), paragraph-level (TextAlign), run-level (FontFamily, FontSize, FontWeight, Color, Format) |
| `set_textbox_style_bulk` | Apply one style to many textboxes in a single call |
| `set_textbox_runs` / `set_textbox_value` | Rich multi-run paragraph content; or replace the value |
| `find_textboxes_by_style` / `find_textbox_by_value` | Locate textboxes to target follow-up edits |
| `style_tablix_row` | Style every cell of a tablix row at once (header / detail / footer) |
| `set_alternating_row_color` | Zebra-stripe a tablix's detail row with `BackgroundColor=IIf(RowNumber(Nothing) Mod 2, "<a>", "<b>")` |
| `set_conditional_row_color` | Drive detail-row `BackgroundColor` from an expression |
| `set_image_sizing` / `set_image_source` | Image `<Sizing>`; switch External / Embedded / Database source |

### Visibility

| Tool | What it edits |
|------|---------------|
| `set_element_visibility` | Set `<Visibility>` on any named ReportItem (Tablix, Textbox, Image, Rectangle, Subreport, Chart). Group / detail-row visibility have their own tools. |

### Layout containers

| Tool | What it builds |
|------|----------------|
| `add_rectangle` | A `<Rectangle>` container (group other items, control page breaks) |
| `add_list` | A list region (single-column tablix template) |
| `add_line` | A `<Line>` report item |

### Positioning & sizing

Move and resize named items in each region. Coordinates are RDL sizes
(`"1in"`, `"2.5cm"`, ...).

| Tool | What it edits |
|------|---------------|
| `set_body_item_position` / `set_header_item_position` / `set_footer_item_position` | `Top` / `Left` of a named item |
| `set_body_item_size` / `set_header_item_size` / `set_footer_item_size` | `Width` / `Height` of a named item |
| `set_body_size` | The `<Body>` region's overall height |

### Interactivity

| Tool | What it edits |
|------|---------------|
| `set_textbox_action` / `set_image_action` / `set_chart_series_action` | `<Action>`: hyperlink, drill-through, or bookmark |
| `set_textbox_tooltip` | Textbox `<ToolTip>` |
| `set_document_map_label` | `<DocumentMapLabel>` for the navigation pane |

### Pagination

| Tool | What it edits |
|------|---------------|
| `set_group_page_break` | `<Group><PageBreak>` (Start / End / Between) |
| `set_repeat_on_new_page` | Repeat a group header/footer on each page |
| `set_keep_together` / `set_keep_with_group` | Keep-together rendering hints |

### Parameters (advanced)

| Tool | What it edits |
|------|---------------|
| `add_parameter` / `remove_parameter` / `rename_parameter` | Manage `<ReportParameter>` elements |
| `set_parameter_prompt` / `set_parameter_type` | Prompt text; data type (Boolean / DateTime / Integer / Float / Text) |
| `set_parameter_available_values` | Static `<ParameterValues>` list (strings or `{value, label}` dicts) **or** `<DataSetReference>` to a lookup dataset |
| `set_parameter_default_values` | Static `<Values>` list **or** `<DataSetReference>` (defaults take ValueField only — defaults are values, not display strings) |
| `update_parameter_advanced` | Toggle the four boolean flags: `multi_value`, `hidden`, `allow_null` (writes `<Nullable>`), `allow_blank` |
| `reorder_parameters` | Reorder `<ReportParameters>` (controls prompt order) |
| `set_parameter_layout` / `sync_parameter_layout` | Position parameters in the `<ReportParametersLayout>` grid |

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

### Report lifecycle

| Tool | What it does |
|------|--------------|
| `create_report` | Scratch-create a minimal valid `.rdl` to start from |
| `duplicate_report` | Copy a report to a new path |
| `backup_report` / `restore_from_backup` | Snapshot a report and roll back to it |

### Expression helpers

These don't mutate the report — they build correct RDL expression strings to
pass into other tools (text, filters, conditional styling).

| Tool | What it returns |
|------|-----------------|
| `count_where` / `sum_where` | A `Count`/`Sum` aggregate expression with an inline condition |
| `iif_format` | An `IIf(...)` expression for conditional values/formatting |
| `get_expression_reference` | A reference sheet of common RDL expression patterns |

### Transactions & validation

Batch many edits into one atomic save, and check correctness before/after.

| Tool | What it does |
|------|--------------|
| `start_editing_transaction` / `commit_editing_transaction` / `cancel_editing_transaction` | Open an in-memory transaction, lint-and-save once, or discard. See [docs/TRANSACTIONS.md](docs/TRANSACTIONS.md). |
| `apply_edits` | Apply a list of tool calls in one transaction |
| `dry_run_edit` | Preview an edit's effect without writing to disk |
| `validate_report` / `verify_report` | Structural validation (and opt-in XSD validation against the bundled `reportdefinition.xsd`) |
| `lint_report` | Surface warnings/errors Report Builder would flag |

### Raw XML escape hatch

| Tool | What it does |
|------|--------------|
| `raw_xml_view` | Read the XML under an XPath |
| `raw_xml_replace` | Replace the XML at an XPath — last resort for anything without a dedicated tool |

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
│   │   ├── encoding.py         # XML declaration / self-closing-tag fidelity
│   │   ├── transactions.py     # In-memory transaction registry
│   │   └── schema.py           # Structural + opt-in XSD validation
│   ├── ops/                    # One module per RDL concern; wired into tools.py
│   │   ├── reader.py           # describe / get_datasets / get_params / get_tablixes
│   │   ├── datasource.py       # PBI XMLA connection + data-source management
│   │   ├── dataset.py          # DAX body, query params, fields, calc fields, filters
│   │   ├── tablix.py           # Row/column groups, sort, visibility, matrix
│   │   ├── tablix_columns.py   # Add/remove tablix columns
│   │   ├── tablix_cells.py     # Cell span
│   │   ├── tablix_static.py    # Static rows / columns
│   │   ├── tablix_subtotals.py # Subtotal rows / columns
│   │   ├── chart.py            # Chart series, axes, legend, labels, palette
│   │   ├── page.py             # Page setup + orientation
│   │   ├── layout.py           # Pagination (page breaks, keep-together, repeat)
│   │   ├── header_footer.py    # Page header / footer authoring
│   │   ├── body.py             # Body textboxes / images / containers / removal
│   │   ├── positioning.py      # Move / resize named items per region
│   │   ├── templates.py        # Chart + tablix snippet builders
│   │   ├── styling.py          # Textbox styles, runs, bulk, row styling, find
│   │   ├── images.py           # Image sizing / source
│   │   ├── actions.py          # Actions, tooltips, document-map labels
│   │   ├── visibility.py       # Element-level visibility
│   │   ├── parameters.py       # Lifecycle, values, advanced flags, layout
│   │   ├── embedded_images.py  # Base64 image embedding
│   │   ├── expressions.py      # Expression-builder helpers (count/sum/iif)
│   │   ├── filter_types.py     # Filter operator definitions
│   │   ├── clone.py            # duplicate_report
│   │   ├── scratch.py          # create_report
│   │   ├── snapshot.py         # backup / restore
│   │   ├── transactions.py     # start/commit/cancel + apply_edits
│   │   ├── dry_run.py          # dry_run_edit
│   │   ├── validate.py         # validate / verify
│   │   ├── lint.py             # lint_report
│   │   └── escape.py           # raw_xml_view / raw_xml_replace
│   └── schemas/                # Bundled RDL 2016 XSD (reportdefinition.xsd) for opt-in validation
└── tests/
    ├── fixtures/
    │   └── pbi_paginated_minimal.rdl  # Hand-tuned to match Report Builder's emitted style
    └── test_*.py               # 1188 tests — every tool plus round-trip invariants
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

The suite is fast (~1.7s for 1188 tests) so re-run on every change.

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

---

<!-- Ownership marker read by registry.modelcontextprotocol.io to verify this
     PyPI package maps to the io.github.mafaq229/pbirb-mcp registry entry. -->

```
mcp-name: io.github.mafaq229/pbirb-mcp
```
