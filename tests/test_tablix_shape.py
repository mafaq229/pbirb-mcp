"""Tests for structural detail-group detection (issue #23).

Report Builder names the detail group ``Details`` only for the first table
in a report — group names are report-wide unique, so a second table gets
``Details1``, a list gets ``<list>_Details``, and authors rename them. The
detail group is therefore identified by shape (a leaf ``<Group>`` without
``<GroupExpressions>``), never by name. Every test that touches a tool is
parametrised over ``Details``, ``Details1`` and ``DriverList_Details``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.tablix_shape import (
    NO_DETAIL_GROUP,
    data_leaf,
    data_row_index,
    detail_member,
    detail_row_index,
    is_detail_group,
    is_dynamic_group,
    leaf_row_index,
    row_leaf,
    row_leaves,
)
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.reader import describe_report
from pbirb_mcp.ops.styling import (
    set_alternating_row_color,
    set_conditional_row_color,
    style_tablix_row,
)
from pbirb_mcp.ops.tablix import (
    add_row_group,
    convert_to_matrix,
    remove_row_group,
    set_detail_row_visibility,
)
from pbirb_mcp.ops.tablix_columns import add_column_group, add_tablix_column
from pbirb_mcp.ops.tablix_static import add_static_row
from pbirb_mcp.ops.tablix_subtotals import add_subtotal_row

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
DETAIL_NAMES = ["Details", "Details1", "DriverList_Details"]
_DETAIL_ELEMENT = '<Group Name="Details" />'


def _write_fixture(tmp_path: Path, replacement: str) -> Path:
    text = FIXTURE.read_text(encoding="utf-8")
    assert text.count(_DETAIL_ELEMENT) == 1
    dest = tmp_path / "report.rdl"
    dest.write_text(text.replace(_DETAIL_ELEMENT, replacement), encoding="utf-8")
    return dest


@pytest.fixture(params=DETAIL_NAMES)
def detail_name(request) -> str:
    return request.param


@pytest.fixture
def rdl_path(tmp_path: Path, detail_name: str) -> Path:
    """Minimal fixture with its detail group renamed to ``detail_name``."""
    return _write_fixture(tmp_path, f'<Group Name="{detail_name}" />')


def _tablix(path: Path, name: str = "MainTable"):
    doc = RDLDocument.open(path)
    return doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='{name}']")


def _rows(tablix):
    body = find_child(tablix, "TablixBody")
    return find_children(find_child(body, "TablixRows"), "TablixRow")


def _row_textbox_names(row):
    return [tb.get("Name") for tb in row.iter(q("Textbox"))]


def _matrix(path: Path, wrap: str = "details") -> Path:
    """add_row_group + add_column_group + convert_to_matrix: a tablix
    with no detail group whose innermost dynamic row group is Region —
    itself the leaf owning the (aggregated) data row."""
    add_row_group(str(path), "MainTable", "Region", "=Fields!ProductName.Value", wrap=wrap)
    add_column_group(str(path), "MainTable", "Date", "=Fields!ProductID.Value")
    convert_to_matrix(str(path), "MainTable", "Region", "Date")
    return path


def _footer_matrix(path: Path) -> Path:
    """:func:`_matrix` with a static footer row added first and wrapped
    too (``wrap="all"``), so Region wraps static members: its data row
    and the footer."""
    add_static_row(str(path), "MainTable", "TotalRow", ["Total"])
    return _matrix(path, wrap="all")


# ---- core helpers ---------------------------------------------------------


class TestHelpers:
    def test_detail_member_found_whatever_its_name(self, rdl_path, detail_name):
        tablix = _tablix(rdl_path)
        member = detail_member(tablix)
        assert member is not None
        assert find_child(member, "Group").get("Name") == detail_name
        assert detail_row_index(tablix) == 1
        assert data_leaf(tablix) is member
        assert data_row_index(tablix) == 1

    def test_group_classification(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        tablix = _tablix(rdl_path)
        groups = {g.get("Name"): g for g in tablix.iter(q("Group"))}
        assert is_dynamic_group(groups["Region"])
        assert not is_detail_group(groups["Region"])
        detail = find_child(detail_member(tablix), "Group")
        assert is_detail_group(detail)
        assert not is_dynamic_group(detail)
        assert not is_detail_group(None)
        assert not is_dynamic_group(None)

    def test_dynamic_group_named_details_is_not_the_detail_group(self, tmp_path):
        path = _write_fixture(
            tmp_path,
            '<Group Name="Details"><GroupExpressions>'
            "<GroupExpression>=Fields!ProductID.Value</GroupExpression>"
            "</GroupExpressions></Group>",
        )
        tablix = _tablix(path)
        assert detail_member(tablix) is None
        assert detail_row_index(tablix) is None
        # ...but it is the innermost dynamic group, so it is the data leaf.
        leaf = data_leaf(tablix)
        assert find_child(leaf, "Group").get("Name") == "Details"
        assert data_row_index(tablix) == 1

    def test_leaf_row_mapping_after_nesting(self, rdl_path, detail_name):
        # Region wraps [Region header, column header, detail]; a footer
        # subtotal appends a 4th leaf. Leaves map 1:1 onto body rows.
        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_subtotal_row(str(rdl_path), "MainTable", "Region", [], position="footer")
        tablix = _tablix(rdl_path)
        leaves = row_leaves(tablix)
        assert len(leaves) == len(_rows(tablix)) == 4
        for idx, leaf in enumerate(leaves):
            assert leaf_row_index(tablix, leaf) == idx
            assert row_leaf(tablix, idx) is leaf
        assert row_leaf(tablix, 4) is None
        assert row_leaf(tablix, -1) is None
        # The detail leaf is row 2 — NOT the last row (the subtotal).
        assert detail_row_index(tablix) == 2
        assert find_child(leaves[2], "Group").get("Name") == detail_name

    def test_leaf_row_index_of_non_leaf_is_none(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        tablix = _tablix(rdl_path)
        wrapper = next(
            m
            for m in tablix.iter(q("TablixMember"))
            if find_child(m, "Group") is not None and find_child(m, "Group").get("Name") == "Region"
        )
        assert leaf_row_index(tablix, wrapper) is None

    def test_data_leaf_scoped_to_group(self, rdl_path, detail_name):
        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        tablix = _tablix(rdl_path)
        assert data_leaf(tablix, "Region") is detail_member(tablix)
        assert data_leaf(tablix, "NoSuchGroup") is None

    def test_matrix_data_leaf_is_innermost_groups_leaf(self, rdl_path):
        _matrix(rdl_path)
        tablix = _tablix(rdl_path)
        assert detail_member(tablix) is None
        # Region wraps [Region header, column header]; its first leaf is
        # the group-header row at body row 0.
        assert data_row_index(tablix) == 0
        assert data_row_index(tablix, "Region") == 0

    def test_innermost_of_nested_groups_wins(self, tmp_path):
        path = _write_fixture(
            tmp_path,
            '<Group Name="Product"><GroupExpressions>'
            "<GroupExpression>=Fields!ProductID.Value</GroupExpression>"
            "</GroupExpressions></Group>",
        )
        # Region wraps [Region header, column header, Product(leaf)].
        add_row_group(str(path), "MainTable", "Region", "=Fields!ProductName.Value")
        tablix = _tablix(path)
        leaf = data_leaf(tablix)
        assert find_child(leaf, "Group").get("Name") == "Product"
        assert data_row_index(tablix) == 2
        assert data_row_index(tablix, "Region") == 2
        assert data_row_index(tablix, "Product") == 2

    def test_all_static_tablix_has_no_data_leaf(self, tmp_path):
        path = _write_fixture(tmp_path, "")
        tablix = _tablix(path)
        assert detail_member(tablix) is None
        assert data_leaf(tablix) is None
        assert data_row_index(tablix) is None


# ---- tools route through the helper (issue repro) -------------------------


class TestDetailTools:
    def test_set_detail_row_visibility(self, rdl_path, detail_name):
        set_detail_row_visibility(str(rdl_path), "MainTable", "=True")
        member = detail_member(_tablix(rdl_path))
        hidden = member.find(f"{q('Visibility')}/{q('Hidden')}")
        assert hidden is not None and hidden.text == "=True"
        assert find_child(member, "Group").get("Name") == detail_name

    def test_set_alternating_row_color(self, rdl_path):
        result = set_alternating_row_color(str(rdl_path), "MainTable", "White", "Gray")
        assert result["row_index"] == 1
        assert result["cells"] == ["ProductID", "ProductName", "Amount"]

    def test_set_conditional_row_color(self, rdl_path):
        result = set_conditional_row_color(
            str(rdl_path), "MainTable", "Fields!Amount.Value", {"0": "Red"}
        )
        assert result["row_index"] == 1
        assert result["cells"] == ["ProductID", "ProductName", "Amount"]

    def test_style_tablix_row_details(self, rdl_path):
        result = style_tablix_row(str(rdl_path), "MainTable", "details", font_weight="Bold")
        assert result["row_index"] == 1
        assert result["cells"] == ["ProductID", "ProductName", "Amount"]

    def test_add_tablix_column_targets_detail_row(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_subtotal_row(str(rdl_path), "MainTable", "Region", [], position="footer")
        add_tablix_column(str(rdl_path), "MainTable", "Qty", "=Fields!Qty.Value")
        rows = _rows(_tablix(rdl_path))
        # Detail row is body row 2; the trailing subtotal row stays blank.
        assert "Qty" in _row_textbox_names(rows[2])
        assert "Qty" not in _row_textbox_names(rows[3])

    def test_add_subtotal_row_resolves_detail_row_names(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_subtotal_row(
            str(rdl_path),
            "MainTable",
            "Region",
            [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        last = _rows(_tablix(rdl_path))[-1]
        values = [v.text for v in last.iter(q("Value"))]
        assert values[2] == "=Sum(Fields!Amount.Value)"

    def test_convert_to_matrix_keeps_detail_row_and_trailing_subtotal(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_column_group(str(rdl_path), "MainTable", "Date", "=Fields!ProductID.Value")
        add_subtotal_row(
            str(rdl_path),
            "MainTable",
            "Region",
            [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        before = _rows(_tablix(rdl_path))
        detail_names = _row_textbox_names(before[2])
        assert "Amount" in detail_names

        convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")

        tablix = _tablix(rdl_path)
        after = _rows(tablix)
        assert detail_member(tablix) is None
        # The two caption rows went (#31); the detail row is now Region's
        # aggregated data row and the subtotal row (formerly last) survived.
        assert [_row_textbox_names(r) for r in after] == [["Amount"], ["Region_SubtotalFooter_2"]]
        assert [v.text for v in after[0].iter(q("Value"))] == ["=Sum(Fields!Amount.Value)"]
        assert len(row_leaves(tablix)) == len(after)
        RDLDocument.open(rdl_path).validate()

    def test_convert_to_matrix_second_call_refuses(self, rdl_path):
        _matrix(rdl_path)
        with pytest.raises(ValueError, match="already a matrix") as excinfo:
            convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")
        assert NO_DETAIL_GROUP in str(excinfo.value)

    def test_remove_row_group_refuses_detail_group(self, rdl_path, detail_name):
        with pytest.raises(ValueError, match="detail group"):
            remove_row_group(str(rdl_path), "MainTable", detail_name)

    def test_describe_report_detail_group_is_not_a_group(self, rdl_path):
        shapes = describe_report(str(rdl_path))["tablixes"]
        assert shapes[0]["has_groups"] is False


class TestConvertToMatrixSoleChild:
    def test_detail_as_only_child_collapses_into_group(self, tmp_path, detail_name):
        # Report Builder's native group shape: the dynamic group's only
        # child is the detail leaf. Removing it must not leave an empty
        # <TablixMembers/>; the group becomes the leaf and keeps the row.
        path = _write_fixture(
            tmp_path,
            '<Group Name="Product"><GroupExpressions>'
            "<GroupExpression>=Fields!ProductID.Value</GroupExpression>"
            "</GroupExpressions></Group>"
            f'<TablixMembers><TablixMember><Group Name="{detail_name}" />'
            "</TablixMember></TablixMembers>",
        )
        add_column_group(str(path), "MainTable", "Date", "=Fields!ProductID.Value")
        assert len(_rows(_tablix(path))) == 2  # column titles + detail

        result = convert_to_matrix(str(path), "MainTable", "Product", "Date")

        assert "details_member_removed" in result["changed"]
        tablix = _tablix(path)
        assert detail_member(tablix) is None
        # The column-title row goes (#31); the detail row stays as the
        # group's own row.
        assert len(_rows(tablix)) == 1
        assert len(row_leaves(tablix)) == 1
        assert find_child(data_leaf(tablix), "Group").get("Name") == "Product"
        assert find_child(data_leaf(tablix), "TablixMembers") is None
        RDLDocument.open(path).validate()


# ---- errors name the structural rule --------------------------------------


class TestErrors:
    def _matrix_without_dynamic(self, tmp_path):
        return _write_fixture(tmp_path, "")

    @pytest.mark.parametrize(
        "call",
        [
            lambda p: set_detail_row_visibility(p, "MainTable", "=True"),
            lambda p: set_alternating_row_color(p, "MainTable", "White", "Gray"),
            lambda p: set_conditional_row_color(p, "MainTable", "Fields!Amount.Value", {"0": "R"}),
            lambda p: style_tablix_row(p, "MainTable", "details", font_weight="Bold"),
        ],
        ids=["visibility", "alternating", "conditional", "style_row"],
    )
    def test_no_detail_group_error_message(self, tmp_path, call):
        path = self._matrix_without_dynamic(tmp_path)
        with pytest.raises(ElementNotFoundError, match="no detail group") as excinfo:
            call(str(path))
        assert NO_DETAIL_GROUP in str(excinfo.value)


# ---- matrix: innermost group's leaf row -----------------------------------


class TestMatrix:
    def test_style_tablix_row_details_uses_innermost_group_row(self, rdl_path):
        _matrix(rdl_path)
        result = style_tablix_row(str(rdl_path), "MainTable", "details", font_weight="Bold")
        assert result["row_index"] == 0
        assert result["cells"] == ["Amount"]

    def test_add_subtotal_row_uses_groups_own_leaf_row(self, rdl_path):
        _footer_matrix(rdl_path)
        # First subtotal: aggregate against the Region row's textbox.
        add_subtotal_row(
            str(rdl_path),
            "MainTable",
            "Region",
            [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        # Second subtotal: the last row is now the first subtotal; the
        # data-row names must still come from Region's own leaf row.
        add_subtotal_row(
            str(rdl_path),
            "MainTable",
            "Region",
            [{"column": "Amount", "expression": "=Count(Fields!Amount.Value)"}],
            position="header",
        )
        tablix = _tablix(rdl_path)
        assert len(row_leaves(tablix)) == len(_rows(tablix))

    def test_add_subtotal_row_rejects_last_row_names(self, rdl_path):
        _footer_matrix(rdl_path)
        add_subtotal_row(str(rdl_path), "MainTable", "Region", [], position="footer")
        # Region_Footer_* are the trailing subtotal's names, not data-row
        # names — the old last-row fallback would have accepted them.
        with pytest.raises(ElementNotFoundError, match=r"\[.ProductID., .Amount.\]"):
            add_subtotal_row(
                str(rdl_path),
                "MainTable",
                "Region",
                [{"column": "Region_Footer_1", "expression": "=Sum(Fields!Amount.Value)"}],
            )

    def test_add_tablix_column_uses_innermost_group_row(self, rdl_path):
        _matrix(rdl_path)
        add_tablix_column(str(rdl_path), "MainTable", "Qty", "=Fields!Qty.Value")
        rows = _rows(_tablix(rdl_path))
        assert "Qty" in _row_textbox_names(rows[0])


# ---- column-hierarchy leaves (#33) ------------------------------------------


def test_column_leaves_match_body_columns_on_fixture():
    from pbirb_mcp.core.tablix_shape import column_leaf, column_leaves, leaf_column_index

    doc = RDLDocument.open(FIXTURE)
    tablix = next(doc.root.iter(f"{{{RDL_NS}}}Tablix"))
    leaves = column_leaves(tablix)
    cols = list(tablix.iter(f"{{{RDL_NS}}}TablixColumn"))
    assert len(leaves) == len(cols)
    for i, leaf in enumerate(leaves):
        assert leaf_column_index(tablix, leaf) == i
        assert column_leaf(tablix, i) is leaf
    assert column_leaf(tablix, len(leaves)) is None
    assert column_leaf(tablix, -1) is None


def test_column_leaves_descend_into_groups(tmp_path):
    from pbirb_mcp.core.tablix_shape import column_leaves, subtree_leaves

    member = (
        f'<TablixColumnHierarchy xmlns="{RDL_NS}"><TablixMembers>'
        "<TablixMember><Group Name='G'><GroupExpressions><GroupExpression>=1"
        "</GroupExpression></GroupExpressions></Group><TablixMembers>"
        "<TablixMember/><TablixMember/></TablixMembers></TablixMember>"
        "<TablixMember/></TablixMembers></TablixColumnHierarchy>"
    )
    from lxml import etree

    tablix = etree.fromstring(f'<Tablix xmlns="{RDL_NS}">{member}</Tablix>')
    leaves = column_leaves(tablix)
    assert len(leaves) == 3
    group_member = tablix.find(f".//{{{RDL_NS}}}TablixMember")
    assert subtree_leaves(group_member) == leaves[:2]
