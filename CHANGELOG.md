## [Unreleased]

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