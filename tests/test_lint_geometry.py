"""Geometry lint rules (#49): body-wider-than-page, item-out-of-bounds,
overlapping-items, tablix-width-mismatch."""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest

from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.lint import ALL_RULES, lint_report
from pbirb_mcp.ops.page import set_body_size, set_page_setup
from pbirb_mcp.ops.reader import describe_report
from pbirb_mcp.ops.tablix import set_tablix_size
from pbirb_mcp.ops.validate import validate_report

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "pbi_paginated_minimal.rdl"
GEOMETRY_RULES = (
    "body-wider-than-page",
    "item-out-of-bounds",
    "overlapping-items",
    "tablix-width-mismatch",
)


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(MINIMAL, dest)
    return dest


def _geometry(path: Path) -> list[dict]:
    return lint_report(str(path), rules=list(GEOMETRY_RULES))["issues"]


def _rules(path: Path) -> list[str]:
    return [i["rule"] for i in _geometry(path)]


def _patch(path: Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    assert old in src, old
    path.write_text(src.replace(old, new, 1), encoding="utf-8")


def _build_repro(path: Path) -> None:
    # page 8.5in, margins 1in + 1in -> 6.5in printable. Columns 1+2+1 = 4in.
    # A stale Width: the tools keep it in step with the columns (#89).
    _patch(path, "<Width>4in</Width>", "<Width>5in</Width>")
    set_body_size(str(path), width="9in", height="2in")
    add_body_textbox(str(path), "Overlap", "x", "0.6in", "0.6in", "2in", "0.25in")
    add_body_textbox(str(path), "Outside", "x", "3in", "8.5in", "2in", "0.25in")


def test_rules_registered():
    for rule in GEOMETRY_RULES:
        assert rule in ALL_RULES


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURES.glob("*.rdl")), ids=lambda n: n)
def test_fixtures_are_clean(fixture):
    assert _geometry(FIXTURES / fixture) == []


class TestIssueRepro:
    def test_exactly_one_of_each_rule(self, rdl_path):
        _build_repro(rdl_path)
        assert validate_report(str(rdl_path))["valid"]
        issues = _geometry(rdl_path)
        assert Counter(i["rule"] for i in issues) == Counter(GEOMETRY_RULES)
        assert all(i["severity"] == "warning" for i in issues)
        by_rule = {i["rule"]: i for i in issues}
        assert "11in" in by_rule["body-wider-than-page"]["message"]
        assert "Outside" in by_rule["item-out-of-bounds"]["location"]
        assert "MainTable" in by_rule["overlapping-items"]["message"]
        assert "Overlap" in by_rule["overlapping-items"]["message"]
        assert "4in" in by_rule["tablix-width-mismatch"]["message"]

    def test_describe_report_page_body(self, rdl_path):
        _build_repro(rdl_path)
        page = describe_report(str(rdl_path))["page"]
        assert page["body"] == {"width": "9in", "height": "2in"}
        assert page["printable_width"] == "6.5in"
        assert page["columns"] == 1
        assert page["column_spacing"] == "0.5in"


class TestBodyWiderThanPage:
    def test_fits_exactly_is_clean(self, rdl_path):
        set_body_size(str(rdl_path), width="6.5in")
        assert _rules(rdl_path) == []

    def test_mixed_units(self, rdl_path):
        # 16.6cm = 6.535in > 6.5in printable.
        set_body_size(str(rdl_path), width="16.6cm")
        assert _rules(rdl_path) == ["body-wider-than-page"]

    def test_absent_page_width_uses_rdl_default(self, rdl_path):
        _patch(rdl_path, "<PageWidth>8.5in</PageWidth>", "")
        set_body_size(str(rdl_path), width="7in")
        assert _rules(rdl_path) == ["body-wider-than-page"]

    def test_columns_and_spacing_respected(self, rdl_path):
        def page_rule():
            return lint_report(str(rdl_path), rules=["body-wider-than-page"])["issues"]

        # 2 x 3in + 0.5in spacing + 2in margins = 8.5in -> fits.
        set_body_size(str(rdl_path), width="3in")
        set_page_setup(str(rdl_path), columns=2)
        assert page_rule() == []
        # 2 x 3.1in + 0.5in + 2in = 8.7in -> overflows.
        set_body_size(str(rdl_path), width="3.1in")
        issues = page_rule()
        assert len(issues) == 1
        assert "2 columns" in issues[0]["message"]


class TestItemOutOfBounds:
    def test_past_body_width(self, rdl_path):
        add_body_textbox(str(rdl_path), "Wide", "x", "1.5in", "4in", "1.5in", "0.25in")
        issues = _geometry(rdl_path)
        assert [i["rule"] for i in issues] == ["item-out-of-bounds"]
        assert "5.5in" in issues[0]["message"]

    def test_below_body_height_is_not_flagged(self, rdl_path):
        # The body grows downward to hold its items.
        add_body_textbox(str(rdl_path), "Low", "x", "5in", "0in", "1in", "0.25in")
        assert _rules(rdl_path) == []

    def test_header_height_and_printable_width(self, rdl_path):
        from pbirb_mcp.ops.header_footer import add_header_textbox, set_page_header

        add_header_textbox(str(rdl_path), "Tall", "x", "0.25in", "0in", "1in", "0.5in")
        add_header_textbox(str(rdl_path), "Far", "x", "0in", "6in", "1in", "0.2in")
        # add_header_textbox grows the band to fit (#50); force it back down.
        set_page_header(str(rdl_path), height="0.5in")
        issues = _geometry(rdl_path)
        assert [i["rule"] for i in issues] == ["item-out-of-bounds", "item-out-of-bounds"]
        assert "Tall" in issues[0]["location"] and "bottom edge" in issues[0]["message"]
        # Far reaches 7in > 6.5in printable width (body is only 5in wide).
        assert "Far" in issues[1]["location"] and "6.5in" in issues[1]["message"]

    def test_outside_rectangle(self, rdl_path):
        _patch(
            rdl_path,
            "</Tablix>\n        </ReportItems>",
            "</Tablix>"
            '<Rectangle Name="Frame"><ReportItems>'
            '<Textbox Name="Spill"><Paragraphs><Paragraph><TextRuns><TextRun>'
            "<Value>x</Value></TextRun></TextRuns></Paragraph></Paragraphs>"
            "<Top>0.5in</Top><Left>1in</Left><Height>0.25in</Height><Width>1.5in</Width>"
            "</Textbox></ReportItems>"
            "<Top>1.25in</Top><Left>0in</Left><Height>0.5in</Height><Width>2in</Width>"
            "</Rectangle></ReportItems>",
        )
        issues = _geometry(rdl_path)
        assert [i["rule"] for i in issues] == ["item-out-of-bounds"]
        assert "Spill" in issues[0]["location"]
        assert "Rectangle 'Frame'" in issues[0]["message"]
        assert "right edge 2.5in" in issues[0]["message"]
        assert "bottom edge 0.75in" in issues[0]["message"]


class TestOverlappingItems:
    def test_touching_edges_are_not_overlap(self, rdl_path):
        # MainTable spans top 0.5..1.0in; this one starts right at 1in.
        add_body_textbox(str(rdl_path), "Below", "x", "1in", "0.5in", "2in", "0.25in")
        assert _rules(rdl_path) == []

    def test_conditionally_visible_item_ignored(self, rdl_path):
        from pbirb_mcp.ops.visibility import set_element_visibility

        add_body_textbox(str(rdl_path), "Alt", "x", "0.6in", "0.6in", "2in", "0.25in")
        assert _rules(rdl_path) == ["overlapping-items"]
        set_element_visibility(
            str(rdl_path), "Alt", hidden_expression="=Parameters!DateFrom.Value Is Nothing"
        )
        assert _rules(rdl_path) == []

    def test_lines_ignored(self, rdl_path):
        from pbirb_mcp.ops.layout import add_line

        add_line(str(rdl_path), "Rule1", top="0.75in", left="0in", width="5in", height="0in")
        assert _rules(rdl_path) == []

    def test_pairwise_in_header(self, rdl_path):
        from pbirb_mcp.ops.header_footer import add_header_textbox, set_page_header

        set_page_header(str(rdl_path), height="1in")
        add_header_textbox(str(rdl_path), "H1", "x", "0in", "0in", "2in", "0.5in")
        add_header_textbox(str(rdl_path), "H2", "x", "0.25in", "1in", "2in", "0.5in")
        issues = _geometry(rdl_path)
        assert [i["rule"] for i in issues] == ["overlapping-items"]
        assert "page header" in issues[0]["message"]


class TestTablixWidthMismatch:
    def test_within_tolerance_is_clean(self, rdl_path):
        _patch(rdl_path, "<Width>4in</Width>", "<Width>4.005in</Width>")
        assert _rules(rdl_path) == []

    def test_mismatch_flagged_with_fix(self, rdl_path):
        _patch(rdl_path, "<Width>4in</Width>", "<Width>3in</Width>")
        issues = _geometry(rdl_path)
        assert [i["rule"] for i in issues] == ["tablix-width-mismatch"]
        assert "set_tablix_size(width='4in')" in issues[0]["suggestion"]
        # The suggested fix clears it.
        set_tablix_size(str(rdl_path), "MainTable", width="4in")
        assert _rules(rdl_path) == []

    def test_row_header_sizes_count(self, rdl_path):
        # Row headers of 1.5in on every row member: the tablix renders at
        # 1.5 + (1 + 2 + 1) = 5.5in.
        src = rdl_path.read_text(encoding="utf-8")
        for i, old in enumerate(
            ("<KeepWithGroup>After</KeepWithGroup>", '<Group Name="Details" />')
        ):
            header = (
                "<TablixHeader><Size>1.5in</Size><CellContents>"
                f'<Textbox Name="RowHead{i}"><Paragraphs><Paragraph><TextRuns><TextRun>'
                "<Value /></TextRun></TextRuns></Paragraph></Paragraphs></Textbox>"
                "</CellContents></TablixHeader>"
            )
            assert old in src
            src = src.replace(old, (header + old) if i == 0 else (old + header), 1)
        rdl_path.write_text(src, encoding="utf-8")
        assert validate_report(str(rdl_path))["valid"]
        issues = _geometry(rdl_path)
        assert [i["rule"] for i in issues] == ["tablix-width-mismatch"]
        assert "5.5in" in issues[0]["message"]
        set_tablix_size(str(rdl_path), "MainTable", width="5.5in")
        assert "tablix-width-mismatch" not in _rules(rdl_path)
