"""Tests for create_report (v0.4 commits 13 + 14).

create_report emits a minimal valid RDL from scratch. The
resulting file must:
  - Pass RDLDocument.open() — well-formed XML in the right namespace.
  - Pass structural validation (DataSources / DataSets /
    ReportSections present).
  - Be reachable by the read-only inventory (describe_report,
    get_datasets, get_data_source).
  - Survive a round-trip via RDLDocument.open + save without byte
    drift.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, find_child
from pbirb_mcp.ops.reader import describe_report
from pbirb_mcp.ops.scratch import create_report


class TestCreateReport:
    def test_creates_a_valid_rdl_at_path(self, tmp_path: Path):
        dst = tmp_path / "new.rdl"
        result = create_report(str(dst))
        assert result["path"] == str(dst)
        assert result["validated"] is True
        assert result["size_bytes"] == dst.stat().st_size
        assert dst.is_file()

    def test_refuses_to_clobber_existing(self, tmp_path: Path):
        dst = tmp_path / "exists.rdl"
        dst.write_text("placeholder")
        with pytest.raises(FileExistsError, match="already exists"):
            create_report(str(dst))
        # Placeholder content untouched.
        assert dst.read_text() == "placeholder"

    def test_produced_file_validates_structurally(self, tmp_path: Path):
        dst = tmp_path / "valid.rdl"
        create_report(str(dst))
        doc = RDLDocument.open(dst)
        # validate() runs structural + XSD (when bundled). No raise = OK.
        doc.validate()

    def test_default_page_setup_matches_us_letter_portrait(self, tmp_path: Path):
        dst = tmp_path / "page.rdl"
        create_report(str(dst))
        out = describe_report(path=str(dst))
        page = out["page"]
        assert page["height"] == "11in"
        assert page["width"] == "8.5in"
        assert page["margin_top"] == "1in"
        assert page["margin_bottom"] == "1in"
        assert page["margin_left"] == "1in"
        assert page["margin_right"] == "1in"

    def test_page_setup_overrides_honoured(self, tmp_path: Path):
        dst = tmp_path / "wide.rdl"
        create_report(
            str(dst),
            page_setup={
                "page_width": "16in",
                "page_height": "12in",
                "margin_left": "0.25in",
            },
        )
        out = describe_report(path=str(dst))
        assert out["page"]["width"] == "16in"
        assert out["page"]["height"] == "12in"
        assert out["page"]["margin_left"] == "0.25in"
        # Unspecified margins keep the default.
        assert out["page"]["margin_top"] == "1in"

    def test_unknown_page_setup_key_rejected(self, tmp_path: Path):
        dst = tmp_path / "bad.rdl"
        with pytest.raises(ValueError, match="unknown page_setup key"):
            create_report(str(dst), page_setup={"nonsense": "1in"})
        # File never created on the failure path.
        assert not dst.exists()

    def test_has_placeholder_datasource_and_dataset(self, tmp_path: Path):
        dst = tmp_path / "stub.rdl"
        create_report(str(dst))
        out = describe_report(path=str(dst))
        assert "DataSource1" in out["data_sources"]
        # DataSet1 references DataSource1.
        doc = RDLDocument.open(dst)
        ds = next(iter(doc.root.iter(f"{{{RDL_NS}}}DataSet")))
        ds_name_node = find_child(find_child(ds, "Query"), "DataSourceName")
        assert ds_name_node.text == "DataSource1"

    def test_body_is_empty(self, tmp_path: Path):
        dst = tmp_path / "empty_body.rdl"
        create_report(str(dst))
        out = describe_report(path=str(dst))
        assert out["body_items"] == []
        assert out["tablixes"] == []
        assert out["charts"] == []

    def test_datasource_name_override_via_datasource_arg(self, tmp_path: Path):
        dst = tmp_path / "renamed_ds.rdl"
        create_report(str(dst), datasource={"name": "MyCustomDS"})
        out = describe_report(path=str(dst))
        assert "MyCustomDS" in out["data_sources"]

    def test_round_trip_after_create(self, tmp_path: Path):
        """A no-op open + save_as on the created file must produce
        identical bytes — proves the writer emits the canonical
        save_as shape that RDLDocument can round-trip."""
        dst = tmp_path / "rt.rdl"
        create_report(str(dst))
        original = dst.read_bytes()
        original_sha = hashlib.sha256(original).hexdigest()

        rt = tmp_path / "rt-out.rdl"
        RDLDocument.open(dst).save_as(rt)
        assert hashlib.sha256(rt.read_bytes()).hexdigest() == original_sha

    def test_fresh_uuids_per_call(self, tmp_path: Path):
        """Each create_report invocation must mint fresh rd:ReportID
        and rd:DataSourceID GUIDs. Without this, two reports created
        in succession would collide in Report Builder."""
        from pbirb_mcp.core.xpath import RD_NS

        a = tmp_path / "a.rdl"
        b = tmp_path / "b.rdl"
        create_report(str(a))
        create_report(str(b))
        doc_a = RDLDocument.open(a)
        doc_b = RDLDocument.open(b)
        rid_a = doc_a.root.find(f"{{{RD_NS}}}ReportID").text
        rid_b = doc_b.root.find(f"{{{RD_NS}}}ReportID").text
        assert rid_a != rid_b


class TestCreateReportWithDatasource:
    """v0.4 commit 14 — `datasource={workspace_url, dataset_name,
    provider, ...}` wires a real PBI XMLA connection at create time.
    Both provider variants emit the same shape ``add_data_source``
    (v0.4 commit 1) produces, so the scratch-built source round-trips
    through ``_is_pbidataset_dataset`` and the rest of the tools.
    """

    def test_pbidataset_provider_emits_modern_shape(self, tmp_path: Path):
        from pbirb_mcp.ops.datasource import get_data_source

        dst = tmp_path / "pbids.rdl"
        create_report(
            str(dst),
            datasource={
                "name": "MyDS",
                "workspace_url": "ADNOC",
                "dataset_name": "RAG Report",
                "provider": "pbidataset",
            },
        )
        result = get_data_source(path=str(dst), name="MyDS")
        assert result["data_provider"] == "PBIDATASET"
        assert "pbiazure://api.powerbi.com" in result["connect_string"]
        assert "Integrated Security=ClaimsToken" in result["connect_string"]

    def test_pbidataset_adds_workspace_and_dataset_siblings(self, tmp_path: Path):
        from pbirb_mcp.core.xpath import RD_NS

        dst = tmp_path / "pbids.rdl"
        create_report(
            str(dst),
            datasource={
                "name": "MyDS",
                "workspace_url": "ADNOC",
                "dataset_name": "RAG Report",
                "provider": "pbidataset",
            },
        )
        doc = RDLDocument.open(dst)
        ds = next(d for d in doc.root.iter(f"{{{RDL_NS}}}DataSource") if d.get("Name") == "MyDS")
        assert ds.find(f"{{{RD_NS}}}PowerBIWorkspaceName").text == "ADNOC"
        assert ds.find(f"{{{RD_NS}}}PowerBIDatasetName").text == "RAG Report"

    def test_sql_provider_emits_legacy_shape(self, tmp_path: Path):
        from pbirb_mcp.ops.datasource import get_data_source

        dst = tmp_path / "sqlds.rdl"
        create_report(
            str(dst),
            datasource={
                "name": "MyDS",
                "workspace_url": "MyWorkspace",
                "dataset_name": "SalesAnalytics",
                "provider": "sql",
            },
        )
        result = get_data_source(path=str(dst), name="MyDS")
        assert result["data_provider"] == "SQL"
        assert "powerbi://api.powerbi.com" in result["connect_string"]
        assert "MyWorkspace" in result["connect_string"]
        assert result["security_type"] == "Integrated"

    def test_default_provider_is_sql(self, tmp_path: Path):
        from pbirb_mcp.ops.datasource import get_data_source

        dst = tmp_path / "default.rdl"
        create_report(
            str(dst),
            datasource={
                "name": "MyDS",
                "workspace_url": "MyWS",
                "dataset_name": "MyDS",
            },
        )
        result = get_data_source(path=str(dst), name="MyDS")
        assert result["data_provider"] == "SQL"

    def test_unknown_provider_rejected(self, tmp_path: Path):
        dst = tmp_path / "bad.rdl"
        with pytest.raises(ValueError, match="unknown provider"):
            create_report(
                str(dst),
                datasource={
                    "name": "X",
                    "workspace_url": "WS",
                    "dataset_name": "DS",
                    "provider": "not-real",
                },
            )
        assert not dst.exists()

    def test_unknown_datasource_key_rejected(self, tmp_path: Path):
        dst = tmp_path / "bad.rdl"
        with pytest.raises(ValueError, match="unknown datasource key"):
            create_report(
                str(dst),
                datasource={
                    "name": "X",
                    "garbage_key": "no",
                },
            )
        assert not dst.exists()

    def test_pbidataset_source_recognised_by_is_pbidataset_helper(self, tmp_path: Path):
        from lxml import etree

        from pbirb_mcp.core.xpath import q
        from pbirb_mcp.ops.dataset import _is_pbidataset_dataset

        dst = tmp_path / "checked.rdl"
        create_report(
            str(dst),
            datasource={
                "name": "MyDS",
                "workspace_url": "ADNOC",
                "dataset_name": "RAG Report",
                "provider": "pbidataset",
            },
        )
        doc = RDLDocument.open(dst)
        # Build a synthetic DataSet bound to MyDS and check the helper.
        datasets_root = doc.root.find(f"{{{RDL_NS}}}DataSets")
        synthetic = etree.SubElement(datasets_root, q("DataSet"), Name="Probe")
        query = etree.SubElement(synthetic, q("Query"))
        etree.SubElement(query, q("DataSourceName")).text = "MyDS"
        assert _is_pbidataset_dataset(doc, synthetic) is True

    def test_integrated_security_false_omits_claimstoken(self, tmp_path: Path):
        from pbirb_mcp.ops.datasource import get_data_source

        dst = tmp_path / "noauth.rdl"
        create_report(
            str(dst),
            datasource={
                "name": "MyDS",
                "workspace_url": "ADNOC",
                "dataset_name": "RAG Report",
                "provider": "pbidataset",
                "integrated_security": False,
            },
        )
        result = get_data_source(path=str(dst), name="MyDS")
        assert "Integrated Security=ClaimsToken" not in result["connect_string"]


def _ds_canonical(path: Path, name: str) -> str:
    """The DataSource's XML with whitespace and its random DataSourceID
    normalised away."""
    from lxml import etree

    from pbirb_mcp.core.xpath import RD_NS

    doc = RDLDocument.open(path)
    ds = next(d for d in doc.root.iter(f"{{{RDL_NS}}}DataSource") if d.get("Name") == name)
    ds = etree.fromstring(etree.tostring(ds))
    # Namespaces merely in scope (create_report's df, #88) aren't shape.
    etree.cleanup_namespaces(ds)
    ds.find(f"{{{RD_NS}}}DataSourceID").text = "-"
    for el in ds.iter():
        el.tail = None
        if el.text is not None and not el.text.strip():
            el.text = None
    return etree.tostring(ds, encoding="unicode")


class TestOneConnectionShape:
    """#71: create_report, add_data_source and set_datasource_connection
    write one identical shape, per provider."""

    FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"

    @pytest.mark.parametrize(
        ("provider", "extra"),
        [
            ("pbidataset", {}),
            ("pbidataset", {"catalog_id": "sobe-1", "identity_provider": "p, q"}),
            ("sql", {}),
            ("sql", {"catalog_id": "sobe-1"}),
        ],
    )
    def test_three_entry_points_agree(self, tmp_path: Path, provider, extra):
        import shutil

        from pbirb_mcp.ops.datasource import add_data_source, set_datasource_connection
        from pbirb_mcp.ops.validate import validate_report

        ws, model = "powerbi://api.powerbi.com/v1.0/myorg/Sales WS", "Sales; Model"
        scratch = tmp_path / "scratch.rdl"
        result = create_report(
            str(scratch),
            datasource={
                "name": "Src",
                "workspace_url": ws,
                "dataset_name": model,
                "provider": provider,
                **extra,
            },
        )
        added = tmp_path / "added.rdl"
        shutil.copy(self.FIXTURE, added)
        add_data_source(str(added), "Src", ws, model, provider=provider, **extra)
        repointed = tmp_path / "repointed.rdl"
        shutil.copy(self.FIXTURE, repointed)
        add_data_source(str(repointed), "Src", "Old WS", "Old", provider=provider)
        set_datasource_connection(str(repointed), "Src", ws, model, **extra)

        shape = _ds_canonical(scratch, "Src")
        assert _ds_canonical(added, "Src") == shape
        assert _ds_canonical(repointed, "Src") == shape
        for p in (scratch, added, repointed):
            assert validate_report(str(p))["valid"]
        if provider == "pbidataset":
            assert "<rd:SecurityType>None</rd:SecurityType>" in shape
            assert "<rd:PowerBIWorkspaceName>Sales WS</rd:PowerBIWorkspaceName>" in shape
            assert ("warnings" in result) == (not extra)
        assert 'Initial Catalog="' in shape or "Initial Catalog=sobe-1" in shape


class TestReportBuilderScaffold:
    """#88: the scaffold looks like Report Builder's output and can declare
    DataSet1's fields."""

    DF_NS = "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition/defaultfontfamily"

    def test_default_output_is_indented_and_valid(self, tmp_path: Path):
        from pbirb_mcp.ops.validate import validate_report

        dst = tmp_path / "new.rdl"
        create_report(str(dst))
        text = dst.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert len(lines) > 30
        assert lines[0] == '<?xml version="1.0" encoding="utf-8"?>'
        assert "  <AutoRefresh>0</AutoRefresh>" in lines
        assert "        <ConnectString />" in lines  # six levels deep, 2-space indent
        assert validate_report(str(dst))["valid"]

    def test_df_namespace_in_report_builder_order(self, tmp_path: Path):
        dst = tmp_path / "new.rdl"
        create_report(str(dst))
        lines = dst.read_text(encoding="utf-8").splitlines()
        assert lines[1] == (
            f'<Report MustUnderstand="df" xmlns="{RDL_NS}" '
            'xmlns:rd="http://schemas.microsoft.com/SQLServer/reporting/reportdesigner" '
            f'xmlns:df="{self.DF_NS}">'
        )
        assert lines[2] == "  <df:DefaultFontFamily>Segoe UI</df:DefaultFontFamily>"
        root = RDLDocument.open(dst).root
        assert root.get("MustUnderstand") == "df"
        assert root[0].tag == f"{{{self.DF_NS}}}DefaultFontFamily"
        assert root[1].tag == f"{{{RDL_NS}}}AutoRefresh"

    def test_noop_reopen_save_is_byte_identical(self, tmp_path: Path):
        dst = tmp_path / "new.rdl"
        create_report(str(dst))
        before = dst.read_bytes()
        doc = RDLDocument.open(dst)
        assert doc.to_bytes() == before

    def test_default_has_no_fields_block(self, tmp_path: Path):
        dst = tmp_path / "new.rdl"
        create_report(str(dst))
        assert "<Fields>" not in dst.read_text(encoding="utf-8")

    def test_fields_emitted(self, tmp_path: Path):
        from pbirb_mcp.ops.dataset import get_dataset
        from pbirb_mcp.ops.validate import validate_report

        dst = tmp_path / "new.rdl"
        create_report(
            str(dst),
            fields=[
                "Amount",
                {"name": "Region", "data_field": "Sales[Region]", "type_name": "System.String"},
                {"name": "Qty", "type_name": "System.Int32"},
            ],
        )
        fields = {f["name"]: f for f in get_dataset(str(dst), "DataSet1")["fields"]}
        assert list(fields) == ["Amount", "Region", "Qty"]
        assert fields["Amount"]["data_field"] == "Amount"
        assert fields["Region"]["data_field"] == "Sales[Region]"
        assert fields["Region"]["type_name"] == "System.String"
        assert fields["Qty"]["data_field"] == "Qty"
        assert validate_report(str(dst))["valid"]

    @pytest.mark.parametrize(
        ("fields", "match"),
        [
            (["Amount", "amount"], "more than once"),
            (["Not valid"], "field"),
            ([{"data_field": "X"}], "'name' is required"),
            ([{"name": "A", "bogus": 1}], "unknown key"),
            ([3], "expected a field name"),
        ],
    )
    def test_bad_fields_rejected_before_writing(self, tmp_path: Path, fields, match):
        dst = tmp_path / "new.rdl"
        with pytest.raises(ValueError, match=match):
            create_report(str(dst), fields=fields)
        assert not dst.exists()

    def test_fields_then_template_tablix_lints_clean(self, tmp_path: Path):
        from pbirb_mcp.ops.lint import lint_report
        from pbirb_mcp.ops.templates import insert_tablix_from_template
        from pbirb_mcp.ops.validate import validate_report

        dst = tmp_path / "new.rdl"
        create_report(str(dst), fields=["Amount"])
        insert_tablix_from_template(
            str(dst),
            name="T",
            dataset_name="DataSet1",
            columns=["Amount"],
            top="0in",
            left="0in",
            width="1in",
            height="0.5in",
        )
        issues = lint_report(str(dst))["issues"]
        assert [i for i in issues if i["rule"] != "unused-data-source"] == []
        assert validate_report(str(dst))["valid"]

    def test_fields_via_server(self, tmp_path: Path):
        import json

        from pbirb_mcp.server import MCPServer
        from pbirb_mcp.tools import register_all_tools

        server = MCPServer()
        register_all_tools(server)
        dst = tmp_path / "new.rdl"
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "create_report",
                    "arguments": {"path": str(dst), "fields": ["A", {"name": "B"}]},
                },
            }
        )
        result = resp["result"]
        assert not result.get("isError"), json.loads(result["content"][0]["text"])
        assert dst.read_text(encoding="utf-8").count("<Field Name=") == 2


class TestCreateReportToolRegistration:
    def test_tool_registered(self):
        from pbirb_mcp.server import MCPServer
        from pbirb_mcp.tools import register_all_tools

        server = MCPServer()
        register_all_tools(server)
        assert "create_report" in server._tools
