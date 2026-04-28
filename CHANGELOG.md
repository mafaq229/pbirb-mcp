# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
adapted for an MCP tool surface — the contract is the set of tool names,
their `inputSchema`, the shape of their return values, and their error
semantics. See [CONTRIBUTING.md](CONTRIBUTING.md#versioning) for the
bump rules.

## [Unreleased]

## [0.1.1] - 2026-04-28

Internal infrastructure release. No tool surface changes — the 42 tools
and their schemas from 0.1.0 are unchanged.

### Added
- GitHub Actions CI workflow running `pytest` on Python 3.9 / 3.10 / 3.11 / 3.12 plus a Ruff lint job.
- Ruff configuration in `pyproject.toml` (line length 100, `E,F,W,I,UP,B,SIM` rule set; PEP 604 union rules disabled because Python 3.9 is supported).
- Pre-commit hooks running `ruff format`, `ruff check --fix`, baseline `pre-commit-hooks` checks, and a fast `pytest -x` against the project venv.
- `CONTRIBUTING.md` codifying the project rules (tests-first, lxml only, stable IDs, atomic save, byte-identity round-trip, manual smoke in Power BI Report Builder).
- `SECURITY.md` documenting the disclosure channel and the local-files-only threat model.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).
- `.github/ISSUE_TEMPLATE/` — bug report (asks for the failing RDL fragment + tool call + diff), feature request, tool proposal.
- `.github/PULL_REQUEST_TEMPLATE.md` mirroring the contribution checklist.
- `.github/dependabot.yml` — weekly updates for GitHub Actions and pip.
- README badges (CI, PyPI, Python versions) and new Stability / Versioning / Releases sections.
- `keywords` and additional `classifiers` in `pyproject.toml` for PyPI discoverability; `Changelog` project URL.
- `ruff` in `[project.optional-dependencies].dev`.

### Changed
- 35 files reformatted by `ruff format` and `ruff check --fix`. No behavioural changes; round-trip byte-identity tests still pass.
- `pbirb_mcp/core/document.py`: imports moved above the self-closing-tag regex compile to satisfy `E402`; `try/except OSError/pass` replaced with `contextlib.suppress(OSError)`.
- `pbirb_mcp/ops/tablix.py`: collapsed a nested `if` in `_insert_member_child` into a single `and` per `SIM102`.

## [0.1.0] - 2026-04-27

Initial public release on PyPI. 42 tools targeting Power BI Report Builder
paginated reports — the gaps in upstream
[bethmaloney/rdl-mcp](https://github.com/bethmaloney/rdl-mcp) that
otherwise force hand-written XML.

### Added

#### Read-only inventory
- `describe_report` — top-level inventory of data sources, datasets, parameters, tablixes, page setup.
- `get_datasets` — full DAX command text, fields, query parameters, dataset filters.
- `get_parameters` — report parameters with data type, prompt, and flags.
- `get_tablixes` — tablix layout: columns, row groups, sort expressions, filters, visibility.
- `list_tablix_filters`, `list_embedded_images`.

#### Datasource & dataset
- `set_datasource_connection` — repoint a `<DataSource>` at a Power BI XMLA endpoint (`Data Source=powerbi://api.powerbi.com/v1.0/myorg/<workspace>;Initial Catalog=<dataset>` with `DataProvider=SQL`).
- `update_dataset_query` — replace `<DataSet>/<Query>/<CommandText>` with a DAX expression. No `<CommandType>` is emitted (PBI paginated reports differ from SSRS here).
- `add_query_parameter`, `update_query_parameter`, `remove_query_parameter`.

#### Tablix
- `add_tablix_filter`, `remove_tablix_filter` — RDL 2016 `FilterOperator` enum.
- `add_row_group`, `remove_row_group` — wraps the row hierarchy in a new outer group with header row.
- `set_group_sort`, `set_group_visibility`, `set_detail_row_visibility`, `set_row_height`.

#### Page
- `set_page_setup`, `set_page_orientation` (idempotent portrait/landscape swap).

#### Page header / footer
- `set_page_header`, `set_page_footer` — height + `PrintOnFirstPage` / `PrintOnLastPage`.
- `add_header_textbox`, `add_footer_textbox` — Textbox with static text or `=expression`.
- `add_header_image`, `add_footer_image` — Embedded / External / Database image source.
- `remove_header_item`, `remove_footer_item`.

#### Body composition
- `add_body_textbox`, `add_body_image`, `remove_body_item`.

#### Snippet templates
- `insert_tablix_from_template` — basic Tablix bound to a named dataset, one column per requested field.
- `insert_chart_from_template` — basic Column chart with one category and one Y series; post-insert edits configure Type / palette / etc.

#### Styling
- `set_textbox_style` — three-level Style routing (box-level / paragraph-level / run-level) with per-property optionality.
- `set_alternating_row_color` — zebra stripes via `BackgroundColor=IIf(RowNumber(Nothing) Mod 2, "<a>", "<b>")`.

#### Visibility
- `set_element_visibility` — sets `<Visibility>` on any named ReportItem (Tablix, Textbox, Image, Rectangle, Subreport, Chart).

#### Parameters (advanced)
- `set_parameter_available_values` — static or `<DataSetReference>` lookup.
- `set_parameter_default_values` — static or query-driven.
- `update_parameter_advanced` — multi-value, hidden, allow-null (writes `<Nullable>`), allow-blank.

#### Embedded images
- `add_embedded_image` (reads from disk, base64-encodes), `list_embedded_images`, `remove_embedded_image`.

### Foundations
- `RDLDocument` (lxml) with byte-identical round-trip and atomic save (`<path>.tmp` then rename).
- Stable-ID resolvers (`resolve_tablix`, `resolve_dataset`, `resolve_parameter`, `resolve_textbox`, `resolve_column_group`).
- 273-test suite covering every tool plus round-trip invariants.

### RDL gotchas codified by tests
- XML declaration uses **double quotes** (`<?xml version="1.0" encoding="utf-8"?>`), not lxml's default singles.
- Self-closing tags emit `<Tag />` with the leading space, not `<Tag/>`.
- The `rd:` namespace prefix (`http://schemas.microsoft.com/SQLServer/reporting/reportdesigner`) is preserved on round-trip.
- `<Report>` does not carry `MustUnderstand="df"` (Report Builder rejects unknown `MustUnderstand` references).
- `<ChartCategoryAxes>` / `<ChartValueAxes>` hold `<ChartAxis>` directly — there is no `<ChartCategoryAxis>` wrapper.
- `<ChartMember>` requires a `<Label>` child, even an empty one.
- DAX queries write `<CommandText>` only; PBI paginated reports do not carry `<CommandType>` (unlike SSRS).

[Unreleased]: https://github.com/mafaq229/pbirb-mcp/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/mafaq229/pbirb-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/mafaq229/pbirb-mcp/releases/tag/v0.1.0
