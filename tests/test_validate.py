"""Tests for validate_report (Phase 7 commit 30).

The bundled package doesn't ship the (non-redistributable) Microsoft RDL
XSD, so ``xsd_used`` is always ``False`` in CI. Structural validation is
the load-bearing layer here; the XSD path is exercised at the
:mod:`pbirb_mcp.core.schema` unit-test level.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.ops.validate import validate_report
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
        assert result == {"valid": True, "errors": [], "xsd_used": False}

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


class TestToolRegistration:
    def test_validate_report_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "validate_report" in names
