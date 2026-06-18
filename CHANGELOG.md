## [Unreleased]

## [0.4.1] - 2026-06-18

### Added
- `server.json` manifest and an `mcp-name` marker in the README, enabling
  submission to the official
  [MCP Registry](https://registry.modelcontextprotocol.io).

### Changed
- `SERVER_VERSION` bumped to 0.4.1 to stay in lockstep with the package
  version reported in `initialize` responses.

## [0.4.0] - 2026-05-17

MINOR consolidating five phases of work (originally planned as
v0.4 + v0.6) into one release. Eliminates the per-edit round-trip
cost via the v0.6 transaction surface, adds scratch-creation, closes
the v0.4 ergonomics wishlist from the 2026-04-30 sweep, AND ships
the matrix-pivot + chart-fan-out surfaces from the 2026-05-11
session feedback. **Tool count: 132 → 150+. Pytest: 1035 → 1188.**

### Added

**Transaction surface** — `pbirb_mcp/core/transactions.py` +
`pbirb_mcp/ops/transactions.py` (Phases B + C):
- `start_editing_transaction(path)` — open a transaction; returns
  `{transaction_id, path, expires_at}`. Subsequent edit tools
  accept an optional `transaction_id` to operate against the
  in-memory tree without touching disk.
- `commit_editing_transaction(transaction_id)` — lints the
  in-memory tree, saves once (atomic `.tmp` + rename), deregisters.
  Aborts (without saving) on `severity: 'error'` lint issues;
  transaction stays open so the caller can fix + re-commit.
- `cancel_editing_transaction(transaction_id)` — discards the
  in-memory tree; disk untouched.
- `apply_edits(path, ops)` — atomic batch. Opens an internal
  transaction, dispatches each op via JSON-RPC, commits at the end.
  On any op failure: rolls back; disk byte-identical to pre-call.
- `PBIRB_MCP_TRANSACTION_TIMEOUT_S` env var (default 600s) controls
  orphan expiry. Lazy sweep on every transaction-aware call.

**Scratch creation** — `pbirb_mcp/ops/scratch.py` (Phase D):
- `create_report(path, page_setup=None, datasource=None)` — emits
  a minimal XSD-valid RDL from scratch. Refuses if `path` exists.
  Default page_setup is US Letter portrait; pass any subset of
  `{page_*, margin_*, body_*}` to override. Optional `datasource`
  block wires a real PBI XMLA connection at creation time via the
  same shape `add_data_source` produces.

**v0.4 ergonomics** (Phase A):
- `add_data_source(provider="pbidataset")` — modern PBI Desktop
  shape (DataProvider=PBIDATASET, pbiazure:// ConnectString,
  `<rd:PowerBIWorkspaceName>` / `<rd:PowerBIDatasetName>` siblings).
  Default `provider="sql"` preserves v0.3.x byte output.
- `restore_from_backup(backup_path, target_path=None, force=False)`
  — symmetric counterpart to `backup_report`. Derives target from
  the backup filename when not supplied. Refuses with `ValueError`
  if target mtime > backup mtime (staleness guard); `force=True`
  overrides.
- `describe_report.charts` — new top-level array of chart names,
  parity with the existing `tablixes` field.

**Matrix completeness** — `pbirb_mcp/ops/tablix.py` +
`tablix_subtotals.py` + `page.py` (Phase F):
- `convert_to_matrix(path, tablix_name, row_group, column_group)`
  — drops the residual `Details` row group and body row from a
  tablix that already has a named row group + named column group.
  Idempotent-by-explicit-refusal on second call.
- `set_tablix_corner(path, tablix_name, text=None, expression=None)`
  — writes the `<TablixCorner>` block with a single 1×1 textbox at
  the top-left of a matrix. Refuses when no column group is present.
- `add_subtotal_column(path, tablix_name, group_name, aggregates,
  position='after', width='1in')` — column-axis mirror of
  `add_subtotal_row`. Adds a new TablixMember + TablixColumn + cells.
- `set_body_size(path, width=None, height=None)` — sets
  `<Body>/<Height>` and `<ReportSection>/<Width>` (the body's width
  sibling). Distinct from `set_page_setup` (page chrome) and
  `set_body_item_size` (items inside the body).

**Chart fan-out** — `pbirb_mcp/ops/chart.py` (Phase F):
- `set_chart_series_grouping(path, chart_name, series_name,
  group_field=None, group_expression=None, replace=False)` —
  promotes a chart's static `<ChartMember>` to a dynamic-fanout
  series by writing `<Group>/<GroupExpressions>/<GroupExpression>`.
  One rendered series per distinct group expression value at preview
  time.
- `set_chart_data_labels` per-point kwargs: `visible_expression`,
  `position` (Auto/Top/.../Outside enum), `use_value_as_label`,
  `font_weight`, `color`. `visible` and `visible_expression`
  mutually exclusive.

### Changed

- **BREAKING** — `describe_report.tablixes` shape: was bare strings
  `["Name", ...]`, now `[{name, rows, columns, has_groups,
  has_subtotals, has_spans}, ...]`. Migration:
  `[t["name"] for t in r["tablixes"]]` recovers the old list.
- `set_chart_series_type.series_subtype` default flipped from
  `"Plain"` to `None` ("preserve existing Subtype"). Pre-v0.4
  always wrote `Plain` on type-only edits, silently resetting
  Bar/Stacked back to Bar/Plain. Existing callers passing
  `series_subtype` explicitly are unaffected.
- `RDLDocument.open` consults the transaction registry; returns the
  registered in-memory tree by object identity when a transaction
  owns the path. Invisible to non-transaction callers.
- `RDLDocument.save_as` silently no-ops when the document is inside
  an active transaction. Commit clears the flag and re-saves.
- `MCPServer._tools_call` routes `transaction_id` to the registry;
  pops the id before unpacking handler kwargs (leakage guard); skips
  the auto-verify branch inside a transaction (the post-commit
  verify covers it).
- `add_rectangle.contained_items` schema tightened with `default: []`
  and `minItems: 0` to help client-side Zod / JSON-schema validators
  honour the optional intent. Server-side dispatch was always fine.

### Fixed

- `update_dataset_query.alias_strategy="preserve_field_names"`
  no longer strips the `Table[Col]` prefix from `<DataField>` cells
  when the new DAX uses bracket-token references. Bare-token DAX
  (no qualified form recoverable) leaves existing `<DataField>`
  untouched instead of overwriting with the bare column name —
  pre-v0.4 silently broke field binding at preview time. From the
  2026-05-11 matrix-report session feedback (gap #7).

### Added (infrastructure, no user-facing surface)

- `RDLDocument.batch(path)` context manager — open once / save
  once on clean exit / no save on exception. Used by `create_report`
  and the transaction commit path.
- `pbirb_mcp.core.transactions` — in-memory registry mapping
  `transaction_id ↔ (abspath, RDLDocument)`. Pure-data, no
  `MCPServer` dependency. Lazy orphan sweep via `sweep_orphans`.
- `pbirb_mcp.ops.lint._lint_doc(doc, rules=None)` — already-open-doc
  entry point for lint; `lint_report(path)` is now a thin wrapper.
  Used by `commit_editing_transaction` and `apply_edits` to lint
  the in-memory tree without writing to a tempfile and re-parsing.

### Notes

- The matrix-pivot + chart-fan-out flow (commits 17–22) produces an
  XSD-valid RDL end-to-end (`verify_report` returns `valid: true,
  xsd_used: true` against the v0.4 sweep output).
- Round-trip byte-identity (`tests/test_document.py::test_round_trip_byte_identical_to_fixture`)
  green at every commit — the regression canary that defends the
  open/save interception.
- Pytest 1035 → 1188 (+153). 22+ git commits since v0.3.1; plus
  `.local/` archival artifacts (gitignored). Three docs landed
  under `docs/`: TRANSACTIONS.md, MATRIX-cookbook.md,
  PBIDATASET-cookbook.md (extended).

## [0.3.1] - 2026-05-02

PATCH driven by the v0.3.0 live-MCP sweep
(`.local/feedback/2026-04-30-v030-live-mcp-sweep.md`). The sweep
cleared every static gate yet Power BI Report Builder rejected the
file on load with four schema-conformance bugs — root cause: the XSD
layer was silently skipped because we never shipped the schema file.
v0.3.1 closes that gap and ships the small ergonomics deltas the same
sweep flagged. **Tool count: 131 → 132. Lint rules: 15 → 16.**

### Added

- **Bundled RDL 2016/01 XSD** — `pbirb_mcp/schemas/reportdefinition.xsd`
  ships by default. `validate_report` / `verify_report` now report
  `xsd_used: true` out of the box and catch the schema-conformance
  bug class (wrong child elements, wrong-host nesting, missing
  required children) at the static layer — the same gate Report
  Builder runs on load. See `pbirb_mcp/schemas/NOTICE.md` for the
  redistribution permission granted by the MS-RDL Open Specifications
  IP Rights Notice.
- **`xsd-not-bundled` warning** — `validate_report` emits a
  `severity: warning, rule: xsd-not-bundled` issue when the bundled
  XSD is missing (e.g. a source-build that didn't copy package-data),
  instead of silently skipping. The silent skip is what masked the
  four 2026-04-30 RB-only bugs.
- **`dataset-fields-out-of-sync` lint rule (16th)** — detects
  `<Field>` declared but DAX `<CommandText>` doesn't return that
  column. Reuses `_extract_dax_field_names`. Skips datasets with
  unparseable DAX (e.g. bare `EVALUATE 'Table'`) to avoid false
  flags.
- **`remove_dataset_field` tool** — symmetric counterpart to
  `remove_calculated_field` for data-bound `<Field>` entries
  (`<DataField>`). Refuses on calculated fields (with hint pointing
  at the right tool) and on still-referenced fields (locator list,
  `force=True` override). Closes the cookbook flow
  `refresh_dataset_fields` (lists orphans) → review →
  `remove_dataset_field` (drops them).
- **`get_chart.series[].data_labels`** — per-series read-back of
  `<ChartDataLabel>` (`{visible, format}` or `None`). Closes the
  asymmetric reader/writer gap with `set_chart_data_labels`.

### Fixed

- **`get_chart.legend.position`** no longer returns the boolean text
  of `<DockOutsideChartArea>` when both that element and `<Position>`
  are present. Reads only `<Position>`. Pure bug fix; same key,
  same shape.
- **`tests/fixtures/pbi_chart_rich.rdl`** migrated from legacy
  `<ChartAxis><Title>` to canonical `<ChartAxisTitle>` (same bug
  class as v0.3.0 `09225df`). Without the migration, every
  `test_round_trip_safe` in `test_chart.py` and `test_actions.py`
  would fail under the bundled XSD layer.

### Changed

- **Stale "not redistributable" docstrings** in
  `pbirb_mcp/ops/validate.py` and `pbirb_mcp/core/schema.py` retired
  — predated the licensing research; the MS-RDL Open Specifications
  IP Rights Notice explicitly grants redistribution of schemas
  included in the spec documentation.
- `lint_report` description updated to "Sixteen rules" and lists
  the new `dataset-fields-out-of-sync` rule.
- `validate_report` and `verify_report` descriptions document the
  bundled XSD posture and the new `xsd-not-bundled` warning shape.
- `remove_calculated_field` description now points at the new
  `remove_dataset_field` for data-bound fields.

### Notes

- Three of the four 2026-04-30 RB-only bugs are now caught at the
  XSD layer (regression fixtures under `tests/fixtures/known_bad/`,
  driven by `tests/test_xsd_regressions.py`). The fourth
  (`d999da5`: `<ChartAxis><Visible>true</Visible>` lowercase) is an
  RB-runtime constraint that the bundled XSD treats as `xsd:string`
  — needs the v0.7 `load_test_report` runner or a future lint rule.
- Pytest 1004 → 1035 green; JSON-RPC stdio smoke + uvx smoke still
  green; byte-identical round-trip preserved.

## [0.3.0] - 2026-04-30

A consolidated MINOR that bundled what was originally planned as v0.3 +
v0.4 + v0.5, plus a Phase-6 ergonomics chunk driven by a second LLM
session and a Phase-7 verify suite driven by the headline ask "I don't
want to check after every run and find an error". 60+ new tools across
13 phases. 1004 pytest tests, JSON-RPC stdio smoke, byte-identical
round-trip preserved throughout. **Tool count: 68 → 131.**

### Added

**Chart authoring** — `pbirb_mcp/ops/chart.py` (Phase 1):
- `get_chart` — single-chart read-back (palette / series / category groups / axes / legend / title / style / visibility).
- `add_chart_series`, `remove_chart_series` (refusal-on-last-series).
- `set_chart_axis(axis ∈ {Category, Value}, title?, format?, min/max?, log_scale?, interval?, visible?)`.
- `set_chart_legend(position?, visible?)`, `set_chart_data_labels(series_name?, visible?, format?)`.
- `set_chart_palette`, `set_chart_series_type` (combo charts), `set_chart_title`, `set_series_color`.

**Textbox round-out** (Phase 2):
- `set_textbox_runs` — multi-run rich text in a single textbox.
- `set_textbox_value` — single-run text/expression edit (refuses on multi-run).
- `set_textbox_style_bulk` — apply one style kwarg-set to a list of textboxes in one call.
- `find_textboxes_by_style` — discovery helper for the bulk applier.
- `set_textbox_style` gained `padding_top/bottom/left/right`, `can_grow`, `can_shrink`, `writing_mode`.

**Datasource & dataset CRUD** — `pbirb_mcp/ops/datasource.py`, extended `dataset.py` (Phase 3):
- `list_data_sources` (rich shape), `get_data_source`, `add_data_source`, `remove_data_source` (refusal-on-references), `rename_data_source` (rewrites references atomically).
- `get_dataset`, `list_dataset_filters`, `add_dataset_filter`, `remove_dataset_filter`.
- `add_calculated_field`, `remove_calculated_field`.
- **PBIDATASET `@`-prefix defence**: `add_query_parameter` / `update_query_parameter` auto-strip a leading `@` for PBIDATASET-bound datasets and emit a structured warning. Override with `force_at_prefix=True`.

**Sizing / repointing / ordering** (Phase 4):
- `set_image_sizing`, `set_image_source` (repoint embedded reference without delete-and-readd).
- `set_column_width` (accepts integer index OR textbox name), `set_tablix_size`.
- `reorder_parameters` with strict permutation check.

**Actions & interactivity** — `pbirb_mcp/ops/actions.py` (Phase 5):
- `set_textbox_action`, `set_image_action`, `set_chart_series_action` — Hyperlink / Drillthrough / BookmarkLink.
- `set_textbox_tooltip`, `set_document_map_label` (works on any named ReportItem).

**Session-2 ergonomics** (Phase 6):
- `style_tablix_row` — apply one style kwarg-set to every cell of a tablix row in one call (12 cell-styling calls → 1). `row` accepts an integer index OR `"header"` / `"details"` / `"<group>_header"` / `"<group>_footer"`.
- `set_header_item_size`, `set_footer_item_size` — parity with v0.2's `set_body_item_size`.
- `add_dataset_field` (data-bound, distinct from `add_calculated_field`), `refresh_dataset_fields` (regex-based DAX shape detection — SUMMARIZECOLUMNS / SELECTCOLUMNS aliases / Table[Col]).
- `set_parameter_layout(rows, columns, parameter_order)` — explicit grid + cell-order writer. Strict permutation check.
- `duplicate_report(src_path, dst_path, regenerate_ids=True)` — atomic clone with optional GUID regeneration.
- `get_embedded_image_data` — base64 + mime + byte_size for porting embedded images between reports.

**Verify suite + auto-verify** — `pbirb_mcp/ops/validate.py`, `lint.py`, `dry_run.py` (Phase 7):
- `validate_report` — XSD opt-in + structural. Returns `{valid, errors, xsd_used}`.
- `lint_report` — 15 static-analysis rules (`multi-value-eq`, `unused-data-source`, `unused-data-set`, `date-param-as-string`, `missing-field-reference`, `page-number-out-of-chrome`, `expression-syntax`, `dangling-embedded-image`, `dangling-data-source-reference`, `dangling-action`, `pbidataset-at-prefix`, `parameter-layout-out-of-sync`, `double-encoded-entities`, `stale-designer-state`, `tablix-span-misplaced`).
- `dry_run_edit(path, ops)` — apply ops to a tempfile clone, return unified diff + verify; original never modified.
- `verify_report` — composite single-call (validate ∪ lint), `valid: bool` based on `severity == "error"`.
- **`PBIRB_MCP_AUTO_VERIFY=1`** env var — wraps mutating-tool responses as `{result, verify}` automatically.

**DAX-aware authoring** (Phase 8):
- `update_dataset_query` gains `alias_strategy="preserve_field_names"` — positional remap of `<Field>/<DataField>` cells when DAX rewrites change column names.
- `add_tablix_filter` / `add_dataset_filter` gain `field_format` (wraps expression in `Format(...)` for typed-field-vs-string-param comparison) and emit type-mismatch warnings.

**Expression reference + emitters** — `pbirb_mcp/ops/expressions.py` (Phase 9):
- `get_expression_reference` — static cheat-sheet across 7 categories (globals, parameters, fields, aggregates, conditionals, strings, dates) with the encoding gotcha for `&` / `&amp;` documented in the strings entry.
- `count_where(condition)`, `sum_where(field, condition)`, `iif_format(condition, true, false)` — pure SSRS expression emitters.

**Pagination + layout containers** — `pbirb_mcp/ops/layout.py` (Phases 10–11):
- `set_group_page_break(location ∈ {None, Start, End, StartAndEnd, Between})`.
- `set_repeat_on_new_page`, `set_keep_together` (Tablix / Rectangle / Chart / Textbox / Map / Gauge), `set_keep_with_group(value ∈ {None, Before, After})`.
- `add_rectangle(...contained_items=[])` — empty frame OR move named items in with unit-aware Top/Left recalc.
- `add_list(name, dataset_name, ...)` — single-cell repeating Tablix; inner Rectangle named `<list>_Rect`.
- `add_line(name, top, left, width, height, color?, line_thickness?, line_style?)`.

**Reader extensions + escape hatch** — extended `reader.py`, new `escape.py` (Phase 12):
- `find_textbox_by_value(pattern)` — regex search over textbox values.
- `raw_xml_view(xpath)`, `raw_xml_replace(xpath, content)` — single-element XPath escape hatch with namespace-default injection on the replacement content.
- `describe_report` gained `parameter_layout`, `embedded_images`, `dataset_query_parameters`, `designer_state_present`.

**Phase 0 — internal hardening** (no new tool surface, but load-bearing):
- `set_cell_span` placement is now XSD-aligned (spans inside `<CellContents>`).
- `pbirb_mcp/core/encoding.py::encode_text` — single idempotent normaliser routed through every text writer; pre-encoded entities (`&amp;`, `&lt;`, etc.) no longer double-encode.
- `update_dataset_query` syncs `<rd:DesignerState>/<Statement>` so the PBI Query Designer GUI doesn't display stale DAX.
- `add_parameter` / `remove_parameter` / `rename_parameter` keep `<ReportParametersLayout>/<CellDefinitions>` in sync; standalone `sync_parameter_layout` for legacy reports.

### Changed

- **Tool descriptions** for `add_header_textbox`, `add_body_textbox`, `set_textbox_value`, `set_textbox_runs` explicitly document the encoding contract — pass raw text, do not pre-encode XML entities (`&` not `&amp;`, including for the VB.NET concat operator). Closes a recurring foot-gun where pre-encoded `&amp;` produced `&amp;amp;` on disk and tripped `BC30451 'amp' is not declared'` at preview.

### Fixed

Two bugs caught by the static `verify_report` sweep on a real PBI XMLA report:

- `get_chart.legend.visible` — the reader echoed raw `<Hidden>` text under the field name `visible` without inversion. `set_chart_legend(visible=True)` correctly wrote `<Hidden>false</Hidden>` but read-back returned `visible="false"`. Reader now inverts.
- `refresh_dataset_fields` — the SELECTCOLUMNS alias-extraction pass correctly pulled quoted aliases, but the bracket pass then ALSO ran over the entire DAX and pulled out source-column names from inside SELECTCOLUMNS pairs, producing phantom `<Field>` entries. Now skips the bracket pass when SELECTCOLUMNS already produced aliases.

Four schema-conformance bugs caught only by Power BI Report Builder
load-test (motivates v0.3.1's bundled-XSD work):

- Phase-5 setters wrote a bare `<Action>` directly under `<Textbox>` / `<Image>` / `<ChartSeries>`. RDL 2016 requires `<ActionInfo>/<Actions>/<Action>`. Writers now emit the canonical envelope; pre-existing legacy bare `<Action>` is migrated on rewrite.
- `set_chart_series_action` placed `<ActionInfo>` directly under `<ChartSeries>`, but RDL 2016 disallows it there — chart-series actions live on the template `<ChartDataPoint>` inside `<ChartDataPoints>`. Setter now writes ActionInfo on the data point and migrates pre-fix wrong-host shapes.
- `set_chart_axis(title=...)` wrote `<Title>`; the correct element name on a ChartAxis is `<ChartAxisTitle>`. Writer + reader updated; legacy `<Title>` migrated on rewrite.
- `set_chart_axis(visible=..., log_scale=...)` wrote lowercase `true` / `false`; `ChartAxis/Visible` and `ChartAxis/LogScale` are typed `BooleanLocalizableType` (case-sensitive enum {True, False}). Other ReportItem booleans (`CanGrow`, `KeepTogether`, `Hidden`, `RepeatOnNewPage`) use the more permissive `BooleanExpressionType` and stay lowercase.

## [0.2.0] - 2026-04-28

### Added
- `set_conditional_row_color(tablix_name, value_expression, color_map, default_color?, case_sensitive?)`
  — Switch-based row coloring by field value. Sibling to `set_alternating_row_color`.
- Tablix completeness: `add_column_group`, `remove_column_group`,
  `set_column_group_sort`, `set_column_group_visibility`, `add_tablix_column`,
  `remove_tablix_column`, `add_subtotal_row`, `set_cell_span`,
  `add_static_column`, `add_static_row`.
- Body editing: `set_body_item_position`, `set_header_item_position`,
  `set_footer_item_position`, `set_body_item_size`.
- Read-back: `list_body_items`, `list_header_items`, `list_footer_items`,
  `get_textbox`, `get_image`, `get_rectangle`. Extended `describe_report`
  to enumerate body/header/footer items; extended `get_tablixes` with
  per-cell textbox names.
- Snapshot: `backup_report` and the `PBIRB_MCP_AUTO_BACKUP=1` opt-in.
- Parameter CRUD: `set_parameter_prompt`, `set_parameter_type`,
  `add_parameter`, `remove_parameter` (safe-by-default with `force`),
  `rename_parameter` (atomic reference rewrite).

### Changed
- **Tool errors are now returned as MCP-spec `isError: true` result content**
  with structured `{error_type, message}` payloads, replacing JSON-RPC
  `-32603 INTERNAL_ERROR` envelopes. Clients that branched on the JSON-RPC
  error code for tool failures must update; clients that rendered "Tool
  execution failed" will now see the actual reason.
- `get_textbox` style return shape is now nested
  `{box, border, paragraph, run}` matching `set_textbox_style`'s routing,
  with per-run style on `runs[]` entries. Previously only `Textbox/Style`
  was reported.
- `set_body_item_position`, `set_header_item_position`,
  `set_footer_item_position`, `set_body_item_size` return `changed: bool`
  and skip the file save on a no-op.
- `set_page_header` / `set_page_footer` return `changed: list[str]` and
  skip the file save on a no-op.
- Reader output coerces missing `<Top>`/`<Left>` to `"0in"` for top-level
  positioned items (cell-level textboxes still report `null`).
- `set_group_sort`, `set_group_visibility`, `remove_row_group`,
  `set_detail_row_visibility` now refuse column-axis groups with a hint
  pointing at the column-axis sibling tool. Symmetric with
  `set_column_group_*` which already refused row-axis groups.

### Fixed
- `add_tablix_column` places the expression in the Details row (walked
  via `_detail_row_index`), not the literal last row. Previously
  produced a blank data row when called after `add_subtotal_row`.
- `remove_embedded_image` refuses by default when an `<Image
  Source="Embedded" Value=name>` references the image, listing the
  offending Image names. Pass `force=True` to remove anyway.
- `add_embedded_image` sniffs the file's magic bytes and rejects
  mime/format mismatches up front instead of silently embedding bad
  bytes that fail at preview time.
