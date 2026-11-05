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

Edit results: every tool that writes a report returns `changed` — `false`
(or `[]`) when the call was a no-op and the file was left untouched,
otherwise `true` or the list of what changed — plus `warnings` when there
are any. With `PBIRB_MCP_AUTO_VERIFY` on, the result is nested as
`{result, verify}`.

Saving: every tool that writes a report writes a temp file next to it and
renames it over the report, so a failed save leaves the report as it was.
Through a symlink the link's target is updated and the link stays a link;
the report keeps its permission bits (and owner, where allowed); a
read-only report is refused with a `PermissionError` and left untouched.
The rename gives the report a new inode, so other hard links to it keep
the old content.

Tablix width: a tablix renders at the sum of its column widths plus its
row-header sizes (the widest `TablixHeader/Size` path of the row
hierarchy), whatever its `<Width>` says. Every tool that adds, removes or
resizes a column or a row header — add_tablix_column, remove_tablix_column,
set_column_width, add_static_column, add_subtotal_column, convert_to_matrix,
add_row_group / remove_row_group with a row-header cell, and
remove_column_group's legacy-column cleanup — sets `<Width>` to that sum
(in the unit `<Width>` already uses) and reports the new value as
`tablix_width` (convert_to_matrix / add_subtotal_column also list
`tablix_width` in `changed`), so `tablix-width-mismatch` does not fire on
their output. set_tablix_size refuses a contradicting width unless
`distribute=true`.

Paths: a leading `~` in any path argument (`path`, `*_path`, `*_dir`) is
expanded to the user's home directory before the allowed-roots check and
the tool see it. A file that is well-formed XML but whose root isn't an
RDL 2016 `<Report>` is refused by every tool except `validate_report`
(which reports it as a `structural` error) with `NotAnRDLReport`, naming
the root element.

## `core`

### `describe_report`

*read-only*

Top-level inventory of an RDL: data sources, datasets, parameters, tablixes, charts, and page setup. Always the first call when planning edits. `tablixes` is a list of shape hints [{name, rows, columns, has_groups, has_subtotals, has_spans, column_details}]. column_details lists each body column {index, header_text, header_cell, data_cell, bound_fields} — how to find 'the Asset Name column' and its cell textbox names. `charts` is a top-level array of chart names. Also: `subreports` [{name, report_name, parameters}], `maps`, `gauges`, `body` {height, width}, `nested_items` (items inside Rectangles, with parent/region); `page` fills absent sizes with the RDL defaults (11in x 8.5in, 0in margins) and lists them in `page.defaulted`, plus page.columns, page.column_spacing, page.body {width, height} and page.printable_width (width minus left/right margins) for overflow checks.

### `validate_report`

*read-only*

Run schema/structural validation against an .rdl. Returns {valid, errors, xsd_used}. Structural checks always run (root element + required top-level sections). The Microsoft RDL 2016/01 XSD is bundled — when it's loaded, xsd_used is True and the schema-conformance bug class Power BI Report Builder rejects on load gets caught here. `xsi:type` attributes on elements outside the RDL namespace — Report Builder's `rd:DesignerState` query-designer state (`QuerySpecification xsi:type="MDXQuerySpecification"`) — are ignored for the XSD check, since they name types from schemas that aren't bundled; the file itself isn't changed. If the bundled XSD is missing (source-build without package-data) a {severity:warning, rule:'xsd-not-bundled'} issue surfaces instead of silent skip. Each issue is {severity, rule, location, message, suggestion?}; valid is True iff no severity='error' issue.

### `verify_report`

*read-only*

One-shot static check: union of validate_report and lint_report. Returns {valid, issues, xsd_used} where valid is True iff no issue has severity='error'. Warnings (including 'xsd-not-bundled' when the schema file is missing) don't invalidate the report. Use this as the single 'is the report OK?' tool, or set PBIRB_MCP_AUTO_VERIFY=1 to have it run after every mutating call outside a transaction (commit_editing_transaction verifies at commit). With baseline_path (e.g. a backup taken before an edit), or inside a transaction, each issue gets new: true|false plus introduced_count / preexisting_count.

### `lint_report`

*read-only*

Run static-analysis lint rules against an .rdl (`lint_report` returns `rules_run`; the rules live in `pbirb_mcp/ops/lint_rules/`). They cover the known bug classes (multi-value-eq, query-parameter-mismatch, multi-value-query-binding, missing-field-reference, dangling-embedded-image, database-image-missing-mimetype, pbidataset-at-prefix, parameter-layout-out-of-sync, double-encoded-entities, stale-designer-state, tablix-span-misplaced, dataset-fields-out-of-sync, dangling-dataset-reference-field, parameter-state, duplicate-name, invalid-identifier, tablix-structure, chart-structure, etc.). Returns {issues, rules_run} with each issue {severity (error|warning), rule, location, message, suggestion?}; location names the nearest named items, e.g. Tablix[Name='T']>Textbox[Name='X']>Value. Optional `rules` selects a subset by name (empty = all).

### `dry_run_edit`

*read-only*

Apply a list of {tool, args} ops to a tempfile clone of the report; return a diff and a verify (XSD + structural + lint) report. The original file is NEVER modified. The harness auto-injects `path` into each op's args, so callers don't supply it. On op failure, dispatch stops; partial diff + verify are still returned. Use this to preview risky multi-step edits before committing.

The result has the apply_edits shape: `applied` is `[{tool, ok, result|error}]` with `result` the op's result object (`result_text`, the same result as a JSON string, is deprecated and will be removed in the next release), and `verify` is `{valid, issues, rules_run, xsd_used, introduced_count, preexisting_count, introduced_error_count}`. XSD and structural errors are among `issues`; `valid` is false only when an error-severity issue remains (warnings don't count, as in verify_report); each issue carries `new: true` unless the source already had it.

`diff` picks the change report: `unified` (default) is the unified text diff; `summary` is a list of `"<change> <element path>"` lines, e.g. `changed Textbox[Title]/Paragraphs/Paragraph/TextRuns/TextRun/Value` or `added Tablix[T]/TablixBody/TablixColumns/TablixColumn[4]`; `none` leaves it out. Either diff is capped at max_diff_bytes (default 40000, 0 = no cap), the truncation marker included: a capped unified diff ends in a `\ pbirb-mcp: diff truncated ...` line, a capped summary in a `"... N more"` line, and diff_truncated is true. `diff_stats` (lines added/removed, hunks of the text diff) is always returned. summary=true drops the op results, reduces verify to {valid, xsd_used, error_count, issue_count, issues_by_rule} and leaves the diff out unless `diff` is given.

### `start_editing_transaction`

Open the report and start an editing transaction. Returns {transaction_id, path, expires_at}. Pass transaction_id to subsequent edit tools to mutate the live in-memory tree WITHOUT touching disk between calls — eliminates the per-edit parse+serialize round-trip. The transaction times out after PBIRB_MCP_TRANSACTION_TIMEOUT_S seconds (default 600); call commit_editing_transaction to flush or cancel_editing_transaction to discard. Refuses if an active transaction already owns this path.

### `commit_editing_transaction`

Lint the in-memory tree, save to disk once (atomic .tmp + rename), and deregister. Aborts (saved=False) if lint surfaces any severity='error' issue — the transaction stays OPEN so the caller can fix the offending state and re-commit. Also refuses (saved=False, error) when the file changed on disk since the transaction started, unless force=true. Returns {transaction_id, path, saved, verify}.

### `cancel_editing_transaction`

Discard a transaction. The in-memory tree is dropped; the on-disk file is unchanged from when the transaction was started. Returns {transaction_id, path, discarded}.

### `apply_edits`

Atomic batch: open once → apply ops → lint → save once. Opens an internal transaction against path, dispatches each {tool, args} op through the JSON-RPC tools/call path with transaction_id injected, and commits at the end. On any op failure or lint-error at commit, rolls back — disk is byte-identical to its pre-call state. Compare with dry_run_edit (clones to tempfile, never touches the real file): use dry_run_edit to preview a plan, apply_edits to land it. Returns {applied: [{tool, ok, result|error}], verify, committed}; `verify` has commit_editing_transaction's keys {valid, issues, rules_run, xsd_used, introduced_count, preexisting_count, introduced_error_count} — nothing checked (valid false, no issues) when an op failed — the same keys as dry_run_edit's.

### `get_expression_reference`

*read-only*

Return a static cheat-sheet of common RDL expression patterns: globals, parameters, fields, aggregates (incl. the scope argument, CountRows, RowNumber, RunningValue, InScope), lookups (Lookup / LookupSet / MultiLookup), conditionals, strings, dates. Each entry is {name, syntax, example, description}. Call this when authoring a textbox value or filter expression instead of guessing — and note the explicit encoding hint for the `&` concat operator.

### `count_where`

*read-only*

Emit =Count(IIf(`<condition>`, 1, Nothing)) — count the rows matching a condition (Count skips Nothing; 0 when none match). condition is an RDL expression body; one leading '=' is dropped. scope (a dataset, data region or group name) appends the aggregate's scope argument, `, "<scope>"` (quotes doubled) — needed outside a data region. Returns a complete top-level expression suitable for set_textbox_value or any RDL `<Value>` sink.

### `sum_where`

*read-only*

Emit =Sum(IIf(`<condition>`, `<field_expression>`, Nothing)) — sum a value where a condition holds. The false branch is Nothing, not 0: an Int32 0 mixed with the Double / Decimal values of Power BI numeric columns fails with 'aggregate function on data of varying data types'; Sum ignores Nothing (the result is blank when no row matches). Both args are expression bodies; one leading '=' on each is dropped. scope works as in count_where.

### `iif_format`

*read-only*

Emit =IIf(`<condition>`, `<true_value>`, `<false_value>`). All three args are expression bodies (one leading '=' on each is dropped); string literals must already be quoted, e.g. true_value='"Yes"'.

## `data`

### `get_datasets`

*read-only*

Full DAX command text, fields, query parameters, and dataset-level filters for every DataSet in the report. Each entry is exactly the get_dataset shape (including designer_state_present). `response_format: "concise"` keeps name, data_source, field and query-parameter names, filter_count and designer_state_present.

### `update_dataset_query`

*idempotent*

Replace the DAX command text of a named dataset. The full DAX expression is written verbatim — no XML-entity decoding, so a DAX literal such as SUBSTITUTE(col, "&amp;", "&") is kept exactly (the file escapes it once); empty bodies are rejected. Optional alias_strategy='preserve_field_names' keeps every `<Field Name>` (so Fields!X.Value references keep resolving) and re-aims `<DataField>` cells at the new DAX columns (Table[Col] or [Alias]): Fields whose column is still returned stay as they are (any order); a Field whose column vanished is re-aimed only when exactly one new column took its slot in the old DAX's column order; everything else is left untouched and reported in warnings. dry_run=true returns the result (including mapped) without saving.

Report Builder's Query Designer keeps its own copy of the query in `<rd:DesignerState>/QueryDefinition/Query/Statement`. designer_state='sync' (default) rewrites that Statement to the new DAX (`designer_state_synced`); only the text is updated, because the designer's Select/Items graph cannot describe arbitrary DAX. designer_state='drop' removes `<rd:DesignerState>` (`designer_state_dropped`) so Report Builder opens the query in its text editor and can't regenerate the old query from the stale graph — prefer it for hand-written DAX.

### `add_query_parameter`

Add a `<QueryParameter>` binding to a dataset's query. Use to wire report parameters into DAX (e.g. =Parameters!DateFrom.Value).

PBIDATASET parameter naming rule: in DAX/`<CommandText>`, write @MyParam (with @). In `<QueryParameter Name=...>`, write MyParam (no @). SQL/MDX use @ in both places. This tool detects the dataset's provider and AUTO-STRIPS a leading @ from `name` for PBIDATASET datasets, returning {normalised: true, warning: ...}. Pass force_at_prefix=true to override (rare; needed for some RSCustomDaxFilter patterns).

`warnings` lists the lint findings about the new binding: a Power BI dataset's DAX that never uses `@<name>` (query-parameter-mismatch), a multi-value report parameter passed raw into an `@<name>` the DAX compares as a scalar (multi-value-query-binding), a `Parameters!X` in the value that names no report parameter (undefined-parameter).

### `update_query_parameter`

*idempotent*

Change the value expression of an existing query parameter. Same PBIDATASET @-prefix normalisation as add_query_parameter applies on lookup; legacy Name='@X' parameters that already exist on disk are addressable via either @X or X. Returns the same `warnings` as add_query_parameter.

### `set_datasource_connection`

*idempotent*

Repoint a DataSource at another Power BI workspace / semantic model, in the source's own shape. provider omitted keeps the current provider: a PBIDATASET source stays PBIDATASET (pbiazure:// ConnectString with its other keys such as Identity Provider kept, `<rd:SecurityType>None`, `<rd:PowerBIWorkspaceName>` / `<rd:PowerBIDatasetName>` labels updated — the workspace label is the bare name, any powerbi://…/myorg/ prefix stripped); a SQL source keeps the XMLA shape (Data Source=powerbi://…/myorg/`<workspace>`;Initial Catalog=`<model>`, `<IntegratedSecurity>`, `<rd:SecurityType>`). A source with no DataProvider gets the SQL shape; any other provider is refused unless provider='sql'/'pbidataset' converts it. PBIDATASET's Initial Catalog is the model's internal id (sobe_wowvirtualserver-`<guid>` in Report Builder output): catalog_id sets it; without it the current id is kept when the workspace and model labels are unchanged, otherwise the display name is written and a warning returned. identity_provider sets PBIDATASET's Identity Provider. Values are quoted when they contain `;`, `=` or quotes, so a name can't inject keys. Returns {name, data_provider, connect_string, integrated_security, changed, before, after, warnings}; before/after are get_data_source shapes.

### `list_data_sources`

*read-only*

Return a rich list of every `<DataSource>` in the report — name, data_provider, connect_string, integrated_security, shared_reference, security_type, data_source_id, powerbi_workspace_name, powerbi_dataset_name (the rd: labels). describe_report.data_sources returns names only; this tool is the authoring-friendly read.

### `get_data_source`

*read-only*

Single-DataSource read-back (parity with get_textbox / get_image / get_rectangle / get_chart). Returns the same shape as one entry of list_data_sources.

### `add_data_source`

Create a new `<DataSource>` for a Power BI XMLA endpoint. workspace_url accepts a bare workspace name or a full powerbi:// URL. Generates a fresh rd:DataSourceID GUID. Refuses if a DataSource of the same name already exists. provider='sql' (default) emits the legacy DataProvider=SQL + powerbi:// ConnectString + `<IntegratedSecurity>` + `<rd:SecurityType>Integrated` shape; provider='pbidataset' emits the shape Power BI Report Builder writes — DataProvider=PBIDATASET, `Data Source=pbiazure://api.powerbi.com/;[Identity Provider=…;]Initial Catalog=…;Integrated Security=ClaimsToken`, `<rd:SecurityType>None`, plus `<rd:PowerBIWorkspaceName>` (bare workspace name; a powerbi:// URL prefix is stripped) / `<rd:PowerBIDatasetName>` labels. catalog_id is the Initial Catalog — for PBIDATASET the model's internal id (sobe_wowvirtualserver-`<guid>`); without it the display name is used and a warning returned. identity_provider (PBIDATASET only) sets Identity Provider. Values are quoted when they contain `;`, `=` or quotes. Returns the get_data_source shape plus {kind, warnings}. create_report's datasource and set_datasource_connection write the identical shape.

### `remove_data_source`

*destructive*

Remove a named `<DataSource>`. Refuses by default if any DataSet/Query/DataSourceName still references it; the error lists the offending locators. Pass force=True to remove anyway. A `<DataSourceReference>` is a shared data source's server path, not an embedded name, so it never counts as a reference. Always refuses to remove the report's last data source (even with force): a report without one is invalid RDL — add the replacement first, or repoint it with set_datasource_connection.

### `rename_data_source`

Rename a `<DataSource>` and rewrite every DataSet/Query/DataSourceName that names it; `references_rewritten` counts them. `<DataSourceReference>` (a shared data source's server path) is left untouched. Atomic: stages all matches before committing. Errors if new_name already exists or equals old_name.

### `list_dataset_filters`

*read-only*

List dataset-level filters in document order. Returns [{expression, operator, values, data_type}] (data_type: the `<FilterValue DataType>`, null when unset); index in the list is the stable handle for remove_dataset_filter. DataSet filters apply to every consumer of the dataset (every Tablix / Chart bound to it); use list_tablix_filters for per-tablix filtering.

### `add_dataset_filter`

Append a `<Filter>` to the named dataset's `<Filters>` block. operator ∈ Equal / NotEqual / GreaterThan / LessThan / GreaterThanOrEqual / LessThanOrEqual / Like / In / Between / TopN / BottomN / TopPercent / BottomPercent (case-sensitive; the schema enumerates them). Arity is checked: Between takes exactly 2 values (low, high), In one or more, every other operator exactly 1; TopN / BottomN take an integer literal or an `=`-expression, TopPercent / BottomPercent a number or an expression. Returns the new filter's index for later removal. Optional field_format wraps the expression as Format(`<body>`, fmt) to coerce typed fields for string-parameter comparison.

value_data_type (Boolean / DateTime / Integer / Float / String) writes `<FilterValue DataType="...">` on every value, so a literal such as `50` compares as an Integer instead of a String; Integer / Float / Boolean literals are checked against the type. Response `warnings` (best-effort): a `Fields!X.Value` whose rd:TypeName group differs from a `Parameters!Y` value's DataType; a numeric or date field compared with bare literal values and no value_data_type (Report Builder's run-time "comparison failed") — the warning names the DataType to pass. Fields wrapped in Format( / CStr( / .ToString compare as strings and are not checked (so field_format clears the warning), nor are TopN-style counts.

Text handling: a literal value or expression (no leading `=`) is written verbatim — `R&amp;D` stays `R&amp;D`. An `=`-expression is entity-decoded (`="A &amp; B"` → `="A & B"`, the guard against double-encoded expressions) and the rewrite is reported in `warnings`. `expression` / `values` in the response echo what was written.

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

Remove a calculated `<Field>` by name. Refuses if the field is data-bound (carries `<DataField>` instead of `<Value>`) — those reflect the source query's columns. Drop a data-bound field via remove_dataset_field, or rewrite the dataset query via update_dataset_query. Also refuses while the field is still referenced — the same scope-aware check as remove_dataset_field; pass force=True to remove anyway.

### `remove_dataset_field`

*destructive*

Remove a data-bound `<Field>` by name (one with `<DataField>`). Symmetric counterpart to remove_calculated_field. Refuses on calculated fields (use remove_calculated_field instead) and on still-referenced fields. A reference is `Fields!<name>.<member>` or `Fields("<name>")` in an expression evaluated against this dataset — the scope argument of an aggregate or Lookup, else the enclosing data region's DataSetName (Tablix, Chart, Gauge, map), else the DataSet itself for calculated fields and dataset filters — or a report parameter's ValidValues/DefaultValue DataSetReference whose ValueField/LabelField names the field. A same-named field used by a region bound to another dataset doesn't block. The error lists up to 5 locators such as `Tablix[Name='T']>Textbox[Name='Amount']/…/Value` or `ReportParameter[Name='P']/ValidValues/DataSetReference/ValueField`. Pass force=True to delete anyway. Closes the cookbook flow: refresh_dataset_fields lists orphans, remove_dataset_field drops them. Returns {dataset, removed, kind: 'DataBoundField'}.

### `add_dataset_field`

Append a data-bound `<Field>` to a dataset's `<Fields>` block. Writes `<DataField>data_field</DataField>` (and optional `<rd:TypeName>type_name</rd`:TypeName>). Distinct from add_calculated_field which writes `<Value>` for derived fields. Use after rewriting the DAX to declare a new column that came back from the query but isn't yet in `<Fields>`. Refuses on duplicate field name.

### `refresh_dataset_fields`

*idempotent*

Sync a dataset's `<Fields>` block against the shape detected in its DAX `<CommandText>`. Eliminates the manual 'open Report Builder → right-click → Refresh Fields' step after a query rewrite. Matches existing Fields to the query's result columns by `<DataField>` (Table[Col] / [Alias]), not by Name. Adds a Field per unbound column the way Report Builder does: Name sanitised (Amount (USD) -> Amount__USD_; duplicates Name1, Name2 ...), DataField Table[Col] or [Alias]. Lists orphans without auto-removing (caller decides what to drop). Returns {added: [{name, data_field}], orphans: [{name, data_field}], unchanged: [names], warnings}. When the DAX can't be fully analysed (e.g. bare EVALUATE 'Table') nothing is added and warnings explain why.

### `set_dataset_data_source`

*idempotent*

Point a dataset's `<Query>/<DataSourceName>` at another data source — e.g. to retire a duplicate, unused source. The source must exist (the error lists the data sources). `warnings` flags a switch between provider families: a Power BI source (PBIDATASET, or SQL with a `powerbi://` connect string — DAX, `@Name` in the query and a bare `Name` in `<QueryParameter>`) and a SQL / other one (`@Name` in both places); the command text and query-parameter names probably need rewriting too. Returns {dataset, data_source, previous, changed, warnings}; the same source again → changed: false, no write.

### `add_dataset`

Create `<DataSet Name><Query><DataSourceName/><CommandText/></Query></DataSet>` — e.g. the lookup dataset a cascading parameter's available values need. `name` must be a valid identifier that is neither an existing dataset nor a report item / group name (scope strings such as `Sum(…, "X")` would be ambiguous); the data source must exist; `command_text` (DAX for a Power BI source) is stored verbatim and must not be empty. `fields` optionally seeds data-bound Fields from DataField bindings (`Sales[Amount]`, `'Sales'[Region (EU)]`, `[Total]`), named the way Report Builder's Refresh Fields does (`Region__EU_`, duplicates `Name1` …); refresh_dataset_fields seeds them from the DAX instead. `warnings` lists `@Name` parameters the DAX uses without a QueryParameter (add them with add_query_parameter). Returns {dataset, kind: 'DataSet', data_source, fields: [{name, data_field}], changed, warnings}.

### `remove_dataset`

*destructive*

Remove a `<DataSet>`. Refuses while anything still uses it — a `<DataSetName>` of a data region (Tablix, Chart, GaugePanel, map, CustomReportItem) or of a parameter's ValidValues / DefaultValue `DataSetReference`, or an aggregate / lookup scope string (`Sum(…, "DS")`, `Lookup(…, "DS")`, `CountRows("DS")`) outside the dataset itself — and lists up to 5 locators; force=true removes anyway. The empty `<DataSets>` block is dropped with the last dataset. Returns {removed, force, references, changed}.

### `rename_dataset`

Rename a `<DataSet>` and rewrite every reference to it: each `<DataSetName>` (data regions and parameter DataSetReference) and each aggregate / lookup / scope-function scope string naming it (`Sum(…, "Old")`, `Lookup(…, "Old")`, `CountRows("Old")`, `RowNumber("Old")`). Names in other string literals and comments are left alone. new_name is checked like add_dataset's name. Atomic. Returns {old_name, new_name, references_rewritten (elements rewritten), changed}.

### `rename_dataset_field`

Rename a `<Field>` of a dataset and rewrite its consumers — the references remove_dataset_field looks for: `Fields!<old>.<member>` and `Fields("<old>")` in expressions evaluated against this dataset (aggregate / lookup scope argument, else the data region's DataSetName, else the DataSet itself), and a parameter DataSetReference's ValueField / LabelField. Same-named fields of other datasets are untouched; names in string literals and comments too. A reference whose dataset can't be resolved statically is rewritten only when no other dataset has a field of that name, otherwise left alone and reported in `warnings`. The `<DataField>` (the query column binding) doesn't change. Atomic. Returns {dataset, old_name, new_name, references_rewritten, changed, warnings}.

### `set_field_type`

*idempotent*

Set a field's `<rd:TypeName>` — the .NET type the filter tools' type checks read. type_name is one of System.String, Boolean, Char, Byte, SByte, Int16, Int32, Int64, UInt16, UInt32, UInt64, Single, Double, Decimal, DateTime, DateTimeOffset, TimeSpan, Guid, Object, Byte[] (with or without `System.`, any case; written as `System.X`); null removes the element. Returns {dataset, field, type_name, changed}.

### `set_dataset_options`

*idempotent*

Set dataset properties: timeout (the query `<Timeout>`, seconds, 0 = none), collation (`<Collation>`, e.g. Latin1_General; "" removes it) and case_sensitivity / accent_sensitivity / kanatype_sensitivity / width_sensitivity (True / False / Auto). Omitted options are left alone; at least one is required. Returns {dataset, options: {timeout, case_sensitivity, collation, accent_sensitivity, kanatype_sensitivity, width_sensitivity}, changed}; nothing different → changed: false, no write.

### `remove_query_parameter`

*destructive*

Remove a query parameter from a dataset. Same lookup as update_query_parameter: on a PBIDATASET dataset `@X` finds the `Name="X"` binding add_query_parameter wrote (and a legacy `Name="@X"`). Cleans up the empty `<QueryParameters/>` block when removing the last one. Returns {dataset, removed (the Name removed), warnings}; warnings flags an `@X` the DAX still uses (Report Builder: "The query contains the 'X' parameter, which is not declared").

## `parameters`

### `get_parameters`

*read-only*

Report parameter declarations: name, data_type, prompt, flags (nullable, allow_blank, multi_value, hidden), plus `default_values` — {source: 'static', values: [...]} or {source: 'query', dataset, value_field} — `valid_values` — {source: 'static', values: [{value, label}]} or {source: 'query', dataset, value_field, label_field} — and `layout`, the parameter's {row, column} cell (0-based) in `<ReportParametersLayout>`; each is null when not set. `response_format: "concise"` keeps name, data_type, prompt, multi_value, hidden, layout, default_values_source and valid_values_source.

### `reorder_parameters`

*idempotent*

Reorder `<ReportParameter>` children to match the supplied names list. names MUST be a permutation of every existing parameter — no missing, no duplicates, no unknown names. Refuses (naming each `dependant -> dependency` pair) an order that declares a parameter before one it depends on: a Parameters! reference in its DefaultValue / ValidValues, or in the lookup dataset (query parameters, filters) its DataSetReference points at — Report Builder evaluates parameters in declaration order and rejects forward dependencies. The same graph drives lint's parameter-forward-dependency rule. The parameter pane's positions come from each cell's RowIndex / ColumnIndex, so by default the pane looks the same afterwards (the CellDefinition elements are reordered in lockstep but keep their slots); reflow_layout=true rewrites the slots row-major in the new order (wrapping at NumberOfColumns; NumberOfRows set to fit). Returns {order, kind, changed, layout_reflowed}; nothing to do → no save.

### `set_parameter_layout`

*idempotent*

Author `<ReportParametersLayout>/<GridLayoutDefinition>` explicitly. Writes `<NumberOfRows>` + `<NumberOfColumns>`; rewrites `<CellDefinitions>` so each name in parameter_order lands at (row=index // columns, col=index % columns). Strict permutation check (every existing parameter exactly once). rows*columns must be ≥ parameter count. Auto-creates the layout block when absent. Idempotent: same grid + order → no save. Complements reorder_parameters (declaration order) and sync_parameter_layout (gap-filling). Returns {rows, columns, order, kind, changed}.

### `set_parameter_prompt`

*idempotent*

Write the `<Prompt>` text on a ReportParameter. Empty string clears the `<Prompt>` element entirely; pass a single space ' ' for blank-but-present.

### `set_parameter_type`

*idempotent*

Set `<DataType>` on a ReportParameter. type ∈ {Boolean, DateTime, Integer, Float, String}. Rejects with ValueError if any existing literal default value or static valid value would be incompatible with the new type, or the new type is Boolean on a multi-value parameter — fix those first via set_parameter_default_values / set_parameter_available_values, then retry. Setting the type it already has returns changed=false without checking anything.

### `add_parameter`

Create a new ReportParameter with a minimal valid declaration. Appends to `<ReportParameters>` (creating it if absent). Pair with set_parameter_available_values / set_parameter_default_values afterwards for value lists. Booleans are only emitted when an explicit value is supplied. Like every parameter writer, it checks the resulting parameter (#93) and refuses a state it introduced that Report Builder rejects: MultiValue + Nullable, a multi-value Boolean, more than one static default without MultiValue, or a literal default / static valid value that doesn't fit the DataType; a hidden parameter with neither a default nor Nullable only adds a `warnings` entry. A problem the parameter already had doesn't block the edit — it comes back in `warnings` (lint rule `parameter-state` reports it).

### `remove_parameter`

*destructive*

Remove a ReportParameter by name. Refuses (lists offending locators) if the parameter is still referenced anywhere in the report — Parameters!`<name>` with any member (.Value, .Label, .Count, .IsMultiValue …), Parameters("`<name>`"), or Report.Parameters… in the `<Code>` block — in any expression (textboxes, query-parameter values, filters, other parameters' defaults / valid values, subreport and drillthrough parameters …). References inside the parameter's own declaration, string literals and comments don't count. Pass force=True to remove anyway. The parameter's pane cell is dropped, leaving a hole; compact_layout=true re-packs the remaining cells row-major in their current slot order and shrinks NumberOfRows. Returns {parameter, removed, force, layout_synced, layout_compacted}.

### `rename_parameter`

Rename a ReportParameter and rewrite every reference to it, in the same forms remove_parameter looks for: Parameters!`<old_name>` with any member (.Value, .Label, .Count, .IsMultiValue …), Parameters("`<old_name>`"), and Report.Parameters… in the `<Code>` block, in every expression of the report (textboxes, visibility, group / sort / filter expressions, styles, query-parameter values — including the lookup datasets behind available / default values — other parameters' defaults, subreport / drillthrough parameters). Only the name token changes; names in string literals and comments are left alone. Parameter-pane layout cells are renamed too. `<DataSetReference>` holds dataset and field names, not parameter references, so it isn't touched, and `<QueryParameter Name>` (the query-side name) isn't renamed. Case-sensitive. Atomic. Errors if new_name already exists or equals old_name. Returns {old_name, new_name, references_rewritten (elements rewritten), layout_cells_rewritten}. Leftover dangling references are reported by lint's undefined-parameter rule.

### `sync_parameter_layout`

*idempotent*

Bring `<ReportParametersLayout>/<GridLayoutDefinition>/<CellDefinitions>` into sync with `<ReportParameters>`. Drops cells whose ParameterName no longer exists (and a second cell for one parameter); moves cells that share a (row, column) slot with an earlier cell or sit outside the grid (ColumnIndex ≥ NumberOfColumns, a missing or negative index); appends cells for parameters that have no entry. Moved and new cells go to the next free slot after the last occupied one, wrapping at NumberOfColumns; NumberOfRows grows to fit (a RowIndex beyond it is kept). add_parameter / remove_parameter / rename_parameter call this internally; the standalone tool repairs reports whose layout drifted. No-op when the report has no `<ReportParametersLayout>`. Returns {added, removed, relocated: [{parameter, from, to, reason}], grid_problems (what was wrong before the repair: duplicate-slot / column-out-of-range / row-out-of-range / invalid-index), changed}. Lint's parameter-layout-out-of-sync rule reports the same grid problems.

### `set_parameter_available_values`

*idempotent*

Set or clear `<ValidValues>` on a report parameter. source='static' with a non-empty static_values writes a list of `<ParameterValue>` entries (each entry can be a string or {value, label} dict). source='static' with static_values=[] or omitted CLEARS the `<ValidValues>` element entirely (mirrors set_parameter_prompt('') and returns cleared=true). source='query' writes a `<DataSetReference>` to a lookup dataset; query_value_field and query_label_field must be fields of query_dataset. Replaces any existing `<ValidValues>` block. Like every parameter writer, it checks the resulting parameter (#93) and refuses a state it introduced that Report Builder rejects: MultiValue + Nullable, a multi-value Boolean, more than one static default without MultiValue, or a literal default / static valid value that doesn't fit the DataType; a hidden parameter with neither a default nor Nullable only adds a `warnings` entry. A problem the parameter already had doesn't block the edit — it comes back in `warnings` (lint rule `parameter-state` reports it).

### `set_parameter_default_values`

*idempotent*

Set or clear `<DefaultValue>` on a report parameter. source='static' with a non-empty static_values writes a `<Values>` list of expressions. source='static' with static_values=[] or omitted CLEARS the `<DefaultValue>` element entirely (mirrors set_parameter_prompt('') and returns cleared=true). source='query' writes a `<DataSetReference>` with ValueField only (no LabelField — defaults are values, not display strings); query_value_field must be a field of query_dataset. Replaces any existing block. Like every parameter writer, it checks the resulting parameter (#93) and refuses a state it introduced that Report Builder rejects: MultiValue + Nullable, a multi-value Boolean, more than one static default without MultiValue, or a literal default / static valid value that doesn't fit the DataType; a hidden parameter with neither a default nor Nullable only adds a `warnings` entry. A problem the parameter already had doesn't block the edit — it comes back in `warnings` (lint rule `parameter-state` reports it).

### `update_parameter_advanced`

*idempotent*

Toggle the four boolean flags on a report parameter: multi_value, hidden, allow_null (writes `<Nullable>`), allow_blank. Each is independently optional. With no flags passed it's a no-op. Cascading parameters are NOT a flag — use set_parameter_available_values(source='query') + add_query_parameter on the lookup dataset to wire a dependency on another parameter. Like every parameter writer, it checks the resulting parameter (#93) and refuses a state it introduced that Report Builder rejects: MultiValue + Nullable, a multi-value Boolean, more than one static default without MultiValue, or a literal default / static valid value that doesn't fit the DataType; a hidden parameter with neither a default nor Nullable only adds a `warnings` entry. A problem the parameter already had doesn't block the edit — it comes back in `warnings` (lint rule `parameter-state` reports it).

## `tablix`

### `get_tablixes`

*read-only*

Tablix layout with stable IDs: columns, row/column groups, sort expressions, filters, visibility. Required input for any tablix edit. Per tablix:

- `columns` (widths) and `rows` (heights), one per body column / row.
- `cells`: every body cell `{row, col, textbox_name, row_span, col_span}`. Spans are read from `<CellContents>` (falling back to the pre-2010 `<TablixCell>` location); a cell inside another cell's span (a placeholder) also carries `covered_by: [row, col]`, its owner.
- `header_cells`: the `TablixHeader` textboxes of both hierarchies, `{axis: row|column, level (0 = outermost), group (null on a static member), textbox_name, size, leaves: [first, last]}`. `corner_cells`: the `TablixCorner` grid, same shape as `cells`.
- `row_members` / `column_members`: the member tree, each node `{group (null for a static member), kind: static|group|details, expressions, keep_with_group, repeat_on_new_page, visibility, sort: [{value, direction}], page_break, header?, children, leaf_index (leaves only)}`.
- `row_leaves` / `column_leaves`: one per body row / column, `{index, path (child positions from the top), kind, groups (enclosing group names)}`; row leaves add `roles`, the `style_tablix_row` row names that resolve to that row (`header`, `details`, `<group>_header`, `<group>_footer`), and `role` (the first, or null).
- `row_groups` / `column_groups`: flat `{name, expressions}` lists (every group, nested ones included).
- `sort_expressions` / `sort`: the tablix's own `<SortExpressions>` only (values / `{value, direction}`); group sorts are on their member nodes.

`response_format: "concise"` keeps names, position, counts and the flat group names.

### `set_column_width`

*idempotent*

Set `<Width>` on a tablix's body column. column accepts a 0-based integer index OR a textbox name living in any cell of that column (mirrors how set_cell_span / add_subtotal_row address columns), or column_ref {header | field} to pick it by header text or field. width is an RDL size ('1.5in', '4cm', '80pt'). The tablix `<Width>` follows (see "Tablix width" above). Idempotent: same width → {changed: false}, no save.

### `set_tablix_size`

*idempotent*

Resize a tablix by writing `<Height>` and / or `<Width>` directly. Each arg independently optional. Use after adding header / footer rows that change the body's natural height. Both values are RDL size strings. A tablix renders at the sum of its column widths plus its row-header sizes, so a `width` that disagrees with them (beyond 0.001in) is refused, naming the actual sum; with `distribute=true` the body columns are scaled proportionally to fit instead (row headers keep their size; each column keeps its unit, the last one takes the rounding remainder) and the result lists the new `column_widths`. Columns with a missing or invalid width can't be checked and the width is written as given. Height is written as given. Returns {tablix, kind, changed: list[str], column_widths?} — `changed` is empty when inputs match existing; it holds `columns` when columns were rescaled.

### `set_group_page_break`

*idempotent*

Set `<BreakLocation>` on a tablix group's page-break rule. location ∈ {None, Start, End, StartAndEnd, Between}. Passing 'None' removes the `<PageBreak>` element (the canonical 'no break' shape). Idempotent. Returns {tablix, group, kind: 'Group', location, changed}.

### `set_repeat_on_new_page`

*idempotent*

Set `<TablixMember>/<RepeatOnNewPage>` on one row-hierarchy member, named by exactly one of `group_name` (the member that wraps that group) or `member` (a row: a body row index, or a `style_tablix_row` role — `'header'`, `'details'`, `'<group>_header'`, `'<group>_footer'`). Static rows have no group, so `member` is how the column-header row (`member='header'`; with no row marked KeepWithGroup=After yet, the first static row above the data) or a group's header row (`'<group>_header'`) is reached; from the row's leaf the flag climbs to the outermost static member holding only that row. Most common use: repeat the column-header row on every page — `set_repeat_header_rows` does that and also sets `KeepWithGroup=After`. Setting repeat=False removes the element (False is default). Returns {tablix, group, kind: 'TablixMember', repeat, changed} plus `member` / `row_index` for a `member` selector.

### `set_repeat_header_rows`

*idempotent*

Repeat a table's header rows at the top of every page, as Report Builder does it: `RepeatOnNewPage=true` and `KeepWithGroup=After` on every static row member before the first member holding a group (the column-header row, and e.g. a title row above it). When the tablix has column-header cells (a matrix's `TablixHeader` rows) its `RepeatColumnHeaders` is set too. `fixed=true` also keeps them visible while scrolling in the web viewer (`FixedData` on those members, `FixedColumnHeaders` on a matrix); `fixed=false` removes those; omitted leaves them alone. `repeat=false` removes `RepeatOnNewPage` / `RepeatColumnHeaders` (KeepWithGroup stays). Refuses a tablix with neither static header rows nor column-header cells. Returns {tablix, kind: 'Tablix', repeat, fixed, header_rows, changed: list[str]} — `changed` names the elements written or removed (empty on a repeat).

### `set_keep_together`

*idempotent*

Set `<KeepTogether>` on a named Tablix / Rectangle / Chart / Textbox / Map / Gauge. Tells the renderer 'don't split this across pages if you can help it'. Best-effort — items larger than a page are still split. keep=False removes the element. Refuses for Image / Line / Subreport and other kinds where the RDL XSD doesn't allow it.

### `set_keep_with_group`

*idempotent*

Set `<TablixMember>/<KeepWithGroup>` on a static row's member. value ∈ {None, Before, After}. Typical use: `member='header'`, `value='After'` glues the column-header row to the data rows that follow (`Before` for a total row after them). The member is named by `member` (body row index or `style_tablix_row` role, as in `set_repeat_on_new_page`) or `group_name`. RDL defines KeepWithGroup for static members only (the `tablix-structure` lint rule warns on a grouped one), so a member carrying a `<Group>` — any `group_name`, or `member='details'` — is refused with `ValueError`; `value='None'` is still accepted there to remove a misplaced element. value='None' removes the element. Returns {tablix, group, kind: 'TablixMember', value, changed} plus `member` / `row_index` for a `member` selector.

### `list_tablix_filters`

*read-only*

List all filters on a named tablix in document order. Returns expression, operator, values and data_type (`<FilterValue DataType>`, null when unset) per filter; index in the list is the stable handle for remove_tablix_filter.

### `add_tablix_filter`

Append a `<Filter>` to a tablix. Operator must be one of the RDL 2016 enumeration (Equal, NotEqual, GreaterThan, In, Between, ...). Returns the new filter's index for follow-up calls. Optional field_format wraps the expression as Format(`<body>`, fmt) to coerce typed fields for string-parameter comparison. Arity, value_data_type and the type warnings work as in add_dataset_filter (checked against the tablix's dataset). Literals (no leading `=`) are written verbatim; `=`-expressions are entity-decoded with a warning; the response echoes what was written (same rules as add_dataset_filter).

### `add_row_group`

Add a dynamic row group, like Report Builder's Add Group > Parent Group.

- **Where:** `parent_group` names the existing row group the new one nests directly inside (default: the top level). `wrap` picks the members at that level it wraps: `"details"` (default) only the member on the path to the detail rows (on a matrix, the innermost group), so the column-header row and grand-total rows stay outside and render once; `"member:<group>"` that group's member wherever it is (`parent_group`, if given, must be its parent; `member:Details` adds the innermost parent of the detail rows); `"all"` every member at that level (the old behaviour: header/total rows then repeat per group).
- **Header:** `"row"` inserts a group-header row (static member, `KeepWithGroup=After`, textboxes `<group>_Header_<i>`, the expression in the first cell) at the first wrapped row's index. `"column"` gives the group a row-header cell (`TablixHeader`, textbox `<group>_Header`): other rows get blank header cells so the header area stays rectangular, the `TablixCorner` is created/resized and the tablix `Width` grows to include the header size. `"none"` adds no header. `"auto"` (default) is `"column"` when the tablix already has row-header cells (a matrix; `"row"`/`"none"` are refused there), else `"row"`.
- Refuses (file untouched) duplicate names, unknown groups, a `parent_group` whose member is itself a leaf, and any result with a new tablix-shape problem.
- Returns {tablix, group, expression, parent_group, wrap, header, wrapped, header_row, header_textboxes, corner_textboxes, changed}. `remove_row_group` is the inverse for every variant.

### `remove_row_group`

*destructive*

Remove a row group: its wrapped members move up to where the group was and keep their body rows; the group's own header row (found via the row hierarchy, any nesting depth) and its add_subtotal_row subtotal rows are deleted. A group with no header row (e.g. a Report Builder group wrapping just the data row) is unwrapped without deleting any row; a leading row of literal captions only (a wrapped column-title row) is kept unless add_row_group named it. keep_subtotals=true keeps subtotal rows (their aggregates then cover the parent scope). A group with a row-header cell (add_row_group header='column') loses it together with the blank header cells other rows got as padding; the corner and the tablix Width shrink back (`removed_textboxes` lists the textboxes). Refuses the detail group, a group whose member is itself a leaf, and a row-header group whose removal would drop non-blank header cells of other rows.

### `convert_to_matrix`

*destructive*

Turn a grouped table into a Report Builder-shaped matrix (run after insert_tablix_from_template + add_row_group + add_column_group). The detail rows aggregate into the row group: the detail row is kept and its =Fields!X.Value cells become =`<aggregate>`(Fields!X.Value) (default Sum; cells that already aggregate are left alone, complex ones are left with a warning). Group captions become TablixHeader cells: the row group's caption row moves into a row header; body columns that only repeat a row/column group expression and the column-title row are removed (with 2+ value columns left, the titles become column headers instead). Rows such as totals/footers are kept. Creates the TablixCorner (column-header depth x row-header depth). Refuses: row_group that is the detail group or does not enclose the detail rows, unknown column_group, a tablix already without a detail group (second call), or a result that would not be a consistent matrix — file untouched. Returns {tablix, kind, changed, row_headers, column_headers, aggregated, removed_rows, removed_columns, removed_textboxes, corner_textboxes, warnings}. Then set_tablix_corner for the top-left label.

### `set_tablix_corner`

*idempotent*

Set the value of a `<TablixCorner>` cell — the top-left block of a matrix, column-header depth x row-header depth, that holds the row-axis caption (e.g. 'Type'). Pass `text` for a literal value OR `expression` for a VB.NET '=...' formula (mutually exclusive); `row`/`col` (0-based, default 0/0) pick the cell. The corner is created or resized to the header depths first (new cells: '`<tablix_name>_Corner'` textboxes); the value is set in place, keeping the cell textbox's name and style; an identical call changes nothing (changed: []). Refuses when the tablix has no row headers or no column headers (run convert_to_matrix first), and for a cell outside the corner, covered by a span, or not holding a textbox. Returns {tablix, name, kind: 'TablixCorner', row, col, changed}.

### `set_group_sort`

*idempotent*

Replace a row group's `<SortExpressions>` with a fresh list of sort keys, in order. Each entry is an RDL expression string (e.g. `=Fields!Region.Value`) or `{value, direction}` with direction `Ascending` (default) or `Descending`. A string, or a dict without `direction`, whose value matches an existing key keeps that key's Direction, so re-sending the current keys to add a secondary one doesn't lose a Descending sort. `<Direction>` is written only for Descending. A SQL-style suffix (`=Fields!X.Value DESC`) is refused: pass the direction instead. `[]` removes `<SortExpressions>` (an empty block is XSD-invalid). Returns {tablix, group, sort_expressions (the values), sort: [{value, direction}], changed}; an identical call changes nothing and doesn't write.

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

Replace a column-axis group's `<SortExpressions>` with a fresh list of sort keys: expression strings or `{value, direction}` (`Ascending`/`Descending`). Same contract as set_group_sort (an existing key's Direction is kept for a plain string, `[]` removes the sort, returns `sort` and `changed`), but refuses up front if group_name is on the row axis (use set_group_sort instead).

### `set_column_group_visibility`

*idempotent*

Set `<Visibility>` on a column-axis group's TablixMember. Accepts a Hidden expression and an optional ToggleItem (textbox name that toggles expand/collapse). Mirrors set_group_visibility but refuses up front if group_name is on the row axis. Hidden must be true/false or an =expression. toggle_textbox must be an existing textbox in the same region/tablix scope and NOT in the rows/columns being hidden (e.g. use the parent group's header cell); omit it to keep the current toggle, pass null or  to remove it. Returns changed; no write when nothing changed.

### `add_tablix_column`

Append (or insert) a column into a tablix. column_name is the textbox name placed in the data row's new cell — must be unique report-wide. expression goes inside that textbox's TextRun (typically =Fields!X.Value). The data row is the detail group's row (on a matrix, the innermost row group's; the last row only when the tablix has no row groups at all) and gets expression; for a tablix with >= 2 rows the first row gets header_text (default = column_name) as a literal, and every other row (group headers, subtotals) a blank cell. Cell names: column_name for the data cell; header_name for the header cell (default `<column_name>_0`); `<column_name>_<row>` elsewhere. Generated names are suffixed when taken report-wide; column_name and an explicit header_name must be unused (header_name is refused when there is no header cell). copy_style_from (default `left`) gives every new cell the look of its neighbour in the same row: `left` (the right neighbour at position 0), `right` (the left one at the end), a textbox name (that textbox's column), or null for bare cells. The textbox, first-paragraph and first-run `<Style>` are copied (fill, borders, font, alignment, number format); Name and Value stay the new cell's own, and actions, visibility and interactive sorts are not copied. format sets the data cell's number format after that (`''` clears an inherited one). position is the 0-indexed body column; default appends at end. width defaults to 1in. The column's hierarchy member is placed so it owns that body column and stays outside every column group: a position strictly inside a group is moved to just before that group (the result's position/warnings say so) unless inside_group names it. Existing columns keep their group membership. A position inside a merged (ColSpan) cell widens that span instead of adding a textbox under it (refused if the data row is merged there). Returns {tablix, column_name, position, width, textboxes, row_textboxes (one per body row, null where a span grew over the column), header_textbox, data_textbox, style_copied_from_column, ...}.

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

Build and append a basic Tablix to `<Body>/<ReportItems>`. One column per entry in `columns`: a field name of the dataset, or {"field": `<field name>`, "header": `<caption>}` for a display caption that is not an identifier (e.g. 'Unit Price (USD)'). The header row shows the caption (default: the field name), the detail row binds to =Fields!`<field>.Value`. dataset_name must already exist and every field must be declared in it (unknown fields are refused, listing the available ones; declare them first with add_dataset_field, or with create_report's `fields` for a new report). The tablix name must be unused report-wide; generated textbox and detail group names are suffixed until unique. `width` is the tablix outer width, split evenly over the columns in its unit (width=6in with 3 columns gives three 2in columns; the last column takes any rounding remainder), so `<Width>` is exactly the sum of the columns. Resize columns afterwards with set_column_width. `container` names a Rectangle in the body to put the tablix in instead (top/left relative to it; see add_body_textbox); inside a List's rectangle the list must be bound to the same dataset. The header row's member carries `KeepWithGroup=After` and, unless `repeat_header=false`, `RepeatOnNewPage=true`, so the header repeats at the top of every page.

## `chart`

### `get_chart`

*read-only*

Return effective state of a named Chart — everything the chart tools write, so an agent can confirm its own edits: position, size, dataset, palette, custom_palette_colors (`ChartCustomPaletteColors`, in order) and palette_hatch_behavior; series list (name, type, subtype, value expression, color, data_labels — the label that renders: the template data point's, else the series-level one, as {source: point|series, visible, position, use_value_as_label, format, font_weight, color}, or null — plus label (the series' hierarchy-member `<Label>`, the legend text), value_axis / category_axis / chart_area / legend_name (`ValueAxisName`, `CategoryAxisName`, `ChartAreaName`, `LegendName`; null = the first one), hidden, and action ({action_type, target_expression, drillthrough_parameters} in the argument shape of set_chart_series_action, or null)). A series without `<Type>` (Report Builder omits it for Column) reports type 'Column' with type_implicit: true. series_groups and category_groups walk their hierarchy depth-first, one entry per ChartMember: {name, expression (the `<Group>`'s; null when static), label, sort: [{value, direction}], static, level}; series_groups leaves also name their series. derived_series: [{name, source, formula, parameters: [{name, value, source}], type}]. chart_areas: [{name, hidden, axes: {category, value}}] for every ChartArea; axes is the first area's. legends / titles list every ChartLegend / ChartTitle; legend / title are the first. Plus Style and Visibility. The concise view keeps name, position, dataset, series {name, type}, category_groups names and the dynamic series_groups names. Symmetric with get_textbox / get_image / get_rectangle.

### `insert_chart_from_template`

Build and append a basic Column chart to `<Body>/<ReportItems>`. Single category axis grouped by category_field; single Y series Sum(Fields!`<value_field>.Value`). dataset_name must already exist, and category_field / value_field must be fields of it — bare names, not expressions; an unknown field is refused with the list of available fields. For real expressions pass category_expression (group + label =expression) or value_expression (the whole Y =expression, e.g. `=Sum(Fields!A.Value)/Sum(Fields!B.Value)`; the series is then named Series1) instead, each exclusive with its field. name must be a valid RDL identifier; top/left (non-negative) and width/height (positive) are validated RDL sizes. The series is Column/Plain; change it post-insert with set_chart_series_type (RDL Types: Column, Bar, Line, Area, Shape, Scatter, Range, Polar — e.g. a pie chart is Shape/Pie, a doughnut Shape/Doughnut, a stepped line Line/Stepped). `container` names a Rectangle in the body to put the chart in instead, as for insert_tablix_from_template.

### `add_chart_series`

Append a new `<ChartSeries>` to a named chart. series_name must be a valid RDL identifier. value_field is the dataset field whose Sum becomes the Y expression (=Sum(Fields!`<value_field>.Value`)); it must be a field of the chart's dataset (unknown fields are refused with the list of available ones). value_expression takes the whole Y =expression instead (exclusive with value_field). series_type defaults to Column; combine series of different types in one chart for combo charts (e.g. Bar + Line). series_subtype defaults to the type's default (Plain; Pie for Shape). The pair must be a real RDL Type/Subtype (see set_chart_series_type); shorthands like Pie or Bubble are normalised and the canonical type/subtype returned. Also creates the series' ChartSeriesHierarchy member (`<ChartMember><Label>series_name</Label>`, the legend text; a sibling of any dynamic series group) and one ChartDataPoint per category leaf, so series and members stay matched by position. Trailing series that lack a member (files written by older versions) get one first, listed in repaired_members. Refuses if series_name is already used by a series or a derived series.

### `remove_chart_series`

*destructive*

Remove a named `<ChartSeries>` from a chart together with its ChartSeriesHierarchy leaf member; parent members left empty are pruned (their groups listed in pruned_groups), so the remaining series keep their own labels and groups. Refuses to remove the last remaining series (use remove_body_item to drop the whole chart instead). Refuses when a derived series (ChartDerivedSeries) uses it as SourceChartSeriesName unless cascade=true, which removes those derived series too. Returns {chart, removed, remaining, removed_derived, pruned_groups, repaired_members}.

### `set_chart_series_type`

*idempotent*

Update `<Type>` and optionally `<Subtype>` on a named ChartSeries. Combo charts work by setting different types per series in the same chart (e.g. one Bar series and one Line series). Only real RDL pairs are written — Column/Bar: Plain|Stacked|PercentStacked; Line: Plain|Smooth|Stepped; Area: Plain|Smooth|Stacked|PercentStacked; Shape: Pie|ExplodedPie|Doughnut|ExplodedDoughnut|Funnel|Pyramid|TreeMap|Sunburst; Scatter: Plain|Bubble; Range: Plain|Smooth|Column|Bar|Stock|Candlestick|BoxPlot|ErrorBar; Polar: Plain|Radar. Shorthands are normalised (Pie -> Shape/Pie, Bubble -> Scatter/Bubble, Radar -> Polar/Radar, Stock -> Range/Stock, SmoothLine -> Line/Smooth, subtype 100 -> PercentStacked). series_subtype null/omitted keeps the existing subtype when it suits the new type, otherwise resets it to the type's default (Plain; Pie for Shape) and reports 'Subtype' in changed. Returns {chart, series, kind, type, subtype, changed: list[str]} with the canonical pair — changed is empty when inputs match existing values.

### `set_chart_series_grouping`

*idempotent*

Make a chart series dynamic: one rendered series per distinct value of the group expression (`<ChartMember>/<Group>/<GroupExpressions>/<GroupExpression>`) — the '13 violation types unknown at design time' fan-out. group_field='Type' is a shorthand for =Fields!Type.Value (a bare field name: empty, `=…` or non-identifier values are refused); group_expression accepts any VB.NET =expression and must start with '=' (without it the value is a constant and every row would land in one series). Mutually exclusive. The group goes on series_name's own leaf member of ChartSeriesHierarchy (series match leaf members by position), or on the dynamic member that series already sits under; trailing series without a member (files written by older versions) get one first, listed in repaired_members. Promoting a static member writes the Report Builder shape: `<Label>` (each fanned-out series' legend text) becomes the group expression, or label_expression; `<SortExpressions>` sorts by the group expression — sort = Ascending (default) | Descending | None (no sort); the group is named group_name or `<series>_Group`. An existing group is refused unless replace=true, which rewrites only its GroupExpressions and keeps Group/@Name (so `Sum(..., "SeriesGroup1")` scope references keep working) and its other children (filters, ...); group_name renames it. On replace the label follows the new expression when it was a constant or the old group expression (a hand-written label expression is kept unless label_expression is given), and a sort on the old expression follows the new one (sort sets it explicitly). A call that matches the current state returns changed: [] and leaves the file untouched. Returns {chart, series, kind: 'ChartGroup', group_name, expression, label, sort: [{value, direction}], repaired_members, changed} — changed lists what was touched: Group, Group.Name, GroupExpressions, SortExpressions, Label, ChartSeriesHierarchy.

### `set_chart_axis`

*idempotent*

Configure a chart axis: title (Caption), format (numeric/date format string in `<Style>/<Format>`), min/max range, log_scale, interval, visible. axis ∈ {Category, Value}. chart_area names the ChartArea (default: the first); axis_name defaults to 'Primary', or the area's only axis of that kind. A named axis that is not in the first area but in exactly one other area is found there without chart_area; a miss lists the `area/axis` pairs present. All field args are optional; pass '' to clear an element. title rewrites only `ChartAxisTitle/Caption`, keeping the title's Style (font), Position and TextOrientation. min / max / interval must be a number, NaN (auto) or an =expression (interval not negative). log_scale writes `True`/`False`; a log scale with a literal minimum <= 0 is refused. visible is true / false / "Auto" (written `True` / `False` / `Auto`). Returns {chart, axis, axis_name, chart_area, kind, changed: list[str]}; changed lists only the fields whose value really changed, and is empty (file untouched) when nothing did.

### `set_chart_legend`

*idempotent*

Configure a chart legend: position (TopLeft / TopCenter / TopRight / LeftTop / LeftCenter / LeftBottom / RightTop / RightCenter / RightBottom / BottomLeft / BottomCenter / BottomRight) and visible (writes `<Hidden>true|false</Hidden>`). legend_name defaults to the legend named 'Default' (what the template emits), else the chart's only legend; a miss lists the legends present. Returns {chart, legend, kind, changed: list[str]}; no-op short-circuit when nothing supplied.

### `set_chart_data_labels`

*idempotent*

Configure the data labels of one or all series in a chart. Labels are written where Report Builder keeps them and where they render: `ChartSeries/ChartDataPoints/ChartDataPoint/ChartDataLabel` (every data point of the series — just the template point unless the category hierarchy has several static members). A series-level `ChartSeries/ChartDataLabel` (written by earlier versions) is moved onto the data points that have none, since a point's label overrides it (reported as ChartDataLabel in changed). When series_name is None, the change applies to every series; otherwise only the named series. visible writes `<Visible>true|false</Visible>`; visible_expression writes the same element with a VB.NET =IIf(...) expression (mutually exclusive with visible); it must start with '=' or be a literal true/false. format writes `<Style>/<Format>`; pass '' to clear. position ∈ Auto/Top/TopLeft/TopRight/Left/Center/Right/BottomRight/Bottom/BottomLeft/Outside (the RDL ChartDataLabel positions; TopCenter/BottomCenter are legend positions and are refused); Outside is meant for Shape series (pie, doughnut, funnel, pyramid) and adds a warning on other types. use_value_as_label ∈ true/false; font_weight + color write to `<Style>/<FontWeight>` and `<Style>/<Color>` (pass '' to clear); font_weight must be an RDL weight (Thin … Heavy, 100–900, or an =expression). Returns {chart, series, kind, changed, warnings}; changed is empty (file untouched) when everything already matched.

### `set_chart_palette`

*idempotent*

Set `<Palette>` on a chart. palette ∈ Default / EarthTones / Excel / GrayScale / Light / Pastel / SemiTransparent / Berry / Chocolate / Fire / SeaGreen / BrightPastel / Pacific / PacificLight / PacificSemiTransparent / Custom (the RDL ChartPalette enum; the chart-structure lint rule checks the same list). Custom takes its colours from colors — named colours, #RRGGBB / #AARRGGBB or =expressions, whitespace trimmed — written in order as `<ChartCustomPaletteColors><ChartCustomPaletteColor>`; colors is required with Custom (unless the chart already has custom colours, which are then kept) and refused with any other palette. hatch_behavior ∈ Default / None / Always writes `<PaletteHatchBehavior>` ('' removes it). Pass palette '' to clear `<Palette>` and `<ChartCustomPaletteColors>` (RB falls back to its built-in default palette). Returns {chart, kind, palette, custom_colors, changed: bool}; changed is false (file untouched) when everything already matched.

### `set_series_color`

*idempotent*

Set one chart series' colour, overriding the chart palette for just that series. color accepts a named color ('Red'), '#RRGGBB' / '#AARRGGBB', or an =expression; whitespace around a literal is trimmed and anything else is refused. Pass '' to clear (the colour falls back to the palette; the `<Style>` element itself is kept). target picks where it is written: 'series' (`ChartSeries/Style/Color`) or 'point' (`Style/Color` of every ChartDataPoint — what Report Builder's Series Properties > Fill writes, and a point colour wins over the series one at render time; an =expression there can colour per category). Default: 'point' when a data point already has a colour, else 'series'; a series colour still overridden by a point colour is reported in warnings. Returns {chart, series, kind, target, changed: bool, warnings}.

### `set_chart_title`

*idempotent*

Update `<ChartTitle>/<Caption>`. text can be literal text or an =expression. title_name defaults to the title named 'Default' (what the template emits), else the chart's only title (Report Builder names it 'Title'); a miss lists the titles present. Returns {chart, title, kind, changed: bool} with the resolved title name.

### `set_chart_series_properties`

*idempotent*

Set the per-series properties that have no other tool. Every argument is optional (omitted = unchanged; '' clears a string one):

- Legend entry: legend_text writes `ChartItemInLegend/LegendText` (overrides the series' member label in the legend); hide_in_legend writes `ChartItemInLegend/Hidden`; legend_name writes `LegendName` and must name an existing ChartLegend.
- Axes and area: value_axis / category_axis write `ValueAxisName` / `CategoryAxisName`. An axis the series' chart area (its `ChartAreaName`, else the first area) doesn't have is created there as `<ChartAxis Name=…><Location>Opposite</Location>` — e.g. value_axis='Secondary' gives a combo chart a second scale on the right; created axes are listed in created_axes (style them with set_chart_axis). chart_area writes `ChartAreaName` and must name an existing ChartArea.
- hidden writes `ChartSeries/Hidden`.
- position moves the series to that 0-based index in ChartSeriesCollection together with its ChartSeriesHierarchy member (series match members by position; the member order is the legend order). A series that shares a group member with other series can only move among the positions its siblings allow; others are refused with the reachable ones.
- Data points: tooltip (`ToolTip`), marker_type (None / Square / Circle / Diamond / Triangle / Cross / Star4 / Star5 / Star6 / Star10 / Auto) and marker_size (a size, e.g. '6pt') write onto every ChartDataPoint of the series, where Report Builder keeps them (`ChartMarker/Type|Size`). A marker on a series type that draws no markers (Column, Bar, Shape) adds a warning. For a per-point colour see set_series_color.
- Empty points: empty_point_value writes the `EmptyPointValue` custom property — Average (default: plotted between its neighbours) or Zero; empty_point_color writes `ChartEmptyPoints/Style/Color` (empty points are not drawn, leaving a gap, until they have a colour).

Returns {chart, series, kind, position, created_axes, changed, warnings}; changed is empty (file untouched) when everything already matched. get_chart reads every field back (series legend_text, hide_in_legend, legend_name, value_axis, category_axis, chart_area, hidden, tooltip, marker, empty_point_value, empty_point_color; axis location).

### `set_chart_category_group`

*idempotent*

Edit a chart's category grouping, or add a nested category level.

- Edit (no add_level): the group named group_name — default the chart's only category group (with several, group_name is required and the error lists them). group_field / group_expression regroup it (GroupExpressions rewritten, Group/@Name kept; a label that was a constant or the old expression, and a sort on the old expression, follow the new one). label_expression sets the member `<Label>` — the axis label, e.g. `=Format(Fields!OrderDate.Value, "MMM d")`. sort (Ascending / Descending / None) and sort_expression set `<SortExpressions>`: the direction defaults to the current one, the sorted expression to sort_expression, else the current sort's, else the group expression; None removes the sort.
- Add (add_level = inner | outer): a new dynamic level on group_field / group_expression (one required; an expression must start with '='). inner nests it inside the innermost category member (the chart must have exactly one); outer wraps every top-level member (e.g. Year around Month). Its label is label_expression or the expression, its sort sort (default Ascending) on sort_expression or the expression, its name group_name or `<chart>_CategoryGroup<n>`. Leaf members — and so every series' data points — are unchanged. A level on an expression a category group already uses is not added again (changed: []).

Returns {chart, kind: 'ChartGroup', group_name, expression, label, sort: [{value, direction}], level, changed}. get_chart's category_groups lists every level depth-first with its label, sort and level.

## `style`

### `get_textbox`

*read-only*

Return effective state of a named Textbox: position, size, Visibility, CanGrow, CanShrink, plus a nested style dict that mirrors set_textbox_style's routing — {box: {BackgroundColor, VerticalAlign, padding, ...}, border: {Style, Color, Width}, top_border / bottom_border / left_border / right_border (per-side overrides, same shape), paragraph: {TextAlign}, run: {FontFamily, FontSize, FontWeight, Color, Format, ...}}. Empty branches are dropped. runs[] entries each carry their own per-run style (and markup_type) — exactly the shape set_textbox_runs accepts. paragraphs[] is one {index, text_align, list_style, list_level, left_indent, right_indent, hanging_indent, space_before, space_after} per `<Paragraph>` (null when unset). Interactivity: `action` ({type: Hyperlink | Drillthrough | BookmarkLink, target, parameters: [{name, value, omit?}]} or null), `tooltip`, `document_map_label` and `bookmark` (null when absent); run_interactivity[] lists the runs with their own ToolTip / action as {paragraph_index, run_index, tooltip, action}. Searches the entire report; tablix-cell textboxes have None for top/left/width/height. Top-level positioned items with a missing `<Top>` or `<Left>` coerce to '0in' (RDL default).

### `set_textbox_style`

*idempotent*

Set styling on a named Textbox. Properties route to the right nested Style node automatically: background_color, border_*, vertical_align, padding_*, writing_mode go on Textbox/Style; text_align on every Paragraph/Style; font_family / font_size / font_weight / font_style, color, format, text_decoration on every TextRun/Style (a 'Label: value' textbox restyles as a whole — use set_textbox_runs to style one run differently); can_grow / can_shrink go DIRECTLY on Textbox (not inside Style). All fields optional — only what's passed gets written. Literal values are validated before the file is touched (a ValueError names the field and the valid values): colors are a named color, '#RRGGBB' or '#AARRGGBB'; text_align General / Left / Center / Right / Justify; vertical_align Top / Middle / Bottom; writing_mode Horizontal / Vertical / Rotate270; font_weight Thin / ExtraLight / Light / Normal / Medium / SemiBold / Bold / ExtraBold / Heavy (or 100–900, written as the name); font_style Normal / Italic; text_decoration None / Underline / Overline / LineThrough; border_style the RDL enum (None, Solid, Dotted, Dashed, Double, DashDot, DashDotDot, Groove, Ridge, Inset, WindowInset, Outset, Default); sizes as for every size argument. Enum values match case-insensitively and are written in canonical spelling. An =expression is accepted for every field. Cell-level styling: every tablix cell is a Textbox with a unique name, so use this tool with the cell's textbox name (e.g. 'HeaderAmount'). Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name.

### `set_textbox_runs`

*idempotent*

Set a textbox's text runs for mixed styling within one display — e.g. '**Asset(s):** value' with a bold prefix + regular value in a single textbox — or for multi-paragraph content. Each run is a dict with required 'text' (or 'value') and optional paragraph_index (default 0) / font_family / font_size / font_weight / font_style / color / format / text_decoration / markup_type, or a 'style' dict keyed by RDL names (FontWeight, ...); font_size, font_weight, font_style, color and text_decoration literals are validated as in set_textbox_style. Edits in place: run i of paragraph j overwrites the existing run there and keeps every field you don't pass (MarkupType, ToolTip, ActionInfo, other style keys; pass a style key as null to remove it); paragraph properties (TextAlign, ListStyle, indents) are kept, new paragraphs inherit the previous one's. Refuses (file untouched) when existing paragraphs would be left unaddressed, unless drop_extra_paragraphs=true. get_textbox.runs[] is accepted verbatim, so feeding it back is a no-op. Returns {textbox, kind, runs, paragraphs, changed}. Pass raw text in each run's `text` — encoding is handled; don't pre-encode XML entities (use `&` not `&amp;`, including for the VB.NET string-concat operator). Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name.

### `set_textbox_value`

*idempotent*

Replace the text content of a single-run textbox. value can be raw text or an =expression. Use this for the everyday 'change the textbox content' case (swap a literal label, update a stale parameter reference, replace a broken aggregate). Refuses with a redirect to set_textbox_runs when the textbox has multiple text runs or paragraphs (multi-run content needs the rich-text editor). Idempotent: identical value → {changed: false} no-op. Returns {textbox, kind, changed}. Pass raw text — encoding is handled; don't pre-encode XML entities (use `&` not `&amp;`, including for the VB.NET string-concat operator in expressions). Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name.

### `set_textbox_style_bulk`

*idempotent*

Apply the same style kwargs to every named textbox in one call. Same kwarg surface, routing (every run / paragraph) and validation as set_textbox_style; a bad value is refused before any textbox is written. The report is opened once, every name is resolved before anything is written (a name repeated in the input counts once), and the file is saved once, only if something changed. A name with no textbox lands in skipped and a name several textboxes share lands in ambiguous; the rest are still styled. strict=true refuses the whole call (nothing written) when any name is skipped or ambiguous. Returns {textboxes, skipped, ambiguous, changed} where changed is the union of sub-paths affected across all textboxes. Pair with find_textboxes_by_style to discover names by current style. cells (a list of {tablix, header | field | index, row}) adds tablix cells picked by column header or field.

### `copy_textbox_style`

*idempotent*

Make target textboxes look like a source textbox, e.g. the cells add_tablix_column creates unstyled. Copies the source's elements: scope 'box' (every Textbox/Style property except borders — background, padding, vertical alignment, writing mode, ...), 'border' (Border and the per-side TopBorder / BottomBorder / LeftBorder / RightBorder), 'paragraph' (the first source paragraph's Style plus its paragraph properties — indents, ListStyle / ListLevel, SpaceBefore / SpaceAfter — onto every target paragraph) and 'run' (the first source run's Style, onto every run of every target paragraph); scope picks a subset (default all four). include_textbox_flags (default true) also copies CanGrow / CanShrink. Within a scope the target ends up with exactly the source's properties: one the source lacks is removed. Values, actions, tooltips, visibility and names are never touched. targets resolve like set_textbox_style_bulk's names — repeats count once, unknown names land in skipped, names several textboxes share in ambiguous, strict=true refuses both before any write; the source must name exactly one textbox. Opens and saves the report once; an identical repeat returns changed: [] and leaves the file byte-identical. Returns {source, targets, skipped, ambiguous, changed} where changed lists the parts that changed ('box', 'border', 'paragraph', 'run', 'textbox.CanGrow', 'textbox.CanShrink').

### `find_textboxes_by_style`

*read-only*

Search for textboxes matching one or more style filters. Filters AND together (every supplied filter must match). Values compare normalised: colours by value (named colours, '#RRGGBB' / '#AARRGGBB' in either case — 'White' matches '#ffffff'), sizes in points ('10pt' matches '10.0pt'), enums and font_family case-insensitively; format and =expressions compare as written. Paragraph filters (text_align) match when any paragraph matches them; run filters (font_*, color, format, text_decoration) when any run of any paragraph matches all of them. effective=true lets an unset property match the RDL default (FontSize 10pt, FontFamily Arial, Color Black, FontWeight / FontStyle Normal, TextDecoration None, TextAlign General, VerticalAlign Top, WritingMode Horizontal, padding 0pt, BackgroundColor Transparent, border Style None / Color Black / Width 1pt). Returns [{name, location, matched_fields, matched_at}] where location is best-effort 'body' / 'header' / 'footer' / 'tablix:`<name>'` / 'rectangle:`<name>'`, matched_fields maps each filter to the value found (the default when matched under effective) and matched_at gives {paragraph_index} / {paragraph_index, run_index} for paragraph / run filters. Returns [] when no filters supplied. Pair with set_textbox_style_bulk for discovery+apply patterns (e.g. 'recolor every red textbox').

### `find_textbox_by_value`

*read-only*

Find every Textbox whose displayed value — the `<Value>` of each Paragraph/TextRun — matches a Python regex pattern. Searches Body / PageHeader / PageFooter. A textbox with multiple matching runs surfaces once per match. Other expressions on a textbox are not its value and are searched only when include names them: 'tooltip' (textbox and run ToolTip), 'action' (ActionInfo texts: hyperlink, drillthrough report name and parameter values, bookmark link), 'visibility' (Visibility/Hidden). Returns [{textbox, value, region, kind}] with kind 'value' or the include kind. Useful for finding stale =Parameters!Old.Value references after rename_parameter, or any cross-cutting expression edit.

### `style_tablix_row`

*idempotent*

Apply the same style kwargs to every cell in a tablix row in ONE call. Replaces the 12-individual-set_textbox_style-calls-per-row pattern. row accepts: integer (0-based body row index), 'header' (the column header row: the last static top-level row marked KeepWithGroup=After above the row groups — a group's own header row never counts), 'details' (the detail leaf row; on a matrix the innermost row group's row), '`<group>_header'` (alias '`<group>_group_header'`: the group's own static header row, e.g. from add_row_group), '`<group>_footer'` (the group's static footer row after its data, e.g. from add_subtotal_row). Both group roles raise when the group has no such row — a group whose only child is its data row has neither. Header-area cells: '`<group>_rowheader'` styles the group's TablixHeader cell (row or column group) and 'corner' every TablixCornerCell; row_index is null for these. Same style kwargs, routing and validation as set_textbox_style (font_* incl. font_style, color, text_decoration, background_color, border_*, padding_*, writing_mode, can_grow, can_shrink, etc.). Last writer wins: a background_color replaces a zebra or conditional expression from the row-colour tools (compose those with set_conditional_row_color's base_color instead). Returns {tablix, row, row_index, kind, cells, changed, skipped}.

### `set_alternating_row_color`

*idempotent*

Zebra-stripe a tablix's detail row by writing BackgroundColor=IIf(RowNumber(Nothing) Mod 2, color_a, color_b) on every detail cell's Textbox. Walks the row hierarchy to find the Details leaf — works after add_row_group nests the structure deeper. Both colours are validated as in set_textbox_style (named colour, '#RRGGBB', '#AARRGGBB' or an =expression) before the file is touched; literals are quoted, an =expression colour (e.g. '=Parameters!Stripe.Value') is embedded as an expression. Replaces any existing BackgroundColor (last writer wins) — to combine striping with a value-driven colour use set_conditional_row_color with base_color='zebra:<a>,<b>'. Returns {tablix, row_index, expression, cells, changed}.

### `set_conditional_row_color`

*idempotent*

Color every cell of a tablix's detail row based on the value of one of its fields. Builds a Switch(...) expression mapping field values to colors and writes it as BackgroundColor on every detail cell. value_expression is the field reference (e.g. 'Fields!Status.Value' — a leading '=' is accepted). color_map is an ordered dict of value->color (e.g. {"Red":"#FF0000","Yellow":"#FFFF00"}); first match wins. Unmatched values fall back to default_color (default 'Transparent'), or to base_color when given: 'zebra:<a>,<b>' keeps zebra striping on unmatched rows (Switch(..., True, IIf(RowNumber(Nothing) Mod 2, a, b)) — striping and conditional colour in one BackgroundColor), and an =expression (e.g. the one set_alternating_row_color returned) is used as the fallback as is; base_color and a non-default default_color are exclusive. Every colour is validated as in set_textbox_style before the file is touched; literals are quoted (embedded quotes doubled), =expression colours are embedded as expressions. When case_sensitive is False (default), wraps the field reference in UCase() and uppercases the keys for case-insensitive matching. Walks the row hierarchy to find the Details leaf — works after add_row_group nests the structure. Replaces any existing BackgroundColor (last writer wins). Returns {tablix, row_index, expression, cells, changed}.

## `layout`

### `set_image_sizing`

*idempotent*

Set `<Image>/<Sizing>` on a named Image. sizing ∈ AutoSize / Fit / FitProportional / Clip. AutoSize renders at native size (box grows to fit); Fit stretches to fill (ignores aspect ratio); FitProportional preserves aspect ratio; Clip renders at native size, clipped to the box. Idempotent: same value → {changed: false}, no save.

### `set_image_source`

*idempotent*

Repoint an existing `<Image>` without delete-and-readd: writes `<Source>`, `<Value>` and (Database only) `<MIMEType>`. source='Embedded': value names an `<EmbeddedImage>` (or is an =expression); a missing name is refused — a dangling reference would render as a broken image, so add it first via add_embedded_image. source='External': value is a URL / report-server path or an =expression. source='Database': value is an =expression returning the image bytes (e.g. =First(Fields!Photo.Value)) and mime_type is required (image/png, image/jpeg, image/gif, image/bmp, image/x-png, or an =expression) — Report Builder refuses to publish a database image without it. MIMEType is removed when switching to Embedded or External; mime_type is refused for them. embedded_name is the deprecated spelling of source='Embedded', value=`<name>`. Idempotent: same (Source, Value, MIMEType) → {changed: false}, no save. Returns {name, kind, source, changed: bool}.

### `get_embedded_image_data`

*read-only*

Read an embedded image's base64 `<ImageData>` for porting it to another report without re-reading from disk. Returns {name, mime_type, base64, byte_size}. base64 is the raw text of the ImageData element; byte_size is the decoded size for sanity-checking. Refuses with ElementNotFoundError if the named entry isn't in `<EmbeddedImages>`.

### `add_rectangle`

Add a `<Rectangle>` to `<Body>/<ReportItems>`. With no contained_items the rectangle is empty (a visual frame). With contained_items=[name1, name2, ...], each named body item is MOVED into the rectangle's `<ReportItems>` and its `<Top>/<Left>` recalculated so the on-screen position is preserved. Every moved item must lie inside the rectangle's box: a misfit is refused (naming each item and how far it overflows) unless fit=true, which grows the rectangle to enclose them. Refuses on duplicate name or if any contained_item isn't in `<Body>/<ReportItems>`; nothing is written on refusal. `container` names a Rectangle in the body (a List's `<name>_Rect` too) to add the new rectangle inside: top/left are then relative to it and contained_items must be that rectangle's items. Returns {name, kind: 'Rectangle', moved}, plus {fitted, top, left, width, height} when fit is passed, plus `container` (and `warnings` when the new rectangle overflows it) when given.

### `add_list`

Add a List (single-cell repeating Tablix) bound to a dataset. RDL has no distinct `<List>` element — Report Builder's List is a Tablix with one column, one detail row, and a Rectangle inside the cell. Items placed in the rectangle repeat once per dataset row. The inner rectangle is named '`<name>_Rect'` (suffixed when taken — read it from the result). Fill it with `container='<name>_Rect'` on add_body_textbox / add_body_image / add_line / add_rectangle / insert_tablix_from_template / insert_chart_from_template, or move existing items in with move_report_item; their top/left are relative to the cell (width x height). Returns {name, kind: 'Tablix', dataset, rectangle, details_group}.

### `add_line`

Add a `<Line>` to `<Body>/<ReportItems>`. RDL Line semantics: top/left is the start point; width/height is the offset (vector) to the end point — horizontal line uses height='0in', vertical uses width='0in'. Optional color, line_thickness ('1pt' default), line_style (Solid/Dashed/Dotted/Double/etc.). `container` names a Rectangle in the body to add it inside (see add_body_textbox). Returns {name, kind: 'Line'} (+ container, warnings).

### `move_report_item`

*idempotent*

Move a report item into the body, page header, page footer or a Rectangle: `to` is 'body', 'header', 'footer' or the name of a Rectangle (at any depth, a List's `<name>_Rect` included). The item may come from anywhere — the body, a band, a Rectangle, a List's rectangle — and keeps its whole subtree (style, actions, visibility, nested items) and its name; it is no longer a remove plus a re-add. Without top/left it keeps its on-page position: its Top/Left are converted to the destination's coordinates (in the item's units) from the design-time layout — the header starts at the top margin, the body below the header, the footer above the bottom margin, a List cell at its tablix's position plus row-header width / column-header height and the cells before it. A move that would need a negative Top/Left is refused (pass top / left); either one given overrides that axis. A missing page header / footer is created (moving into a new header pushes the body down — a warning says so) and the band grows to hold the item (`band_height`, `warnings`). Refuses: a data region (Tablix, Chart, GaugePanel, Map, CustomReportItem) or Subreport — or a rectangle holding one — into the header / footer; an item whose `Fields!` references are not inside an aggregate with a dataset scope (e.g. `First(Fields!X.Value, "DataSet1")`) when it goes into the header / footer, or out of its data region into another dataset's or none (a body item moved into a List gains that list's scope and is allowed); a tablix cell's own content (move the items inside it instead); a Rectangle into itself. Idempotent: an item already in `to` at the requested position returns changed: false. Returns {name, kind, from, to, top, left, changed} (+ band_height, warnings); from/to are 'body', 'header', 'footer' or 'rectangle:`<Name>`'.

### `ungroup_rectangle`

Remove a Rectangle and move its items out to the rectangle's own container (body, band or outer rectangle), at the rectangle's place in the item order, keeping their on-page position: each child's Top/Left += the rectangle's, in the child's units — the inverse of add_rectangle(contained_items=...), restoring the pre-grouping coordinates. Warns (listing the items) when the rectangle had a Visibility, a border or background, an action, tooltip, bookmark, document-map label or page break, which no longer applies to them. Refuses a rectangle that fills a tablix cell (a List's): the cell must hold an item — use move_report_item on its items. Returns {name, moved, to, changed} (+ warnings).

### `rename_report_item`

Rename a report item (Textbox, Rectangle, Image, Line, Subreport, Tablix, Chart, GaugePanel, Map, CustomReportItem) or a Group, anywhere in the report (body, header, footer, nested in rectangles and tablix cells), and rewrite every reference to it: `@Name`; `rd:DefaultName` and `DataElementName` on the element when they equal the old name; `ReportItems!Old` / `ReportItems("Old")` in any expression; the quoted scope argument of aggregates, `RowNumber`, `RunningValue`, `Previous`, `CountRows`, `InScope`, `Level` (`"Old"`); `<ToggleItem>`, `<RepeatWith>`, UserSort `<SortExpressionScope>` / `<SortTarget>`, `<DomainScope>`, `<TransformationScope>`. Only the name token changes — names in string literals and comments, and a scope string that names a dataset, are left alone. new_name must be a valid RDL identifier (letter first, then letters, digits, underscores) not used by any report item or group; a data region or group also may not take a dataset's name. kind (a report-item type or `Group`) narrows the lookup when old_name is ambiguous. rename_inner (a Tablix made by add_list) also renames the tablix's own `<old>_Rect` cell rectangle and `<old>_Details` group (any of its cell rectangles / groups named `<old>_…`) to `<new>_…`. Chart sub-objects (series, chart areas, legends) are not report-scope names and aren't renamed here. Returns {renamed, old_name, kind, references_updated: [{location, before, after}], changed} (+ also_renamed {old: new}, warnings, with rename_inner). Idempotent: when old_name is gone and new_name exists (already renamed), or the names are equal, nothing is written and changed is false.

### `set_body_item_position`

*idempotent*

Move an existing named ReportItem inside `<Body>` to (top, left), at the body's top level or nested in a Rectangle / List cell (coordinates stay relative to that rectangle, as RDL defines them). Preserves all other properties (size, style, group structure). top and left are passed through verbatim — RDL accepts any size unit (2cm, 0.75in, 108pt). Errors if no body item by that name (naming the region it is in, if any) and for a tablix cell's own content. The result carries `parent` (the holding Rectangle, null at the top level) and `warnings` when a nested item now extends outside its rectangle.

### `set_header_item_position`

*idempotent*

Move an existing named ReportItem inside `<PageHeader>` (also nested in a Rectangle there; see set_body_item_position) to (top, left). Errors if there is no `<PageHeader>` (call set_page_header first) or no item by that name. Returns `warnings` when the item now hangs below the band or past the printable page width; grow_band=true grows the band to fit instead (result gets band_height). For a nested item the band check measures its top-level rectangle.

### `set_footer_item_position`

*idempotent*

Move an existing named ReportItem inside `<PageFooter>` (also nested in a Rectangle there) to (top, left). Errors if there is no `<PageFooter>` (call set_page_footer first) or no item by that name. Returns `warnings` when the item now hangs below the band or past the printable page width; grow_band=true grows the band to fit instead (result gets band_height).

### `set_body_item_size`

*idempotent*

Resize an existing named ReportItem inside `<Body>`, also nested in a Rectangle / List cell (see set_body_item_position). At least one of width / height must be supplied; missing fields are left untouched. Same RDL size-string convention as the position tools.

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

Return effective state of a named Image: position, size, Source (External / Embedded / Database), Value, Sizing (AutoSize / Fit / FitProportional / Clip), MIMEType, Visibility, and style as {box: {BackgroundColor, VerticalAlign, padding, ...}, border: {Style, Color, Width}, top_border / bottom_border / left_border / right_border} (empty branches dropped; null when nothing is set). Interactivity as in get_textbox: `action` ({type, target, parameters} or null), `tooltip`, `document_map_label`, `bookmark`.

### `get_rectangle`

*read-only*

Return effective state of a named Rectangle: position, size, names of contained ReportItems, Visibility, style in the get_image shape ({box, border, per-side borders}; a rectangle from add_rectangle reads {border: {Style: None}}), and `action`, `tooltip`, `document_map_label`, `bookmark` as in get_textbox.

### `set_page_setup`

*idempotent*

Update `<Page>` dimensions, margins, and column count on the first ReportSection. All fields are optional — only what's passed gets written. columns=1 strips the `<Columns/>` element. When a change to the page width, a side margin or the column count leaves the body wider than the page (columns x body + column spacing + margins > page width; absent page elements at their RDL defaults), the result carries `warnings: [{rule: 'body-wider-than-page', body_width, printable_width, overflow, suggestion}]` (plus `columns` when > 1) — the overflow renders a mostly blank extra page after every page; fit_page_to_body fixes it.

### `set_page_orientation`

*idempotent*

Set page orientation by swapping PageHeight and PageWidth when the current orientation doesn't match the requested one. Idempotent. Accepts 'Portrait' or 'Landscape'. A missing `<PageHeight>` / `<PageWidth>` is the RDL default (11in x 8.5in — Report Builder omits values equal to it); a swap writes both explicitly. InteractiveHeight / InteractiveWidth are swapped too when both are present. Returns {path, orientation, changed, page_height, page_width}, plus the same `warnings` (body-wider-than-page, with body_width, printable_width, overflow) as set_page_setup when the body no longer fits the re-oriented page — e.g. Landscape -> Portrait with a wide body.

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

Add an Image to `<PageHeader>/<ReportItems>`. image_source is External (URL in value), Embedded (EmbeddedImage Name in value), or Database (=Fields!Photo.Value-style expression). A Database image requires mime_type (image/png, image/jpeg, image/gif, image/bmp, image/x-png, or an =expression), written as `<MIMEType>` after `<Value>`; mime_type is refused for the other sources. Sizes the band like add_header_textbox (band_height, warnings).

### `add_footer_image`

Same as add_header_image, for `<PageFooter>`.

### `remove_header_item`

*destructive*

Remove a named item from `<PageHeader>`, at its top level or nested in a Rectangle there (the result then has `parent`). Empties the ReportItems block when the last item leaves. Everything inside goes too; `also_removed` names those report items and groups. Refuses while anything outside the removed subtree still references the item or one of those — `ReportItems!X` / `ReportItems("X")`, a `<ToggleItem>`, the scope string of an aggregate / `RowNumber` / `RunningValue` / `InScope` …, `<RepeatWith>`, a UserSort `<SortExpressionScope>` / `<SortTarget>`, `<DomainScope>` — and lists each; force=true removes it anyway, leaves those references in place and returns them as `dangling_refs` [{kind, name, location, text}] (lint_report flags them as undefined-report-item, dangling-toggle-item, unknown-aggregate-scope). Returns {region, removed, kind, parent?, also_removed, dangling_refs}.

### `remove_footer_item`

*destructive*

Same as remove_header_item, for `<PageFooter>`.

### `add_body_textbox`

Add a Textbox to `<Body>/<ReportItems>`. text accepts static strings or RDL expressions (e.g. =Globals!ReportName). Coexists with the existing tablix; rejects names already in use. Pass raw text — encoding is handled; don't pre-encode XML entities (use `&` not `&amp;`, including for the VB.NET string-concat operator in expressions). `container` names a Rectangle in the body — at any depth, a List's `<name>_Rect` (or a rectangle in another tablix cell) included — to add the textbox inside instead; top/left are then relative to that rectangle. Refuses a container that doesn't exist, isn't a Rectangle or sits in the page header / footer (add in the body, then move_report_item). The result then carries `container`, plus `warnings` when the item overflows the rectangle (or the List cell).

### `add_body_image`

Add an Image to `<Body>/<ReportItems>`, or inside the body Rectangle `container` (as for add_body_textbox). image_source: External (URL), Embedded (EmbeddedImage Name), Database (=Fields!Photo.Value). A Database image requires mime_type (image/png, image/jpeg, image/gif, image/bmp, image/x-png, or an =expression), written as `<MIMEType>`; mime_type is refused for the other sources.

### `remove_body_item`

*destructive*

Remove a named item (Textbox, Image, Tablix, Chart, ...) from the body — at its top level or nested in a Rectangle / List cell (the result then has `parent`, the rectangle it left). Destructive but explicit — raises if the name doesn't match anything. A tablix cell's own content (e.g. a List's rectangle) is refused: the cell must hold an item. Everything inside goes too; `also_removed` names those report items and groups. Refuses while anything outside the removed subtree still references the item or one of those — `ReportItems!X` / `ReportItems("X")`, a `<ToggleItem>`, the scope string of an aggregate / `RowNumber` / `RunningValue` / `InScope` …, `<RepeatWith>`, a UserSort `<SortExpressionScope>` / `<SortTarget>`, `<DomainScope>` — and lists each; force=true removes it anyway, leaves those references in place and returns them as `dangling_refs` [{kind, name, location, text}] (lint_report flags them as undefined-report-item, dangling-toggle-item, unknown-aggregate-scope). Returns {removed, kind, parent?, also_removed, dangling_refs}.

### `add_embedded_image`

Read a real image file off disk, base64-encode it, and store it under `<EmbeddedImages>`. Reference it later with image_source='Embedded' + value=`<name>`. Supported MIME types: image/bmp, image/gif, image/jpeg, image/png, image/x-png. The file's magic bytes are sniffed and must match mime_type — claiming PNG bytes as image/jpeg is rejected here rather than letting Report Builder fail at preview time. The structure is checked too (no decoding): PNG needs an IHDR chunk right after the signature with width and height > 0 and an IEND chunk at the end; JPEG a start-of-image FFD8 and an end-of-image FFD9; GIF a logical screen descriptor and a ';' trailer; BMP a header whose file size does not exceed the file and a pixel offset inside it — so a bare signature, a truncated file or random bytes behind a signature are refused, naming what is missing. name must be an RDL identifier (a letter, then letters, digits or _). Nothing is written on a refusal. Returns {name, mime_type, bytes, width, height} (pixels, from the header; null when not cheaply known), plus warnings when the file is larger than warn_above_bytes (default 1,000,000): base64 adds about 4/3 of the image size to the .rdl — consider a smaller image or an External one.

### `list_embedded_images`

*read-only*

List embedded images in the report by name and MIME type. Returns an empty list when `<EmbeddedImages>` is absent.

### `remove_embedded_image`

*destructive*

Remove a named embedded image. Refuses (file untouched; lists the references) while anything with Source=Embedded still uses it: an `<Image>` or a `Style/<BackgroundImage>` (Body, Rectangle, Textbox, Tablix, Chart, ...) whose `<Value>` is the name, or whose =expression Value contains it as a string literal (e.g. =IIf(..., "LogoA", "LogoB"), reported as a possible reference). Pass force=True to remove anyway and accept the dangling references. Drops the empty `<EmbeddedImages/>` block when removing the last entry.

### `set_element_visibility`

*idempotent*

Set `<Visibility>` on any named ReportItem (Tablix, Textbox, Image, Rectangle, Subreport, Chart, Line, Map, GaugePanel). For group-level visibility use set_group_visibility; for detail-row use set_detail_row_visibility. hidden_expression is 'true' / 'false' or an =expression (required unless clear). toggle_textbox names the textbox that toggles expand/collapse: it must exist, not be the item or inside it, be in the same region (body/header/footer), and if it is a tablix cell the item must be inside that tablix. Omit toggle_textbox to keep the existing toggle; pass '' or null to remove it. clear=true removes `<Visibility>`. Invalid input is refused with the file untouched. Returns {element, kind, hidden_expression, toggle_textbox, changed}.

## `interactivity`

### `set_textbox_action`

*idempotent*

Set the textbox's `<ActionInfo>` to a Hyperlink (URL), Drillthrough (another report + optional parameters), or BookmarkLink (jump within the document). target_expression accepts literal text or =expression. drillthrough_parameters is a list of {name, value, omit?} dicts wired into `<Drillthrough>/<Parameters>`; omit (true / false or an =expression) writes `<Omit>`. Refused, file untouched: a parameter name that is empty, not an identifier or repeated; drillthrough_parameters with a Hyperlink / BookmarkLink action (they would be dropped); an empty target_expression; a literal BookmarkLink target that no item's `<Bookmark>` carries (set it first with set_bookmark — an =expression target, or any =expression bookmark in the report, passes). clear=true (with no other action arguments) removes the action, leaving the file byte-identical to before it was added. Idempotent: same action_type + target + parameters → {changed: false}, no save. Returns {textbox, kind, action_type, changed}. Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name.

### `set_image_action`

*idempotent*

Same shape and checks as set_textbox_action but operates on a named `<Image>`. action_type ∈ Hyperlink / Drillthrough / BookmarkLink; clear=true removes the action. Returns {image, kind, action_type, changed}.

### `set_item_action`

*idempotent*

set_textbox_action for any named Textbox, Image, Rectangle or Line anywhere in the report (body, header, footer, rectangles, tablix cells), addressed by `name`: same arguments, checks and clear=true. Other item types are refused (a chart's click action lives on its series — set_chart_series_action; a tablix's on its cells' textboxes). Returns {name, kind, action_type, changed}.

### `set_textbox_tooltip`

*idempotent*

Set `<Textbox>/<ToolTip>`. Literal text or =expression. Pass '' to clear. Idempotent. Returns {textbox, kind, changed: bool}. Tablix cell by column header or field: pass cell {tablix, header | field | index, row: data (default) | header} instead of textbox_name. For other item types use set_item_tooltip.

### `set_item_tooltip`

*idempotent*

Set `<ToolTip>` on any named report item — Textbox, Image, Rectangle, Line, Chart, Tablix, Subreport, Map, GaugePanel, CustomReportItem — anywhere in the report. Literal text or =expression; '' clears (byte-identical to before). Idempotent. Returns {name, kind, changed}. Read it back as `tooltip` from get_textbox / get_image / get_rectangle.

### `set_bookmark`

*idempotent*

Set `<Bookmark>` on any named report item: the anchor a BookmarkLink action (set_textbox_action / set_image_action / set_item_action) jumps to. Literal id or =expression; '' clears. Idempotent. Returns {name, kind, changed}, plus warnings when another item already carries the same literal bookmark (a link jumps to the first one). Clearing a bookmark that a link still points at is left to the dangling-bookmark-link lint rule. Read it back as `bookmark` from get_textbox / get_image / get_rectangle.

### `set_document_map_label`

*idempotent*

Set `<DocumentMapLabel>` on any named ReportItem (Textbox / Image / Rectangle / Chart / Tablix / etc.). Surfaces in the rendered report's navigable document-map / table-of-contents pane. Pass '' to clear. Idempotent. Returns {element, kind, changed: bool}.

### `set_chart_series_action`

*idempotent*

Set the click action of a chart series to a Hyperlink (URL), Drillthrough (another report + optional parameters), or BookmarkLink. It is written as `ActionInfo/Actions/Action` on the series' template `ChartDataPoint` (RDL has no ActionInfo on ChartSeries; a legacy series-level one is migrated). Same kwarg surface as set_textbox_action / set_image_action; the chart series is addressed by (chart_name, series_name). drillthrough_parameters are refused unless action_type is Drillthrough. action_type 'None' removes the action (every data point's ActionInfo plus any legacy series-level one) and takes no target_expression; changed is false when there was none. Idempotent on structural equality of the inner block.

## `admin`

### `duplicate_report`

Clone an .rdl to a new path. When regenerate_ids=true (default), every `<rd:DataSourceID>` and any `<rd:ReportID>` is rewritten to a fresh uuid4() so Power BI Report Builder doesn't refuse to load the duplicate due to identity collision. The copy is written atomically (temp file + rename) either way. Refuses if dst_path already exists. Returns {src, dst, regenerated_ids: list[str]}.

### `create_report`

Emit a minimal valid RDL from scratch at `path`. Refuses if `path` exists (no clobbering). Default page_setup is US Letter portrait with 1in margins; pass any subset of {page_height, page_width, margin_top, margin_bottom, margin_left, margin_right, body_width, body_height} to override. Pass `datasource` {name, workspace_url, dataset_name, provider, integrated_security, catalog_id, identity_provider} to wire a real Power BI connection in exactly the add_data_source shape; omit it (or workspace_url / dataset_name) for a placeholder DataSource1 + DataSet1 stub the caller fills in via subsequent tools. `fields` declares DataSet1's `<Fields>`: each entry is a field name (its `DataField` is the same name) or {name, data_field, type_name} (`type_name` writes `<rd:TypeName>`, e.g. `System.Decimal`). Declare the fields here (or later with add_dataset_field) before insert_tablix_from_template or any other tool that binds to them: those refuse undeclared fields. The file is written the way Report Builder writes one — indented, one element per line, with the `df` namespace marked `MustUnderstand="df"` and `<df:DefaultFontFamily>Segoe UI</df:DefaultFontFamily>` as the first child. Validates structurally + against the bundled XSD before saving (atomic .tmp + rename). Returns {path, validated, size_bytes}, plus warnings when the connection builder has any (e.g. a PBIDATASET catalog taken from the display name).

### `raw_xml_view`

*read-only*

Read-only XPath query against the report. Returns matched elements as serialised XML strings. XPath context is `<Report>` (the root); 'r:' is bound to the RDL namespace, 'rd:' to the rd namespace. Examples: "r:DataSources/r:DataSource[@Name='X']" / ".//r:Textbox[@Name='X']/r:Style". Returns [] when nothing matches; raises on malformed xpath. Output is capped at max_bytes (default 40000); a capped result ends in an explicit '`<!-- pbirb-mcp: truncated ... -->'` marker.

### `raw_xml_replace`

*destructive, idempotent*

Replace the single element matched by xpath with new content. content is parsed with RDL as the default namespace and 'rd:' bound, so bare names like `<Textbox><Value>x</Value></Textbox>` work without explicit xmlns. Refuses on zero matches, multiple matches, or if the xpath targets the `<Report>` root. Saves atomically. Returns {xpath, kind, changed}.

### `backup_report`

Copy the report to `<dest_dir>/<name>.bak.<UTC-timestamp>[-<n>][.<label>]`; the report is untouched. `dest_dir` (an existing folder) defaults to the report's folder; `label` (1-64 letters, digits, `-`, `_`) tags the name, e.g. `report.rdl.bak.20261105-101500.before-cleanup`. A sidecar `<backup>.meta.json` records `{source, sha1, size_bytes, source_mtime_ns, label, created, reason, in_transaction}`, so restore_from_backup finds the report even from another folder. The backup keeps the report's permission bits (plus owner-write); its mtime is the time of the backup. Called with a `transaction_id`, the backup holds the transaction's edited (not yet committed) state. Returns {source, backup, size_bytes, label, created, in_transaction}. Nothing is rotated or deleted.

With `PBIRB_MCP_AUTO_BACKUP` on, the server takes a backup like this by itself before every removal (`remove_*`), `rename_data_source`, `rename_parameter`, `rename_report_item`, `update_dataset_query`, `refresh_dataset_fields`, `raw_xml_replace` and `convert_to_matrix`, and adds its path to the result as `auto_backup`. When the newest backup already holds the file's bytes it is reused, so a repeated call — or every op of one transaction, whose file doesn't change until commit — shares one backup: the state before the edits.

### `list_backups`

*read-only*

The report's backups (in its folder, or `dest_dir`), newest first: {path, backups: [{backup, created, label, size_bytes, reason, same_as_report}]}. `reason` is `manual` (backup_report), `auto` (PBIRB_MCP_AUTO_BACKUP) or `pre-restore` (restore_from_backup's safety copy); `same_as_report` is true when the backup holds exactly the report's current bytes. Backups whose sidecar names a different report are left out.

### `restore_from_backup`

*destructive*

Copy a backup over its report (undo). `target_path` defaults to the report recorded in the backup's sidecar, else the path its `<target>.bak.<UTC-timestamp>` name implies; pass it when neither is known. Before writing, the report's current state is backed up (`safety_backup`, reused when a backup already holds it), so a restore can be undone too. Refuses a backup that isn't well-formed XML; refuses one that isn't a structurally valid report, or that was taken of a different report than the target, unless `force=true`. Refuses a report held by an open editing transaction and a read-only report. Returns {source, restored_to, bytes_restored, safety_backup, changed}; `changed` is false (nothing written) when the report already holds the backup's bytes.
