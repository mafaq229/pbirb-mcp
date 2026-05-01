"""Tests for lint_report (Phase 7 commit 31).

15 rules. Each has a clean-input test (asserts no issues fire) and a
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
from pbirb_mcp.ops.lint import ALL_RULES, lint_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

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
        assert len(ALL_RULES) == 15

    def test_unknown_rule_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="unknown lint rule"):
            lint_report(str(rdl_path), rules=["does-not-exist"])

    def test_subset_only_runs_named(self, rdl_path):
        result = lint_report(str(rdl_path), rules=["unused-data-source"])
        assert result["rules_run"] == ["unused-data-source"]


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
