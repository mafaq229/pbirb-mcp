# Roadmap

What's shipped, what's queued, and the design rationale behind each
release. For the per-release change list see
[`CHANGELOG.md`](../CHANGELOG.md).

## Shipped

### v0.1.0 — initial public release

42 tools across 8 phases. Tablix groups, sorting, filters, page
setup, parameter values, embedded images, headers/footers, body
content. The baseline.

### v0.2.0 — tablix completeness + body editing + parameter CRUD

Driven by the 2026-04-27 LLM-driven editing session feedback. Closed
the read-back / repositioning gaps that blocked the first session
and made the parameter surface a complete CRUD set.

Highlights:

* Tablix completeness — column groups, tablix columns, subtotals,
  cell spans, static rows/columns.
* Repositioning — `set_body_item_position`, `set_header_item_position`,
  `set_footer_item_position`, `set_body_item_size`.
* Read-back — `list_body_items` / `list_header_items` /
  `list_footer_items` / `get_textbox` / `get_image` /
  `get_rectangle`, plus extended `describe_report` / `get_tablixes`.
* Parameter CRUD — `set_parameter_prompt`, `set_parameter_type`,
  `add_parameter`, `remove_parameter`, `rename_parameter`.
* Snapshot — `backup_report` + `PBIRB_MCP_AUTO_BACKUP=1`.
* MCP server now returns tool failures as `isError: true` result
  content with structured `{error_type, message}` payloads (per MCP
  spec) instead of opaque JSON-RPC `-32603 INTERNAL_ERROR`.

### v0.3.0 — chart maturity, datasource CRUD, verify suite, layout containers, escape hatch

A consolidated MINOR that bundled what was originally planned as
v0.3 + v0.4 + v0.5, plus a Phase-6 ergonomics chunk driven by a
second LLM session and a Phase-7 verify suite driven by the headline
ask *"I don't want to check after every run and find an error"*.
**60+ new tools across 13 phases.** Tool count: 68 → 131.

| Phase | Theme | Surface |
|---|---|---|
| 0 | Critical bug fixes | XSD-aligned span placement, idempotent encoding, DesignerState sync, parameter-layout auto-sync, plus standalone `sync_parameter_layout` |
| 1 | Chart maturity | `get_chart`, series CRUD, axes, legend, data labels, palette, series colour, title — 10 tools |
| 2 | Textbox round-out | `set_textbox_runs` / `set_textbox_value` / `set_textbox_style_bulk` / `find_textboxes_by_style`; padding / `can_grow` / `can_shrink` / `writing_mode` on `set_textbox_style` |
| 3 | Datasource & dataset CRUD | datasource CRUD, dataset filters, calculated fields, **PBIDATASET `@`-prefix defence** — 11 tools |
| 4 | Sizing / repointing / ordering | `set_image_sizing`, `set_image_source`, `set_column_width`, `set_tablix_size`, `reorder_parameters` |
| 5 | Actions & interactivity | textbox / image / chart-series action, tooltip, document-map label — 5 tools |
| 6 | Session-2 ergonomics | `style_tablix_row` (12 cell-styling calls → 1), `set_header_item_size` / `set_footer_item_size`, `add_dataset_field` / `refresh_dataset_fields`, `set_parameter_layout`, `duplicate_report`, `get_embedded_image_data` — 8 tools |
| 7 | Verify suite + auto-verify | `validate_report`, `lint_report` (15 rules), `dry_run_edit`, `verify_report`, `PBIRB_MCP_AUTO_VERIFY` env var |
| 8 | DAX-aware authoring | `update_dataset_query.alias_strategy='preserve_field_names'`, `field_format` + type-mismatch warnings on `add_*_filter` |
| 9 | Expression reference + emitters | `get_expression_reference`, `count_where`, `sum_where`, `iif_format` |
| 10 | Pagination | `set_group_page_break`, `set_repeat_on_new_page`, `set_keep_together`, `set_keep_with_group` |
| 11 | Layout containers | `add_rectangle` (with `contained_items` move + unit-aware coord recalc), `add_list`, `add_line` |
| 12 | Reader extensions + escape hatch | `find_textbox_by_value`, `raw_xml_view`, `raw_xml_replace`, extended `describe_report` (`parameter_layout`, `embedded_images`, `dataset_query_parameters`, `designer_state_present`) |

The PBIDATASET conventions documented in
[`PBIDATASET-cookbook.md`](./PBIDATASET-cookbook.md) capture the
authoring rules that bit real LLM-driven sessions (the `@`-prefix
rule, DesignerState sync, SELECTCOLUMNS field naming).

#### Live-MCP sweep findings

The 2026-04-30 sweep against a real PBI XMLA report cleared every
phase's static gate **and** found 6 bugs that fixed mid-sweep:

* 2 caught by `verify_report` static lint:
  * `get_chart.legend.visible` — reader echoed raw `<Hidden>`
    without inversion.
  * `refresh_dataset_fields` — SELECTCOLUMNS source columns
    over-matched into the result-set field list.
* 4 caught only by Power BI Report Builder load-test
  (RDL-XSD-conformance class):
  * Bare `<Action>` directly under a ReportItem — RDL 2016 requires
    `<ActionInfo>/<Actions>/<Action>`.
  * `<ActionInfo>` directly on `<ChartSeries>` — actually lives on
    the template `<ChartDataPoint>`.
  * `<Title>` on `<ChartAxis>` — correct element name is
    `<ChartAxisTitle>`.
  * `ChartAxis/Visible` and `ChartAxis/LogScale` accept
    `BooleanLocalizableType` (`True` / `False` capitalised), not
    the lowercase `BooleanExpressionType` used elsewhere.

The same root cause across all 4 RB-only bugs — hand-rolled writer
constants drifted from the real RDL 2016 XSD. **v0.3.1's headline
work bundles Microsoft's official XSD so the schema-conformance
gate runs at write-time.**

## Queued

### v0.3.1 (PATCH) — bundle the RDL 2016 XSD

* **P0** — drop `pbirb_mcp/schemas/reportdefinition.xsd` and flip
  `verify_report` to fail-loudly when the file is missing instead of
  silently returning `xsd_used: false`. This single change catches
  every "wrong element name" / "wrong host" / "wrong value type"
  error class — exactly the four schema-conformance bugs that
  escaped v0.3.0's static sweep.
* **P1** — `prune` flag on `refresh_dataset_fields` (orphan field
  cleanup) plus a `dataset-fields-out-of-sync` lint rule.
* **P1** — `get_chart` should surface `data_labels` per series
  (asymmetric reader/writer today: `set_chart_data_labels` writes
  them but `get_chart` doesn't read them).
* **P1** — `_chart_legend_dict.position` reader currently has a
  wrong fallback to `<DockOutsideChartArea>` (a boolean, not a
  position).

### v0.4 (MINOR) — post-sweep ergonomics

Drives off the 2026-04-30 live-MCP sweep findings. None of these
block; all are real friction the sweep flagged.

* `add_data_source` should emit the modern `<DataProvider>PBIDATASET</DataProvider>` shape rather than the legacy `SQL + powerbi://` provider/connect-string combo.
* Investigate `add_rectangle.contained_items` array marshalling (empty path works; non-empty hits a Zod `Expected array, received string` from the MCP wrapper layer despite an identical schema to other tools that accept arrays).
* `describe_report.charts` top-level array (parity with `tablixes`).
* `describe_report.tablixes[*]` shape hints — row/col counts, has_groups, has_subtotals, has_spans.
* `restore_from_backup` companion to `backup_report`.

### v0.6 (MINOR) — transactional batch + scratch creation

Eliminates the "21 sequential round-trips per session" overhead.
Two transaction styles ship together because they fit different LLM
use cases:

* `apply_edits(path, ops=[...])` — single-call atomic batch.
* `start_editing_transaction(path)` / `commit_editing_transaction` /
  `cancel_editing_transaction` — stateful transaction (Canva pattern).
* `create_report(path, page_setup, datasource)` — minimal valid RDL
  from scratch.

### v0.7 (MINOR) — render preview / RB-load gate

The 2026-04-30 sweep argues for splitting this into two tools:

* `load_test_report(path)` — opens the RDL via RB's deserializer +
  parser, returns `{loaded: bool, errors: [...]}`. No actual render.
  Catches runtime expression failures that even the bundled XSD
  can't see.
* `render_preview(path, parameter_values?, page=1, format='png')` —
  full render returning bytes.

Implementation path is non-trivial (Microsoft doesn't ship a
headless RDL renderer). Investigation phase first; documented in
`.local/maintainer/render-preview-investigation.md` (when written).

### v1.0 — stable

Exit criteria:

* v0.3.1 + v0.4 + v0.6 shipped. v0.5 already shipped inside v0.3.0.
  v0.7 strongly preferred but may slip.
* ≥ 1 external user, or formal "personal stable use" sign-off.
* CI green for ≥ 30 consecutive days.
* MCP server registry entry live for ≥ one MINOR.
* README declares the tool-surface contract stable.

## Cross-references

* [`CHANGELOG.md`](../CHANGELOG.md) — per-release change list.
* [`PBIDATASET-cookbook.md`](./PBIDATASET-cookbook.md) — authoring
  conventions for Power BI XMLA-bound datasets.
* `pbirb_mcp/server.py::SERVER_VERSION` — current package version
  (matches `pyproject.toml`'s `version`).
* `pbirb_mcp/server.py::PROTOCOL_VERSION` — MCP spec version (ISO
  date string from Anthropic's MCP scheme; independent of the
  package version).

## Historic plans

The full commit-by-commit design plans for each MINOR live in the
maintainer's local working tree as `.local/PLAN-v0.X.md`. They're
not redistributed — the public surface is the CHANGELOG and this
roadmap. Internal contributors have the maintainer doc at
`.local/maintainer/05-roadmap.md` for the day-to-day checklist.
