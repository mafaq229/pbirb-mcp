"""Tests for ``style_tablix_row`` — Phase 6 commit 25 of v0.3.0.

The headline tool: collapse N-individual-set_textbox_style-calls-per-row
into 1. Driven by the Overspeed-Violations session feedback where
styling 12 cells of a single row required 12 separate tool calls.

The fixture's ``MainTable`` has 2 body rows × 3 columns:
- Row 0 (header): HeaderProductID, HeaderProductName, HeaderAmount
- Row 1 (details): ProductID, ProductName, Amount

Tests exercise integer + string-role addressing, group-role addressing
(after add_row_group), the headline ratio (cells styled = 1 call), and
the validation surface.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.ops.reader import get_textbox
from pbirb_mcp.ops.styling import style_tablix_row
from pbirb_mcp.ops.tablix import add_row_group
from pbirb_mcp.ops.tablix_subtotals import add_subtotal_row
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _bg_color(rdl_path: Path, textbox_name: str) -> str | None:
    """Read the BackgroundColor from a textbox's box-level Style."""
    tb = get_textbox(path=str(rdl_path), name=textbox_name)
    style = tb.get("style") or {}
    return style.get("box", {}).get("BackgroundColor")


def _font_weight(rdl_path: Path, textbox_name: str) -> str | None:
    tb = get_textbox(path=str(rdl_path), name=textbox_name)
    style = tb.get("style") or {}
    return style.get("run", {}).get("FontWeight")


# ---- integer index addressing -------------------------------------------


class TestStyleTablixRowByIndex:
    def test_styles_every_cell_in_row_zero(self, rdl_path):
        result = style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row=0,
            background_color="#003366",
            color="#FFFFFF",
            font_weight="Bold",
        )
        assert result["tablix"] == "MainTable"
        assert result["row"] == 0
        assert result["row_index"] == 0
        assert result["kind"] == "TablixRow"
        # All three header cells touched.
        assert sorted(result["cells"]) == sorted(
            ["HeaderProductID", "HeaderProductName", "HeaderAmount"]
        )
        assert result["skipped"] == []
        # Union of changed sub-paths covers box.BackgroundColor + run.Color
        # + run.FontWeight.
        assert "box.BackgroundColor" in result["changed"]
        assert "run.Color" in result["changed"]
        assert "run.FontWeight" in result["changed"]
        # Spot-check actual XML.
        for name in ["HeaderProductID", "HeaderProductName", "HeaderAmount"]:
            assert _bg_color(rdl_path, name) == "#003366"
            assert _font_weight(rdl_path, name) == "Bold"

    def test_styles_data_row(self, rdl_path):
        style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row=1,
            color="#003366",
            text_align="Center",
        )
        # Data cells: ProductID, ProductName, Amount.
        for name in ["ProductID", "ProductName", "Amount"]:
            tb = get_textbox(path=str(rdl_path), name=name)
            assert tb["style"]["run"]["Color"] == "#003366"
            assert tb["style"]["paragraph"]["TextAlign"] == "Center"

    def test_index_out_of_range_raises(self, rdl_path):
        with pytest.raises(IndexError, match="out of range"):
            style_tablix_row(
                path=str(rdl_path),
                tablix_name="MainTable",
                row=99,
                background_color="#000000",
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            style_tablix_row(
                path=str(rdl_path),
                tablix_name="NoSuchTablix",
                row=0,
                background_color="#000000",
            )

    def test_bool_row_rejected(self, rdl_path):
        # Bool is an int subclass in Python; reject explicitly so True
        # doesn't silently become row 1.
        with pytest.raises(TypeError, match="bool"):
            style_tablix_row(
                path=str(rdl_path),
                tablix_name="MainTable",
                row=True,  # type: ignore[arg-type]
                background_color="#000000",
            )

    def test_non_int_non_str_row_rejected(self, rdl_path):
        with pytest.raises(TypeError, match="int or str"):
            style_tablix_row(
                path=str(rdl_path),
                tablix_name="MainTable",
                row=[0],  # type: ignore[arg-type]
                background_color="#000",
            )


# ---- string-role addressing: 'header' / 'details' -----------------------


class TestStyleTablixRowByRole:
    def test_role_header_resolves_to_first_KeepWithGroup_After_row(self, rdl_path):
        result = style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row="header",
            background_color="#FF0000",
        )
        assert result["row"] == "header"
        assert result["row_index"] == 0
        assert sorted(result["cells"]) == sorted(
            ["HeaderProductID", "HeaderProductName", "HeaderAmount"]
        )
        for name in result["cells"]:
            assert _bg_color(rdl_path, name) == "#FF0000"

    def test_role_details_resolves_to_details_leaf_row(self, rdl_path):
        result = style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row="details",
            background_color="#00FF00",
        )
        assert result["row"] == "details"
        assert result["row_index"] == 1
        assert sorted(result["cells"]) == sorted(["ProductID", "ProductName", "Amount"])
        for name in result["cells"]:
            assert _bg_color(rdl_path, name) == "#00FF00"

    def test_role_unknown_raises_value_error(self, rdl_path):
        with pytest.raises(ValueError, match="unknown row role"):
            style_tablix_row(
                path=str(rdl_path),
                tablix_name="MainTable",
                row="banner",
                background_color="#000",
            )


# ---- group-role addressing: '<group>_header' / '<group>_footer' --------


class TestStyleTablixRowByGroupRole:
    def test_group_header_after_add_row_group(self, rdl_path):
        # Wrap the existing hierarchy in a Region group; this inserts a
        # new header row at body row 0.
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!ProductName.Value",
        )
        result = style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row="Region_header",
            background_color="#0000FF",
        )
        assert result["row"] == "Region_header"
        assert result["row_index"] == 0
        # add_row_group emits cell textbox names like 'Region_Header_<col>'.
        assert all(c.startswith("Region_Header_") for c in result["cells"])

    def test_group_footer_after_add_subtotal_row(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!ProductName.Value",
        )
        # Add a subtotal row — gives the group a footer leaf.
        # The 'column' kwarg refers to a textbox name in the tablix's
        # CURRENT last row (per the v0.2 add_subtotal_row contract).
        # After add_row_group, the last row is the Details row with
        # cells ProductID/ProductName/Amount.
        add_subtotal_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            position="footer",
            aggregates=[
                {"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"},
            ],
        )
        result = style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row="Region_footer",
            background_color="#FF00FF",
        )
        assert result["row"] == "Region_footer"
        # Footer row index = last body row (after header+details).
        # After add_row_group: row 0 = Region header, row 1 = original
        # column header, row 2 = Details. add_subtotal_row(footer) adds
        # row 3.
        assert result["row_index"] == 3

    def test_group_footer_missing_raises_when_no_footer(self, rdl_path):
        # Add a Region group but no footer subtotal row.
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!ProductName.Value",
        )
        with pytest.raises(ElementNotFoundError, match="footer row for group"):
            style_tablix_row(
                path=str(rdl_path),
                tablix_name="MainTable",
                row="Region_footer",
                background_color="#FFF",
            )

    def test_unknown_group_header_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError, match="header row for group"):
            style_tablix_row(
                path=str(rdl_path),
                tablix_name="MainTable",
                row="Imaginary_header",
                background_color="#FFF",
            )


# ---- the headline collapse: N-call → 1-call check -----------------------


class TestHeadlineCollapseRatio:
    """Plan-mandated verification step: drive the equivalent of N
    set_textbox_style operations via 1 style_tablix_row call against an
    N-column tablix. cells list in result is the union of all touched
    textbox names; len(cells) == column count."""

    def test_one_call_styles_three_cells_in_minimal_fixture(self, rdl_path):
        # The minimal fixture has 3 columns. 3-cell row × 1 call.
        result = style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row="details",
            font_size="10pt",
            color="#003366",
            text_align="Center",
        )
        assert len(result["cells"]) == 3
        assert "run.FontSize" in result["changed"]
        assert "run.Color" in result["changed"]
        assert "paragraph.TextAlign" in result["changed"]

    def test_skipped_is_empty_when_every_cell_has_textbox(self, rdl_path):
        # Healthy fixture — every cell has a Textbox. No skips.
        result = style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row=0,
            background_color="#000",
        )
        assert result["skipped"] == []


# ---- no-op behaviour ----------------------------------------------------


class TestStyleTablixRowNoOp:
    def test_no_style_kwargs_short_circuits(self, rdl_path):
        before = (rdl_path).read_bytes()
        result = style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row=0,
        )
        # No fields supplied → no save — the underlying bulk call's
        # all-None short-circuit kicks in.
        assert result["changed"] == []
        assert (rdl_path).read_bytes() == before


# ---- pre-encoded text regression (Phase 0 contract) --------------------


class TestStyleTablixRowEncoding:
    def test_pre_encoded_format_no_double_encode(self, rdl_path):
        # Format string with an encoded entity must end up &amp; on disk,
        # not &amp;amp; — the encoding rule from Phase 0 transferred
        # via set_textbox_style_bulk.
        style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row="details",
            format="A &amp; B",
        )
        assert b"&amp;amp;" not in (rdl_path).read_bytes()


# ---- round-trip ----------------------------------------------------------


class TestStyleTablixRowRoundTrip:
    def test_styled_file_validates(self, rdl_path):
        style_tablix_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            row="header",
            background_color="#003366",
            color="#FFFFFF",
            font_weight="Bold",
            font_size="11pt",
            padding_top="2pt",
        )
        RDLDocument.open(rdl_path).validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_style_tablix_row_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "style_tablix_row" in names
