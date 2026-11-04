"""Tablix header levels, the ``TablixCorner``, and a shape checker.

Header cells of a tablix do not live in the body. A row or column group's
caption is the ``<TablixHeader>`` (``<Size>`` + ``<CellContents>``) of its
hierarchy member; body columns map 1:1 to column-hierarchy *leaves* and
body rows to row-hierarchy leaves (see :mod:`pbirb_mcp.core.tablix_shape`).

* The **column-header depth** is the number of header rows above the body:
  along every root-to-leaf path of the column hierarchy, the sum of the
  ``TablixHeader`` levels (a header covering several levels says so with
  ``RowSpan`` in its ``CellContents``). Every path must agree.
* The **row-header depth** is the number of header columns left of the
  body, likewise from the row hierarchy (``ColSpan`` spans levels).
* The **corner** (``<TablixCorner>``) fills the top-left block where the
  two meet: one ``TablixCornerRow`` per column-header level, each with one
  ``TablixCornerCell`` per row-header level. It exists only when both
  depths are non-zero.

Public helpers:

* :func:`header_levels` / :func:`header_depth` — per-leaf and overall depth.
* :func:`ensure_corner` — create, grow, shrink or drop the corner so it
  matches ``column depth × row depth`` (span-aware; reports the textbox
  names it created or removed).
* :func:`pad_header_levels` — give short hierarchy paths blank headers so
  every path has the same depth.
* :func:`new_textbox` — the blank/static textbox Report Builder emits.
* :func:`new_header` / :func:`header_around` — a ``<TablixHeader>`` with a
  new textbox, or around an existing report item.
* :func:`shape_problems` — every leaf/column/row/span/header/corner
  inconsistency of one tablix, as human-readable strings;
  :func:`shape_findings` — the same, each with its location in the
  tablix and a severity (the ``tablix-structure`` lint rule's source).
"""

from __future__ import annotations

from typing import Any, NamedTuple, Optional

from lxml import etree

from pbirb_mcp.core import tablix_spans as spans
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.tablix_shape import column_leaves, row_leaves
from pbirb_mcp.core.xpath import find_child, find_children, q, qrd

COLUMN = "column"
ROW = "row"
_AXES = (COLUMN, ROW)

# Default header sizes Report Builder uses: header rows are 0.25in tall,
# row-header columns 1in wide.
DEFAULT_HEADER_SIZE = {COLUMN: "0.25in", ROW: "1in"}

# A TablixCorner sits before these Tablix children in Report Builder's
# element order (the XSD itself is an unordered choice).
_CORNER_FOLLOWED_BY = (
    "TablixBody",
    "TablixColumnHierarchy",
    "TablixRowHierarchy",
    "LayoutDirection",
    "GroupsBeforeRowHeaders",
    "RepeatColumnHeaders",
    "RepeatRowHeaders",
    "FixedColumnHeaders",
    "FixedRowHeaders",
    "DataSetName",
)

# TablixMember children that follow <TablixHeader> in Report Builder's order.
_MEMBER_CHILDREN_AFTER_HEADER = (
    "Visibility",
    "HideIfNoRows",
    "KeepTogether",
    "DataElementName",
    "DataElementOutput",
    "TablixMembers",
)


def _check_axis(axis: str) -> None:
    if axis not in _AXES:
        raise ValueError(f"axis must be 'column' or 'row'; got {axis!r}")


def _level_tag(axis: str) -> str:
    # A column header covering several header *rows* spans with RowSpan;
    # a row header covering several header *columns* with ColSpan.
    return spans.ROW_SPAN if axis == COLUMN else spans.COL_SPAN


# ---- textbox builder -------------------------------------------------------


def new_textbox(name: str, value: str = "") -> etree._Element:
    """A ``<Textbox>`` shaped like the ones Report Builder writes into
    tablix cells (CanGrow, KeepTogether, one paragraph / run,
    ``rd:DefaultName``, Style). ``value`` is a literal or ``=expression``."""
    tb = etree.Element(q("Textbox"), Name=name)
    etree.SubElement(tb, q("CanGrow")).text = "true"
    etree.SubElement(tb, q("KeepTogether")).text = "true"
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    paragraph = etree.SubElement(paragraphs, q("Paragraph"))
    textruns = etree.SubElement(paragraph, q("TextRuns"))
    textrun = etree.SubElement(textruns, q("TextRun"))
    etree.SubElement(textrun, q("Value")).text = encode_text(value)
    etree.SubElement(textrun, q("Style"))
    etree.SubElement(paragraph, q("Style"))
    etree.SubElement(tb, qrd("DefaultName")).text = name
    etree.SubElement(tb, q("Style"))
    return tb


def new_header(size: str, textbox_name: str, value: str = "") -> etree._Element:
    """A ``<TablixHeader>`` with ``<Size>`` and a textbox ``CellContents``."""
    return header_around(size, new_textbox(textbox_name, value))


def header_around(size: str, item: etree._Element) -> etree._Element:
    """A ``<TablixHeader>`` with ``<Size>`` whose ``CellContents`` holds
    ``item`` — an existing report item (typically a textbox moved out of
    a body cell, keeping its name and style)."""
    header = etree.Element(q("TablixHeader"))
    etree.SubElement(header, q("Size")).text = size
    contents = etree.SubElement(header, q("CellContents"))
    if item.getparent() is not None:
        item.getparent().remove(item)
    item.tail = None
    contents.append(item)
    return header


def insert_member_header(member: etree._Element, header: etree._Element) -> None:
    """Place ``header`` in ``member`` at Report Builder's slot (after
    Group / SortExpressions, before Visibility ... TablixMembers),
    replacing an existing ``<TablixHeader>``."""
    existing = find_child(member, "TablixHeader")
    if existing is not None:
        member.replace(existing, header)
        return
    for child in member:
        if isinstance(child.tag, str) and etree.QName(child).localname in (
            _MEMBER_CHILDREN_AFTER_HEADER
        ):
            child.addprevious(header)
            return
    member.append(header)


# ---- header levels ---------------------------------------------------------


def _hierarchy(tablix: etree._Element, axis: str) -> Optional[etree._Element]:
    _check_axis(axis)
    local = "TablixColumnHierarchy" if axis == COLUMN else "TablixRowHierarchy"
    return find_child(tablix, local)


def _leaves(tablix: etree._Element, axis: str) -> list[etree._Element]:
    return column_leaves(tablix) if axis == COLUMN else row_leaves(tablix)


def member_levels(member: etree._Element, axis: str) -> int:
    """Header levels ``member``'s own ``<TablixHeader>`` covers (0 if none)."""
    header = find_child(member, "TablixHeader")
    if header is None:
        return 0
    return spans.span(header, _level_tag(axis))


def header_levels(tablix: etree._Element, axis: str) -> list[int]:
    """Header levels along the path to each leaf of the ``axis`` hierarchy,
    in leaf (= body column / row) order."""
    hierarchy = _hierarchy(tablix, axis)
    out: list[int] = []
    for leaf in _leaves(tablix, axis):
        total = 0
        node: Optional[etree._Element] = leaf
        while node is not None and node is not hierarchy:
            if node.tag == q("TablixMember"):
                total += member_levels(node, axis)
            node = node.getparent()
        out.append(total)
    return out


def header_depth(tablix: etree._Element, axis: str) -> int:
    """Number of header rows (``axis='column'``) or header columns
    (``axis='row'``): the deepest path's header levels, 0 when none."""
    return max(header_levels(tablix, axis), default=0)


def _first_header_size(tablix: etree._Element, axis: str) -> str:
    hierarchy = _hierarchy(tablix, axis)
    if hierarchy is not None:
        for header in hierarchy.iter(q("TablixHeader")):
            size = find_child(header, "Size")
            if size is not None and (size.text or "").strip():
                return size.text.strip()
    return DEFAULT_HEADER_SIZE[axis]


def pad_header_levels(
    tablix: etree._Element,
    axis: str,
    registry: Any,
    name_base: Optional[str] = None,
) -> list[str]:
    """Give every ``axis`` leaf whose path is shallower than the deepest
    one the missing header levels, so the header grid stays rectangular.

    A leaf with its own ``<TablixHeader>`` has that header's span grown;
    a leaf without one gets a blank header (named from ``name_base``,
    default ``<tablix>_Header``, through ``registry``) spanning the
    missing levels. Returns the textbox names created.
    """
    levels = header_levels(tablix, axis)
    depth = max(levels, default=0)
    created: list[str] = []
    if not levels or min(levels) == depth:
        return created
    base = name_base or f"{tablix.get('Name')}_Header"
    tag = _level_tag(axis)
    size = _first_header_size(tablix, axis)
    for leaf, level in zip(_leaves(tablix, axis), levels):
        missing = depth - level
        if missing <= 0:
            continue
        header = find_child(leaf, "TablixHeader")
        if header is not None:
            spans.set_span(header, tag, spans.span(header, tag) + missing)
            continue
        name = registry.unique(base)
        header = new_header(size, name)
        if missing > 1:
            spans.set_span(header, tag, missing)
        insert_member_header(leaf, header)
        created.append(name)
    return created


# ---- TablixCorner ----------------------------------------------------------


def _corner_rows(corner: etree._Element) -> tuple[etree._Element, list[etree._Element]]:
    rows_root = find_child(corner, "TablixCornerRows")
    if rows_root is None:
        rows_root = etree.SubElement(corner, q("TablixCornerRows"))
    return rows_root, find_children(rows_root, "TablixCornerRow")


def corner_shape(tablix: etree._Element) -> Optional[tuple[int, list[int]]]:
    """``(rows, cells per row)`` of the existing corner, or ``None``."""
    corner = find_child(tablix, "TablixCorner")
    if corner is None:
        return None
    rows_root = find_child(corner, "TablixCornerRows")
    rows = find_children(rows_root, "TablixCornerRow") if rows_root is not None else []
    return len(rows), [len(find_children(r, "TablixCornerCell")) for r in rows]


def _remove_corner_row(rows: list[etree._Element], k: int) -> list[str]:
    """Drop corner row ``k`` keeping RowSpan bookkeeping: spans reaching
    into it from above shrink; a spanning owner in it moves down a row."""
    removed: list[str] = []
    for i, row in enumerate(rows[:k]):
        for cell in find_children(row, "TablixCornerCell"):
            rs = spans.span(cell, spans.ROW_SPAN)
            if not spans.is_placeholder(cell) and i + rs - 1 >= k:
                spans.set_span(cell, spans.ROW_SPAN, rs - 1)
    below = find_children(rows[k + 1], "TablixCornerCell") if k + 1 < len(rows) else []
    for j, cell in enumerate(find_children(rows[k], "TablixCornerCell")):
        rs = spans.span(cell, spans.ROW_SPAN)
        if not spans.is_placeholder(cell) and rs > 1 and j < len(below):
            target = below[j]
            tail = target.tail
            target.addprevious(cell)
            cell.tail = tail
            target.getparent().remove(target)
            spans.set_span(cell, spans.ROW_SPAN, rs - 1)
        else:
            removed.extend(spans.report_item_names(cell))
    spans.detach(rows[k])
    return removed


def _insert_corner(tablix: etree._Element, corner: etree._Element) -> None:
    for local in _CORNER_FOLLOWED_BY:
        anchor = find_child(tablix, local)
        if anchor is not None:
            anchor.addprevious(corner)
            return
    tablix.append(corner)


def ensure_corner(
    tablix: etree._Element,
    registry: Any,
    *,
    rows_at: str = "top",
    cols_at: str = "left",
    name_base: Optional[str] = None,
) -> dict[str, Any]:
    """Make ``tablix``'s ``<TablixCorner>`` exactly
    ``header_depth(column) × header_depth(row)``.

    * Either depth 0 → no corner: an existing one is removed.
    * No corner yet → one is created, every cell a blank textbox named
      ``<tablix>_Corner`` (suffixed through ``registry``; ``name_base``
      overrides the stem).
    * Otherwise rows are added or removed at ``rows_at`` (``"top"`` — a new
      outermost column group — or ``"bottom"``) and cells at ``cols_at``
      (``"left"`` / ``"right"``), keeping ColSpan / RowSpan owners and
      their placeholders consistent.

    Existing corner cells are never rewritten. Returns ``{rows, columns,
    created, removed}`` — the corner's shape and the textbox names
    created / deleted.
    """
    if rows_at not in ("top", "bottom") or cols_at not in ("left", "right"):
        raise ValueError("rows_at must be 'top'/'bottom' and cols_at 'left'/'right'")
    n_rows = header_depth(tablix, COLUMN)
    n_cols = header_depth(tablix, ROW)
    created: list[str] = []
    removed: list[str] = []
    result = {"rows": n_rows, "columns": n_cols, "created": created, "removed": removed}
    corner = find_child(tablix, "TablixCorner")

    if n_rows == 0 or n_cols == 0:
        if corner is not None:
            removed.extend(
                el.get("Name") for el in corner.iter() if el is not corner and el.get("Name")
            )
            spans.detach(corner)
        result["rows"] = result["columns"] = 0
        return result

    base = name_base or f"{tablix.get('Name')}_Corner"

    def make_cell() -> etree._Element:
        cell = etree.Element(q("TablixCornerCell"))
        name = registry.unique(base)
        etree.SubElement(cell, q("CellContents")).append(new_textbox(name))
        created.append(name)
        return cell

    def make_row() -> etree._Element:
        row = etree.Element(q("TablixCornerRow"))
        for _ in range(n_cols):
            row.append(make_cell())
        return row

    if corner is None:
        corner = etree.Element(q("TablixCorner"))
        rows_root = etree.SubElement(corner, q("TablixCornerRows"))
        for _ in range(n_rows):
            rows_root.append(make_row())
        _insert_corner(tablix, corner)
        return result

    rows_root, rows = _corner_rows(corner)
    # Width first (existing rows), then height — new rows are built at
    # the target width.
    for row in rows:
        while True:
            cells = find_children(row, "TablixCornerCell")
            if len(cells) == n_cols:
                break
            if len(cells) < n_cols:
                spans.insert_cell(
                    row, 0 if cols_at == "left" else len(cells), make_cell, "TablixCornerCell"
                )
            else:
                removed.extend(
                    spans.remove_cell(
                        row, 0 if cols_at == "left" else len(cells) - 1, "TablixCornerCell"
                    )
                )
    while len(rows) > n_rows:
        removed.extend(_remove_corner_row(rows, 0 if rows_at == "top" else len(rows) - 1))
        rows = find_children(rows_root, "TablixCornerRow")
    while len(rows) < n_rows:
        new_row = make_row()
        if rows and rows_at == "top":
            rows[0].addprevious(new_row)
        elif rows:
            rows[-1].addnext(new_row)
        else:
            rows_root.append(new_row)
        rows = find_children(rows_root, "TablixCornerRow")
    return result


# ---- shape checker ---------------------------------------------------------


class ShapeProblem(NamedTuple):
    """One finding of :func:`shape_findings`.

    * ``message`` — human-readable, as :func:`shape_problems` returns it.
    * ``path`` — where inside the tablix, as ``>``-joined steps with
      0-based indices (``TablixBody>TablixRow[2]>TablixCell[1]``,
      ``TablixCorner>TablixCornerRow[0]``, ``TablixColumnHierarchy``);
      ``""`` for the tablix itself.
    * ``severity`` — ``"error"`` (Report Builder rejects the file) or
      ``"warning"`` (loads, but not the shape Report Builder writes).
    """

    message: str
    path: str = ""
    severity: str = "error"


def _grid_problems(
    where: str,
    path: str,
    cells: list[etree._Element],
    width: int,
    body: bool,
    row_covered: frozenset = frozenset(),
) -> list[ShapeProblem]:
    """Problems of one cell row. ``row_covered`` holds the column indices
    a corner RowSpan from an earlier row covers."""
    problems: list[ShapeProblem] = []
    cell_tag = "TablixCell" if body else "TablixCornerCell"
    if len(cells) != width:
        problems.append(ShapeProblem(f"{where} has {len(cells)} cells; expected {width}", path))
    owners = spans.owner_map(cells)
    for j, cell in enumerate(cells):
        at = f"{path}>{cell_tag}[{j}]"
        if spans.is_placeholder(cell):
            if owners[j] == j and j not in row_covered:
                problems.append(
                    ShapeProblem(f"{where} cell {j} has no CellContents and no span covers it", at)
                )
            continue
        if owners[j] != j:
            problems.append(
                ShapeProblem(
                    f"{where} cell {j} is covered by the ColSpan of cell {owners[j]} "
                    "but has contents",
                    at,
                )
            )
            continue
        cs = spans.span(cell)
        if j + cs > width:
            problems.append(
                ShapeProblem(f"{where} cell {j} ColSpan={cs} overflows {width} column(s)", at)
            )
        if body and spans.span(cell, spans.ROW_SPAN) > 1:
            problems.append(
                ShapeProblem(f"{where} cell {j} has RowSpan>1 (only valid in TablixCorner)", at)
            )
    return problems


def shape_findings(tablix: etree._Element) -> list[ShapeProblem]:
    """Structural inconsistencies Report Builder rejects but the XSD
    cannot see, each with its location inside the tablix and a severity:

    * column-hierarchy leaves vs ``TablixColumn`` count, row-hierarchy
      leaves vs ``TablixRow`` count;
    * per body row: cell count vs columns, ColSpan overflow, covered cells
      with contents, empty cells no span covers, ``RowSpan>1``;
    * per axis: unequal header levels along the leaf paths;
    * corner: present without both header depths, row count vs
      column-header depth, cells per row vs row-header depth (span-aware,
      RowSpan included). A missing corner where both depths are non-zero
      is a ``warning``; everything else is an ``error``.

    Empty when clean. :func:`shape_problems` is the message-only view.
    """
    problems: list[ShapeProblem] = []
    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    rows_root = find_child(body, "TablixRows") if body is not None else None
    n_cols = len(find_children(cols_root, "TablixColumn")) if cols_root is not None else 0
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []

    n_col_leaves = len(column_leaves(tablix))
    if n_col_leaves != n_cols:
        problems.append(
            ShapeProblem(
                f"column hierarchy has {n_col_leaves} leaves but the body has {n_cols} columns",
                "TablixColumnHierarchy",
            )
        )
    n_row_leaves = len(row_leaves(tablix))
    if n_row_leaves != len(rows):
        problems.append(
            ShapeProblem(
                f"row hierarchy has {n_row_leaves} leaves but the body has {len(rows)} rows",
                "TablixRowHierarchy",
            )
        )

    for i, row in enumerate(rows):
        cells_root = find_child(row, "TablixCells")
        cells = find_children(cells_root, "TablixCell") if cells_root is not None else []
        problems.extend(
            _grid_problems(f"body row {i}", f"TablixBody>TablixRow[{i}]", cells, n_cols, body=True)
        )

    for axis in _AXES:
        levels = header_levels(tablix, axis)
        if len(set(levels)) > 1:
            problems.append(
                ShapeProblem(
                    f"{axis} hierarchy paths have unequal header levels {levels}",
                    "TablixColumnHierarchy" if axis == COLUMN else "TablixRowHierarchy",
                )
            )

    n_hdr_rows = header_depth(tablix, COLUMN)
    n_hdr_cols = header_depth(tablix, ROW)
    corner = find_child(tablix, "TablixCorner")
    if n_hdr_rows and n_hdr_cols:
        if corner is None:
            problems.append(
                ShapeProblem(
                    f"missing TablixCorner ({n_hdr_rows} header row(s) x "
                    f"{n_hdr_cols} header column(s))",
                    severity="warning",
                )
            )
        else:
            rows_root = find_child(corner, "TablixCornerRows")
            corner_rows = (
                find_children(rows_root, "TablixCornerRow") if rows_root is not None else []
            )
            if len(corner_rows) != n_hdr_rows:
                problems.append(
                    ShapeProblem(
                        f"TablixCorner has {len(corner_rows)} rows; expected {n_hdr_rows}",
                        "TablixCorner",
                    )
                )
            covered: dict[int, set] = {}
            for i, row in enumerate(corner_rows):
                cells = find_children(row, "TablixCornerCell")
                row_path = f"TablixCorner>TablixCornerRow[{i}]"
                problems.extend(
                    _grid_problems(
                        f"corner row {i}",
                        row_path,
                        cells,
                        n_hdr_cols,
                        body=False,
                        row_covered=frozenset(covered.get(i, ())),
                    )
                )
                for j, cell in enumerate(cells):
                    if spans.is_placeholder(cell):
                        continue
                    rs = spans.span(cell, spans.ROW_SPAN)
                    if i + rs > len(corner_rows):
                        problems.append(
                            ShapeProblem(
                                f"corner row {i} cell {j} RowSpan={rs} overflows "
                                f"{len(corner_rows)} row(s)",
                                f"{row_path}>TablixCornerCell[{j}]",
                            )
                        )
                    for k in range(i + 1, i + rs):
                        covered.setdefault(k, set()).update(range(j, j + spans.span(cell)))
    elif corner is not None:
        problems.append(
            ShapeProblem(
                "TablixCorner present but the tablix has "
                f"{n_hdr_rows} column-header row(s) x {n_hdr_cols} row-header column(s)",
                "TablixCorner",
            )
        )
    return problems


def shape_problems(tablix: etree._Element) -> list[str]:
    """Structural inconsistencies Report Builder rejects but the XSD
    cannot see: leaves vs body columns / rows, cells per row, ColSpan
    placeholders, header depths and the corner shape. Empty when clean.

    The messages of :func:`shape_findings` (warnings included), in order."""
    return [p.message for p in shape_findings(tablix)]


__all__ = [
    "COLUMN",
    "DEFAULT_HEADER_SIZE",
    "ROW",
    "ShapeProblem",
    "corner_shape",
    "ensure_corner",
    "header_around",
    "header_depth",
    "header_levels",
    "insert_member_header",
    "member_levels",
    "new_header",
    "new_textbox",
    "pad_header_levels",
    "shape_findings",
    "shape_problems",
]
