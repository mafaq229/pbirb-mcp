"""Static (no-group) row / column tools (v0.2 commit 5).

These differ from :func:`add_tablix_column` and :func:`add_subtotal_row`
in that the new row / column is **not** bound to a data field or a
group expression — every cell holds literal text. Use them for header
captions, footer labels, or banner rows.

Both tools insert a corresponding ``<TablixMember/>`` (without a
``<Group>``) into the matching hierarchy at the same index, keeping
the hierarchy width consistent with the body.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import resolve_tablix
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd


def _all_textbox_names(doc: RDLDocument) -> set[str]:
    return {tb.get("Name") for tb in doc.root.iter(q("Textbox")) if tb.get("Name")}


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
    value.text = cell_text
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    default_name = etree.SubElement(tb, qrd("DefaultName"))
    default_name.text = textbox_name
    etree.SubElement(tb, q("Style"))
    return cell


def add_static_row(
    path: str,
    tablix_name: str,
    row_name: str,
    cells: Optional[list[str]] = None,
    position: Optional[int] = None,
    height: Optional[str] = None,
) -> dict[str, Any]:
    """Add a static (no-group) row.

    ``cells`` is a list of literal strings, one per body column (left to
    right). Shorter list = blank cells for the trailing columns; longer
    list raises ``ValueError``. Each cell's textbox is named
    ``<row_name>_<col_index>``; the cell at column 0 also uses the bare
    ``row_name`` for findability.

    ``position`` is 0-indexed into ``<TablixRows>``; default = append.
    ``height`` defaults to ``"0.25in"``.
    """
    height_value = height if height is not None else "0.25in"
    cells = cells or []

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

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
    n_rows = len(find_children(rows_root, "TablixRow"))
    insert_at = n_rows if position is None else position
    if insert_at < 0 or insert_at > n_rows:
        raise IndexError(f"position {position} out of range; tablix has {n_rows} row(s)")

    # Validate name uniqueness for the bare row_name (col 0 cell).
    existing = _all_textbox_names(doc)
    if row_name in existing:
        raise ValueError(
            f"textbox name {row_name!r} already exists in this report; pick a unique name."
        )

    new_row = etree.Element(q("TablixRow"))
    etree.SubElement(new_row, q("Height")).text = height_value
    cells_root = etree.SubElement(new_row, q("TablixCells"))
    for col_idx in range(n_cols):
        cell_text = cells[col_idx] if col_idx < len(cells) else ""
        textbox_name = row_name if col_idx == 0 else f"{row_name}_{col_idx}"
        cells_root.append(_build_static_cell(textbox_name, cell_text))

    if insert_at == n_rows:
        rows_root.append(new_row)
    else:
        rows_root.insert(insert_at, new_row)

    # Mirror in row hierarchy: insert a leaf <TablixMember/> at same index.
    row_h = find_child(tablix, "TablixRowHierarchy")
    if row_h is None:
        row_h = etree.SubElement(tablix, q("TablixRowHierarchy"))
    members_root = find_child(row_h, "TablixMembers")
    if members_root is None:
        members_root = etree.SubElement(row_h, q("TablixMembers"))
    new_member = etree.Element(q("TablixMember"))
    top_members = list(members_root)
    if insert_at >= len(top_members):
        members_root.append(new_member)
    else:
        members_root.insert(insert_at, new_member)

    doc.save()
    return {
        "tablix": tablix_name,
        "row_name": row_name,
        "position": insert_at,
        "cells": list(cells),
    }


def add_static_column(
    path: str,
    tablix_name: str,
    column_name: str,
    cells: Optional[list[str]] = None,
    position: Optional[int] = None,
    width: Optional[str] = None,
) -> dict[str, Any]:
    """Add a static (no-group) column.

    ``cells`` is a list of literal strings, one per body row (top to
    bottom). Shorter list = blank cells for the trailing rows; longer
    list raises ``ValueError``. Each cell's textbox is named
    ``<column_name>_<row_index>``; the cell at row 0 also uses the bare
    ``column_name`` for findability.

    ``position`` is 0-indexed into ``<TablixColumns>``; default =
    append. ``width`` defaults to ``"1in"``.
    """
    width_value = width if width is not None else "1in"
    cells = cells or []

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

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
    n_cols = len(find_children(cols_root, "TablixColumn"))
    insert_at = n_cols if position is None else position
    if insert_at < 0 or insert_at > n_cols:
        raise IndexError(f"position {position} out of range; tablix has {n_cols} column(s)")

    existing = _all_textbox_names(doc)
    if column_name in existing:
        raise ValueError(
            f"textbox name {column_name!r} already exists in this report; pick a unique name."
        )

    # Insert <TablixColumn> at insert_at.
    new_column = etree.Element(q("TablixColumn"))
    etree.SubElement(new_column, q("Width")).text = width_value
    if insert_at == n_cols:
        cols_root.append(new_column)
    else:
        cols_root.insert(insert_at, new_column)

    # Insert one cell at insert_at in every row.
    for row_idx, row in enumerate(rows):
        cells_root = find_child(row, "TablixCells")
        if cells_root is None:
            cells_root = etree.SubElement(row, q("TablixCells"))
        cell_text = cells[row_idx] if row_idx < len(cells) else ""
        textbox_name = column_name if row_idx == 0 else f"{column_name}_{row_idx}"
        new_cell = _build_static_cell(textbox_name, cell_text)
        existing_cells = find_children(cells_root, "TablixCell")
        if insert_at == len(existing_cells):
            cells_root.append(new_cell)
        else:
            cells_root.insert(insert_at, new_cell)

    # Mirror in column hierarchy.
    col_h = find_child(tablix, "TablixColumnHierarchy")
    if col_h is None:
        col_h = etree.SubElement(tablix, q("TablixColumnHierarchy"))
    members_root = find_child(col_h, "TablixMembers")
    if members_root is None:
        members_root = etree.SubElement(col_h, q("TablixMembers"))
    new_member = etree.Element(q("TablixMember"))
    top_members = list(members_root)
    if insert_at >= len(top_members):
        members_root.append(new_member)
    else:
        members_root.insert(insert_at, new_member)

    doc.save()
    return {
        "tablix": tablix_name,
        "column_name": column_name,
        "position": insert_at,
        "cells": list(cells),
        "width": width_value,
    }


__all__ = ["add_static_column", "add_static_row"]
