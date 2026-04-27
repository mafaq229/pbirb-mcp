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
from pbirb_mcp.ops.datasource import set_datasource_connection
from pbirb_mcp.ops.reader import describe_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


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


class TestToolRegistration:
    def test_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "set_datasource_connection" in names

    def test_input_schema_required_fields(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        tool = next(t for t in listing if t["name"] == "set_datasource_connection")
        assert set(tool["inputSchema"]["required"]) == {
            "path",
            "name",
            "workspace_url",
            "dataset_name",
        }
