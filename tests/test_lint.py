"""Tests for lint_report (Phase 7 commit 31; v0.3.1 added rule 16).

18 rules (17-18, duplicate-name / invalid-identifier, are tested in
tests/test_names.py). Each has a clean-input test (asserts no issues fire) and a
dirty-input test (asserts exactly the expected rule fires). Dirty
inputs are produced by mutating ``pbi_paginated_minimal.rdl`` in-test
rather than maintaining a hand-written ``pbi_lint_warnings.rdl``
fixture — keeps test setup local to the assertion and dodges the
brittleness of one fixture trying to trigger 15 unrelated rules.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, q, qrd
from pbirb_mcp.ops.dataset import update_dataset_query
from pbirb_mcp.ops.escape import raw_xml_replace
from pbirb_mcp.ops.lint import ALL_RULES, lint_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools
from tests._designer_state import build_designer_state_report

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
FIXTURE_MULTI_DS = Path(__file__).parent / "fixtures" / "pbi_multi_datasource.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def multi_ds_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_MULTI_DS, dest)
    return dest


# ---- shared helpers -----------------------------------------------------


def _save(doc: RDLDocument) -> None:
    doc.save()


def _rules_fired(result: dict) -> list[str]:
    return [i["rule"] for i in result["issues"]]


def _rule_count(result: dict, rule: str) -> int:
    return sum(1 for i in result["issues"] if i["rule"] == rule)


# ---- registry-level tests ----------------------------------------------


class TestLintRegistry:
    def test_clean_fixture_has_no_issues(self, rdl_path):
        result = lint_report(str(rdl_path))
        assert result["issues"] == []
        assert set(result["rules_run"]) == set(ALL_RULES)
        assert len(ALL_RULES) == len(set(ALL_RULES))
        # The core rules are always present (families in ops/lint_rules/ add more).
        from pbirb_mcp.ops.lint import _RULES

        assert len(_RULES) >= 19

    def test_unknown_rule_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="unknown lint rule"):
            lint_report(str(rdl_path), rules=["does-not-exist"])

    def test_subset_only_runs_named(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["unused-data-source"])
        assert result["rules_run"] == ["unused-data-source"]


class TestLintDoc:
    """v0.4 commit 12 — _lint_doc(doc) entry point.

    Lets the transaction commit path (v0.4 commit 10) and apply_edits
    (v0.4 commit 11) lint the in-memory tree without writing to a
    tempfile and re-parsing. lint_report(path) is now a thin wrapper.
    """

    def test_lint_doc_matches_lint_report(self, rdl_path):
        from pbirb_mcp.core.document import RDLDocument
        from pbirb_mcp.ops.lint import _lint_doc

        via_path = lint_report(str(rdl_path))
        doc = RDLDocument.open(rdl_path)
        via_doc = _lint_doc(doc)
        assert via_path == via_doc

    def test_lint_doc_honours_rules_subset(self, rdl_path):
        from pbirb_mcp.core.document import RDLDocument
        from pbirb_mcp.ops.lint import _lint_doc

        doc = RDLDocument.open(rdl_path)
        result = _lint_doc(doc, rules=["unused-data-source"])
        assert result["rules_run"] == ["unused-data-source"]

    def test_lint_doc_rejects_unknown_rule(self, rdl_path):
        from pbirb_mcp.core.document import RDLDocument
        from pbirb_mcp.ops.lint import _lint_doc

        doc = RDLDocument.open(rdl_path)
        with pytest.raises(ValueError, match="unknown lint rule"):
            _lint_doc(doc, rules=["nope"])


# ---- helpers for mutating the fixture -----------------------------------


def _add_textbox_with_value(doc: RDLDocument, name: str, value: str) -> etree._Element:
    """Append a Body textbox with a given Value text. Returns the
    Textbox element so callers can inject side-effects (e.g. wrap it in
    PageHeader) before saving.
    """
    body = doc.root.find(f"{{{RDL_NS}}}ReportSections/{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body")
    items = body.find(q("ReportItems"))
    if items is None:
        items = etree.SubElement(body, q("ReportItems"))
    tb = etree.SubElement(items, q("Textbox"), Name=name)
    paragraphs = etree.SubElement(tb, q("Paragraphs"))
    p = etree.SubElement(paragraphs, q("Paragraph"))
    runs = etree.SubElement(p, q("TextRuns"))
    run = etree.SubElement(runs, q("TextRun"))
    val = etree.SubElement(run, q("Value"))
    val.text = value
    return tb


# ---- rule 1: multi-value-eq --------------------------------------------


class TestMultiValueEq:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["multi-value-eq"])
        assert result["issues"] == []

    def test_fires_when_multi_value_param_compared_with_eq(self, rdl_path):
        # Mark DateFrom multi-value, then add a textbox using = comparison.
        doc = RDLDocument.open(rdl_path)
        rp = doc.root.find(
            f"{{{RDL_NS}}}ReportParameters/{{{RDL_NS}}}ReportParameter[@Name='DateFrom']"
        )
        assert rp is not None, "fixture should have DateFrom"
        etree.SubElement(rp, q("MultiValue")).text = "true"
        _add_textbox_with_value(doc, "BadCmp", "=Fields!X.Value = Parameters!DateFrom.Value")
        _save(doc)
        result = lint_report(str(rdl_path), rules=["multi-value-eq"])
        assert _rule_count(result, "multi-value-eq") == 1
        assert "DateFrom" in result["issues"][0]["message"]


# ---- rule 2: unused-data-source ----------------------------------------


class TestUnusedDataSource:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["unused-data-source"])
        assert result["issues"] == []

    def test_fires_for_unreferenced_data_source(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        sources = doc.root.find(q("DataSources"))
        new_ds = etree.SubElement(sources, q("DataSource"), Name="OrphanDS")
        cp = etree.SubElement(new_ds, q("ConnectionProperties"))
        etree.SubElement(cp, q("DataProvider")).text = "SQL"
        etree.SubElement(cp, q("ConnectString")).text = "Server=x"
        _save(doc)
        result = lint_report(str(rdl_path), rules=["unused-data-source"])
        assert _rule_count(result, "unused-data-source") == 1
        assert "OrphanDS" in result["issues"][0]["message"]


# ---- rule 3: unused-data-set -------------------------------------------


class TestUnusedDataSet:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["unused-data-set"])
        # The fixture's only dataset is referenced by the tablix.
        assert result["issues"] == []

    def test_fires_for_unreferenced_data_set(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        datasets = doc.root.find(q("DataSets"))
        new_ds = etree.SubElement(datasets, q("DataSet"), Name="OrphanDataSet")
        query = etree.SubElement(new_ds, q("Query"))
        etree.SubElement(query, q("DataSourceName")).text = "DataSource1"
        etree.SubElement(query, q("CommandText")).text = "EVALUATE 'X'"
        _save(doc)
        result = lint_report(str(rdl_path), rules=["unused-data-set"])
        assert _rule_count(result, "unused-data-set") == 1


# ---- rule 4: date-param-as-string --------------------------------------


class TestDateParamAsString:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["date-param-as-string"])
        assert result["issues"] == []

    def test_fires_when_date_param_is_string(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        rp_block = doc.root.find(q("ReportParameters"))
        rp = etree.SubElement(rp_block, q("ReportParameter"), Name="ReportDate")
        etree.SubElement(rp, q("DataType")).text = "String"
        _save(doc)
        result = lint_report(str(rdl_path), rules=["date-param-as-string"])
        assert _rule_count(result, "date-param-as-string") == 1
        assert "ReportDate" in result["issues"][0]["message"]


# ---- rule 5: missing-field-reference -----------------------------------


class TestMissingFieldReference:
    def test_clean(self, rdl_path):
        # Add a textbox referencing a real field — should not fire.
        doc = RDLDocument.open(rdl_path)
        # Find any existing field name.
        any_field = doc.root.iter(q("Field")).__next__()
        real = any_field.get("Name")
        _add_textbox_with_value(doc, "RealFieldRef", f"=Fields!{real}.Value")
        _save(doc)
        result = lint_report(str(rdl_path), rules=["missing-field-reference"])
        assert result["issues"] == []

    def test_fires_for_unknown_field(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        _add_textbox_with_value(doc, "BadRef", "=Fields!DoesNotExist.Value")
        _save(doc)
        result = lint_report(str(rdl_path), rules=["missing-field-reference"])
        assert _rule_count(result, "missing-field-reference") == 1
        assert "DoesNotExist" in result["issues"][0]["message"]


# ---- rule 6: page-number-out-of-chrome ---------------------------------


class TestPageNumberOutOfChrome:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["page-number-out-of-chrome"])
        assert result["issues"] == []

    def test_fires_in_body_textbox(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        _add_textbox_with_value(doc, "PageInBody", "=Globals!PageNumber")
        _save(doc)
        result = lint_report(str(rdl_path), rules=["page-number-out-of-chrome"])
        assert _rule_count(result, "page-number-out-of-chrome") == 1


# ---- rule 7: expression-syntax -----------------------------------------


class TestExpressionSyntax:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["expression-syntax"])
        assert result["issues"] == []

    def test_fires_on_unbalanced_parens(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        _add_textbox_with_value(doc, "BadParen", "=Sum(Fields!X.Value")
        _save(doc)
        result = lint_report(str(rdl_path), rules=["expression-syntax"])
        assert _rule_count(result, "expression-syntax") == 1


# ---- rule 8: dangling-embedded-image -----------------------------------


class TestDanglingEmbeddedImage:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["dangling-embedded-image"])
        assert result["issues"] == []

    def test_fires_for_undeclared_embedded_image(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        body = doc.root.find(
            f"{{{RDL_NS}}}ReportSections/{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body"
        )
        items = body.find(q("ReportItems"))
        if items is None:
            items = etree.SubElement(body, q("ReportItems"))
        img = etree.SubElement(items, q("Image"), Name="DanglingImg")
        etree.SubElement(img, q("Source")).text = "Embedded"
        etree.SubElement(img, q("Value")).text = "NoSuchImage"
        _save(doc)
        result = lint_report(str(rdl_path), rules=["dangling-embedded-image"])
        assert _rule_count(result, "dangling-embedded-image") == 1
        assert "NoSuchImage" in result["issues"][0]["message"]

    def _body_image(self, rdl_path, value: str, source: str = "Embedded") -> None:
        doc = RDLDocument.open(rdl_path)
        body = doc.root.find(
            f"{{{RDL_NS}}}ReportSections/{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Body"
        )
        items = body.find(q("ReportItems"))
        img = etree.SubElement(items, q("Image"), Name="Pick")
        etree.SubElement(img, q("Source")).text = source
        etree.SubElement(img, q("Value")).text = value
        _save(doc)

    def test_expression_string_literal_naming_no_image_warns(self, rdl_path):
        # #81: the Value expression may pick "LogoA", which isn't declared.
        self._body_image(rdl_path, '=IIf(Parameters!DateFrom.Value Is Nothing, "LogoA", "LogoA")')
        issues = lint_report(str(rdl_path), rules=["dangling-embedded-image"])["issues"]
        assert [(i["severity"], i["location"]) for i in issues] == [
            ("warning", "Image[Name='Pick']")
        ]
        assert "LogoA" in issues[0]["message"]

    @pytest.mark.parametrize(
        "value",
        [
            "=Fields!ProductName.Value",  # no literal at all
            '="http://x/" & Fields!ProductID.Value',  # not an image name
            '=IIf(True, "Logo A", "")',  # not identifiers
            '=Code.Pick() \' "Ghost" in a comment',
        ],
    )
    def test_expression_without_image_name_literals_is_silent(self, rdl_path, value):
        self._body_image(rdl_path, value)
        assert lint_report(str(rdl_path), rules=["dangling-embedded-image"])["issues"] == []

    def test_body_background_image_located_by_owner(self, rdl_path):
        text = rdl_path.read_text(encoding="utf-8")
        old = "<Height>2in</Height>\n        <Style />"
        assert old in text
        rdl_path.write_text(
            text.replace(
                old,
                "<Height>2in</Height>\n        <Style><BackgroundImage><Source>Embedded</Source>"
                "<Value>Watermark</Value></BackgroundImage></Style>",
            ),
            encoding="utf-8",
        )
        issues = lint_report(str(rdl_path), rules=["dangling-embedded-image"])["issues"]
        assert [(i["severity"], i["location"]) for i in issues] == [
            ("error", "Body/Style/BackgroundImage")
        ]


# ---- rule 9: dangling-data-source-reference ----------------------------


class TestDanglingDataSourceReference:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["dangling-data-source-reference"])
        assert result["issues"] == []

    def test_fires_for_unknown_data_source_in_dataset(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(q("DataSets")).find(q("DataSet"))
        ds_name_node = ds.find(q("Query")).find(q("DataSourceName"))
        ds_name_node.text = "GhostDataSource"
        _save(doc)
        result = lint_report(str(rdl_path), rules=["dangling-data-source-reference"])
        assert _rule_count(result, "dangling-data-source-reference") == 1
        assert "GhostDataSource" in result["issues"][0]["message"]


# ---- rule 10: dangling-action ------------------------------------------


class TestDanglingAction:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["dangling-action"])
        assert result["issues"] == []

    def test_fires_on_empty_drillthrough(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        tb = _add_textbox_with_value(doc, "ActTb", '="x"')
        # ActionInfo/Actions/Action/Drillthrough/ReportName
        action_info = etree.SubElement(tb, q("ActionInfo"))
        actions = etree.SubElement(action_info, q("Actions"))
        action = etree.SubElement(actions, q("Action"))
        drill = etree.SubElement(action, q("Drillthrough"))
        etree.SubElement(drill, q("ReportName")).text = ""
        _save(doc)
        result = lint_report(str(rdl_path), rules=["dangling-action"])
        assert _rule_count(result, "dangling-action") == 1


# ---- rule 11: pbidataset-at-prefix -------------------------------------


class TestPBIDatasetAtPrefix:
    def test_clean(self, multi_ds_path):
        result = lint_report(str(multi_ds_path), rules=["pbidataset-at-prefix"])
        assert result["issues"] == []

    def test_fires_when_pbidataset_qp_has_at_prefix(self, multi_ds_path):
        # multi_ds fixture has one PBIDATASET-bound dataset; manually
        # write a QueryParameter with an @-prefix to trigger the rule.
        doc = RDLDocument.open(multi_ds_path)
        # Find the PBIDATASET-bound dataset.
        from pbirb_mcp.ops.dataset import _is_pbidataset_dataset

        target = None
        for ds in doc.root.iter(q("DataSet")):
            if _is_pbidataset_dataset(doc, ds):
                target = ds
                break
        assert target is not None, "fixture should have a PBIDATASET dataset"
        query = target.find(q("Query"))
        qps = query.find(q("QueryParameters"))
        if qps is None:
            qps = etree.SubElement(query, q("QueryParameters"))
        qp = etree.SubElement(qps, q("QueryParameter"), Name="@WrongName")
        etree.SubElement(qp, q("Value")).text = "=Parameters!Param1.Value"
        _save(doc)
        result = lint_report(str(multi_ds_path), rules=["pbidataset-at-prefix"])
        assert _rule_count(result, "pbidataset-at-prefix") == 1


# ---- rule 12: parameter-layout-out-of-sync -----------------------------


def _inject_parameter_layout(doc: RDLDocument, cell_count: int) -> None:
    """Append a <ReportParametersLayout> block with ``cell_count`` cells.

    Used in lint tests to set up a known cell-count vs param-count
    relationship without going through the parameter-layout authoring
    helpers (whose own behaviour we're not testing here).
    """
    from pbirb_mcp.ops.parameters import (
        _all_parameter_names,
        _build_cell_definition,
    )

    layout = etree.SubElement(doc.root, q("ReportParametersLayout"))
    grid = etree.SubElement(layout, q("GridLayoutDefinition"))
    etree.SubElement(grid, q("NumberOfColumns")).text = "4"
    etree.SubElement(grid, q("NumberOfRows")).text = "1"
    cells = etree.SubElement(grid, q("CellDefinitions"))
    names = _all_parameter_names(doc)
    # Pad / truncate to exactly cell_count entries.
    for i in range(cell_count):
        name = names[i] if i < len(names) else f"Padding{i}"
        cells.append(_build_cell_definition(name, 0, i))


class TestParameterLayoutOutOfSync:
    def test_clean(self, rdl_path):
        # Build a layout whose cell count exactly matches ReportParameters.
        doc = RDLDocument.open(rdl_path)
        param_count = len(doc.root.find(q("ReportParameters")))
        _inject_parameter_layout(doc, param_count)
        _save(doc)
        result = lint_report(str(rdl_path), rules=["parameter-layout-out-of-sync"])
        assert result["issues"] == []

    def test_fires_when_counts_diverge(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        # Layout has one fewer cell than declared parameters → mismatch.
        param_count = len(doc.root.find(q("ReportParameters")))
        _inject_parameter_layout(doc, param_count - 1)
        _save(doc)
        result = lint_report(str(rdl_path), rules=["parameter-layout-out-of-sync"])
        assert _rule_count(result, "parameter-layout-out-of-sync") == 1


# ---- rule 13: double-encoded-entities ----------------------------------


class TestDoubleEncodedEntities:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["double-encoded-entities"])
        assert result["issues"] == []

    def test_fires_on_double_encoded_amp(self, rdl_path):
        # Write the file directly with `&amp;amp;` in source — after
        # parse, .text reads as `&amp;` literal which is what we detect.
        text = rdl_path.read_text(encoding="utf-8")
        # Inject into the existing tablix textbox value. Find a <Value>
        # element under TextRun and rewrite it.
        text = text.replace(
            "</TextRuns>",
            "</TextRuns>",
            1,
        )
        # Direct injection: append a TextRun with a double-encoded body.
        # Use any existing Paragraph/TextRuns insertion point.
        injected = (
            "<Paragraphs><Paragraph><TextRuns><TextRun><Value>"
            "Foo &amp;amp; Bar"
            "</Value></TextRun></TextRuns></Paragraph></Paragraphs>"
        )
        # Replace only the FIRST <Paragraphs> ... </Paragraphs> with our
        # injected block to keep round-trip valid.
        start = text.index("<Paragraphs>")
        end = text.index("</Paragraphs>", start) + len("</Paragraphs>")
        text = text[:start] + injected + text[end:]
        rdl_path.write_text(text, encoding="utf-8")
        result = lint_report(str(rdl_path), rules=["double-encoded-entities"])
        assert _rule_count(result, "double-encoded-entities") >= 1


# ---- rule 14: stale-designer-state -------------------------------------


class TestStaleDesignerState:
    def test_clean(self, rdl_path):
        # Fixture has no DesignerState; rule fires only when one exists
        # AND its Statement diverges.
        result = lint_report(str(rdl_path), rules=["stale-designer-state"])
        assert result["issues"] == []

    def test_fires_when_statement_diverges_from_command_text(self, rdl_path):
        # Inject a DesignerState/Statement that doesn't match CommandText.
        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(q("DataSets")).find(q("DataSet"))
        query = ds.find(q("Query"))
        designer = etree.SubElement(query, qrd("DesignerState"))
        statement = etree.SubElement(designer, qrd("Statement"))
        statement.text = "EVALUATE 'totally different shape'"
        _save(doc)
        result = lint_report(str(rdl_path), rules=["stale-designer-state"])
        assert _rule_count(result, "stale-designer-state") == 1

    # ---- #70: Report Builder's real DesignerState shape ----------------

    def test_report_builder_shape_in_sync_is_clean(self, tmp_path):
        path = build_designer_state_report(tmp_path / "rb.rdl")
        result = lint_report(str(path), rules=["stale-designer-state"])
        assert result["issues"] == []

    def test_fires_after_command_text_only_raw_edit(self, tmp_path):
        # A CommandText-only edit (raw_xml_replace) leaves the designer's
        # QueryDefinition/Query/Statement stale; the rule must notice.
        path = build_designer_state_report(tmp_path / "rb.rdl")
        raw_xml_replace(
            str(path),
            ".//r:DataSet[@Name='MainDataset']/r:Query/r:CommandText",
            '<CommandText>EVALUATE ROW("x", 1)</CommandText>',
        )
        result = lint_report(str(path), rules=["stale-designer-state"])
        assert _rule_count(result, "stale-designer-state") == 1
        issue = result["issues"][0]
        assert "MainDataset" in issue["location"]
        assert "QueryDefinition/Query/Statement" in issue["location"]

    def test_clean_again_after_update_dataset_query(self, tmp_path):
        path = build_designer_state_report(tmp_path / "rb.rdl")
        raw_xml_replace(
            str(path),
            ".//r:DataSet[@Name='MainDataset']/r:Query/r:CommandText",
            '<CommandText>EVALUATE ROW("x", 1)</CommandText>',
        )
        update_dataset_query(str(path), "MainDataset", 'EVALUATE ROW("x", 1)')
        result = lint_report(str(path), rules=["stale-designer-state"])
        assert result["issues"] == []


# ---- rule 15: tablix-span-misplaced ------------------------------------


class TestTablixSpanMisplaced:
    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["tablix-span-misplaced"])
        assert result["issues"] == []

    def test_fires_when_colspan_is_direct_child_of_tablix_cell(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        cell = doc.root.iter(q("TablixCell")).__next__()
        # Misplace a ColSpan directly under TablixCell (legacy v0.2 shape).
        etree.SubElement(cell, q("ColSpan")).text = "2"
        _save(doc)
        result = lint_report(str(rdl_path), rules=["tablix-span-misplaced"])
        assert _rule_count(result, "tablix-span-misplaced") == 1


# ---- rule 16: dataset-fields-out-of-sync -------------------------------


class TestDatasetFieldsOutOfSync:
    """Detects ``<Field>`` declared but DAX ``<CommandText>`` doesn't
    return that column. Same regression class as the v0.3.0
    SELECTCOLUMNS over-match (commit 9792281), but at the static-lint
    layer instead of the runtime ``refresh_dataset_fields`` reporter.
    """

    def test_clean(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["dataset-fields-out-of-sync"])
        assert result["issues"] == []

    def test_fires_when_field_is_orphan(self, rdl_path):
        # Fixture's MainDataset uses bare EVALUATE 'Table' which the
        # extractor can't see into. Rewrite to a SUMMARIZECOLUMNS shape
        # whose bracketed columns are extractable, then add a phantom
        # <Field> that isn't in the DAX.
        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(q("DataSets")).find(q("DataSet"))
        cmd = ds.find(q("Query")).find(q("CommandText"))
        cmd.text = (
            "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], 'Sales'[ProductName], 'Sales'[Amount])"
        )
        fields = ds.find(q("Fields"))
        new_field = etree.SubElement(fields, q("Field"), Name="GhostColumn")
        df = etree.SubElement(new_field, q("DataField"))
        df.text = "GhostColumn"
        _save(doc)
        result = lint_report(str(rdl_path), rules=["dataset-fields-out-of-sync"])
        assert _rule_count(result, "dataset-fields-out-of-sync") == 1
        issue = next(i for i in result["issues"] if i["rule"] == "dataset-fields-out-of-sync")
        assert issue["severity"] == "warning"
        assert "GhostColumn" in issue["location"]
        assert "remove_dataset_field" in issue["suggestion"]

    def test_skips_when_dax_is_unparseable(self, rdl_path):
        # When _extract_dax_field_names returns warnings (no recognisable
        # shape), don't false-flag every <Field> as orphan. The rule
        # should silently skip the dataset.
        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(q("DataSets")).find(q("DataSet"))
        cmd = ds.find(q("Query")).find(q("CommandText"))
        # Bare-table EVALUATE — extractor can't see columns.
        cmd.text = "EVALUATE 'OpaqueTable'"
        _save(doc)
        result = lint_report(str(rdl_path), rules=["dataset-fields-out-of-sync"])
        assert result["issues"] == []


# #51: Fields shaped the way Report Builder writes them — sanitised Name,
# the real column only in <DataField>.
def _rb_sanitised(rdl_path: Path) -> str:
    s = rdl_path.read_text(encoding="utf-8")
    s = s.replace('"ProductName"', '"Product_Name"').replace(
        "Sales[ProductName]", "Sales[Product Name]"
    )
    s = s.replace('"Amount"', '"Amount__USD_"').replace("Sales[Amount]", "Sales[Amount (USD)]")
    rdl_path.write_text(s, encoding="utf-8")
    return str(rdl_path)


def _out_of_sync(path: str) -> list[dict]:
    return lint_report(path, rules=["dataset-fields-out-of-sync"])["issues"]


class TestDatasetFieldsOutOfSyncDataField:
    """#51: the rule compares <DataField> (never Field/@Name), both ways."""

    def test_issue_repro_flags_only_the_unbound_column(self, rdl_path):
        from pbirb_mcp.ops.dataset import update_dataset_query

        p = _rb_sanitised(rdl_path)
        update_dataset_query(
            p,
            "MainDataset",
            "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS('Sales'[ProductID], "
            "'Sales'[Product Name], 'Sales'[Amount (USD)], 'Sales'[Region]), "
            "FILTER(VALUES('Sales'[Date]), 'Sales'[Date] >= @From))",
        )
        issues = _out_of_sync(p)
        assert len(issues) == 1
        (issue,) = issues
        assert issue["kind"] == "column-not-bound"
        assert issue["severity"] == "warning"
        assert "Sales[Region]" in issue["message"]
        assert issue["location"] == "DataSet[Name='MainDataset']/Fields"

    def test_unchanged_sanitised_report_is_clean(self, rdl_path):
        from pbirb_mcp.ops.dataset import update_dataset_query

        p = _rb_sanitised(rdl_path)
        update_dataset_query(
            p,
            "MainDataset",
            "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], 'Sales'[Product Name], "
            "'Sales'[Amount (USD)])",
        )
        assert _out_of_sync(p) == []

    def test_quoted_and_case_folded_data_fields_match(self, rdl_path):
        from pbirb_mcp.ops.dataset import update_dataset_query

        p = str(rdl_path)
        s = rdl_path.read_text(encoding="utf-8").replace(
            "Sales[ProductName]", "'Sales'[productname]"
        )
        rdl_path.write_text(s, encoding="utf-8")
        update_dataset_query(
            p,
            "MainDataset",
            "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], 'Sales'[ProductName], 'Sales'[Amount])",
        )
        assert _out_of_sync(p) == []

    def test_selectcolumns_alias_binds_by_data_field(self, rdl_path):
        # Name ID_X_ binds DataField [X] — the SELECTCOLUMNS alias shape.
        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(q("DataSets")).find(q("DataSet"))
        ds.find(q("Query")).find(
            q("CommandText")
        ).text = "EVALUATE SELECTCOLUMNS('Sales', \"X\", 'Sales'[ProductID])"
        fields = ds.find(q("Fields"))
        for f in list(fields):
            fields.remove(f)
        field = etree.SubElement(fields, q("Field"), Name="ID_X_")
        etree.SubElement(field, q("DataField")).text = "[X]"
        _save(doc)
        assert _out_of_sync(str(rdl_path)) == []

    def test_filter_only_column_field_is_not_returned(self, rdl_path):
        # A Field bound to a column the DAX only filters on (#24 phantom).
        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(q("DataSets")).find(q("DataSet"))
        ds.find(q("Query")).find(q("CommandText")).text = (
            "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS('Sales'[ProductID], "
            "'Sales'[ProductName], 'Sales'[Amount]), "
            "FILTER(VALUES('Sales'[Date]), 'Sales'[Date] >= @From))"
        )
        field = etree.SubElement(ds.find(q("Fields")), q("Field"), Name="Date")
        etree.SubElement(field, q("DataField")).text = "Sales[Date]"
        _save(doc)
        issues = _out_of_sync(str(rdl_path))
        assert [(i["kind"], i["location"]) for i in issues] == [
            ("field-not-returned", "DataSet[Name='MainDataset']/Fields/Field[Name='Date']")
        ]

    def test_calculated_field_is_ignored(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(q("DataSets")).find(q("DataSet"))
        ds.find(q("Query")).find(
            q("CommandText")
        ).text = (
            "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], 'Sales'[ProductName], 'Sales'[Amount])"
        )
        field = etree.SubElement(ds.find(q("Fields")), q("Field"), Name="Doubled")
        etree.SubElement(field, q("Value")).text = "=Fields!Amount.Value * 2"
        _save(doc)
        assert _out_of_sync(str(rdl_path)) == []


# ---- registration ------------------------------------------------------


class TestToolRegistration:
    def test_lint_report_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "lint_report" in names


# ---- #48: empty selector runs everything; locations name the item -------------


def test_empty_rules_list_runs_all_rules(rdl_path):
    result = lint_report(str(rdl_path), rules=[])
    assert set(result["rules_run"]) == set(ALL_RULES)


def test_textbox_value_issue_is_located_at_the_textbox(tmp_path):
    p = tmp_path / "r.rdl"
    p.write_text(FIXTURE.read_text().replace("=Fields!Amount.Value", "=Fields!Typo.Value"))
    issues = [i for i in lint_report(str(p))["issues"] if i["rule"] == "missing-field-reference"]
    assert issues
    location = issues[0]["location"]
    assert "Textbox[Name='Amount']" in location
    assert location.startswith("Tablix[Name='MainTable']>")
    assert location.endswith(">Value")


def test_ancestor_chain_keeps_three_nearest_named_ancestors():
    from lxml import etree

    from pbirb_mcp.ops.lint import _ancestor_chain

    root = etree.fromstring(
        "<A Name='a'><B Name='b'><C Name='c'><D Name='d'><E><F/></E></D></C></B></A>"
    )
    leaf = root.find(".//F")
    assert _ancestor_chain(leaf) == "B[Name='b']>C[Name='c']>D[Name='d']>F"
    assert _ancestor_chain(root.find(".//D")) == "B[Name='b']>C[Name='c']>D[Name='d']"
    assert _ancestor_chain(etree.fromstring("<X><Y/></X>").find("Y")) == "(unnamed)"


def test_lint_rules_package_modules_are_registered(tmp_path, monkeypatch):
    import sys
    import types

    from pbirb_mcp.ops import lint_rules

    fake = types.ModuleType("pbirb_mcp.ops.lint_rules.zz_fake")
    fake.RULES = {"zz-fake-rule": lambda doc: []}
    monkeypatch.setitem(sys.modules, "pbirb_mcp.ops.lint_rules.zz_fake", fake)
    real_iter = lint_rules.pkgutil.iter_modules

    def iter_modules(path):
        yield from real_iter(path)
        yield types.SimpleNamespace(name="zz_fake")

    monkeypatch.setattr(lint_rules.pkgutil, "iter_modules", iter_modules)
    assert "zz-fake-rule" in lint_rules.load()


# ---- #50: page-chrome rules (lint_rules/page_chrome.py) -------------------

_CHROME_RULES = [
    "page-number-out-of-chrome",
    "field-in-page-chrome",
    "page-section-missing-height",
]


def _chrome_issues(path: str) -> list[tuple[str, str]]:
    return [(i["rule"], i["location"]) for i in lint_report(path, rules=_CHROME_RULES)["issues"]]


class TestPageChromeRules:
    def test_fixtures_are_clean(self):
        for fixture in sorted((Path(__file__).parent / "fixtures").glob("*.rdl")):
            assert _chrome_issues(str(fixture)) == [], fixture.name

    def test_issue_50_repro(self, rdl_path):
        from pbirb_mcp.ops.body import add_body_textbox
        from pbirb_mcp.ops.header_footer import add_header_textbox, set_page_header

        p = str(rdl_path)
        set_page_header(p, print_on_first_page=False)
        add_header_textbox(
            p, "HdrField", "=Fields!ProductName.Value", "0in", "0in", "2in", "0.25in"
        )
        add_body_textbox(
            p,
            "Pg1",
            '=Globals!OverallPageNumber & "/" & Globals!OverallTotalPages',
            "1.2in",
            "0.5in",
            "2in",
            "0.25in",
        )
        add_body_textbox(p, "Pg2", "=Globals!PageName", "1.5in", "0.5in", "2in", "0.25in")
        add_body_textbox(p, "Pg3", "=Globals!PageNumber", "1.8in", "0.5in", "2in", "0.25in")
        found = _chrome_issues(p)
        assert sorted(found) == sorted(
            [
                ("field-in-page-chrome", "Textbox[Name='HdrField']>Value"),
                ("page-number-out-of-chrome", "Textbox[Name='Pg1']>Value"),
                ("page-number-out-of-chrome", "Textbox[Name='Pg2']>Value"),
                ("page-number-out-of-chrome", "Textbox[Name='Pg3']>Value"),
            ]
        )
        issue = next(
            i
            for i in lint_report(p)["issues"]
            if i["rule"] == "page-number-out-of-chrome" and "Pg1" in i["location"]
        )
        assert issue["severity"] == "error"
        assert "OverallPageNumber" in issue["message"]
        assert "OverallTotalPages" in issue["message"]

    def test_page_globals_inside_chrome_are_fine(self, rdl_path):
        from pbirb_mcp.ops.header_footer import add_footer_textbox

        add_footer_textbox(
            str(rdl_path),
            "Pager",
            '=Globals!PageName & " " & Globals!OverallPageNumber',
            "0in",
            "0in",
            "3in",
            "0.25in",
        )
        assert _chrome_issues(str(rdl_path)) == []

    def test_page_global_in_string_literal_is_ignored(self, rdl_path):
        from pbirb_mcp.ops.body import add_body_textbox

        add_body_textbox(
            str(rdl_path), "Lit", '="Globals!PageName"', "1.2in", "0.5in", "2in", "0.25in"
        )
        assert _chrome_issues(str(rdl_path)) == []

    @pytest.mark.parametrize(
        "expr",
        [
            '=First(Fields!ProductName.Value, "MainDataset")',
            '=Sum(Fields!Amount.Value, "MainDataset")',
            '="Total: " & Format(Sum(Fields!Amount.Value, "maindataset"), "N0")',
            '=Max(Fields!Amount.Value + First(Fields!Amount.Value, "MainDataset"), "MainDataset")',
            "=ReportItems!Title.Value",
        ],
    )
    def test_scoped_aggregate_in_header_is_not_flagged(self, rdl_path, expr):
        from pbirb_mcp.ops.header_footer import add_header_textbox

        add_header_textbox(str(rdl_path), "Hdr", expr, "0in", "0in", "2in", "0.25in")
        assert _chrome_issues(str(rdl_path)) == []

    @pytest.mark.parametrize(
        "expr,needle",
        [
            ("=Fields!ProductName.Value", "not inside an aggregate"),
            ("=First(Fields!ProductName.Value)", "not inside an aggregate"),
            ('=First(Fields!ProductName.Value, "NoSuchSet")', "'NoSuchSet' is not a dataset"),
            ('=Format(Fields!Amount.Value, "N0")', "not inside an aggregate"),
            (
                '=First(Fields!Amount.Value, "MainDataset") & Fields!ProductName.Value',
                "Fields!ProductName",
            ),
        ],
    )
    def test_unscoped_field_in_footer_is_flagged(self, rdl_path, expr, needle):
        from pbirb_mcp.ops.header_footer import add_footer_textbox

        add_footer_textbox(str(rdl_path), "Ftr", expr, "0in", "0in", "2in", "0.25in")
        issues = lint_report(str(rdl_path), rules=["field-in-page-chrome"])["issues"]
        assert len(issues) == 1
        assert issues[0]["severity"] == "error"
        assert issues[0]["location"] == "Textbox[Name='Ftr']>Value"
        assert needle in issues[0]["message"]
        assert "MainDataset" in issues[0]["suggestion"]

    def test_field_in_chrome_style_expression_is_flagged(self, rdl_path):
        from pbirb_mcp.ops.header_footer import add_header_textbox

        add_header_textbox(str(rdl_path), "Hdr", "x", "0in", "0in", "2in", "0.25in")
        doc = RDLDocument.open(rdl_path)
        tb = next(t for t in doc.root.iter(q("Textbox")) if t.get("Name") == "Hdr")
        etree.SubElement(
            tb.find(q("Style")), q("Color")
        ).text = '=IIf(Fields!Amount.Value > 0, "Black", "Red")'
        _save(doc)
        assert _chrome_issues(str(rdl_path)) == [
            ("field-in-page-chrome", "Textbox[Name='Hdr']>Color")
        ]

    def test_field_in_body_is_not_a_chrome_issue(self, rdl_path):
        assert lint_report(str(rdl_path), rules=["field-in-page-chrome"])["issues"] == []

    def test_page_section_missing_height(self, rdl_path):
        from pbirb_mcp.ops.validate import validate_report

        doc = RDLDocument.open(rdl_path)
        page = doc.root.find(f".//{{{RDL_NS}}}Page")
        footer = etree.Element(q("PageFooter"))
        etree.SubElement(footer, q("PrintOnFirstPage")).text = "true"
        page.insert(0, footer)
        _save(doc)
        # The XSD's choice model lets it through; the lint rule doesn't.
        assert validate_report(str(rdl_path))["valid"]
        issues = lint_report(str(rdl_path), rules=["page-section-missing-height"])["issues"]
        assert [(i["severity"], i["location"]) for i in issues] == [("error", "Page>PageFooter")]
        assert "<PageFooter> has no <Height>" in issues[0]["message"]

    def test_set_page_footer_fixes_missing_height(self, rdl_path):
        from pbirb_mcp.ops.header_footer import set_page_footer

        doc = RDLDocument.open(rdl_path)
        page = doc.root.find(f".//{{{RDL_NS}}}Page")
        footer = etree.Element(q("PageFooter"))
        etree.SubElement(footer, q("PrintOnFirstPage")).text = "true"
        page.insert(0, footer)
        _save(doc)
        set_page_footer(str(rdl_path), print_on_last_page=False)
        assert lint_report(str(rdl_path), rules=["page-section-missing-height"])["issues"] == []
