"""Group subtotal-row tool (v0.2 commit 3).

``add_subtotal_row`` appends an extra ``<TablixMember>`` inside an
existing row-group's ``<TablixMembers>`` and adds a matching body
row. Each cell of the new row either holds an aggregate expression
(when the user supplies one for that column) or is blank.

Aggregates are matched to columns by **textbox name** in the existing
data row — the same handle used by :func:`add_tablix_column` and
:func:`remove_tablix_column`. The user passes a list of
``{"column": <data-row textbox name>, "expression": <aggregate>}``
entries; only listed columns get aggregate cells, all others stay
blank.

Position:

* ``"footer"`` (default) — appends after every existing child of the
  group wrapper. The new body row goes at the end of ``<TablixRows>``.
* ``"header"`` — inserts immediately after the group-header leaf
  (position 1 of the wrapper's ``<TablixMembers>``). The new body row
  goes immediately after the existing group-header row (body row 1).

Both positions assume the group was added via :func:`add_row_group`,
which leaves a single group-header leaf at child-index 0 of the
wrapper.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd

_VALID_POSITIONS = frozenset({"header", "footer"})


def _find_row_member_wrapper(tablix: etree._Element, group_name: str) -> Optional[etree._Element]:
    """Find the row-axis TablixMember whose <Group Name=...> matches.
    Returns None if absent or only present on the column axis."""
    row_hierarchy = find_child(tablix, "TablixRowHierarchy")
    if row_hierarchy is None:
        return None
    for member in row_hierarchy.iter(q("TablixMember")):
        group = find_child(member, "Group")
        if group is not None and group.get("Name") == group_name:
            return member
    return None


def _data_row_textbox_names(tablix: etree._Element) -> list[Optional[str]]:
    """Return the textbox name at each column index of the *last* TablixRow.
    None for cells with no textbox."""
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    if rows_root is None:
        return []
    rows = find_children(rows_root, "TablixRow")
    if not rows:
        return []
    last_row = rows[-1]
    cells_root = find_child(last_row, "TablixCells")
    if cells_root is None:
        return []
    names: list[Optional[str]] = []
    for cell in find_children(cells_root, "TablixCell"):
        tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
        names.append(tb.get("Name") if tb is not None else None)
    return names


def _build_subtotal_cell(textbox_name: str, cell_text: str) -> etree._Element:
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


def add_subtotal_row(
    path: str,
    tablix_name: str,
    group_name: str,
    aggregates: list[dict[str, str]],
    position: str = "footer",
) -> dict[str, Any]:
    """Add a subtotal row to ``group_name``'s row-axis member.

    ``aggregates`` is a list of ``{"column": <textbox-name>, "expression":
    <aggregate>}`` entries matched against the data row's textbox names.
    Columns not listed get blank cells. New cell textbox names are
    ``<group>_<position>_<row_index_in_member>_<col>`` — synthesized to
    stay report-wide unique.

    ``position`` is ``"footer"`` (default — appends) or ``"header"``
    (inserts after the group-header leaf).
    """
    if position not in _VALID_POSITIONS:
        raise ValueError(
            f"position {position!r} not valid; expected one of {sorted(_VALID_POSITIONS)}"
        )

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)

    wrapper = _find_row_member_wrapper(tablix, group_name)
    if wrapper is None:
        raise ElementNotFoundError(
            f"row-axis group {group_name!r} not found in tablix {tablix_name!r}"
        )

    inner_members = find_child(wrapper, "TablixMembers")
    if inner_members is None:
        # Group with no children — can't meaningfully add a subtotal because
        # there's no data structure to total.
        raise ValueError(
            f"group {group_name!r} has no nested members; nothing to total. "
            "Was the group added via add_row_group?"
        )

    # Build the new member (a static leaf — no <Group>, just a placeholder).
    new_member = etree.Element(q("TablixMember"))

    # Build the new body row.
    new_row = etree.Element(q("TablixRow"))
    etree.SubElement(new_row, q("Height")).text = "0.25in"
    cells_root = etree.SubElement(new_row, q("TablixCells"))

    column_names = _data_row_textbox_names(tablix)
    aggregates_by_column = {a["column"]: a["expression"] for a in aggregates}

    # Validate all referenced columns exist in the data row.
    missing = [c for c in aggregates_by_column if c not in column_names]
    if missing:
        raise ElementNotFoundError(
            f"aggregate column(s) {missing!r} not found among data-row "
            f"textbox names {[c for c in column_names if c is not None]!r} "
            f"in tablix {tablix_name!r}"
        )

    member_index_label = "Footer" if position == "footer" else "Header"
    for col_idx, col_textbox in enumerate(column_names):
        textbox_name = f"{group_name}_{member_index_label}_{col_idx}"
        cell_text = (
            aggregates_by_column[col_textbox]
            if col_textbox is not None and col_textbox in aggregates_by_column
            else ""
        )
        cells_root.append(_build_subtotal_cell(textbox_name, cell_text))

    # Insert into the row hierarchy + body.
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    if rows_root is None:
        rows_root = etree.SubElement(body, q("TablixRows")) if body is not None else None

    if position == "footer":
        inner_members.append(new_member)
        if rows_root is not None:
            rows_root.append(new_row)
    else:  # "header"
        # Insert at index 1 — right after the group-header leaf.
        inner_members.insert(1, new_member)
        if rows_root is not None:
            existing_rows = find_children(rows_root, "TablixRow")
            insert_at = 1 if existing_rows else 0
            rows_root.insert(insert_at, new_row)

    doc.save()
    return {
        "tablix": tablix_name,
        "group": group_name,
        "position": position,
        "aggregates": list(aggregates),
    }


__all__ = ["add_subtotal_row"]
