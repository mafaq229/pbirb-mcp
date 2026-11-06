"""Snippet-template tools.

Build RDL fragments programmatically and append them to
``<Body>/<ReportItems>``. The tablix template lives here; the chart
template moved to ``pbirb_mcp.ops.chart`` in v0.3.0 alongside the chart
mutation tools.

Tablix template: a basic Tablix mirroring the fixture's ``MainTable``
shape (header row with static labels, detail row binding each column to
a Field).

Dataset binding: ``dataset_name`` must already exist in the report's
``<DataSets>`` collection — we don't create datasets implicitly — and
every column must be one of its ``<Field>`` names.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import resolve_dataset
from pbirb_mcp.core.xpath import q, qrd
from pbirb_mcp.ops.containers import place

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


def _normalize_columns(columns: Any) -> list[tuple[str, str]]:
    """``columns`` entries → ``[(field, header caption)]``.

    An entry is a field name (its header shows the name) or
    ``{"field": <name>, "header": <caption>}`` for a display caption that
    need not be an identifier (``"Unit Price (USD)"``)."""
    if not isinstance(columns, list) or not columns:
        raise ValueError("columns must list at least one field name")
    out: list[tuple[str, str]] = []
    for i, entry in enumerate(columns):
        if isinstance(entry, str):
            field, header = entry, entry
        elif isinstance(entry, dict):
            unknown = set(entry) - {"field", "header"}
            if unknown or "field" not in entry:
                raise ValueError(
                    f"columns[{i}] must be a field name or {{'field': ..., 'header': ...}}; "
                    f"got {entry!r}"
                )
            field = entry["field"]
            header = entry.get("header", field)
            if not isinstance(header, str):
                raise ValueError(f"columns[{i}].header must be a string; got {header!r}")
        else:
            raise ValueError(
                f"columns[{i}] must be a field name or {{'field': ..., 'header': ...}}; "
                f"got {entry!r}"
            )
        try:
            names.validate_identifier(field, "column field")
        except ValueError as exc:
            raise ValueError(
                f"{exc}. To show a caption like {field!r}, pass "
                f'{{"field": "<dataset field>", "header": {json.dumps(field)}}}.'
            ) from None
        out.append((field, header))
    fields = [f for f, _ in out]
    if len(set(fields)) != len(fields):
        raise ValueError(f"columns lists a field more than once: {fields!r}")
    return out


def _check_fields(dataset: etree._Element, dataset_name: str, fields: list[str]) -> None:
    """Every requested field must be a ``<Field Name>`` of the dataset."""
    available = [f.get("Name") for f in dataset.findall(f"{q('Fields')}/{q('Field')}")]
    missing = [f for f in fields if f not in available]
    if missing:
        listing = ", ".join(available) if available else "none declared"
        raise ValueError(
            f"dataset {dataset_name!r} has no field(s) {', '.join(map(repr, missing))}; "
            f"available fields: {listing}."
        )


def _split_width(width: str, count: int) -> list[str]:
    """``width`` split into ``count`` equal column widths in its unit, the
    last taking the rounding remainder — so ``Tablix/Width`` is exactly
    the sum of its columns (#89)."""
    unit = units.parse_size(width).unit
    total = units.to_inches(width)
    share = units.format_size(total / count, unit)
    last = units.format_size(total - units.to_inches(share) * (count - 1), unit)
    return [share] * (count - 1) + [last]


# ---- insert_tablix_from_template -----------------------------------------


def insert_tablix_from_template(
    path: str,
    name: str,
    dataset_name: str,
    columns: list[Any],
    top: str,
    left: str,
    width: str,
    height: str,
    container: Optional[str] = None,
    repeat_header: bool = True,
) -> dict[str, Any]:
    """Append a header-row + detail-row Tablix bound to ``dataset_name``.

    ``columns`` entries are field names of the dataset (header shows the
    name) or ``{"field": ..., "header": ...}`` for a display caption.
    Unknown fields raise, listing the dataset's fields. The tablix name
    must be unused report-wide; every generated name (header / detail
    textboxes ``<name>_Header_<field>`` / ``<name>_<field>``, the detail
    group ``Details`` → ``Details1`` ...) is suffixed until unique.
    With ``container`` it goes inside that body Rectangle (#83; a List's
    only when the list is bound to ``dataset_name``).

    The header row's member is ``KeepWithGroup=After`` and, with
    ``repeat_header`` (default), ``RepeatOnNewPage=true`` — the header
    repeats at the top of every page (#67).
    """
    names.validate_identifier(name, "tablix")
    spec = _normalize_columns(columns)
    top, left, width, height = units.box(top, left, width, height)

    doc = RDLDocument.open(path)
    # Validate dataset exists; the resolver raises ElementNotFoundError if not.
    dataset = resolve_dataset(doc, dataset_name)
    registry = names.NameRegistry(doc)
    registry.ensure_unique(name)
    registry.claim(name)
    fields = [f for f, _ in spec]
    _check_fields(dataset, dataset_name, fields)
    # Generated names — suffixed when taken anywhere in the report, so a
    # second template emits Details1 rather than a second Details.
    header_names = [
        registry.unique(names.sanitize_identifier(f"{name}_Header_{f}")) for f in fields
    ]
    detail_names = [registry.unique(names.sanitize_identifier(f"{name}_{f}")) for f in fields]
    details_group = registry.unique("Details")

    tablix = etree.Element(q("Tablix"), Name=name)

    # ---- TablixBody ----
    body_root = etree.SubElement(tablix, q("TablixBody"))
    cols_root = etree.SubElement(body_root, q("TablixColumns"))
    for column_width in _split_width(width, len(fields)):
        col = etree.SubElement(cols_root, q("TablixColumn"))
        etree.SubElement(col, q("Width")).text = column_width

    rows_root = etree.SubElement(body_root, q("TablixRows"))
    # Header row.
    header_row = etree.SubElement(rows_root, q("TablixRow"))
    etree.SubElement(header_row, q("Height")).text = "0.25in"
    header_cells = etree.SubElement(header_row, q("TablixCells"))
    for (_field, header), tb_name in zip(spec, header_names):
        # Header textbox name follows the fixture's "Header<Name>" convention,
        # prefixed with the tablix name to avoid report-wide collisions.
        header_cells.append(_build_cell_textbox(tb_name, header))
    # Detail row.
    detail_row = etree.SubElement(rows_root, q("TablixRow"))
    etree.SubElement(detail_row, q("Height")).text = "0.25in"
    detail_cells = etree.SubElement(detail_row, q("TablixCells"))
    for field, tb_name in zip(fields, detail_names):
        detail_cells.append(_build_cell_textbox(tb_name, f"=Fields!{field}.Value"))

    # ---- TablixColumnHierarchy ----
    col_h = etree.SubElement(tablix, q("TablixColumnHierarchy"))
    col_members = etree.SubElement(col_h, q("TablixMembers"))
    for _ in fields:
        etree.SubElement(col_members, q("TablixMember"))

    # ---- TablixRowHierarchy ----
    row_h = etree.SubElement(tablix, q("TablixRowHierarchy"))
    row_members = etree.SubElement(row_h, q("TablixMembers"))
    # Header leaf (KeepWithGroup=After matches the fixture pattern;
    # RepeatOnNewPage repeats it on every page, as Report Builder's
    # "repeat header rows" does for a table).
    header_member = etree.SubElement(row_members, q("TablixMember"))
    etree.SubElement(header_member, q("KeepWithGroup")).text = "After"
    if repeat_header:
        etree.SubElement(header_member, q("RepeatOnNewPage")).text = "true"
    # Details leaf.
    details_member = etree.SubElement(row_members, q("TablixMember"))
    etree.SubElement(details_member, q("Group"), Name=details_group)

    # ---- footer fields: dataset binding + layout ----
    etree.SubElement(tablix, q("DataSetName")).text = dataset_name
    etree.SubElement(tablix, q("Top")).text = top
    etree.SubElement(tablix, q("Left")).text = left
    etree.SubElement(tablix, q("Height")).text = height
    etree.SubElement(tablix, q("Width")).text = width
    style = etree.SubElement(tablix, q("Style"))
    border = etree.SubElement(style, q("Border"))
    etree.SubElement(border, q("Style")).text = "None"

    # Append into Body/ReportItems (or the container Rectangle's).
    result = {
        "name": name,
        "kind": "Tablix",
        "columns": fields,
        "headers": [header for _, header in spec],
        "header_textboxes": header_names,
        "detail_textboxes": detail_names,
        "details_group": details_group,
    }
    return place(doc, tablix, container, result, dataset_name=dataset_name)


# Chart template insertion moved to pbirb_mcp.ops.chart in v0.3.0
# alongside the chart mutation tools (add_chart_series, set_chart_axis,
# set_chart_legend, etc.). Re-exported here for backward compatibility
# with any caller importing it from this module.
from pbirb_mcp.ops.chart import insert_chart_from_template  # noqa: E402, F401

__all__ = [
    "insert_chart_from_template",
    "insert_tablix_from_template",
]
