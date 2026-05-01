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

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
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


def set_cell_span(
    path: str,
    tablix_name: str,
    row_index: int,
    column_name: str,
    row_span: Optional[int] = None,
    col_span: Optional[int] = None,
) -> dict[str, Any]:
    """Set ``<RowSpan>`` and/or ``<ColSpan>`` on a tablix cell.

    Spans are written **inside** ``<CellContents>`` per the RDL 2016
    schema. Cells with the legacy v0.2 placement (spans on TablixCell)
    are migrated to the correct location on touch.

    ``row_index`` is 0-based into ``<TablixRows>``. ``column_name`` is the
    textbox name inside the target cell. At least one of ``row_span`` /
    ``col_span`` must be supplied; both must be >= 1.
    """
    if row_span is None and col_span is None:
        raise ValueError(
            "at least one of row_span or col_span must be provided; both None is a no-op."
        )
    if row_span is not None and row_span < 1:
        raise ValueError(f"row_span must be >= 1; got {row_span}")
    if col_span is not None and col_span < 1:
        raise ValueError(f"col_span must be >= 1; got {col_span}")

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

    # Repair v0.2 legacy placement before writing new spans.
    _migrate_legacy_span_placement(target)

    if row_span is not None:
        rs = etree.Element(q("RowSpan"))
        rs.text = str(row_span)
        _insert_into_cell_contents(contents, rs)
    if col_span is not None:
        cs = etree.Element(q("ColSpan"))
        cs.text = str(col_span)
        _insert_into_cell_contents(contents, cs)

    doc.save()
    return {
        "tablix": tablix_name,
        "row_index": row_index,
        "column_name": column_name,
        "column_index": target_col,
        "row_span": row_span,
        "col_span": col_span,
    }


__all__ = ["set_cell_span"]
