# Matrix layouts cookbook

v0.4.0 closes the matrix-authoring gap from the 2026-05-11 session
feedback. This cookbook walks the end-to-end flow: start from a
template, build a matrix with a row group, column group, corner
caption, and a Grand Total column.

## The problem (pre-v0.4)

`insert_tablix_from_template` produced a tablix that LOOKED like a
matrix after `add_row_group` + `add_column_group`, but:

- The residual `Details` row group rendered cells at detail
  granularity instead of aggregating at the row-group leaf.
- No `<TablixCorner>` element existed → the top-left "Type" caption
  had no proper home.
- `add_subtotal_row` covered the row axis but there was no column-
  axis equivalent for a Grand Total column.
- `<Body>/<Width>` (rendering region) couldn't be edited, so a wide
  matrix clipped at preview time.

All four are closed in v0.4.0 Phase F.

## The flow

```python
# Start from a template tablix (existing v0.2 tool).
insert_tablix_from_template(
    path="/path/to/report.rdl",
    name="MainTable",
    dataset_name="MainDataset",
    columns=["ProductID", "ProductName", "Amount"],
    top="0.5in",
    left="0.5in",
    width="4in",
    height="0.5in",
)

# Add the row group (existing v0.2 tool).
add_row_group(
    path="/path/to/report.rdl",
    tablix_name="MainTable",
    group_name="Region",
    group_expression="=Fields!ProductName.Value",
)

# Add the column group (existing v0.2 tool).
add_column_group(
    path="/path/to/report.rdl",
    tablix_name="MainTable",
    group_name="Date",
    group_expression="=Fields!ProductID.Value",
)

# Turn it into a matrix: group captions become TablixHeaders, the
# Details row aggregates at the row-group level (=Sum(Fields!Amount.Value)),
# and the TablixCorner is created.
convert_to_matrix(
    path="/path/to/report.rdl",
    tablix_name="MainTable",
    row_group="Region",
    column_group="Date",
    aggregate="Sum",  # default
)

# Write the corner caption (edits the corner cell in place).
set_tablix_corner(
    path="/path/to/report.rdl",
    tablix_name="MainTable",
    text="Type",
)

# Add a Grand Total column with the aggregate per row. The matrix body
# is now a single row (row 0).
add_subtotal_column(
    path="/path/to/report.rdl",
    tablix_name="MainTable",
    group_name="Date",
    aggregates=[{"row": 0, "expression": "=Sum(Fields!Amount.Value)"}],
    position="after",  # canonical Grand Total slot
    width="1in",
)

# v0.4: expand the body so the wider matrix doesn't clip on a 16in landscape page.
set_body_size(
    path="/path/to/report.rdl",
    width="14in",
    height="10in",
)
```

After this seven-call sequence, the RDL is XSD-valid and renders as
a canonical matrix in Power BI Report Builder:

- Rows × Dates of aggregated values
- "Type" caption in the top-left corner cell
- Grand Total column on the right with the row-level sum
- Body region wide enough for the layout

### Run as a single atomic batch

The whole sequence can run as one `apply_edits` call:

```python
apply_edits(
    path="/path/to/report.rdl",
    ops=[
        {"tool": "add_row_group", "args": {...}},
        {"tool": "add_column_group", "args": {...}},
        {"tool": "convert_to_matrix", "args": {...}},
        {"tool": "set_tablix_corner", "args": {...}},
        {"tool": "add_subtotal_column", "args": {...}},
        {"tool": "set_body_size", "args": {...}},
    ],
)
```

If any op fails — or lint surfaces an error at commit — the entire
batch rolls back. The on-disk file is byte-identical to its pre-call
state. See `docs/TRANSACTIONS.md` for the full atomicity contract.

## Tool reference

### `convert_to_matrix(path, tablix_name, row_group, column_group, aggregate='Sum')`

Turns the grouped table into Report Builder's matrix shape:

- **Row headers.** Every row group around the detail rows gets a
  `<TablixHeader>`. The caption row `add_row_group` created moves into
  it (the textbox keeps its name and style) and that row is deleted.
- **Caption columns.** A body column whose data cell is exactly a row or
  column group expression (here `ProductName` and `ProductID`) only
  repeats a header, so it is removed. Its width becomes the row
  header's `Size`. It stays, with a warning, when another kept row has
  content in it or it is the column group's only column.
- **Column titles.** The column-title row (`Product ID | … | Amount`) is
  deleted. When two or more value columns remain, its captions become
  column headers below the column group instead.
- **Data row.** The `Details` row is kept and now renders once per
  group. Its `=Fields!X.Value` cells (and plain arithmetic over fields)
  become `=<aggregate>(…)`. Cells that already aggregate are left
  alone, and anything more complex is left with a warning. Total and
  footer rows are untouched.
- **Corner.** Header levels are padded, and the `<TablixCorner>` is
  created at column-header depth × row-header depth.

**Pre-conditions (ValueError, file untouched):**
- `row_group` is a dynamic row group that encloses the detail rows. It
  can't be the detail group itself.
- `column_group` is a dynamic column group.
- A detail group (a leaf `<Group>` without `<GroupExpressions>`, any
  name) is still present.
- At least one body column holds a value to aggregate.
- The result passes the matrix shape check.

**Idempotency:** second call refuses with `"already a matrix"` —
the verb is idempotent-by-explicit-refusal.

**Returns:** `{tablix, kind: 'Tablix', changed, row_headers,
column_headers, aggregated, removed_rows, removed_columns,
removed_textboxes, corner_textboxes, warnings}`.

### `set_tablix_corner(path, tablix_name, text=None, expression=None, row=0, col=0)`

Sets one `<TablixCorner>` cell (`row` / `col`, 0-based; default the
top-left one).

- Either `text` (literal) OR `expression` (`'=...'` VB.NET) — mutually
  exclusive.
- The corner is created or resized to column-header depth × row-header
  depth first. New cells are textboxes named `<tablix_name>_Corner`.
- The value is set in place, so the cell's textbox keeps its name and
  style. An identical call returns `changed: []`.
- Refuses when the tablix has no row headers or no column headers (run
  `convert_to_matrix` first). Also refuses a cell that is outside the
  corner, covered by a span, or not a textbox.

**Returns:** `{tablix, name, kind: 'TablixCorner', row, col, changed}`.

### `add_subtotal_column(path, tablix_name, group_name, aggregates, position='after', width='1in')`

Column-axis mirror of `add_subtotal_row`. Adds a new static
`<TablixMember>` inside the column-group's `<TablixMembers>`, a new
`<TablixColumn>` in the body, and a cell at the new column index in
every existing body row.

- `aggregates`: `[{"row": <int>, "expression": "<agg>"}, ...]`. Rows
  not listed get blank cells. Row indices validated BEFORE mutation
  (atomic).
- `position`: `'after'` (Grand Total slot, default) or `'before'`.

**Returns:** `{tablix, group, position, aggregates, kind: 'TablixMember', column_index, changed}`.

### `set_body_size(path, width=None, height=None)`

Sets the body's rendering region:
- `<Body>/<Height>` (Height is INSIDE `<Body>`)
- `<ReportSection>/<Width>` (the body's width SIBLING of `<Body>` —
  an RDL historical quirk)

Distinct from:
- `set_page_setup` — touches `<Page>/<PageWidth>` / `<PageHeight>` (paper chrome)
- `set_body_item_size` — touches size of items INSIDE `<Body>`

**Returns:** `{kind: 'Body', changed}` (idempotent; empty `changed`
when values unchanged).
