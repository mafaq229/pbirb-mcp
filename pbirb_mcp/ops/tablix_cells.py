"""Tablix cell-level tools.

``set_cell_span`` writes ``<RowSpan>`` and / or ``<ColSpan>`` on a
specific tablix cell. The cell is addressed by ``(row_index, column_name)``
where ``column_name`` is the textbox name inside that cell — same handle
used by :func:`add_tablix_column` / :func:`remove_tablix_column` /
:func:`add_subtotal_row`. Either ``row_span`` or ``col_span`` (or both)
must be supplied; passing both as ``None`` is rejected as a no-op.

Per the **official RDL 2016 schema** (Microsoft ReportDefinition.xsd),
``<ColSpan>`` and ``<RowSpan>`` are children of ``<CellContents>`` — NOT
children of ``<TablixCell>``. v0.2.0 placed them on ``<TablixCell>`` based
on a misreading of online examples; Report Builder rejects that with
``"invalid child element 'ColSpan'"`` and the document fails to render.

Schema sketch::

    <TablixCell>
      <CellContents>
        <Textbox Name="..."> ... </Textbox>     <!-- the report item -->
        <ColSpan>2</ColSpan>                    <!-- spans go here -->
        <RowSpan>1</RowSpan>
      </CellContents>
    </TablixCell>

For round-trip / migration safety, when this tool encounters a cell with
the legacy v0.2 placement (``ColSpan``/``RowSpan`` directly under
``TablixCell``), it migrates them into ``CellContents`` before writing
the requested spans, so a single ``set_cell_span`` call repairs broken
v0.2-written files alongside its primary purpose.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names, tablix_spans
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
from pbirb_mcp.core.tablix_headers import new_textbox
from pbirb_mcp.core.xpath import find_child, find_children, q

# Per RDL XSD, CellContents children appear in this order. The single
# ReportItem (Textbox / Image / Rectangle / Subreport / etc.) comes first;
# ColSpan/RowSpan come after the item.
_CELL_CONTENTS_SPAN_ORDER = ("ColSpan", "RowSpan")

# Names of report-item children of CellContents; everything else (rd:* metadata
# excluded) is the wrapped item itself.
_REPORT_ITEM_TAGS = (
    "Textbox",
    "Image",
    "Rectangle",
    "Subreport",
    "Chart",
    "Tablix",
    "Map",
    "Gauge",
    "Line",
    "List",
)


def _migrate_legacy_span_placement(cell: etree._Element) -> None:
    """If ``<ColSpan>`` or ``<RowSpan>`` are direct children of ``cell``
    (the v0.2 bug placement), move them inside ``<CellContents>`` per the
    RDL XSD. Idempotent: a no-op when no legacy placement is present."""
    contents = find_child(cell, "CellContents")
    if contents is None:
        return
    for span_local in _CELL_CONTENTS_SPAN_ORDER:
        legacy = find_child(cell, span_local)
        if legacy is None:
            continue
        # Drop any existing (correctly-placed) duplicate inside CellContents
        # to avoid two competing values; the one on TablixCell was the
        # outdated source of truth.
        existing_inside = find_child(contents, span_local)
        if existing_inside is not None:
            contents.remove(existing_inside)
        cell.remove(legacy)
        _insert_into_cell_contents(contents, legacy)


def _insert_into_cell_contents(contents: etree._Element, span: etree._Element) -> None:
    """Insert a ``<ColSpan>`` or ``<RowSpan>`` element into ``CellContents``,
    respecting the schema-required order: report-item first, then ColSpan,
    then RowSpan. Replaces any existing element of the same local name."""
    new_local = etree.QName(span).localname
    if new_local not in _CELL_CONTENTS_SPAN_ORDER:
        raise ValueError(
            f"_insert_into_cell_contents only handles ColSpan/RowSpan; got {new_local!r}"
        )
    existing = find_child(contents, new_local)
    if existing is not None:
        contents.replace(existing, span)
        return
    # Find the insertion point: after the report item, in span-order.
    new_idx = _CELL_CONTENTS_SPAN_ORDER.index(new_local)
    insert_at = len(contents)  # default: append at the end
    for i, child in enumerate(list(contents)):
        local = etree.QName(child).localname
        if local in _CELL_CONTENTS_SPAN_ORDER:
            other_idx = _CELL_CONTENTS_SPAN_ORDER.index(local)
            if other_idx > new_idx:
                insert_at = i
                break
    contents.insert(insert_at, span)


def _check_span_arg(label: str, value: Any) -> None:
    if value is None:
        return
    # bool is an int subclass; <ColSpan>True</ColSpan> is schema-invalid.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer >= 1; got {value!r}")
    if value < 1:
        raise ValueError(f"{label} must be >= 1; got {value}")


def set_cell_span(
    path: str,
    tablix_name: str,
    row_index: int,
    column_name: str,
    row_span: Optional[int] = None,
    col_span: Optional[int] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Set ``<ColSpan>`` (and/or reset ``<RowSpan>``) on a tablix body cell.

    Spans are written **inside** ``<CellContents>`` per the RDL 2016
    schema. Cells with the legacy v0.2 placement (spans on TablixCell)
    are migrated to the correct location on touch.

    ``row_index`` is 0-based into ``<TablixRows>``. ``column_name`` is the
    textbox name inside the target cell. At least one of ``row_span`` /
    ``col_span`` must be supplied; both must be integers >= 1.

    Report Builder's merged-cell shape is kept: an owner with
    ``ColSpan=N`` is followed by ``N-1`` placeholder cells without
    contents.

    * ``col_span`` may not run past the last body column.
    * Growing a span covers the cells to its right. Cells that are blank
      (no contents, or a textbox without text) are cleared; a covered
      cell with real content is refused unless ``force=True`` clears it.
      Cleared report-item names are returned as ``cleared``.
    * Shrinking a span gives each uncovered column a blank textbox
      (returned as ``created``).
    * ``row_span`` > 1 is refused: a body cell cannot span rows
      (RowSpan is only meaningful in a ``TablixCorner``). ``row_span=1``
      resets it.
    """
    if row_span is None and col_span is None:
        raise ValueError(
            "at least one of row_span or col_span must be provided; both None is a no-op."
        )
    _check_span_arg("row_span", row_span)
    _check_span_arg("col_span", col_span)
    if row_span is not None and row_span > 1:
        raise ValueError(
            f"row_span={row_span} refused: body cells cannot span rows (RowSpan is "
            "only meaningful in a TablixCorner). Use a row group to merge "
            "repeated values, or pass row_span=1 to reset."
        )

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    if row_index < 0 or row_index >= len(rows):
        raise IndexError(
            f"tablix {tablix_name!r} has no row at index {row_index} (rows={len(rows)})"
        )

    row = rows[row_index]
    cells_root = find_child(row, "TablixCells")
    cells = find_children(cells_root, "TablixCell") if cells_root is not None else []

    target: Optional[etree._Element] = None
    target_col: Optional[int] = None
    for col_idx, cell in enumerate(cells):
        tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
        if tb is not None and tb.get("Name") == column_name:
            target = cell
            target_col = col_idx
            break
    if target is None:
        raise ElementNotFoundError(
            f"no textbox named {column_name!r} in row {row_index} of tablix {tablix_name!r}"
        )

    contents = find_child(target, "CellContents")
    if contents is None:
        raise ElementNotFoundError(
            f"cell at row={row_index}, column={column_name!r} has no <CellContents>; "
            "the cell is malformed and cannot accept spans."
        )

    # Repair v0.2 legacy placement before reading / writing spans.
    had_legacy_col_span = find_child(target, "ColSpan") is not None
    _migrate_legacy_span_placement(target)

    cols_root = find_child(body, "TablixColumns")
    n_cols = len(find_children(cols_root, "TablixColumn")) if cols_root is not None else 0
    n_cols = n_cols or len(cells)

    cleared: list[str] = []
    if had_legacy_col_span and col_span is None:
        # The migrated ColSpan now takes effect (it was inert where v0.2
        # wrote it), so the cells it covers must become placeholders — the
        # same rule as widening a span (#34): blank ones are emptied,
        # non-blank ones refuse unless force (which clears them).
        legacy_covered = cells[target_col + 1 : target_col + tablix_spans.span(target)]
        blocking = [
            (target_col + 1 + k, tablix_spans.report_item_names(c))
            for k, c in enumerate(legacy_covered)
            if not tablix_spans.is_blank(c)
        ]
        if blocking and not force:
            detail = "; ".join(f"column {j}: {', '.join(n) or 'content'}" for j, n in blocking)
            raise ValueError(
                f"cell {column_name!r} carries a v0.2-style ColSpan of "
                f"{tablix_spans.span(target)} that would now cover non-empty cells "
                f"({detail}); clear them, pass force=True to migrate the span and "
                "delete their contents, or pass col_span (e.g. 1) to set the span "
                "yourself."
            )
        for cell in legacy_covered:
            cleared.extend(tablix_spans.report_item_names(cell))
            cell_contents = find_child(cell, "CellContents")
            if cell_contents is not None:
                tablix_spans.detach(cell_contents)

    owners = tablix_spans.owner_map(cells)
    if owners[target_col] != target_col:
        owner_names = tablix_spans.report_item_names(cells[owners[target_col]])
        raise ValueError(
            f"cell {column_name!r} (column {target_col}) is covered by the ColSpan of "
            f"column {owners[target_col]} ({', '.join(owner_names) or 'unnamed'}); "
            "shrink that span first."
        )

    # A v0.2 ColSpan never covered anything (it was inert where v0.2 put
    # it), so an explicit col_span is measured from a span of 1.
    old_span = 1 if had_legacy_col_span else tablix_spans.span(target)
    created: list[str] = []
    if col_span is not None:
        if target_col + col_span > n_cols:
            raise ValueError(
                f"col_span={col_span} at column {target_col} overflows the tablix's "
                f"{n_cols} column(s); the largest span here is {n_cols - target_col}."
            )
        newly_covered = cells[target_col + old_span : target_col + col_span]
        blocking = [
            (target_col + old_span + k, tablix_spans.report_item_names(c))
            for k, c in enumerate(newly_covered)
            if not tablix_spans.is_blank(c)
        ]
        if blocking and not force:
            detail = "; ".join(f"column {j}: {', '.join(n) or 'content'}" for j, n in blocking)
            raise ValueError(
                f"col_span={col_span} would cover non-empty cells ({detail}); clear "
                "them first or pass force=True to delete their contents."
            )
        for cell in newly_covered:
            cleared.extend(tablix_spans.report_item_names(cell))
            cell_contents = find_child(cell, "CellContents")
            if cell_contents is not None:
                tablix_spans.detach(cell_contents)

    if row_span is not None:
        rs = etree.Element(q("RowSpan"))
        rs.text = str(row_span)
        _insert_into_cell_contents(contents, rs)
    if col_span is not None:
        cs = etree.Element(q("ColSpan"))
        cs.text = str(col_span)
        _insert_into_cell_contents(contents, cs)

        # Every placeholder no span covers any more (columns a shrink
        # uncovered, or the tail of a span this one swallowed) gets a
        # blank textbox, so no column is left without contents.
        registry = names.NameRegistry(doc)
        row_cells = find_children(cells_root, "TablixCell")
        for j, owner in enumerate(tablix_spans.owner_map(row_cells)):
            cell = row_cells[j]
            if owner == j and tablix_spans.is_placeholder(cell):
                name = registry.unique(f"{tablix_name}_Cell_{row_index}_{j}")
                etree.SubElement(cell, q("CellContents")).append(new_textbox(name))
                created.append(name)

    doc.save()
    return {
        "tablix": tablix_name,
        "row_index": row_index,
        "column_name": column_name,
        "column_index": target_col,
        "row_span": row_span,
        "col_span": col_span,
        "cleared": cleared,
        "created": created,
    }


__all__ = ["set_cell_span"]
