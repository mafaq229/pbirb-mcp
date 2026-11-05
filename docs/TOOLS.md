# Tool reference

The full behaviour notes for every tool, grouped by toolset (see
[README § Toolsets](../README.md#toolsets-and-read-only-mode)). The
descriptions the server sends to the model (`tools/list`) are short on
purpose; the detail lives here. Input and output shapes are the
`inputSchema` / `outputSchema` in `tools/list`.

Tags: **read-only** (`readOnlyHint`), **destructive** (`destructiveHint`:
deletes content or overwrites a file; the `pbirb-mcp` server asks for
`confirm=true`), **idempotent** (`idempotentHint`: an identical repeat call
changes nothing).

## `core`

### `describe_report`

*read-only*

Top-level inventory of an RDL: data sources, datasets, parameters, tablixes, charts, and page setup. Always the first call when planning edits. `tablixes` is a list of shape hints [{name, rows, columns, has_groups, has_subtotals, has_spans, column_details}]. column_details lists each body column {index, header_text, header_cell, data_cell, bound_fields} — how to find 'the Asset Name column' and its cell textbox names. `charts` is a top-level array of chart names. Also: `subreports` [{name, report_name, parameters}], `maps`, `gauges`, `body` {height, width}, `nested_items` (items inside Rectangles, with parent/region); `page` fills absent sizes with the RDL defaults (11in x 8.5in, 0in margins) and lists them in `page.defaulted`, plus page.columns, page.column_spacing, page.body {width, height} and page.printable_width (width minus left/right margins) for overflow checks.

### `validate_report`

*read-only*

Run schema/structural validation against an .rdl. Returns {valid, errors, xsd_used}. Structural checks always run (root element + required top-level sections). The Microsoft RDL 2016/01 XSD is bundled — when it's loaded, xsd_used is True and the schema-conformance bug class Power BI Report Builder rejects on load gets caught here. If the bundled XSD is missing (source-build without package-data) a {severity:warning, rule:'xsd-not-bundled'} issue surfaces instead of silent skip. Each issue is {severity, rule, location, message, suggestion?}; valid is True iff no severity='error' issue.

### `verify_report`

*read-only*

One-shot static check: union of validate_report and lint_report. Returns {valid, issues, xsd_used} where valid is True iff no issue has severity='error'. Warnings (including 'xsd-not-bundled' when the schema file is missing) don't invalidate the report. Use this as the single 'is the report OK?' tool, or set PBIRB_MCP_AUTO_VERIFY=1 to have it run after every mutating call outside a transaction (commit_editing_transaction verifies at commit). With baseline_path (e.g. a backup taken before an edit), or inside a transaction, each issue gets new: true|false plus introduced_count / preexisting_count.

### `lint_report`

*read-only*

Run static-analysis lint rules against an .rdl (`lint_report` returns `rules_run`; the rules live in `pbirb_mcp/ops/lint_rules/`). They cover the known bug classes (multi-value-eq, missing-field-reference, dangling-embedded-image, pbidataset-at-prefix, parameter-layout-out-of-sync, double-encoded-entities, stale-designer-state, tablix-span-misplaced, dataset-fields-out-of-sync, duplicate-name, invalid-identifier, tablix-structure, chart-structure, etc.). Returns {issues, rules_run} with each issue {severity (error|warning), rule, location, message, suggestion?}; location names the nearest named items, e.g. Tablix[Name='T']>Textbox[Name='X']>Value. Optional `rules` selects a subset by name (empty = all).

### `dry_run_edit`

*read-only*

Apply a list of {tool, args} ops to a tempfile clone of the report; return the unified diff and a verify (validate + lint) report. The original file is NEVER modified. The harness auto-injects `path` into each op's args, so callers don't supply it. On op failure, dispatch stops; partial diff + verify are still returned. Use this to preview risky multi-step edits before committing. The diff is capped at max_diff_bytes (default 40000; capped diffs end in a 'diff truncated' marker line and set diff_truncated); summary=true returns only per-op outcomes, diff_stats and verify counts.

### `start_editing_transaction`

Open the report and start an editing transaction. Returns {transaction_id, path, expires_at}. Pass transaction_id to subsequent edit tools to mutate the live in-memory tree WITHOUT touching disk between calls — eliminates the per-edit parse+serialize round-trip. The transaction times out after PBIRB_MCP_TRANSACTION_TIMEOUT_S seconds (default 600); call commit_editing_transaction to flush or cancel_editing_transaction to discard. Refuses if an active transaction already owns this path.

### `commit_editing_transaction`

Lint the in-memory tree, save to disk once (atomic .tmp + rename), and deregister. Aborts (saved=False) if lint surfaces any severity='error' issue — the transaction stays OPEN so the caller can fix the offending state and re-commit. Also refuses (saved=False, error) when the file changed on disk since the transaction started, unless force=true. Returns {transaction_id, path, saved, verify}.

### `cancel_editing_transaction`

Discard a transaction. The in-memory tree is dropped; the on-disk file is unchanged from when the transaction was started. Returns {transaction_id, path, discarded}.

### `apply_edits`

Atomic batch: open once → apply ops → lint → save once. Opens an internal transaction against path, dispatches each {tool, args} op through the JSON-RPC tools/call path with transaction_id injected, and commits at the end. On any op failure or lint-error at commit, rolls back — disk is byte-identical to its pre-call state. Compare with dry_run_edit (clones to tempfile, never touches the real file): use dry_run_edit to preview a plan, apply_edits to land it. Returns {applied: [{tool, ok, result|error}], verify, committed}.

### `get_expression_reference`

*read-only*

Return a static cheat-sheet of common RDL expression patterns: globals, parameters, fields, aggregates, conditionals, strings, dates. Each entry is {name, syntax, example, description}. Call this when authoring a textbox value or filter expression instead of guessing — and note the explicit encoding hint for the `&` concat operator.

### `count_where`

*read-only*

Emit =Sum(IIf(`<condition>`, 1, 0)) — the SSRS conditional-count idiom. condition is an RDL expression body (no leading '='). Returns a complete top-level expression suitable for set_textbox_value or any RDL `<Value>` sink.

### `sum_where`

*read-only*

Emit =Sum(IIf(`<condition>`, `<field_expression>`, 0)) — the SSRS conditional-sum idiom. Both args are expression bodies (no leading '=').

### `iif_format`

*read-only*

Emit =IIf(`<condition>`, `<true_value>`, `<false_value>`). All three args are expression bodies (no leading '='); string literals must already be quoted, e.g. true_value='"Yes"'.

## `data`

### `get_datasets`

*read-only*

Full DAX command text, fields, query parameters, and dataset-level filters for every DataSet in the report.

### `update_dataset_query`

*idempotent*

Replace the DAX command text of a named dataset. The full DAX expression is accepted verbatim; empty bodies are rejected. Optional alias_strategy='preserve_field_names' keeps every `<Field Name>` (so Fields!X.Value references keep resolving) and re-aims `<DataField>` cells at the new DAX columns (Table[Col] or [Alias]): Fields whose column is still returned stay as they are (any order); a Field whose column vanished is re-aimed only when exactly one new column took its slot in the old DAX's column order; everything else is left untouched and reported in warnings. dry_run=true returns the result (including mapped) without saving.

### `add_query_parameter`

Add a `<QueryParameter>` binding to a dataset's query. Use to wire report parameters into DAX (e.g. =Parameters!DateFrom.Value).

PBIDATASET parameter naming rule: in DAX/`<CommandText>`, write @MyParam (with @). In `<QueryParameter Name=...>`, write MyParam (no @). SQL/MDX use @ in both places. This tool detects the dataset's provider and AUTO-STRIPS a leading @ from `name` for PBIDATASET datasets, returning {normalised: true, warning: ...}. Pass force_at_prefix=true to override (rare; needed for some RSCustomDaxFilter patterns).

### `update_query_parameter`

*idempotent*

Change the value expression of an existing query parameter. Same PBIDATASET @-prefix normalisation as add_query_parameter applies on lookup; legacy Name='@X' parameters that already exist on disk are addressable via either @X or X.

### `set_datasource_connection`

*idempotent*

Repoint a DataSource at a Power BI XMLA endpoint. workspace_url accepts a bare workspace name or a full powerbi:// URL; DataProvider is set to SQL (the AS provider id in RDL).

### `list_data_sources`

*read-only*

Return a rich list of every `<DataSource>` in the report — name, data_provider, connect_string, integrated_security, shared_reference, security_type, data_source_id. describe_report.data_sources returns names only; this tool is the authoring-friendly read.

### `get_data_source`

*read-only*

Single-DataSource read-back (parity with get_textbox / get_image / get_rectangle / get_chart). Returns the same shape as one entry of list_data_sources.

### `add_data_source`

Create a new `<DataSource>` for a Power BI XMLA endpoint. workspace_url accepts a bare workspace name or a full powerbi:// URL. Generates a fresh rd:DataSourceID GUID. Refuses if a DataSource of the same name already exists. provider='sql' (default) emits the legacy DataProvider=SQL + powerbi:// ConnectString + `<rd:SecurityType>` shape; provider='pbidataset' emits the modern PBI Desktop shape — DataProvider=PBIDATASET, pbiazure:// ConnectString with ClaimsToken auth, plus `<rd:PowerBIWorkspaceName>` / `<rd:PowerBIDatasetName>` siblings, no `<rd:SecurityType>`.

### `remove_data_source`

*destructive*

Remove a named `<DataSource>`. Refuses by default if any DataSet/Query/DataSourceName or DataSource/DataSourceReference still references it; the error lists the offending locators. Pass force=True to remove anyway.

### `rename_data_source`

Rename a `<DataSource>` and rewrite every reference: DataSet/Query/DataSourceName entries AND any DataSource/DataSourceReference shared-source links. Atomic: stages all matches before committing. Errors if new_name already exists or equals old_name.

### `list_dataset_filters`

*read-only*

List dataset-level filters in document order. Returns [{expression, operator, values}]; index in the list is the stable handle for remove_dataset_filter. DataSet filters apply to every consumer of the dataset (every Tablix / Chart bound to it); use list_tablix_filters for per-tablix filtering.

### `add_dataset_filter`

Append a `<Filter>` to the named dataset's `<Filters>` block. operator ∈ Equal / NotEqual / GreaterThan / LessThan / GreaterThanOrEqual / LessThanOrEqual / Like / In / Between / TopN / BottomN / TopPercent / BottomPercent. values must be non-empty (single-value filters use a one-element list). Returns the new filter's index for later removal. Optional field_format wraps the expression as Format(`<body>`, fmt) to coerce typed fields for string-parameter comparison. Response includes warnings for field/parameter type mismatches detected via best-effort cross-check.

### `remove_dataset_filter`

*destructive*

Remove a dataset-level filter by its 0-based document-order index. Cleans up the empty `<Filters>` block when removing the last entry.

### `get_dataset`

*read-only*

Single-DataSet read-back (parity with get_textbox / get_image / get_rectangle / get_chart / get_data_source). Returns dataset name, data_source, command_text, fields (with both data_field and value), query_parameters, filters, and designer_state_present.

### `add_calculated_field`

Append a calculated `<Field>` to the named dataset. Calculated fields carry an expression (`<Value>`) instead of a column reference (`<DataField>`); use them for derived fields like Total = Amount * Quantity that aren't in the source query but should be available via Fields!Name.Value. Refuses if a field of the same name already exists.

### `remove_calculated_field`

*destructive*

Remove a calculated `<Field>` by name. Refuses if the field is data-bound (carries `<DataField>` instead of `<Value>`) — those reflect the source query's columns. Drop a data-bound field via remove_dataset_field, or rewrite the dataset query via update_dataset_query.

### `remove_dataset_field`

*destructive*

Remove a data-bound `<Field>` by name (one with `<DataField>`). Symmetric counterpart to remove_calculated_field. Refuses on calculated fields (use remove_calculated_field instead) and on still-referenced fields (any expression containing Fields!`<name>.Value` / .IsMissing / .Count). Pass force=True to delete anyway. Closes the cookbook flow: refresh_dataset_fields lists orphans, remove_dataset_field drops them. Returns {dataset, removed, kind: 'DataBoundField'}.

### `add_dataset_field`

Append a data-bound `<Field>` to a dataset's `<Fields>` block. Writes `<DataField>data_field</DataField>` (and optional `<rd:TypeName>type_name</rd`:TypeName>). Distinct from add_calculated_field which writes `<Value>` for derived fields. Use after rewriting the DAX to declare a new column that came back from the query but isn't yet in `<Fields>`. Refuses on duplicate field name.

### `refresh_dataset_fields`

*idempotent*

Sync a dataset's `<Fields>` block against the shape detected in its DAX `<CommandText>`. Eliminates the manual 'open Report Builder → right-click → Refresh Fields' step after a query rewrite. Matches existing Fields to the query's result columns by `<DataField>` (Table[Col] / [Alias]), not by Name. Adds a Field per unbound column the way Report Builder does: Name sanitised (Amount (USD) -> Amount__USD_; duplicates Name1, Name2 ...), DataField Table[Col] or [Alias]. Lists orphans without auto-removing (caller decides what to drop). Returns {added: [{name, data_field}], orphans: [{name, data_field}], unchanged: [names], warnings}. When the DAX can't be fully analysed (e.g. bare EVALUATE 'Table') nothing is added and warnings explain why.

### `remove_query_parameter`

*destructive*

Remove a query parameter from a dataset. Cleans up the empty `<QueryParameters/>` block when removing the last one.

## `parameters`

### `get_parameters`

*read-only*

Report parameter declarations: name, data type, prompt, flags.

### `reorder_parameters`

*idempotent*

Reorder `<ReportParameter>` children to match the supplied names list. names MUST be a permutation of every existing parameter — no missing, no duplicates, no unknown names. When a `<ReportParametersLayout>` exists, its CellDefinition entries are reordered in lockstep so the parameter pane shows fields in the new declaration order. RowIndex / ColumnIndex are not recomputed (RB's layout grid is independent of declaration order). Returns {order, kind, changed: bool}; same order → no save.

### `set_parameter_layout`

*idempotent*

Author `<ReportParametersLayout>/<GridLayoutDefinition>` explicitly. Writes `<NumberOfRows>` + `<NumberOfColumns>`; rewrites `<CellDefinitions>` so each name in parameter_order lands at (row=index // columns, col=index % columns). Strict permutation check (every existing parameter exactly once). rows*columns must be ≥ parameter count. Auto-creates the layout block when absent. Idempotent: same grid + order → no save. Complements reorder_parameters (declaration order) and sync_parameter_layout (gap-filling). Returns {rows, columns, order, kind, changed}.

### `set_parameter_prompt`

*idempotent*

Write the `<Prompt>` text on a ReportParameter. Empty string clears the `<Prompt>` element entirely; pass a single space ' ' for blank-but-present.

### `set_parameter_type`

*idempotent*

Set `<DataType>` on a ReportParameter. type ∈ {Boolean, DateTime, Integer, Float, String}. Rejects with ValueError if any existing literal default value would be incompatible with the new type — fix defaults first via set_parameter_default_values, then retry.

### `add_parameter`

Create a new ReportParameter with a minimal valid declaration. Appends to `<ReportParameters>` (creating it if absent). Pair with set_parameter_available_values / set_parameter_default_values afterwards for value lists. Booleans are only emitted when an explicit value is supplied.

### `remove_parameter`

*destructive*

Remove a ReportParameter by name. Refuses (lists offending locators) if the parameter is still referenced anywhere in the report by Parameters!`<name>.Value` or .Label. Pass force=True to remove anyway.

### `rename_parameter`

Rename a ReportParameter and rewrite every textual occurrence of Parameters!`<old_name>.Value` / .Label across the entire report. Case-sensitive. Atomic: collects all matches first, then commits. Errors if new_name already exists or equals old_name.

### `sync_parameter_layout`

*idempotent*

Bring `<ReportParametersLayout>/<GridLayoutDefinition>/<CellDefinitions>` into sync with `<ReportParameters>`. Drops cells whose ParameterName no longer exists; appends cells for parameters that have no entry, placed at the next free (row, col) slot. add_parameter / remove_parameter / rename_parameter call this internally; the standalone tool repairs reports whose layout drifted. No-op when the report has no `<ReportParametersLayout>`. Returns {added, removed}.

### `set_parameter_available_values`

*idempotent*

Set or clear `<ValidValues>` on a report parameter. source='static' with a non-empty static_values writes a list of `<ParameterValue>` entries (each entry can be a string or {value, label} dict). source='static' with static_values=[] or omitted CLEARS the `<ValidValues>` element entirely (mirrors set_parameter_prompt('') and returns cleared=true). source='query' writes a `<DataSetReference>` to a lookup dataset. Replaces any existing `<ValidValues>` block.

### `set_parameter_default_values`

*idempotent*

Set or clear `<DefaultValue>` on a report parameter. source='static' with a non-empty static_values writes a `<Values>` list of expressions. source='static' with static_values=[] or omitted CLEARS the `<DefaultValue>` element entirely (mirrors set_parameter_prompt('') and returns cleared=true). source='query' writes a `<DataSetReference>` with ValueField only (no LabelField — defaults are values, not display strings). Replaces any existing block.

### `update_parameter_advanced`

*idempotent*

Toggle the four boolean flags on a report parameter: multi_value, hidden, allow_null (writes `<Nullable>`), allow_blank. Each is independently optional. With no flags passed it's a no-op. Cascading parameters are NOT a flag — use set_parameter_available_values(source='query') + add_query_parameter on the lookup dataset to wire a dependency on another parameter.

## `tablix`

### `get_tablixes`

*read-only*

Tablix layout with stable IDs: columns, row/column groups, sort expressions, filters, visibility. Required input for any tablix edit.

### `set_column_width`

*idempotent*

Set `<Width>` on a tablix's body column. column accepts a 0-based integer index OR a textbox name living in any cell of that column (mirrors how set_cell_span / add_subtotal_row address columns), or column_ref {header | field} to pick it by header text or field. width is an RDL size ('1.5in', '4cm', '80pt'). Idempotent: same width → {changed: false}, no save.

### `set_tablix_size`

*idempotent*

Resize a tablix by writing `<Height>` and / or `<Width>` directly. Each arg independently optional. Use after adding header / footer rows that change the body's natural height. Both values are RDL size strings. Returns {tablix, kind, changed: list[str]} — empty list when inputs match existing.

### `set_group_page_break`

*idempotent*

Set `<BreakLocation>` on a tablix group's page-break rule. location ∈ {None, Start, End, StartAndEnd, Between}. Passing 'None' removes the `<PageBreak>` element (the canonical 'no break' shape). Idempotent. Returns {tablix, group, kind: 'Group', location, changed}.

### `set_repeat_on_new_page`

*idempotent*

Set `<TablixMember>/<RepeatOnNewPage>` on the member that wraps the named group. Most common use: repeat a group header row at the top of every page the group spans. Setting repeat=False removes the element (False is default). Returns {tablix, group, kind: 'TablixMember', repeat, changed}.

### `set_keep_together`

*idempotent*

Set `<KeepTogether>` on a named Tablix / Rectangle / Chart / Textbox / Map / Gauge. Tells the renderer 'don't split this across pages if you can help it'. Best-effort — items larger than a page are still split. keep=False removes the element. Refuses for Image / Line / Subreport and other kinds where the RDL XSD doesn't allow it.

### `set_keep_with_group`

*idempotent*

Set `<TablixMember>/<KeepWithGroup>` on the member that wraps the named group. value ∈ {None, Before, After}. Typical use: a column-header row's member with 'After' to glue it to the data rows that follow. value='None' removes the element.

### `list_tablix_filters`

*read-only*

List all filters on a named tablix in document order. Returns expression, operator, and values per filter; index in the list is the stable handle for remove_tablix_filter.

### `add_tablix_filter`

Append a `<Filter>` to a tablix. Operator must be one of the RDL 2016 enumeration (Equal, NotEqual, GreaterThan, In, Between, ...). Returns the new filter's index for follow-up calls. Optional field_format wraps the expression as Format(`<body>`, fmt) to coerce typed fields for string-parameter comparison. The response includes warnings for cross-checked field/parameter type mismatches.

### `add_row_group`

Add a row group that wraps the entire current top-level row hierarchy. Inserts a matching group-header row at body row 0 with the group expression in the first cell. parent_group (nesting under an existing group) is reserved for a future commit and currently raises NotImplementedError.

### `remove_row_group`

*destructive*

Remove a row group: its wrapped members move up to where the group was and keep their body rows; the group's own header row (found via the row hierarchy, any nesting depth) and its add_subtotal_row subtotal rows are deleted. A group with no header row (e.g. a Report Builder group wrapping just the data row) is unwrapped without deleting any row. keep_subtotals=true keeps subtotal rows (their aggregates then cover the parent scope). Refuses the detail group, a group whose member is itself a leaf, and a matrix group carrying a row-header cell.

### `convert_to_matrix`

*destructive*

Turn a grouped table into a Report Builder-shaped matrix (run after insert_tablix_from_template + add_row_group + add_column_group). The detail rows aggregate into the row group: the detail row is kept and its =Fields!X.Value cells become =`<aggregate>`(Fields!X.Value) (default Sum; cells that already aggregate are left alone, complex ones are left with a warning). Group captions become TablixHeader cells: the row group's caption row moves into a row header; body columns that only repeat a row/column group expression and the column-title row are removed (with 2+ value columns left, the titles become column headers instead). Rows such as totals/footers are kept. Creates the TablixCorner (column-header depth x row-header depth). Refuses: row_group that is the detail group or does not enclose the detail rows, unknown column_group, a tablix already without a detail group (second call), or a result that would not be a consistent matrix — file untouched. Returns {tablix, kind, changed, row_headers, column_headers, aggregated, removed_rows, removed_columns, removed_textboxes, corner_textboxes, warnings}. Then set_tablix_corner for the top-left label.

### `set_tablix_corner`

*idempotent*

Set the value of a `<TablixCorner>` cell — the top-left block of a matrix, column-header depth x row-header depth, that holds the row-axis caption (e.g. 'Type'). Pass `text` for a literal value OR `expression` for a VB.NET '=...' formula (mutually exclusive); `row`/`col` (0-based, default 0/0) pick the cell. The corner is created or resized to the header depths first (new cells: '`<tablix_name>_Corner'` textboxes); the value is set in place, keeping the cell textbox's name and style; an identical call changes nothing (changed: []). Refuses when the tablix has no row headers or no column headers (run convert_to_matrix first), and for a cell outside the corner, covered by a span, or not holding a textbox. Returns {tablix, name, kind: 'TablixCorner', row, col, changed}.

### `set_group_sort`

*idempotent*

Replace a group's `<SortExpressions>` with a fresh list. Each entry is an RDL expression, e.g. =Fields!Region.Value.

### `set_group_visibility`

*idempotent*

Set `<Visibility>` on a group's TablixMember. Accepts a Hidden expression and an optional ToggleItem (a textbox name that toggles the group expand/collapse). Hidden must be true/false or an =expression. toggle_textbox must be an existing textbox in the same region/tablix scope and NOT in the rows/columns being hidden (e.g. use the parent group's header cell); omit it to keep the current toggle, pass null or  to remove it. Returns changed; no write when nothing changed.

### `add_column_group`

Add a column group that wraps the current top-level column hierarchy. The group's caption is a TablixHeader on the new group member (a 0.25in header row above the body, textbox `<group>_Header` showing the group expression); body columns and cells are not changed, so column-hierarchy leaves keep matching body columns. If the tablix has row headers the TablixCorner gains a row (or is created). Returns {tablix, group, expression, header_textbox, header_textboxes, corner_textboxes}. Refuses a tablix whose column leaves already disagree with its body columns. parent_group nesting is reserved for a future commit and currently raises NotImplementedError.

### `remove_column_group`

*destructive*

Exact inverse of add_column_group: unwraps a column-axis group's children back into its parent (its TablixHeader goes with it) and shrinks the TablixCorner by that header row; body columns are untouched. Unwrapped columns that sit beside other columns get blank headers so header depth stays uniform. Also removes the `<group>_HeaderCol_<i>` body column that older versions of add_column_group inserted. Returns {tablix, removed, removed_textboxes}. Errors if group_name only exists on the row axis.

### `set_column_group_sort`

*idempotent*

Replace a column-axis group's `<SortExpressions>` with a fresh list. Mirrors set_group_sort but refuses up front if group_name is on the row axis (use set_group_sort instead).

### `set_column_group_visibility`

*idempotent*

Set `<Visibility>` on a column-axis group's TablixMember. Accepts a Hidden expression and an optional ToggleItem (textbox name that toggles expand/collapse). Mirrors set_group_visibility but refuses up front if group_name is on the row axis. Hidden must be true/false or an =expression. toggle_textbox must be an existing textbox in the same region/tablix scope and NOT in the rows/columns being hidden (e.g. use the parent group's header cell); omit it to keep the current toggle, pass null or  to remove it. Returns changed; no write when nothing changed.

### `add_tablix_column`

Append (or insert) a column into a tablix. column_name is the textbox name placed in the data row's new cell — must be unique report-wide. expression goes inside that textbox's TextRun (typically =Fields!X.Value). For a tablix with >= 2 rows the first row gets header_text (default = column_name) as a literal, middle rows get blank cells, and the last row gets expression. position is the 0-indexed body column; default appends at end. width defaults to 1in. The column's hierarchy member is placed so it owns that body column and stays outside every column group: a position strictly inside a group is moved to just before that group (the result's position/warnings say so) unless inside_group names it. Existing columns keep their group membership. A position inside a merged (ColSpan) cell widens that span instead of adding a textbox under it (refused if the data row is merged there).

### `remove_tablix_column`

*destructive*

Remove the tablix column whose body cell holds a textbox named column_name: drops its TablixColumn, the cell at that index from every row, and the column's own leaf in the column hierarchy (found through any column groups, so leaves and columns stay equal). Refuses the tablix's last column, and a column that is the only column of a column group unless remove_group=true (then the group member and its header go too). Merged (ColSpan) cells shrink by one instead of losing their contents. Refuses to delete a textbox a ToggleItem points at unless force=true (which drops those toggles). Errors if no row contains a textbox with the given name. Instead of column_name, column_ref {header | field | index} picks the column by header text or field.

### `add_subtotal_row`

Add a subtotal row for a row-axis group. aggregates is a list of {column, expression} entries; column matches against the group's data-row textbox names (the same names add_tablix_column uses as column_name — NOT field names), or give column_ref {header | field | index} instead of column. expression is the aggregate (e.g. =Sum(Fields!X.Value)). Columns not listed get blank cells. position='footer' (default) puts the row just after the group's last row; 'header' at the group's first row (after its own group-header row, if any). scope='group' (default) places it inside the group: one subtotal per group instance. scope='total' places it next to the group: once per parent instance — a grand total for an outermost group (Report Builder's Add Total). Existing rows keep their group binding. Returns row_index and the emitted textbox names.

### `add_subtotal_column`

Column-axis mirror of add_subtotal_row: adds a static column member, a TablixColumn and a cell in every body row at the column the new member owns. aggregates is a list of {row, expression} entries where row is the 0-based body row index; rows not listed get blank cells. position='after' (default — just right of the group's last column) or 'before' (at its first column). scope='group' (default) puts the column inside the group (one per group instance); scope='total' next to the group (once per parent instance — a grand total column for an outermost group). Static columns beside the group stay outside it. width defaults to '1in'.

### `set_cell_span`

*idempotent*

Set `<ColSpan>` on a tablix body cell (merge it with the cells to its right). The cell is addressed by (row_index, column_name) where column_name is the textbox name inside the cell (or column_ref {header | field | index} picks the column). At least one of row_span / col_span must be supplied; both must be integers >= 1. col_span may not run past the last column. Covered cells become empty placeholders: blank ones are cleared, a covered cell with content is refused unless force=true deletes it. Shrinking a span gives the uncovered cells blank textboxes. row_span > 1 is refused (body cells cannot span rows; RowSpan only applies in a TablixCorner); pass 1 to reset. Returns {..., cleared, created} - the textbox names deleted / created.

### `add_static_row`

Add a static (no-group) row to a tablix. Each cell holds literal text. cells is a list of strings, one per body column (left to right); shorter list = blank trailing cells, longer list errors. Cell textboxes are named row_name (col 0) and row_name_`<col_index>` (others) — unique report-wide so row_name must not clash with any existing textbox. position is the 0-indexed body row; default appends. The row stays outside every row group: a position strictly inside a group is moved to just before that group (the result's position/warnings say so) unless inside_group names it. Existing rows keep their group membership. height defaults to 0.25in.

### `add_static_column`

Add a static (no-group) column to a tablix. Each cell holds literal text. cells is a list of strings, one per body row (top to bottom); shorter list = blank trailing cells, longer list errors. Cell textboxes are named column_name (row 0) and column_name_`<row_index>` (others). position is the 0-indexed body column; default appends. The column stays outside every column group: a position strictly inside a group is moved to just before that group (the result's position/warnings say so) unless inside_group names it. In a matrix the new member gets column-header cells matching its siblings. width defaults to 1in.

### `set_detail_row_visibility`

*idempotent*

Set `<Visibility>` on the tablix's Details group, optionally with a ToggleItem textbox name. Use to hide detail rows by expression without restructuring the row hierarchy. Hidden must be true/false or an =expression. toggle_textbox must be an existing textbox in the same region/tablix scope and NOT in the rows/columns being hidden (e.g. use the parent group's header cell); omit it to keep the current toggle, pass null or  to remove it. Returns changed; no write when nothing changed.

### `set_row_height`

*idempotent*

Set the Height of the Nth body row (0-indexed) in a tablix. Accepts any RDL size string ('0.25in', '1cm', '12pt').

### `remove_tablix_filter`

*destructive*

Remove a filter by index. Filters are anonymous in RDL, so use list_tablix_filters first to find the right index. Removing the last filter also drops the empty `<Filters/>` block.

### `insert_tablix_from_template`

Build and append a basic Tablix to `<Body>/<ReportItems>`. One column per entry in `columns`: a field name of the dataset, or {"field": `<field name>`, "header": `<caption>}` for a display caption that is not an identifier (e.g. 'Unit Price (USD)'). The header row shows the caption (default: the field name), the detail row binds to =Fields!`<field>.Value`. dataset_name must already exist and every field must be declared in it (unknown fields are refused, listing the available ones). The tablix name must be unused report-wide; generated textbox and detail group names are suffixed until unique. `width` is the tablix outer width — each column defaults to 1in regardless, so for a 3-column tablix the columns sum to 3in even if you pass width=10cm. Resize columns afterwards via direct edits or future column-width tools.

## `chart`

### `get_chart`

*read-only*

Return effective state of a named Chart: position, size, dataset, palette, series list (name/type/subtype/value expression/color/data_labels — the label that renders: the template data point's, else the series-level one, as {source: point|series, visible, position, use_value_as_label, format, font_weight, color}, or null), category groups (name/expression/label), axes (category and value), legend, title, Style, Visibility. Symmetric with get_textbox / get_image / get_rectangle.

### `insert_chart_from_template`

Build and append a basic Column chart to `<Body>/<ReportItems>`. Single category axis grouped by category_field; single Y series Sum(Fields!`<value_field>.Value`). dataset_name must already exist. The series is Column/Plain; change it post-insert with set_chart_series_type (RDL Types: Column, Bar, Line, Area, Shape, Scatter, Range, Polar — e.g. a pie chart is Shape/Pie, a doughnut Shape/Doughnut, a stepped line Line/Stepped).

### `add_chart_series`

Append a new `<ChartSeries>` to a named chart. value_field is the dataset field whose Sum becomes the Y expression (=Sum(Fields!`<value_field>.Value`)). series_type defaults to Column; combine series of different types in one chart for combo charts (e.g. Bar + Line). series_subtype defaults to the type's default (Plain; Pie for Shape). The pair must be a real RDL Type/Subtype (see set_chart_series_type); shorthands like Pie or Bubble are normalised and the canonical type/subtype returned. Also creates the series' ChartSeriesHierarchy member (`<ChartMember><Label>series_name</Label>`, the legend text; a sibling of any dynamic series group) and one ChartDataPoint per category leaf, so series and members stay matched by position. Trailing series that lack a member (files written by older versions) get one first, listed in repaired_members. Refuses if series_name is already used by a series or a derived series.

### `remove_chart_series`

*destructive*

Remove a named `<ChartSeries>` from a chart together with its ChartSeriesHierarchy leaf member; parent members left empty are pruned (their groups listed in pruned_groups), so the remaining series keep their own labels and groups. Refuses to remove the last remaining series (use remove_body_item to drop the whole chart instead). Refuses when a derived series (ChartDerivedSeries) uses it as SourceChartSeriesName unless cascade=true, which removes those derived series too. Returns {chart, removed, remaining, removed_derived, pruned_groups, repaired_members}.

### `set_chart_series_type`

*idempotent*

Update `<Type>` and optionally `<Subtype>` on a named ChartSeries. Combo charts work by setting different types per series in the same chart (e.g. one Bar series and one Line series). Only real RDL pairs are written — Column/Bar: Plain|Stacked|PercentStacked; Line: Plain|Smooth|Stepped; Area: Plain|Smooth|Stacked|PercentStacked; Shape: Pie|ExplodedPie|Doughnut|ExplodedDoughnut|Funnel|Pyramid|TreeMap|Sunburst; Scatter: Plain|Bubble; Range: Plain|Smooth|Column|Bar|Stock|Candlestick|BoxPlot|ErrorBar; Polar: Plain|Radar. Shorthands are normalised (Pie -> Shape/Pie, Bubble -> Scatter/Bubble, Radar -> Polar/Radar, Stock -> Range/Stock, SmoothLine -> Line/Smooth, subtype 100 -> PercentStacked). series_subtype null/omitted keeps the existing subtype when it suits the new type, otherwise resets it to the type's default (Plain; Pie for Shape) and reports 'Subtype' in changed. Returns {chart, series, kind, type, subtype, changed: list[str]} with the canonical pair — changed is empty when inputs match existing values.

### `set_chart_series_grouping`

*idempotent*

Make a chart series dynamic: one rendered series per distinct value of the group expression (`<ChartMember>/<Group>/<GroupExpressions>/<GroupExpression>`) — the '13 violation types unknown at design time' fan-out. group_field='Type' is a shorthand for =Fields!Type.Value; group_expression accepts any VB.NET. Mutually exclusive. The group goes on series_name's own leaf member of ChartSeriesHierarchy (series match leaf members by position), or on the dynamic member that series already sits under; trailing series without a member (files written by older versions) get one first, listed in repaired_members. Promoting a static member writes the Report Builder shape: `<Label>` (each fanned-out series' legend text) becomes the group expression, or label_expression; `<SortExpressions>` sorts by the group expression — sort = Ascending (default) | Descending | None (no sort); the group is named group_name or `<series>_Group`. An existing group is refused unless replace=true, which rewrites only its GroupExpressions and keeps Group/@Name (so `Sum(..., "SeriesGroup1")` scope references keep working) and its other children (filters, ...); group_name renames it. On replace the label follows the new expression when it was a constant or the old group expression (a hand-written label expression is kept unless label_expression is given), and a sort on the old expression follows the new one (sort sets it explicitly). A call that matches the current state returns changed: [] and leaves the file untouched. Returns {chart, series, kind: 'ChartGroup', group_name, expression, label, sort: [{value, direction}], repaired_members, changed} — changed lists what was touched: Group, Group.Name, GroupExpressions, SortExpressions, Label, ChartSeriesHierarchy.

### `set_chart_axis`

*idempotent*

Configure a chart axis: title (Caption), format (numeric/date format string in `<Style>/<Format>`), min/max range, log_scale, interval, visible. axis ∈ {Category, Value}; axis_name defaults to 'Primary' (the only axis the template emits — pass a real name for secondary axes). All field args are optional; pass '' to clear an element. Returns {chart, axis, axis_name, kind, changed: list[str]} with the affected sub-element names.

### `set_chart_legend`

*idempotent*

Configure a chart legend: position (TopLeft / TopCenter / TopRight / LeftTop / LeftCenter / LeftBottom / RightTop / RightCenter / RightBottom / BottomLeft / BottomCenter / BottomRight) and visible (writes `<Hidden>true|false</Hidden>`). legend_name defaults to 'Default' (the only one the template emits). Returns {chart, legend, kind, changed: list[str]}; no-op short-circuit when nothing supplied.

### `set_chart_data_labels`

*idempotent*

Configure the data labels of one or all series in a chart. Labels are written where Report Builder keeps them and where they render: `ChartSeries/ChartDataPoints/ChartDataPoint/ChartDataLabel` (every data point of the series — just the template point unless the category hierarchy has several static members). A series-level `ChartSeries/ChartDataLabel` (written by earlier versions) is moved onto the data points that have none, since a point's label overrides it (reported as ChartDataLabel in changed). When series_name is None, the change applies to every series; otherwise only the named series. visible writes `<Visible>true|false</Visible>`; visible_expression writes the same element with a VB.NET =IIf(...) expression (mutually exclusive with visible). format writes `<Style>/<Format>`; pass '' to clear. position ∈ Auto/Top/TopLeft/TopRight/Left/Center/Right/BottomRight/Bottom/BottomLeft/Outside (the RDL ChartDataLabel positions; TopCenter/BottomCenter are legend positions and are refused); Outside is meant for Shape series (pie, doughnut, funnel, pyramid) and adds a warning on other types. use_value_as_label ∈ true/false; font_weight + color write to `<Style>/<FontWeight>` and `<Style>/<Color>` (pass '' to clear). Returns {chart, series, kind, changed, warnings}; changed is empty (file untouched) when everything already matched.

### `set_chart_palette`

*idempotent*

Set `<Palette>` on a chart. palette ∈ Default / EarthTones / Excel / GrayScale / Light / Pastel / SemiTransparent / Berry / Chocolate / Fire / SeaGreen / BrightPastel. Pass '' to clear (RB falls back to its built-in default palette). Returns {chart, kind, changed: bool}.

### `set_series_color`

*idempotent*

Write `<Color>` into a named series's `<Style>` block, overriding the chart palette for just that series. color accepts a named color ('Red'), a hex string ('#FF0000'), or an =expression. Pass '' to clear (series falls back to the palette). Returns {chart, series, kind, changed: bool}.

### `set_chart_title`

*idempotent*

Update `<ChartTitle>/<Caption>`. text can be literal text or an =expression. title_name defaults to 'Default' (the only title the template emits). Returns {chart, title, kind, changed: bool}.

## `style`

### `get_textbox`

*read-only*

Return effective state of a named Textbox: position, size, Visibility, CanGrow, CanShrink, plus a nested style dict that mirrors set_textbox_style's routing — {box: {BackgroundColor, VerticalAlign, padding, ...}, border: {Style, Color, Width}, paragraph: {TextAlign}, run: {FontFamily, FontSize, FontWeight, Color, Format, ...}}. Empty branches are dropped. runs[] entries each carry their own per-run style. Searches the entire report; tablix-cell textboxes have None for top/left/width/height. Top-level positioned items with a missing `<Top>` or `<Left>` coerce to '0in' (RDL default).

### `set_textbox_style`

*idempotent*

Set styling on a named Textbox. Properties route to the right nested Style node automatically: background_color, border_*, vertical_align, padding_*, writing_mode go on Textbox/Style; text_align on Paragraph/Style; font_*, color, format on TextRun/Style; can_grow / can_shrink go DIRECTLY on Textbox (not inside Style). All fields optional — only what's passed gets written. Cell-level styling: every tablix cell is a Textbox with a unique name, so use this tool with the cell's textbox name (e.g. 'HeaderAmount'). Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name.

### `set_textbox_runs`

*idempotent*

Set a textbox's text runs for mixed styling within one display — e.g. '**Asset(s):** value' with a bold prefix + regular value in a single textbox — or for multi-paragraph content. Each run is a dict with required 'text' (or 'value') and optional paragraph_index (default 0) / font_family / font_size / font_weight / font_style / color / format / text_decoration / markup_type, or a 'style' dict keyed by RDL names (FontWeight, ...). Edits in place: run i of paragraph j overwrites the existing run there and keeps every field you don't pass (MarkupType, ToolTip, ActionInfo, other style keys; pass a style key as null to remove it); paragraph properties (TextAlign, ListStyle, indents) are kept, new paragraphs inherit the previous one's. Refuses (file untouched) when existing paragraphs would be left unaddressed, unless drop_extra_paragraphs=true. get_textbox.runs[] is accepted verbatim, so feeding it back is a no-op. Returns {textbox, kind, runs, paragraphs, changed}. Pass raw text in each run's `text` — encoding is handled; don't pre-encode XML entities (use `&` not `&amp;`, including for the VB.NET string-concat operator). Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name.

### `set_textbox_value`

*idempotent*

Replace the text content of a single-run textbox. value can be raw text or an =expression. Use this for the everyday 'change the textbox content' case (swap a literal label, update a stale parameter reference, replace a broken aggregate). Refuses with a redirect to set_textbox_runs when the textbox has multiple text runs or paragraphs (multi-run content needs the rich-text editor). Idempotent: identical value → {changed: false} no-op. Returns {textbox, kind, changed}. Pass raw text — encoding is handled; don't pre-encode XML entities (use `&` not `&amp;`, including for the VB.NET string-concat operator in expressions). Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name.

### `set_textbox_style_bulk`

*idempotent*

Apply the same style kwargs to every named textbox in one call. Same kwarg surface as set_textbox_style. Missing names land in skipped rather than raising. Returns {textboxes, skipped, changed} where changed is the union of sub-paths affected across all textboxes. Pair with find_textboxes_by_style to discover names by current style. cells (a list of {tablix, header | field | index, row}) adds tablix cells picked by column header or field.

### `find_textboxes_by_style`

*read-only*

Search for textboxes matching one or more style filters. Filters AND together (every supplied filter must match). Returns [{name, location, matched_fields}] where location is best-effort 'body' / 'header' / 'footer' / 'tablix:`<name>'` / 'rectangle:`<name>'`. Returns [] when no filters supplied. Pair with set_textbox_style_bulk for discovery+apply patterns (e.g. 'recolor every red textbox').

### `find_textbox_by_value`

*read-only*

Find every Textbox whose `<Value>` text matches a Python regex pattern. Searches Body / PageHeader / PageFooter. A textbox with multiple matching runs surfaces once per match. Returns [{textbox, value, region}]. Useful for finding stale =Parameters!Old.Value references after rename_parameter, or any cross-cutting expression edit.

### `style_tablix_row`

*idempotent*

Apply the same style kwargs to every cell in a tablix row in ONE call. Replaces the 12-individual-set_textbox_style-calls-per-row pattern. row accepts: integer (0-based body row index), 'header' (first leaf with KeepWithGroup=After — the column header row), 'details' (the Details leaf row), '`<group>_header'` (header row of a named row group), '`<group>_footer'` (footer row when present, e.g. after add_subtotal_row). Same style kwargs as set_textbox_style (font_*, color, background_color, border_*, padding_*, writing_mode, can_grow, can_shrink, etc.). Delegates writes to set_textbox_style_bulk. Returns {tablix, row, row_index, kind, cells, changed, skipped}.

### `set_alternating_row_color`

*idempotent*

Zebra-stripe a tablix's detail row by writing BackgroundColor=IIf(RowNumber(Nothing) Mod 2, color_a, color_b) on every detail cell's Textbox. Walks the row hierarchy to find the Details leaf — works after add_row_group nests the structure deeper. Replaces any existing BackgroundColor.

### `set_conditional_row_color`

*idempotent*

Color every cell of a tablix's detail row based on the value of one of its fields. Builds a Switch(...) expression mapping field values to colors and writes it as BackgroundColor on every detail cell. value_expression is the field reference (e.g. 'Fields!Status.Value' — a leading '=' is accepted). color_map is an ordered dict of value->color (e.g. {"Red":"#FF0000","Yellow":"#FFFF00"}); first match wins. Unmatched values fall back to default_color (default 'Transparent'). When case_sensitive is False (default), wraps the field reference in UCase() and uppercases the keys for case-insensitive matching. Walks the row hierarchy to find the Details leaf — works after add_row_group nests the structure. Replaces any existing BackgroundColor.

## `layout`

### `set_image_sizing`

*idempotent*

Set `<Image>/<Sizing>` on a named Image. sizing ∈ AutoSize / Fit / FitProportional / Clip. AutoSize renders at native size (box grows to fit); Fit stretches to fill (ignores aspect ratio); FitProportional preserves aspect ratio; Clip renders at native size, clipped to the box. Idempotent: same value → {changed: false}, no save.

### `set_image_source`

*idempotent*

Repoint an existing `<Image>` at a different embedded image entry without delete-and-readd. Sets Source=Embedded and rewrites `<Value>` to embedded_name. Refuses with a clear error if embedded_name isn't in `<EmbeddedImages>` — leaving a dangling reference would render as a broken image. Add the image first via add_embedded_image. Idempotent: same (Source, Value) pair → {changed: false}, no save. Returns {name, kind, changed: bool}.

### `get_embedded_image_data`

*read-only*

Read an embedded image's base64 `<ImageData>` for porting it to another report without re-reading from disk. Returns {name, mime_type, base64, byte_size}. base64 is the raw text of the ImageData element; byte_size is the decoded size for sanity-checking. Refuses with ElementNotFoundError if the named entry isn't in `<EmbeddedImages>`.

### `add_rectangle`

Add a `<Rectangle>` to `<Body>/<ReportItems>`. With no contained_items the rectangle is empty (a visual frame). With contained_items=[name1, name2, ...], each named body item is MOVED into the rectangle's `<ReportItems>` and its `<Top>/<Left>` recalculated so the on-screen position is preserved. Every moved item must lie inside the rectangle's box: a misfit is refused (naming each item and how far it overflows) unless fit=true, which grows the rectangle to enclose them. Refuses on duplicate name or if any contained_item isn't in `<Body>/<ReportItems>`; nothing is written on refusal. Returns {name, kind: 'Rectangle', moved}, plus {fitted, top, left, width, height} when fit is passed.

### `add_list`

Add a List (single-cell repeating Tablix) bound to a dataset. RDL has no distinct `<List>` element — Report Builder's List is a Tablix with one column, one detail row, and a Rectangle inside the cell. Items placed in the rectangle repeat once per dataset row. The inner rectangle is named '`<name>_Rect'` for subsequent lookup. Returns {name, kind: 'Tablix', dataset, rectangle}.

### `add_line`

Add a `<Line>` to `<Body>/<ReportItems>`. RDL Line semantics: top/left is the start point; width/height is the offset (vector) to the end point — horizontal line uses height='0in', vertical uses width='0in'. Optional color, line_thickness ('1pt' default), line_style (Solid/Dashed/Dotted/Double/etc.). Returns {name, kind: 'Line'}.

### `set_body_item_position`

*idempotent*

Move an existing named ReportItem inside `<Body>` to (top, left). Preserves all other properties (size, style, group structure). top and left are passed through verbatim — RDL accepts any size unit (2cm, 0.75in, 108pt). Errors if no body item by that name.

### `set_header_item_position`

*idempotent*

Move an existing named ReportItem inside `<PageHeader>` to (top, left). Errors if there is no `<PageHeader>` (call set_page_header first) or no item by that name. Returns `warnings` when the item now hangs below the band or past the printable page width; grow_band=true grows the band to fit instead (result gets band_height).

### `set_footer_item_position`

*idempotent*

Move an existing named ReportItem inside `<PageFooter>` to (top, left). Errors if there is no `<PageFooter>` (call set_page_footer first) or no item by that name. Returns `warnings` when the item now hangs below the band or past the printable page width; grow_band=true grows the band to fit instead (result gets band_height).

### `set_body_item_size`

*idempotent*

Resize an existing named ReportItem inside `<Body>`. At least one of width / height must be supplied; missing fields are left untouched. Same RDL size-string convention as the position tools.

### `set_header_item_size`

*idempotent*

Resize a named item inside `<PageHeader>`. Mirrors set_body_item_size — at least one of width / height; missing fields untouched; idempotent ({changed: false} when nothing differs). Returns `warnings` when the item outgrows the band or the printable page width; grow_band=true grows the band to fit instead (result gets band_height).

### `set_footer_item_size`

*idempotent*

Resize a named item inside `<PageFooter>`. Same shape as set_header_item_size.

### `list_body_items`

*read-only*

List every named ReportItem at the top level of `<Body>`. Returns name, type (Tablix / Textbox / Image / Rectangle / Subreport / Chart / etc.), top, left, width, height. Use before set_body_item_position / set_body_item_size when you don't already know what's in the body. Each entry has `parent` (null at the top level); recursive=true also lists the items inside each Rectangle, right after it, with parent = the Rectangle's name (their top/left are relative to it).

### `list_header_items`

*read-only*

Same shape as list_body_items but for `<PageHeader>`.

### `list_footer_items`

*read-only*

Same shape as list_body_items but for `<PageFooter>`.

### `get_image`

*read-only*

Return effective state of a named Image: position, size, Source (External / Embedded / Database), Value, Sizing (AutoSize / Fit / FitProportional / Clip), MIMEType, Style, Visibility.

### `get_rectangle`

*read-only*

Return effective state of a named Rectangle: position, size, names of contained ReportItems, Style, Visibility.

### `set_page_setup`

*idempotent*

Update `<Page>` dimensions, margins, and column count on the first ReportSection. All fields are optional — only what's passed gets written. columns=1 strips the `<Columns/>` element.

### `set_page_orientation`

*idempotent*

Set page orientation by swapping PageHeight and PageWidth when the current orientation doesn't match the requested one. Idempotent. Accepts 'Portrait' or 'Landscape'.

### `set_body_size`

*idempotent*

Set the body's rendering region inside the page. `<Body>/<Height>` and `<ReportSection>/<Width>` (the sibling of `<Body>`) — both are RDL size strings (e.g. '14in', '9in', '297mm'). Either or both kwargs required. Distinct from set_page_setup (which sets the paper chrome `<Page>/<PageWidth>/<PageHeight>`) and set_body_item_size (size of items inside the body). Use this when a wide tablix or chart needs the body region expanded — without it the right edge is clipped at preview time. Idempotent: same value → empty changed. Returns {kind: 'Body', changed: list[str]}.

### `fit_page_to_body`

*idempotent*

Fix a body wider than the page (lint body-wider-than-page: blank pages after every page). mode='page' (default) widens PageWidth to columns x body width + column spacing + left/right margins (a page already wide enough is left alone). mode='body' shrinks the body width (ReportSection Width) to the right edge of its rightmost item; errors when even that exceeds the printable width. Returns {mode, changed, page_width, body_width, printable_width}.

### `center_item`

*idempotent*

Centre a named report item in its container by setting Left (axis='horizontal', default), Top ('vertical') or both: Left = (container width - item width) / 2. Container = the body (body width) for body items, the printable width (page width minus margins) for page header/footer items, the Rectangle for items inside one. A Tablix is centred on the width its columns render at. Errors when the item is larger than its container. Idempotent. Returns {name, container, axis, top, left, changed, warnings}.

### `set_page_header`

*idempotent*

Create or update `<PageHeader>`: height plus PrintOnFirstPage / PrintOnLastPage flags. All fields optional — only what's passed gets written, except that a section always gets a Height (0.5in when none is given). Returns `warnings` when the new height is shorter than the band's items.

### `set_page_footer`

*idempotent*

Same as set_page_header, for `<PageFooter>`.

### `add_header_textbox`

Add a Textbox to `<PageHeader>/<ReportItems>`. text accepts static strings or RDL expressions (=Parameters!DateFrom.Value). Pass raw text — encoding is handled; don't pre-encode XML entities (use `&` not `&amp;`, including for the VB.NET string-concat operator in expressions). Creates the band when missing and grows its Height to max(0.5in, top + height); returns band_height, plus `warnings` when an existing band grew or the item passes the printable page width. Fields!X needs an aggregate with a dataset scope here, e.g. =First(Fields!X.Value, "DataSet1").

### `add_footer_textbox`

Same as add_header_textbox, for `<PageFooter>`.

### `add_header_image`

Add an Image to `<PageHeader>/<ReportItems>`. image_source is External (URL in value), Embedded (EmbeddedImage Name in value), or Database (=Fields!Photo.Value-style expression). Sizes the band like add_header_textbox (band_height, warnings).

### `add_footer_image`

Same as add_header_image, for `<PageFooter>`.

### `remove_header_item`

*destructive*

Remove a named Textbox or Image from `<PageHeader>/<ReportItems>`. Empties the ReportItems block when the last item leaves.

### `remove_footer_item`

*destructive*

Same as remove_header_item, for `<PageFooter>`.

### `add_body_textbox`

Add a Textbox to `<Body>/<ReportItems>`. text accepts static strings or RDL expressions (e.g. =Globals!ReportName). Coexists with the existing tablix; rejects names already in use. Pass raw text — encoding is handled; don't pre-encode XML entities (use `&` not `&amp;`, including for the VB.NET string-concat operator in expressions).

### `add_body_image`

Add an Image to `<Body>/<ReportItems>`. image_source: External (URL), Embedded (EmbeddedImage Name), Database (=Fields!Photo.Value).

### `remove_body_item`

*destructive*

Remove a named item (Textbox, Image, or Tablix) from `<Body>/<ReportItems>`. Destructive but explicit — raises if the name doesn't match anything.

### `add_embedded_image`

Read a real image file off disk, base64-encode it, and store it under `<EmbeddedImages>`. Reference it later with image_source='Embedded' + value=`<name>`. Supported MIME types: image/bmp, image/gif, image/jpeg, image/png, image/x-png. The file's magic bytes are sniffed and must match mime_type — claiming PNG bytes as image/jpeg is rejected here rather than letting Report Builder fail at preview time.

### `list_embedded_images`

*read-only*

List embedded images in the report by name and MIME type. Returns an empty list when `<EmbeddedImages>` is absent.

### `remove_embedded_image`

*destructive*

Remove a named embedded image. Refuses (lists offending Image elements) when any `<Image Source="Embedded"><Value>=name>` still references it; pass force=True to remove anyway and accept the dangling references. Drops the empty `<EmbeddedImages/>` block when removing the last entry.

### `set_element_visibility`

*idempotent*

Set `<Visibility>` on any named ReportItem (Tablix, Textbox, Image, Rectangle, Subreport, Chart, Line, Map, GaugePanel). For group-level visibility use set_group_visibility; for detail-row use set_detail_row_visibility. hidden_expression is 'true' / 'false' or an =expression (required unless clear). toggle_textbox names the textbox that toggles expand/collapse: it must exist, not be the item or inside it, be in the same region (body/header/footer), and if it is a tablix cell the item must be inside that tablix. Omit toggle_textbox to keep the existing toggle; pass '' or null to remove it. clear=true removes `<Visibility>`. Invalid input is refused with the file untouched. Returns {element, kind, hidden_expression, toggle_textbox, changed}.

## `interactivity`

### `set_textbox_action`

*idempotent*

Set `<Textbox>/<Action>` to a Hyperlink (URL), Drillthrough (another report + optional parameters), or BookmarkLink (jump within document). target_expression accepts literal text or =expression. drillthrough_parameters is a list of {name, value} dicts wired into `<Drillthrough>/<Parameters>`. Idempotent: same action_type + target + parameters → {changed: false}, no save. Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name.

### `set_image_action`

*idempotent*

Same shape as set_textbox_action but operates on a named `<Image>`. action_type ∈ Hyperlink / Drillthrough / BookmarkLink. Returns {image, kind, action_type, changed}.

### `set_textbox_tooltip`

*idempotent*

Set `<Textbox>/<ToolTip>`. Literal text or =expression. Pass '' to clear. Idempotent. Returns {textbox, kind, changed: bool}. Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name.

### `set_document_map_label`

*idempotent*

Set `<DocumentMapLabel>` on any named ReportItem (Textbox / Image / Rectangle / Chart / Tablix / etc.). Surfaces in the rendered report's navigable document-map / table-of-contents pane. Pass '' to clear. Idempotent. Returns {element, kind, changed: bool}.

### `set_chart_series_action`

*idempotent*

Set `<ChartSeries>/<Action>` to a Hyperlink (URL), Drillthrough (another report + optional parameters), or BookmarkLink. Same kwarg surface as set_textbox_action / set_image_action; the chart series is addressed by (chart_name, series_name). Schema-aware insertion respects ChartSeries child order. Idempotent on structural equality of the inner block.

## `admin`

### `duplicate_report`

Clone an .rdl to a new path. When regenerate_ids=true (default), every `<rd:DataSourceID>` and any `<rd:ReportID>` is rewritten to a fresh uuid4() so Power BI Report Builder doesn't refuse to load the duplicate due to identity collision. The copy is written atomically (temp file + rename) either way. Refuses if dst_path already exists. Returns {src, dst, regenerated_ids: list[str]}.

### `create_report`

Emit a minimal valid RDL from scratch at `path`. Refuses if `path` exists (no clobbering). Default page_setup is US Letter portrait with 1in margins; pass any subset of {page_height, page_width, margin_top, margin_bottom, margin_left, margin_right, body_width, body_height} to override. Pass `datasource` {name, workspace_url, dataset_name, provider, integrated_security} to wire a real PBI XMLA endpoint; omit for a placeholder DataSource1 + DataSet1 stub the caller fills in via subsequent tools. Validates structurally + against the bundled XSD before saving (atomic .tmp + rename). Returns {path, validated, size_bytes}.

### `raw_xml_view`

*read-only*

Read-only XPath query against the report. Returns matched elements as serialised XML strings. XPath context is `<Report>` (the root); 'r:' is bound to the RDL namespace, 'rd:' to the rd namespace. Examples: "r:DataSources/r:DataSource[@Name='X']" / ".//r:Textbox[@Name='X']/r:Style". Returns [] when nothing matches; raises on malformed xpath. Output is capped at max_bytes (default 40000); a capped result ends in an explicit '`<!-- pbirb-mcp: truncated ... -->'` marker.

### `raw_xml_replace`

*destructive, idempotent*

Replace the single element matched by xpath with new content. content is parsed with RDL as the default namespace and 'rd:' bound, so bare names like `<Textbox><Value>x</Value></Textbox>` work without explicit xmlns. Refuses on zero matches, multiple matches, or if the xpath targets the `<Report>` root. Saves atomically. Returns {xpath, kind, changed}.

### `backup_report`

Copy the report to `<path>.bak.<UTC-timestamp>`. Original is untouched. Cheap explicit checkpoint to call before a destructive batch (remove_*, rename_parameter, etc.). Returns the backup path. There are no automatic backups yet (PBIRB_MCP_AUTO_BACKUP is not implemented, #63): call this yourself before a risky edit.

### `restore_from_backup`

*destructive*

Restore a backup file over its original target. target_path defaults to the path implied by the backup's `<target>.bak.<UTC-timestamp>` filename; pass target_path explicitly when the backup name doesn't match that shape. Refuses if target mtime is newer than backup (staleness guard) — pass force=True to override. Returns {source, restored_to, bytes_restored}.
