"""Dataset-mutation tool tests.

Covers:
* ``update_dataset_query`` — replace the DAX command text.
* ``add_query_parameter`` / ``update_query_parameter`` / ``remove_query_parameter``
  — manage the ``<Query><QueryParameters>`` block.

Mutations always go through ``RDLDocument.save_as`` so the round-trip rules
(self-closing tag style, declaration quoting, atomicity) still apply. After
each tool call we reopen the file from disk to confirm the change persisted.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops.dataset import (
    add_query_parameter,
    remove_query_parameter,
    update_dataset_query,
    update_query_parameter,
)
from pbirb_mcp.ops.reader import get_datasets
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


# ---- update_dataset_query --------------------------------------------------


class TestUpdateDatasetQuery:
    def test_replaces_command_text(self, rdl_path):
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE TOPN(10, 'Sales')",
        )
        ds = get_datasets(path=str(rdl_path))[0]
        assert ds["command_text"] == "EVALUATE TOPN(10, 'Sales')"

    def test_returns_summary(self, rdl_path):
        result = update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE 'Customer'",
        )
        assert result["dataset"] == "MainDataset"
        assert result["command_text"] == "EVALUATE 'Customer'"

    def test_unknown_dataset_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            update_dataset_query(
                path=str(rdl_path),
                dataset_name="NoSuchDataset",
                dax_body="EVALUATE 'Sales'",
            )

    def test_empty_dax_body_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            update_dataset_query(
                path=str(rdl_path),
                dataset_name="MainDataset",
                dax_body="",
            )

    def test_whitespace_only_dax_body_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            update_dataset_query(
                path=str(rdl_path),
                dataset_name="MainDataset",
                dax_body="   \n\t  ",
            )

    def test_command_type_not_added_for_dax(self, rdl_path):
        # Per CLAUDE.md: PBI paginated reports don't use <CommandType> for DAX.
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE TOPN(5, 'Sales')",
        )
        doc = RDLDocument.open(rdl_path)
        ct = doc.root.find(f".//{{{RDL_NS}}}DataSet/{{{RDL_NS}}}Query/{{{RDL_NS}}}CommandType")
        assert ct is None

    def test_save_is_round_trip_safe(self, rdl_path):
        # The file must reopen cleanly and structural validate must still pass.
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE TOPN(5, 'Sales')",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()  # no raise


# ---- add_query_parameter ---------------------------------------------------


class TestAddQueryParameter:
    def test_creates_query_parameters_block_when_absent(self, rdl_path):
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        ds = get_datasets(path=str(rdl_path))[0]
        assert ds["query_parameters"] == [
            {"name": "DateFrom", "value": "=Parameters!DateFrom.Value"}
        ]

    def test_appends_to_existing_block(self, rdl_path):
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateTo",
            value_expression="=Parameters!DateTo.Value",
        )
        names = [p["name"] for p in get_datasets(path=str(rdl_path))[0]["query_parameters"]]
        assert names == ["DateFrom", "DateTo"]

    def test_duplicate_name_rejected(self, rdl_path):
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        with pytest.raises(ValueError):
            add_query_parameter(
                path=str(rdl_path),
                dataset_name="MainDataset",
                name="DateFrom",
                value_expression="=Parameters!DateFrom.Value",
            )

    def test_unknown_dataset_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_query_parameter(
                path=str(rdl_path),
                dataset_name="Nope",
                name="X",
                value_expression="=1",
            )


# ---- update_query_parameter ------------------------------------------------


class TestUpdateQueryParameter:
    def test_changes_value_expression(self, rdl_path):
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        update_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
            value_expression="=Today()",
        )
        ds = get_datasets(path=str(rdl_path))[0]
        assert ds["query_parameters"][0]["value"] == "=Today()"

    def test_unknown_query_param_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            update_query_parameter(
                path=str(rdl_path),
                dataset_name="MainDataset",
                name="Ghost",
                value_expression="=1",
            )


# ---- remove_query_parameter ------------------------------------------------


class TestRemoveQueryParameter:
    def test_removes_named_parameter(self, rdl_path):
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateTo",
            value_expression="=Parameters!DateTo.Value",
        )
        remove_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
        )
        names = [p["name"] for p in get_datasets(path=str(rdl_path))[0]["query_parameters"]]
        assert names == ["DateTo"]

    def test_removes_empty_block_when_last_param_removed(self, rdl_path):
        # A leftover empty <QueryParameters/> can confuse Report Builder; clean it up.
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        remove_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
        )
        doc = RDLDocument.open(rdl_path)
        qp_root = doc.root.find(
            f".//{{{RDL_NS}}}DataSet/{{{RDL_NS}}}Query/{{{RDL_NS}}}QueryParameters"
        )
        assert qp_root is None

    def test_unknown_query_param_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_query_parameter(
                path=str(rdl_path),
                dataset_name="MainDataset",
                name="NeverExisted",
            )


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_dataset_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {
            "update_dataset_query",
            "add_query_parameter",
            "update_query_parameter",
            "remove_query_parameter",
        } <= names

    def test_update_dataset_query_input_schema(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        tool = next(t for t in listing if t["name"] == "update_dataset_query")
        assert set(tool["inputSchema"]["required"]) == {"path", "dataset_name", "dax_body"}
