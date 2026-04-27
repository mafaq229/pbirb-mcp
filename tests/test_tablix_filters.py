"""Tablix-level filter tool tests.

Filters are anonymous in RDL, so tools index them by their ordinal position
within the tablix's ``<Filters>`` block. add appends, so existing indices
stay valid across adds; only remove can shift them. Callers should
``list_tablix_filters`` between mutating calls if they're chaining edits.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child
from pbirb_mcp.ops.tablix import (
    add_tablix_filter,
    list_tablix_filters,
    remove_tablix_filter,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


# ---- list_tablix_filters --------------------------------------------------


class TestListTablixFilters:
    def test_returns_empty_when_no_filters_block(self, rdl_path):
        assert list_tablix_filters(path=str(rdl_path), tablix_name="MainTable") == []

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            list_tablix_filters(path=str(rdl_path), tablix_name="NoSuchTable")


# ---- add_tablix_filter ----------------------------------------------------


class TestAddTablixFilter:
    def test_creates_filters_block_when_absent(self, rdl_path):
        result = add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!Amount.Value",
            operator="GreaterThan",
            values=["1000"],
        )
        assert result["index"] == 0
        filters = list_tablix_filters(path=str(rdl_path), tablix_name="MainTable")
        assert len(filters) == 1
        assert filters[0]["expression"] == "=Fields!Amount.Value"
        assert filters[0]["operator"] == "GreaterThan"
        assert filters[0]["values"] == ["1000"]

    def test_appends_to_existing_block_with_stable_indices(self, rdl_path):
        add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!Amount.Value",
            operator="GreaterThan",
            values=["1000"],
        )
        result = add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["Widget"],
        )
        assert result["index"] == 1
        filters = list_tablix_filters(path=str(rdl_path), tablix_name="MainTable")
        assert [f["expression"] for f in filters] == [
            "=Fields!Amount.Value",
            "=Fields!ProductName.Value",
        ]

    def test_in_operator_writes_multiple_filter_values(self, rdl_path):
        add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!ProductName.Value",
            operator="In",
            values=["Widget", "Gadget", "Gizmo"],
        )
        filters = list_tablix_filters(path=str(rdl_path), tablix_name="MainTable")
        assert filters[0]["values"] == ["Widget", "Gadget", "Gizmo"]

    def test_unknown_operator_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            add_tablix_filter(
                path=str(rdl_path),
                tablix_name="MainTable",
                expression="=Fields!Amount.Value",
                operator="GtNotARealOp",
                values=["1000"],
            )

    def test_empty_values_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            add_tablix_filter(
                path=str(rdl_path),
                tablix_name="MainTable",
                expression="=Fields!Amount.Value",
                operator="Equal",
                values=[],
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_tablix_filter(
                path=str(rdl_path),
                tablix_name="NoSuchTable",
                expression="=Fields!Amount.Value",
                operator="Equal",
                values=["1"],
            )

    def test_round_trip_safe(self, rdl_path):
        add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!Amount.Value",
            operator="GreaterThan",
            values=["1000"],
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()  # structural validate still passes


# ---- remove_tablix_filter -------------------------------------------------


class TestRemoveTablixFilter:
    def test_removes_filter_by_index(self, rdl_path):
        add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!Amount.Value",
            operator="GreaterThan",
            values=["1000"],
        )
        add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["Widget"],
        )
        remove_tablix_filter(path=str(rdl_path), tablix_name="MainTable", filter_index=0)
        remaining = list_tablix_filters(path=str(rdl_path), tablix_name="MainTable")
        assert [f["expression"] for f in remaining] == ["=Fields!ProductName.Value"]

    def test_removes_block_when_last_filter_removed(self, rdl_path):
        add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!Amount.Value",
            operator="GreaterThan",
            values=["1000"],
        )
        remove_tablix_filter(path=str(rdl_path), tablix_name="MainTable", filter_index=0)
        doc = RDLDocument.open(rdl_path)
        tablix = doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
        assert find_child(tablix, "Filters") is None

    def test_invalid_filter_index_raises(self, rdl_path):
        with pytest.raises(IndexError):
            remove_tablix_filter(
                path=str(rdl_path), tablix_name="MainTable", filter_index=0
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_tablix_filter(
                path=str(rdl_path), tablix_name="NoSuch", filter_index=0
            )


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_three_filter_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {
            "add_tablix_filter",
            "list_tablix_filters",
            "remove_tablix_filter",
        } <= names
