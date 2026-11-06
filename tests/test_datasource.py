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
from pbirb_mcp.ops.validate import validate_report
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

    def test_provider_sql_is_default_back_compat(self, rdl_path):
        """The default provider must stay 'sql' so existing callers see
        no byte-level surprise — same DataProvider, same ConnectString
        shape, same <rd:SecurityType> as before v0.4.
        """
        add_data_source(
            path=str(rdl_path),
            name="LegacyDefault",
            workspace_url="LegacyWS",
            dataset_name="LegacyDS",
        )
        result = get_data_source(path=str(rdl_path), name="LegacyDefault")
        assert result["data_provider"] == "SQL"
        assert "powerbi://api.powerbi.com" in result["connect_string"]
        assert "LegacyWS" in result["connect_string"]
        # SQL shape keeps the legacy <rd:SecurityType> element.
        assert result["security_type"] == "Integrated"


class TestAddDataSourcePbidataset:
    """`provider="pbidataset"` emits the Report Builder shape:

    * ``<DataProvider>PBIDATASET</DataProvider>``
    * ``<ConnectString>Data Source=pbiazure://api.powerbi.com/;Initial
      Catalog=<catalog>;Integrated Security=ClaimsToken``
    * Adds ``<rd:PowerBIWorkspaceName>`` and ``<rd:PowerBIDatasetName>``
      siblings for human-readable display in Report Builder.
    * ``<rd:SecurityType>None</rd:SecurityType>`` (#71).

    Recognised round-trip-safe by ``_is_pbidataset_dataset`` so the
    PBIDATASET ``@``-prefix defence kicks in on datasets bound to this
    source.
    """

    def test_emits_modern_pbidataset_shape(self, rdl_path):
        result = add_data_source(
            path=str(rdl_path),
            name="ModernSource",
            workspace_url="ADNOC",
            dataset_name="RAG Report",
            provider="pbidataset",
        )
        assert result["data_provider"] == "PBIDATASET"
        assert "pbiazure://api.powerbi.com" in result["connect_string"]
        assert "Initial Catalog=RAG Report" in result["connect_string"]
        assert "Integrated Security=ClaimsToken" in result["connect_string"]

    def test_emits_human_readable_workspace_and_dataset_siblings(self, rdl_path):
        from pbirb_mcp.core.xpath import RD_NS

        add_data_source(
            path=str(rdl_path),
            name="ModernSource",
            workspace_url="ADNOC",
            dataset_name="RAG Report",
            provider="pbidataset",
        )
        doc = RDLDocument.open(rdl_path)
        ds = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSource") if d.get("Name") == "ModernSource"
        )
        assert ds.find(f"{{{RD_NS}}}PowerBIWorkspaceName").text == "ADNOC"
        assert ds.find(f"{{{RD_NS}}}PowerBIDatasetName").text == "RAG Report"

    def test_pbidataset_shape_recognised_by_is_pbidataset_helper(self, rdl_path):
        """The ``@``-prefix defence depends on `_is_pbidataset_dataset`
        recognising the new shape. Verify it does by adding a query
        parameter with a leading '@' and asserting it gets stripped.
        """
        from pbirb_mcp.ops.dataset import _is_pbidataset_dataset

        add_data_source(
            path=str(rdl_path),
            name="ModernSource",
            workspace_url="ADNOC",
            dataset_name="RAG Report",
            provider="pbidataset",
        )
        doc = RDLDocument.open(rdl_path)
        # Build a synthetic dataset bound to ModernSource and check the
        # helper directly — cheaper than driving the full DAX path.
        from lxml import etree

        from pbirb_mcp.core.xpath import q

        ds_root = doc.root.find(f"{{{RDL_NS}}}DataSets")
        synthetic = etree.SubElement(ds_root, q("DataSet"), Name="Probe")
        query = etree.SubElement(synthetic, q("Query"))
        etree.SubElement(query, q("DataSourceName")).text = "ModernSource"
        assert _is_pbidataset_dataset(doc, synthetic) is True

    def test_pbidataset_writes_security_type_none(self, rdl_path):
        """Every real Report Builder PBIDATASET source carries
        ``<rd:SecurityType>None</rd:SecurityType>`` (#71) — auth itself is
        ``Integrated Security=ClaimsToken`` inside the ConnectString.
        """
        from pbirb_mcp.core.xpath import RD_NS

        add_data_source(
            path=str(rdl_path),
            name="ModernSource",
            workspace_url="ADNOC",
            dataset_name="RAG Report",
            provider="pbidataset",
        )
        doc = RDLDocument.open(rdl_path)
        ds = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSource") if d.get("Name") == "ModernSource"
        )
        assert ds.find(f"{{{RD_NS}}}SecurityType").text == "None"

    def test_unknown_provider_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="unknown provider"):
            add_data_source(
                path=str(rdl_path),
                name="X",
                workspace_url="W",
                dataset_name="C",
                provider="not-a-provider",
            )

    def test_round_trip_safe(self, rdl_path):
        add_data_source(
            path=str(rdl_path),
            name="ModernSource",
            workspace_url="ADNOC",
            dataset_name="RAG Report",
            provider="pbidataset",
        )
        RDLDocument.open(rdl_path).validate()


class TestRemoveDataSource:
    def test_refuses_when_referenced(self, multi_path):
        # PowerBIDataset is referenced by MainDataset's <DataSourceName>.
        with pytest.raises(ValueError, match="still referenced") as exc:
            remove_data_source(path=str(multi_path), name="PowerBIDataset")
        # SharedRef's <DataSourceReference> is a server path, not a
        # reference to the embedded name (#92).
        assert "DataSourceReference" not in str(exc.value)
        assert "1 location(s)" in str(exc.value)

    def test_shared_reference_path_does_not_block_removal(self, multi_path):
        # #92: SharedRef's <DataSourceReference>LookupDataset</...> is a
        # shared-source server path that merely happens to equal an
        # embedded name — it must not count as a reference.
        doc = RDLDocument.open(multi_path)
        shared = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSource") if d.get("Name") == "SharedRef"
        )
        find_child(shared, "DataSourceReference").text = "LookupDataset"
        lookup_set = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == "LookupSet"
        )
        find_child(find_child(lookup_set, "Query"), "DataSourceName").text = "PowerBIDataset"
        doc.save()
        result = remove_data_source(path=str(multi_path), name="LookupDataset")
        assert result["removed"] == "LookupDataset"
        assert get_data_source(str(multi_path), "SharedRef")["shared_reference"] == "LookupDataset"

    def test_refuses_last_data_source_even_with_force(self, rdl_path):
        # #92: removing the only source used to delete <DataSources>,
        # leaving a structurally invalid report.
        before = rdl_path.read_bytes()
        for force in (False, True):
            with pytest.raises(ValueError, match="only data source"):
                remove_data_source(path=str(rdl_path), name="PowerBIDataset", force=force)
        assert rdl_path.read_bytes() == before
        assert validate_report(str(rdl_path))["valid"]

    def test_removing_down_to_one_keeps_report_valid(self, multi_path):
        remove_data_source(path=str(multi_path), name="SharedRef")
        remove_data_source(path=str(multi_path), name="LookupDataset", force=True)
        assert [d["name"] for d in list_data_sources(str(multi_path))] == ["PowerBIDataset"]
        result = validate_report(str(multi_path))
        assert not [e for e in result["errors"] if e["rule"] in ("structural", "xsd")]
        with pytest.raises(ValueError, match="only data source"):
            remove_data_source(path=str(multi_path), name="PowerBIDataset", force=True)

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

    def test_leaves_shared_reference_path_alone(self, multi_path):
        # #92: SharedRef's <DataSourceReference>PowerBIDataset</...> is a
        # shared data source's server path, not a link to the embedded
        # PowerBIDataset — renaming must not rewrite it.
        result = rename_data_source(
            path=str(multi_path),
            old_name="PowerBIDataset",
            new_name="Sales",
        )
        # 1 reference: MainDataset/Query/DataSourceName only.
        assert result["references_rewritten"] == 1
        doc = RDLDocument.open(multi_path)
        main_ds = next(
            ds for ds in doc.root.iter(f"{{{RDL_NS}}}DataSet") if ds.get("Name") == "MainDataset"
        )
        ref1 = main_ds.find(f"{{{RDL_NS}}}Query/{{{RDL_NS}}}DataSourceName")
        assert ref1.text == "Sales"
        assert get_data_source(str(multi_path), "SharedRef")["shared_reference"] == "PowerBIDataset"

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


IDP = (
    "https://login.microsoftonline.com/organizations, "
    "https://analysis.windows.net/powerbi/api, 929d0ec0-7a41-4b1e-bc7c-b754a28bddcc"
)
RB_SOURCE = (
    '<DataSource Name="Model">'
    "<ConnectionProperties><DataProvider>PBIDATASET</DataProvider>"
    "<ConnectString>Data Source=pbiazure://api.powerbi.com/;"
    f'Identity Provider="{IDP}";'
    "Initial Catalog=sobe_wowvirtualserver-0f1e2d3c;Integrated Security=ClaimsToken"
    "</ConnectString></ConnectionProperties>"
    "<rd:SecurityType>None</rd:SecurityType>"
    "<rd:DataSourceID>44444444-4444-4444-4444-444444444444</rd:DataSourceID>"
    "<rd:PowerBIWorkspaceName>Sales WS</rd:PowerBIWorkspaceName>"
    "<rd:PowerBIDatasetName>Sales Model</rd:PowerBIDatasetName></DataSource>"
)


@pytest.fixture
def rb_path(tmp_path: Path) -> Path:
    """The minimal fixture plus a Report Builder–authored PBIDATASET source."""
    dest = tmp_path / "rb.rdl"
    text = FIXTURE.read_text(encoding="utf-8")
    text = text.replace("</DataSources>", RB_SOURCE + "</DataSources>", 1)
    dest.write_text(text, encoding="utf-8")
    return dest


def _ds_xml(path: Path, name: str) -> str:
    """The DataSource's canonical XML, minus its (random) DataSourceID."""
    from lxml import etree

    from pbirb_mcp.core.xpath import RD_NS

    doc = RDLDocument.open(path)
    ds = next(d for d in doc.root.iter(f"{{{RDL_NS}}}DataSource") if d.get("Name") == name)
    ds = etree.fromstring(etree.tostring(ds))
    ds.find(f"{{{RD_NS}}}DataSourceID").text = "-"
    for el in ds.iter():
        el.tail = None
        if el.text is not None and not el.text.strip():
            el.text = None
    return etree.tostring(ds, encoding="unicode")


class TestPbidatasetConnection:
    """#71: the real PBIDATASET shape is preserved and emitted; connect
    strings are escaped."""

    def test_set_keeps_pbidataset_provider_and_updates_labels(self, rb_path):
        result = set_datasource_connection(str(rb_path), "Model", "Sales WS", "Sales Model v2")
        ds = get_data_source(str(rb_path), "Model")
        assert ds["data_provider"] == "PBIDATASET"
        assert ds["connect_string"].startswith("Data Source=pbiazure://api.powerbi.com/;")
        assert "Integrated Security=ClaimsToken" in ds["connect_string"]
        assert ds["integrated_security"] is None  # no <IntegratedSecurity> element
        assert ds["security_type"] == "None"
        assert ds["powerbi_dataset_name"] == "Sales Model v2"
        assert ds["powerbi_workspace_name"] == "Sales WS"
        # The Identity Provider key survives the rewrite, still quoted.
        assert f'Identity Provider="{IDP}"' in ds["connect_string"]
        assert result["before"]["powerbi_dataset_name"] == "Sales Model"
        assert result["after"] == ds
        # No catalog_id: the display name goes in Initial Catalog, with a warning.
        assert "Initial Catalog=Sales Model v2;" in ds["connect_string"]
        assert result["warnings"] and "catalog_id" in result["warnings"][0]

    def test_set_with_catalog_id(self, rb_path):
        result = set_datasource_connection(
            str(rb_path), "Model", "Other WS", "Sales Model", catalog_id="sobe_wowvirtualserver-99"
        )
        ds = get_data_source(str(rb_path), "Model")
        assert "Initial Catalog=sobe_wowvirtualserver-99;" in ds["connect_string"]
        assert ds["powerbi_workspace_name"] == "Other WS"
        assert result["warnings"] == []

    def test_set_same_model_keeps_internal_id_and_bytes(self, rb_path):
        before = rb_path.read_bytes()
        result = set_datasource_connection(
            str(rb_path), "Model", "powerbi://api.powerbi.com/v1.0/myorg/Sales WS", "Sales Model"
        )
        assert result["changed"] is False
        assert result["warnings"] == []
        assert rb_path.read_bytes() == before

    def test_workspace_url_prefix_stripped_from_label(self, rdl_path):
        add_data_source(
            str(rdl_path),
            "Pbi",
            "powerbi://api.powerbi.com/v1.0/myorg/Sales WS",
            "Sales Model",
            provider="pbidataset",
        )
        assert get_data_source(str(rdl_path), "Pbi")["powerbi_workspace_name"] == "Sales WS"

    def test_add_pbidataset_with_ids_matches_report_builder_shape(self, rdl_path, rb_path):
        result = add_data_source(
            str(rdl_path),
            "Model",
            "Sales WS",
            "Sales Model",
            provider="pbidataset",
            catalog_id="sobe_wowvirtualserver-0f1e2d3c",
            identity_provider=IDP,
        )
        assert result["warnings"] == []
        assert _ds_xml(rdl_path, "Model") == _ds_xml(rb_path, "Model")

    def test_add_then_set_gives_the_add_shape(self, rdl_path, tmp_path):
        other = tmp_path / "other.rdl"
        shutil.copy(FIXTURE, other)
        kwargs = {"provider": "pbidataset", "catalog_id": "sobe-1", "identity_provider": IDP}
        add_data_source(str(rdl_path), "Src", "Sales WS", "Sales Model", **kwargs)
        add_data_source(str(other), "Src", "Old WS", "Old Model", provider="pbidataset")
        set_datasource_connection(
            str(other), "Src", "Sales WS", "Sales Model", catalog_id="sobe-1", identity_provider=IDP
        )
        assert _ds_xml(other, "Src") == _ds_xml(rdl_path, "Src")

    def test_explicit_provider_converts_sql_to_pbidataset(self, rdl_path):
        set_datasource_connection(
            str(rdl_path), "PowerBIDataset", "WS", "Model", provider="pbidataset"
        )
        ds = get_data_source(str(rdl_path), "PowerBIDataset")
        assert ds["data_provider"] == "PBIDATASET"
        assert ds["integrated_security"] is None
        assert ds["security_type"] == "None"
        assert ds["data_source_id"] == "11111111-1111-1111-1111-111111111111"
        assert ds["connect_string"] == (
            "Data Source=pbiazure://api.powerbi.com/;Initial Catalog=Model;"
            "Integrated Security=ClaimsToken"
        )

    def test_explicit_provider_converts_pbidataset_to_sql(self, rb_path):
        set_datasource_connection(str(rb_path), "Model", "WS", "Model", provider="sql")
        ds = get_data_source(str(rb_path), "Model")
        assert ds["data_provider"] == "SQL"
        assert ds["connect_string"] == (
            "Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;Initial Catalog=Model"
        )
        assert ds["integrated_security"] is True
        assert ds["security_type"] == "Integrated"
        assert ds["powerbi_workspace_name"] is None
        assert ds["powerbi_dataset_name"] is None

    def test_other_provider_refused_without_explicit_provider(self, rdl_path):
        text = rdl_path.read_text(encoding="utf-8").replace(
            "<DataProvider>SQL</DataProvider>", "<DataProvider>OLEDB</DataProvider>", 1
        )
        rdl_path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="OLEDB"):
            set_datasource_connection(str(rdl_path), "PowerBIDataset", "WS", "Model")

    def test_unknown_provider_argument_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="unknown provider"):
            set_datasource_connection(str(rdl_path), "PowerBIDataset", "WS", "M", provider="x")

    @pytest.mark.parametrize("provider", ["sql", "pbidataset"])
    def test_injected_keys_are_quoted(self, rdl_path, provider):
        from pbirb_mcp.core.connection import get_value, parse_connect_string

        evil = 'm;Integrated Security=SSPI;Data Source="elsewhere"'
        add_data_source(str(rdl_path), "Inj", "ws", evil, provider=provider)
        cs = get_data_source(str(rdl_path), "Inj")["connect_string"]
        pairs = parse_connect_string(cs)
        assert [k for k, _v in pairs].count("Data Source") == 1
        assert get_value(pairs, "Initial Catalog") == evil
        set_datasource_connection(str(rdl_path), "Inj", "ws;x=y", evil, catalog_id=evil)
        pairs = parse_connect_string(get_data_source(str(rdl_path), "Inj")["connect_string"])
        assert [k for k, _v in pairs].count("Data Source") == 1
        assert get_value(pairs, "Initial Catalog") == evil

    def test_output_is_xsd_valid(self, rb_path):
        from pbirb_mcp.ops.validate import validate_report

        set_datasource_connection(str(rb_path), "Model", "WS 2", "Model 2", catalog_id="id-2")
        add_data_source(str(rb_path), "New", "WS", "M", provider="pbidataset")
        assert validate_report(str(rb_path))["valid"]


class TestConnectStringSyntax:
    def test_round_trip_with_quotes(self):
        from pbirb_mcp.core.connection import format_connect_string, parse_connect_string

        pairs = [("Data Source", "a"), ("Initial Catalog", 'x;"y"=z'), ("Identity Provider", IDP)]
        text = format_connect_string(pairs)
        assert 'Initial Catalog="x;""y""=z"' in text
        assert parse_connect_string(text) == pairs

    def test_parses_single_quotes_and_whitespace(self):
        from pbirb_mcp.core.connection import parse_connect_string

        assert parse_connect_string(" Data Source = a ; Initial Catalog='b;c''d' ;;") == [
            ("Data Source", "a"),
            ("Initial Catalog", "b;c'd"),
        ]

    def test_merge_is_verbatim_when_nothing_changes(self):
        from pbirb_mcp.core.connection import merge_connect_string

        text = "Data Source = a;Initial Catalog=b"
        assert merge_connect_string(text, [("Initial Catalog", "b")]) == text
        assert merge_connect_string(text, [("Identity Provider", "p")]) == (
            "Data Source=a;Identity Provider=p;Initial Catalog=b"
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
