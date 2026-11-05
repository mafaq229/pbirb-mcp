"""Validators for RDL style constants (colours and enum-valued styles).

The bundled XSD types every ``<Style>`` child as a plain string, so a
tool that writes ``Color=notacolor`` or ``TextAlign=Middle`` produces a
file that validates and lints clean — and that Report Builder rejects at
publish. Every style-writing tool runs its arguments through these
validators before it opens the document, so a bad value never reaches
disk.

Rules shared by every validator:

* ``None`` passes through (the argument was not supplied);
* a string starting with ``=`` is an RDL expression and passes through
  untouched (all of these styles are ``StringOrExpression``);
* anything else must be a string literal from the field's vocabulary,
  or a :class:`ValueError` naming the field and the valid values is
  raised.

Enum literals are matched case-insensitively and returned in their
canonical spelling (``"center"`` -> ``"Center"``), so what is written is
always a spelling Report Builder accepts. Colour literals are returned
unchanged: named colours are case-insensitive in Report Builder too.

Sizes (``FontSize``, ``Padding*``, ``Border/Width``) are validated by
:mod:`pbirb_mcp.core.units` (#22), not here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Optional

# The named colours Report Builder accepts: the .NET / HTML web colours
# plus Transparent (System colours such as "ControlText" are refused at
# publish, so they are not listed).
NAMED_COLORS: tuple[str, ...] = (
    "AliceBlue",
    "AntiqueWhite",
    "Aqua",
    "Aquamarine",
    "Azure",
    "Beige",
    "Bisque",
    "Black",
    "BlanchedAlmond",
    "Blue",
    "BlueViolet",
    "Brown",
    "BurlyWood",
    "CadetBlue",
    "Chartreuse",
    "Chocolate",
    "Coral",
    "CornflowerBlue",
    "Cornsilk",
    "Crimson",
    "Cyan",
    "DarkBlue",
    "DarkCyan",
    "DarkGoldenrod",
    "DarkGray",
    "DarkGreen",
    "DarkKhaki",
    "DarkMagenta",
    "DarkOliveGreen",
    "DarkOrange",
    "DarkOrchid",
    "DarkRed",
    "DarkSalmon",
    "DarkSeaGreen",
    "DarkSlateBlue",
    "DarkSlateGray",
    "DarkTurquoise",
    "DarkViolet",
    "DeepPink",
    "DeepSkyBlue",
    "DimGray",
    "DodgerBlue",
    "Firebrick",
    "FloralWhite",
    "ForestGreen",
    "Fuchsia",
    "Gainsboro",
    "GhostWhite",
    "Gold",
    "Goldenrod",
    "Gray",
    "Green",
    "GreenYellow",
    "Honeydew",
    "HotPink",
    "IndianRed",
    "Indigo",
    "Ivory",
    "Khaki",
    "Lavender",
    "LavenderBlush",
    "LawnGreen",
    "LemonChiffon",
    "LightBlue",
    "LightCoral",
    "LightCyan",
    "LightGoldenrodYellow",
    "LightGray",
    "LightGreen",
    "LightGrey",
    "LightPink",
    "LightSalmon",
    "LightSeaGreen",
    "LightSkyBlue",
    "LightSlateGray",
    "LightSteelBlue",
    "LightYellow",
    "Lime",
    "LimeGreen",
    "Linen",
    "Magenta",
    "Maroon",
    "MediumAquamarine",
    "MediumBlue",
    "MediumOrchid",
    "MediumPurple",
    "MediumSeaGreen",
    "MediumSlateBlue",
    "MediumSpringGreen",
    "MediumTurquoise",
    "MediumVioletRed",
    "MidnightBlue",
    "MintCream",
    "MistyRose",
    "Moccasin",
    "NavajoWhite",
    "Navy",
    "OldLace",
    "Olive",
    "OliveDrab",
    "Orange",
    "OrangeRed",
    "Orchid",
    "PaleGoldenrod",
    "PaleGreen",
    "PaleTurquoise",
    "PaleVioletRed",
    "PapayaWhip",
    "PeachPuff",
    "Peru",
    "Pink",
    "Plum",
    "PowderBlue",
    "Purple",
    "Red",
    "RosyBrown",
    "RoyalBlue",
    "SaddleBrown",
    "Salmon",
    "SandyBrown",
    "SeaGreen",
    "SeaShell",
    "Sienna",
    "Silver",
    "SkyBlue",
    "SlateBlue",
    "SlateGray",
    "Snow",
    "SpringGreen",
    "SteelBlue",
    "Tan",
    "Teal",
    "Thistle",
    "Tomato",
    "Transparent",
    "Turquoise",
    "Violet",
    "Wheat",
    "White",
    "WhiteSmoke",
    "Yellow",
    "YellowGreen",
)
_NAMED_COLORS_LOWER = frozenset(c.lower() for c in NAMED_COLORS)
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")

TEXT_ALIGN = ("General", "Left", "Center", "Right", "Justify")
VERTICAL_ALIGN = ("Top", "Middle", "Bottom")
WRITING_MODE = ("Horizontal", "Vertical", "Rotate270")
FONT_WEIGHT = (
    "Thin",
    "ExtraLight",
    "Light",
    "Normal",
    "Medium",
    "SemiBold",
    "Bold",
    "ExtraBold",
    "Heavy",
)
# Numeric weights, mapped to the names RDL 2008+ uses for them.
_NUMERIC_FONT_WEIGHTS = {str(n): name for n, name in zip(range(100, 1000, 100), FONT_WEIGHT)}
FONT_STYLE = ("Normal", "Italic")
TEXT_DECORATION = ("None", "Underline", "Overline", "LineThrough")
BORDER_STYLE = (
    "Default",
    "None",
    "Dotted",
    "Dashed",
    "Solid",
    "Double",
    "DashDot",
    "DashDotDot",
    "Groove",
    "Ridge",
    "Inset",
    "WindowInset",
    "Outset",
)


def _is_expression(value: object) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected a string; got {type(value).__name__}")
    return value


def color(value: Optional[str], field: str) -> Optional[str]:
    """Validate a colour: a named colour (case-insensitive), ``#RRGGBB``,
    ``#AARRGGBB`` or an ``=`` expression. Returned unchanged."""
    if value is None or _is_expression(value):
        return value
    text = _require_str(value, field)
    if _HEX_COLOR_RE.match(text) or text.lower() in _NAMED_COLORS_LOWER:
        return text
    raise ValueError(
        f"{field}: invalid color {value!r}; expected a named color (e.g. 'Red', "
        "'SteelBlue', 'Transparent'), '#RRGGBB', '#AARRGGBB' or an =expression"
    )


def enum(value: Optional[str], field: str, allowed: Sequence[str]) -> Optional[str]:
    """Validate ``value`` against ``allowed`` (case-insensitive) and return
    its canonical spelling; ``=`` expressions pass through."""
    if value is None or _is_expression(value):
        return value
    text = _require_str(value, field)
    for candidate in allowed:
        if candidate.lower() == text.lower():
            return candidate
    raise ValueError(
        f"{field}: invalid value {value!r}; expected one of {', '.join(allowed)} or an =expression"
    )


def text_align(value: Optional[str], field: str = "text_align") -> Optional[str]:
    return enum(value, field, TEXT_ALIGN)


def vertical_align(value: Optional[str], field: str = "vertical_align") -> Optional[str]:
    return enum(value, field, VERTICAL_ALIGN)


def writing_mode(value: Optional[str], field: str = "writing_mode") -> Optional[str]:
    return enum(value, field, WRITING_MODE)


def font_weight(value: Optional[str], field: str = "font_weight") -> Optional[str]:
    """A named weight (``Thin`` … ``Heavy``) or a multiple of 100 from
    100 to 900, written as its name (``700`` -> ``Bold``)."""
    if isinstance(value, str) and value.strip() in _NUMERIC_FONT_WEIGHTS:
        return _NUMERIC_FONT_WEIGHTS[value.strip()]
    try:
        return enum(value, field, FONT_WEIGHT)
    except ValueError:
        raise ValueError(
            f"{field}: invalid value {value!r}; expected one of {', '.join(FONT_WEIGHT)}, "
            "a multiple of 100 from 100 to 900, or an =expression"
        ) from None


def font_style(value: Optional[str], field: str = "font_style") -> Optional[str]:
    return enum(value, field, FONT_STYLE)


def text_decoration(value: Optional[str], field: str = "text_decoration") -> Optional[str]:
    return enum(value, field, TEXT_DECORATION)


def border_style(value: Optional[str], field: str = "border_style") -> Optional[str]:
    return enum(value, field, BORDER_STYLE)


# RDL style element (local name) -> validator, for callers that hold
# values keyed by RDL name (set_textbox_runs' ``style`` sub-dict).
BY_STYLE_LOCAL = {
    "Color": color,
    "BackgroundColor": color,
    "TextAlign": text_align,
    "VerticalAlign": vertical_align,
    "WritingMode": writing_mode,
    "FontWeight": font_weight,
    "FontStyle": font_style,
    "TextDecoration": text_decoration,
}


__all__ = [
    "BORDER_STYLE",
    "BY_STYLE_LOCAL",
    "FONT_STYLE",
    "FONT_WEIGHT",
    "NAMED_COLORS",
    "TEXT_ALIGN",
    "TEXT_DECORATION",
    "VERTICAL_ALIGN",
    "WRITING_MODE",
    "border_style",
    "color",
    "enum",
    "font_style",
    "font_weight",
    "text_align",
    "text_decoration",
    "vertical_align",
    "writing_mode",
]
