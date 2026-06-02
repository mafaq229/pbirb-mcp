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
    column_names=["ProductID", "ProductName", "Amount"],
    top="0.5in", left="0.5in", width="4in", height="0.5in",
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

# v0.4: drop the Details leaf so cells aggregate at the row-group level.
convert_to_matrix(
    path="/path/to/report.rdl",
    tablix_name="MainTable",
    row_group="Region",
    column_group="Date",
)

# v0.4: write the corner caption.
set_tablix_corner(
    path="/path/to/report.rdl",
    tablix_name="MainTable",
    text="Type",
)

# v0.4: add a Grand Total column with the aggregate per row.
add_subtotal_column(
    path="/path/to/report.rdl",
    tablix_name="MainTable",
    group_name="Date",
    aggregates=[{"row": 1, "expression": "=Sum(Fields!Amount.Value)"}],
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
        {"tool": "add_row_group",      "args": {...}},
        {"tool": "add_column_group",   "args": {...}},
        {"tool": "convert_to_matrix",  "args": {...}},
        {"tool": "set_tablix_corner",  "args": {...}},
        {"tool": "add_subtotal_column","args": {...}},
        {"tool": "set_body_size",      "args": {...}},
    ],
)
```

If any op fails — or lint surfaces an error at commit — the entire
batch rolls back. The on-disk file is byte-identical to its pre-call
state. See `docs/TRANSACTIONS.md` for the full atomicity contract.

## Tool reference

### `convert_to_matrix(path, tablix_name, row_group, column_group)`

Drops the `Details` `<TablixMember>` from the row hierarchy and
removes the corresponding body row.

**Pre-conditions (all checked, ValueError on miss):**
- `tablix_name` exists.
- `row_group` is the `Name` of an existing
  `<TablixMember>/<Group>` in the row hierarchy.
- `column_group` is the `Name` of an existing
  `<TablixMember>/<Group>` in the column hierarchy.
- A `<Group Name="Details">` is still present in the row hierarchy.

**Idempotency:** second call refuses with `"already a matrix"` —
the verb is idempotent-by-explicit-refusal.

**Returns:** `{tablix, kind: 'Tablix', changed: ['details_member_removed', 'details_body_row_removed']}`.

### `set_tablix_corner(path, tablix_name, text=None, expression=None)`

Writes the `<TablixCorner>` block with a single 1×1 textbox.

- Either `text` (literal) OR `expression` (`'=...'` VB.NET) — mutually
  exclusive.
- Textbox name is deterministic: `<tablix_name>_Corner`.
- Refuses if the tablix has no named column-axis group — the corner
  is only meaningful in a matrix.
- Replaces any existing TablixCorner block.

**Returns:** `{tablix, name, kind: 'TablixCorner', changed}`.

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
