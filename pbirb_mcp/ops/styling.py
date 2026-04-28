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
)


def _ensure_style(parent: etree._Element) -> etree._Element:
    """Return ``parent``'s ``<Style>`` child, creating it (as the *last*
    child) if absent."""
    style = find_child(parent, "Style")
    if style is None:
        style = etree.SubElement(parent, q("Style"))
    return style


def _set_or_create_in_style(style: etree._Element, local: str, value: str) -> None:
    existing = find_child(style, local)
    if existing is not None:
        existing.text = value
        return
    new_node = etree.Element(q(local))
    new_node.text = value
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
) -> dict[str, Any]:
    box_props = {
        "BackgroundColor": background_color,
        "VerticalAlign": vertical_align,
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

    nothing_to_do = (
        all(v is None for v in box_props.values())
        and all(v is None for v in paragraph_props.values())
        and all(v is None for v in run_props.values())
        and all(v is None for v in border_props.values())
    )
    if nothing_to_do:
        # No-op — leave the file bytes untouched so callers can defensively
        # call this with all-None args without forcing a save.
        return {"textbox": textbox_name, "changed": []}

    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)

    changed: list[str] = []

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
                existing = find_child(border, local)
                if existing is not None:
                    existing.text = value
                else:
                    new_node = etree.SubElement(border, q(local))
                    new_node.text = value
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


__all__ = ["set_alternating_row_color", "set_textbox_style"]
