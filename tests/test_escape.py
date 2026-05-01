"""Tests for Phase 12 commit 45 — XPath escape-hatch tools.

The escape hatch is the safety valve for cases the structured surface
doesn't cover yet. These tests cover the contract: read-only view,
single-element replace, and refusals on the dangerous shapes
(multi-match, root, malformed content).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops.escape import raw_xml_replace, raw_xml_view
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


# ---- raw_xml_view -------------------------------------------------------


class TestRawXmlView:
    def test_returns_single_match(self, rdl_path):
        out = raw_xml_view(
            path=str(rdl_path),
            xpath="r:DataSources/r:DataSource[@Name='PowerBIDataset']",
        )
        assert len(out) == 1
        assert "DataSource" in out[0]
        assert 'Name="PowerBIDataset"' in out[0]

    def test_returns_multiple_matches(self, rdl_path):
        out = raw_xml_view(
            path=str(rdl_path),
            xpath=".//r:ReportParameter",
        )
        # Fixture has 2 ReportParameters: DateFrom, DateTo.
        assert len(out) == 2

    def test_returns_empty_on_no_match(self, rdl_path):
        out = raw_xml_view(
            path=str(rdl_path),
            xpath=".//r:DataSource[@Name='NoSuch']",
        )
        assert out == []

    def test_invalid_xpath_raises_value_error(self, rdl_path):
        with pytest.raises(ValueError, match="invalid xpath"):
            raw_xml_view(path=str(rdl_path), xpath="(((not-valid")

    def test_empty_xpath_raises(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            raw_xml_view(path=str(rdl_path), xpath="")

    def test_does_not_save(self, rdl_path):
        before = rdl_path.read_bytes()
        raw_xml_view(path=str(rdl_path), xpath=".//r:Textbox")
        assert rdl_path.read_bytes() == before


# ---- raw_xml_replace ----------------------------------------------------


class TestRawXmlReplace:
    def test_replaces_single_element(self, rdl_path):
        # Replace HeaderProductID's <rd:DefaultName> with a different value.
        # First confirm what's there.
        before = raw_xml_view(
            path=str(rdl_path),
            xpath=".//r:Textbox[@Name='HeaderProductID']/rd:DefaultName",
        )
        assert before  # exists

        result = raw_xml_replace(
            path=str(rdl_path),
            xpath=".//r:Textbox[@Name='HeaderProductID']/rd:DefaultName",
            content="<rd:DefaultName>Replaced</rd:DefaultName>",
        )
        assert result == {
            "xpath": ".//r:Textbox[@Name='HeaderProductID']/rd:DefaultName",
            "kind": "DefaultName",
            "changed": True,
        }
        # Read back: the new value is in place.
        doc = RDLDocument.open(rdl_path)
        rd_ns = "http://schemas.microsoft.com/SQLServer/reporting/reportdesigner"
        names = doc.root.xpath(
            ".//r:Textbox[@Name='HeaderProductID']/rd:DefaultName",
            namespaces={"r": RDL_NS, "rd": rd_ns},
        )
        assert names[0].text == "Replaced"

    def test_default_namespace_injected_for_bare_content(self, rdl_path):
        # Replace a Style element with new content using the BARE form
        # (no xmlns declaration). The wrapper injects RDL as default
        # namespace so this just works.
        result = raw_xml_replace(
            path=str(rdl_path),
            xpath=".//r:Tablix[@Name='MainTable']/r:Style",
            content=("<Style><Border><Style>Solid</Style><Color>#ff0000</Color></Border></Style>"),
        )
        assert result["changed"] is True
        # New border color is in place.
        doc = RDLDocument.open(rdl_path)
        colors = doc.root.xpath(
            ".//r:Tablix[@Name='MainTable']/r:Style/r:Border/r:Color",
            namespaces={"r": RDL_NS},
        )
        assert colors[0].text == "#ff0000"

    def test_zero_matches_raises_not_found(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            raw_xml_replace(
                path=str(rdl_path),
                xpath=".//r:DataSource[@Name='Ghost']",
                content="<DataSource Name='Ghost' />",
            )

    def test_multi_match_refused(self, rdl_path):
        # ".//r:ReportParameter" matches 2 elements in the fixture.
        with pytest.raises(ValueError, match="matched 2"):
            raw_xml_replace(
                path=str(rdl_path),
                xpath=".//r:ReportParameter",
                content="<ReportParameter Name='X' />",
            )

    def test_root_replace_refused(self, rdl_path):
        with pytest.raises(ValueError, match="<Report> root"):
            raw_xml_replace(
                path=str(rdl_path),
                xpath="/r:Report",
                content="<Report />",
            )

    def test_malformed_content_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="not valid XML"):
            raw_xml_replace(
                path=str(rdl_path),
                xpath=".//r:Textbox[@Name='HeaderProductID']/rd:DefaultName",
                content="<unclosed>",
            )

    def test_zero_top_level_elements_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="zero"):
            raw_xml_replace(
                path=str(rdl_path),
                xpath=".//r:Textbox[@Name='HeaderProductID']/rd:DefaultName",
                content="not-xml-just-text",
            )

    def test_multiple_top_level_elements_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="got 2"):
            raw_xml_replace(
                path=str(rdl_path),
                xpath=".//r:Textbox[@Name='HeaderProductID']/rd:DefaultName",
                content="<rd:DefaultName>A</rd:DefaultName><rd:DefaultName>B</rd:DefaultName>",
            )

    def test_round_trip_safe_after_replace(self, rdl_path):
        raw_xml_replace(
            path=str(rdl_path),
            xpath=".//r:Tablix[@Name='MainTable']/r:Style",
            content=("<Style><Border><Style>None</Style></Border></Style>"),
        )
        # File must reopen + structurally validate.
        RDLDocument.open(rdl_path).validate()


# ---- registration ------------------------------------------------------


class TestToolRegistration:
    def test_escape_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "raw_xml_view" in names
        assert "raw_xml_replace" in names
