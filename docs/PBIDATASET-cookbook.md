# PBIDATASET cookbook

Power BI paginated reports that bind to a Power BI workspace dataset
(via XMLA / Analysis Services) carry a `<DataSource>` whose
`<DataProvider>` is `PBIDATASET`. Authoring against PBIDATASET has a
handful of conventions that differ from SSRS / SQL / MDX. This page
captures the ones that bit real LLM-driven editing sessions.

## The `@`-prefix rule

PBIDATASET parameters are addressed differently in the DAX text vs
the `<QueryParameter Name="...">` attribute:

* In DAX (`<CommandText>`) — write `@MyParam` **with** the `@`.
* In `<QueryParameter Name="...">` — write `MyParam` **without** the
  `@`.

SQL/MDX bindings use `@` in **both** places. Confusing the two for a
PBIDATASET dataset produces this preview-time error from Report
Builder:

> The query contains the 'X' parameter, which is not declared.

`add_query_parameter` and `update_query_parameter` detect the
provider via the resolved DataSource and **auto-strip a leading `@`**
for PBIDATASET-bound datasets, returning a structured warning so the
caller knows it happened:

```json
{
  "dataset": "DataSet1",
  "name": "Year",
  "value": "=Parameters!Year.Value",
  "normalised": true,
  "warning": "PBIDATASET parameter naming: stripped the leading '@' from '@Year' → 'Year'. The DAX text continues to reference '@Year' (with the '@'); only the <QueryParameter Name=...> attribute uses the bare form. Pass force_at_prefix=True to keep the '@' anyway."
}
```

Override with `force_at_prefix=True` for the rare cases that need the
`@` (e.g. some `RSCustomDaxFilter` patterns).

## Provider-shape detection

`_is_pbidataset_dataset` recognises two shapes for "this dataset is
PBIDATASET":

1. **Modern.** `<DataProvider>PBIDATASET</DataProvider>` — what current
   Power BI Desktop emits.
2. **Legacy.** `<DataProvider>SQL</DataProvider>` plus a
   `<ConnectString>` starting with `Data Source=powerbi://...` — the
   wire identifier our own `set_datasource_connection` and current
   `add_data_source` emit.

Both are functionally equivalent; the resolver doesn't care. (The
v0.4 roadmap tracks bumping `add_data_source` to emit the modern
shape.)

## DesignerState sync

PBIDATASET datasets typically carry an `<rd:DesignerState>/<Statement>`
block whose text is what Report Builder's Query Designer GUI displays.
When `<CommandText>` changes but `<Statement>` doesn't, the GUI shows
stale DAX, masking the actual runtime query.

`update_dataset_query` rewrites both in lockstep — no separate call
needed. The `stale-designer-state` lint rule flags any drift if a tool
outside `update_dataset_query` mutates the command text.

## Field names: SELECTCOLUMNS vs SUMMARIZECOLUMNS

PBIDATASET DAX often uses `SELECTCOLUMNS` to alias source columns:

```dax
EVALUATE
SELECTCOLUMNS('Sales',
    "Order_ID",      'Sales'[Order ID],
    "Customer_Name", 'Sales'[Customer Name (display)],
    "Amount",        'Sales'[Amount])
```

The `<Field>` declarations in `<DataSet>/<Fields>` use the **alias**
(quoted first arg) — `Order_ID`, `Customer_Name`, `Amount` — not the
bracketed source-column names. `Fields!Order_ID.Value` resolves; a
typo like `Fields!Order ID.Value` does not.

`refresh_dataset_fields` recognises both shapes:

* SELECTCOLUMNS pairs → quoted aliases become field names.
* SUMMARIZECOLUMNS / ad-hoc `Table[Col]` references → the bracketed
  column name becomes the field name (Table prefix stripped).
* Bare `EVALUATE 'Table'` shapes return a warning recommending an
  explicit SELECTCOLUMNS / SUMMARIZECOLUMNS rewrite or manual
  `add_dataset_field` calls — the column list isn't extractable
  without a metadata fetch.

## Common patterns

### Filter a PBIDATASET dataset by a Year + Month parameter pair

```python
# 1. Add the report parameters (idempotent if they exist).
add_parameter(path, name="Year",     data_type="Integer", prompt="Year")
add_parameter(path, name="MonthNum", data_type="Integer", prompt="Month (1-12)")

# 2. Wire them into the dataset's DAX. Note: name=@Year is auto-
#    stripped to Year for the <QueryParameter Name=...> attribute;
#    the DAX text inside CommandText still reads @Year / @MonthNum.
add_query_parameter(path, dataset_name="DataSet1",
                    name="@Year",     value_expression="=Parameters!Year.Value")
add_query_parameter(path, dataset_name="DataSet1",
                    name="@MonthNum", value_expression="=Parameters!MonthNum.Value")
```

The DAX itself references the parameters as `@Year` / `@MonthNum`;
`update_dataset_query` doesn't rewrite the references for you, so the
DAX you author should already use the `@`-prefixed names.

### Discover what's currently bound

```python
describe_report(path)
# Returns (among other fields):
#   "dataset_query_parameters": [
#     {"dataset": "DataSet1", "name": "Year",     "value": "=Parameters!Year.Value"},
#     {"dataset": "DataSet1", "name": "MonthNum", "value": "=Parameters!MonthNum.Value"}
#   ],
#   "designer_state_present": true,
```

This is the fast way to see PBIDATASET-shaped bindings without
walking each dataset's `<QueryParameters>` block manually.

## Lint rules that catch PBIDATASET-specific mistakes

* `pbidataset-at-prefix` — `<QueryParameter Name="@…">` on a
  PBIDATASET dataset. Caught at write-time when the auto-strip
  short-circuits via `force_at_prefix=True` AND in legacy reports.
* `stale-designer-state` — `<rd:DesignerState>/<Statement>` text
  diverges from `<CommandText>`.
* `multi-value-eq` — `=Parameters!X.Value` (multi-value) compared
  with `=` instead of `IN`. The PBI Query Designer accepts the
  ill-formed shape but the runtime errors at preview.

Run `verify_report` to surface all three plus the other 12 rules in
one call.

## v0.4 — `add_data_source(provider="pbidataset")`

Pre-v0.4, `add_data_source` always emitted the legacy
`<DataProvider>SQL</DataProvider>` + `powerbi://api.powerbi.com/v1.0/myorg/<workspace>`
ConnectString shape. Current Power BI Desktop emits the modern
PBIDATASET shape directly; for fresh-report authoring, match what RB
itself writes today.

```python
add_data_source(
    path="/path/to/report.rdl",
    name="PowerBIDataset",
    workspace_url="ADNOC",
    dataset_name="RAG Report",
    provider="pbidataset",   # NEW in v0.4 — default is "sql" for back-compat
)
```

Bytes:

```xml
<DataSource Name="PowerBIDataset">
  <ConnectionProperties>
    <DataProvider>PBIDATASET</DataProvider>
    <ConnectString>Data Source=pbiazure://api.powerbi.com/;Initial Catalog=RAG Report;Integrated Security=ClaimsToken</ConnectString>
  </ConnectionProperties>
  <rd:DataSourceID>...</rd:DataSourceID>
  <rd:PowerBIWorkspaceName>ADNOC</rd:PowerBIWorkspaceName>
  <rd:PowerBIDatasetName>RAG Report</rd:PowerBIDatasetName>
</DataSource>
```

Notes:
- `<DataProvider>PBIDATASET</DataProvider>` instead of `SQL`.
- `pbiazure://api.powerbi.com/` ConnectString with `Integrated
  Security=ClaimsToken` (vs the legacy `powerbi://api.powerbi.com/v1.0/myorg/<workspace>`).
- `<rd:PowerBIWorkspaceName>` and `<rd:PowerBIDatasetName>` siblings
  for RB display.
- No `<rd:SecurityType>` element — auth lives in the ConnectString.

Both shapes round-trip through `_is_pbidataset_dataset` so the
`@`-prefix defence kicks in regardless. The default `provider="sql"`
preserves v0.3.x byte output exactly — existing fixtures and tests
see no surprise.

`create_report(datasource={..., "provider": "pbidataset"})` (v0.4
commit 14) emits the same shape inside a scratch RDL, so a
brand-new report can ship with a real PBI XMLA connection in one
call.

## See also

* `pbirb_mcp/ops/dataset.py::_is_pbidataset_dataset` — the resolver
  for "is this PBIDATASET-bound?".
* `pbirb_mcp/ops/dataset.py::_normalise_query_parameter_name` —
  the auto-strip helper.
* `pbirb_mcp/ops/lint.py` — the 16 static rules (15 v0.3.0 + 1 v0.3.1).
* `docs/TRANSACTIONS.md` — v0.4 transaction surface for multi-edit sessions.
* `docs/MATRIX-cookbook.md` — v0.4 matrix authoring flow.
