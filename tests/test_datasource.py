"""Datasource-mutation tool tests.

``set_datasource_connection`` repoints a ``<DataSource>`` at a Power BI XMLA
endpoint. Power BI Report Builder uses ``DataProvider=SQL`` for Analysis
Services-backed connections (this is the AS provider's wire identifier in
RDL, despite the name) and the connection string is the canonical
``Data Source=powerbi://api.powerbi.com/v1.0/myorg/<workspace>;Initial Catalog=<dataset>``
form.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child
from pbirb_mcp.ops.datasource import (
    add_data_source,
    get_data_source,
    list_data_sources,
    remove_data_source,
    rename_data_source,
    set_datasource_connection,
)
from pbirb_mcp.ops.reader import describe_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
FIXTURE_MULTI = Path(__file__).parent / "fixtures" / "pbi_multi_datasource.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _conn_props(rdl_path: Path) -> dict:
    doc = RDLDocument.open(rdl_path)
    ds = doc.root.find(f".//{{{RDL_NS}}}DataSource")
    cp = find_child(ds, "ConnectionProperties")
    return {
        "data_provider": find_child(cp, "DataProvider").text,
        "connect_string": find_child(cp, "ConnectString").text,
        "integrated_security": (
            find_child(cp, "IntegratedSecurity").text
            if find_child(cp, "IntegratedSecurity") is not None
            else None
        ),
    }


class TestSetDatasourceConnection:
    def test_writes_canonical_xmla_connection_string(self, rdl_path):
        set_datasource_connection(
            path=str(rdl_path),
            name="PowerBIDataset",
            workspace_url="MyWorkspace",
            dataset_name="SalesAnalytics",
        )
        props = _conn_props(rdl_path)
        assert props["connect_string"] == (
            "Data Source=powerbi://api.powerbi.com/v1.0/myorg/MyWorkspace"
            ";Initial Catalog=SalesAnalytics"
        )

    def test_data_provider_is_SQL_for_xmla(self, rdl_path):
        # Report Builder writes "SQL" for AS-backed PBI XMLA connections.
        set_datasource_connection(
            path=str(rdl_path),
            name="PowerBIDataset",
            workspace_url="MyWorkspace",
            dataset_name="SalesAnalytics",
        )
        assert _conn_props(rdl_path)["data_provider"] == "SQL"

    def test_integrated_security_default_true(self, rdl_path):
        set_datasource_connection(
            path=str(rdl_path),
            name="PowerBIDataset",
            workspace_url="MyWorkspace",
            dataset_name="SalesAnalytics",
        )
        assert _conn_props(rdl_path)["integrated_security"] == "true"

    def test_integrated_security_can_be_disabled(self, rdl_path):
        set_datasource_connection(
            path=str(rdl_path),
            name="PowerBIDataset",
            workspace_url="MyWorkspace",
            dataset_name="SalesAnalytics",
            integrated_security=False,
        )
        # When false, the element is omitted (Report Builder's convention).
        doc = RDLDocument.open(rdl_path)
        cp = doc.root.find(f".//{{{RDL_NS}}}ConnectionProperties")
        assert find_child(cp, "IntegratedSecurity") is None

    def test_full_workspace_url_accepted(self, rdl_path):
        # Caller may pass the full XMLA URL; tool should not double-prefix.
        full = "powerbi://api.powerbi.com/v1.0/myorg/MyWorkspace"
        set_datasource_connection(
            path=str(rdl_path),
            name="PowerBIDataset",
            workspace_url=full,
            dataset_name="SalesAnalytics",
        )
        assert _conn_props(rdl_path)["connect_string"] == (
            f"Data Source={full};Initial Catalog=SalesAnalytics"
        )

    def test_unknown_data_source_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_datasource_connection(
                path=str(rdl_path),
                name="NoSuchDataSource",
                workspace_url="MyWorkspace",
                dataset_name="SalesAnalytics",
            )

    def test_round_trip_safe(self, rdl_path):
        set_datasource_connection(
            path=str(rdl_path),
            name="PowerBIDataset",
            workspace_url="MyWorkspace",
            dataset_name="SalesAnalytics",
        )
        # Reopens and validates structurally.
        doc = RDLDocument.open(rdl_path)
        doc.validate()

    def test_describe_report_still_lists_data_source_after_edit(self, rdl_path):
        set_datasource_connection(
            path=str(rdl_path),
            name="PowerBIDataset",
            workspace_url="MyWorkspace",
            dataset_name="SalesAnalytics",
        )
        out = describe_report(path=str(rdl_path))
        assert out["data_sources"] == ["PowerBIDataset"]


# ---- v0.3 datasource CRUD --------------------------------------------------


@pytest.fixture
def multi_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_MULTI, dest)
    return dest


class TestListDataSources:
    def test_returns_rich_shape(self, multi_path):
        result = list_data_sources(path=str(multi_path))
        names = [r["name"] for r in result]
        assert sorted(names) == sorted(["PowerBIDataset", "LookupDataset", "SharedRef"])
        # PowerBIDataset has full connection properties.
        pbi = next(r for r in result if r["name"] == "PowerBIDataset")
        assert pbi["data_provider"] == "SQL"
        assert "powerbi://" in pbi["connect_string"]
        assert pbi["integrated_security"] is True
        assert pbi["data_source_id"] is not None
        # SharedRef uses DataSourceReference instead of ConnectionProperties.
        shared = next(r for r in result if r["name"] == "SharedRef")
        assert shared["shared_reference"] == "PowerBIDataset"
        assert shared["connect_string"] is None

    def test_empty_when_no_data_sources(self, tmp_path):
        # Build a minimal report with no DataSources block.

        # Easier: copy fixture and remove the DataSources block.
        dst = tmp_path / "no_ds.rdl"
        shutil.copy(FIXTURE, dst)
        doc = RDLDocument.open(dst)
        ds_root = find_child(doc.root, "DataSources")
        if ds_root is not None:
            doc.root.remove(ds_root)
        doc.save()
        # validate may fail (DataSources is required); skip validate
        # and just call the reader.
        assert list_data_sources(path=str(dst)) == []


class TestGetDataSource:
    def test_returns_single_data_source(self, multi_path):
        result = get_data_source(path=str(multi_path), name="LookupDataset")
        assert result["name"] == "LookupDataset"
        assert result["data_provider"] == "SQL"
        assert "LookupWS" in result["connect_string"]

    def test_unknown_raises(self, multi_path):
        with pytest.raises(ElementNotFoundError):
            get_data_source(path=str(multi_path), name="NoSuch")


class TestAddDataSource:
    def test_appends_new_data_source(self, rdl_path):
        result = add_data_source(
            path=str(rdl_path),
            name="NewSource",
            workspace_url="NewWorkspace",
            dataset_name="NewCatalog",
        )
        assert result["name"] == "NewSource"
        assert result["kind"] == "DataSource"
        assert result["data_provider"] == "SQL"
        assert "NewWorkspace" in result["connect_string"]
        assert result["integrated_security"] is True
        # Verify it round-trips on disk.
        names = [d["name"] for d in list_data_sources(path=str(rdl_path))]
        assert "NewSource" in names

    def test_generates_unique_data_source_id(self, rdl_path):
        add_data_source(
            path=str(rdl_path),
            name="A",
            workspace_url="W",
            dataset_name="C",
        )
        add_data_source(
            path=str(rdl_path),
            name="B",
            workspace_url="W",
            dataset_name="C",
        )
        a = get_data_source(path=str(rdl_path), name="A")
        b = get_data_source(path=str(rdl_path), name="B")
        assert a["data_source_id"] != b["data_source_id"]
        assert a["data_source_id"] is not None
        assert b["data_source_id"] is not None

    def test_duplicate_name_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="already exists"):
            add_data_source(
                path=str(rdl_path),
                name="PowerBIDataset",  # already in fixture
                workspace_url="W",
                dataset_name="C",
            )

    def test_integrated_security_false(self, rdl_path):
        add_data_source(
            path=str(rdl_path),
            name="External",
            workspace_url="W",
            dataset_name="C",
            integrated_security=False,
        )
        result = get_data_source(path=str(rdl_path), name="External")
        # When False, IntegratedSecurity element is omitted.
        assert result["integrated_security"] is None
        # SecurityType reflects the choice.
        assert result["security_type"] == "None"

    def test_round_trip_safe(self, rdl_path):
        add_data_source(
            path=str(rdl_path),
            name="X",
            workspace_url="W",
            dataset_name="C",
        )
        RDLDocument.open(rdl_path).validate()


class TestRemoveDataSource:
    def test_refuses_when_referenced(self, multi_path):
        # PowerBIDataset is referenced by MainDataset's <DataSourceName>
        # AND by SharedRef's <DataSourceReference>.
        with pytest.raises(ValueError, match="still referenced"):
            remove_data_source(path=str(multi_path), name="PowerBIDataset")

    def test_force_removes_anyway(self, multi_path):
        result = remove_data_source(path=str(multi_path), name="PowerBIDataset", force=True)
        assert result["removed"] == "PowerBIDataset"
        assert result["force"] is True
        names = [d["name"] for d in list_data_sources(path=str(multi_path))]
        assert "PowerBIDataset" not in names

    def test_removes_unreferenced(self, multi_path):
        # SharedRef has no inbound references — safe to remove without force.
        result = remove_data_source(path=str(multi_path), name="SharedRef")
        assert result["removed"] == "SharedRef"
        names = [d["name"] for d in list_data_sources(path=str(multi_path))]
        assert "SharedRef" not in names

    def test_unknown_raises(self, multi_path):
        with pytest.raises(ElementNotFoundError):
            remove_data_source(path=str(multi_path), name="NoSuch")


class TestRenameDataSource:
    def test_rewrites_data_source_name_in_query(self, multi_path):
        result = rename_data_source(
            path=str(multi_path),
            old_name="LookupDataset",
            new_name="LookupRenamed",
        )
        assert result["new_name"] == "LookupRenamed"
        # 1 reference: LookupSet's Query/DataSourceName.
        assert result["references_rewritten"] == 1
        # Verify the rewrite landed.
        doc = RDLDocument.open(multi_path)
        lookup_set = next(
            ds for ds in doc.root.iter(f"{{{RDL_NS}}}DataSet") if ds.get("Name") == "LookupSet"
        )
        ref = lookup_set.find(f"{{{RDL_NS}}}Query/{{{RDL_NS}}}DataSourceName")
        assert ref.text == "LookupRenamed"

    def test_rewrites_data_source_reference_in_shared_link(self, multi_path):
        # PowerBIDataset is referenced by SharedRef's DataSourceReference.
        # Renaming must rewrite that link too.
        result = rename_data_source(
            path=str(multi_path),
            old_name="PowerBIDataset",
            new_name="PowerBIRenamed",
        )
        # 2 references: MainDataset/Query/DataSourceName +
        # SharedRef/DataSourceReference.
        assert result["references_rewritten"] == 2
        # Verify both rewrites landed.
        doc = RDLDocument.open(multi_path)
        main_ds = next(
            ds for ds in doc.root.iter(f"{{{RDL_NS}}}DataSet") if ds.get("Name") == "MainDataset"
        )
        ref1 = main_ds.find(f"{{{RDL_NS}}}Query/{{{RDL_NS}}}DataSourceName")
        assert ref1.text == "PowerBIRenamed"
        shared = next(
            ds for ds in doc.root.iter(f"{{{RDL_NS}}}DataSource") if ds.get("Name") == "SharedRef"
        )
        ref2 = find_child(shared, "DataSourceReference")
        assert ref2.text == "PowerBIRenamed"

    def test_renames_declaration(self, multi_path):
        rename_data_source(
            path=str(multi_path),
            old_name="LookupDataset",
            new_name="LookupRenamed",
        )
        names = [d["name"] for d in list_data_sources(path=str(multi_path))]
        assert "LookupRenamed" in names
        assert "LookupDataset" not in names

    def test_rejects_collision(self, multi_path):
        with pytest.raises(ValueError, match="already exists"):
            rename_data_source(
                path=str(multi_path),
                old_name="LookupDataset",
                new_name="PowerBIDataset",
            )

    def test_rejects_identity(self, multi_path):
        with pytest.raises(ValueError, match="identical"):
            rename_data_source(
                path=str(multi_path),
                old_name="LookupDataset",
                new_name="LookupDataset",
            )

    def test_unknown_old_raises(self, multi_path):
        with pytest.raises(ElementNotFoundError):
            rename_data_source(
                path=str(multi_path),
                old_name="NoSuch",
                new_name="X",
            )


class TestToolRegistration:
    def test_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "set_datasource_connection" in names

    def test_v03_crud_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {
            "list_data_sources",
            "get_data_source",
            "add_data_source",
            "remove_data_source",
            "rename_data_source",
        } <= names

    def test_input_schema_required_fields(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        tool = next(t for t in listing if t["name"] == "set_datasource_connection")
        assert set(tool["inputSchema"]["required"]) == {
            "path",
            "name",
            "workspace_url",
            "dataset_name",
        }
