"""Lint rules on top of the VB tokenizer (issue #21).

Covers the false positives / false negatives from the issue:
``expression-syntax`` counting delimiters inside string literals and
missing unterminated literals, and ``double-encoded-entities`` scanning
``<Code>`` / ``<CommandText>`` / ``rd:*``. Inputs are built by patching
the shipped fixtures.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, q
from pbirb_mcp.ops import body, lint
from pbirb_mcp.ops import styling as S
from pbirb_mcp.ops import transactions as TX
from pbirb_mcp.ops.lint import lint_report

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _errors(path: Path, rule: str) -> list[dict]:
    return [
        i
        for i in lint_report(str(path), rules=[rule])["issues"]
        if i["severity"] == "error" and i["rule"] == rule
    ]


def _add_textbox_value(doc: RDLDocument, name: str, value: str) -> etree._Element:
    body_el = doc.root.find(
        f"{{{RDL_NS}}}ReportSections/{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body"
    )
    items = body_el.find(q("ReportItems"))
    tb = etree.SubElement(items, q("Textbox"), Name=name)
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    p = etree.SubElement(paragraphs, q("Paragraph"))
    runs = etree.SubElement(p, q("TextRuns"))
    run = etree.SubElement(runs, q("TextRun"))
    etree.SubElement(run, q("Value")).text = value
    return run


def _with_value(path: Path, value: str) -> Path:
    doc = RDLDocument.open(path)
    _add_textbox_value(doc, "Probe", value)
    doc.save()
    return path


def _command_text(doc: RDLDocument) -> etree._Element:
    return doc.root.find(
        q("DataSets") + "/" + q("DataSet") + "/" + q("Query") + "/" + q("CommandText")
    )


# ---- expression-syntax ----------------------------------------------------


class TestExpressionSyntaxTokenized:
    def test_paren_inside_string_literal_not_flagged(self, rdl_path):
        # Repro from the issue, through the real tool.
        body.add_body_textbox(
            str(rdl_path),
            "Num",
            '=Fields!ProductID.Value & ") " & Fields!ProductName.Value',
            "3in",
            "0in",
            "2in",
            "0.25in",
        )
        assert _errors(rdl_path, "expression-syntax") == []

    @pytest.mark.parametrize(
        "value",
        [
            '="[" & Fields!ProductName.Value & "]"',
            '="(" & Fields!ProductName.Value',
            "=Sum(Fields!ProductID.Value) ' note: (unbalanced in comment",
            '="He said ""(hi"" "',
            '=IIf(Fields!ProductID.Value > 0, Format(Fields!ProductID.Value, "(#)"), "")',
        ],
    )
    def test_valid_expressions_not_flagged(self, rdl_path, value):
        assert _errors(_with_value(rdl_path, value), "expression-syntax") == []

    def test_unterminated_string_literal_flagged(self, rdl_path):
        body.add_body_textbox(
            str(rdl_path), "Num", "=Fields!ProductName.Value", "3in", "0in", "2in", "0.25in"
        )
        S.set_textbox_value(str(rdl_path), "Num", '="Total (" & Fields!ProductName.Value & ")')
        errs = _errors(rdl_path, "expression-syntax")
        assert len(errs) == 1
        assert "unterminated string literal" in errs[0]["message"]

    def test_real_unbalanced_paren_still_flagged(self, rdl_path):
        errs = _errors(
            _with_value(rdl_path, '=Sum(Fields!ProductID.Value & ")"'), "expression-syntax"
        )
        assert len(errs) == 1
        assert errs[0]["message"].startswith("unbalanced parentheses")

    def test_real_unbalanced_bracket_still_flagged(self, rdl_path):
        errs = _errors(_with_value(rdl_path, "=Fields!X.Value(0]"), "expression-syntax")
        assert len(errs) == 1
        assert errs[0]["message"].startswith("unbalanced brackets")

    def test_literal_text_not_checked(self, rdl_path):
        # No leading '=' → a literal, not an expression.
        assert _errors(_with_value(rdl_path, 'Total ("'), "expression-syntax") == []


# ---- double-encoded-entities ---------------------------------------------


class TestDoubleEncodedScope:
    def test_command_text_amp_not_flagged(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        _command_text(doc).text = "EVALUATE FILTER('Sales', 'Sales'[Name] <> \"&amp;\")"
        doc.save()
        assert _errors(rdl_path, "double-encoded-entities") == []

    def test_code_block_amp_not_flagged(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        code = etree.SubElement(doc.root, q("Code"))
        code.text = 'Public Function Amp() As String\n  Return "&amp; &lt;"\nEnd Function'
        doc.save()
        assert _errors(rdl_path, "double-encoded-entities") == []

    def test_rd_designer_state_not_flagged(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        query = doc.root.find(q("DataSets") + "/" + q("DataSet") + "/" + q("Query"))
        ds = etree.SubElement(query, f"{{{RD_NS}}}DesignerState")
        etree.SubElement(ds, f"{{{RD_NS}}}Statement").text = 'EVALUATE "&amp;"'
        doc.save()
        assert _errors(rdl_path, "double-encoded-entities") == []

    def test_image_data_not_flagged(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        block = etree.SubElement(doc.root, q("EmbeddedImages"))
        img = etree.SubElement(block, q("EmbeddedImage"), Name="Logo")
        etree.SubElement(img, q("MIMEType")).text = "image/png"
        etree.SubElement(img, q("ImageData")).text = "&amp;"
        doc.save()
        assert _errors(rdl_path, "double-encoded-entities") == []

    def test_textbox_value_amp_still_flagged(self, rdl_path):
        errs = _errors(
            _with_value(rdl_path, '=Fields!ProductName.Value &amp; " x"'),
            "double-encoded-entities",
        )
        assert len(errs) == 1
        assert "&amp;" in errs[0]["message"]

    def test_textbox_literal_amp_still_flagged(self, rdl_path):
        assert len(_errors(_with_value(rdl_path, "Foo &amp; Bar"), "double-encoded-entities")) == 1

    def test_html_markup_text_run_not_flagged(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        run = _add_textbox_value(doc, "Html", '="Tom &amp; Jerry &lt;b&gt;"')
        etree.SubElement(run, q("MarkupType")).text = "HTML"
        doc.save()
        assert _errors(rdl_path, "double-encoded-entities") == []


# ---- token-based field / parameter rules ----------------------------------


class TestTokenizedReferenceRules:
    def test_field_ref_inside_string_literal_ignored(self, rdl_path):
        _with_value(rdl_path, '="Use Fields!Bogus.Value here"')
        assert _errors(rdl_path, "missing-field-reference") == []

    def test_field_ref_in_literal_text_ignored(self, rdl_path):
        _with_value(rdl_path, "See Fields!Bogus.Value")
        assert _errors(rdl_path, "missing-field-reference") == []

    def test_field_ref_in_code_still_flagged(self, rdl_path):
        _with_value(rdl_path, '="x" & Fields!Bogus.Value')
        errs = _errors(rdl_path, "missing-field-reference")
        assert len(errs) == 1 and "Bogus" in errs[0]["message"]

    def _make_multi(self, path: Path, value: str) -> list[dict]:
        doc = RDLDocument.open(path)
        rp = doc.root.find(
            f"{{{RDL_NS}}}ReportParameters/{{{RDL_NS}}}ReportParameter[@Name='DateFrom']"
        )
        etree.SubElement(rp, q("MultiValue")).text = "true"
        _add_textbox_value(doc, "Cmp", value)
        doc.save()
        return lint_report(str(path), rules=["multi-value-eq"])["issues"]

    @pytest.mark.parametrize(
        "value",
        [
            "=Fields!X.Value = Parameters!DateFrom.Value",
            "=Parameters!DateFrom.Value = Fields!X.Value",
        ],
    )
    def test_multi_value_eq_still_fires(self, rdl_path, value):
        assert len(self._make_multi(rdl_path, value)) == 1

    @pytest.mark.parametrize(
        "value",
        [
            "=Fields!X.Value <= Parameters!DateFrom.Value",
            "=Fields!X.Value >= Parameters!DateFrom.Value",
            "=Parameters!DateFrom.Value(0) = Fields!X.Value",
            "=Fields!X.Value = Parameters!DateFrom.Value(0)",
            '="Parameters!DateFrom.Value = 1"',
            '=Join(Parameters!DateFrom.Value, ",") \' = Parameters!DateFrom.Value',
        ],
    )
    def test_multi_value_eq_no_false_positive(self, rdl_path, value):
        assert self._make_multi(rdl_path, value) == []


# ---- end-to-end: issue repro ----------------------------------------------


def test_apply_edits_commits_after_false_positives_removed(rdl_path):
    p = str(rdl_path)
    body.add_body_textbox(
        p,
        "Num",
        '=Fields!ProductID.Value & ") " & Fields!ProductName.Value',
        "3in",
        "0in",
        "2in",
        "0.25in",
    )
    doc = RDLDocument.open(p)
    _command_text(doc).text = "EVALUATE FILTER('Sales', 'Sales'[Name] <> \"&amp;\")"
    doc.save()
    assert [i for i in lint.lint_report(p)["issues"] if i["severity"] == "error"] == []
    r = TX.apply_edits(
        p,
        [
            {
                "tool": "set_textbox_style",
                "args": {"textbox_name": "ProductID", "font_weight": "Bold"},
            }
        ],
    )
    assert r["committed"] is True


# ---- fixture baseline -----------------------------------------------------

_EXPECTED_FIXTURE_ISSUES = {
    "pbi_paginated_minimal.rdl": [],
    "pbi_chart_rich.rdl": [],
    "pbi_multi_datasource.rdl": [
        ("warning", "unused-data-source"),
        ("warning", "unused-data-set"),
    ],
    "known_bad/actioninfo_under_chartseries.rdl": [],
    "known_bad/bare_action_under_textbox.rdl": [],
    "known_bad/chart_axis_title_wrong_element.rdl": [],
}


@pytest.mark.parametrize("rel", sorted(_EXPECTED_FIXTURE_ISSUES))
def test_fixture_lint_output_unchanged(rel):
    issues = lint_report(str(FIXTURES / rel))["issues"]
    assert [(i["severity"], i["rule"]) for i in issues] == _EXPECTED_FIXTURE_ISSUES[rel]
