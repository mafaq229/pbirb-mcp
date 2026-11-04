"""Tests for the shared RDL size parser / validator (issue #22)."""

from __future__ import annotations

import pytest

from pbirb_mcp.core import units
from pbirb_mcp.core.units import (
    Size,
    format_size,
    parse_size,
    to_inches,
    validate_optional_size,
    validate_size,
)

# (input, parsed value, parsed unit)
_VALID = [
    ("2cm", 2.0, "cm"),
    ("0.75in", 0.75, "in"),
    ("108pt", 108.0, "pt"),
    ("10mm", 10.0, "mm"),
    ("1pc", 1.0, "pc"),
    ("0in", 0.0, "in"),
    ("0.750in", 0.75, "in"),
    (".5in", 0.5, "in"),
    ("-1.25in", -1.25, "in"),
    (" 2 cm ", 2.0, "cm"),
    ("160in", 160.0, "in"),
]

_MALFORMED = [
    "",
    "   ",
    "abc",
    "banana",
    "12",  # no unit
    "in",  # no number
    "1,5in",  # culture-specific decimal comma
    "1e3in",  # exponent
    "+1in",  # explicit plus
    "1.in",  # dangling decimal point
    "1IN",  # units are lower-case
    "1px",  # not an RDL unit
    "1in2",
    "1 in 2",
    "=1in",  # expression (only style sizes allow these)
    "١in",  # non-ASCII digit
]


class TestParseSize:
    @pytest.mark.parametrize("text,value,unit", _VALID)
    def test_valid(self, text, value, unit):
        assert parse_size(text) == Size(value, unit)

    @pytest.mark.parametrize("text", _MALFORMED + [None, 5, 1.5])
    def test_malformed(self, text):
        with pytest.raises(ValueError, match="invalid RDL size"):
            parse_size(text)


class TestToInchesAndFormat:
    @pytest.mark.parametrize(
        "text,inches",
        [
            ("1in", 1.0),
            ("2.54cm", 1.0),
            ("25.4mm", 1.0),
            ("72pt", 1.0),
            ("6pc", 1.0),
            ("-0.5in", -0.5),
        ],
    )
    def test_to_inches(self, text, inches):
        assert to_inches(text) == pytest.approx(inches)

    @pytest.mark.parametrize(
        "inches,unit,expected",
        [
            (2.0, "in", "2in"),
            (0.75, "in", "0.75in"),
            (1.0, "cm", "2.54cm"),
            (2.5 / 2.54, "cm", "2.5cm"),
            (1.5, "pt", "108pt"),
            (-0.5, "in", "-0.5in"),
            (-1e-9, "in", "0in"),
            (1 / 3, "in", "0.33333in"),
        ],
    )
    def test_format_size(self, inches, unit, expected):
        assert format_size(inches, unit) == expected

    def test_format_size_unknown_unit(self):
        with pytest.raises(ValueError, match="unknown RDL size unit"):
            format_size(1.0, "px")

    @pytest.mark.parametrize("text", ["2cm", "0.75in", "108pt", "10mm", "1pc", "-3.5in"])
    def test_round_trip(self, text):
        assert format_size(to_inches(text), parse_size(text).unit) == text


class TestValidateSize:
    @pytest.mark.parametrize("text", ["2cm", "0.75in", "108pt", "10mm", "1pc", "0.750in", ".5in"])
    def test_canonical_values_returned_unchanged(self, text):
        assert validate_size(text, field="width") is text

    def test_whitespace_stripped(self):
        assert validate_size(" 2 cm ", field="width") == "2cm"

    @pytest.mark.parametrize("text", _MALFORMED + [None, 5])
    def test_malformed_names_field_and_value(self, text):
        with pytest.raises(ValueError) as exc:
            validate_size(text, field="page_width")
        msg = str(exc.value)
        assert msg.startswith("page_width: invalid RDL size ")
        assert repr(text) in msg

    def test_negative_rejected_by_default(self):
        with pytest.raises(ValueError, match=r"^height: invalid RDL size '-1in'; must not be"):
            validate_size("-1in", field="height")

    def test_negative_allowed_when_requested(self):
        assert validate_size("-1in", field="height", allow_negative=True) == "-1in"

    def test_negative_zero_is_zero(self):
        assert validate_size("-0in", field="top") == "-0in"
        with pytest.raises(ValueError, match="greater than zero"):
            validate_size("-0in", field="width", allow_zero=False)

    def test_zero(self):
        assert validate_size("0in", field="top") == "0in"
        with pytest.raises(ValueError, match=r"^width: .*greater than zero"):
            validate_size("0cm", field="width", allow_zero=False)

    @pytest.mark.parametrize("text", ["161in", "4065mm", "11521pt", "-161in"])
    def test_max(self, text):
        with pytest.raises(ValueError, match="exceeds the RDL maximum of 160in"):
            validate_size(text, field="width", allow_negative=True)

    def test_max_boundary_and_override(self):
        assert validate_size("160in", field="width") == "160in"
        assert validate_size("4064mm", field="width") == "4064mm"
        assert validate_size("500in", field="width", max_in=None) == "500in"
        with pytest.raises(ValueError, match="maximum of 10in"):
            validate_size("11in", field="width", max_in=10)

    def test_expression_only_when_allowed(self):
        assert validate_size("=Parameters!Size.Value", field="f", allow_expression=True) == (
            "=Parameters!Size.Value"
        )
        with pytest.raises(ValueError, match="invalid RDL size"):
            validate_size("=Parameters!Size.Value", field="f")

    def test_optional(self):
        assert validate_optional_size(None, field="x") is None
        assert validate_optional_size("1in", field="x") == "1in"
        with pytest.raises(ValueError, match="^x: "):
            validate_optional_size("1", field="x")


class TestPresets:
    def test_dimension_is_strictly_positive(self):
        assert units.dimension(None, "w") is None
        assert units.dimension("1in", "w") == "1in"
        for bad in ("0in", "-1in", "abc"):
            with pytest.raises(ValueError, match="^w: "):
                units.dimension(bad, "w")

    def test_position_allows_zero_not_negative(self):
        assert units.position("0in", "top") == "0in"
        with pytest.raises(ValueError, match="^top: "):
            units.position("-0.1in", "top")

    def test_style_size(self):
        assert units.style_size("=Fields!S.Value", "font_size", positive=True) == "=Fields!S.Value"
        assert units.style_size("0pt", "padding_top") == "0pt"
        with pytest.raises(ValueError, match="^font_size: "):
            units.style_size("0pt", "font_size", positive=True)
        with pytest.raises(ValueError, match="^padding_top: "):
            units.style_size("-2pt", "padding_top")

    def test_box(self):
        assert units.box("0in", "0in", "1in", "0.25in") == ("0in", "0in", "1in", "0.25in")
        with pytest.raises(ValueError, match="^height: "):
            units.box("0in", "0in", "1in", "")
        with pytest.raises(ValueError, match="^left: "):
            units.box("0in", "-1in", "1in", "1in")
