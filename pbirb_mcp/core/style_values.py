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

# Lower-cased named colour -> its .NET ``System.Drawing.Color`` value as
# lower-case ``#rrggbb`` (``#aarrggbb`` for Transparent), used to compare
# colours by value (find_textboxes_by_style, #107).
_NAMED_COLOR_TABLE = (
    "aliceblue=#f0f8ff antiquewhite=#faebd7 aqua=#00ffff aquamarine=#7fffd4 "
    "azure=#f0ffff beige=#f5f5dc bisque=#ffe4c4 black=#000000 "
    "blanchedalmond=#ffebcd blue=#0000ff blueviolet=#8a2be2 brown=#a52a2a "
    "burlywood=#deb887 cadetblue=#5f9ea0 chartreuse=#7fff00 chocolate=#d2691e "
    "coral=#ff7f50 cornflowerblue=#6495ed cornsilk=#fff8dc crimson=#dc143c "
    "cyan=#00ffff darkblue=#00008b darkcyan=#008b8b darkgoldenrod=#b8860b "
    "darkgray=#a9a9a9 darkgreen=#006400 darkkhaki=#bdb76b darkmagenta=#8b008b "
    "darkolivegreen=#556b2f darkorange=#ff8c00 darkorchid=#9932cc darkred=#8b0000 "
    "darksalmon=#e9967a darkseagreen=#8fbc8b darkslateblue=#483d8b "
    "darkslategray=#2f4f4f darkturquoise=#00ced1 darkviolet=#9400d3 "
    "deeppink=#ff1493 deepskyblue=#00bfff dimgray=#696969 dodgerblue=#1e90ff "
    "firebrick=#b22222 floralwhite=#fffaf0 forestgreen=#228b22 fuchsia=#ff00ff "
    "gainsboro=#dcdcdc ghostwhite=#f8f8ff gold=#ffd700 goldenrod=#daa520 "
    "gray=#808080 green=#008000 greenyellow=#adff2f honeydew=#f0fff0 "
    "hotpink=#ff69b4 indianred=#cd5c5c indigo=#4b0082 ivory=#fffff0 khaki=#f0e68c "
    "lavender=#e6e6fa lavenderblush=#fff0f5 lawngreen=#7cfc00 lemonchiffon=#fffacd "
    "lightblue=#add8e6 lightcoral=#f08080 lightcyan=#e0ffff "
    "lightgoldenrodyellow=#fafad2 lightgray=#d3d3d3 lightgreen=#90ee90 "
    "lightgrey=#d3d3d3 lightpink=#ffb6c1 lightsalmon=#ffa07a lightseagreen=#20b2aa "
    "lightskyblue=#87cefa lightslategray=#778899 lightsteelblue=#b0c4de "
    "lightyellow=#ffffe0 lime=#00ff00 limegreen=#32cd32 linen=#faf0e6 "
    "magenta=#ff00ff maroon=#800000 mediumaquamarine=#66cdaa mediumblue=#0000cd "
    "mediumorchid=#ba55d3 mediumpurple=#9370db mediumseagreen=#3cb371 "
    "mediumslateblue=#7b68ee mediumspringgreen=#00fa9a mediumturquoise=#48d1cc "
    "mediumvioletred=#c71585 midnightblue=#191970 mintcream=#f5fffa "
    "mistyrose=#ffe4e1 moccasin=#ffe4b5 navajowhite=#ffdead navy=#000080 "
    "oldlace=#fdf5e6 olive=#808000 olivedrab=#6b8e23 orange=#ffa500 "
    "orangered=#ff4500 orchid=#da70d6 palegoldenrod=#eee8aa palegreen=#98fb98 "
    "paleturquoise=#afeeee palevioletred=#db7093 papayawhip=#ffefd5 "
    "peachpuff=#ffdab9 peru=#cd853f pink=#ffc0cb plum=#dda0dd powderblue=#b0e0e6 "
    "purple=#800080 red=#ff0000 rosybrown=#bc8f8f royalblue=#4169e1 "
    "saddlebrown=#8b4513 salmon=#fa8072 sandybrown=#f4a460 seagreen=#2e8b57 "
    "seashell=#fff5ee sienna=#a0522d silver=#c0c0c0 skyblue=#87ceeb "
    "slateblue=#6a5acd slategray=#708090 snow=#fffafa springgreen=#00ff7f "
    "steelblue=#4682b4 tan=#d2b48c teal=#008080 thistle=#d8bfd8 tomato=#ff6347 "
    "transparent=#00ffffff turquoise=#40e0d0 violet=#ee82ee wheat=#f5deb3 "
    "white=#ffffff whitesmoke=#f5f5f5 yellow=#ffff00 yellowgreen=#9acd32"
)
_NAMED_COLOR_HEX: dict[str, str] = dict(pair.split("=") for pair in _NAMED_COLOR_TABLE.split())


def normalize_color(value: str) -> str:
    """A colour's comparison key: named colours and hex literals map to
    lower-case ``#rrggbb`` (``#aarrggbb`` unless fully opaque), so
    ``White``, ``#FFFFFF``, ``#ffffff`` and ``#FFFFFFFF`` compare equal.
    Expressions and unknown text come back stripped, otherwise unchanged."""
    text = value.strip()
    if text.startswith("="):
        return text
    lowered = text.lower()
    if lowered in _NAMED_COLOR_HEX:
        return _NAMED_COLOR_HEX[lowered]
    if _HEX_COLOR_RE.match(text):
        if len(lowered) == 9 and lowered.startswith("#ff"):
            return "#" + lowered[3:]
        return lowered
    return text


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
    "normalize_color",
    "text_align",
    "text_decoration",
    "vertical_align",
    "writing_mode",
]
