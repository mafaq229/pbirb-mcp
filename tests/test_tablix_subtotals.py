"""Tests for add_subtotal_row (v0.2 commit 3)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.tablix_shape import column_leaves, row_leaves
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.tablix import add_row_group, remove_row_group
from pbirb_mcp.ops.tablix_columns import add_column_group
from pbirb_mcp.ops.tablix_static import add_static_row
from pbirb_mcp.ops.tablix_subtotals import add_subtotal_column, add_subtotal_row
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def rdl_with_region_group(rdl_path):
    add_row_group(
        path=str(rdl_path),
        tablix_name="MainTable",
        group_name="Region",
        group_expression="=Fields!Region.Value",
    )
    return rdl_path


def _tablix(doc: RDLDocument, name: str):
    return doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='{name}']")


def _wrapper_member_count(tablix, group_name: str) -> int:
    for member in tablix.find(q("TablixRowHierarchy")).iter(q("TablixMember")):
        g = find_child(member, "Group")
        if g is not None and g.get("Name") == group_name:
            inner = find_child(member, "TablixMembers")
            return len(list(inner)) if inner is not None else 0
    return 0


def _body_rows(tablix):
    return find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")


def _row_cell_textboxes(row):
    return [
        cell.find(f"{q('CellContents')}/{q('Textbox')}")
        for cell in row.findall(f"{q('TablixCells')}/{q('TablixCell')}")
    ]


def _textrun_value(textbox):
    if textbox is None:
        return None
    val = textbox.find(
        f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
    )
    if val is None:
        return None
    # lxml round-trips empty Value text as None — normalise to "" so blank
    # cells compare equal to the "" literal we emit at write time.
    return val.text if val.text is not None else ""


class TestAddSubtotalRow:
    def test_appends_member_in_wrapper_at_footer(self, rdl_with_region_group):
        tablix_before = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        before = _wrapper_member_count(tablix_before, "Region")

        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[
                {"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"},
            ],
            position="footer",
        )
        tablix = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        assert _wrapper_member_count(tablix, "Region") == before + 1

    def test_appends_body_row_at_end(self, rdl_with_region_group):
        before = len(_body_rows(_tablix(RDLDocument.open(rdl_with_region_group), "MainTable")))
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[
                {"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"},
            ],
            position="footer",
        )
        rows = _body_rows(_tablix(RDLDocument.open(rdl_with_region_group), "MainTable"))
        assert len(rows) == before + 1

    def test_aggregate_lands_in_correct_column(self, rdl_with_region_group):
        # Fixture has data-row textboxes [ProductID, ProductName, Amount].
        # An aggregate keyed on "Amount" should land in column 2.
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[
                {"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"},
            ],
            position="footer",
        )
        tablix = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        rows = _body_rows(tablix)
        last_row = rows[-1]
        textboxes = _row_cell_textboxes(last_row)
        # Column 0 + 1 are blank; column 2 holds the aggregate.
        assert _textrun_value(textboxes[0]) == ""
        assert _textrun_value(textboxes[1]) == ""
        assert _textrun_value(textboxes[2]) == "=Sum(Fields!Amount.Value)"

    def test_header_position_inserts_after_group_header(self, rdl_with_region_group):
        # Body before footer/header: [header (row 0), group header (1), data (2)]
        # (#65: the column-header row stays outside the group).
        # header position: new subtotal row goes at index 2.
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[],
            position="header",
        )
        tablix = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        rows = _body_rows(tablix)
        # Find the new (all-blank) row — it should be at index 2.
        textboxes = _row_cell_textboxes(rows[2])
        for tb in textboxes:
            assert _textrun_value(tb) == ""

    def test_invalid_position_raises(self, rdl_with_region_group):
        with pytest.raises(ValueError):
            add_subtotal_row(
                path=str(rdl_with_region_group),
                tablix_name="MainTable",
                group_name="Region",
                aggregates=[],
                position="middle",
            )

    def test_unknown_group_raises(self, rdl_with_region_group):
        with pytest.raises(ElementNotFoundError):
            add_subtotal_row(
                path=str(rdl_with_region_group),
                tablix_name="MainTable",
                group_name="NoSuch",
                aggregates=[],
            )

    def test_unknown_aggregate_column_raises(self, rdl_with_region_group):
        with pytest.raises(ElementNotFoundError):
            add_subtotal_row(
                path=str(rdl_with_region_group),
                tablix_name="MainTable",
                group_name="Region",
                aggregates=[{"column": "NoSuchColumn", "expression": "=Sum(0)"}],
            )

    def test_round_trip_safe(self, rdl_with_region_group):
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        RDLDocument.open(rdl_with_region_group).validate()

    def test_second_footer_call_still_resolves_details_row_columns(self, rdl_with_region_group):
        """Regression: pre-fix, the second add_subtotal_row(footer) call
        looked at the literal last row — which was the previous subtotal
        row's textboxes (e.g. ``Region_Footer_2``), so passing the original
        Details-row name (``Amount``) raised ElementNotFoundError. The fix
        walks the row hierarchy for the Details leaf, so the same name
        keeps working across repeat calls."""
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
            position="footer",
        )
        # Second call must still accept the Details-row column name.
        add_subtotal_row(
            path=str(rdl_with_region_group),
            tablix_name="MainTable",
            group_name="Region",
            aggregates=[{"column": "Amount", "expression": "=Avg(Fields!Amount.Value)"}],
            position="footer",
        )
        # Both subtotal rows landed and both have the aggregate in column 2.
        tablix = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        rows = _body_rows(tablix)
        # Body rows: [group header, header, Details, subtotal#1, subtotal#2].
        assert len(rows) == 5
        sub1 = _row_cell_textboxes(rows[3])
        sub2 = _row_cell_textboxes(rows[4])
        assert _textrun_value(sub1[2]) == "=Sum(Fields!Amount.Value)"
        assert _textrun_value(sub2[2]) == "=Avg(Fields!Amount.Value)"


class TestAddSubtotalColumn:
    """Phase F commit 19 — column-axis mirror of add_subtotal_row.
    Adds a new TablixMember inside a column-group's <TablixMembers>,
    a new <TablixColumn> in TablixBody/TablixColumns, and a cell at
    the new column index in every existing body row."""

    @pytest.fixture
    def rdl_with_column_group(self, rdl_path):
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Date",
            group_expression="=Fields!ProductID.Value",
        )
        return rdl_path

    def test_appends_total_column_after_group(self, rdl_with_column_group):
        result = add_subtotal_column(
            path=str(rdl_with_column_group),
            tablix_name="MainTable",
            group_name="Date",
            aggregates=[
                {"row": 1, "expression": "=Sum(Fields!Amount.Value)"},
            ],
        )
        assert result["tablix"] == "MainTable"
        assert result["kind"] == "TablixMember"
        assert result["position"] == "after"
        assert "member_added" in result["changed"]
        assert "column_added" in result["changed"]

        doc = RDLDocument.open(rdl_with_column_group)
        tablix = _tablix(doc, "MainTable")
        # Body has one more column than before.
        cols = find_children(tablix.find(f"{q('TablixBody')}/{q('TablixColumns')}"), "TablixColumn")
        # Minimal fixture 3 cols; add_column_group adds no body column (its
        # caption is a TablixHeader, #29); add_subtotal_column adds 1 → 4.
        assert len(cols) == 4
        # Row 1's last cell holds the aggregate expression.
        rows = _body_rows(tablix)
        last_cell = _row_cell_textboxes(rows[1])[-1]
        assert _textrun_value(last_cell) == "=Sum(Fields!Amount.Value)"

    def test_unlisted_rows_get_blank_cells(self, rdl_with_column_group):
        add_subtotal_column(
            path=str(rdl_with_column_group),
            tablix_name="MainTable",
            group_name="Date",
            # No aggregate for row 0 — should be blank.
            aggregates=[{"row": 1, "expression": "=Sum(Fields!Amount.Value)"}],
        )
        doc = RDLDocument.open(rdl_with_column_group)
        tablix = _tablix(doc, "MainTable")
        rows = _body_rows(tablix)
        # Row 0 last cell exists but is blank.
        last_cell = _row_cell_textboxes(rows[0])[-1]
        assert _textrun_value(last_cell) == ""

    def test_unknown_column_group_raises(self, rdl_with_column_group):
        with pytest.raises(ElementNotFoundError, match="column-axis group"):
            add_subtotal_column(
                path=str(rdl_with_column_group),
                tablix_name="MainTable",
                group_name="NoSuchGroup",
                aggregates=[{"row": 0, "expression": "=1"}],
            )

    def test_invalid_position_rejected(self, rdl_with_column_group):
        with pytest.raises(ValueError, match="position"):
            add_subtotal_column(
                path=str(rdl_with_column_group),
                tablix_name="MainTable",
                group_name="Date",
                aggregates=[{"row": 0, "expression": "=1"}],
                position="middle",
            )

    def test_row_index_out_of_range_raises(self, rdl_with_column_group):
        with pytest.raises(ElementNotFoundError, match="out of range"):
            add_subtotal_column(
                path=str(rdl_with_column_group),
                tablix_name="MainTable",
                group_name="Date",
                aggregates=[{"row": 99, "expression": "=1"}],
            )

    def test_malformed_aggregate_entry_rejected(self, rdl_with_column_group):
        with pytest.raises(ValueError, match="aggregate entry"):
            add_subtotal_column(
                path=str(rdl_with_column_group),
                tablix_name="MainTable",
                group_name="Date",
                aggregates=[{"expression": "=1"}],  # missing 'row'
            )

    def test_round_trip_valid(self, rdl_with_column_group):
        add_subtotal_column(
            path=str(rdl_with_column_group),
            tablix_name="MainTable",
            group_name="Date",
            aggregates=[{"row": 1, "expression": "=Sum(Fields!Amount.Value)"}],
        )
        RDLDocument.open(rdl_with_column_group).validate()

    def test_position_before_prepends(self, rdl_with_column_group):
        result = add_subtotal_column(
            path=str(rdl_with_column_group),
            tablix_name="MainTable",
            group_name="Date",
            aggregates=[{"row": 1, "expression": "=Sum(Fields!Amount.Value)"}],
            position="before",
        )
        assert result["position"] == "before"
        # Insertion before the group → column_index sits at the left
        # edge of the group, never at the very end.
        doc = RDLDocument.open(rdl_with_column_group)
        tablix = _tablix(doc, "MainTable")
        cols = find_children(tablix.find(f"{q('TablixBody')}/{q('TablixColumns')}"), "TablixColumn")
        # 3 (fixture) + 0 (add_column_group, #29) + 1 (add_subtotal_column) = 4.
        assert len(cols) == 4


# ---- placement follows the hierarchy; unique names; scope (#32) -------------

_DETAIL = '<Group Name="Details" />'
_COLUMN_MEMBERS = (
    "<TablixMember />\n                <TablixMember />\n                <TablixMember />"
)


def _group_xml(name: str, expr: str = "=1") -> str:
    return (
        f'<Group Name="{name}"><GroupExpressions><GroupExpression>{expr}'
        "</GroupExpression></GroupExpressions></Group>"
    )


def _patch(path: Path, old: str, new: str) -> Path:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def _group_path(leaf) -> str:
    names, node = [], leaf
    while node is not None and node.tag == q("TablixMember"):
        group = find_child(node, "Group")
        if group is not None:
            names.append(group.get("Name"))
        node = node.getparent().getparent()
    return "/".join(reversed(names)) or "static"


def _row_map(path) -> list[tuple[str, str]]:
    tablix = _tablix(RDLDocument.open(path), "MainTable")
    leaves, rows = row_leaves(tablix), _body_rows(tablix)
    assert len(leaves) == len(rows)
    return [
        (_group_path(leaf), _row_cell_textboxes(r)[0].get("Name")) for leaf, r in zip(leaves, rows)
    ]


def _column_map(path, row: int = 1) -> list[tuple[str, str]]:
    tablix = _tablix(RDLDocument.open(path), "MainTable")
    leaves = column_leaves(tablix)
    cells = _row_cell_textboxes(_body_rows(tablix)[row])
    cols = find_children(tablix.find(f"{q('TablixBody')}/{q('TablixColumns')}"), "TablixColumn")
    assert len(leaves) == len(cols) == len(cells)
    return [(_group_path(leaf), tb.get("Name")) for leaf, tb in zip(leaves, cells)]


def _without(mapping, new_names):
    return [entry for entry in mapping if entry[1] not in set(new_names)]


@pytest.fixture
def rb_shape(rdl_path):
    """Report Builder shape: header / Product group (data leaf) / grand total."""
    add_static_row(str(rdl_path), "MainTable", "GrandTotal", ["Total"])
    return _patch(
        rdl_path,
        _DETAIL,
        _group_xml("Product", "=Fields!ProductName.Value")
        + "<TablixMembers><TablixMember /></TablixMembers>",
    )


@pytest.fixture
def middle_column_group(rdl_path):
    """Column group CG over the middle body column only."""
    return _patch(
        rdl_path,
        _COLUMN_MEMBERS,
        f"<TablixMember /><TablixMember>{_group_xml('CG')}<TablixMembers><TablixMember />"
        "</TablixMembers></TablixMember><TablixMember />",
    )


def _dupes(path) -> list[str]:
    names = [tb.get("Name") for tb in RDLDocument.open(path).root.iter(q("Textbox"))]
    return sorted({n for n in names if names.count(n) > 1})


class TestSubtotalPlacement:
    def test_no_duplicate_names_after_many_calls(self, rdl_with_region_group):
        p = str(rdl_with_region_group)
        for position in ("header", "footer", "header", "footer"):
            for scope in ("group", "total"):
                add_subtotal_row(p, "MainTable", "Region", [], position=position, scope=scope)
        assert _dupes(rdl_with_region_group) == []
        assert validate_report(p)["valid"]

    def test_subtotal_names_never_reuse_the_group_header_stem(self, rdl_with_region_group):
        result = add_subtotal_row(str(rdl_with_region_group), "MainTable", "Region", [], "header")
        assert all(n.startswith("Region_SubtotalHeader_") for n in result["textboxes"])

    def test_report_builder_shape_footer_inside_group_grand_total_static(self, rb_shape):
        before = _row_map(rb_shape)
        result = add_subtotal_row(
            str(rb_shape),
            "MainTable",
            "Product",
            [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        after = _row_map(rb_shape)
        assert _without(after, result["textboxes"]) == before
        assert result["row_index"] == 2
        assert after == [
            ("static", "HeaderProductID"),
            ("Product", "ProductID"),
            ("Product", "Product_SubtotalFooter_0"),
            ("static", "GrandTotal"),
        ]
        assert validate_report(str(rb_shape))["valid"]

    def test_header_scope_group_after_group_header_row(self, rdl_with_region_group):
        result = add_subtotal_row(
            str(rdl_with_region_group), "MainTable", "Region", [], position="header"
        )
        assert result["row_index"] == 2
        assert _row_map(rdl_with_region_group)[2] == ("Region", "Region_SubtotalHeader_0")

    def test_header_without_group_header_row_goes_first(self, rb_shape):
        result = add_subtotal_row(str(rb_shape), "MainTable", "Product", [], position="header")
        assert result["row_index"] == 1
        assert _row_map(rb_shape)[1:3] == [
            ("Product", "Product_SubtotalHeader_0"),
            ("Product", "ProductID"),
        ]

    def test_scope_total_row_is_a_static_sibling(self, rb_shape):
        before = _row_map(rb_shape)
        result = add_subtotal_row(
            str(rb_shape),
            "MainTable",
            "Product",
            [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
            scope="total",
        )
        after = _row_map(rb_shape)
        assert _without(after, result["textboxes"]) == before
        assert after[2] == ("static", "Product_Total_0")
        member = row_leaves(_tablix(RDLDocument.open(rb_shape), "MainTable"))[2]
        assert find_child(member, "KeepWithGroup").text == "Before"
        assert validate_report(str(rb_shape))["valid"]

    def test_scope_total_header_before_group(self, rdl_with_region_group):
        result = add_subtotal_row(
            str(rdl_with_region_group), "MainTable", "Region", [], position="header", scope="total"
        )
        assert result["row_index"] == 1  # after the table header, before Region (#65)
        tablix = _tablix(RDLDocument.open(rdl_with_region_group), "MainTable")
        member = row_leaves(tablix)[1]
        assert _group_path(member) == "static"
        assert find_child(member, "KeepWithGroup").text == "After"

    def test_nested_groups_footer_lands_after_inner_group(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "Inner", "=1")
        add_row_group(str(rdl_path), "MainTable", "Outer", "=2")
        before = _row_map(rdl_path)
        result = add_subtotal_row(str(rdl_path), "MainTable", "Inner", [])
        after = _row_map(rdl_path)
        assert _without(after, result["textboxes"]) == before
        assert after[result["row_index"]] == ("Outer/Inner", result["textboxes"][0])

    def test_subtotal_column_on_middle_group_keeps_static_columns(self, middle_column_group):
        before = _column_map(middle_column_group)
        result = add_subtotal_column(
            str(middle_column_group),
            "MainTable",
            "CG",
            [{"row": 1, "expression": "=Sum(Fields!Amount.Value)"}],
        )
        after = _column_map(middle_column_group)
        assert _without(after, result["textboxes"]) == before
        assert result["column_index"] == 2
        assert after == [
            ("static", "ProductID"),
            ("CG", "ProductName"),
            ("CG", "CG_TotalColumn_1"),
            ("static", "Amount"),
        ]
        assert "warnings" not in result
        assert validate_report(str(middle_column_group))["valid"]

    def test_subtotal_column_scope_total_is_static_adjacent(self, middle_column_group):
        result = add_subtotal_column(
            str(middle_column_group), "MainTable", "CG", [], position="after", scope="total"
        )
        after = _column_map(middle_column_group)
        assert after == [
            ("static", "ProductID"),
            ("CG", "ProductName"),
            ("static", "CG_TotalColumn_1"),
            ("static", "Amount"),
        ]
        assert result["column_index"] == 2

    def test_subtotal_column_before(self, middle_column_group):
        result = add_subtotal_column(
            str(middle_column_group), "MainTable", "CG", [], position="before"
        )
        assert result["column_index"] == 1
        assert _column_map(middle_column_group)[1] == ("CG", "CG_TotalColumn_1")

    def test_leaf_column_group_needs_scope_total(self, rdl_path):
        _patch(
            rdl_path,
            _COLUMN_MEMBERS,
            f"<TablixMember /><TablixMember>{_group_xml('CG')}</TablixMember><TablixMember />",
        )
        raw = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="scope='total'"):
            add_subtotal_column(str(rdl_path), "MainTable", "CG", [])
        assert rdl_path.read_bytes() == raw
        add_subtotal_column(str(rdl_path), "MainTable", "CG", [], scope="total")
        assert _column_map(rdl_path)[2] == ("static", "CG_TotalColumn_1")

    def test_matrix_column_headers_get_matching_depth(self, rdl_path):
        header = (
            '<TablixHeader><Size>0.3in</Size><CellContents><Textbox Name="{n}"><Paragraphs>'
            "<Paragraph><TextRuns><TextRun><Value /></TextRun></TextRuns></Paragraph>"
            "</Paragraphs></Textbox></CellContents></TablixHeader>"
        )
        _patch(
            rdl_path,
            _COLUMN_MEMBERS,
            "<TablixMember>"
            + header.format(n="H0")
            + f"</TablixMember><TablixMember>{_group_xml('CG')}"
            + header.format(n="H1")
            + "<TablixMembers><TablixMember /></TablixMembers></TablixMember><TablixMember>"
            + header.format(n="H2")
            + "</TablixMember>",
        )
        result = add_subtotal_column(str(rdl_path), "MainTable", "CG", [], scope="total")
        assert result["header_textboxes"] == ["CG_TotalColumnHeader"]
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        new_leaf = column_leaves(tablix)[2]
        assert find_child(find_child(new_leaf, "TablixHeader"), "Size").text == "0.3in"
        assert validate_report(str(rdl_path))["valid"]

    def test_remove_row_group_removes_new_subtotals(self, rdl_with_region_group):
        p = str(rdl_with_region_group)
        add_subtotal_row(p, "MainTable", "Region", [], position="header")
        add_subtotal_row(p, "MainTable", "Region", [])
        remove_row_group(p, "MainTable", "Region")
        assert _row_map(rdl_with_region_group) == [
            ("static", "HeaderProductID"),
            ("Details", "ProductID"),
        ]


class TestToolRegistration:
    def test_tool_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "add_subtotal_row" in server._tools

    def test_add_subtotal_column_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert "add_subtotal_column" in server._tools
