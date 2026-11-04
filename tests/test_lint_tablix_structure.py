"""Tests for the ``tablix-structure`` lint rule (#44).

Each numbered check of the issue has a positive (fires) and a negative
(stays quiet) case. Broken shapes are made by patching
``pbi_paginated_minimal.rdl`` in-test — the tools themselves refuse to
produce them since M2 — and a Report Builder–shaped matrix (row group,
two-level column header, corner with a RowSpan, a ColSpan in the body) is
written by hand as the "real report" baseline.

Checks 7 (report-wide unique names) and 8 (ToggleItem targets) are owned
by the existing ``duplicate-name`` / ``dangling-toggle-item`` rules; the
tests here pin that they cover the tablix cases.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.tablix_headers import new_textbox, shape_findings, shape_problems
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.lint import ALL_RULES, lint_report
from pbirb_mcp.ops.validate import validate_report, verify_doc, verify_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "pbi_paginated_minimal.rdl"
RULE = "tablix-structure"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


# ---- helpers ---------------------------------------------------------------


def _tablix(doc: RDLDocument, name: str = "MainTable") -> etree._Element:
    return doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='{name}']")


def _rows(tablix: etree._Element) -> list[etree._Element]:
    return find_children(find_child(find_child(tablix, "TablixBody"), "TablixRows"), "TablixRow")


def _cells(row: etree._Element) -> list[etree._Element]:
    return find_children(find_child(row, "TablixCells"), "TablixCell")


def _set_span(cell: etree._Element, tag: str, value: int) -> None:
    contents = find_child(cell, "CellContents")
    etree.SubElement(contents, q(tag)).text = str(value)


def _empty(cell: etree._Element) -> None:
    for child in list(cell):
        cell.remove(child)
    cell.text = None


def _issues(path: Path) -> list[dict]:
    return [i for i in lint_report(str(path), [RULE])["issues"]]


def _errors(path: Path) -> list[dict]:
    return [i for i in _issues(path) if i["severity"] == "error"]


def _messages(issues: list[dict]) -> list[str]:
    return [i["message"] for i in issues]


def _patch(path: Path, fn) -> None:
    doc = RDLDocument.open(path)
    fn(doc)
    doc.save()


# ---- a Report Builder–shaped matrix ---------------------------------------


def _tb(name: str, value: str = "") -> etree._Element:
    return new_textbox(name, value)


def _member(
    *,
    group: str = None,
    header_size: str = None,
    header_box: str = None,
    header_span: tuple = None,
    children: list = None,
    keep: str = None,
) -> etree._Element:
    m = etree.Element(q("TablixMember"))
    if group is not None:
        g = etree.SubElement(m, q("Group"), Name=group)
        ge = etree.SubElement(g, q("GroupExpressions"))
        etree.SubElement(ge, q("GroupExpression")).text = "=Fields!ProductName.Value"
    if header_size is not None:
        h = etree.SubElement(m, q("TablixHeader"))
        etree.SubElement(h, q("Size")).text = header_size
        cc = etree.SubElement(h, q("CellContents"))
        cc.append(_tb(header_box))
        if header_span is not None:
            etree.SubElement(cc, q(header_span[0])).text = str(header_span[1])
    if children:
        ms = etree.SubElement(m, q("TablixMembers"))
        for c in children:
            ms.append(c)
    if keep is not None:
        etree.SubElement(m, q("KeepWithGroup")).text = keep
    return m


def _body_cell(name: str = None, col_span: int = None) -> etree._Element:
    cell = etree.Element(q("TablixCell"))
    if name is None:
        return cell
    cc = etree.SubElement(cell, q("CellContents"))
    cc.append(_tb(name, "=Sum(Fields!Amount.Value)"))
    if col_span is not None:
        etree.SubElement(cc, q("ColSpan")).text = str(col_span)
    return cell


def _rb_matrix(name: str = "Matrix") -> etree._Element:
    """Row group Region (+ a total row), column group Year + a static
    Total column under one "Sales" header row, a 2x1 corner whose single
    cell spans both header rows, and a total row merged across both
    columns. Width = 1in row header + 2 x 1in columns."""
    t = etree.Element(q("Tablix"), Name=name)
    corner = etree.SubElement(t, q("TablixCorner"))
    crows = etree.SubElement(corner, q("TablixCornerRows"))
    r0 = etree.SubElement(crows, q("TablixCornerRow"))
    c0 = etree.SubElement(r0, q("TablixCornerCell"))
    cc = etree.SubElement(c0, q("CellContents"))
    cc.append(_tb(f"{name}_Corner", "Region"))
    etree.SubElement(cc, q("RowSpan")).text = "2"
    r1 = etree.SubElement(crows, q("TablixCornerRow"))
    etree.SubElement(r1, q("TablixCornerCell"))

    body = etree.SubElement(t, q("TablixBody"))
    cols = etree.SubElement(body, q("TablixColumns"))
    for _ in range(2):
        etree.SubElement(etree.SubElement(cols, q("TablixColumn")), q("Width")).text = "1in"
    rows = etree.SubElement(body, q("TablixRows"))
    for cells in (
        [_body_cell(f"{name}_V"), _body_cell(f"{name}_VT")],
        [_body_cell(f"{name}_GT", col_span=2), _body_cell()],
    ):
        row = etree.SubElement(rows, q("TablixRow"))
        etree.SubElement(row, q("Height")).text = "0.25in"
        tc = etree.SubElement(row, q("TablixCells"))
        for c in cells:
            tc.append(c)

    col_h = etree.SubElement(t, q("TablixColumnHierarchy"))
    etree.SubElement(col_h, q("TablixMembers")).append(
        _member(
            header_size="0.25in",
            header_box=f"{name}_Sales",
            children=[
                _member(group=f"{name}_Year", header_size="0.25in", header_box=f"{name}_Y"),
                _member(header_size="0.25in", header_box=f"{name}_TotalCol"),
            ],
        )
    )
    row_h = etree.SubElement(t, q("TablixRowHierarchy"))
    row_members = etree.SubElement(row_h, q("TablixMembers"))
    row_members.append(_member(group=f"{name}_Region", header_size="1in", header_box=f"{name}_R"))
    row_members.append(_member(header_size="1in", header_box=f"{name}_TotalRow", keep="Before"))
    etree.SubElement(t, q("DataSetName")).text = "MainDataset"
    etree.SubElement(t, q("Top")).text = "2in"
    etree.SubElement(t, q("Left")).text = "0.5in"
    etree.SubElement(t, q("Height")).text = "1in"
    etree.SubElement(t, q("Width")).text = "3in"
    etree.SubElement(t, q("Style"))
    return t


@pytest.fixture
def matrix_path(rdl_path: Path) -> Path:
    def add(doc):
        _tablix(doc).addnext(_rb_matrix())

    _patch(rdl_path, add)
    return rdl_path


def _matrix(doc: RDLDocument) -> etree._Element:
    return _tablix(doc, "Matrix")


# ---- registry / baseline ---------------------------------------------------


def test_rule_registered():
    assert RULE in ALL_RULES


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.rdl")), ids=lambda p: p.name)
def test_repo_fixtures_are_clean(path):
    assert _issues(path) == []


def test_report_builder_matrix_is_clean(matrix_path):
    assert _issues(matrix_path) == []
    assert validate_report(str(matrix_path))["valid"]
    # The whole lint run is clean for it as well (no name clashes etc.).
    assert [i for i in lint_report(str(matrix_path))["issues"] if i["severity"] == "error"] == []


def test_shape_problems_is_message_view_of_findings(matrix_path):
    def corrupt(doc):
        m = _matrix(doc)
        m.remove(find_child(m, "TablixCorner"))
        etree.SubElement(
            find_child(find_child(m, "TablixBody"), "TablixColumns"), q("TablixColumn")
        )

    _patch(matrix_path, corrupt)
    m = _matrix(RDLDocument.open(matrix_path))
    findings = shape_findings(m)
    assert shape_problems(m) == [f.message for f in findings]
    assert {f.severity for f in findings} == {"error", "warning"}


# ---- the issue's repro, rebuilt by hand -----------------------------------


def test_issue_repro_corruptions_are_reported(rdl_path):
    def corrupt(doc):
        t = _tablix(doc)
        cols = find_child(find_child(t, "TablixBody"), "TablixColumns")
        etree.SubElement(etree.SubElement(cols, q("TablixColumn")), q("Width")).text = "1in"
        for row in _rows(t):
            _cells(row)[-1].addnext(_body_cell(f"Extra{_rows(t).index(row)}"))
        _set_span(_cells(_rows(t)[0])[0], "ColSpan", 9)
        # a generated name colliding with an existing one
        _cells(_rows(t)[1])[3].find(f"{q('CellContents')}/{q('Textbox')}").set("Name", "Amount")

    _patch(rdl_path, corrupt)
    assert validate_report(str(rdl_path))["valid"]  # the XSD sees none of it
    issues = lint_report(str(rdl_path))["issues"]
    messages = " | ".join(i["message"] for i in issues if i["rule"] == RULE)
    assert "column hierarchy has 3 leaves but the body has 4 columns" in messages
    assert "ColSpan=9 overflows 4 column(s)" in messages
    assert "covered by the ColSpan of cell 0 but has contents" in messages
    assert "duplicate-name" in {i["rule"] for i in issues}
    verify = verify_report(str(rdl_path))
    assert verify["valid"] is False
    assert RULE in {i["rule"] for i in verify["issues"]}


# ---- check 1: cells per row ------------------------------------------------


class TestCellsPerRow:
    def test_missing_cell(self, rdl_path):
        _patch(
            rdl_path,
            lambda d: (
                _cells(_rows(_tablix(d))[1])[2].getparent().remove(_cells(_rows(_tablix(d))[1])[2])
            ),
        )
        errs = _errors(rdl_path)
        assert any("body row 1 has 2 cells; expected 3" in m for m in _messages(errs))
        loc = next(i["location"] for i in errs if "has 2 cells" in i["message"])
        assert loc == "Tablix[Name='MainTable']>TablixBody>TablixRow[1]"

    def test_extra_cell(self, rdl_path):
        _patch(rdl_path, lambda d: _cells(_rows(_tablix(d))[0])[-1].addnext(_body_cell("X")))
        assert any("body row 0 has 4 cells; expected 3" in m for m in _messages(_errors(rdl_path)))

    def test_clean(self, rdl_path):
        assert _issues(rdl_path) == []


# ---- check 2: spans --------------------------------------------------------


class TestSpans:
    def test_overflow(self, rdl_path):
        def corrupt(doc):
            cells = _cells(_rows(_tablix(doc))[0])
            _set_span(cells[1], "ColSpan", 3)
            _empty(cells[2])

        _patch(rdl_path, corrupt)
        errs = _errors(rdl_path)
        assert _messages(errs) == [
            "tablix 'MainTable': body row 0 cell 1 ColSpan=3 overflows 3 column(s)"
        ]
        assert errs[0]["location"].endswith("TablixBody>TablixRow[0]>TablixCell[1]")

    def test_covered_cell_with_contents(self, rdl_path):
        _patch(rdl_path, lambda d: _set_span(_cells(_rows(_tablix(d))[0])[0], "ColSpan", 2))
        msgs = _messages(_errors(rdl_path))
        assert msgs == [
            "tablix 'MainTable': body row 0 cell 1 is covered by the ColSpan of cell 0 "
            "but has contents"
        ]

    def test_uncovered_empty_cell(self, rdl_path):
        _patch(rdl_path, lambda d: _empty(_cells(_rows(_tablix(d))[1])[2]))
        assert _messages(_errors(rdl_path)) == [
            "tablix 'MainTable': body row 1 cell 2 has no CellContents and no span covers it"
        ]

    def test_valid_span_is_clean(self, rdl_path):
        def merge(doc):
            cells = _cells(_rows(_tablix(doc))[0])
            _set_span(cells[0], "ColSpan", 3)
            _empty(cells[1])
            _empty(cells[2])

        _patch(rdl_path, merge)
        assert _issues(rdl_path) == []


# ---- check 3: RowSpan in the body ----------------------------------------


class TestBodyRowSpan:
    def test_rowspan_in_body(self, rdl_path):
        _patch(rdl_path, lambda d: _set_span(_cells(_rows(_tablix(d))[0])[0], "RowSpan", 2))
        assert _messages(_errors(rdl_path)) == [
            "tablix 'MainTable': body row 0 cell 0 has RowSpan>1 (only valid in TablixCorner)"
        ]

    def test_rowspan_1_in_body_and_rowspan_in_corner_are_clean(self, matrix_path):
        _patch(matrix_path, lambda d: _set_span(_cells(_rows(_tablix(d))[0])[0], "RowSpan", 1))
        assert _issues(matrix_path) == []  # the matrix corner carries RowSpan=2


# ---- check 4: hierarchy leaves vs body -----------------------------------


class TestLeafCounts:
    def test_more_columns_than_column_leaves(self, rdl_path):
        def corrupt(doc):
            t = _tablix(doc)
            members = find_child(find_child(t, "TablixColumnHierarchy"), "TablixMembers")
            members.remove(find_children(members, "TablixMember")[-1])

        _patch(rdl_path, corrupt)
        errs = _errors(rdl_path)
        assert _messages(errs) == [
            "tablix 'MainTable': column hierarchy has 2 leaves but the body has 3 columns"
        ]
        assert errs[0]["location"] == "Tablix[Name='MainTable']>TablixColumnHierarchy"

    def test_row_leaves_vs_rows(self, rdl_path):
        def corrupt(doc):
            t = _tablix(doc)
            members = find_child(find_child(t, "TablixRowHierarchy"), "TablixMembers")
            members.append(etree.Element(q("TablixMember")))

        _patch(rdl_path, corrupt)
        assert _messages(_errors(rdl_path)) == [
            "tablix 'MainTable': row hierarchy has 3 leaves but the body has 2 rows"
        ]

    def test_nested_member_leaves_count(self, rdl_path):
        # Wrapping the detail member in a group keeps 2 leaves: clean.
        def wrap(doc):
            t = _tablix(doc)
            members = find_child(find_child(t, "TablixRowHierarchy"), "TablixMembers")
            detail = find_children(members, "TablixMember")[1]
            outer = _member(group="G", children=[])
            detail.addprevious(outer)
            etree.SubElement(outer, q("TablixMembers")).append(detail)

        _patch(rdl_path, wrap)
        assert _issues(rdl_path) == []

    def test_nested_tablix_is_checked(self, rdl_path):
        def nest(doc):
            outer = _tablix(doc)
            inner = copy.deepcopy(outer)
            inner.set("Name", "Inner")
            for el in inner.iter():
                if el is not inner and el.get("Name"):
                    el.set("Name", "Inner_" + el.get("Name"))
            members = find_child(find_child(inner, "TablixColumnHierarchy"), "TablixMembers")
            members.remove(find_children(members, "TablixMember")[0])
            contents = find_child(_cells(_rows(outer)[1])[1], "CellContents")
            contents.replace(find_child(contents, "Textbox"), inner)

        _patch(rdl_path, nest)
        errs = _errors(rdl_path)
        assert [i["location"] for i in errs] == [
            "Tablix[Name='MainTable']>Tablix[Name='Inner']>TablixColumnHierarchy"
        ]
        assert "tablix 'Inner'" in errs[0]["message"]


# ---- check 5: header depth per axis --------------------------------------


class TestHeaderDepth:
    def test_unequal_column_header_levels(self, rdl_path):
        def corrupt(doc):
            t = _tablix(doc)
            members = find_child(find_child(t, "TablixColumnHierarchy"), "TablixMembers")
            m = find_children(members, "TablixMember")[0]
            h = etree.SubElement(m, q("TablixHeader"))
            etree.SubElement(h, q("Size")).text = "0.25in"
            etree.SubElement(h, q("CellContents")).append(_tb("Hdr"))

        _patch(rdl_path, corrupt)
        errs = _errors(rdl_path)
        assert _messages(errs) == [
            "tablix 'MainTable': column hierarchy paths have unequal header levels [1, 0, 0]"
        ]
        assert errs[0]["location"].endswith(">TablixColumnHierarchy")

    def test_unequal_row_header_levels(self, matrix_path):
        def corrupt(doc):
            members = find_child(find_child(_matrix(doc), "TablixRowHierarchy"), "TablixMembers")
            total = find_children(members, "TablixMember")[1]
            total.remove(find_child(total, "TablixHeader"))

        _patch(matrix_path, corrupt)
        assert "row hierarchy paths have unequal header levels [1, 0]" in " ".join(
            _messages(_errors(matrix_path))
        )

    def test_spanning_header_counts_its_levels(self, matrix_path):
        # Move the static Total column out from under "Sales" and give its
        # header RowSpan=2: still 2 header levels on every column path.
        def reshape(doc):
            m = _matrix(doc)
            members = find_child(find_child(m, "TablixColumnHierarchy"), "TablixMembers")
            sales = find_children(members, "TablixMember")[0]
            inner = find_children(find_child(sales, "TablixMembers"), "TablixMember")
            total = inner[1]
            sales.find(q("TablixMembers")).remove(total)
            sales.addnext(total)
            header = find_child(total, "TablixHeader")
            etree.SubElement(find_child(header, "CellContents"), q("RowSpan")).text = "2"

        _patch(matrix_path, reshape)
        assert _issues(matrix_path) == []


# ---- check 6: corner -------------------------------------------------------


class TestCorner:
    def test_corner_on_headerless_tablix(self, rdl_path):
        def corrupt(doc):
            t = _tablix(doc)
            corner = etree.Element(q("TablixCorner"))
            row = etree.SubElement(
                etree.SubElement(corner, q("TablixCornerRows")), q("TablixCornerRow")
            )
            etree.SubElement(
                etree.SubElement(row, q("TablixCornerCell")), q("CellContents")
            ).append(_tb("C"))
            find_child(t, "TablixBody").addprevious(corner)

        _patch(rdl_path, corrupt)
        errs = _errors(rdl_path)
        assert _messages(errs) == [
            "tablix 'MainTable': TablixCorner present but the tablix has "
            "0 column-header row(s) x 0 row-header column(s)"
        ]
        assert errs[0]["location"] == "Tablix[Name='MainTable']>TablixCorner"
        assert "set_tablix_corner" in errs[0]["suggestion"]

    def test_corner_row_count(self, matrix_path):
        def corrupt(doc):
            rows = find_child(find_child(_matrix(doc), "TablixCorner"), "TablixCornerRows")
            rows.remove(find_children(rows, "TablixCornerRow")[1])

        _patch(matrix_path, corrupt)
        msgs = _messages(_errors(matrix_path))
        assert "tablix 'Matrix': TablixCorner has 1 rows; expected 2" in msgs
        assert "tablix 'Matrix': corner row 0 cell 0 RowSpan=2 overflows 1 row(s)" in msgs

    def test_corner_cells_per_row(self, matrix_path):
        def corrupt(doc):
            rows = find_child(find_child(_matrix(doc), "TablixCorner"), "TablixCornerRows")
            row1 = find_children(rows, "TablixCornerRow")[1]
            etree.SubElement(
                etree.SubElement(row1, q("TablixCornerCell")), q("CellContents")
            ).append(_tb("Extra"))

        _patch(matrix_path, corrupt)
        errs = _errors(matrix_path)
        assert "tablix 'Matrix': corner row 1 has 2 cells; expected 1" in _messages(errs)
        assert any(
            i["location"] == "Tablix[Name='Matrix']>TablixCorner>TablixCornerRow[1]" for i in errs
        )

    def test_missing_corner_is_a_warning(self, matrix_path):
        _patch(matrix_path, lambda d: _matrix(d).remove(find_child(_matrix(d), "TablixCorner")))
        issues = _issues(matrix_path)
        assert [(i["severity"], i["message"]) for i in issues] == [
            (
                "warning",
                "tablix 'Matrix': missing TablixCorner (2 header row(s) x 1 header column(s))",
            )
        ]
        assert verify_report(str(matrix_path))["valid"] is True

    def test_corner_matching_depths_is_clean(self, matrix_path):
        assert _issues(matrix_path) == []


# ---- checks 7 / 8: owned by duplicate-name / dangling-toggle-item ---------


class TestNamesAndToggles:
    def test_duplicate_group_name_in_tablix(self, matrix_path):
        _patch(
            matrix_path,
            lambda d: d.root.find(f".//{{{RDL_NS}}}Group[@Name='Matrix_Year']").set(
                "Name", "Matrix_Region"
            ),
        )
        rules = [i["rule"] for i in lint_report(str(matrix_path))["issues"]]
        assert "duplicate-name" in rules

    def test_duplicate_textbox_name_across_tablixes(self, matrix_path):
        _patch(
            matrix_path,
            lambda d: d.root.find(f".//{{{RDL_NS}}}Textbox[@Name='Matrix_V']").set(
                "Name", "Amount"
            ),
        )
        assert "duplicate-name" in [i["rule"] for i in lint_report(str(matrix_path))["issues"]]

    def test_dangling_toggle_item_in_tablix(self, matrix_path):
        def toggle(doc):
            members = find_child(find_child(_matrix(doc), "TablixRowHierarchy"), "TablixMembers")
            vis = etree.SubElement(find_children(members, "TablixMember")[0], q("Visibility"))
            etree.SubElement(vis, q("Hidden")).text = "true"
            etree.SubElement(vis, q("ToggleItem")).text = "NoSuchTextbox"

        _patch(matrix_path, toggle)
        assert "dangling-toggle-item" in [
            i["rule"] for i in lint_report(str(matrix_path))["issues"]
        ]

    def test_unique_names_and_valid_toggle_are_clean(self, matrix_path):
        assert (
            lint_report(str(matrix_path), ["duplicate-name", "dangling-toggle-item"])["issues"]
            == []
        )


# ---- check 9: KeepWithGroup on a grouped member --------------------------


class TestKeepWithGroup:
    def test_on_grouped_member_warns(self, rdl_path):
        def corrupt(doc):
            detail = doc.root.find(f".//{{{RDL_NS}}}Group[@Name='Details']").getparent()
            etree.SubElement(detail, q("KeepWithGroup")).text = "After"

        _patch(rdl_path, corrupt)
        issues = _issues(rdl_path)
        assert [(i["severity"], i["location"]) for i in issues] == [
            ("warning", "Tablix[Name='MainTable']>Group[Name='Details']>KeepWithGroup")
        ]
        assert "KeepWithGroup='After'" in issues[0]["message"]

    def test_on_static_member_is_clean(self, matrix_path):
        # The fixture's header member and the matrix's total row both
        # carry KeepWithGroup on a static member.
        assert _issues(matrix_path) == []


# ---- check 10: Width vs columns -------------------------------------------


class TestWidth:
    def test_width_mismatch_is_info(self, rdl_path):
        _patch(rdl_path, lambda d: find_child(_tablix(d), "Width").__setattr__("text", "6in"))
        issues = _issues(rdl_path)
        assert [(i["severity"], i["location"]) for i in issues] == [
            ("info", "Tablix[Name='MainTable']>Width")
        ]
        assert "add up to 4in" in issues[0]["message"]
        assert verify_report(str(rdl_path))["valid"] is True

    def test_row_headers_count_towards_width(self, matrix_path):
        _patch(matrix_path, lambda d: find_child(_matrix(d), "Width").__setattr__("text", "2in"))
        assert "add up to 3in" in _issues(matrix_path)[0]["message"]

    def test_mixed_units_within_tolerance_are_clean(self, rdl_path):
        _patch(rdl_path, lambda d: find_child(_tablix(d), "Width").__setattr__("text", "10.16cm"))
        assert _issues(rdl_path) == []

    def test_expression_width_is_skipped(self, rdl_path):
        _patch(rdl_path, lambda d: find_child(_tablix(d), "Width").__setattr__("text", "=1"))
        assert _issues(rdl_path) == []


# ---- verify_report / auto-verify / commit verify ---------------------------


def _break_columns(doc: RDLDocument) -> None:
    cols = find_child(find_child(_tablix(doc), "TablixBody"), "TablixColumns")
    etree.SubElement(etree.SubElement(cols, q("TablixColumn")), q("Width")).text = "1in"


def test_verify_report_includes_rule(rdl_path):
    _patch(rdl_path, _break_columns)
    verify = verify_report(str(rdl_path))
    assert verify["valid"] is False
    assert any(i["rule"] == RULE for i in verify["issues"])


def test_verify_doc_includes_rule(rdl_path):
    assert RULE in verify_doc(RDLDocument.open(rdl_path))["rules_run"]


def test_auto_verify_includes_rule(rdl_path, monkeypatch):
    _patch(rdl_path, _break_columns)
    monkeypatch.setenv("PBIRB_MCP_AUTO_VERIFY", "1")
    server = MCPServer()
    register_all_tools(server)
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "set_textbox_style",
                "arguments": {
                    "path": str(rdl_path),
                    "textbox_name": "ProductID",
                    "font_weight": "Bold",
                },
            },
        }
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["verify"]["valid"] is False
    assert any(i["rule"] == RULE for i in payload["verify"]["issues"])
