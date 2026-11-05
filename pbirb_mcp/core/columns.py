"""Address tablix columns and cells the way people describe them (#59).

People ask for "the Asset Name column" or "the cell showing AssetID", not
for ``FleetTable_AssetName``. This module maps those descriptions onto the
body-column index and cell textbox names the tools work with.

A **column spec** is one of:

* an ``int`` — the 0-based body-column index (a digit string counts too);
* a ``str`` — the name of a textbox in any body cell of the column (the
  long-standing handle);
* a ``dict`` with exactly one of ``index`` (int), ``header`` (the
  column's header text), ``field`` (a dataset field the column shows,
  ``AssetID`` or ``Fields!AssetID.Value``) or ``textbox`` (as the str
  form).

A **cell spec** is a dict holding a column spec's key plus optional
``tablix`` (may be left out when the report has one tablix), ``row``
(``"data"``, the default, or ``"header"``) and ``row_index`` (a body row,
overriding ``row``).

What a column is (:func:`describe_columns`):

* ``header_cell`` / ``header_text`` — the textbox in the column-hierarchy
  leaf's ``<TablixHeader>`` (matrix column headers, #29/#31) when it holds
  text, else the textbox in the nearest body *header row* above the data
  row (a row whose hierarchy leaf is static and outside every group;
  failing that, any static row above it), else the header of the nearest
  column group around the leaf. A header merged over several columns
  (ColSpan) heads each of them.
* ``data_cell`` — the textbox in the tablix's data row
  (:func:`pbirb_mcp.core.tablix_shape.data_row_index`: the detail row, or
  in a matrix the innermost group's row) — the cell that repeats per
  record.
* ``bound_fields`` — ``Fields!X`` referenced by the text-run values of
  the column's body cells (data cell first; the body header cell
  excluded) and of its column-hierarchy header.

Matching ``header`` is case- and whitespace-insensitive and tried in
tiers: exact text, then letters and digits only (``"Asset Name"`` matches
``AssetName``), then a containing header. ``field`` matches field names
case-insensitively, preferring a column showing only that field. A spec
matching several columns raises :class:`AmbiguousColumnError`; one matching
none raises :class:`ColumnNotFoundError`. Both list the columns
(``payload``) so the caller can retry.
"""

from __future__ import annotations

import functools
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional, Union

from lxml import etree

from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError
from pbirb_mcp.core.tablix_shape import (
    column_leaves,
    data_row_index,
    owner_member,
    row_leaves,
)
from pbirb_mcp.core.tablix_spans import owner_map
from pbirb_mcp.core.xpath import find_child, find_children, q
from pbirb_mcp.ops import vbexpr
from pbirb_mcp.ops.expr_walk import expression_tokens, references

ColumnSpec = Union[int, str, dict]

COLUMN_KEYS = ("index", "header", "field", "textbox")
CELL_ROWS = ("data", "header")
_CELL_EXTRA_KEYS = ("tablix", "row", "row_index")

_VALUE_PATH = f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"


class ColumnNotFoundError(ElementNotFoundError):
    """No column of the tablix matches the spec. ``payload["columns"]``
    lists every column ({index, header_text, data_cell, bound_fields})."""

    def __init__(self, message: str, columns: list[dict[str, Any]]):
        super().__init__(message)
        self.payload = {"columns": columns}


class AmbiguousColumnError(AmbiguousElementError):
    """The spec matches several columns. ``payload["candidates"]`` lists
    them ({index, header_text, data_cell, bound_fields})."""

    def __init__(self, message: str, candidates: list[dict[str, Any]]):
        super().__init__(message)
        self.payload = {"candidates": candidates}


@dataclass
class ColumnInfo:
    index: int
    header_text: Optional[str]
    header_cell: Optional[str]
    data_cell: Optional[str]
    bound_fields: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def brief(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "header_text": self.header_text,
            "data_cell": self.data_cell,
            "bound_fields": list(self.bound_fields),
        }


# ---- reading cells ----------------------------------------------------------


def _body_rows(tablix: etree._Element) -> list[etree._Element]:
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    return find_children(rows_root, "TablixRow") if rows_root is not None else []


def _body_column_count(tablix: etree._Element) -> int:
    body = find_child(tablix, "TablixBody")
    cols_root = find_child(body, "TablixColumns") if body is not None else None
    return len(find_children(cols_root, "TablixColumn")) if cols_root is not None else 0


def _row_cells(row: etree._Element) -> list[etree._Element]:
    cells_root = find_child(row, "TablixCells")
    return find_children(cells_root, "TablixCell") if cells_root is not None else []


def _cell_textbox(cell: etree._Element) -> Optional[etree._Element]:
    return cell.find(f"{q('CellContents')}/{q('Textbox')}")


def _textbox_at(row: etree._Element, column: int, owned_only: bool = False):
    """The textbox shown at ``column`` of ``row`` — the textbox of the cell
    whose span covers it. ``owned_only`` drops a cell spanning in from a
    column to the left."""
    cells = _row_cells(row)
    if column >= len(cells):
        return None
    owner = owner_map(cells)[column]
    if owned_only and owner != column:
        return None
    return _cell_textbox(cells[owner])


def _values(textbox: etree._Element) -> list[str]:
    return [v.text or "" for v in textbox.findall(_VALUE_PATH)]


def textbox_text(textbox: Optional[etree._Element]) -> Optional[str]:
    """What a textbox displays, for matching: its text-run values joined
    by spaces; a ``="literal"`` expression is decoded, any other
    expression returned as written. ``None`` without a textbox."""
    if textbox is None:
        return None
    parts = []
    for value in _values(textbox):
        tokens = expression_tokens(value)
        if tokens is not None and len(tokens) == 1 and tokens[0].kind == vbexpr.STRING:
            value = vbexpr.string_value(tokens[0])
        if value.strip():
            parts.append(value.strip())
    return " ".join(parts)


def _fields_of(textbox: Optional[etree._Element]) -> list[str]:
    out: list[str] = []
    if textbox is None:
        return out
    for value in _values(textbox):
        tokens = expression_tokens(value)
        if tokens is None:
            continue
        for ref in references(tokens, "Fields"):
            if ref.name not in out:
                out.append(ref.name)
    return out


def _outside_groups(member: etree._Element) -> bool:
    """True iff neither ``member`` nor any member above it has a Group."""
    while member is not None:
        if find_child(member, "Group") is not None:
            return False
        member = owner_member(member)
    return True


def _header_row_indices(tablix: etree._Element, data_row: Optional[int]) -> list[int]:
    """Body rows that can hold column headers, nearest the data row first:
    static rows above the data row that sit outside every group — or,
    when there are none (a header row wrapped inside a group), every
    static row above it. Row 0 of an all-static tablix with more than one
    row."""
    leaves = row_leaves(tablix)
    if data_row is None:
        return [0] if len(_body_rows(tablix)) > 1 else []
    above = list(range(min(data_row, len(leaves)) - 1, -1, -1))
    outside = [i for i in above if _outside_groups(leaves[i])]
    return outside or [i for i in above if find_child(leaves[i], "Group") is None]


def _leaf_header_textbox(leaf: etree._Element) -> Optional[etree._Element]:
    header = find_child(leaf, "TablixHeader")
    if header is None:
        return None
    return header.find(f"{q('CellContents')}/{q('Textbox')}")


def describe_columns(tablix: etree._Element) -> list[ColumnInfo]:
    """One :class:`ColumnInfo` per body column, in order."""
    rows = _body_rows(tablix)
    data_row = data_row_index(tablix)
    if data_row is not None and data_row >= len(rows):
        data_row = None
    header_rows = _header_row_indices(tablix, data_row)
    leaves = column_leaves(tablix)

    out: list[ColumnInfo] = []
    for col in range(_body_column_count(tablix)):
        candidates: list[etree._Element] = []
        leaf = leaves[col] if col < len(leaves) else None
        leaf_header = _leaf_header_textbox(leaf) if leaf is not None else None
        if leaf_header is not None:
            candidates.append(leaf_header)
        for r in header_rows:
            tb = _textbox_at(rows[r], col)
            if tb is not None:
                candidates.append(tb)
        # Last resort: the header of a column group around the leaf (a
        # matrix's dynamic column has no header of its own).
        ancestors = []
        member = owner_member(leaf) if leaf is not None else None
        while member is not None:
            tb = _leaf_header_textbox(member)
            if tb is not None:
                ancestors.append(tb)
            member = owner_member(member)
        candidates += ancestors
        header_tb = next((tb for tb in candidates if textbox_text(tb)), None)
        if header_tb is None and candidates:
            header_tb = candidates[0]

        data_tb = _textbox_at(rows[data_row], col) if data_row is not None else None

        bound: list[str] = []
        ordered = [data_row] if data_row is not None else []
        ordered += [r for r in range(len(rows)) if r != data_row]
        for r in ordered:
            tb = _textbox_at(rows[r], col, owned_only=True)
            if tb is header_tb:
                continue
            for name in _fields_of(tb):
                if name not in bound:
                    bound.append(name)
        hierarchy_header = leaf_header
        if hierarchy_header is None and any(tb is header_tb for tb in ancestors):
            hierarchy_header = header_tb
        for name in _fields_of(hierarchy_header):
            if name not in bound:
                bound.append(name)

        out.append(
            ColumnInfo(
                index=col,
                header_text=textbox_text(header_tb),
                header_cell=header_tb.get("Name") if header_tb is not None else None,
                data_cell=data_tb.get("Name") if data_tb is not None else None,
                bound_fields=bound,
            )
        )
    return out


# ---- resolving a column spec ---------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _alnum(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).casefold()


def _field_name(text: str) -> str:
    """``Fields!X.Value`` / ``=Fields!X.Value`` / ``[X]`` / ``X`` → ``x``."""
    s = text.strip().lstrip("=").strip()
    m = re.fullmatch(r"(?i)fields\s*!\s*(\w+)(?:\s*\.\s*value)?", s)
    if m:
        s = m.group(1)
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return s.casefold()


def _listing(cols: list[ColumnInfo]) -> str:
    parts = []
    for c in cols:
        fields = f" fields={','.join(c.bound_fields)}" if c.bound_fields else ""
        parts.append(f"[{c.index}] header={c.header_text!r} cell={c.data_cell!r}{fields}")
    return "; ".join(parts)


def _pick(
    tablix: etree._Element,
    cols: list[ColumnInfo],
    matches: list[ColumnInfo],
    what: str,
) -> int:
    name = tablix.get("Name")
    if len(matches) == 1:
        return matches[0].index
    if matches:
        raise AmbiguousColumnError(
            f"{what} matches {len(matches)} columns of tablix {name!r}: {_listing(matches)}. "
            'Pass a more specific spec, or {"index": n}.',
            [c.brief() for c in matches],
        )
    raise ColumnNotFoundError(
        f"no column of tablix {name!r} has {what}. Columns: {_listing(cols) or 'none'}",
        [c.brief() for c in cols],
    )


def _by_header(tablix: etree._Element, cols: list[ColumnInfo], text: str) -> int:
    want = _norm(text)
    want_alnum = _alnum(text)
    with_header = [c for c in cols if c.header_text]
    tiers = [
        [c for c in with_header if _norm(c.header_text) == want],
        [c for c in with_header if want_alnum and _alnum(c.header_text) == want_alnum],
        [c for c in with_header if want_alnum and want_alnum in _alnum(c.header_text)],
    ]
    matches = next((t for t in tiers if t), [])
    return _pick(tablix, cols, matches, f"header {text!r}")


def _by_field(tablix: etree._Element, cols: list[ColumnInfo], text: str) -> int:
    want = _field_name(text)
    matches = [c for c in cols if any(f.casefold() == want for f in c.bound_fields)]
    if len(matches) > 1:
        only = [c for c in matches if len(c.bound_fields) == 1]
        if len(only) == 1:
            matches = only
    return _pick(tablix, cols, matches, f"field {text!r}")


def _by_textbox(tablix: etree._Element, name: str) -> int:
    for row in _body_rows(tablix):
        for col_idx, cell in enumerate(_row_cells(row)):
            tb = _cell_textbox(cell)
            if tb is not None and tb.get("Name") == name:
                return col_idx
    raise ElementNotFoundError(
        f"no cell with textbox named {name!r} in tablix "
        f"{tablix.get('Name')!r}; cannot resolve column index"
    )


def _by_index(tablix: etree._Element, index: int) -> int:
    count = _body_column_count(tablix)
    if index < 0 or index >= count:
        raise IndexError(
            f"tablix {tablix.get('Name')!r} has {count} columns; index {index} is out of range"
        )
    return index


def _column_key(spec: dict, allowed_extra: tuple = ()) -> tuple[str, Any]:
    unknown = [k for k in spec if k not in COLUMN_KEYS and k not in allowed_extra]
    if unknown:
        raise ValueError(
            f"unknown key(s) {unknown!r} in column spec; use one of {list(COLUMN_KEYS)}"
        )
    keys = [k for k in COLUMN_KEYS if spec.get(k) is not None]
    if len(keys) != 1:
        raise ValueError(
            f"a column spec needs exactly one of {list(COLUMN_KEYS)}; got {keys or 'none'}"
        )
    return keys[0], spec[keys[0]]


def resolve_column(tablix: etree._Element, spec: ColumnSpec) -> int:
    """0-based body-column index of the column ``spec`` names (see the
    module docstring). Raises :class:`ColumnNotFoundError` /
    :class:`AmbiguousColumnError` for header/field specs,
    ``ElementNotFoundError`` for an unknown textbox, ``IndexError`` for an
    out-of-range index, ``ValueError`` / ``TypeError`` for a malformed
    spec."""
    if isinstance(spec, bool):
        raise TypeError("column spec must be an int, str or object; got bool")
    if isinstance(spec, int):
        return _by_index(tablix, spec)
    if isinstance(spec, str):
        # Textbox names can't be all digits, so a digit string is an index.
        return _by_index(tablix, int(spec)) if spec.isdigit() else _by_textbox(tablix, spec)
    if not isinstance(spec, dict):
        raise TypeError(f"column spec must be an int, str or object; got {type(spec).__name__}")
    key, value = _column_key(spec)
    if key == "index":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"column index must be an integer; got {value!r}")
        return _by_index(tablix, value)
    if not isinstance(value, str):
        raise TypeError(f"column {key} must be a string; got {value!r}")
    if key == "textbox":
        return _by_textbox(tablix, value)
    cols = describe_columns(tablix)
    if key == "header":
        return _by_header(tablix, cols, value)
    return _by_field(tablix, cols, value)


# ---- resolving a cell spec -----------------------------------------------------


def cell_textbox(
    tablix: etree._Element,
    column: int,
    row: Union[str, int, None] = "data",
) -> str:
    """Name of the textbox at body column ``column`` of ``row``:
    ``"data"`` (the data row), ``"header"`` (the column's header cell,
    see :func:`describe_columns`) or a 0-based body-row index."""
    name = tablix.get("Name")
    if isinstance(row, int) and not isinstance(row, bool):
        rows = _body_rows(tablix)
        if row < 0 or row >= len(rows):
            raise IndexError(f"tablix {name!r} has no row at index {row} (rows={len(rows)})")
        tb = _textbox_at(rows[row], column)
        if tb is None:
            raise ElementNotFoundError(
                f"row {row} of tablix {name!r} has no textbox at column {column}"
            )
        return tb.get("Name")
    row = row or "data"
    if row not in CELL_ROWS:
        raise ValueError(f"row must be one of {list(CELL_ROWS)} or a row index; got {row!r}")
    info = describe_columns(tablix)[column]
    found = info.data_cell if row == "data" else info.header_cell
    if found is None:
        raise ElementNotFoundError(
            f"column {column} of tablix {name!r} has no {row} cell textbox; "
            "pass row_index to pick a body row"
        )
    return found


def _resolve_tablix_for_cell(root: etree._Element, tablix_name: Optional[str]) -> etree._Element:
    tablixes = list(root.iter(q("Tablix")))
    if tablix_name is not None:
        matches = [t for t in tablixes if t.get("Name") == tablix_name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise AmbiguousElementError(f"Tablix {tablix_name!r} is ambiguous")
        raise ElementNotFoundError(
            f"Tablix {tablix_name!r} not found; tablixes: {[t.get('Name') for t in tablixes]}"
        )
    if len(tablixes) == 1:
        return tablixes[0]
    raise ValueError(
        "cell.tablix is required when the report has "
        f"{len(tablixes)} tablixes: {[t.get('Name') for t in tablixes]}"
    )


def resolve_cell(root: etree._Element, spec: dict) -> str:
    """Textbox name of the tablix cell a cell spec names (see the module
    docstring)."""
    if not isinstance(spec, dict):
        raise TypeError(f"cell must be an object; got {type(spec).__name__}")
    _column_key(spec, _CELL_EXTRA_KEYS)
    tablix = _resolve_tablix_for_cell(root, spec.get("tablix"))
    column = resolve_column(tablix, {k: v for k, v in spec.items() if k in COLUMN_KEYS})
    row_index = spec.get("row_index")
    row = row_index if row_index is not None else spec.get("row", "data")
    return cell_textbox(tablix, column, row)


def _open_root(path: str) -> etree._Element:
    from pbirb_mcp.core.document import RDLDocument

    return RDLDocument.open(path).root


def with_cell_target(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a ``textbox_name`` tool handler so it also accepts ``cell`` (a
    cell spec) in place of ``textbox_name``."""

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> Any:
        cell = kwargs.pop("cell", None)
        if cell is not None:
            if kwargs.get("textbox_name") is not None:
                raise ValueError("pass textbox_name or cell, not both")
            kwargs["textbox_name"] = resolve_cell(_open_root(kwargs["path"]), cell)
        elif kwargs.get("textbox_name") is None:
            raise ValueError(
                "textbox_name or cell is required (cell addresses a tablix cell by "
                'column header or field, e.g. {"tablix": "T", "header": "Amount"})'
            )
        return fn(**kwargs)

    return wrapper


def with_cells_target(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a ``textbox_names`` tool handler so it also accepts ``cells``
    (a list of cell specs), appended to ``textbox_names``."""

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> Any:
        cells = kwargs.pop("cells", None)
        names_ = list(kwargs.get("textbox_names") or [])
        if cells:
            root = _open_root(kwargs["path"])
            for spec in cells:
                name = resolve_cell(root, spec)
                if name not in names_:
                    names_.append(name)
        if not names_:
            raise ValueError("textbox_names or cells must name at least one textbox")
        kwargs["textbox_names"] = names_
        return fn(**kwargs)

    return wrapper


# ---- published schemas ---------------------------------------------------------

_COLUMN_PROPERTIES: dict = {
    "header": {
        "type": "string",
        "description": "The column's header text, e.g. 'Asset Name' (case/space-insensitive).",
    },
    "field": {
        "type": "string",
        "description": "A dataset field the column shows, e.g. 'AssetID'.",
    },
    "index": {"type": "integer", "minimum": 0, "description": "0-based body column."},
    "textbox": {"type": "string", "description": "Name of a textbox in any cell of the column."},
}

COLUMN_REF_SCHEMA: dict = {
    "type": "object",
    "description": (
        "Pick the column by exactly one of header / field / index / textbox "
        "(describe_report lists each tablix's column_details). Ambiguous or "
        "unmatched specs fail with the candidate columns."
    ),
    "properties": dict(_COLUMN_PROPERTIES),
    "additionalProperties": False,
}

CELL_SCHEMA: dict = {
    "type": "object",
    "description": (
        "Address a tablix cell instead of textbox_name: tablix (optional when "
        "the report has one) plus exactly one of header / field / index / "
        "textbox picking the column, and row ('data' — the repeating row, "
        "default — or 'header') or row_index."
    ),
    "properties": {
        "tablix": {"type": "string"},
        **_COLUMN_PROPERTIES,
        "row": {"type": "string", "enum": list(CELL_ROWS)},
        "row_index": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}


__all__ = [
    "CELL_SCHEMA",
    "COLUMN_REF_SCHEMA",
    "AmbiguousColumnError",
    "ColumnInfo",
    "ColumnNotFoundError",
    "cell_textbox",
    "describe_columns",
    "resolve_cell",
    "resolve_column",
    "textbox_text",
    "with_cell_target",
    "with_cells_target",
]
