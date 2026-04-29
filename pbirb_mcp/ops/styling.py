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

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_tablix, resolve_textbox
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


def _set_or_create_textbox_direct_child(
    textbox: etree._Element, local: str, value: str
) -> bool:
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


def _set_or_create_in_style(style: etree._Element, local: str, value: str) -> None:
    encoded = encode_text(value)
    existing = find_child(style, local)
    if existing is not None:
        existing.text = encoded
        return
    new_node = etree.Element(q(local))
    new_node.text = encoded
    if local in _STYLE_CHILD_ORDER:
        new_idx = _STYLE_CHILD_ORDER.index(local)
        for i, child in enumerate(list(style)):
            child_local = etree.QName(child).localname
            if (
                child_local in _STYLE_CHILD_ORDER
                and _STYLE_CHILD_ORDER.index(child_local) > new_idx
            ):
                style.insert(i, new_node)
                return
    style.append(new_node)


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


def set_textbox_style(
    path: str,
    textbox_name: str,
    *,
    font_family: Optional[str] = None,
    font_size: Optional[str] = None,
    font_weight: Optional[str] = None,
    color: Optional[str] = None,
    background_color: Optional[str] = None,
    border_style: Optional[str] = None,
    border_color: Optional[str] = None,
    border_width: Optional[str] = None,
    text_align: Optional[str] = None,
    vertical_align: Optional[str] = None,
    format: Optional[str] = None,
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
    - **Paragraph** (``Textbox/Paragraphs/Paragraph/Style``): ``text_align``.
    - **Run** (``Textbox/.../TextRun/Style``): ``font_family|size|weight``,
      ``color``, ``format``.
    - **Direct Textbox children** (NOT inside Style): ``can_grow``, ``can_shrink``.

    Returns ``{textbox, changed: list[str]}`` with prefixed sub-paths
    (``"box.PaddingTop"``, ``"textbox.CanGrow"``, etc.). All-None call
    is a no-op short-circuit.
    """
    box_props = {
        "BackgroundColor": background_color,
        "VerticalAlign": vertical_align,
        "PaddingTop": padding_top,
        "PaddingBottom": padding_bottom,
        "PaddingLeft": padding_left,
        "PaddingRight": padding_right,
        "WritingMode": writing_mode,
    }
    paragraph_props = {
        "TextAlign": text_align,
    }
    run_props = {
        "FontFamily": font_family,
        "FontSize": font_size,
        "FontWeight": font_weight,
        "Color": color,
        "Format": format,
    }
    border_props = {
        "Style": border_style,
        "Color": border_color,
        "Width": border_width,
    }
    direct_textbox_props: dict[str, Optional[bool]] = {
        "CanGrow": can_grow,
        "CanShrink": can_shrink,
    }

    nothing_to_do = (
        all(v is None for v in box_props.values())
        and all(v is None for v in paragraph_props.values())
        and all(v is None for v in run_props.values())
        and all(v is None for v in border_props.values())
        and all(v is None for v in direct_textbox_props.values())
    )
    if nothing_to_do:
        # No-op — leave the file bytes untouched so callers can defensively
        # call this with all-None args without forcing a save.
        return {"textbox": textbox_name, "changed": []}

    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)

    changed: list[str] = []

    # Direct Textbox children — CanGrow / CanShrink — written as direct
    # Textbox children, NOT inside Style. Per the RDL XSD they appear
    # before <Paragraphs>.
    for local, value in direct_textbox_props.items():
        if value is None:
            continue
        text_value = "true" if value else "false"
        if _set_or_create_textbox_direct_child(textbox, local, text_value):
            changed.append(f"textbox.{local}")

    # Box level + border (border lives inside Textbox/Style/Border).
    if any(v is not None for v in box_props.values()) or any(
        v is not None for v in border_props.values()
    ):
        outer_style = _ensure_style(textbox)
        for local, value in box_props.items():
            if value is None:
                continue
            _set_or_create_in_style(outer_style, local, value)
            changed.append(f"box.{local}")
        if any(v is not None for v in border_props.values()):
            border = _ensure_border(outer_style)
            for local, value in border_props.items():
                if value is None:
                    continue
                # Border children share a small fixed sub-order.
                encoded = encode_text(value)
                existing = find_child(border, local)
                if existing is not None:
                    existing.text = encoded
                else:
                    new_node = etree.SubElement(border, q(local))
                    new_node.text = encoded
                changed.append(f"border.{local}")

    # Paragraph + run levels.
    if any(v is not None for v in paragraph_props.values()) or any(
        v is not None for v in run_props.values()
    ):
        paragraph, textrun = _resolve_paragraph_run(textbox)
        if any(v is not None for v in paragraph_props.values()):
            p_style = _ensure_style(paragraph)
            for local, value in paragraph_props.items():
                if value is None:
                    continue
                _set_or_create_in_style(p_style, local, value)
                changed.append(f"paragraph.{local}")
        if any(v is not None for v in run_props.values()):
            r_style = _ensure_style(textrun)
            for local, value in run_props.items():
                if value is None:
                    continue
                _set_or_create_in_style(r_style, local, value)
                changed.append(f"run.{local}")

    if changed:
        doc.save()
    return {"textbox": textbox_name, "changed": changed}


# ---- alternating row color -----------------------------------------------


def _detail_row_index(tablix: etree._Element) -> Optional[int]:
    """Return the body-row index of the ``Details`` leaf in a depth-first
    walk of the row hierarchy. ``None`` if the tablix has no Details group.

    Body rows correspond 1:1 with leaf TablixMembers in document order, so
    counting leaves until we find the Details group yields the right
    ``TablixBody/TablixRows/TablixRow`` index — even after add_row_group
    nests the original hierarchy under a new outer group.
    """
    rh = find_child(tablix, "TablixRowHierarchy")
    if rh is None:
        return None
    members_root = find_child(rh, "TablixMembers")
    if members_root is None:
        return None

    counter = [0]
    found_at: list[int] = []

    def walk(member: etree._Element) -> None:
        children = find_child(member, "TablixMembers")
        leaves = list(children) if children is not None else []
        if not leaves:
            group = find_child(member, "Group")
            if group is not None and group.get("Name") == "Details":
                found_at.append(counter[0])
            counter[0] += 1
            return
        for sub in leaves:
            walk(sub)

    for m in list(members_root):
        walk(m)
    return found_at[0] if found_at else None


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
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    detail_idx = _detail_row_index(tablix)
    if detail_idx is None:
        raise ElementNotFoundError(
            f"tablix {tablix_name!r} has no Details group; alternating-row-color requires one"
        )

    body = find_child(tablix, "TablixBody")
    rows_root = find_child(body, "TablixRows") if body is not None else None
    rows = find_children(rows_root, "TablixRow") if rows_root is not None else []
    detail_row = rows[detail_idx]

    expression = f'=IIf(RowNumber(Nothing) Mod 2, "{color_a}", "{color_b}")'

    cells_root = find_child(detail_row, "TablixCells")
    if cells_root is None:
        return {"tablix": tablix_name, "row_index": detail_idx, "cells": []}
    cells_touched: list[str] = []

    for cell in find_children(cells_root, "TablixCell"):
        contents = find_child(cell, "CellContents")
        textbox = find_child(contents, "Textbox") if contents is not None else None
        if textbox is None:
            continue
        outer_style = _ensure_style(textbox)
        _set_or_create_in_style(outer_style, "BackgroundColor", expression)
        name = textbox.get("Name")
        if name:
            cells_touched.append(name)

    doc.save()
    return {
        "tablix": tablix_name,
        "row_index": detail_idx,
        "expression": expression,
        "cells": cells_touched,
    }


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
    as the ``BackgroundColor`` style on every cell of the Details row —
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
    ``{tablix, row_index, expression, cells}``.
    """
    if not color_map:
        raise ValueError("color_map must contain at least one value→color entry")

    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    detail_idx = _detail_row_index(tablix)
    if detail_idx is None:
        raise ElementNotFoundError(
            f"tablix {tablix_name!r} has no Details group; set_conditional_row_color requires one"
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
        }
    cells_touched: list[str] = []
    for cell in find_children(cells_root, "TablixCell"):
        contents = find_child(cell, "CellContents")
        textbox = find_child(contents, "Textbox") if contents is not None else None
        if textbox is None:
            continue
        outer_style = _ensure_style(textbox)
        _set_or_create_in_style(outer_style, "BackgroundColor", expression)
        name = textbox.get("Name")
        if name:
            cells_touched.append(name)

    doc.save()
    return {
        "tablix": tablix_name,
        "row_index": detail_idx,
        "expression": expression,
        "cells": cells_touched,
    }


# ---- set_textbox_runs ----------------------------------------------------


# Run-level style fields callers can supply via the runs[] list. Mirrors
# reader.py::_RUN_STYLE_FIELDS so a get_textbox → set_textbox_runs round
# trip preserves the same fields.
_RUN_KWARG_TO_STYLE_LOCAL: tuple[tuple[str, str], ...] = (
    ("font_family", "FontFamily"),
    ("font_size", "FontSize"),
    ("font_weight", "FontWeight"),
    ("font_style", "FontStyle"),
    ("color", "Color"),
    ("format", "Format"),
    ("text_decoration", "TextDecoration"),
)
_RECOGNISED_RUN_KEYS = frozenset(
    ["text"] + [kwarg for kwarg, _ in _RUN_KWARG_TO_STYLE_LOCAL]
)


def _runs_match_specs(
    paragraphs: etree._Element, specs: list[dict[str, Any]]
) -> bool:
    """Structural equality check: do the existing TextRuns under the
    first Paragraph match the supplied specs exactly?

    Compares the Value text and every recognised style field. Used by
    set_textbox_runs to short-circuit no-op writes without serialising
    to bytes (which is fooled by namespace-prefix drift).
    """
    paragraph = find_child(paragraphs, "Paragraph")
    if paragraph is None:
        return False
    textruns_root = find_child(paragraph, "TextRuns")
    if textruns_root is None:
        return False
    textruns = find_children(textruns_root, "TextRun")
    if len(textruns) != len(specs):
        return False
    for run_el, spec in zip(textruns, specs):
        value = find_child(run_el, "Value")
        existing_value = value.text if value is not None else None
        if existing_value != encode_text(str(spec["text"])):
            return False
        # Compare every recognised style field.
        style = find_child(run_el, "Style")
        for kwarg, local in _RUN_KWARG_TO_STYLE_LOCAL:
            spec_v = spec.get(kwarg)
            existing_v: Optional[str] = None
            if style is not None:
                node = find_child(style, local)
                if node is not None:
                    existing_v = node.text
            if spec_v is None:
                if existing_v is not None:
                    return False
            else:
                if existing_v != encode_text(str(spec_v)):
                    return False
    return True


def _build_run_style(run_spec: dict[str, str]) -> Optional[etree._Element]:
    """Build a ``<Style>`` element for a single run spec. Returns ``None``
    when no style fields are populated so callers can keep an empty
    ``<Style/>`` (Report Builder's emitted shape) by inserting their own
    placeholder."""
    style = etree.Element(q("Style"))
    has_any = False
    for kwarg, local in _RUN_KWARG_TO_STYLE_LOCAL:
        v = run_spec.get(kwarg)
        if v is None:
            continue
        node = etree.SubElement(style, q(local))
        node.text = encode_text(str(v))
        has_any = True
    return style if has_any else None


def set_textbox_runs(
    path: str,
    textbox_name: str,
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replace the textbox content with multiple ``<TextRun>`` children.

    Use this when the textbox needs mixed styling within one display —
    e.g. *"**Asset(s):** value"* with bold prefix + regular value in a
    single textbox. RDL renders the runs side-by-side inside one
    ``<Paragraph>``; for separate paragraphs, do a follow-up edit (this
    tool stays single-paragraph for v0.3 — multi-paragraph is deferred).

    Each entry in ``runs`` is a dict::

        {
          "text": str,                    # required
          "font_family": str,             # optional
          "font_size": str,               # optional, RDL size like '11pt'
          "font_weight": str,             # optional ('Bold', 'Normal', ...)
          "font_style": str,              # optional ('Italic', 'Normal', ...)
          "color": str,                   # optional hex or named
          "format": str,                  # optional number/date format
          "text_decoration": str,         # optional ('Underline', etc.)
        }

    The text and every style field route through encode_text so already-
    encoded entities don't double-encode.

    Round-trip contract: get_textbox.runs[] returns the same shape this
    tool writes (text + style sub-dict).

    Returns ``{textbox, kind, runs: int, changed: list[str]}`` —
    ``changed`` is ``["Paragraphs"]`` when the subtree was rewritten,
    ``[]`` when input matched existing exactly (no save).

    Errors:
    - Empty ``runs`` list: rejects with ValueError; use remove_body_item
      to drop the textbox or pass at least one run.
    - Non-dict run entries: rejects with ValueError.
    - Missing ``text`` key: rejects with ValueError.
    - Unknown keys in a run entry: rejects with ValueError listing the
      offending keys.
    """
    if not isinstance(runs, list) or not runs:
        raise ValueError(
            "runs must be a non-empty list; supply at least one run entry"
        )
    for i, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ValueError(f"runs[{i}] must be a dict; got {type(run).__name__}")
        if "text" not in run:
            raise ValueError(f"runs[{i}] missing required key 'text'")
        unknown = set(run.keys()) - _RECOGNISED_RUN_KEYS
        if unknown:
            raise ValueError(
                f"runs[{i}] has unrecognised keys {sorted(unknown)!r}; "
                f"valid keys are {sorted(_RECOGNISED_RUN_KEYS)!r}"
            )

    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)

    # Structural no-op check: compare the incoming run specs against
    # the textbox's existing runs before any DOM rewrite. Bytes-level
    # comparison is fooled by namespace-prefix and whitespace drift, so
    # walk the field set instead.
    existing = find_child(textbox, "Paragraphs")
    if existing is not None and _runs_match_specs(existing, runs):
        return {
            "textbox": textbox_name,
            "kind": "Textbox",
            "runs": len(runs),
            "changed": [],
        }

    # Build the new <Paragraphs> subtree from scratch.
    new_paragraphs = etree.Element(q("Paragraphs"))
    paragraph = etree.SubElement(new_paragraphs, q("Paragraph"))
    textruns = etree.SubElement(paragraph, q("TextRuns"))
    for run_spec in runs:
        textrun = etree.SubElement(textruns, q("TextRun"))
        value = etree.SubElement(textrun, q("Value"))
        value.text = encode_text(str(run_spec["text"]))
        run_style = _build_run_style(run_spec)
        # Always emit a <Style> child — Report Builder writes one even
        # when empty, so absence would drift round-trip byte-identity
        # for any textbox round-tripped through this tool.
        if run_style is None:
            etree.SubElement(textrun, q("Style"))
        else:
            textrun.append(run_style)
    # Empty <Style/> on the Paragraph itself (RB convention).
    etree.SubElement(paragraph, q("Style"))

    # Replace or insert the Paragraphs subtree, preserving sibling order.
    if existing is not None:
        textbox.replace(existing, new_paragraphs)
    else:
        # Find the insertion point: <Paragraphs> sits between the direct
        # boolean children (CanGrow/CanShrink/KeepTogether/...) and the
        # rd:DefaultName / Top / Left / etc. trailing fields. Use the
        # established _TEXTBOX_DIRECT_CHILD_ORDER lookup.
        new_idx = _TEXTBOX_DIRECT_CHILD_ORDER.index("Paragraphs")
        inserted = False
        for i, child in enumerate(list(textbox)):
            child_local = etree.QName(child).localname
            if (
                child_local in _TEXTBOX_DIRECT_CHILD_ORDER
                and _TEXTBOX_DIRECT_CHILD_ORDER.index(child_local) > new_idx
            ):
                textbox.insert(i, new_paragraphs)
                inserted = True
                break
        if not inserted:
            textbox.append(new_paragraphs)

    doc.save()
    return {
        "textbox": textbox_name,
        "kind": "Textbox",
        "runs": len(runs),
        "changed": ["Paragraphs"],
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
            "set_textbox_value only edits the first run of a single-paragraph "
            "textbox. Use set_textbox_runs for multi-run / multi-paragraph "
            "content."
        )
    paragraph = find_child(paragraphs, "Paragraph")
    textruns_root = find_child(paragraph, "TextRuns") if paragraph is not None else None
    textruns = find_children(textruns_root, "TextRun") if textruns_root is not None else []
    if len(textruns) > 1:
        raise ValueError(
            f"textbox {textbox_name!r} has {len(textruns)} text runs; "
            "set_textbox_value only edits a single-run textbox. Use "
            "set_textbox_runs for multi-run content."
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
    *,
    font_family: Optional[str] = None,
    font_size: Optional[str] = None,
    font_weight: Optional[str] = None,
    color: Optional[str] = None,
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
    """Apply the same style kwargs to every named textbox in one call.

    Same kwarg surface as :func:`set_textbox_style`. Iterates over each
    name; missing names land in ``skipped`` rather than raising. Atomic
    per-textbox: a successful textbox is saved even if a later one is
    missing — that mirrors how individual ``set_textbox_style`` calls
    would have behaved if invoked sequentially.

    Returns ``{textboxes: [names], skipped: [names], changed: list[str]}``
    where ``changed`` is the **union** of sub-paths affected across all
    textboxes (e.g. ``["box.PaddingTop", "run.FontWeight"]``). Empty
    ``textbox_names`` and all-None kwargs both short-circuit to a no-op.

    Designed for the recurring "style 21+ headers + 21 data + 21 totals
    cells the same way" loop that v0.2 forced into 63 individual tool
    calls. RAG-Report session feedback #6.
    """
    if not textbox_names:
        return {
            "textboxes": [],
            "skipped": [],
            "changed": [],
        }

    style_kwargs: dict[str, Any] = {
        "font_family": font_family,
        "font_size": font_size,
        "font_weight": font_weight,
        "color": color,
        "background_color": background_color,
        "border_style": border_style,
        "border_color": border_color,
        "border_width": border_width,
        "text_align": text_align,
        "vertical_align": vertical_align,
        "format": format,
        "padding_top": padding_top,
        "padding_bottom": padding_bottom,
        "padding_left": padding_left,
        "padding_right": padding_right,
        "writing_mode": writing_mode,
        "can_grow": can_grow,
        "can_shrink": can_shrink,
    }
    if all(v is None for v in style_kwargs.values()):
        return {
            "textboxes": list(textbox_names),
            "skipped": [],
            "changed": [],
        }

    affected: list[str] = []
    skipped: list[str] = []
    union_changed: set[str] = set()
    for name in textbox_names:
        try:
            result = set_textbox_style(path, name, **style_kwargs)
        except ElementNotFoundError:
            skipped.append(name)
            continue
        affected.append(name)
        for entry in result.get("changed", []):
            union_changed.add(entry)

    return {
        "textboxes": affected,
        "skipped": skipped,
        "changed": sorted(union_changed),
    }


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
        # emits. The Details leaf has <Group Name="Details">; if it
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
    - ``"details"`` — the leaf with ``Group Name="Details"``.
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
    if isinstance(row, int):
        if row < 0 or row >= len(rows):
            raise IndexError(
                f"tablix {tablix.get('Name')!r} has {len(rows)} rows; "
                f"index {row} is out of range"
            )
        return row
    if not isinstance(row, str):
        raise TypeError(
            f"row must be int or str; got {type(row).__name__}"
        )

    if row == "details":
        idx = _detail_row_index(tablix)
        if idx is None:
            raise ElementNotFoundError(
                f"tablix {tablix.get('Name')!r} has no Details group; "
                "no row with role 'details' to resolve"
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
                f"tablix {tablix.get('Name')!r} has no header row for "
                f"group {group!r}"
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
    *,
    font_family: Optional[str] = None,
    font_size: Optional[str] = None,
    font_weight: Optional[str] = None,
    color: Optional[str] = None,
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
    """Apply the same style kwargs to every cell in a tablix row in ONE call.

    Headline tool of v0.3 Phase 6 — replaces the recurring 12-cells-per-
    row × N-rows pattern that turned a single styling intent into 12+
    individual ``set_textbox_style`` invocations.

    ``row`` accepts:
    - **Integer**: 0-based body row index. Range-checked against
      ``<TablixBody>/<TablixRows>``.
    - **"header"**: first leaf with ``KeepWithGroup=After`` (the
      conventional column header row).
    - **"details"**: the row containing the ``Details`` group leaf.
    - **"<group>_header"**: header row of a named row group (the group
      created by ``add_row_group("<group>", ...)``).
    - **"<group>_footer"**: footer row of a named row group (present
      after ``add_subtotal_row("<group>", position="footer", ...)``).

    Style kwargs are identical to :func:`set_textbox_style`; this tool
    delegates the actual writes to :func:`set_textbox_style_bulk` so
    the encoding rules from Phase 0, the Style child-order from Phase 2,
    and the canonical ``{textboxes, changed, skipped}`` shape all
    transfer for free.

    Returns ``{tablix, row, row_index, kind: 'TablixRow', cells:
    list[str], changed: list[str], skipped: list[str]}``.
    ``cells`` is the list of textbox names that resolved (column order);
    ``skipped`` lists names that didn't resolve (rare — e.g. malformed
    cells without a Textbox child).
    """
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, tablix_name)
    row_index = _resolve_row_index(tablix, row)
    cell_names = _row_cell_textbox_names(tablix, row_index)
    # Don't save here — set_textbox_style_bulk handles the writes.
    # We just opened to discover names.

    bulk_result = set_textbox_style_bulk(
        path=path,
        textbox_names=cell_names,
        font_family=font_family,
        font_size=font_size,
        font_weight=font_weight,
        color=color,
        background_color=background_color,
        border_style=border_style,
        border_color=border_color,
        border_width=border_width,
        text_align=text_align,
        vertical_align=vertical_align,
        format=format,
        padding_top=padding_top,
        padding_bottom=padding_bottom,
        padding_left=padding_left,
        padding_right=padding_right,
        writing_mode=writing_mode,
        can_grow=can_grow,
        can_shrink=can_shrink,
    )

    return {
        "tablix": tablix_name,
        "row": row,
        "row_index": row_index,
        "kind": "TablixRow",
        "cells": bulk_result["textboxes"],
        "changed": bulk_result["changed"],
        "skipped": bulk_result["skipped"],
    }


__all__ = [
    "set_alternating_row_color",
    "set_conditional_row_color",
    "set_textbox_runs",
    "set_textbox_style",
    "set_textbox_style_bulk",
    "set_textbox_value",
    "style_tablix_row",
]
