"""Textbox-styling tool.

Textboxes in RDL hold three nested ``<Style>`` blocks, and the right one
to write to depends on which property is being set:

- ``Textbox/Style``                                  — the BOX. BackgroundColor,
  Border (Style/Color/Width), VerticalAlign live here.
- ``Textbox/Paragraphs/Paragraph/Style``             — the PARAGRAPH. TextAlign
  belongs here. Putting it on Textbox/Style is silently ignored by Report
  Builder.
- ``Textbox/Paragraphs/Paragraph/TextRuns/TextRun/Style`` — the RUN. Font
  (Family/Size/Weight), Color (text), Format live here. The fixture's
  Amount cell already demonstrates this with its ``Format>#,0.00``.

Cell-level styling is intentionally NOT a separate tool. Every tablix
cell is a uniquely-named ``<Textbox>``, so addressing the textbox by name
already gives full per-cell control with no awkward "header column 0 has
a different name than data column 0" indirection.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import style_values, units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix, resolve_textbox
from pbirb_mcp.core.mutation import set_text_if_different
from pbirb_mcp.core.tablix_shape import NO_DETAIL_GROUP, data_row_index, detail_row_index
from pbirb_mcp.core.xpath import find_child, find_children, q

# Per RDL XSD, the order of <Style> children includes (subset relevant here):
#   Border, TopBorder, BottomBorder, LeftBorder, RightBorder,
#   BackgroundColor, BackgroundGradientType, BackgroundImage,
#   FontStyle, FontFamily, FontSize, FontWeight, Format,
#   TextDecoration, TextAlign, VerticalAlign, Color, ...
# We only enforce ordering for the children we touch — others (if already
# present) keep their relative position because we never reflow them.
_STYLE_CHILD_ORDER = (
    "Border",
    "TopBorder",
    "BottomBorder",
    "LeftBorder",
    "RightBorder",
    "BackgroundColor",
    "BackgroundGradientType",
    "BackgroundImage",
    "FontStyle",
    "FontFamily",
    "FontSize",
    "FontWeight",
    "Format",
    "TextDecoration",
    "TextAlign",
    "VerticalAlign",
    "Color",
    "PaddingLeft",
    "PaddingRight",
    "PaddingTop",
    "PaddingBottom",
    "WritingMode",
)


# Per RDL XSD, Textbox-direct children appear in this order (subset
# relevant to the v0.3 styling extensions): CanGrow / CanShrink / KeepTogether
# come BEFORE Paragraphs / Style. We touch only the two booleans here; the
# helper enforces the position so the writer doesn't drift round-trip
# byte-identity.
_TEXTBOX_DIRECT_CHILD_ORDER = (
    "CanGrow",
    "CanShrink",
    "KeepTogether",
    "HideDuplicates",
    "ToggleImage",
    "DataElementName",
    "DataElementOutput",
    "DataElementStyle",
    "Paragraphs",
    "rd:DefaultName",
    "Top",
    "Left",
    "Height",
    "Width",
    "ZIndex",
    "Visibility",
    "ToolTip",
    "DocumentMapLabel",
    "Bookmark",
    "RepeatWith",
    "CustomProperties",
    "Action",
    "Style",
)


def _set_or_create_textbox_direct_child(textbox: etree._Element, local: str, value: str) -> bool:
    """Set ``<local>value</local>`` as a direct child of ``Textbox``,
    respecting the RDL XSD child order. Returns True iff the value
    actually changed (used by ``changed: list[str]`` callers).
    """
    encoded = encode_text(value)
    existing = find_child(textbox, local)
    if existing is not None:
        if existing.text == encoded:
            return False
        existing.text = encoded
        return True
    new_node = etree.Element(q(local))
    new_node.text = encoded
    if local in _TEXTBOX_DIRECT_CHILD_ORDER:
        new_idx = _TEXTBOX_DIRECT_CHILD_ORDER.index(local)
        for i, child in enumerate(list(textbox)):
            child_local = etree.QName(child).localname
            qualified = (
                f"rd:{child_local}"
                if child.tag.startswith("{http://schemas.microsoft.com/SQLServer/")
                else child_local
            )
            check_local = qualified if qualified in _TEXTBOX_DIRECT_CHILD_ORDER else child_local
            if (
                check_local in _TEXTBOX_DIRECT_CHILD_ORDER
                and _TEXTBOX_DIRECT_CHILD_ORDER.index(check_local) > new_idx
            ):
                textbox.insert(i, new_node)
                return True
    textbox.append(new_node)
    return True


def _ensure_style(parent: etree._Element) -> etree._Element:
    """Return ``parent``'s ``<Style>`` child, creating it (as the *last*
    child) if absent."""
    style = find_child(parent, "Style")
    if style is None:
        style = etree.SubElement(parent, q("Style"))
    return style


def _set_or_create_in_style(style: etree._Element, local: str, value: str) -> bool:
    """Write ``<local>`` into ``style`` in XSD order; ``True`` iff changed."""
    return set_text_if_different(style, local, encode_text(value), order=_STYLE_CHILD_ORDER)


def _ensure_border(style: etree._Element) -> etree._Element:
    border = find_child(style, "Border")
    if border is not None:
        return border
    border = etree.Element(q("Border"))
    # Border is the first child per the order table above.
    style.insert(0, border)
    return border


def _resolve_paragraph_run(
    textbox: etree._Element,
) -> tuple[etree._Element, etree._Element]:
    """Find (or create) the first ``<Paragraph>`` and its first
    ``<TextRun>`` inside ``textbox``. PBI Report Builder always emits both
    even for empty textboxes; the create branch is defensive."""
    paragraphs = find_child(textbox, "Paragraphs")
    if paragraphs is None:
        paragraphs = etree.SubElement(textbox, q("Paragraphs"))
    paragraph = find_child(paragraphs, "Paragraph")
    if paragraph is None:
        paragraph = etree.SubElement(paragraphs, q("Paragraph"))
    textruns = find_child(paragraph, "TextRuns")
    if textruns is None:
        # TextRuns must precede Paragraph/Style.
        textruns = etree.Element(q("TextRuns"))
        paragraph.insert(0, textruns)
    textrun = find_child(textruns, "TextRun")
    if textrun is None:
        textrun = etree.SubElement(textruns, q("TextRun"))
    return paragraph, textrun


# ---- public tool ----------------------------------------------------------


def _size(field: str, *, positive: bool = False) -> Callable[[Any], Any]:
    return lambda v: units.style_size(v, field, positive=positive)


def _check(validator: Callable[..., Any], field: str) -> Callable[[Any], Any]:
    return lambda v: validator(v, field)


def _boolean(field: str) -> Callable[[Any], Any]:
    def check(v: Any) -> Any:
        if v is not None and not isinstance(v, bool):
            raise ValueError(f"{field}: expected true or false; got {v!r}")
        return v

    return check


def _string(field: str) -> Callable[[Any], Any]:
    def check(v: Any) -> Any:
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{field}: expected a string; got {type(v).__name__}")
        return v

    return check


# Every style kwarg the three style tools (set_textbox_style,
# set_textbox_style_bulk, style_tablix_row) accept:
#   kwarg -> (level, RDL local name, validator)
# ``level`` is where the property lives (see the module docstring):
#   textbox   direct Textbox child (CanGrow / CanShrink), NOT inside Style
#   box       Textbox/Style
#   border    Textbox/Style/Border
#   paragraph every Paragraph/Style
#   run       every TextRun/Style
# The validators run before the document is opened (sizes via #22's
# core/units.py, colours and enums via core/style_values.py); each passes
# ``=`` expressions through untouched. Within a level, table order is the
# order entries appear in ``changed``.
_STYLE_FIELDS: dict[str, tuple[str, str, Callable[[Any], Any]]] = {
    "can_grow": ("textbox", "CanGrow", _boolean("can_grow")),
    "can_shrink": ("textbox", "CanShrink", _boolean("can_shrink")),
    "background_color": ("box", "BackgroundColor", _check(style_values.color, "background_color")),
    "vertical_align": (
        "box",
        "VerticalAlign",
        _check(style_values.vertical_align, "vertical_align"),
    ),
    "padding_top": ("box", "PaddingTop", _size("padding_top")),
    "padding_bottom": ("box", "PaddingBottom", _size("padding_bottom")),
    "padding_left": ("box", "PaddingLeft", _size("padding_left")),
    "padding_right": ("box", "PaddingRight", _size("padding_right")),
    "writing_mode": ("box", "WritingMode", _check(style_values.writing_mode, "writing_mode")),
    "border_style": ("border", "Style", _check(style_values.border_style, "border_style")),
    "border_color": ("border", "Color", _check(style_values.color, "border_color")),
    "border_width": ("border", "Width", _size("border_width")),
    "text_align": ("paragraph", "TextAlign", _check(style_values.text_align, "text_align")),
    "font_family": ("run", "FontFamily", _string("font_family")),
    "font_size": ("run", "FontSize", _size("font_size", positive=True)),
    "font_weight": ("run", "FontWeight", _check(style_values.font_weight, "font_weight")),
    "font_style": ("run", "FontStyle", _check(style_values.font_style, "font_style")),
    "color": ("run", "Color", _check(style_values.color, "color")),
    "format": ("run", "Format", _string("format")),
    "text_decoration": (
        "run",
        "TextDecoration",
        _check(style_values.text_decoration, "text_decoration"),
    ),
}
# The kwargs in signature order — also the order they are validated in,
# so the first bad argument (as written) is the one reported.
STYLE_KWARGS: tuple[str, ...] = (
    "font_family",
    "font_size",
    "font_weight",
    "font_style",
    "color",
    "text_decoration",
    "background_color",
    "border_style",
    "border_color",
    "border_width",
    "text_align",
    "vertical_align",
    "format",
    "padding_top",
    "padding_bottom",
    "padding_left",
    "padding_right",
    "writing_mode",
    "can_grow",
    "can_shrink",
)
assert set(STYLE_KWARGS) == set(_STYLE_FIELDS)
_LEVELS = ("textbox", "box", "border", "paragraph", "run")


def _style_props(kwargs: dict[str, Any], tool: str) -> dict[str, dict[str, Any]]:
    """Validate the style kwargs and group the supplied (non-None) ones
    by level: ``{level: {RDL local: value}}``. Raises ``TypeError`` for an
    unknown kwarg and ``ValueError`` for a bad value — before any file is
    opened."""
    unknown = sorted(set(kwargs) - set(_STYLE_FIELDS))
    if unknown:
        raise TypeError(
            f"{tool}() got unexpected style argument(s) {unknown}; valid: {list(STYLE_KWARGS)}"
        )
    clean = {kwarg: _STYLE_FIELDS[kwarg][2](kwargs.get(kwarg)) for kwarg in STYLE_KWARGS}
    props: dict[str, dict[str, Any]] = {level: {} for level in _LEVELS}
    for kwarg, (level, local, _validate) in _STYLE_FIELDS.items():
        if clean[kwarg] is not None:
            props[level][local] = clean[kwarg]
    return props


def _no_style(props: dict[str, dict[str, Any]]) -> bool:
    return not any(props.values())


def _note(changed: list[str], entry: str) -> None:
    if entry not in changed:
        changed.append(entry)


def _apply_textbox_style(textbox: etree._Element, props: dict[str, dict[str, Any]]) -> list[str]:
    """Write ``props`` (from :func:`_style_props`) onto ``textbox``, in
    memory, and return the changed sub-paths (``"box.PaddingTop"``,
    ``"run.Color"``, ...). Paragraph properties go on every
    ``<Paragraph>``, run properties on every ``<TextRun>`` of every
    paragraph, so a "Label: value" textbox restyles as a whole."""
    changed: list[str] = []

    # CanGrow / CanShrink: direct Textbox children (before <Paragraphs>).
    for local, value in props["textbox"].items():
        if _set_or_create_textbox_direct_child(textbox, local, "true" if value else "false"):
            _note(changed, f"textbox.{local}")

    if props["box"] or props["border"]:
        outer_style = _ensure_style(textbox)
        for local, value in props["box"].items():
            if _set_or_create_in_style(outer_style, local, value):
                _note(changed, f"box.{local}")
        if props["border"]:
            border = _ensure_border(outer_style)
            for local, value in props["border"].items():
                # Border children share a small fixed sub-order.
                if set_text_if_different(border, local, encode_text(value)):
                    _note(changed, f"border.{local}")

    if props["paragraph"] or props["run"]:
        # Bootstraps Paragraphs/Paragraph/TextRuns/TextRun when absent.
        _resolve_paragraph_run(textbox)
        for paragraph in find_children(find_child(textbox, "Paragraphs"), "Paragraph"):
            if props["paragraph"]:
                p_style = _ensure_style(paragraph)
                for local, value in props["paragraph"].items():
                    if _set_or_create_in_style(p_style, local, value):
                        _note(changed, f"paragraph.{local}")
            if props["run"]:
                for textrun in _textruns_of(paragraph):
                    r_style = _ensure_style(textrun)
                    for local, value in props["run"].items():
                        if _set_or_create_in_style(r_style, local, value):
                            _note(changed, f"run.{local}")
    return changed


def _style_textboxes(
    doc: RDLDocument, names: list[str], props: dict[str, dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """Apply ``props`` to each named textbox of ``doc`` (in memory).
    Returns ``(affected, skipped, changed)``: names that resolved, names
    that didn't, and the sorted union of changed sub-paths."""
    affected: list[str] = []
    skipped: list[str] = []
    union_changed: set[str] = set()
    for name in names:
        try:
            textbox = resolve_textbox(doc, name)
        except ElementNotFoundError:
            skipped.append(name)
            continue
        affected.append(name)
        union_changed.update(_apply_textbox_style(textbox, props))
    return affected, skipped, sorted(union_changed)


def set_textbox_style(
    path: str,
    textbox_name: str,
    *,
    font_family: Optional[str] = None,
    font_size: Optional[str] = None,
    font_weight: Optional[str] = None,
    font_style: Optional[str] = None,
    color: Optional[str] = None,
    text_decoration: Optional[str] = None,
    background_color: Optional[str] = None,
    border_style: Optional[str] = None,
    border_color: Optional[str] = None,
    border_width: Optional[str] = None,
    text_align: Optional[str] = None,
    vertical_align: Optional[str] = None,
    format: Optional[str] = None,  # noqa: A002 - tool-facing
    padding_top: Optional[str] = None,
    padding_bottom: Optional[str] = None,
    padding_left: Optional[str] = None,
    padding_right: Optional[str] = None,
    writing_mode: Optional[str] = None,
    can_grow: Optional[bool] = None,
    can_shrink: Optional[bool] = None,
) -> dict[str, Any]:
    """Set styling properties on a named Textbox.

    Property routing (per RDL XSD):
    - **Box-level** (``Textbox/Style``): ``background_color``, ``vertical_align``,
      ``padding_top|bottom|left|right``, ``writing_mode``.
    - **Border** (``Textbox/Style/Border``): ``border_style|color|width``.
    - **Paragraph** (every ``Paragraph/Style``): ``text_align``.
    - **Run** (every ``TextRun/Style`` of every paragraph):
      ``font_family|size|weight|style``, ``color``, ``format``,
      ``text_decoration``. To style one run of a multi-run textbox
      differently, use :func:`set_textbox_runs`.
    - **Direct Textbox children** (NOT inside Style): ``can_grow``, ``can_shrink``.

    Literal values are validated before the file is opened — sizes per
    #22 (core/units.py), colours and enum styles per
    :mod:`pbirb_mcp.core.style_values` (enums are written in their
    canonical spelling, ``"center"`` -> ``"Center"``). An ``=``
    expression passes through for every field. A bad value raises
    ``ValueError`` naming the field and the valid values.

    Returns ``{textbox, changed: list[str]}`` with prefixed sub-paths
    (``"box.PaddingTop"``, ``"textbox.CanGrow"``, etc.). All-None call
    is a no-op short-circuit.
    """
    kwargs = {k: v for k, v in locals().items() if k in _STYLE_FIELDS}
    props = _style_props(kwargs, "set_textbox_style")
    if _no_style(props):
        # No-op — leave the file bytes untouched so callers can defensively
        # call this with all-None args without forcing a save.
        return {"textbox": textbox_name, "changed": []}

    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)
    changed = _apply_textbox_style(textbox, props)
    if changed:
        doc.save()
    return {"textbox": textbox_name, "changed": changed}


# ---- alternating row color -----------------------------------------------


def set_alternating_row_color(
    path: str,
    tablix_name: str,
    color_a: str,
    color_b: str,
) -> dict[str, Any]:
    """Set a zebra-striping ``BackgroundColor`` expression on every cell
    in the tablix's detail row.

    The expression is::

        =IIf(RowNumber(Nothing) Mod 2, "<color_a>", "<color_b>")

    Odd rows get ``color_a``; even rows get ``color_b``. The expression is
    written verbatim per cell, replacing any existing BackgroundColor.
    Returns ``{tablix, row_index, expression, cells, changed}``.
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    detail_idx = detail_row_index(tablix)
    if detail_idx is None:
        raise ElementNotFoundError(
            f"tablix {tablix_name!r} has {NO_DETAIL_GROUP}; set_alternating_row_color requires one"
        )

    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    detail_row = rows[detail_idx]

    expression = f'=IIf(RowNumber(Nothing) Mod 2, "{color_a}", "{color_b}")'

    cells_root = find_child(detail_row, "TablixCells")
    if cells_root is None:
        return {"tablix": tablix_name, "row_index": detail_idx, "cells": [], "changed": False}
    cells_touched, changed = _set_row_background(cells_root, expression)
    if changed:
        doc.save()
    return {
        "tablix": tablix_name,
        "row_index": detail_idx,
        "expression": expression,
        "cells": cells_touched,
        "changed": changed,
    }


def _set_row_background(cells_root: etree._Element, expression: str) -> tuple[list[str], bool]:
    """Write ``BackgroundColor`` on every cell textbox of a row; returns
    the textbox names and whether any value changed."""
    cells_touched: list[str] = []
    changed = False
    for cell in find_children(cells_root, "TablixCell"):
        contents = find_child(cell, "CellContents")
        textbox = find_child(contents, "Textbox") if contents is not None else None
        if textbox is None:
            continue
        outer_style = _ensure_style(textbox)
        changed |= _set_or_create_in_style(outer_style, "BackgroundColor", expression)
        name = textbox.get("Name")
        if name:
            cells_touched.append(name)
    return cells_touched, changed


def _normalise_value_expression(expr: str) -> str:
    """Strip a leading ``=`` so we can use the expression inline inside a
    Switch(...) without doubling up. The ``set_conditional_row_color`` tool
    accepts either ``Fields!X.Value`` or ``=Fields!X.Value`` from the LLM
    and produces a single, well-formed Switch expression either way.
    """
    return expr[1:] if expr.startswith("=") else expr


def _build_conditional_color_switch(
    value_expression: str,
    color_map: dict[str, str],
    default_color: str,
    *,
    case_sensitive: bool,
) -> str:
    """Build a Switch(...) expression mapping ``value_expression`` outcomes
    to colors. The expression is the value an LLM might type directly:
    each row of the detail body picks up the right BackgroundColor at
    render time.
    """
    inner = _normalise_value_expression(value_expression)
    operand = inner if case_sensitive else f"UCase({inner})"

    arms: list[str] = []
    for raw_value, color in color_map.items():
        match_value = raw_value if case_sensitive else raw_value.upper()
        # RDL string literals use double quotes; embedded quotes are doubled.
        match_literal = '"' + match_value.replace('"', '""') + '"'
        color_literal = '"' + color.replace('"', '""') + '"'
        arms.append(f"{operand}={match_literal}, {color_literal}")
    # Final fallback arm — Switch with no match returns Nothing, which RDL
    # renders as no fill. Always emit a default so unmatched values pick up
    # default_color explicitly.
    default_literal = '"' + default_color.replace('"', '""') + '"'
    arms.append(f"True, {default_literal}")

    return "=Switch(" + ", ".join(arms) + ")"


def set_conditional_row_color(
    path: str,
    tablix_name: str,
    value_expression: str,
    color_map: dict[str, str],
    default_color: str = "Transparent",
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """Color every cell of a tablix's detail row based on the value of
    one of its fields.

    Builds a ``Switch(...)`` expression from ``color_map`` and writes it
    as the ``BackgroundColor`` style on every cell of the detail row —
    same surface as ``set_alternating_row_color``, but conditional on a
    field value rather than ``RowNumber Mod 2``.

    Args:
        value_expression: Field reference, e.g. ``"Fields!Status.Value"``
            (a leading ``=`` is accepted and stripped).
        color_map: Mapping of expected values to color strings, e.g.
            ``{"Red": "#FF0000", "Yellow": "#FFFF00"}``. Order is preserved
            in the generated Switch — the first matching arm wins.
        default_color: Fallback for values not in ``color_map``.
            Default ``"Transparent"`` (no fill).
        case_sensitive: When False (default), wraps the field reference in
            ``UCase(...)`` and uppercases the keys, so the comparison is
            case-insensitive.

    Returns the same shape as ``set_alternating_row_color``:
    ``{tablix, row_index, expression, cells, changed}``.
    """
    if not color_map:
        raise ValueError("color_map must contain at least one value→color entry")

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    detail_idx = detail_row_index(tablix)
    if detail_idx is None:
        raise ElementNotFoundError(
            f"tablix {tablix_name!r} has {NO_DETAIL_GROUP}; set_conditional_row_color requires one"
        )

    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    detail_row = rows[detail_idx]

    expression = _build_conditional_color_switch(
        value_expression=value_expression,
        color_map=color_map,
        default_color=default_color,
        case_sensitive=case_sensitive,
    )

    cells_root = find_child(detail_row, "TablixCells")
    if cells_root is None:
        return {
            "tablix": tablix_name,
            "row_index": detail_idx,
            "expression": expression,
            "cells": [],
            "changed": False,
        }
    cells_touched, changed = _set_row_background(cells_root, expression)
    if changed:
        doc.save()
    return {
        "tablix": tablix_name,
        "row_index": detail_idx,
        "expression": expression,
        "cells": cells_touched,
        "changed": changed,
    }


# ---- set_textbox_runs ----------------------------------------------------


# Run-level style fields callers can supply via the runs[] list, as flat
# snake_case kwargs or (the get_textbox shape) a ``style`` sub-dict keyed
# by the RDL local name. Mirrors reader.py::_RUN_STYLE_FIELDS so a
# get_textbox → set_textbox_runs round trip preserves the same fields.
_RUN_KWARG_TO_STYLE_LOCAL: tuple[tuple[str, str], ...] = (
    ("font_family", "FontFamily"),
    ("font_size", "FontSize"),
    ("font_weight", "FontWeight"),
    ("font_style", "FontStyle"),
    ("color", "Color"),
    ("format", "Format"),
    ("text_decoration", "TextDecoration"),
)
_RUN_STYLE_LOCALS = frozenset(local for _, local in _RUN_KWARG_TO_STYLE_LOCAL)
_RECOGNISED_RUN_KEYS = frozenset(
    ["text", "value", "style", "paragraph_index", "markup_type"]
    + [kwarg for kwarg, _ in _RUN_KWARG_TO_STYLE_LOCAL]
)
_MARKUP_TYPES = ("None", "HTML")


def _normalise_run_spec(i: int, run: Any) -> dict[str, Any]:
    """Validate one ``runs[]`` entry and return its canonical form::

        {"text": str, "paragraph_index": int,
         "style": {Local: str | None},       # only keys the caller passed
         "markup_type": str | None}           # key present only if passed

    Accepts both the flat write shape (``text`` + snake_case style kwargs)
    and the ``get_textbox`` read shape (``value`` + ``style`` sub-dict
    keyed by RDL local names). A ``None`` style value removes that field;
    an omitted one keeps whatever the existing run carries.
    """
    if not isinstance(run, dict):
        raise ValueError(f"runs[{i}] must be a dict; got {type(run).__name__}")
    unknown = set(run.keys()) - _RECOGNISED_RUN_KEYS
    if unknown:
        raise ValueError(
            f"runs[{i}] has unrecognised keys {sorted(unknown)!r}; "
            f"valid keys are {sorted(_RECOGNISED_RUN_KEYS)!r}"
        )
    if "text" not in run and "value" not in run:
        raise ValueError(
            f"runs[{i}] missing required key 'text' (or 'value', the key get_textbox returns)"
        )
    text = run.get("text") if "text" in run else run.get("value")
    if "text" in run and "value" in run and run["text"] != run["value"]:
        raise ValueError(f"runs[{i}] has both 'text' and 'value' with different contents")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ValueError(f"runs[{i}].text must be a string; got {type(text).__name__}")

    paragraph_index = run.get("paragraph_index", 0)
    if paragraph_index is None:
        paragraph_index = 0
    if (
        not isinstance(paragraph_index, int)
        or isinstance(paragraph_index, bool)
        or paragraph_index < 0
    ):
        raise ValueError(
            f"runs[{i}].paragraph_index must be an integer >= 0; got {paragraph_index!r}"
        )

    style: dict[str, Optional[str]] = {}
    raw_style = run.get("style")
    if raw_style is not None:
        if not isinstance(raw_style, dict):
            raise ValueError(f"runs[{i}].style must be a dict; got {type(raw_style).__name__}")
        bad = set(raw_style) - _RUN_STYLE_LOCALS
        if bad:
            raise ValueError(
                f"runs[{i}].style has unrecognised keys {sorted(bad)!r}; "
                f"valid keys are {sorted(_RUN_STYLE_LOCALS)!r}"
            )
        style.update(raw_style)
    for kwarg, local in _RUN_KWARG_TO_STYLE_LOCAL:
        if kwarg not in run:
            continue
        if local in style and style[local] != run[kwarg]:
            raise ValueError(
                f"runs[{i}] sets {local} twice with different values "
                f"({kwarg}={run[kwarg]!r}, style.{local}={style[local]!r})"
            )
        style[local] = run[kwarg]
    for local, v in style.items():
        if v is not None and not isinstance(v, str):
            raise ValueError(f"runs[{i}] {local} must be a string or null; got {v!r}")
    if style.get("FontSize") is not None:
        style["FontSize"] = units.style_size(
            style["FontSize"], f"runs[{i}].font_size", positive=True
        )
    for kwarg, local in _RUN_KWARG_TO_STYLE_LOCAL:
        validate = style_values.BY_STYLE_LOCAL.get(local)
        if validate is not None and style.get(local) is not None:
            style[local] = validate(style[local], f"runs[{i}].{kwarg}")

    spec: dict[str, Any] = {"text": text, "paragraph_index": paragraph_index, "style": style}
    if "markup_type" in run:
        markup = run["markup_type"]
        if markup is not None and markup not in _MARKUP_TYPES:
            raise ValueError(
                f"runs[{i}].markup_type must be one of {list(_MARKUP_TYPES)!r} or null; "
                f"got {markup!r}"
            )
        spec["markup_type"] = markup
    return spec


def _same_text(existing: Optional[str], wanted: str) -> bool:
    """True when ``wanted`` (caller text) already matches ``existing`` (DOM text).

    A raw match is checked first so a value read back verbatim by
    ``get_textbox`` is a no-op even when it happens to contain
    entity-shaped text that :func:`encode_text` would normalise.
    """
    existing = existing or ""
    return wanted == existing or encode_text(wanted) == existing


def _apply_run_spec(textrun: etree._Element, spec: dict[str, Any]) -> bool:
    """Edit ``textrun`` in place to match ``spec``. Children the spec does
    not mention (ToolTip, ActionInfo, Label, MarkupType, other style keys)
    are left alone. Returns True iff anything changed."""
    changed = False
    value = find_child(textrun, "Value")
    if value is None:
        value = etree.Element(q("Value"))
        textrun.insert(0, value)
        changed = True
    if not _same_text(value.text, spec["text"]):
        value.text = encode_text(spec["text"])
        changed = True

    if "markup_type" in spec:
        markup = find_child(textrun, "MarkupType")
        wanted = spec["markup_type"]
        if wanted is None:
            if markup is not None:
                _remove_keep_layout(markup)
                changed = True
        elif markup is None:
            markup = etree.Element(q("MarkupType"))
            markup.text = wanted
            run_style = find_child(textrun, "Style")
            if run_style is not None:
                run_style.addprevious(markup)
            else:
                textrun.append(markup)
            changed = True
        elif markup.text != wanted:
            markup.text = wanted
            changed = True

    style = find_child(textrun, "Style")
    for local, wanted in spec["style"].items():
        node = find_child(style, local) if style is not None else None
        if wanted is None:
            if node is not None:
                _remove_keep_layout(node)
                changed = True
            continue
        if node is not None and _same_text(node.text, wanted):
            continue
        if style is None:
            style = etree.SubElement(textrun, q("Style"))
        _set_or_create_in_style(style, local, wanted)
        changed = True
    return changed


def _new_textrun(spec: dict[str, Any]) -> etree._Element:
    """Build a fresh ``<TextRun>`` (Value [, MarkupType], Style) for a run
    with no existing counterpart. Always carries a ``<Style>`` child —
    Report Builder writes one even when empty."""
    textrun = etree.Element(q("TextRun"))
    value = etree.SubElement(textrun, q("Value"))
    value.text = encode_text(spec["text"])
    if spec.get("markup_type") is not None:
        etree.SubElement(textrun, q("MarkupType")).text = spec["markup_type"]
    style = etree.SubElement(textrun, q("Style"))
    for local in _STYLE_CHILD_ORDER:
        wanted = spec["style"].get(local)
        if wanted is not None:
            _set_or_create_in_style(style, local, wanted)
    return textrun


def _remove_keep_layout(el: etree._Element) -> None:
    """Remove ``el`` without breaking the parent's pretty-printing: lxml
    drops the element's tail with it, which for a last child is the
    indentation of the parent's closing tag — hand it to the previous
    sibling instead."""
    parent = el.getparent()
    prev = el.getprevious()
    if el.getnext() is None and prev is not None:
        prev.tail = el.tail
    parent.remove(el)


def _textruns_of(paragraph: etree._Element) -> list[etree._Element]:
    root = find_child(paragraph, "TextRuns")
    return find_children(root, "TextRun") if root is not None else []


def _new_paragraph_like(previous: Optional[etree._Element]) -> etree._Element:
    """A new ``<Paragraph>`` that inherits the paragraph-level properties
    (Style, ListStyle, ListLevel, indents, spacing) of ``previous`` — the
    same thing Report Builder does when Enter starts a new paragraph."""
    paragraph = etree.Element(q("Paragraph"))
    etree.SubElement(paragraph, q("TextRuns"))
    if previous is not None:
        for child in previous:
            if not isinstance(child.tag, str) or etree.QName(child).localname == "TextRuns":
                continue
            paragraph.append(copy.deepcopy(child))
    if find_child(paragraph, "Style") is None:
        etree.SubElement(paragraph, q("Style"))
    return paragraph


def set_textbox_runs(
    path: str,
    textbox_name: str,
    runs: list[dict[str, Any]],
    drop_extra_paragraphs: bool = False,
) -> dict[str, Any]:
    """Set a textbox's text runs, editing the existing tree in place.

    Use this when the textbox needs mixed styling within one display —
    e.g. *"**Asset(s):** value"* with bold prefix + regular value in a
    single textbox — or to rewrite a multi-paragraph textbox.

    Each entry in ``runs`` is a dict::

        {
          "text": str,                    # required (or "value")
          "paragraph_index": int,         # optional, default 0
          "font_family": str,             # optional
          "font_size": str,               # optional, RDL size like '11pt'
          "font_weight": str,             # optional ('Bold', 'Normal', ...)
          "font_style": str,              # optional ('Italic', 'Normal', ...)
          "color": str,                   # optional hex or named
          "format": str,                  # optional number/date format
          "text_decoration": str,         # optional ('Underline', etc.)
          "markup_type": "None" | "HTML", # optional
        }

    ``get_textbox(...)["runs"]`` entries — ``{value, style: {FontWeight,
    ...}, paragraph_index, markup_type?}`` — are accepted verbatim, so
    ``set_textbox_runs(p, n, get_textbox(p, n)["runs"])`` is a no-op.

    Preservation: run *i* of paragraph *j* is written onto the existing
    ``TextRun`` *i* of ``Paragraph`` *j* where one exists. Only the fields
    the caller passes change; ``MarkupType``, ``ToolTip``, ``ActionInfo``,
    ``Label`` and style keys not passed are kept (pass a style key as
    ``None`` to remove it). Paragraph-level properties (``TextAlign``,
    ``ListStyle``, ``LeftIndent``, spacing, ...) are never touched; a new
    paragraph inherits them from the one before it. Surplus runs in an
    addressed paragraph are removed.

    A textbox paragraph that holds runs but that ``runs`` does not address
    is refused rather than silently deleted; pass
    ``drop_extra_paragraphs=True`` to delete trailing unaddressed
    paragraphs (e.g. to collapse a two-paragraph textbox to one).

    The text and every style field route through encode_text so already-
    encoded entities don't double-encode.

    Returns ``{textbox, kind, runs: int, paragraphs: int, changed:
    list[str]}`` — ``changed`` is ``["Paragraphs"]`` when the subtree was
    edited, ``[]`` when input matched existing exactly (no save).

    Errors (all raised before the file is touched):
    - Empty ``runs`` list, non-dict entries, missing ``text``/``value``,
      unknown keys, bad ``paragraph_index`` / ``markup_type``.
    - A ``paragraph_index`` that would leave a gap after the existing
      paragraphs, or existing paragraphs with runs left unaddressed.
    """
    if not isinstance(runs, list) or not runs:
        raise ValueError("runs must be a non-empty list; supply at least one run entry")
    specs = [_normalise_run_spec(i, run) for i, run in enumerate(runs)]

    groups: dict[int, list[dict[str, Any]]] = {}
    for spec in specs:
        groups.setdefault(spec["paragraph_index"], []).append(spec)
    max_index = max(groups)

    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)
    paragraphs_root = find_child(textbox, "Paragraphs")
    existing = find_children(paragraphs_root, "Paragraph") if paragraphs_root is not None else []

    # ---- validate the paragraph mapping before touching the tree ----
    missing_new = [j for j in range(len(existing), max_index + 1) if j not in groups]
    if missing_new:
        raise ValueError(
            f"textbox {textbox_name!r} has {len(existing)} paragraph(s); "
            f"runs address paragraph_index {max_index} but give no runs for "
            f"paragraph(s) {missing_new}. New paragraphs must be contiguous."
        )
    to_drop: list[etree._Element] = []
    for j, paragraph in enumerate(existing):
        if j in groups or not _textruns_of(paragraph):
            continue
        if j > max_index and drop_extra_paragraphs:
            to_drop.append(paragraph)
            continue
        hint = (
            "pass runs with paragraph_index for every paragraph (get_textbox"
            "(...)['runs'] shows the current layout)"
        )
        if j > max_index:
            hint += ", or drop_extra_paragraphs=true to delete the unaddressed paragraphs"
        raise ValueError(
            f"textbox {textbox_name!r} has {len(existing)} paragraphs but runs do not "
            f"address paragraph {j}; refusing to delete it silently — {hint}."
        )

    # ---- edit in place ----
    changed = False
    if paragraphs_root is None:
        paragraphs_root = etree.Element(q("Paragraphs"))
        new_idx = _TEXTBOX_DIRECT_CHILD_ORDER.index("Paragraphs")
        for i, child in enumerate(list(textbox)):
            child_local = etree.QName(child).localname if isinstance(child.tag, str) else ""
            if (
                child_local in _TEXTBOX_DIRECT_CHILD_ORDER
                and _TEXTBOX_DIRECT_CHILD_ORDER.index(child_local) > new_idx
            ):
                textbox.insert(i, paragraphs_root)
                break
        else:
            textbox.append(paragraphs_root)
        changed = True

    paragraphs = list(existing)
    for j in sorted(groups):
        if j < len(paragraphs):
            paragraph = paragraphs[j]
        else:
            paragraph = _new_paragraph_like(paragraphs[-1] if paragraphs else None)
            if paragraphs:
                paragraphs[-1].addnext(paragraph)
            else:
                paragraphs_root.append(paragraph)
            paragraphs.append(paragraph)
            changed = True
        textruns_root = find_child(paragraph, "TextRuns")
        if textruns_root is None:
            textruns_root = etree.Element(q("TextRuns"))
            paragraph.insert(0, textruns_root)
            changed = True
        current = find_children(textruns_root, "TextRun")
        for i, spec in enumerate(groups[j]):
            if i < len(current):
                changed |= _apply_run_spec(current[i], spec)
            else:
                textruns_root.append(_new_textrun(spec))
                changed = True
        for surplus in current[len(groups[j]) :]:
            _remove_keep_layout(surplus)
            changed = True
    for paragraph in to_drop:
        _remove_keep_layout(paragraph)
        paragraphs.remove(paragraph)
        changed = True

    if changed:
        doc.save()
    return {
        "textbox": textbox_name,
        "kind": "Textbox",
        "runs": len(specs),
        "paragraphs": len(paragraphs),
        "changed": ["Paragraphs"] if changed else [],
    }


# ---- set_textbox_value ---------------------------------------------------


def set_textbox_value(
    path: str,
    textbox_name: str,
    value: str,
) -> dict[str, Any]:
    """Replace the **text content** of a single-run textbox.

    Use this for the everyday "change the textbox content" case — e.g.
    swapping a literal label, updating a stale ``=Parameters!Old.Value``
    expression to ``=Parameters!New.Value``, or replacing a broken
    aggregate expression with a literal placeholder. ``value`` accepts
    raw text or an ``=expression``; encoding is handled by ``encode_text``
    so already-encoded entities don't double-encode.

    Refuses with a redirect to :func:`set_textbox_runs` when the textbox
    has more than one ``<TextRun>`` — multi-run content needs the
    rich-text editor since this tool would otherwise silently flatten
    the run-level styling.

    Returns ``{textbox, kind, changed: bool}`` — False when ``value``
    matched the existing run text (no save).
    """
    encoded = encode_text(str(value))

    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)

    paragraphs = find_child(textbox, "Paragraphs")
    if paragraphs is None:
        # No content yet — bootstrap a fresh single-run shape via
        # _resolve_paragraph_run, then set the value.
        paragraph, textrun = _resolve_paragraph_run(textbox)
        value_node = find_child(textrun, "Value")
        if value_node is None:
            value_node = etree.SubElement(textrun, q("Value"))
        value_node.text = encoded
        # Make sure paragraph has its trailing empty <Style/> sibling for
        # round-trip parity with template-built textboxes.
        if find_child(paragraph, "Style") is None:
            etree.SubElement(paragraph, q("Style"))
        # And the run's empty Style child for the same reason.
        if find_child(textrun, "Style") is None:
            etree.SubElement(textrun, q("Style"))
        doc.save()
        return {
            "textbox": textbox_name,
            "kind": "Textbox",
            "changed": True,
        }

    paragraph_count = len(find_children(paragraphs, "Paragraph"))
    if paragraph_count > 1:
        raise ValueError(
            f"textbox {textbox_name!r} has {paragraph_count} paragraphs; "
            "set_textbox_value only edits a single-paragraph textbox. Use "
            "set_textbox_runs with runs for every paragraph (each carrying "
            "its paragraph_index — get_textbox(...)['runs'] shows the current "
            "layout) to keep every paragraph, or drop_extra_paragraphs=true "
            "to collapse it to one paragraph."
        )
    paragraph = find_child(paragraphs, "Paragraph")
    textruns_root = find_child(paragraph, "TextRuns") if paragraph is not None else None
    textruns = find_children(textruns_root, "TextRun") if textruns_root is not None else []
    if len(textruns) > 1:
        raise ValueError(
            f"textbox {textbox_name!r} has {len(textruns)} text runs; "
            "set_textbox_value only edits a single-run textbox. Use "
            "set_textbox_runs for multi-run content (runs keep their "
            "existing styles unless you override them)."
        )
    if not textruns:
        # Single empty paragraph — bootstrap a TextRun.
        _, textrun = _resolve_paragraph_run(textbox)
    else:
        textrun = textruns[0]

    value_node = find_child(textrun, "Value")
    if value_node is None:
        value_node = etree.SubElement(textrun, q("Value"))
    if value_node.text == encoded:
        return {
            "textbox": textbox_name,
            "kind": "Textbox",
            "changed": False,
        }
    value_node.text = encoded
    doc.save()
    return {
        "textbox": textbox_name,
        "kind": "Textbox",
        "changed": True,
    }


# ---- set_textbox_style_bulk ----------------------------------------------


def set_textbox_style_bulk(
    path: str,
    textbox_names: list[str],
    **style: Any,
) -> dict[str, Any]:
    """Apply the same style kwargs to every named textbox in one call.

    Same kwarg surface (:data:`STYLE_KWARGS`), routing and validation as
    :func:`set_textbox_style`; every value is checked before the file is
    opened. Missing names land in ``skipped`` rather than raising; the
    textboxes that resolve are styled and saved together.

    Returns ``{textboxes: [names], skipped: [names], changed: list[str]}``
    where ``changed`` is the **union** of sub-paths affected across all
    textboxes (e.g. ``["box.PaddingTop", "run.FontWeight"]``). Empty
    ``textbox_names`` and all-None kwargs both short-circuit to a no-op.
    """
    props = _style_props(style, "set_textbox_style_bulk")
    if not textbox_names:
        return {"textboxes": [], "skipped": [], "changed": []}
    if _no_style(props):
        return {"textboxes": list(textbox_names), "skipped": [], "changed": []}

    doc = RDLDocument.open(path)
    affected, skipped, changed = _style_textboxes(doc, list(textbox_names), props)
    if changed:
        doc.save()
    return {"textboxes": affected, "skipped": skipped, "changed": changed}


# ---- style_tablix_row ----------------------------------------------------


def _walk_row_hierarchy_leaves(
    tablix: etree._Element,
) -> list[tuple[etree._Element, int, str]]:
    """Walk ``<TablixRowHierarchy>`` depth-first and return leaf members
    with their body row index and position-in-parent.

    Returns ``[(leaf_member, body_row_index, position_in_parent), ...]``
    where ``position_in_parent`` is:
    - ``"header"`` if the leaf is the first child of its parent's
      ``<TablixMembers>`` (or the very top if no nesting);
    - ``"footer"`` if the leaf is the last child of its parent's
      ``<TablixMembers>`` (and the parent has more than one child);
    - ``"middle"`` otherwise.

    Body rows correspond 1:1 with leaf TablixMembers in DFS order, so
    the returned indexes match ``TablixBody/TablixRows/TablixRow``
    indexes.
    """
    rh = find_child(tablix, "TablixRowHierarchy")
    if rh is None:
        return []
    members_root = find_child(rh, "TablixMembers")
    if members_root is None:
        return []

    out: list[tuple[etree._Element, int, str]] = []
    counter = [0]

    def walk(member: etree._Element, position: str) -> None:
        children_root = find_child(member, "TablixMembers")
        children = list(children_root) if children_root is not None else []
        if not children:
            out.append((member, counter[0], position))
            counter[0] += 1
            return
        for i, sub in enumerate(children):
            if len(children) == 1:
                child_pos = "middle"
            elif i == 0:
                child_pos = "header"
            elif i == len(children) - 1:
                child_pos = "footer"
            else:
                child_pos = "middle"
            walk(sub, child_pos)

    top_members = list(members_root)
    for i, top in enumerate(top_members):
        if len(top_members) == 1:
            top_pos = "middle"
        elif i == 0:
            top_pos = "header"
        elif i == len(top_members) - 1:
            top_pos = "footer"
        else:
            top_pos = "middle"
        walk(top, top_pos)

    return out


def _first_header_row_index(tablix: etree._Element) -> Optional[int]:
    """Return the body row index of the first leaf with
    ``KeepWithGroup=After`` — the conventional column header row."""
    for leaf, idx, _position in _walk_row_hierarchy_leaves(tablix):
        kwg = find_child(leaf, "KeepWithGroup")
        if kwg is not None and kwg.text == "After":
            return idx
    return None


def _first_leaf_descendant(member: etree._Element) -> etree._Element:
    """Recurse into ``member`` until a leaf TablixMember is reached.
    A leaf has no nested ``<TablixMembers>``."""
    children_root = find_child(member, "TablixMembers")
    children = list(children_root) if children_root is not None else []
    if not children:
        return member
    return _first_leaf_descendant(children[0])


def _last_leaf_descendant(member: etree._Element) -> etree._Element:
    children_root = find_child(member, "TablixMembers")
    children = list(children_root) if children_root is not None else []
    if not children:
        return member
    return _last_leaf_descendant(children[-1])


def _group_row_index(
    tablix: etree._Element,
    group_name: str,
    position: str,
) -> Optional[int]:
    """Return the body row index of a named group's header or footer leaf.

    The header is the first leaf descendant of the group's
    ``<TablixMembers>``; the footer is the last leaf descendant.
    Returns None if the group isn't present, or if the position can't
    be resolved (e.g. footer requested but the group has only its
    header child).
    """
    rh = find_child(tablix, "TablixRowHierarchy")
    if rh is None:
        return None

    target_wrapper: Optional[etree._Element] = None
    for member in rh.iter(q("TablixMember")):
        g = find_child(member, "Group")
        if g is not None and g.get("Name") == group_name:
            target_wrapper = member
            break
    if target_wrapper is None:
        return None

    inner_members = find_child(target_wrapper, "TablixMembers")
    if inner_members is None:
        return None
    inner_children = list(inner_members)
    if not inner_children:
        return None

    if position == "header":
        target_leaf = _first_leaf_descendant(inner_children[0])
    elif position == "footer":
        # A "real" group footer is a placeholder TablixMember WITHOUT
        # a <Group> child — the shape add_subtotal_row(position="footer")
        # emits. The detail leaf carries a <Group> (no GroupExpressions); if it
        # happens to be the last child, that's NOT a footer. Likewise,
        # bare add_row_group leaves the group with [header, ...,
        # Details] and no real footer.
        last_child = inner_children[-1]
        last_leaf = _last_leaf_descendant(last_child)
        if find_child(last_leaf, "Group") is not None:
            return None
        target_leaf = last_leaf
    else:
        return None

    for leaf, idx, _pos in _walk_row_hierarchy_leaves(tablix):
        if leaf is target_leaf:
            return idx
    return None


def _resolve_row_index(tablix: etree._Element, row: object) -> int:
    """Resolve a ``row`` argument to a 0-based body row index.

    ``row`` accepts:
    - ``int`` — taken as the body row index directly; range-checked.
    - ``"header"`` — first leaf with ``KeepWithGroup=After`` (the
      conventional column header).
    - ``"details"`` — the detail leaf (a ``<Group>`` without
      ``<GroupExpressions>``, whatever its name); on a matrix with no
      detail group, the innermost dynamic row group's leaf.
    - ``"<group>_header"`` — header leaf of a named row group.
    - ``"<group>_footer"`` — footer leaf of a named row group (when
      present, e.g. after ``add_subtotal_row``).

    Raises ``IndexError`` for out-of-range integers,
    ``ElementNotFoundError`` for unresolvable string roles, and
    ``ValueError`` for malformed inputs.
    """
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []

    if isinstance(row, bool):
        # Guard: bool is an int subclass in Python; reject explicitly so
        # a stray True / False doesn't silently become row 1 / 0.
        raise TypeError("row must be int or str; bool is rejected as ambiguous")
    # tools/list publishes `row` as a string; a digit string is an index
    # (no role or group name is all digits).
    if isinstance(row, str) and row.isdigit():
        row = int(row)
    if isinstance(row, int):
        if row < 0 or row >= len(rows):
            raise IndexError(
                f"tablix {tablix.get('Name')!r} has {len(rows)} rows; index {row} is out of range"
            )
        return row
    if not isinstance(row, str):
        raise TypeError(f"row must be int or str; got {type(row).__name__}")

    if row == "details":
        idx = data_row_index(tablix)
        if idx is None:
            raise ElementNotFoundError(
                f"tablix {tablix.get('Name')!r} has {NO_DETAIL_GROUP} and "
                "no dynamic row group; no row with role 'details' to resolve. "
                "Use an integer row index instead."
            )
        return idx
    if row == "header":
        idx = _first_header_row_index(tablix)
        if idx is None:
            raise ElementNotFoundError(
                f"tablix {tablix.get('Name')!r} has no leaf with "
                "KeepWithGroup=After; no row with role 'header' to "
                "resolve. Use an integer row index instead."
            )
        return idx

    if row.endswith("_header") and len(row) > len("_header"):
        group = row[: -len("_header")]
        idx = _group_row_index(tablix, group, "header")
        if idx is None:
            raise ElementNotFoundError(
                f"tablix {tablix.get('Name')!r} has no header row for group {group!r}"
            )
        return idx
    if row.endswith("_footer") and len(row) > len("_footer"):
        group = row[: -len("_footer")]
        idx = _group_row_index(tablix, group, "footer")
        if idx is None:
            raise ElementNotFoundError(
                f"tablix {tablix.get('Name')!r} has no footer row for "
                f"group {group!r}; add one via add_subtotal_row first"
            )
        return idx

    raise ValueError(
        f"unknown row role {row!r}; expected an integer, 'header', "
        "'details', '<group>_header', or '<group>_footer'"
    )


def _row_cell_textbox_names(tablix: etree._Element, row_index: int) -> list[str]:
    """Return the textbox names of every cell in a body row, in column
    order. Cells without a Textbox child (rare) are skipped."""
    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    target_row = rows[row_index]
    cells_root = find_child(target_row, "TablixCells")
    cells = find_children(cells_root, "TablixCell") if cells_root is not None else []
    out: list[str] = []
    for cell in cells:
        tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
        if tb is not None and tb.get("Name") is not None:
            out.append(tb.get("Name"))
    return out


def style_tablix_row(
    path: str,
    tablix_name: str,
    row: object,
    **style: Any,
) -> dict[str, Any]:
    """Apply the same style kwargs to every cell in a tablix row in ONE call.

    ``row`` accepts:
    - **Integer**: 0-based body row index. Range-checked against
      ``<TablixBody>/<TablixRows>``.
    - **"header"**: first leaf with ``KeepWithGroup=After`` (the
      conventional column header row).
    - **"details"**: the row containing the detail group leaf (a
      ``<Group>`` without ``<GroupExpressions>`` — ``Details``,
      ``Details1``, ...); on a matrix (no detail group), the innermost
      dynamic row group's leaf row.
    - **"<group>_header"**: header row of a named row group (the group
      created by ``add_row_group("<group>", ...)``).
    - **"<group>_footer"**: footer row of a named row group (present
      after ``add_subtotal_row("<group>", position="footer", ...)``).

    Style kwargs, routing and validation are identical to
    :func:`set_textbox_style`; the writes share
    :func:`set_textbox_style_bulk`'s helpers.

    Returns ``{tablix, row, row_index, kind: 'TablixRow', cells:
    list[str], changed: list[str], skipped: list[str]}``.
    ``cells`` is the list of textbox names that resolved (column order);
    ``skipped`` lists names that didn't resolve (rare — e.g. malformed
    cells without a Textbox child).
    """
    props = _style_props(style, "style_tablix_row")
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    row_index = _resolve_row_index(tablix, row)
    cell_names = _row_cell_textbox_names(tablix, row_index)
    if _no_style(props):
        cells, skipped, changed = list(cell_names), [], []
    else:
        cells, skipped, changed = _style_textboxes(doc, cell_names, props)
        if changed:
            doc.save()
    return {
        "tablix": tablix_name,
        "row": row,
        "row_index": row_index,
        "kind": "TablixRow",
        "cells": cells,
        "changed": changed,
        "skipped": skipped,
    }


__all__ = [
    "STYLE_KWARGS",
    "set_alternating_row_color",
    "set_conditional_row_color",
    "set_textbox_runs",
    "set_textbox_style",
    "set_textbox_style_bulk",
    "set_textbox_value",
    "style_tablix_row",
]
