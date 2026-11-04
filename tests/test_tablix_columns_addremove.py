"""Tests for add_tablix_column / remove_tablix_column (v0.2 commit 2).

These tools change body shape (one extra TablixColumn + one TablixMember
in the column hierarchy + one extra cell per row). Removal is the inverse
addressed by ``column_name`` — the textbox name in the data row.

Cell-text convention:

* For an n-row tablix where n >= 2: row 0 (header) gets ``header_text``
  (defaulting to ``column_name``); rows 1..n-2 (middle) get blank
  textboxes; row n-1 (data) gets ``expression``.
* For a 1-row tablix: the single row gets ``expression``.
* For a 0-row tablix: only the column + hierarchy member are added.

Textbox names: the data-row cell uses ``column_name`` exactly; other
rows use ``<column_name>_<row_index>`` so report-wide uniqueness holds
provided ``column_name`` itself is unique.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.tablix_shape import column_leaves
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.tablix_columns import (
    add_tablix_column,
    remove_tablix_column,
)
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _tablix(doc: RDLDocument, name: str):
    return doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='{name}']")


def _body_columns(tablix):
    return find_children(tablix.find(f"{q('TablixBody')}/{q('TablixColumns')}"), "TablixColumn")


def _body_rows(tablix):
    return find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")


def _row_cells(row):
    return row.findall(f"{q('TablixCells')}/{q('TablixCell')}")


def _cell_textbox(cell):
    return cell.find(f"{q('CellContents')}/{q('Textbox')}")


def _column_hierarchy_top_members(tablix):
    return list(tablix.find(q("TablixColumnHierarchy")).find(q("TablixMembers")))


# ---- add_tablix_column ----------------------------------------------------


class TestAddTablixColumn:
    def test_appends_column_at_end_by_default(self, rdl_path):
        doc_before = RDLDocument.open(rdl_path)
        cols_before = len(_body_columns(_tablix(doc_before, "MainTable")))
        members_before = len(_column_hierarchy_top_members(_tablix(doc_before, "MainTable")))
        per_row_before = [len(_row_cells(r)) for r in _body_rows(_tablix(doc_before, "MainTable"))]

        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )

        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        assert len(_body_columns(tablix)) == cols_before + 1
        assert len(_column_hierarchy_top_members(tablix)) == members_before + 1
        for i, row in enumerate(_body_rows(tablix)):
            assert len(_row_cells(row)) == per_row_before[i] + 1

    def test_appends_with_default_width_one_inch(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        last_col = _body_columns(tablix)[-1]
        assert find_child(last_col, "Width").text == "1in"

    def test_inserts_at_explicit_position_zero(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
            position=0,
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        rows = _body_rows(tablix)
        # Data row (last row) — first cell now holds the new column's textbox.
        first_data_cell = _row_cells(rows[-1])[0]
        tb = _cell_textbox(first_data_cell)
        assert tb.get("Name") == "MainTable_Discount"
        textrun_value = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "=Fields!Discount.Value"

    def test_data_row_holds_expression_with_bare_column_name(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        rows = _body_rows(tablix)
        # Last row (= data row), last cell (= just-added column).
        data_cell = _row_cells(rows[-1])[-1]
        tb = _cell_textbox(data_cell)
        assert tb.get("Name") == "MainTable_Discount"
        textrun_value = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "=Fields!Discount.Value"

    def test_header_row_holds_default_header_text(self, rdl_path):
        # Default header_text is column_name itself, placed as a literal.
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        rows = _body_rows(tablix)
        # First row (header), last cell (= just-added column).
        header_cell = _row_cells(rows[0])[-1]
        tb = _cell_textbox(header_cell)
        # Header textbox name suffix.
        assert tb.get("Name") == "MainTable_Discount_0"
        textrun_value = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "MainTable_Discount"

    def test_header_text_override(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
            header_text="Discount",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        rows = _body_rows(tablix)
        header_cell = _row_cells(rows[0])[-1]
        textrun_value = _cell_textbox(header_cell).find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "Discount"

    def test_explicit_width(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
            width="2.5cm",
        )
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        last_col = _body_columns(tablix)[-1]
        assert find_child(last_col, "Width").text == "2.5cm"

    def test_duplicate_column_name_rejected_against_existing_textbox(self, rdl_path):
        # Fixture already has a textbox named "Amount".
        with pytest.raises(ValueError):
            add_tablix_column(
                path=str(rdl_path),
                tablix_name="MainTable",
                column_name="Amount",
                expression="=Fields!Amount.Value",
            )

    def test_position_out_of_range_rejected(self, rdl_path):
        with pytest.raises(IndexError):
            add_tablix_column(
                path=str(rdl_path),
                tablix_name="MainTable",
                column_name="MainTable_Discount",
                expression="=Fields!Discount.Value",
                position=999,
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_tablix_column(
                path=str(rdl_path),
                tablix_name="NoSuch",
                column_name="X",
                expression="=Fields!X.Value",
            )

    def test_round_trip_safe(self, rdl_path):
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        RDLDocument.open(rdl_path).validate()

    def test_data_expression_lands_in_details_row_not_last_row(self, rdl_path):
        """After add_row_group + add_subtotal_row, the literal "last row" is
        the subtotal footer — but the data expression must still land in the
        Details row. Regression test for the prior ``i == n_rows - 1`` bug
        where the expression went to the totals row and the actual data row
        got a blank cell with a `_<idx>` suffix."""
        from pbirb_mcp.ops.tablix import add_row_group
        from pbirb_mcp.ops.tablix_subtotals import add_subtotal_row

        # Build a tablix with: row 0 = group header, row 1 = Details, row 2 = footer.
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        add_subtotal_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
            position="footer",
        )

        # Now add a column. Expression must land in row 1 (Details), not
        # row 2 (footer).
        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="Discount",
            expression="=Fields!Discount.Value",
        )

        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        rows = _body_rows(tablix)
        # Fixture starts with 2 rows (header + Details). After add_row_group
        # → 3 rows (group header + header + Details). After add_subtotal_row
        # → 4 rows (group header + header + Details + footer).
        assert len(rows) == 4

        def cell_textbox_name(row, col_idx):
            cells = _row_cells(row)
            tb = cells[col_idx].find(f"{q('CellContents')}/{q('Textbox')}")
            return tb.get("Name") if tb is not None else None

        # The new column was appended at the end (last column).
        last_col = len(_body_columns(tablix)) - 1

        # Critical assertion: the bare "Discount" name (= data row) must be
        # in the Details row (row 2), not the literal last row (row 3).
        # Pre-fix this would land in row 3 and Details would get "Discount_2".
        assert cell_textbox_name(rows[2], last_col) == "Discount"
        # Other rows get the suffixed/blank textbox name.
        assert cell_textbox_name(rows[0], last_col) == "Discount_0"
        assert cell_textbox_name(rows[1], last_col) == "Discount_1"
        assert cell_textbox_name(rows[3], last_col) == "Discount_3"

        # And the <Value> in the Details cell holds the expression.
        details_cell = _row_cells(rows[2])[last_col]
        details_tb = details_cell.find(f"{q('CellContents')}/{q('Textbox')}")
        value = details_tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert value.text == "=Fields!Discount.Value"


# ---- remove_tablix_column -------------------------------------------------


class TestRemoveTablixColumn:
    def test_inverts_add_tablix_column(self, rdl_path):
        before = RDLDocument.open(rdl_path)
        cols_before = len(_body_columns(_tablix(before, "MainTable")))
        members_before = len(_column_hierarchy_top_members(_tablix(before, "MainTable")))
        per_row_before = [len(_row_cells(r)) for r in _body_rows(_tablix(before, "MainTable"))]

        add_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
            expression="=Fields!Discount.Value",
        )
        remove_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="MainTable_Discount",
        )

        after = RDLDocument.open(rdl_path)
        tablix = _tablix(after, "MainTable")
        assert len(_body_columns(tablix)) == cols_before
        assert len(_column_hierarchy_top_members(tablix)) == members_before
        for i, row in enumerate(_body_rows(tablix)):
            assert len(_row_cells(row)) == per_row_before[i]

    def test_removes_existing_fixture_column(self, rdl_path):
        # Fixture has an "Amount" data textbox at column index 2.
        before = RDLDocument.open(rdl_path)
        cols_before = len(_body_columns(_tablix(before, "MainTable")))

        remove_tablix_column(
            path=str(rdl_path),
            tablix_name="MainTable",
            column_name="Amount",
        )

        after = RDLDocument.open(rdl_path)
        tablix = _tablix(after, "MainTable")
        assert len(_body_columns(tablix)) == cols_before - 1
        # No row should still contain a textbox with that name.
        for row in _body_rows(tablix):
            for cell in _row_cells(row):
                tb = _cell_textbox(cell)
                if tb is not None:
                    assert tb.get("Name") != "Amount"

    def test_unknown_column_name_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_tablix_column(
                path=str(rdl_path),
                tablix_name="MainTable",
                column_name="NoSuchColumn",
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_tablix_column(
                path=str(rdl_path),
                tablix_name="NoSuch",
                column_name="X",
            )


# ---- remove_tablix_column: group columns and the last column (#35) ---------

_COLUMN_MEMBERS = (
    "<TablixMember />\n                <TablixMember />\n                <TablixMember />"
)


def _cg(name: str = "CG") -> str:
    return (
        f'<Group Name="{name}"><GroupExpressions><GroupExpression>=1</GroupExpression>'
        "</GroupExpressions></Group>"
    )


def _patch_columns(rdl_path: Path, members: str) -> Path:
    text = rdl_path.read_text(encoding="utf-8")
    assert text.count(_COLUMN_MEMBERS) == 1
    rdl_path.write_text(text.replace(_COLUMN_MEMBERS, members), encoding="utf-8")
    return rdl_path


def _column_bindings(path) -> list[tuple[str, str]]:
    tablix = _tablix(RDLDocument.open(path), "MainTable")
    leaves = column_leaves(tablix)
    cells = _row_cells(_body_rows(tablix)[1])
    assert len(leaves) == len(_body_columns(tablix)) == len(cells)
    out = []
    for leaf, cell in zip(leaves, cells):
        names, node = [], leaf
        while node is not None and node.tag == q("TablixMember"):
            group = find_child(node, "Group")
            if group is not None:
                names.append(group.get("Name"))
            node = node.getparent().getparent()
        out.append(("/".join(reversed(names)) or "static", _cell_textbox(cell).get("Name")))
    return out


class TestRemoveTablixColumnHierarchy:
    def test_only_column_of_group_refused(self, rdl_path):
        _patch_columns(
            rdl_path,
            f"<TablixMember /><TablixMember>{_cg()}<TablixMembers><TablixMember />"
            "</TablixMembers></TablixMember><TablixMember />",
        )
        raw = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="column group 'CG'"):
            remove_tablix_column(str(rdl_path), "MainTable", "ProductName")
        assert rdl_path.read_bytes() == raw

    def test_only_column_of_group_with_remove_group(self, rdl_path):
        _patch_columns(
            rdl_path,
            f"<TablixMember /><TablixMember>{_cg()}<TablixMembers><TablixMember />"
            "</TablixMembers></TablixMember><TablixMember />",
        )
        result = remove_tablix_column(str(rdl_path), "MainTable", "ProductName", remove_group=True)
        assert result["removed_groups"] == ["CG"]
        assert _column_bindings(rdl_path) == [("static", "ProductID"), ("static", "Amount")]
        assert 'Name="CG"' not in rdl_path.read_text(encoding="utf-8")
        assert validate_report(str(rdl_path))["valid"]

    def test_group_that_is_its_own_leaf_refused(self, rdl_path):
        # Matrix-style column group whose member is itself the leaf.
        _patch_columns(
            rdl_path, f"<TablixMember /><TablixMember>{_cg()}</TablixMember><TablixMember />"
        )
        with pytest.raises(ValueError, match="'CG'"):
            remove_tablix_column(str(rdl_path), "MainTable", "ProductName")
        remove_tablix_column(str(rdl_path), "MainTable", "ProductName", remove_group=True)
        assert _column_bindings(rdl_path) == [("static", "ProductID"), ("static", "Amount")]

    def test_column_inside_multi_column_group_removes_its_leaf(self, rdl_path):
        _patch_columns(
            rdl_path,
            f"<TablixMember /><TablixMember>{_cg()}<TablixMembers><TablixMember />"
            "<TablixMember /></TablixMembers></TablixMember>",
        )
        result = remove_tablix_column(str(rdl_path), "MainTable", "ProductName")
        assert result["removed_groups"] == []
        assert _column_bindings(rdl_path) == [("static", "ProductID"), ("CG", "Amount")]
        assert validate_report(str(rdl_path))["valid"]

    def test_column_after_group_removes_the_right_leaf(self, rdl_path):
        # Top-level index 2 doesn't exist here: CG wraps columns 0-1.
        _patch_columns(
            rdl_path,
            f"<TablixMember>{_cg()}<TablixMembers><TablixMember /><TablixMember />"
            "</TablixMembers></TablixMember><TablixMember />",
        )
        remove_tablix_column(str(rdl_path), "MainTable", "Amount")
        assert _column_bindings(rdl_path) == [("CG", "ProductID"), ("CG", "ProductName")]

    def test_nested_static_wrapper_left_non_empty(self, rdl_path):
        # A static wrapper holding one leaf: removing that column takes the
        # wrapper too (an empty <TablixMembers/> is invalid); no group lost.
        _patch_columns(
            rdl_path,
            "<TablixMember /><TablixMember><TablixMembers><TablixMember />"
            "</TablixMembers></TablixMember><TablixMember />",
        )
        result = remove_tablix_column(str(rdl_path), "MainTable", "ProductName")
        assert result["removed_groups"] == []
        assert _column_bindings(rdl_path) == [("static", "ProductID"), ("static", "Amount")]
        assert validate_report(str(rdl_path))["valid"]

    def test_last_column_refused_bytes_unchanged(self, rdl_path):
        remove_tablix_column(str(rdl_path), "MainTable", "ProductID")
        remove_tablix_column(str(rdl_path), "MainTable", "ProductName")
        raw = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="only column"):
            remove_tablix_column(str(rdl_path), "MainTable", "Amount")
        assert rdl_path.read_bytes() == raw
        assert validate_report(str(rdl_path))["valid"]

    def test_inconsistent_hierarchy_refused(self, rdl_path):
        _patch_columns(rdl_path, "<TablixMember /><TablixMember />")
        raw = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="2 column-hierarchy leaves for 3 body columns"):
            remove_tablix_column(str(rdl_path), "MainTable", "Amount")
        assert rdl_path.read_bytes() == raw

    def test_add_then_remove_is_byte_identical(self, rdl_path):
        raw = rdl_path.read_bytes()
        add_tablix_column(str(rdl_path), "MainTable", "Extra", "=1", position=1)
        remove_tablix_column(str(rdl_path), "MainTable", "Extra")
        assert rdl_path.read_bytes() == raw


# ---- tools.py registration smoke ------------------------------------------


class TestToolRegistration:
    def test_both_tools_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert {"add_tablix_column", "remove_tablix_column"}.issubset(server._tools.keys())
