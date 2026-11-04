"""Static (no-group) row / column tools (v0.2 commit 5).

These differ from :func:`add_tablix_column` and :func:`add_subtotal_row`
in that the new row / column is **not** bound to a data field or a
group expression — every cell holds literal text. Use them for header
captions, footer labels, or banner rows.

Both tools insert a corresponding static ``<TablixMember>`` (without a
``<Group>``) into the matching hierarchy so that it owns the new body
row / column: the member goes at *leaf* position ``position`` (see
:func:`pbirb_mcp.core.tablix_shape.insertion_point`), not top-level
position ``position`` — once a group wraps members the two differ and
every later row/column would silently be re-bound to another leaf (#33).
When the siblings carry ``TablixHeader`` cells (a matrix), the new member
gets headers of the same depth and size.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import resolve_tablix
from pbirb_mcp.core.tablix_shape import (
    insert_nth_child,
    place_static_leaf,
    placement_warnings,
)
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd


def _build_static_cell(textbox_name: str, cell_text: str) -> etree._Element:
    cell = etree.Element(q("TablixCell"))
    contents = etree.SubElement(cell, q("CellContents"))
    tb = etree.SubElement(contents, q("Textbox"), Name=textbox_name)
    etree.SubElement(tb, q("CanGrow")).text = "true"
    etree.SubElement(tb, q("KeepTogether")).text = "true"
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    paragraph = etree.SubElement(paragraphs, q("Paragraph"))
    textruns = etree.SubElement(paragraph, q("TextRuns"))
    textrun = etree.SubElement(textruns, q("TextRun"))
    value = etree.SubElement(textrun, q("Value"))
    value.text = encode_text(cell_text)
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    default_name = etree.SubElement(tb, qrd("DefaultName"))
    default_name.text = textbox_name
    etree.SubElement(tb, q("Style"))
    return cell


def _place(
    tablix: etree._Element,
    axis: str,
    position: Optional[int],
    inside_group: Optional[str],
    header_stem: str,
    registry: names.NameRegistry,
):
    return place_static_leaf(
        tablix, axis, position, inside_group, lambda: registry.unique(header_stem)
    )


def _payload_extras(point, axis: str, header_names: list[str]) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if header_names:
        extras["header_textboxes"] = header_names
    if point.relocated_from is not None:
        extras["requested_position"] = point.requested
    warnings = placement_warnings(point, axis)
    if warnings:
        extras["warnings"] = warnings
    return extras


def add_static_row(
    path: str,
    tablix_name: str,
    row_name: str,
    cells: Optional[list[str]] = None,
    position: Optional[int] = None,
    height: Optional[str] = None,
    inside_group: Optional[str] = None,
) -> dict[str, Any]:
    """Add a static (no-group) row.

    ``cells`` is a list of literal strings, one per body column (left to
    right). Shorter list = blank cells for the trailing columns; longer
    list raises ``ValueError``. Each cell's textbox is named
    ``<row_name>_<col_index>``; the cell at column 0 also uses the bare
    ``row_name`` for findability. ``row_name`` must be unique report-wide;
    generated names are suffixed when taken (the payload's ``textboxes``
    lists the names emitted).

    ``position`` is 0-indexed into ``<TablixRows>``; default = append.
    The row's hierarchy member is placed so it owns that body row and
    stays outside every row group: at a group's edge it becomes the group
    member's sibling; strictly inside a group it is placed just before the
    group instead (the payload's ``position`` is then the actual row and
    ``warnings`` says why). ``inside_group`` names a row group to insert
    inside on purpose (the row then repeats per group instance);
    ``position`` must then lie within that group's rows (or just past
    them). ``height`` defaults to ``"0.25in"``.
    """
    names.validate_identifier(row_name, "textbox")
    height_value = units.dimension(height if height is not None else "0.25in", "height")
    cells = cells or []

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    # Report-wide uniqueness for the bare row_name (col 0 cell), checked
    # before any mutation; the other cells' generated names are suffixed
    # if taken.
    registry = names.NameRegistry(doc)
    registry.ensure_unique(row_name)
    registry.claim(row_name)

    body = find_child(tablix, "TablixBody")
    if body is None:
        raise ValueError(f"tablix {tablix_name!r} has no <TablixBody>")
    cols_root = find_child(body, "TablixColumns")
    n_cols = len(find_children(cols_root, "TablixColumn")) if cols_root is not None else 0
    if len(cells) > n_cols:
        raise ValueError(
            f"cells has {len(cells)} entries but tablix has only {n_cols} column(s); "
            "trim or pad to match."
        )

    rows_root = find_child(body, "TablixRows")
    if rows_root is None:
        rows_root = etree.SubElement(body, q("TablixRows"))

    textbox_names = [row_name if n_cols else None] + [
        registry.unique(f"{row_name}_{col_idx}") for col_idx in range(1, n_cols)
    ]

    # Hierarchy first: it validates position / inside_group before any
    # mutation and tells us which body row the new leaf owns.
    point, header_names = _place(
        tablix, "row", position, inside_group, f"{row_name}_RowHeader", registry
    )
    insert_at = point.index

    new_row = etree.Element(q("TablixRow"))
    etree.SubElement(new_row, q("Height")).text = height_value
    cells_root = etree.SubElement(new_row, q("TablixCells"))
    for col_idx in range(n_cols):
        cell_text = cells[col_idx] if col_idx < len(cells) else ""
        cells_root.append(_build_static_cell(textbox_names[col_idx], cell_text))

    insert_nth_child(rows_root, "TablixRow", insert_at, new_row)

    doc.save()
    return {
        "tablix": tablix_name,
        "row_name": row_name,
        "position": insert_at,
        "cells": list(cells),
        "textboxes": [n for n in textbox_names if n is not None],
        **_payload_extras(point, "row", header_names),
    }


def add_static_column(
    path: str,
    tablix_name: str,
    column_name: str,
    cells: Optional[list[str]] = None,
    position: Optional[int] = None,
    width: Optional[str] = None,
    inside_group: Optional[str] = None,
) -> dict[str, Any]:
    """Add a static (no-group) column.

    ``cells`` is a list of literal strings, one per body row (top to
    bottom). Shorter list = blank cells for the trailing rows; longer
    list raises ``ValueError``. Each cell's textbox is named
    ``<column_name>_<row_index>``; the cell at row 0 also uses the bare
    ``column_name`` for findability. ``column_name`` must be unique
    report-wide; generated names are suffixed when taken (the payload's
    ``textboxes`` lists the names emitted).

    ``position`` is 0-indexed into ``<TablixColumns>``; default =
    append. Hierarchy placement and ``inside_group`` work as in
    :func:`add_static_row` (column groups instead of row groups).
    ``width`` defaults to ``"1in"``.
    """
    names.validate_identifier(column_name, "textbox")
    width_value = units.dimension(width if width is not None else "1in", "width")
    cells = cells or []

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    registry = names.NameRegistry(doc)
    registry.ensure_unique(column_name)
    registry.claim(column_name)

    body = find_child(tablix, "TablixBody")
    if body is None:
        raise ValueError(f"tablix {tablix_name!r} has no <TablixBody>")
    rows_root = find_child(body, "TablixRows")
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    n_rows = len(rows)
    if len(cells) > n_rows:
        raise ValueError(
            f"cells has {len(cells)} entries but tablix has only {n_rows} row(s); "
            "trim or pad to match."
        )

    cols_root = find_child(body, "TablixColumns")
    if cols_root is None:
        cols_root = etree.SubElement(body, q("TablixColumns"))

    textbox_names = [column_name] + [
        registry.unique(f"{column_name}_{row_idx}") for row_idx in range(1, n_rows)
    ]

    point, header_names = _place(
        tablix, "column", position, inside_group, f"{column_name}_ColumnHeader", registry
    )
    insert_at = point.index

    # Insert <TablixColumn> at insert_at.
    new_column = etree.Element(q("TablixColumn"))
    etree.SubElement(new_column, q("Width")).text = width_value
    insert_nth_child(cols_root, "TablixColumn", insert_at, new_column)

    # Insert one cell at insert_at in every row.
    for row_idx, row in enumerate(rows):
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            cells_root = etree.SubElement(row, q("TablixCells"))
        cell_text = cells[row_idx] if row_idx < len(cells) else ""
        new_cell = _build_static_cell(textbox_names[row_idx], cell_text)
        insert_nth_child(cells_root, "TablixCell", insert_at, new_cell)

    doc.save()
    return {
        "tablix": tablix_name,
        "column_name": column_name,
        "position": insert_at,
        "cells": list(cells),
        "width": width_value,
        "textboxes": textbox_names[:n_rows],
        **_payload_extras(point, "column", header_names),
    }


__all__ = ["add_static_column", "add_static_row"]
