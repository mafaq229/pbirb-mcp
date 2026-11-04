"""A tablix with merged band headers, like the "RAG Report" the #34
review used (not shipped in tests/fixtures).

``build_band_report`` rewrites ``MainTable`` of the minimal fixture into::

    row 0 (bands):   IVMSBand ColSpan=3 + 2 placeholders | AFDDBand ColSpan=2 + 1 | SoloBand
    row 1 (headers): Head0 ... Head5
    row 2 (detail):  Data0 ... Data5

Report Builder's shape: a ColSpan=N cell followed by N-1 empty
``<TablixCell />`` placeholders.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.tablix_headers import new_textbox
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
BANDS = (("IVMSBand", 3), ("AFDDBand", 2), ("SoloBand", 1))


def _cell(name: str, value: str, col_span: int = 1) -> etree._Element:
    cell = etree.Element(q("TablixCell"))
    contents = etree.SubElement(cell, q("CellContents"))
    contents.append(new_textbox(name, value))
    if col_span > 1:
        etree.SubElement(contents, q("ColSpan")).text = str(col_span)
    return cell


def _row(cells: list[etree._Element]) -> etree._Element:
    row = etree.Element(q("TablixRow"))
    etree.SubElement(row, q("Height")).text = "0.25in"
    cells_root = etree.SubElement(row, q("TablixCells"))
    for cell in cells:
        cells_root.append(cell)
    return row


def build_band_report(path: Path, bands=BANDS) -> Path:
    shutil.copy(FIXTURE, path)
    doc = RDLDocument.open(path)
    tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
    n_cols = sum(width for _, width in bands)

    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns")
    for col in find_children(cols_root, "TablixColumn"):
        cols_root.remove(col)
    for _ in range(n_cols):
        etree.SubElement(etree.SubElement(cols_root, q("TablixColumn")), q("Width")).text = "1in"

    rows_root = find_child(body, "TablixRows")
    for row in find_children(rows_root, "TablixRow"):
        rows_root.remove(row)
    band_cells: list[etree._Element] = []
    for name, width in bands:
        band_cells.append(_cell(name, name.replace("Band", ""), width))
        band_cells.extend(etree.Element(q("TablixCell")) for _ in range(width - 1))
    rows_root.append(_row(band_cells))
    rows_root.append(_row([_cell(f"Head{j}", f"H{j}") for j in range(n_cols)]))
    rows_root.append(_row([_cell(f"Data{j}", "=Fields!Amount.Value") for j in range(n_cols)]))

    col_members = tablix.find(f"{q('TablixColumnHierarchy')}/{q('TablixMembers')}")
    for member in list(col_members):
        col_members.remove(member)
    for _ in range(n_cols):
        etree.SubElement(col_members, q("TablixMember"))

    row_members = tablix.find(f"{q('TablixRowHierarchy')}/{q('TablixMembers')}")
    static = etree.Element(q("TablixMember"))
    etree.SubElement(static, q("KeepWithGroup")).text = "After"
    row_members.insert(0, static)
    doc.save()
    return path


def band_row(path: Path, row_index: int) -> list[tuple[str, int]]:
    """``[(textbox name or '-', ColSpan)]`` for one body row of MainTable."""
    doc = RDLDocument.open(path)
    tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
    row = find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")[row_index]
    out = []
    for cell in find_children(find_child(row, "TablixCells"), "TablixCell"):
        tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
        span = cell.find(f"{q('CellContents')}/{q('ColSpan')}")
        out.append(
            (tb.get("Name") if tb is not None else "-", int(span.text) if span is not None else 1)
        )
    return out
