"""Span bookkeeping for tablix cell grids (body rows and corner rows).

Report Builder writes a merged cell as one *owner* cell whose
``<CellContents>`` carries ``<ColSpan>N</ColSpan>`` (and, in the
``TablixCorner`` only, ``<RowSpan>M</RowSpan>``), followed by ``N-1``
empty *placeholder* cells (``<TablixCell />`` / ``<TablixCornerCell />``,
no ``<CellContents>``). Every row therefore keeps exactly one cell
element per grid column, and ``Σ ColSpan`` over a row's owners equals the
column count.

The helpers here keep that invariant when a column is inserted into or
removed from a row:

* :func:`owner_map` — the owner index of every cell in a row.
* :func:`insert_cell` — insert at a column index; inside a span the
  owner grows by one and a placeholder is inserted instead of new content.
* :func:`remove_cell` — remove a column index; a covered column shrinks
  its owner, and removing an owner that spans several columns keeps its
  contents (the owner shrinks and one of its placeholders goes instead).

Whitespace: :func:`detach` removes an element without leaving the
parent's closing tag mis-indented, and :func:`reindent` lays out an
element that was moved inside a pretty-printed document.
"""

from __future__ import annotations

from typing import Callable, Optional

from lxml import etree

from pbirb_mcp.core.document import indent_inserted
from pbirb_mcp.core.xpath import find_child, find_children, q

COL_SPAN = "ColSpan"
ROW_SPAN = "RowSpan"

# CellContents children that are spans rather than the wrapped report item.
_SPAN_TAGS = {q(COL_SPAN), q(ROW_SPAN)}


# ---- whitespace-preserving tree edits --------------------------------------


def detach(elem: etree._Element) -> None:
    """Remove ``elem`` from its parent, keeping the parent's layout.

    lxml removes an element together with its tail; when ``elem`` is the
    last child that tail is the whitespace before the parent's closing
    tag, so it is handed to the previous sibling (or the parent's text).
    """
    parent = elem.getparent()
    if parent is None:
        return
    if elem.getnext() is None:
        prev = elem.getprevious()
        if prev is not None:
            prev.tail = elem.tail
        elif not (parent.text or "").strip():
            # Parent becomes empty — collapse it to ``<X />``-style content.
            parent.text = None
    parent.remove(elem)


def _indent_unit(root: etree._Element) -> Optional[str]:
    text = root.text or ""
    if "\n" not in text:
        return None  # not pretty-printed — impose no layout
    unit = text.rsplit("\n", 1)[1]
    return unit if unit and not unit.strip() else "  "


def reindent(elem: etree._Element) -> None:
    """Re-indent ``elem`` (and its subtree) for its current position in a
    pretty-printed document; a no-op for compact documents."""
    root = elem.getroottree().getroot()
    unit = _indent_unit(root)
    if unit is not None:
        indent_inserted(elem, unit)


# ---- span reads / writes ---------------------------------------------------


def span(cell: etree._Element, tag: str = COL_SPAN) -> int:
    """``ColSpan`` (or ``RowSpan``) of ``cell``'s ``<CellContents>``;
    1 when absent, unparsable or < 1."""
    contents = find_child(cell, "CellContents")
    node = find_child(contents, tag) if contents is not None else None
    if node is None:
        return 1
    try:
        value = int((node.text or "").strip())
    except ValueError:
        return 1
    return value if value >= 1 else 1


def set_span(cell: etree._Element, tag: str, value: int) -> None:
    """Write ``<tag>value</tag>`` into ``cell``'s ``<CellContents>``
    (after the report item, ColSpan before RowSpan). A no-op when the
    cell has no contents (placeholders carry no spans)."""
    contents = find_child(cell, "CellContents")
    if contents is None:
        return
    node = find_child(contents, tag)
    if node is None:
        node = etree.Element(q(tag))
        anchor = find_child(contents, ROW_SPAN) if tag == COL_SPAN else None
        if anchor is not None:
            anchor.addprevious(node)
        else:
            contents.append(node)
    node.text = str(value)


def is_placeholder(cell: etree._Element) -> bool:
    """True for a cell without ``<CellContents>`` (a span placeholder)."""
    return find_child(cell, "CellContents") is None


def report_item_names(cell: etree._Element) -> list[str]:
    """``Name`` of the report item(s) inside ``cell``'s ``<CellContents>``
    (descendants included, e.g. textboxes inside a rectangle)."""
    contents = find_child(cell, "CellContents")
    if contents is None:
        return []
    return [el.get("Name") for el in contents.iter() if el is not contents and el.get("Name")]


def is_blank(cell: etree._Element) -> bool:
    """True for a placeholder or a cell holding only a textbox whose
    values are all empty — content a span may cover without losing
    anything the author wrote."""
    contents = find_child(cell, "CellContents")
    if contents is None:
        return True
    items = [c for c in contents if isinstance(c.tag, str) and c.tag not in _SPAN_TAGS]
    if not items:
        return True
    if len(items) != 1 or items[0].tag != q("Textbox"):
        return False
    return all(not (v.text or "").strip() for v in items[0].iter(q("Value")))


# ---- row-level span maps ---------------------------------------------------


def cells_of(container: etree._Element, cell_tag: str = "TablixCell") -> list[etree._Element]:
    return find_children(container, cell_tag)


def owner_map(cells: list[etree._Element]) -> list[int]:
    """For each cell, the index of the cell whose ``ColSpan`` covers it
    (its own index for an owner, or for a stray placeholder no span
    reaches). Spans are honoured positionally: the ``N-1`` cells after an
    owner with ``ColSpan=N`` are covered whatever they contain."""
    owners: list[int] = []
    i = 0
    while i < len(cells):
        width = 1 if is_placeholder(cells[i]) else span(cells[i])
        covered = min(i + width, len(cells)) - i
        owners.extend([i] * covered)
        i += covered
    return owners


def inside_span(cells: list[etree._Element], index: int) -> bool:
    """True iff inserting a cell before position ``index`` lands inside an
    existing span (between an owner and one of its placeholders)."""
    if index <= 0 or index >= len(cells):
        return False
    return owner_map(cells)[index] < index


def widen_span(
    container: etree._Element,
    index: int,
    cell_tag: str = "TablixCell",
) -> bool:
    """If a column inserted at ``index`` lands inside a span, grow that
    span's owner by one, insert an empty placeholder at ``index`` and
    return True; otherwise change nothing and return False."""
    cells = cells_of(container, cell_tag)
    if not inside_span(cells, index):
        return False
    owner = cells[owner_map(cells)[index]]
    set_span(owner, COL_SPAN, span(owner) + 1)
    cells[index].addprevious(etree.Element(q(cell_tag)))
    return True


def insert_cell(
    container: etree._Element,
    index: int,
    make_cell: Callable[[], etree._Element],
    cell_tag: str = "TablixCell",
) -> Optional[etree._Element]:
    """Insert a column at ``index`` into one row.

    Inside a span the owner's ``ColSpan`` grows by one and an empty
    placeholder is inserted (``None`` is returned, ``make_cell`` is not
    called); otherwise ``make_cell()`` is inserted and returned.
    """
    if widen_span(container, index, cell_tag):
        return None
    cells = cells_of(container, cell_tag)
    new_cell = make_cell()
    if index >= len(cells):
        if cells:
            cells[-1].addnext(new_cell)
        else:
            container.append(new_cell)
    else:
        cells[index].addprevious(new_cell)
    return new_cell


def remove_cell(
    container: etree._Element,
    index: int,
    cell_tag: str = "TablixCell",
) -> list[str]:
    """Remove column ``index`` from one row; returns the names of the
    report items deleted with it.

    * A column covered by a span: its owner's ``ColSpan`` shrinks by one.
    * An owner spanning several columns: it keeps its contents and shrinks
      by one; the next placeholder of the span is removed instead.
    * Anything else: the cell (and its contents) is removed.
    """
    cells = cells_of(container, cell_tag)
    if index < 0 or index >= len(cells):
        return []
    owners = owner_map(cells)
    owner_idx = owners[index]
    owner = cells[owner_idx]
    width = 1 if is_placeholder(owner) else span(owner)
    victim_idx = index if owner_idx != index else index + 1
    if width > 1 and victim_idx < len(cells):
        set_span(owner, COL_SPAN, width - 1)
        victim = cells[victim_idx]
        removed = report_item_names(victim)
        detach(victim)
        return removed
    victim = cells[index]
    removed = report_item_names(victim)
    detach(victim)
    return removed


__all__ = [
    "COL_SPAN",
    "ROW_SPAN",
    "cells_of",
    "detach",
    "inside_span",
    "insert_cell",
    "is_blank",
    "is_placeholder",
    "owner_map",
    "reindent",
    "remove_cell",
    "report_item_names",
    "set_span",
    "span",
    "widen_span",
]
