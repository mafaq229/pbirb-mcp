"""Leaf-indexed insertion of static rows / columns (#33).

Body row/column *i* is owned by hierarchy leaf *i* (depth-first). Inserting
a body row at index *k* must therefore put its member at *leaf* position
*k*: putting it at *top-level* position *k* (the old behaviour) re-binds
every later row/column to a different leaf as soon as a group wraps
members — the counts stay equal, so nothing notices.

The core check throughout: every pre-existing body row/column keeps the
leaf (identified by its group path) it was bound to before the insert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.tablix_shape import column_leaves, insertion_point, row_leaves
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.tablix import add_row_group
from pbirb_mcp.ops.tablix_columns import add_tablix_column
from pbirb_mcp.ops.tablix_static import add_static_column, add_static_row
from pbirb_mcp.ops.validate import validate_report

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
_COLUMN_MEMBERS = (
    "<TablixMember />\n                <TablixMember />\n                <TablixMember />"
)
_DETAIL = '<Group Name="Details" />'


def _group_xml(name: str, expr: str = "=1") -> str:
    return (
        f'<Group Name="{name}"><GroupExpressions><GroupExpression>{expr}'
        "</GroupExpression></GroupExpressions></Group>"
    )


def _textbox_xml(name: str) -> str:
    return (
        f'<Textbox Name="{name}"><Paragraphs><Paragraph><TextRuns><TextRun><Value />'
        "</TextRun></TextRuns></Paragraph></Paragraphs></Textbox>"
    )


def _header_xml(name: str, size: str = "0.25in") -> str:
    return (
        f"<TablixHeader><Size>{size}</Size><CellContents>{_textbox_xml(name)}"
        "</CellContents></TablixHeader>"
    )


def _copy(tmp_path: Path, replacements: tuple[tuple[str, str], ...] = ()) -> Path:
    text = FIXTURE.read_text(encoding="utf-8")
    for old, new in replacements:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    tmp_path.mkdir(parents=True, exist_ok=True)
    dest = tmp_path / "report.rdl"
    dest.write_text(text, encoding="utf-8")
    return dest


# ---- shapes ----------------------------------------------------------------


def _shape_plain(tmp_path):
    return _copy(tmp_path)


def _shape_one_row_group(tmp_path):
    # wrap="all": G wraps the header row too (the pre-#65 default), so
    # positions strictly inside a group with static members are covered.
    p = _copy(tmp_path)
    add_row_group(str(p), "MainTable", "G", "=1", wrap="all")
    return p


def _shape_nested_row_groups(tmp_path):
    p = _copy(tmp_path)
    add_row_group(str(p), "MainTable", "A", "=1")
    add_row_group(str(p), "MainTable", "B", "=2")
    return p


def _shape_rb_row_group(tmp_path):
    # Report Builder shape: header / Product group (wrapping the detail
    # leaf) / trailing static grand total.
    p = _copy(
        tmp_path,
        (
            (
                "<TablixMember>\n                  "
                + _DETAIL
                + "\n                </TablixMember>",
                "<TablixMember>"
                + _group_xml("Product", "=Fields!ProductName.Value")
                + "<TablixMembers><TablixMember>"
                + _DETAIL
                + "</TablixMember></TablixMembers></TablixMember>",
            ),
        ),
    )
    add_static_row(str(p), "MainTable", "GrandTotal", ["Total"])
    return p


ROW_SHAPES = {
    "plain": _shape_plain,
    "one_group": _shape_one_row_group,
    "nested_groups": _shape_nested_row_groups,
    "rb_group": _shape_rb_row_group,
}


def _shape_col_group_middle(tmp_path):
    # Column group CG wraps body columns 1-2.
    return _copy(
        tmp_path,
        (
            (
                _COLUMN_MEMBERS,
                "<TablixMember /><TablixMember>"
                + _group_xml("CG")
                + "<TablixMembers><TablixMember /><TablixMember /></TablixMembers></TablixMember>",
            ),
        ),
    )


def _shape_col_group_nested(tmp_path):
    # CG wraps [static, Inner[static]] — columns 0-1, column 2 static.
    return _copy(
        tmp_path,
        (
            (
                _COLUMN_MEMBERS,
                "<TablixMember>"
                + _group_xml("CG")
                + "<TablixMembers><TablixMember /><TablixMember>"
                + _group_xml("Inner", "=2")
                + "<TablixMembers><TablixMember /></TablixMembers></TablixMember>"
                + "</TablixMembers></TablixMember><TablixMember />",
            ),
        ),
    )


COLUMN_SHAPES = {
    "plain": _shape_plain,
    "group_middle": _shape_col_group_middle,
    "group_nested": _shape_col_group_nested,
}


# ---- leaf -> body binding maps -------------------------------------------


def _tablix(path):
    return next(RDLDocument.open(path).root.iter(f"{{{RDL_NS}}}Tablix"))


def _group_path(leaf) -> str:
    names = []
    node = leaf
    while node is not None and node.tag == q("TablixMember"):
        group = find_child(node, "Group")
        if group is not None:
            names.append(group.get("Name"))
        node = node.getparent().getparent()
    return "/".join(reversed(names)) or "static"


def _rows(tablix):
    return find_children(tablix.find(f"{q('TablixBody')}/{q('TablixRows')}"), "TablixRow")


def _cells(row):
    return row.findall(f"{q('TablixCells')}/{q('TablixCell')}")


def _tb_name(cell):
    tb = cell.find(f"{q('CellContents')}/{q('Textbox')}")
    return tb.get("Name") if tb is not None else None


def _row_map(path) -> list[tuple[str, str]]:
    """(leaf group path, first-cell textbox) for every body row."""
    t = _tablix(path)
    leaves = row_leaves(t)
    rows = _rows(t)
    assert len(leaves) == len(rows)
    return [(_group_path(leaf), _tb_name(_cells(r)[0])) for leaf, r in zip(leaves, rows)]


def _column_map(path, row: int = 1) -> list[tuple[str, str]]:
    """(leaf group path, textbox in body row ``row``) for every column."""
    t = _tablix(path)
    leaves = column_leaves(t)
    cells = _cells(_rows(t)[row])
    cols = find_children(t.find(f"{q('TablixBody')}/{q('TablixColumns')}"), "TablixColumn")
    assert len(leaves) == len(cols) == len(cells)
    return [(_group_path(leaf), _tb_name(c)) for leaf, c in zip(leaves, cells)]


def _without(mapping, new_names):
    return [entry for entry in mapping if entry[1] not in new_names]


# ---- the issue's repro -------------------------------------------------------


class TestRepro:
    def test_static_row_after_group_keeps_bindings(self, tmp_path):
        p = _shape_one_row_group(tmp_path)
        before = _row_map(p)
        result = add_static_row(str(p), "MainTable", "Banner", ["Title"], position=1)
        after = _row_map(p)
        assert _without(after, {"Banner"}) == before
        assert ("G/Details", "ProductID") in after
        assert ("G", "HeaderProductID") in after
        # Position 1 is inside G (between its header and the rest): the
        # static row goes just before the group, and the payload says so.
        assert after[result["position"]] == ("static", "Banner")
        assert result["position"] == 0
        assert result["requested_position"] == 1
        assert "inside_group='G'" in result["warnings"][0]
        assert validate_report(str(p))["valid"]

    @pytest.mark.parametrize(
        "call",
        [
            lambda p: add_static_column(p, "MainTable", "NewCol", ["x"], position=2),
            lambda p: add_tablix_column(p, "MainTable", "NewCol", "=1", position=2),
        ],
        ids=["add_static_column", "add_tablix_column"],
    )
    def test_column_next_to_group_keeps_bindings(self, tmp_path, call):
        p = _shape_col_group_middle(tmp_path)
        before = _column_map(p)
        call(str(p))
        after = _column_map(p)
        new = {name for _, name in after} - {name for _, name in before}
        assert _without(after, new) == before
        # CG wraps columns 1-2 (ProductName, Amount) and keeps both.
        assert ("CG", "Amount") in after
        assert ("CG", "ProductName") in after
        assert ("static", "ProductID") in after
        assert validate_report(str(p))["valid"]


# ---- property: every position, every shape ------------------------------------


class TestEveryPosition:
    @pytest.mark.parametrize("shape", sorted(ROW_SHAPES))
    def test_static_row_at_every_position(self, tmp_path, shape):
        probe = ROW_SHAPES[shape](tmp_path / "probe")
        n = len(_row_map(probe))
        for k in range(n + 1):
            p = ROW_SHAPES[shape](tmp_path / f"k{k}")
            before = _row_map(p)
            result = add_static_row(str(p), "MainTable", "NewRow", ["x"], position=k)
            after = _row_map(p)
            assert len(after) == n + 1
            assert _without(after, {"NewRow"}) == before, (shape, k)
            assert after[result["position"]] == ("static", "NewRow"), (shape, k)
            if "warnings" not in result:
                assert result["position"] == k

    @pytest.mark.parametrize("shape", sorted(COLUMN_SHAPES))
    @pytest.mark.parametrize("tool", ["static", "tablix"])
    def test_column_at_every_position(self, tmp_path, shape, tool):
        probe = COLUMN_SHAPES[shape](tmp_path / "probe")
        n = len(_column_map(probe))
        for k in range(n + 1):
            p = COLUMN_SHAPES[shape](tmp_path / f"k{k}")
            before = _column_map(p)
            if tool == "static":
                result = add_static_column(str(p), "MainTable", "NewCol", ["h", "x"], position=k)
                new_name = result["textboxes"][1]
            else:
                result = add_tablix_column(str(p), "MainTable", "NewCol", "=1", position=k)
                new_name = "NewCol"
            after = _column_map(p)
            assert len(after) == n + 1
            assert _without(after, {new_name}) == before, (shape, k)
            assert after[result["position"]] == ("static", new_name), (shape, k)
            if "warnings" not in result:
                assert result["position"] == k


# ---- inside_group ----------------------------------------------------------------


class TestInsideGroup:
    def test_static_row_inside_group(self, tmp_path):
        p = _shape_one_row_group(tmp_path)
        before = _row_map(p)
        result = add_static_row(
            str(p), "MainTable", "PerGroup", ["x"], position=1, inside_group="G"
        )
        after = _row_map(p)
        assert result["position"] == 1
        assert "warnings" not in result
        assert after[1] == ("G", "PerGroup")
        assert _without(after, {"PerGroup"}) == before

    def test_inside_group_end_of_group(self, tmp_path):
        p = _shape_rb_row_group(tmp_path)
        # rows: header(0), Product/Details(1), GrandTotal(2); Product spans 1..1
        result = add_static_row(str(p), "MainTable", "Foot", position=2, inside_group="Product")
        after = _row_map(p)
        assert result["position"] == 2
        assert after[2] == ("Product", "Foot")
        assert after[3] == ("static", "GrandTotal")

    def test_static_column_inside_column_group(self, tmp_path):
        p = _shape_col_group_middle(tmp_path)
        result = add_static_column(
            str(p), "MainTable", "InCG", ["h", "x"], position=2, inside_group="CG"
        )
        after = _column_map(p)
        assert result["position"] == 2
        assert after[2] == ("CG", result["textboxes"][1])
        assert after[3] == ("CG", "Amount")

    def test_add_tablix_column_inside_group(self, tmp_path):
        p = _shape_col_group_middle(tmp_path)
        # Position 3 = just past CG's last column: still inside CG.
        add_tablix_column(str(p), "MainTable", "InCG", "=1", position=3, inside_group="CG")
        after = _column_map(p)
        assert after[2:] == [("CG", "Amount"), ("CG", "InCG")]
        # Without inside_group the same position lands outside the group.
        q2 = _shape_col_group_middle(tmp_path / "outside")
        add_tablix_column(str(q2), "MainTable", "Out", "=1", position=3)
        assert _column_map(q2)[3] == ("static", "Out")

    def test_position_outside_group_refused(self, tmp_path):
        p = _shape_col_group_middle(tmp_path)
        raw = p.read_bytes()
        with pytest.raises(ValueError, match="outside group 'CG'"):
            add_static_column(str(p), "MainTable", "X", position=0, inside_group="CG")
        assert p.read_bytes() == raw

    def test_unknown_group_refused(self, tmp_path):
        p = _shape_plain(tmp_path)
        with pytest.raises(ElementNotFoundError, match="NoSuch"):
            add_static_row(str(p), "MainTable", "X", position=0, inside_group="NoSuch")

    def test_column_group_name_is_not_a_row_group(self, tmp_path):
        p = _shape_col_group_middle(tmp_path)
        with pytest.raises(ElementNotFoundError, match="row-axis group 'CG'"):
            add_static_row(str(p), "MainTable", "X", position=0, inside_group="CG")


# ---- header depth --------------------------------------------------------------


class TestHeaderDepth:
    def _matrix_columns(self, tmp_path):
        # Column hierarchy with one header level on every member (matrix
        # column headers): static, CG[static], static.
        return _copy(
            tmp_path,
            (
                (
                    _COLUMN_MEMBERS,
                    "<TablixMember>"
                    + _header_xml("H0", "0.3in")
                    + "</TablixMember><TablixMember>"
                    + _group_xml("CG")
                    + _header_xml("H1", "0.3in")
                    + "<TablixMembers><TablixMember /></TablixMembers></TablixMember>"
                    + "<TablixMember>"
                    + _header_xml("H2", "0.3in")
                    + "</TablixMember>",
                ),
            ),
        )

    def _depth(self, leaf) -> list[str]:
        sizes = []
        node = leaf
        while node is not None and node.tag == q("TablixMember"):
            header = find_child(node, "TablixHeader")
            if header is not None:
                sizes.append(find_child(header, "Size").text)
            node = node.getparent().getparent()
        return sizes

    @pytest.mark.parametrize("position", [0, 1, 2, 3])
    def test_new_column_gets_matching_header(self, tmp_path, position):
        p = self._matrix_columns(tmp_path)
        result = add_static_column(str(p), "MainTable", "NewCol", position=position)
        t = _tablix(p)
        depths = {tuple(self._depth(leaf)) for leaf in column_leaves(t)}
        assert depths == {("0.3in",)}
        assert len(result["header_textboxes"]) == 1
        assert validate_report(str(p))["valid"]

    def test_add_tablix_column_gets_matching_header(self, tmp_path):
        p = self._matrix_columns(tmp_path)
        result = add_tablix_column(str(p), "MainTable", "NewCol", "=1")
        t = _tablix(p)
        assert {tuple(self._depth(leaf)) for leaf in column_leaves(t)} == {("0.3in",)}
        assert result["header_textboxes"] == ["NewCol_ColumnHeader"]

    def test_two_level_headers(self, tmp_path):
        # Two header levels: outer static [0.2in] wrapping two static
        # leaves [0.3in] each, then a static leaf chain [0.2in]->[0.3in].
        p = _copy(
            tmp_path,
            (
                (
                    _COLUMN_MEMBERS,
                    "<TablixMember>"
                    + _header_xml("Outer", "0.2in")
                    + "<TablixMembers><TablixMember>"
                    + _header_xml("L0", "0.3in")
                    + "</TablixMember><TablixMember>"
                    + _header_xml("L1", "0.3in")
                    + "</TablixMember></TablixMembers></TablixMember>"
                    + "<TablixMember>"
                    + _header_xml("Outer2", "0.2in")
                    + "<TablixMembers><TablixMember>"
                    + _header_xml("L2", "0.3in")
                    + "</TablixMember></TablixMembers></TablixMember>",
                ),
            ),
        )
        for k in (0, 1, 2, 3):
            add_static_column(str(p), "MainTable", f"C{k}", position=k)
            t = _tablix(p)
            depths = {tuple(reversed(self._depth(leaf))) for leaf in column_leaves(t)}
            assert depths == {("0.2in", "0.3in")}, k
        assert validate_report(str(p))["valid"]

    def test_no_headers_in_plain_table(self, tmp_path):
        p = _shape_plain(tmp_path)
        result = add_static_column(str(p), "MainTable", "NewCol", position=1)
        assert "header_textboxes" not in result
        assert _tablix(p).find(f".//{q('TablixHeader')}") is None


# ---- insertion_point unit ---------------------------------------------------------


class TestInsertionPoint:
    def test_append_after_nested_last_leaf_lands_top_level(self, tmp_path):
        p = _shape_nested_row_groups(tmp_path)
        t = _tablix(p)
        n = len(row_leaves(t))
        point = insertion_point(t, "row", n)
        assert point.index == n
        assert point.before is False
        assert point.members is t.find(f"{q('TablixRowHierarchy')}/{q('TablixMembers')}")

    def test_out_of_range(self, tmp_path):
        t = _tablix(_shape_plain(tmp_path))
        with pytest.raises(IndexError):
            insertion_point(t, "row", 3)
        with pytest.raises(IndexError):
            insertion_point(t, "column", -1)

    def test_inconsistent_hierarchy_refuses_positioned_insert(self, tmp_path):
        p = _copy(tmp_path, ((_COLUMN_MEMBERS, "<TablixMember /><TablixMember />"),))
        raw = p.read_bytes()
        with pytest.raises(ValueError, match="ambiguous"):
            add_static_column(str(p), "MainTable", "X", position=0)
        assert p.read_bytes() == raw
        # A plain append still works, with a warning that the mismatch remains.
        result = add_static_column(str(p), "MainTable", "X")
        assert "mismatch remains" in result["warnings"][0]
        assert len(column_leaves(_tablix(p))) == 3  # was 2 leaves for 3 columns
