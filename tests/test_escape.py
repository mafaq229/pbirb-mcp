"""Tests for Phase 12 commit 45 — XPath escape-hatch tools.

The escape hatch is the safety valve for cases the structured surface
doesn't cover yet. These tests cover the contract: read-only view,
single-element replace, and refusals on the dangerous shapes
(multi-match, root, malformed content).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops.escape import (
    TRUNCATION_MARKER_PREFIX,
    raw_xml_insert,
    raw_xml_remove,
    raw_xml_replace,
    raw_xml_view,
)
from pbirb_mcp.ops.validate import validate_report
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
        assert out["count"] == 1
        xml = out["matches"][0]["xml"]
        assert "DataSource" in xml
        assert 'Name="PowerBIDataset"' in xml

    def test_returns_multiple_matches(self, rdl_path):
        out = raw_xml_view(
            path=str(rdl_path),
            xpath=".//r:ReportParameter",
        )
        # Fixture has 2 ReportParameters: DateFrom, DateTo.
        assert out["count"] == 2
        assert [m["kind"] for m in out["matches"]] == ["element", "element"]

    def test_returns_empty_on_no_match(self, rdl_path):
        out = raw_xml_view(
            path=str(rdl_path),
            xpath=".//r:DataSource[@Name='NoSuch']",
        )
        assert out == {"kind": "nodes", "count": 0, "matches": [], "truncated": False}

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

    def test_identical_content_reports_no_change_and_keeps_file(self, rdl_path):
        # #61: replacing an element with what's already there is a no-op.
        st = rdl_path.stat()
        before = (rdl_path.read_bytes(), st.st_ino, st.st_mtime_ns)
        result = raw_xml_replace(
            path=str(rdl_path),
            xpath="r:ReportSections/r:ReportSection/r:Body/r:Height",
            content="<Height>2in</Height>",
        )
        assert result["changed"] is False
        st = rdl_path.stat()
        assert (rdl_path.read_bytes(), st.st_ino, st.st_mtime_ns) == before

    def test_layout_only_difference_is_no_change(self, rdl_path):
        # The same subtree written compact matches the pretty-printed one.
        result = raw_xml_replace(
            path=str(rdl_path),
            xpath=".//r:Tablix[@Name='MainTable']/r:Style",
            content="<Style><Border><Style>None</Style></Border></Style>",
        )
        assert result["changed"] is False

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
        assert {"raw_xml_insert", "raw_xml_remove"} <= names

    def test_remove_is_destructive_insert_is_not(self):
        from pbirb_mcp.core.toolsets import toolset_of
        from pbirb_mcp.server import AUTO_BACKUP_TOOLS, DESTRUCTIVE_TOOLS

        assert "raw_xml_remove" in DESTRUCTIVE_TOOLS
        assert "raw_xml_remove" in AUTO_BACKUP_TOOLS
        assert "raw_xml_insert" not in DESTRUCTIVE_TOOLS
        assert toolset_of("raw_xml_insert") == toolset_of("raw_xml_remove") == "admin"

    def test_remove_needs_confirm_when_required(self, rdl_path, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_REQUIRE_CONFIRM", "1")
        srv = MCPServer()
        register_all_tools(srv)
        before = rdl_path.read_bytes()
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "raw_xml_remove",
                    "arguments": {"path": str(rdl_path), "xpath": "rd:ReportUnitType"},
                },
            }
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["error_type"] == "ConfirmationRequired"
        assert rdl_path.read_bytes() == before


# ---- #85: namespaces, typed results, hygiene -----------------------------

AM_NS = "http://schemas.microsoft.com/sqlserver/reporting/authoringmetadata"
DF_NS = (
    "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition/defaultfontfamily"
)
PAGE = "r:ReportSections/r:ReportSection/r:Page"


@pytest.fixture
def rb_path(tmp_path: Path) -> Path:
    """The minimal fixture with Report Builder's am:/df: header: `am` is
    declared on <am:AuthoringMetadata> itself, `df` on the root."""
    text = FIXTURE.read_text()
    start = text.index("<Report ")
    end = text.index(">", start) + 1
    head = (
        f'<Report MustUnderstand="df" xmlns="{RDL_NS}" '
        'xmlns:rd="http://schemas.microsoft.com/SQLServer/reporting/reportdesigner" '
        f'xmlns:df="{DF_NS}">\n'
        f'  <am:AuthoringMetadata xmlns:am="{AM_NS}">\n'
        "    <am:CreatedBy>\n      <am:Name>MSRB</am:Name>\n"
        "      <am:Version>15.0.20283.0</am:Version>\n    </am:CreatedBy>\n"
        "  </am:AuthoringMetadata>\n"
        "  <df:DefaultFontFamily>Segoe UI</df:DefaultFontFamily>"
    )
    dest = tmp_path / "rb.rdl"
    dest.write_text(text[:start] + head + text[end:])
    assert validate_report(str(dest))["valid"]
    return dest


class TestViewNamespaces:
    def test_am_prefix_resolves(self, rb_path):
        out = raw_xml_view(str(rb_path), "//am:AuthoringMetadata")
        assert out["count"] == 1
        assert "<am:Name>MSRB</am:Name>" in out["matches"][0]["xml"]

    def test_df_prefix_resolves(self, rb_path):
        out = raw_xml_view(str(rb_path), "string(//df:DefaultFontFamily)")
        assert out == {"kind": "string", "value": "Segoe UI"}

    def test_root_prefixes_are_bound(self, tmp_path):
        dest = tmp_path / "x.rdl"
        dest.write_text(FIXTURE.read_text().replace("<Report ", '<Report xmlns:zz="urn:zz" ', 1))
        assert raw_xml_view(str(dest), "count(//zz:Nothing)") == {"kind": "number", "value": 0}


class TestViewTypedResults:
    def test_attribute_results(self, rdl_path):
        out = raw_xml_view(str(rdl_path), "//r:ReportParameter/@Name")
        assert out["count"] == 2
        assert out["matches"] == [
            {
                "kind": "attribute",
                "name": "Name",
                "value": "DateFrom",
                "element": "ReportParameter",
            },
            {"kind": "attribute", "name": "Name", "value": "DateTo", "element": "ReportParameter"},
        ]

    def test_text_results(self, rdl_path):
        out = raw_xml_view(str(rdl_path), f"{PAGE}/r:PageHeight/text()")
        assert out["matches"] == [{"kind": "text", "value": "11in"}]

    def test_count_is_a_number(self, rdl_path):
        assert raw_xml_view(str(rdl_path), "count(//r:Textbox)") == {"kind": "number", "value": 6}

    def test_fractional_number(self, rdl_path):
        assert raw_xml_view(str(rdl_path), "count(//r:Textbox) div 4")["value"] == 1.5

    def test_string_and_boolean(self, rdl_path):
        assert raw_xml_view(str(rdl_path), "string(//r:Textbox/@Name)") == {
            "kind": "string",
            "value": "HeaderProductID",
        }
        assert raw_xml_view(str(rdl_path), "boolean(//r:Tablix)") == {
            "kind": "boolean",
            "value": True,
        }

    def test_nan_becomes_null(self, rdl_path):
        assert raw_xml_view(str(rdl_path), "number('x')") == {"kind": "number", "value": None}


class TestViewHygiene:
    def test_no_tail_and_no_redundant_xmlns(self, rdl_path):
        xml = raw_xml_view(str(rdl_path), f"{PAGE}/r:PageHeight")["matches"][0]["xml"]
        assert xml == f'<PageHeight xmlns="{RDL_NS}">11in</PageHeight>'

    def test_used_prefixes_are_kept(self, rdl_path):
        xml = raw_xml_view(str(rdl_path), ".//r:Textbox[@Name='Amount']")["matches"][0]["xml"]
        assert xml.count("xmlns") == 2  # RDL default + rd (rd:DefaultName)
        assert xml.endswith("</Textbox>")
        etree.fromstring(xml)

    def test_element_xml_pastes_back_as_a_no_op(self, rdl_path):
        xpath = ".//r:Textbox[@Name='Amount']"
        xml = raw_xml_view(str(rdl_path), xpath)["matches"][0]["xml"]
        assert raw_xml_replace(str(rdl_path), xpath, xml)["changed"] is False

    def test_respects_max_bytes(self, rdl_path):
        out = raw_xml_view(str(rdl_path), ".//r:Textbox", max_bytes=500)
        assert out["truncated"] is True
        assert out["count"] == 6
        texts = [m["xml"] for m in out["matches"]]
        cut = texts[-1][: texts[-1].index(TRUNCATION_MARKER_PREFIX)]
        assert sum(len(t.encode()) for t in texts[:-1]) + len(cut.encode()) <= 500
        assert out["matches"][-1]["truncated"] is True
        assert all("truncated" not in m for m in out["matches"][:-1])

    def test_attribute_values_count_toward_the_cap(self, rdl_path):
        out = raw_xml_view(str(rdl_path), "//r:Textbox/@Name", max_bytes=20)
        assert out["truncated"] is True
        assert len(out["matches"]) < 6


# ---- #85: replace hardening ------------------------------------------------


class TestReplaceHardening:
    HEIGHT = f"{PAGE}/r:PageHeight"

    def test_identity_replace_keeps_the_inode(self, rdl_path):
        st = rdl_path.stat()
        before = (rdl_path.read_bytes(), st.st_ino, st.st_mtime_ns)
        result = raw_xml_replace(str(rdl_path), self.HEIGHT, "<PageHeight>11in</PageHeight>")
        assert result["changed"] is False
        st = rdl_path.stat()
        assert (rdl_path.read_bytes(), st.st_ino, st.st_mtime_ns) == before

    def test_attribute_order_is_not_a_change(self, rdl_path):
        xpath = "r:DataSources/r:DataSource"
        xml = raw_xml_view(str(rdl_path), xpath)["matches"][0]["xml"]
        one = xml.replace('Name="PowerBIDataset"', 'Name="PowerBIDataset" rd:Tag="1"')
        other = xml.replace('Name="PowerBIDataset"', 'rd:Tag="1" Name="PowerBIDataset"')
        assert raw_xml_replace(str(rdl_path), xpath, one)["changed"] is True
        assert raw_xml_replace(str(rdl_path), xpath, other)["changed"] is False

    def test_wrong_namespace_refused(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="same element"):
            raw_xml_replace(
                str(rdl_path),
                f"{PAGE}/r:PageWidth",
                '<PageWidth xmlns="urn:x">9in</PageWidth>',
            )
        assert rdl_path.read_bytes() == before

    def test_different_element_refused(self, rdl_path):
        with pytest.raises(ValueError, match="allow_kind_change"):
            raw_xml_replace(str(rdl_path), self.HEIGHT, "<PageWidth>9in</PageWidth>")

    def test_allow_kind_change(self, rdl_path):
        result = raw_xml_replace(
            str(rdl_path),
            "r:ReportSections/r:ReportSection/r:Page/r:LeftMargin",
            "<RightMargin>1in</RightMargin>",
            allow_kind_change=True,
        )
        assert result == {
            "xpath": "r:ReportSections/r:ReportSection/r:Page/r:LeftMargin",
            "kind": "RightMargin",
            "changed": True,
        }
        assert validate_report(str(rdl_path))["valid"]

    def test_xsd_invalid_result_refused(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="invalid.*xs:boolean"):
            raw_xml_replace(
                str(rdl_path),
                ".//r:Textbox[@Name='HeaderAmount']/r:CanGrow",
                "<CanGrow>maybe</CanGrow>",
            )
        assert rdl_path.read_bytes() == before

    def test_preexisting_xsd_errors_dont_block(self, rdl_path):
        rdl_path.write_text(rdl_path.read_text().replace("<CanGrow>true", "<CanGrow>maybe", 1))
        assert not validate_report(str(rdl_path))["valid"]
        result = raw_xml_replace(str(rdl_path), self.HEIGHT, "<PageHeight>10in</PageHeight>")
        assert result["changed"] is True

    def test_inside_a_transaction_the_commit_checks(self, rdl_path):
        from pbirb_mcp.core import transactions
        from pbirb_mcp.ops.transactions import (
            cancel_editing_transaction,
            start_editing_transaction,
        )

        tx = start_editing_transaction(str(rdl_path))["transaction_id"]
        try:
            with transactions.activate(tx):
                result = raw_xml_replace(
                    str(rdl_path),
                    ".//r:Textbox[@Name='HeaderAmount']/r:CanGrow",
                    "<CanGrow>maybe</CanGrow>",
                )
            assert result["changed"] is True
        finally:
            cancel_editing_transaction(tx)

    def test_duplicate_item_name_refused(self, rdl_path):
        with pytest.raises(ValueError, match="already exists"):
            raw_xml_replace(
                str(rdl_path),
                ".//r:Textbox[@Name='Amount']",
                raw_xml_view(str(rdl_path), ".//r:Textbox[@Name='Amount']")["matches"][0][
                    "xml"
                ].replace('Name="Amount"', 'Name="ProductID"'),
            )

    def test_non_element_target_refused(self, rdl_path):
        with pytest.raises(ValueError, match="non-element"):
            raw_xml_replace(str(rdl_path), "//r:Textbox/@Name", "<X/>")

    def test_scalar_target_refused(self, rdl_path):
        with pytest.raises(ValueError, match="number, not elements"):
            raw_xml_replace(str(rdl_path), "count(//r:Textbox)", "<X/>")


# ---- #85: raw_xml_insert / raw_xml_remove ----------------------------------


class TestInsertRemove:
    PARAMS = "r:ReportParameters"
    LAST = "r:ReportParameters/r:ReportParameter[last()]"
    FIRST = "r:ReportParameters/r:ReportParameter[1]"

    def _xml(self, path, xpath):
        return raw_xml_view(str(path), xpath)["matches"][0]["xml"]

    @pytest.mark.parametrize(
        ("victim", "anchor", "position"),
        [
            (LAST, PARAMS, "append"),
            (FIRST, PARAMS, "prepend"),
            (LAST, FIRST, "after"),
            (FIRST, LAST, "before"),
        ],
    )
    def test_remove_then_insert_round_trips_byte_for_byte(self, rdl_path, victim, anchor, position):
        before = rdl_path.read_bytes()
        xml = self._xml(rdl_path, victim)
        removed = raw_xml_remove(str(rdl_path), victim)
        assert removed == {
            "xpath": victim,
            "removed": ["ReportParameter"],
            "count": 1,
            "changed": True,
        }
        assert raw_xml_view(str(rdl_path), "count(//r:ReportParameter)")["value"] == 1
        assert validate_report(str(rdl_path))["valid"]
        inserted = raw_xml_insert(str(rdl_path), anchor, xml, position=position)
        assert inserted == {
            "xpath": anchor,
            "position": position,
            "inserted": ["ReportParameter"],
            "changed": True,
        }
        assert rdl_path.read_bytes() == before

    def test_insert_several_elements_in_order(self, rdl_path):
        fields = "r:DataSets/r:DataSet/r:Fields"
        content = "".join(
            f'<Field Name="{n}"><DataField>{n}</DataField>'
            "<rd:TypeName>System.String</rd:TypeName></Field>"
            for n in ("A", "B")
        )
        result = raw_xml_insert(str(rdl_path), fields, content)
        assert result["inserted"] == ["Field", "Field"]
        names = raw_xml_view(str(rdl_path), f"{fields}/r:Field/@Name")["matches"]
        assert [m["value"] for m in names][-2:] == ["A", "B"]
        assert validate_report(str(rdl_path))["valid"]
        # Indented like its siblings.
        assert '\n        <Field Name="A">\n          <DataField>A</DataField>' in (
            rdl_path.read_text()
        )

    def test_insert_am_element(self, rb_path):
        raw_xml_insert(
            str(rb_path),
            "am:AuthoringMetadata",
            "<am:LastModifiedTimestamp>2026-11-05T00:00:00Z</am:LastModifiedTimestamp>",
        )
        out = raw_xml_view(str(rb_path), "string(//am:LastModifiedTimestamp)")
        assert out["value"] == "2026-11-05T00:00:00Z"
        assert validate_report(str(rb_path))["valid"]

    def test_insert_refuses_xsd_invalid_result(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="invalid"):
            raw_xml_insert(str(rdl_path), self.PARAMS, "<Bogus/>")
        assert rdl_path.read_bytes() == before

    def test_insert_refuses_duplicate_item_name(self, rdl_path):
        with pytest.raises(ValueError, match="already exists"):
            raw_xml_insert(
                str(rdl_path),
                "r:ReportSections/r:ReportSection/r:Body/r:ReportItems",
                self._xml(rdl_path, ".//r:Textbox[@Name='Amount']"),
            )

    def test_insert_refusals(self, rdl_path):
        with pytest.raises(ValueError, match="before the <Report> root"):
            raw_xml_insert(str(rdl_path), "/r:Report", "<AutoRefresh>0</AutoRefresh>", "before")
        with pytest.raises(ValueError, match="matched 2"):
            raw_xml_insert(str(rdl_path), ".//r:ReportParameter", "<Prompt>x</Prompt>")
        with pytest.raises(ElementNotFoundError):
            raw_xml_insert(str(rdl_path), ".//r:Nope", "<Prompt>x</Prompt>")
        with pytest.raises(ValueError, match="position must be"):
            raw_xml_insert(str(rdl_path), self.PARAMS, "<X/>", position="inside")
        with pytest.raises(ValueError, match="got zero"):
            raw_xml_insert(str(rdl_path), self.PARAMS, "just text")
        with pytest.raises(ValueError, match="text outside"):
            raw_xml_insert(str(rdl_path), self.PARAMS, "<Prompt>x</Prompt> stray")

    def test_remove_refuses_several_matches_unless_all(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="matched 2.*all=true"):
            raw_xml_remove(str(rdl_path), ".//r:ReportParameter/r:Prompt")
        assert rdl_path.read_bytes() == before
        result = raw_xml_remove(str(rdl_path), ".//r:ReportParameter/r:Prompt", all=True)
        assert result["count"] == 2
        assert raw_xml_view(str(rdl_path), "count(//r:Prompt)")["value"] == 0
        assert validate_report(str(rdl_path))["valid"]

    def test_remove_nested_matches_once(self, rdl_path):
        result = raw_xml_remove(
            str(rdl_path),
            ".//r:ReportParameter[@Name='DateTo'] | .//r:ReportParameter/r:Prompt",
            all=True,
        )
        # DateTo's Prompt goes with DateTo; DateFrom's is removed on its own.
        assert result["removed"] == ["Prompt", "ReportParameter"]

    def test_remove_refuses_root_and_non_elements(self, rdl_path):
        with pytest.raises(ValueError, match="<Report> root"):
            raw_xml_remove(str(rdl_path), "/r:Report")
        with pytest.raises(ValueError, match="non-element"):
            raw_xml_remove(str(rdl_path), "//r:Textbox/@Name", all=True)
        with pytest.raises(ElementNotFoundError):
            raw_xml_remove(str(rdl_path), ".//r:Nope")

    def test_remove_refuses_xsd_invalid_result(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="Missing child"):
            raw_xml_remove(str(rdl_path), "r:DataSets/r:DataSet/r:Fields/r:Field", all=True)
        assert rdl_path.read_bytes() == before

    def test_remove_last_child_keeps_layout(self, rdl_path):
        raw_xml_remove(str(rdl_path), "r:DataSources/r:DataSource/rd:DataSourceID")
        text = rdl_path.read_text()
        assert "</rd:SecurityType>\n    </DataSource>" in text
