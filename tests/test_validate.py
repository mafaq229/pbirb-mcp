"""Tests for validate_report (Phase 7 commit 30).

Since v0.3.1 the package ships the Microsoft RDL 2016/01 XSD (see
``pbirb_mcp/schemas/NOTICE.md`` for the redistribution permission), so
``xsd_used`` is ``True`` for every clean fixture run. The
``xsd-not-bundled`` warning path is exercised in
:mod:`tests.test_schema_bundled` (and in the dedicated test below that
patches ``_bundled_xsd_path`` to a non-existent path).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.ops.validate import validate_report, verify_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


class TestValidateReport:
    def test_clean_fixture_is_valid(self, rdl_path):
        result = validate_report(str(rdl_path))
        assert result == {"valid": True, "errors": [], "xsd_used": True}

    def test_missing_file_reports_parse_error(self, tmp_path):
        result = validate_report(str(tmp_path / "nope.rdl"))
        assert result["valid"] is False
        assert result["xsd_used"] is False
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["rule"] == "parse"
        assert err["severity"] == "error"
        assert "not found" in err["message"]

    def test_malformed_xml_reports_parse_error(self, tmp_path):
        bad = tmp_path / "broken.rdl"
        bad.write_text("<Report><unclosed>")
        result = validate_report(str(bad))
        assert result["valid"] is False
        assert result["errors"][0]["rule"] == "parse"
        assert result["xsd_used"] is False

    def test_wrong_root_namespace_is_structural(self, tmp_path):
        wrong = tmp_path / "wrong.rdl"
        wrong.write_text('<?xml version="1.0"?><NotAReport/>')
        result = validate_report(str(wrong))
        assert result["valid"] is False
        assert any(e["rule"] == "structural" for e in result["errors"])

    def test_missing_required_section_is_structural(self, rdl_path):
        # Strip <DataSets> from the fixture to trigger the structural rule.
        text = rdl_path.read_text(encoding="utf-8")
        # Crude but effective: blank out the DataSets block via slicing.
        start = text.index("<DataSets>")
        end = text.index("</DataSets>") + len("</DataSets>")
        rdl_path.write_text(text[:start] + text[end:], encoding="utf-8")
        result = validate_report(str(rdl_path))
        assert result["valid"] is False
        msgs = [e["message"] for e in result["errors"] if e["rule"] == "structural"]
        assert any("DataSets" in m for m in msgs)


class TestVerifyReport:
    def test_clean_fixture(self, rdl_path):
        result = verify_report(str(rdl_path))
        assert result["valid"] is True
        assert result["issues"] == []
        assert result["xsd_used"] is True

    def test_parse_failure_short_circuits_lint(self, tmp_path):
        bad = tmp_path / "broken.rdl"
        bad.write_text("<not-valid")
        result = verify_report(str(bad))
        assert result["valid"] is False
        assert len(result["issues"]) >= 1
        # Parse failure should be in issues; no lint rules ran.
        rules = {i["rule"] for i in result["issues"]}
        assert "parse" in rules

    def test_warning_only_keeps_valid_true(self, rdl_path):
        # Add an unused DataSource — that's only a warning. valid stays True.
        from lxml import etree

        from pbirb_mcp.core.document import RDLDocument
        from pbirb_mcp.core.xpath import q

        doc = RDLDocument.open(rdl_path)
        sources = doc.root.find(q("DataSources"))
        new_ds = etree.SubElement(sources, q("DataSource"), Name="OrphanDS")
        cp = etree.SubElement(new_ds, q("ConnectionProperties"))
        etree.SubElement(cp, q("DataProvider")).text = "SQL"
        etree.SubElement(cp, q("ConnectString")).text = "Server=x"
        doc.save()
        result = verify_report(str(rdl_path))
        # warning-only — still "valid".
        assert result["valid"] is True
        rules = {i["rule"] for i in result["issues"]}
        assert "unused-data-source" in rules

    def test_error_severity_flips_valid_to_false(self, rdl_path):
        # Inject a misplaced ColSpan (rule 15 is severity=error).
        from lxml import etree

        from pbirb_mcp.core.document import RDLDocument
        from pbirb_mcp.core.xpath import q

        doc = RDLDocument.open(rdl_path)
        cell = doc.root.iter(q("TablixCell")).__next__()
        etree.SubElement(cell, q("ColSpan")).text = "2"
        doc.save()
        result = verify_report(str(rdl_path))
        assert result["valid"] is False
        rules = {i["rule"] for i in result["issues"]}
        assert "tablix-span-misplaced" in rules


class TestXsdNotBundledWarning:
    """When the bundled XSD is missing (e.g. a source-build that didn't
    copy package-data), validate_report emits a loud warning instead of
    silently skipping the XSD layer. The silent skip masked four
    schema-conformance bugs in the v0.3.0 live-MCP sweep."""

    def _hide_xsd(self, monkeypatch, tmp_path):
        from pbirb_mcp.core import schema as core_schema

        core_schema._xsd_cache = None
        monkeypatch.setattr(core_schema, "_bundled_xsd_path", lambda: tmp_path / "missing.xsd")

    def test_validate_report_warns_when_xsd_missing(self, monkeypatch, tmp_path, rdl_path):
        self._hide_xsd(monkeypatch, tmp_path)
        result = validate_report(str(rdl_path))
        # Warning-only — `valid` stays True; warnings live in errors[].
        assert result["valid"] is True
        assert result["xsd_used"] is False
        warnings = [e for e in result["errors"] if e["rule"] == "xsd-not-bundled"]
        assert len(warnings) == 1
        w = warnings[0]
        assert w["severity"] == "warning"
        assert "xsd" in w["message"].lower() or "schema" in w["message"].lower()
        assert "suggestion" in w  # actionable

    def test_verify_report_includes_warning(self, monkeypatch, tmp_path, rdl_path):
        self._hide_xsd(monkeypatch, tmp_path)
        result = verify_report(str(rdl_path))
        assert result["valid"] is True  # warning, not error
        assert result["xsd_used"] is False
        rules = {i["rule"] for i in result["issues"]}
        assert "xsd-not-bundled" in rules

    def test_no_warning_when_xsd_bundled(self, rdl_path):
        # Sanity-check the inverse: with the real bundle present, the
        # warning is absent. Reset cache so we hit the real lookup.
        from pbirb_mcp.core import schema as core_schema

        core_schema._xsd_cache = None
        result = validate_report(str(rdl_path))
        assert result["xsd_used"] is True
        assert all(e["rule"] != "xsd-not-bundled" for e in result["errors"])


class TestToolRegistration:
    def test_validate_report_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "validate_report" in names
        assert "verify_report" in names


# ---- issue #76: Report Builder's DesignerState QueryDefinition ---------------

# What Report Builder writes into a dataset's <Query> when the query was built
# in its MDX/DAX designer (as in the "RAG Report" sample): a QueryDefinition
# whose QuerySpecification names a type from a schema that isn't bundled.
_DESIGNER_STATE = """<rd:UseGenericDesigner>true</rd:UseGenericDesigner>
        <rd:DesignerState>
          <QueryDefinition xmlns="http://schemas.microsoft.com/AnalysisServices/QueryDefinition">
            <CommandType>MDX</CommandType>
            <QuerySpecification xsi:type="MDXQuerySpecification" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
              <Select>
                <Items>
                  <Item>
                    <ID xsi:type="Level">
                      <DimensionName>Sales</DimensionName>
                      <LevelName>ProductID</LevelName>
                    </ID>
                    <ItemCaption>ProductID</ItemCaption>
                  </Item>
                </Items>
              </Select>
              <From>Model</From>
              <Filter><FilterItems /></Filter>
              <Calculations />
              <Aggregates />
              <QueryProperties />
            </QuerySpecification>
            <Query>
              <Statement>SELECT 1 ON 0 FROM [Model]</Statement>
              <ParameterDefinitions />
            </Query>
          </QueryDefinition>
        </rd:DesignerState>"""


@pytest.fixture
def designer_rdl(rdl_path: Path) -> Path:
    """The minimal fixture as Report Builder's query designer saves it."""
    text = rdl_path.read_text(encoding="utf-8")
    marker = "<rd:UseGenericDesigner>true</rd:UseGenericDesigner>"
    assert marker in text
    rdl_path.write_text(text.replace(marker, _DESIGNER_STATE, 1), encoding="utf-8")
    return rdl_path


class TestDesignerStateQueryDefinition:
    def test_untouched_report_builder_file_is_valid(self, designer_rdl):
        result = validate_report(str(designer_rdl))
        assert result == {"valid": True, "errors": [], "xsd_used": True}

    def test_verify_has_no_error(self, designer_rdl):
        result = verify_report(str(designer_rdl))
        assert result["valid"] is True, result["issues"]
        assert not [i for i in result["issues"] if i["severity"] == "error"]

    def test_a_real_rdl_error_in_the_same_file_is_still_reported(self, designer_rdl):
        text = designer_rdl.read_text(encoding="utf-8")
        designer_rdl.write_text(
            text.replace("<AutoRefresh>0</AutoRefresh>", "<AutoRefresh>0</AutoRefresh><Bogus />"),
            encoding="utf-8",
        )
        result = validate_report(str(designer_rdl))
        assert result["valid"] is False
        assert [e["rule"] for e in result["errors"]] == ["xsd"]
        assert "Bogus" in result["errors"][0]["message"]
        # Line numbers still point into the file.
        line = int(result["errors"][0]["location"].rsplit(":", 1)[1])
        assert "<Bogus />" in designer_rdl.read_text(encoding="utf-8").splitlines()[line - 1]

    def test_an_rdl_error_inside_the_dataset_is_still_reported(self, designer_rdl):
        text = designer_rdl.read_text(encoding="utf-8")
        designer_rdl.write_text(
            text.replace("<CommandText>", "<Timeout>soon</Timeout><CommandText>", 1),
            encoding="utf-8",
        )
        result = validate_report(str(designer_rdl))
        assert result["valid"] is False
        assert any("Timeout" in e["message"] for e in result["errors"])

    def test_the_document_is_left_untouched(self, designer_rdl):
        from lxml import etree

        from pbirb_mcp.core.document import RDLDocument
        from pbirb_mcp.core.schema import XSI_NS, validate_against_xsd, xsd_view

        doc = RDLDocument.open(designer_rdl)
        validate_against_xsd(doc.tree)  # raised before #76
        doc.validate()
        typed = doc.root.xpath("//*[@xsi:type]", namespaces={"xsi": XSI_NS})
        assert len(typed) == 2
        assert xsd_view(doc.tree) is not doc.tree
        # Nothing to strip: the tree itself is validated (no copy).
        clean = etree.parse(str(FIXTURE))
        assert xsd_view(clean) is clean
        assert doc.save() is False  # still byte-identical on disk

    def test_edit_and_commit_on_such_a_report(self, designer_rdl):
        from pbirb_mcp.ops.styling import set_textbox_value

        set_textbox_value(str(designer_rdl), "HeaderAmount", "Total")
        assert validate_report(str(designer_rdl))["valid"] is True
        assert "MDXQuerySpecification" in designer_rdl.read_text(encoding="utf-8")
