# Eval cases

65 cases. Generated from `cases.py` by `harness.py cases`; edit that file, not this one.

Every edit case is also graded by the structural gate (XSD + lint errors the edit introduced, see `CONTEXT.md`). A known gap is a request no tool can do correctly yet; its oracle is not replayed by `selftest`.

| id | category | difficulty | source | fixture | reference solution | tools in subset arm |
|---|---|---|---|---|---|---|
| d01 | data | easy | feedback | fleet_utilization.rdl | `remove_data_source` → `remove_data_source` | 31 |
| d02 | data | medium | synth | fleet_utilization.rdl | `rename_data_source` | 31 |
| d03 | data | easy | synth | fleet_utilization.rdl | `add_calculated_field` | 31 |
| d04 | data | medium | synth | fleet_utilization.rdl | `add_dataset_filter` | 31 |
| d05 | data | hard | feedback | fleet_utilization.rdl | `update_dataset_query` → `add_dataset_field` | 31 |
| d06 | data | easy | synth | fleet_utilization.rdl | `set_datasource_connection` | 32 |
| d07 | data | medium | synth | fleet_utilization.rdl | `add_query_parameter` → `add_query_parameter` | 31 |
| d08 | data | easy | synth | fleet_utilization.rdl | `remove_dataset_field` | 31 |
| p01 | parameters | easy | feedback | fleet_utilization.rdl | `set_parameter_type` → `set_parameter_type` | 29 |
| p02 | parameters | easy | synth | fleet_utilization.rdl | `set_parameter_prompt` | 29 |
| p03 | parameters | medium | synth | fleet_utilization.rdl | `set_parameter_available_values` | 29 |
| p04 | parameters | easy | synth | fleet_utilization.rdl | `update_parameter_advanced` | 29 |
| p05 | parameters | medium | synth | fleet_utilization.rdl | `set_parameter_default_values` → `set_parameter_default_values` | 29 |
| p06 | parameters | medium | synth | fleet_utilization.rdl | `add_parameter` → `set_parameter_default_values` | 29 |
| p07 | parameters | easy | synth | fleet_utilization.rdl | `rename_parameter` | 29 |
| p08 | parameters | medium | synth | fleet_utilization.rdl | `set_parameter_layout` | 29 |
| t01 | tablix | hard | synth | fleet_utilization.rdl | `add_row_group` → `add_subtotal_row` | 45 |
| t02 | tablix | medium | synth | fleet_utilization.rdl | `add_tablix_column` | 45 |
| t03 | tablix | easy | synth | fleet_utilization.rdl | `remove_tablix_column` | 45 |
| t04 | tablix | easy | synth | fleet_utilization.rdl | `set_column_width` | 45 |
| t05 | tablix | medium | synth | fleet_utilization.rdl | `set_group_sort` | 45 |
| t06 | tablix | hard | feedback | fleet_utilization.rdl | `add_row_group` → `add_column_group` → `convert_to_matrix` → `set_tablix_corner` | 45 |
| t07 | tablix | medium | synth | fleet_utilization.rdl | `add_tablix_filter` | 45 |
| s01 | style | medium | synth | fleet_utilization.rdl | `style_tablix_row` | 28 |
| s02 | style | medium | synth | fleet_utilization.rdl | `set_alternating_row_color` | 28 |
| s03 | style | hard | synth | fleet_utilization.rdl | `set_textbox_style_bulk` | 28 |
| s04 | style | easy | synth | fleet_utilization.rdl | `set_textbox_style_bulk` | 28 |
| s05 | style | easy | synth | fleet_utilization.rdl | `set_textbox_style` | 28 |
| s06 | style | easy | synth | fleet_utilization.rdl | `set_textbox_style` | 28 |
| s07 | style | medium | synth | fleet_utilization.rdl | `set_textbox_runs` | 28 |
| s08 | style | hard | feedback | fleet_utilization.rdl | `set_textbox_runs` | 28 |
| s09 | style | easy | synth | fleet_utilization.rdl | `set_textbox_style_bulk` | 28 |
| l01 | layout | easy | synth | fleet_utilization.rdl | `set_page_orientation` | 52 |
| l02 | layout | easy | feedback | fleet_utilization.rdl | `set_body_item_position` | 52 |
| l03 | layout | medium | feedback | fleet_utilization.rdl | `set_page_footer` → `add_footer_textbox` | 52 |
| l04 | layout | hard | feedback | fleet_utilization.rdl | `set_page_header` → `add_header_textbox` | 52 |
| l05 | layout | hard | feedback | fleet_utilization.rdl | `set_page_setup` → `set_body_size` | 52 |
| l06 | layout | easy | synth | fleet_utilization.rdl | `add_line` | 52 |
| l07 | layout | medium | synth | fleet_utilization.rdl | `set_element_visibility` | 52 |
| l08 | layout | hard | synth | fleet_utilization.rdl | `add_embedded_image` → `set_page_header` → `add_header_image` | 52 |
| c01 | chart | medium | feedback | fleet_utilization.rdl | `set_chart_series_type` | 27 |
| c02 | chart | hard | feedback | fleet_utilization.rdl | `set_chart_series_grouping` | 27 |
| c03 | chart | hard | feedback | fleet_utilization.rdl | `set_chart_data_labels` | 27 |
| c04 | chart | easy | synth | fleet_utilization.rdl | `set_chart_title` → `set_chart_legend` | 27 |
| c05 | chart | easy | synth | fleet_utilization.rdl | `add_chart_series` | 27 |
| c06 | chart | medium | synth | fleet_utilization.rdl | `set_chart_axis` | 27 |
| i01 | interactivity | hard | synth | fleet_utilization.rdl | `set_textbox_action` | 23 |
| i02 | interactivity | easy | synth | fleet_utilization.rdl | `set_textbox_tooltip` | 23 |
| t08 | tablix | hard | synth | fleet_utilization.rdl | `add_row_group` → `add_row_group` | 45 |
| t09 | tablix | medium | synth | fleet_utilization.rdl | `add_static_row` → `set_cell_span` | 45 |
| t10 | tablix | hard | synth | fleet_rb.rdl | `add_subtotal_row` | 45 |
| t11 | tablix | medium | synth | fleet_utilization.rdl | `add_column_group` | 45 |
| t12 | tablix | medium | synth | fleet_rb.rdl | `set_repeat_header_rows` | 46 |
| t13 | tablix | easy | synth | fleet_utilization.rdl | `set_group_sort` | 45 |
| s10 | style | hard | synth | fleet_rb.rdl | `set_textbox_runs` | 28 |
| c07 | chart | medium | synth | fleet_utilization.rdl | `add_chart_series` → `remove_chart_series` | 27 |
| c08 | chart | easy | synth | fleet_rb.rdl | `remove_chart_series` | 27 |
| p09 | parameters | medium | synth | fleet_rb.rdl | `rename_parameter` | 29 |
| q01 | question | easy | synth | fleet_utilization.rdl | `get_parameters`; must not edit | 15 |
| q02 | question | medium | synth | fleet_utilization.rdl | `list_data_sources`; must not edit | 15 |
| q03 | question | medium | feedback | bare_action_under_textbox.rdl | `verify_report`; must not edit | 15 |
| q04 | question | easy | synth | fleet_utilization.rdl | `get_datasets`; must not edit | 15 |
| x01 | decline | easy | synth | fleet_utilization.rdl | must not edit | 15 |
| x02 | decline | easy | synth | fleet_utilization.rdl | must not edit | 15 |
| x03 | decline | easy | synth | fleet_utilization.rdl | must not edit | 15 |

## d01 · data · easy

```text
Clean up this report: remove the two data sources that nothing uses.
```

## d02 · data · medium

```text
Rename the FleetModel data source to FleetSemanticModel.
```

## d03 · data · easy

```text
Add a calculated field called IdlePct: idle hours divided by the sum of operating and idle hours.
```

## d04 · data · medium

```text
Filter the dataset itself so only Excavator equipment comes through.
```

## d05 · data · hard

```text
The query also needs 'Assets'[Manufacturer]. Add it to the DAX and expose it as a Manufacturer field, without breaking the existing fields.
```

## d06 · data · easy

```text
Point the FleetModel connection at the 'FleetAnalytics-Prod' workspace instead. Same semantic model.
```

## d07 · data · medium

```text
Pass the DateFrom and DateTo report parameters through to the dataset query as query parameters.
```

## d08 · data · easy

```text
Nothing uses FuelLitres any more. Remove that field from the dataset.
```

## p01 · parameters · easy

```text
DateFrom and DateTo should be proper date pickers, not free-text boxes.
```

## p02 · parameters · easy

```text
Change the Site parameter's prompt to 'Select site'.
```

## p03 · parameters · medium

```text
Users should pick the site from a fixed list: Ruwais, Habshan, Das Island.
```

## p04 · parameters · easy

```text
Let people select more than one site at a time.
```

## p05 · parameters · medium

```text
Default the date range to the last 30 days: DateFrom 30 days ago, DateTo today.
```

## p06 · parameters · medium

```text
Add a hidden Integer parameter TopN with a default of 10.
```

## p07 · parameters · easy

```text
Rename the Site parameter to SiteName.
```

## p08 · parameters · medium

```text
Lay the parameter prompts out on a single row: DateFrom, DateTo, then Site.
```

## t01 · tablix · hard

```text
Group the table by site, with a subtotal row per site showing total operating hours and idle hours.
```

## t02 · tablix · medium

```text
Add a 'Fuel (L)' column to the table showing fuel litres, right after Idle Hours.
```

## t03 · tablix · easy

```text
Drop the Equipment Type column from the table.
```

## t04 · tablix · easy

```text
Make the Asset Name column in the table 2 inches wide.
```

## t05 · tablix · medium

```text
Sort the table rows by utilization, highest first.
```

## t06 · tablix · hard

```text
Turn the table into a matrix: sites down the side, equipment types across the top, operating hours in the cells, and 'Site' in the top-left corner.
```

## t07 · tablix · medium

```text
In the table only (not the dataset), show just the assets with utilization below 50%.
```

## s01 · style · medium

```text
Make the table's header row bold white text on a dark navy (#1F3864) background.
```

## s02 · style · medium

```text
Shade alternate data rows in the table light grey (#F2F2F2) and white.
```

## s03 · style · hard

```text
Highlight table rows in light red where utilization is under 50%.
```

## s04 · style · easy

```text
Show Operating Hours and Idle Hours in the table with thousands separators and one decimal place.
```

## s05 · style · easy

```text
Format the Utilization values in the table as percentages.
```

## s06 · style · easy

```text
Make the report title 16pt bold Segoe UI.
```

## s07 · style · medium

```text
Change the 'Total Hours' label to read 'Total Operating Hours' and italicise it.
```

## s08 · style · hard

```text
In the report title, show 'Fleet Utilization: ' in bold followed by the selected Site parameter value in normal weight, all in the one textbox.
```

## s09 · style · easy

```text
Centre-align the numbers in the Operating Hours, Idle Hours and Utilization columns of the table.
```

## l01 · layout · easy

```text
Switch the report to landscape.
```

## l02 · layout · easy

```text
Move the table down by an inch so there's breathing room under the title. Keep it at the left edge.
```

## l03 · layout · medium

```text
Add a page footer with 'Page X of Y'.
```

## l04 · layout · hard

```text
Add a page header showing the selected date range (DateFrom to DateTo). It should not print on the first page.
```

## l05 · layout · hard

```text
Make the page 16in wide by 12in tall with 0.3in margins all round, and widen the report body to use the full width.
```

## l06 · layout · easy

```text
Put a thin horizontal line under the report title.
```

## l07 · layout · medium

```text
Hide the chart when the Site parameter is 'Das Island'.
```

## l08 · layout · hard

```text
Add our logo to the top-right of a page header. The logo file is {LOGO_PATH}.
```

## c01 · chart · medium

```text
Make the chart a stacked bar chart.
```

## c02 · chart · hard

```text
Split the chart into one series per equipment type; the types aren't known in advance.
```

## c03 · chart · hard

```text
Show data labels on the chart, but only when the value is at least 100 hours.
```

## c04 · chart · easy

```text
Title the chart 'Operating Hours by Site' and move its legend to the bottom.
```

## c05 · chart · easy

```text
Add idle hours to the chart as a second series.
```

## c06 · chart · medium

```text
Label the chart's value axis 'Hours' and format it with thousands separators, no decimals.
```

## i01 · interactivity · hard

```text
Clicking an asset name in the table should open the 'AssetDetail' report for that asset (pass its AssetID).
```

## i02 · interactivity · easy

```text
Show the asset's ID as a tooltip when hovering over its name in the table.
```

## t08 · tablix · hard

```text
Group the table rows by site, and within each site by equipment type.
```

## t09 · tablix · medium

```text
Add a title row above the table's column headings that says 'Fleet assets', merged across the full width of the table.
```

## t10 · tablix · hard

```text
Add a subtotal row at the end of each site showing that site's total operating hours and idle hours. Keep the grand total row at the bottom.
```

## t11 · tablix · medium

```text
Repeat the table's columns side by side once per site, with the site name as a heading above each block of columns.
```

## t12 · tablix · medium

```text
When the table runs onto more than one page, repeat the column heading row at the top of every page.
```

## t13 · tablix · easy

```text
Sort the table rows by asset name in reverse alphabetical order, Z to A.
```

## s10 · style · hard

```text
In the Utilization column heading, add ' (%)' after the word Utilization in a smaller 8pt, non-bold font. Keep the heading's white-on-navy look and everything else about the cell as it is.
```

## c07 · chart · medium

```text
Change the chart to show idle hours instead of operating hours.
```

## c08 · chart · easy

```text
Take the idle hours series off the chart.
```

## p09 · parameters · medium

```text
Rename the Site parameter to SiteFilter everywhere it's used.
```

## q01 · question · easy

```text
What parameters does this report take, and what data type is each one?
```
Answer must mention: (DateFrom) AND (DateTo) AND (Site) AND (String)

## q02 · question · medium

```text
Which of this report's data sources are actually used by a dataset?
```
Answer must mention: (FleetModel)

## q03 · question · medium

```text
Report Builder refuses to open this report. What's wrong with it? Don't change anything yet.
```
Answer must mention: (Action) AND (ActionInfo or not expected or not allowed or invalid or schema)

## q04 · question · easy

```text
What DAX query does the dataset run?
```
Answer must mention: (SUMMARIZECOLUMNS)

## x01 · decline · easy

```text
Publish this report to the Finance workspace in the Power BI service.
```

## x02 · decline · easy

```text
Export page 1 of this report as a PDF for me.
```

## x03 · decline · easy

```text
Set up an email subscription that sends this report every Monday at 8am.
```
