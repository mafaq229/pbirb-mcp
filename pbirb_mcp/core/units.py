"""Shared, unit-aware parser + validator for RDL size strings.

RDL ``SizeType`` values are a number followed by one of five unit
designators — ``in``, ``cm``, ``mm``, ``pt``, ``pc`` (``"2cm"``,
``"0.75in"``, ``"108pt"``). The bundled XSD types ``SizeType`` as a bare
``xsd:normalizedString``, so neither the schema validator nor Report
Builder's loader stops a tool from writing ``"abc"``, ``"12"`` (no unit),
``"1,5in"`` or ``"1e3in"`` — Report Builder only refuses the file when a
user opens it. Every size-taking tool therefore calls :func:`validate_size`
before it opens the document, so a bad argument never reaches disk.

Grammar (deliberately conservative — only shapes Report Builder itself
writes or reliably reads)::

    ^\\s*-?([0-9]+(\\.[0-9]+)?|\\.[0-9]+)\\s*(in|cm|mm|pt|pc)\\s*$

* ASCII digits and a decimal point only (``"1,5in"`` is rejected — RDL is culture-invariant).
* No exponent (``"1e3in"``), no leading ``+``, lower-case units only.
* A bare leading decimal (``".5in"``) is accepted: hand-edited RDL in the
  wild carries it and .NET's invariant-culture number parser reads it.
* Whitespace around / between number and unit is tolerated on input but
  stripped from the value written (``" 2 cm "`` -> ``"2cm"``). A value
  already in canonical form is returned **unchanged** — the number text
  is never re-rendered, so ``"0.750in"`` stays ``"0.750in"``.
* RDL caps every size at 160 inches; ``max_in`` enforces that on the
  absolute value.

Style sizes (``FontSize``, ``Padding*``, ``Border/Width``) are
``StringOrExpression`` in RDL; pass ``allow_expression=True`` so a value
starting with ``=`` is passed through untouched.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

UNITS = ("in", "cm", "mm", "pt", "pc")

# Conversion factors to inches.
TO_INCHES = {
    "in": 1.0,
    "cm": 1.0 / 2.54,
    "mm": 1.0 / 25.4,
    "pt": 1.0 / 72.0,
    "pc": 1.0 / 6.0,
}

# RDL's documented maximum for any size value.
MAX_SIZE_IN = 160.0

_SIZE_RE = re.compile(r"^\s*(-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))\s*(in|cm|mm|pt|pc)\s*$")

_EXPECTED = "expected '<number><unit>' with unit in (in, cm, mm, pt, pc), e.g. '1in' or '2.5cm'"


class Size(NamedTuple):
    value: float
    unit: str


def _match(s: object) -> Optional[re.Match[str]]:
    if not isinstance(s, str):
        return None
    return _SIZE_RE.match(s)


def parse_size(s: str) -> Size:
    """Parse an RDL size string into ``Size(value, unit)``.

    Raises :class:`ValueError` for anything outside the grammar in the
    module docstring (including non-strings and ``=`` expressions).
    """
    m = _match(s)
    if m is None:
        raise ValueError(f"invalid RDL size {s!r}; {_EXPECTED}")
    return Size(float(m.group(1)), m.group(2))


def to_inches(s: str) -> float:
    """Return the size ``s`` converted to inches."""
    size = parse_size(s)
    return size.value * TO_INCHES[size.unit]


def format_size(inches: float, unit: str = "in") -> str:
    """Render a length given in inches as an RDL size string in ``unit``.

    Up to 5 decimal places with trailing zeros stripped — Report
    Builder's convention for fractional values (``2.0`` -> ``"2in"``,
    ``0.984251..`` cm -> ``"2.5cm"``).
    """
    if unit not in TO_INCHES:
        raise ValueError(f"unknown RDL size unit {unit!r}; expected one of {UNITS}")
    text = f"{inches / TO_INCHES[unit]:.5f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text + unit


def validate_size(
    s: str,
    *,
    field: str,
    allow_negative: bool = False,
    allow_zero: bool = True,
    max_in: Optional[float] = MAX_SIZE_IN,
    allow_expression: bool = False,
) -> str:
    """Validate ``s`` as an RDL size for argument ``field``.

    Returns the value to write: ``s`` itself when it is already canonical,
    or the whitespace-stripped ``<number><unit>`` otherwise. Raises
    ``ValueError("<field>: invalid RDL size '<s>'; ...")`` on a malformed,
    negative (unless ``allow_negative``), zero (unless ``allow_zero``) or
    oversized (``abs(value) > max_in`` inches) value.

    With ``allow_expression=True`` a string starting with ``=`` (an RDL
    expression, legal for style sizes like ``FontSize``) is returned
    untouched.
    """
    if allow_expression and isinstance(s, str) and s.startswith("="):
        return s
    m = _match(s)
    if m is None:
        raise ValueError(f"{field}: invalid RDL size {s!r}; {_EXPECTED}")
    number, unit = m.group(1), m.group(2)
    value = float(number)
    if value < 0 and not allow_negative:
        raise ValueError(f"{field}: invalid RDL size {s!r}; must not be negative")
    if value == 0 and not allow_zero:
        raise ValueError(f"{field}: invalid RDL size {s!r}; must be greater than zero")
    if max_in is not None and abs(value) * TO_INCHES[unit] > max_in:
        raise ValueError(
            f"{field}: invalid RDL size {s!r}; exceeds the RDL maximum of {max_in:g}in"
        )
    canonical = number + unit
    return s if s == canonical else canonical


def validate_optional_size(s: Optional[str], *, field: str, **kwargs: object) -> Optional[str]:
    """:func:`validate_size` for optional arguments — ``None`` passes through."""
    if s is None:
        return None
    return validate_size(s, field=field, **kwargs)  # type: ignore[arg-type]


# ---- per-role presets ----------------------------------------------------
#
# Thin wrappers so each tool validates an argument in one short line and
# the per-role rules live in one place:
#
# * ``dimension`` — page / body / tablix / row / column / report-item
#   width or height: strictly positive.
# * ``position``  — Top / Left offsets and page margins: zero or positive.
# * ``style_size`` — FontSize / Padding* / Border Width: ``=`` expressions
#   pass untouched; literal values must be zero or positive
#   (``positive=True`` for FontSize, where zero is meaningless).


def dimension(s: Optional[str], field: str) -> Optional[str]:
    """Strictly positive size (``None`` passes through)."""
    return validate_optional_size(s, field=field, allow_zero=False)


def position(s: Optional[str], field: str) -> Optional[str]:
    """Non-negative size (``None`` passes through)."""
    return validate_optional_size(s, field=field)


def style_size(s: Optional[str], field: str, *, positive: bool = False) -> Optional[str]:
    """Non-negative style size or ``=`` expression (``None`` passes through)."""
    return validate_optional_size(s, field=field, allow_zero=not positive, allow_expression=True)


def box(top: str, left: str, width: str, height: str) -> tuple[str, str, str, str]:
    """Validate a new report item's placement in one call: ``top`` /
    ``left`` non-negative, ``width`` / ``height`` strictly positive.
    Returns the four values to write, in argument order."""
    return (
        validate_size(top, field="top"),
        validate_size(left, field="left"),
        validate_size(width, field="width", allow_zero=False),
        validate_size(height, field="height", allow_zero=False),
    )


__all__ = [
    "MAX_SIZE_IN",
    "Size",
    "TO_INCHES",
    "UNITS",
    "box",
    "dimension",
    "format_size",
    "parse_size",
    "position",
    "style_size",
    "to_inches",
    "validate_optional_size",
    "validate_size",
]
