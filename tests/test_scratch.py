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


class TestCreateReportToolRegistration:
    def test_tool_registered(self):
        from pbirb_mcp.server import MCPServer
        from pbirb_mcp.tools import register_all_tools

        server = MCPServer()
        register_all_tools(server)
        assert "create_report" in server._tools
