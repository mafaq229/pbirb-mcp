"""Tablix grouping tool tests.

Covers the gnarly Phase-3 commit: adding/removing row groups (which
restructures both ``TablixRowHierarchy`` and ``TablixBody/TablixRows``),
plus the simpler set_group_sort / set_group_visibility editors.

Scope of ``add_row_group`` in this commit: outermost row group only.
Wrapping the entire existing top-level row hierarchy and inserting a
matching group-header row at body row 0. Nested ``parent_group`` is
deliberately deferred — it's a separate kind of restructuring and the
plan flags this commit as the heaviest already.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.tablix_shape import row_leaves
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.reader import get_tablixes
from pbirb_mcp.ops.tablix import (
    add_row_group,
    convert_to_matrix,
    remove_row_group,
    set_group_sort,
    set_group_visibility,
    set_tablix_corner,
)
from pbirb_mcp.ops.tablix_columns import add_column_group
from pbirb_mcp.ops.tablix_static import add_static_row
from pbirb_mcp.ops.tablix_subtotals import add_subtotal_row
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


def _row_hierarchy_top_members(tablix):
    return list(tablix.find(q("TablixRowHierarchy")).find(q("TablixMembers")))


def _body_row_count(tablix) -> int:
    return len(find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow"))


# ---- add_row_group --------------------------------------------------------


class TestAddRowGroup:
    def test_wraps_existing_hierarchy_under_new_outer_member(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        top = _row_hierarchy_top_members(tablix)
        # Exactly one top-level member, which holds the new Region group.
        assert len(top) == 1
        group = top[0].find(q("Group"))
        assert group is not None
        assert group.get("Name") == "Region"
        # The fixture's original two members are preserved as children of the
        # new wrapper, plus a new header-row leaf prepended at index 0.
        wrapped = list(top[0].find(q("TablixMembers")))
        assert len(wrapped) == 3  # group-header leaf + 2 originals

    def test_appends_one_body_row_at_position_zero(self, rdl_path):
        before = RDLDocument.open(rdl_path)
        rows_before = _body_row_count(_tablix(before, "MainTable"))
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        after = RDLDocument.open(rdl_path)
        tablix = _tablix(after, "MainTable")
        assert _body_row_count(tablix) == rows_before + 1
        # The first cell of the new row 0 must hold the group expression.
        first_row = tablix.findall(f"{q('TablixBody')}/{q('TablixRows')}/{q('TablixRow')}")[0]
        first_cell = first_row.findall(f"{q('TablixCells')}/{q('TablixCell')}")[0]
        textrun_value = first_cell.find(
            f"{q('CellContents')}/{q('Textbox')}/{q('Paragraphs')}/"
            f"{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert textrun_value.text == "=Fields!Region.Value"

    def test_get_tablixes_reports_new_group(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        out = get_tablixes(path=str(rdl_path))[0]
        names = [g["name"] for g in out["row_groups"]]
        assert "Region" in names
        # And the GroupExpression is exposed.
        region = next(g for g in out["row_groups"] if g["name"] == "Region")
        assert region["expressions"] == ["=Fields!Region.Value"]

    def test_duplicate_group_name_rejected(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        with pytest.raises(ValueError):
            add_row_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Region",
                group_expression="=Fields!Region.Value",
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_row_group(
                path=str(rdl_path),
                tablix_name="NoSuch",
                group_name="Region",
                group_expression="=Fields!Region.Value",
            )

    def test_parent_group_not_yet_supported(self, rdl_path):
        with pytest.raises(NotImplementedError):
            add_row_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="City",
                group_expression="=Fields!City.Value",
                parent_group="Region",
            )

    def test_round_trip_safe(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()  # structural validate still passes


# ---- remove_row_group ------------------------------------------------------


class TestRemoveRowGroup:
    def test_inverts_add_row_group(self, rdl_path):
        before_doc = RDLDocument.open(rdl_path)
        before_top = [
            etree_node.tag
            for etree_node in _row_hierarchy_top_members(_tablix(before_doc, "MainTable"))
        ]
        before_rows = _body_row_count(_tablix(before_doc, "MainTable"))

        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        remove_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
        )

        after_doc = RDLDocument.open(rdl_path)
        after_top = [n.tag for n in _row_hierarchy_top_members(_tablix(after_doc, "MainTable"))]
        assert after_top == before_top
        assert _body_row_count(_tablix(after_doc, "MainTable")) == before_rows

    def test_remove_unknown_group_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_row_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="NoSuchGroup",
            )

    def test_cannot_remove_details_group(self, rdl_path):
        # The detail group is not a real group with a header row; refuse to
        # remove it (or callers will end up with a tablix that has no leaves).
        with pytest.raises(ValueError):
            remove_row_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Details",
            )


# ---- remove_row_group: the group's own rows, located by leaf (#30) ----------

_DETAIL = '<Group Name="Details" />'
_FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("*.rdl"))


def _leaf_rows(path) -> list[tuple[str, str]]:
    """(leaf group path, first textbox of the bound body row) per row."""
    tablix = _tablix(RDLDocument.open(path), "MainTable")
    rows = find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")
    leaves = row_leaves(tablix)
    assert len(leaves) == len(rows)
    out = []
    for leaf, row in zip(leaves, rows):
        names, node = [], leaf
        while node is not None and node.tag == q("TablixMember"):
            group = find_child(node, "Group")
            if group is not None:
                names.append(group.get("Name"))
            node = node.getparent().getparent()
        out.append(
            ("/".join(reversed(names)) or "static", row.find(f".//{q('Textbox')}").get("Name"))
        )
    return out


def _rb_group(rdl_path: Path, extra_members: str = "") -> Path:
    """Report Builder shape: a Product group wrapping just the data leaf."""
    text = rdl_path.read_text(encoding="utf-8")
    text = text.replace(
        _DETAIL,
        '<Group Name="Product"><GroupExpressions><GroupExpression>=Fields!ProductName.Value'
        "</GroupExpression></GroupExpressions></Group><TablixMembers><TablixMember />"
        f"{extra_members}</TablixMembers>",
    )
    rdl_path.write_text(text, encoding="utf-8")
    return rdl_path


class TestRemoveRowGroupLocatesOwnRows:
    def test_report_builder_group_without_header_deletes_no_row(self, rdl_path):
        _rb_group(rdl_path)
        result = remove_row_group(str(rdl_path), "MainTable", "Product")
        assert result["removed_rows"] == []
        assert _leaf_rows(rdl_path) == [
            ("static", "HeaderProductID"),
            ("static", "ProductID"),
        ]
        tablix = _tablix(RDLDocument.open(rdl_path), "MainTable")
        assert not tablix.findall(f".//{q('Group')}")
        assert validate_report(str(rdl_path))["valid"]

    def test_nested_groups_remove_inner_header_only(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "A", "=1")
        add_row_group(str(rdl_path), "MainTable", "B", "=2")
        result = remove_row_group(str(rdl_path), "MainTable", "A")
        assert result["removed_rows"] == [1]
        assert _leaf_rows(rdl_path) == [
            ("B", "B_Header_0"),
            ("B", "HeaderProductID"),
            ("B/Details", "ProductID"),
        ]
        assert validate_report(str(rdl_path))["valid"]

    def test_nested_groups_remove_outer(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "A", "=1")
        add_row_group(str(rdl_path), "MainTable", "B", "=2")
        remove_row_group(str(rdl_path), "MainTable", "B")
        assert _leaf_rows(rdl_path) == [
            ("A", "A_Header_0"),
            ("A", "HeaderProductID"),
            ("A/Details", "ProductID"),
        ]

    def test_subtotal_rows_removed_with_their_group(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "G", "=1")
        add_subtotal_row(
            str(rdl_path),
            "MainTable",
            "G",
            [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        add_subtotal_row(str(rdl_path), "MainTable", "G", [], position="header")
        remove_row_group(str(rdl_path), "MainTable", "G")
        rows = _leaf_rows(rdl_path)
        assert rows == [("static", "HeaderProductID"), ("Details", "ProductID")]
        assert "G_" not in rdl_path.read_text(encoding="utf-8")

    def test_keep_subtotals(self, rdl_path):
        add_row_group(str(rdl_path), "MainTable", "G", "=1")
        add_subtotal_row(
            str(rdl_path),
            "MainTable",
            "G",
            [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        footer_row = len(_leaf_rows(rdl_path)) - 1
        result = remove_row_group(str(rdl_path), "MainTable", "G", keep_subtotals=True)
        assert result["kept_subtotal_rows"] == [footer_row]
        rows = _leaf_rows(rdl_path)
        assert rows[:2] == [("static", "HeaderProductID"), ("Details", "ProductID")]
        assert len(rows) == 3 and rows[2][0] == "static"

    def test_report_builder_total_is_kept_with_warning(self, rdl_path):
        # RB "Add Total > After" inside the group: a static leaf with
        # KeepWithGroup=Before after the data, plus its own body row.
        _rb_group(
            rdl_path,
            extra_members="<TablixMember><KeepWithGroup>Before</KeepWithGroup></TablixMember>",
        )
        doc = RDLDocument.open(rdl_path)
        rows_root = _tablix(doc, "MainTable").find(f"{q('TablixBody')}/{q('TablixRows')}")
        total = copy.deepcopy(find_children(rows_root, "TablixRow")[-1])
        for tb in total.iter(q("Textbox")):
            tb.set("Name", "Total" + tb.get("Name"))
        rows_root.append(total)
        doc.save()
        assert _leaf_rows(rdl_path)[-1] == ("Product", "TotalProductID")

        # Not tool-emitted, so kept (RB uses the same marker for table
        # footers) — but the payload warns that its scope changed.
        result = remove_row_group(str(rdl_path), "MainTable", "Product")
        assert result["removed_rows"] == []
        assert "KeepWithGroup=Before" in result["warnings"][0]
        assert _leaf_rows(rdl_path) == [
            ("static", "HeaderProductID"),
            ("static", "ProductID"),
            ("static", "TotalProductID"),
        ]
        assert validate_report(str(rdl_path))["valid"]

    def test_report_builder_group_with_subtotal_keeps_data_row(self, rdl_path):
        # Product[data leaf, subtotal]: the lone data leaf is not a header.
        _rb_group(rdl_path)
        add_subtotal_row(str(rdl_path), "MainTable", "Product", [])
        result = remove_row_group(str(rdl_path), "MainTable", "Product")
        assert result["removed_rows"] == [2]
        assert _leaf_rows(rdl_path) == [("static", "HeaderProductID"), ("static", "ProductID")]

    def test_wrapped_table_footer_is_kept(self, rdl_path):
        # A plain static footer the group wrapped is not a subtotal.
        add_static_row(str(rdl_path), "MainTable", "TableFooter", ["end"])
        add_row_group(str(rdl_path), "MainTable", "G", "=1")
        result = remove_row_group(str(rdl_path), "MainTable", "G")
        assert result["removed_rows"] == [0]
        assert _leaf_rows(rdl_path)[-1] == ("static", "TableFooter")

    def test_group_that_is_its_own_leaf_refused(self, rdl_path):
        text = rdl_path.read_text(encoding="utf-8").replace(
            _DETAIL,
            '<Group Name="Leafy"><GroupExpressions><GroupExpression>=1'
            "</GroupExpression></GroupExpressions></Group>",
        )
        rdl_path.write_text(text, encoding="utf-8")
        raw = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="owns a body row"):
            remove_row_group(str(rdl_path), "MainTable", "Leafy")
        assert rdl_path.read_bytes() == raw

    def test_group_holding_only_subtotals_refused(self, rdl_path):
        _rb_group(rdl_path)
        text = rdl_path.read_text(encoding="utf-8")
        for name in ("ProductID", "ProductName", "Amount"):
            text = text.replace(f'Name="{name}"', f'Name="Product_SubtotalFooter_{name}"')
        rdl_path.write_text(text, encoding="utf-8")
        raw = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="only its own header/subtotal rows"):
            remove_row_group(str(rdl_path), "MainTable", "Product")
        assert rdl_path.read_bytes() == raw
        # keep_subtotals keeps the row, so there is something to unwrap.
        remove_row_group(str(rdl_path), "MainTable", "Product", keep_subtotals=True)
        assert len(_leaf_rows(rdl_path)) == 2

    @pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.name)
    def test_add_then_remove_is_byte_identical(self, tmp_path, fixture):
        dest = tmp_path / fixture.name
        shutil.copy(fixture, dest)
        raw = dest.read_bytes()
        for tablix in RDLDocument.open(dest).root.iter(f"{{{RDL_NS}}}Tablix"):
            name = tablix.get("Name")
            add_row_group(str(dest), name, "RoundTripGroup", "=1")
            remove_row_group(str(dest), name, "RoundTripGroup")
        assert dest.read_bytes() == raw


# ---- set_group_sort -------------------------------------------------------


class TestSetGroupSort:
    def test_creates_sort_expressions_when_absent(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_group_sort(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            sort_expressions=["=Fields!Region.Value"],
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        # Find the TablixMember that holds Region.
        member = None
        for m in tablix.iter(q("TablixMember")):
            g = m.find(q("Group"))
            if g is not None and g.get("Name") == "Region":
                member = m
                break
        assert member is not None
        sorts_root = find_child(member, "SortExpressions")
        assert sorts_root is not None
        values = [s.find(q("Value")).text for s in find_children(sorts_root, "SortExpression")]
        assert values == ["=Fields!Region.Value"]

    def test_replaces_existing_sort_expressions(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_group_sort(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            sort_expressions=["=Fields!Region.Value"],
        )
        set_group_sort(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            sort_expressions=["=Fields!Amount.Value", "=Fields!ProductName.Value"],
        )
        out = get_tablixes(path=str(rdl_path))[0]
        # sort_expressions are returned as a flat list across the tablix.
        assert "=Fields!Amount.Value" in out["sort_expressions"]
        assert "=Fields!ProductName.Value" in out["sort_expressions"]
        # The old "Region" sort must be gone.
        assert "=Fields!Region.Value" not in out["sort_expressions"]

    def test_unknown_group_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_group_sort(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Ghost",
                sort_expressions=["=1"],
            )


# ---- set_group_visibility -------------------------------------------------


class TestSetGroupVisibility:
    def test_sets_hidden_expression(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        set_group_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            visibility_expression="=Parameters!HideRegion.Value",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        member = next(
            m
            for m in tablix.iter(q("TablixMember"))
            if m.find(q("Group")) is not None and m.find(q("Group")).get("Name") == "Region"
        )
        vis = find_child(member, "Visibility")
        assert vis is not None
        hidden = find_child(vis, "Hidden")
        assert hidden.text == "=Parameters!HideRegion.Value"
        assert find_child(vis, "ToggleItem") is None

    def test_with_toggle_textbox(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!Region.Value",
        )
        # add_row_group currently wraps the header row too, so a header cell
        # would sit in the rows the group hides — toggle from the body.
        from pbirb_mcp.ops.body import add_body_textbox

        add_body_textbox(str(rdl_path), "HeaderProductID2", "+", "0in", "6in", "0.3in", "0.25in")
        set_group_visibility(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            visibility_expression="true",
            toggle_textbox="HeaderProductID2",
        )
        doc = RDLDocument.open(rdl_path)
        tablix = _tablix(doc, "MainTable")
        member = next(
            m
            for m in tablix.iter(q("TablixMember"))
            if m.find(q("Group")) is not None and m.find(q("Group")).get("Name") == "Region"
        )
        vis = find_child(member, "Visibility")
        assert find_child(vis, "ToggleItem").text == "HeaderProductID2"

    def test_unknown_group_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_group_visibility(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Ghost",
                visibility_expression="true",
            )

    def test_refuses_column_axis_group(self, rdl_path):
        """Symmetric guard: row-axis tools refuse a group living only on the
        column axis (mirrors how set_column_group_visibility rejects row-axis
        groups). Hint message points at the column-axis sibling tool."""
        from pbirb_mcp.ops.tablix_columns import add_column_group

        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Quarter",
            group_expression="=Fields!Quarter.Value",
        )
        with pytest.raises(ElementNotFoundError) as exc:
            set_group_visibility(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Quarter",
                visibility_expression="true",
            )
        assert "column-axis" in str(exc.value) or "set_column_group_visibility" in str(exc.value)


class TestRowAxisGuards:
    def test_set_group_sort_refuses_column_axis(self, rdl_path):
        from pbirb_mcp.ops.tablix_columns import add_column_group

        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Quarter",
            group_expression="=Fields!Quarter.Value",
        )
        with pytest.raises(ElementNotFoundError) as exc:
            set_group_sort(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Quarter",
                sort_expressions=["=1"],
            )
        assert "set_column_group_sort" in str(exc.value)

    def test_remove_row_group_refuses_column_axis(self, rdl_path):
        from pbirb_mcp.ops.tablix_columns import add_column_group

        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Quarter",
            group_expression="=Fields!Quarter.Value",
        )
        with pytest.raises(ElementNotFoundError) as exc:
            remove_row_group(
                path=str(rdl_path),
                tablix_name="MainTable",
                group_name="Quarter",
            )
        assert "remove_column_group" in str(exc.value)


# ---- convert_to_matrix (v0.4 commit 17) -----------------------------------


class TestConvertToMatrix:
    """Phase F commit 17 — convert_to_matrix drops the residual Details
    row group + body row from a tablix that has both a named row group
    and a named column group. The 2026-05-11 matrix-report session
    feedback gap #1 motivated this.

    remove_row_group refuses Details for safety (Detail-table case);
    convert_to_matrix is the explicit verb whose pre-conditions
    (named row group + named column group both exist) make removing
    Details safe.
    """

    @pytest.fixture
    def matrix_path(self, rdl_path):
        """Fixture: add_row_group('Region') + add_column_group('Date')
        so the tablix is in the standard 3-row × N-col post-add state
        with Details still present."""
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!ProductName.Value",
        )
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Date",
            group_expression="=Fields!ProductID.Value",
        )
        return rdl_path

    def _details_member(self, doc: RDLDocument, tablix_name: str):
        tablix = _tablix(doc, tablix_name)
        for member in tablix.iter(f"{{{RDL_NS}}}TablixMember"):
            group = find_child(member, "Group")
            if group is not None and group.get("Name") == "Details":
                return member
        return None

    def test_removes_details_member_and_body_row(self, matrix_path):
        doc_before = RDLDocument.open(matrix_path)
        rows_before = _body_row_count(_tablix(doc_before, "MainTable"))
        assert self._details_member(doc_before, "MainTable") is not None

        result = convert_to_matrix(
            path=str(matrix_path),
            tablix_name="MainTable",
            row_group="Region",
            column_group="Date",
        )

        # Canonical mutator shape.
        assert result["tablix"] == "MainTable"
        assert result["kind"] == "Tablix"
        assert "details_member_removed" in result["changed"]
        assert "details_body_row_removed" in result["changed"]

        doc_after = RDLDocument.open(matrix_path)
        assert self._details_member(doc_after, "MainTable") is None
        # Exactly one body row dropped.
        assert _body_row_count(_tablix(doc_after, "MainTable")) == rows_before - 1

    def test_round_trip_valid(self, matrix_path):
        convert_to_matrix(
            path=str(matrix_path),
            tablix_name="MainTable",
            row_group="Region",
            column_group="Date",
        )
        # Bundled XSD + structural validators both pass.
        RDLDocument.open(matrix_path).validate()

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            convert_to_matrix(
                path=str(rdl_path),
                tablix_name="NoSuchTable",
                row_group="Region",
                column_group="Date",
            )

    def test_missing_row_group_refused(self, rdl_path):
        # No add_row_group yet — only Details exists in row hierarchy.
        with pytest.raises(ValueError, match="row group"):
            convert_to_matrix(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_group="Region",
                column_group="Date",
            )

    def test_missing_column_group_refused(self, rdl_path):
        # add_row_group present but no column group.
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!ProductName.Value",
        )
        with pytest.raises(ValueError, match="column group"):
            convert_to_matrix(
                path=str(rdl_path),
                tablix_name="MainTable",
                row_group="Region",
                column_group="Date",
            )

    def test_idempotent_second_call_refuses(self, matrix_path):
        convert_to_matrix(
            path=str(matrix_path),
            tablix_name="MainTable",
            row_group="Region",
            column_group="Date",
        )
        # Second call: Details is gone, refuse with clear "already
        # matrix" hint.
        with pytest.raises(ValueError, match="already a matrix|no Details"):
            convert_to_matrix(
                path=str(matrix_path),
                tablix_name="MainTable",
                row_group="Region",
                column_group="Date",
            )


# ---- set_tablix_corner (v0.4 commit 18) -----------------------------------


class TestSetTablixCorner:
    """Phase F commit 18 — set_tablix_corner writes the <TablixCorner>
    block with a single textbox at the top-left of a matrix-shaped
    tablix. Required after convert_to_matrix (commit 17) — the corner
    is where the LLM places the "Type" / "Region" caption.
    """

    @pytest.fixture
    def matrix_path(self, rdl_path):
        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!ProductName.Value",
        )
        add_column_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Date",
            group_expression="=Fields!ProductID.Value",
        )
        return rdl_path

    def _corner(self, doc, tablix_name):
        tablix = _tablix(doc, tablix_name)
        return find_child(tablix, "TablixCorner")

    def test_writes_corner_with_literal_text(self, matrix_path):
        result = set_tablix_corner(
            path=str(matrix_path),
            tablix_name="MainTable",
            text="Type",
        )
        assert result["tablix"] == "MainTable"
        assert result["kind"] == "TablixCorner"
        assert result["name"] == "MainTable_Corner"
        assert "corner_written" in result["changed"]

        doc = RDLDocument.open(matrix_path)
        corner = self._corner(doc, "MainTable")
        assert corner is not None
        # Value text lands in the textbox.
        value_node = corner.find(f".//{{{RDL_NS}}}Value")
        assert value_node is not None
        assert value_node.text == "Type"
        # Textbox name is deterministic.
        tb = corner.find(f".//{{{RDL_NS}}}Textbox")
        assert tb.get("Name") == "MainTable_Corner"

    def test_writes_corner_with_expression(self, matrix_path):
        set_tablix_corner(
            path=str(matrix_path),
            tablix_name="MainTable",
            expression="=Fields!Category.Value",
        )
        doc = RDLDocument.open(matrix_path)
        value_node = self._corner(doc, "MainTable").find(f".//{{{RDL_NS}}}Value")
        assert value_node.text == "=Fields!Category.Value"

    def test_text_and_expression_mutually_exclusive(self, matrix_path):
        with pytest.raises(ValueError, match="mutually exclusive"):
            set_tablix_corner(
                path=str(matrix_path),
                tablix_name="MainTable",
                text="a",
                expression="=b",
            )

    def test_neither_text_nor_expression_rejected(self, matrix_path):
        with pytest.raises(ValueError, match="either text or expression"):
            set_tablix_corner(path=str(matrix_path), tablix_name="MainTable")

    def test_refuses_without_column_group(self, rdl_path):
        # Pristine tablix — no column group exists.
        with pytest.raises(ValueError, match="no named column group"):
            set_tablix_corner(
                path=str(rdl_path),
                tablix_name="MainTable",
                text="Type",
            )

    def test_replace_existing_corner_is_idempotent_on_payload(self, matrix_path):
        set_tablix_corner(path=str(matrix_path), tablix_name="MainTable", text="Type")
        result = set_tablix_corner(
            path=str(matrix_path),
            tablix_name="MainTable",
            text="Category",
        )
        # Second call signals it replaced an existing block.
        assert "replaced_existing" in result["changed"]
        doc = RDLDocument.open(matrix_path)
        value_node = self._corner(doc, "MainTable").find(f".//{{{RDL_NS}}}Value")
        assert value_node.text == "Category"

    def test_corner_placed_before_tablix_body(self, matrix_path):
        """Per RDL XSD, TablixCorner is the first Tablix child slot —
        must come before TablixBody and the hierarchies."""
        set_tablix_corner(path=str(matrix_path), tablix_name="MainTable", text="X")
        doc = RDLDocument.open(matrix_path)
        tablix = _tablix(doc, "MainTable")
        children = [etree.QName(c.tag).localname for c in tablix]
        corner_idx = children.index("TablixCorner")
        body_idx = children.index("TablixBody")
        assert corner_idx < body_idx

    def test_round_trip_valid(self, matrix_path):
        set_tablix_corner(path=str(matrix_path), tablix_name="MainTable", text="Type")
        RDLDocument.open(matrix_path).validate()

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_tablix_corner(path=str(rdl_path), tablix_name="NoSuchTable", text="X")


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_grouping_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {
            "add_row_group",
            "remove_row_group",
            "set_group_sort",
            "set_group_visibility",
            "convert_to_matrix",
            "set_tablix_corner",
        } <= names
