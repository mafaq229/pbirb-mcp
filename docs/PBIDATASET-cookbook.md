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

1. **Modern.** `<DataProvider>PBIDATASET</DataProvider>` — what Power
   BI Report Builder writes (every source in the real reports we've
   seen), and what `add_data_source(provider="pbidataset")` emits.
2. **Legacy.** `<DataProvider>SQL</DataProvider>` plus a
   `<ConnectString>` starting with `Data Source=powerbi://...` — the
   XMLA shape `add_data_source` writes by default (`provider="sql"`).

The resolver treats both the same. `set_datasource_connection` keeps a
source's provider (a PBIDATASET source stays PBIDATASET) unless you
pass `provider=` to convert it.

## DesignerState sync

Datasets authored in Report Builder's Query Designer carry an
`<rd:DesignerState>` block. Its payload is a `QueryDefinition` (namespace
`http://schemas.microsoft.com/AnalysisServices/QueryDefinition`) whose
`Query/Statement` text is what the designer displays, next to a
`QuerySpecification` Select/Items graph describing the query it built.
When `<CommandText>` changes but `Statement` doesn't, the designer shows
stale DAX — and clicking OK in the graphical designer can regenerate the
old query over the new one.

`update_dataset_query` rewrites `Statement` in lockstep by default
(`designer_state="sync"`, reported as `designer_state_synced`). The
Select/Items graph can't describe arbitrary DAX, so for hand-written DAX
pass `designer_state="drop"`: the `<rd:DesignerState>` block is removed
and Report Builder opens the query in its text editor instead. The
`stale-designer-state` lint rule flags any drift if a tool outside
`update_dataset_query` (e.g. `raw_xml_replace`) mutates the command text.

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

`refresh_dataset_fields` reads the query's result columns with a DAX
tokenizer (columns used only in FILTER / ORDER BY / measure arguments /
comments don't count) and matches existing `<Field>`s by `<DataField>`,
the way Report Builder does:

* SELECTCOLUMNS / ADDCOLUMNS / SUMMARIZECOLUMNS / GROUPBY name–expression
  pairs → `<DataField>[Alias]</DataField>`; the field name is the alias
  with non-identifier characters turned into `_` (`Amount (USD)` →
  `Amount__USD_`).
* Group-by column references → `<DataField>Table[Col]</DataField>`
  (quotes dropped); the field name is the column name. Same-named columns
  from two tables become `Name` and `Name1`.
* Bare `EVALUATE 'Table'` shapes (and anything else the parser can't
  resolve) return a warning and change nothing — the column list isn't
  extractable without a metadata fetch; rewrite with an explicit
  SELECTCOLUMNS / SUMMARIZECOLUMNS or call `add_dataset_field`.

## Common patterns

### Filter a PBIDATASET dataset by a Year + Month parameter pair

```python
# 1. Add the report parameters (idempotent if they exist).
add_parameter(path, name="Year", data_type="Integer", prompt="Year")
add_parameter(path, name="MonthNum", data_type="Integer", prompt="Month (1-12)")

# 2. Wire them into the dataset's DAX. Note: name=@Year is auto-
#    stripped to Year for the <QueryParameter Name=...> attribute;
#    the DAX text inside CommandText still reads @Year / @MonthNum.
add_query_parameter(
    path, dataset_name="DataSet1", name="@Year", value_expression="=Parameters!Year.Value"
)
add_query_parameter(
    path, dataset_name="DataSet1", name="@MonthNum", value_expression="=Parameters!MonthNum.Value"
)
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
* `stale-designer-state` — the `rd:DesignerState/QueryDefinition/Query/Statement`
  text (or a legacy `rd:Statement`) diverges from `<CommandText>`.
* `multi-value-eq` — `=Parameters!X.Value` (multi-value) compared
  with `=` instead of `IN`. The PBI Query Designer accepts the
  ill-formed shape but the runtime errors at preview.

Run `verify_report` to surface all three plus every other lint rule
(and the XSD check) in one call.

## The PBIDATASET connection shape

`add_data_source(provider="pbidataset")`, `create_report(datasource={...,
"provider": "pbidataset"})` and `set_datasource_connection` on a
PBIDATASET source all write the shape Power BI Report Builder writes:

```python
add_data_source(
    path="/path/to/report.rdl",
    name="PowerBIDataset",
    workspace_url="ADNOC",  # or powerbi://api.powerbi.com/v1.0/myorg/ADNOC
    dataset_name="RAG Report",  # display name -> rd:PowerBIDatasetName
    provider="pbidataset",  # default is "sql" (the legacy XMLA shape)
    catalog_id="sobe_wowvirtualserver-<guid>",  # Initial Catalog (the model id)
    identity_provider="https://login.microsoftonline.com/organizations, ...",  # optional
)
```

Bytes:

```xml
<DataSource Name="PowerBIDataset">
  <ConnectionProperties>
    <DataProvider>PBIDATASET</DataProvider>
    <ConnectString>Data Source=pbiazure://api.powerbi.com/;Identity Provider="https://login.microsoftonline.com/organizations, ...";Initial Catalog=sobe_wowvirtualserver-&lt;guid&gt;;Integrated Security=ClaimsToken</ConnectString>
  </ConnectionProperties>
  <rd:SecurityType>None</rd:SecurityType>
  <rd:DataSourceID>...</rd:DataSourceID>
  <rd:PowerBIWorkspaceName>ADNOC</rd:PowerBIWorkspaceName>
  <rd:PowerBIDatasetName>RAG Report</rd:PowerBIDatasetName>
</DataSource>
```

Notes:
- `Initial Catalog` is the semantic model's internal id, not its
  display name. Copy it from a source Report Builder wrote for that
  model. Without `catalog_id` the display name is written and the call
  returns a warning: the connection may not resolve.
- The workspace appears only in `<rd:PowerBIWorkspaceName>`, as a bare
  name; a `powerbi://…/myorg/` prefix is stripped.
- `<rd:SecurityType>None</rd:SecurityType>`; auth is `Integrated
  Security=ClaimsToken` inside the ConnectString.
- Connect-string values containing `;`, `=` or quotes are written
  double-quoted (`"` doubled), so a name can't inject keys.

`set_datasource_connection` keeps the provider. On a PBIDATASET source
it keeps the other connect-string keys (`Identity Provider`, a
non-default `pbiazure://` host) and updates both labels. Without
`catalog_id` it keeps the current `Initial Catalog` only when the
workspace and model labels don't change. Pass `provider="sql"` or
`provider="pbidataset"` to convert a source on purpose.

## See also

* `pbirb_mcp/ops/dataset.py::_is_pbidataset_dataset` — the resolver
  for "is this PBIDATASET-bound?".
* `pbirb_mcp/ops/dataset.py::_normalise_query_parameter_name` —
  the auto-strip helper.
* `pbirb_mcp/ops/lint.py` — the 16 static rules (15 v0.3.0 + 1 v0.3.1).
* `docs/TRANSACTIONS.md` — v0.4 transaction surface for multi-edit sessions.
* `docs/MATRIX-cookbook.md` — v0.4 matrix authoring flow.
