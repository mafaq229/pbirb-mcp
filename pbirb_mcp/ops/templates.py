"""Snippet-template tools.

Build RDL fragments programmatically and append them to
``<Body>/<ReportItems>``. The tablix template lives here; the chart
template moved to ``pbirb_mcp.ops.chart`` in v0.3.0 alongside the chart
mutation tools.

Tablix template: a basic Tablix mirroring the fixture's ``MainTable``
shape (header row with static labels, detail row binding each column to
a Field).

Dataset binding: ``dataset_name`` must already exist in the report's
``<DataSets>`` collection — we don't create datasets implicitly.
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import resolve_dataset
from pbirb_mcp.core.xpath import find_child, q, qrd
from pbirb_mcp.ops.body import _ensure_body_report_items, _names_in, _resolve_body

# ---- shared cell builders -------------------------------------------------


def _build_cell_textbox(name: str, value_text: str) -> etree._Element:
    """A minimal Textbox suited to live inside a TablixCell — same shape
    Report Builder emits for fixture cells (CanGrow, KeepTogether,
    Paragraphs, rd:DefaultName, Style)."""
    cell = etree.Element(q("TablixCell"))
    contents = etree.SubElement(cell, q("CellContents"))
    tb = etree.SubElement(contents, q("Textbox"), Name=name)
    etree.SubElement(tb, q("CanGrow")).text = "true"
    etree.SubElement(tb, q("KeepTogether")).text = "true"
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    paragraph = etree.SubElement(paragraphs, q("Paragraph"))
    textruns = etree.SubElement(paragraph, q("TextRuns"))
    textrun = etree.SubElement(textruns, q("TextRun"))
    value = etree.SubElement(textrun, q("Value"))
    value.text = encode_text(value_text)
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    default_name = etree.SubElement(tb, qrd("DefaultName"))
    default_name.text = name
    etree.SubElement(tb, q("Style"))
    return cell


def _all_named_items(doc: RDLDocument) -> set[str]:
    """Names of every Tablix / Textbox / Image / Chart in the body."""
    body = _resolve_body(doc)
    items = find_child(body, "ReportItems")
    return _names_in(items) if items is not None else set()


def _ensure_no_collision(doc: RDLDocument, name: str) -> None:
    if name in _all_named_items(doc):
        raise ValueError(f"body item named {name!r} already exists; pick a unique name")


# ---- insert_tablix_from_template -----------------------------------------


def insert_tablix_from_template(
    path: str,
    name: str,
    dataset_name: str,
    columns: list[str],
    top: str,
    left: str,
    width: str,
    height: str,
) -> dict[str, Any]:
    if not columns:
        raise ValueError("columns must list at least one field name")

    doc = RDLDocument.open(path)
    # Validate dataset exists; the resolver raises ElementNotFoundError if not.
    resolve_dataset(doc, dataset_name)
    _ensure_no_collision(doc, name)

    tablix = etree.Element(q("Tablix"), Name=name)

    # ---- TablixBody ----
    body_root = etree.SubElement(tablix, q("TablixBody"))
    cols_root = etree.SubElement(body_root, q("TablixColumns"))
    for _ in columns:
        col = etree.SubElement(cols_root, q("TablixColumn"))
        etree.SubElement(col, q("Width")).text = "1in"

    rows_root = etree.SubElement(body_root, q("TablixRows"))
    # Header row.
    header_row = etree.SubElement(rows_root, q("TablixRow"))
    etree.SubElement(header_row, q("Height")).text = "0.25in"
    header_cells = etree.SubElement(header_row, q("TablixCells"))
    for col_name in columns:
        # Header textbox name follows the fixture's "Header<Name>" convention,
        # prefixed with the tablix name to avoid report-wide collisions.
        header_cells.append(_build_cell_textbox(f"{name}_Header_{col_name}", col_name))
    # Detail row.
    detail_row = etree.SubElement(rows_root, q("TablixRow"))
    etree.SubElement(detail_row, q("Height")).text = "0.25in"
    detail_cells = etree.SubElement(detail_row, q("TablixCells"))
    for col_name in columns:
        detail_cells.append(_build_cell_textbox(f"{name}_{col_name}", f"=Fields!{col_name}.Value"))

    # ---- TablixColumnHierarchy ----
    col_h = etree.SubElement(tablix, q("TablixColumnHierarchy"))
    col_members = etree.SubElement(col_h, q("TablixMembers"))
    for _ in columns:
        etree.SubElement(col_members, q("TablixMember"))

    # ---- TablixRowHierarchy ----
    row_h = etree.SubElement(tablix, q("TablixRowHierarchy"))
    row_members = etree.SubElement(row_h, q("TablixMembers"))
    # Header leaf (KeepWithGroup=After matches the fixture pattern).
    header_member = etree.SubElement(row_members, q("TablixMember"))
    etree.SubElement(header_member, q("KeepWithGroup")).text = "After"
    # Details leaf.
    details_member = etree.SubElement(row_members, q("TablixMember"))
    etree.SubElement(details_member, q("Group"), Name="Details")

    # ---- footer fields: dataset binding + layout ----
    etree.SubElement(tablix, q("DataSetName")).text = dataset_name
    etree.SubElement(tablix, q("Top")).text = top
    etree.SubElement(tablix, q("Left")).text = left
    etree.SubElement(tablix, q("Height")).text = height
    etree.SubElement(tablix, q("Width")).text = width
    style = etree.SubElement(tablix, q("Style"))
    border = etree.SubElement(style, q("Border"))
    etree.SubElement(border, q("Style")).text = "None"

    # Append into Body/ReportItems.
    body = _resolve_body(doc)
    items = _ensure_body_report_items(body)
    items.append(tablix)

    doc.save()
    return {"name": name, "kind": "Tablix", "columns": list(columns)}


# Chart template insertion moved to pbirb_mcp.ops.chart in v0.3.0
# alongside the chart mutation tools (add_chart_series, set_chart_axis,
# set_chart_legend, etc.). Re-exported here for backward compatibility
# with any caller importing it from this module.
from pbirb_mcp.ops.chart import insert_chart_from_template  # noqa: E402, F401


__all__ = [
    "insert_chart_from_template",
    "insert_tablix_from_template",
]
