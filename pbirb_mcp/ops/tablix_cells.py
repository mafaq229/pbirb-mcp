"""Tablix cell-level tools (v0.2 commit 4).

``set_cell_span`` writes ``<RowSpan>`` and / or ``<ColSpan>`` on a
specific tablix cell. The cell is addressed by ``(row_index, column_name)``
where ``column_name`` is the textbox name inside that cell — same handle
used by :func:`add_tablix_column` / :func:`remove_tablix_column` /
:func:`add_subtotal_row`. Either ``row_span`` or ``col_span`` (or both)
must be supplied; passing both as ``None`` is rejected as a no-op.

Per the RDL XSD, ``<RowSpan>`` and ``<ColSpan>`` are children of
``<TablixCell>`` with default 1 if absent. We always write the explicit
element so a future ``get_*`` round-trip surfaces the value back to the
LLM cleanly; passing ``1`` is the canonical way to "reset" a span.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
from pbirb_mcp.core.xpath import find_child, find_children, q

# RDL XSD: TablixCell child order is CellContents, ColSpan, RowSpan,
# DataElementName, DataElementOutput, ID. We only emit a small subset.
_TABLIX_CELL_CHILD_ORDER = (
    "CellContents",
    "ColSpan",
    "RowSpan",
    "DataElementName",
    "DataElementOutput",
)


def _insert_cell_child(cell: etree._Element, new_child: etree._Element) -> None:
    """Insert ``new_child`` into ``cell`` respecting the schema-required
    sibling order. Replaces any existing element of the same local name."""
    new_local = etree.QName(new_child).localname
    existing = find_child(cell, new_local)
    if existing is not None:
        cell.replace(existing, new_child)
        return
    new_idx = _TABLIX_CELL_CHILD_ORDER.index(new_local)
    for i, child in enumerate(list(cell)):
        local = etree.QName(child).localname
        if local in _TABLIX_CELL_CHILD_ORDER and _TABLIX_CELL_CHILD_ORDER.index(local) > new_idx:
            cell.insert(i, new_child)
            return
    cell.append(new_child)


def set_cell_span(
    path: str,
    tablix_name: str,
    row_index: int,
    column_name: str,
    row_span: Optional[int] = None,
    col_span: Optional[int] = None,
) -> dict[str, Any]:
    """Set ``<RowSpan>`` and/or ``<ColSpan>`` on a tablix cell.

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

    if row_span is not None:
        rs = etree.Element(q("RowSpan"))
        rs.text = str(row_span)
        _insert_cell_child(target, rs)
    if col_span is not None:
        cs = etree.Element(q("ColSpan"))
        cs.text = str(col_span)
        _insert_cell_child(target, cs)

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
