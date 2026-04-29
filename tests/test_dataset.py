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

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, find_child
from pbirb_mcp.ops.dataset import (
    add_calculated_field,
    add_dataset_filter,
    add_query_parameter,
    get_dataset,
    list_dataset_filters,
    remove_calculated_field,
    remove_dataset_filter,
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


def _inject_designer_state(rdl_path: Path, dataset_name: str, statement_text: str):
    """Add a <rd:DesignerState>/<Statement> block to the named DataSet's
    <Query>, mimicking what Power BI Report Builder writes for PBIDATASET
    queries authored via the Query Designer GUI."""
    doc = RDLDocument.open(rdl_path)
    ds = next(
        d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == dataset_name
    )
    query = find_child(ds, "Query")
    designer_state = etree.SubElement(query, f"{{{RD_NS}}}DesignerState")
    statement = etree.SubElement(designer_state, f"{{{RD_NS}}}Statement")
    statement.text = statement_text
    doc.save()


class TestUpdateDatasetQueryDesignerStateSync:
    """update_dataset_query must rewrite <rd:DesignerState>/<Statement>
    in lockstep with <CommandText>. Without this, the Query Designer GUI
    in Report Builder shows stale DAX while the runtime executes the new
    DAX — invisibly confusing.
    """

    def test_designer_state_statement_synced(self, rdl_path):
        _inject_designer_state(
            rdl_path, "MainDataset", "OLD: EVALUATE 'OldTable'"
        )
        result = update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="NEW: EVALUATE 'NewTable'",
        )
        assert result["designer_state_synced"] is True

        doc = RDLDocument.open(rdl_path)
        ds = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == "MainDataset"
        )
        statement = ds.find(f"{{{RDL_NS}}}Query/{{{RD_NS}}}DesignerState/{{{RD_NS}}}Statement")
        assert statement is not None
        assert statement.text == "NEW: EVALUATE 'NewTable'"

    def test_no_designer_state_returns_false(self, rdl_path):
        # Fixture has no DesignerState — the sync flag is False.
        result = update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE 'Sales'",
        )
        assert result["designer_state_synced"] is False

    def test_designer_state_no_statement_returns_false(self, rdl_path):
        # Inject an empty DesignerState (no Statement child); sync stays
        # a no-op so we don't synthesise data we don't have.
        doc = RDLDocument.open(rdl_path)
        ds = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == "MainDataset"
        )
        query = find_child(ds, "Query")
        etree.SubElement(query, f"{{{RD_NS}}}DesignerState")
        doc.save()

        result = update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE 'Sales'",
        )
        assert result["designer_state_synced"] is False

    def test_no_op_dax_returns_false(self, rdl_path):
        _inject_designer_state(rdl_path, "MainDataset", "STATIC")
        # Same DAX value → sync is a no-op (text doesn't change).
        result = update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="STATIC",
        )
        assert result["designer_state_synced"] is False

    def test_sync_routes_through_encode_text(self, rdl_path):
        """The DesignerState rewrite must use encode_text — same idempotent
        encoding as CommandText. Otherwise a pre-encoded DAX double-encodes."""
        _inject_designer_state(rdl_path, "MainDataset", "old")
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body='EVALUATE FILTER(\'X\', \'X\'[c] = "A &amp; B")',
        )
        # Disk should not contain &amp;amp;
        assert b"&amp;amp;" not in rdl_path.read_bytes()


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


# ---- v0.3 dataset filters ------------------------------------------------


class TestAddDatasetFilter:
    def test_creates_filters_block(self, rdl_path):
        result = add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!ProductID.Value",
            operator="GreaterThan",
            values=["100"],
        )
        assert result["index"] == 0
        assert result["operator"] == "GreaterThan"
        # Verify on disk.
        filters = list_dataset_filters(path=str(rdl_path), dataset_name="MainDataset")
        assert len(filters) == 1
        assert filters[0]["expression"] == "=Fields!ProductID.Value"
        assert filters[0]["operator"] == "GreaterThan"
        assert filters[0]["values"] == ["100"]

    def test_appends_to_existing(self, rdl_path):
        add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!ProductID.Value",
            operator="GreaterThan",
            values=["100"],
        )
        result = add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!ProductName.Value",
            operator="Like",
            values=["A%"],
        )
        assert result["index"] == 1
        filters = list_dataset_filters(path=str(rdl_path), dataset_name="MainDataset")
        assert len(filters) == 2

    def test_invalid_operator_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="unknown filter operator"):
            add_dataset_filter(
                path=str(rdl_path),
                dataset_name="MainDataset",
                expression="=Fields!X.Value",
                operator="NotAnOperator",
                values=["x"],
            )

    def test_empty_values_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="at least one filter value"):
            add_dataset_filter(
                path=str(rdl_path),
                dataset_name="MainDataset",
                expression="=Fields!X.Value",
                operator="Equal",
                values=[],
            )

    def test_unknown_dataset_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_dataset_filter(
                path=str(rdl_path),
                dataset_name="NoSuchDataset",
                expression="=Fields!X.Value",
                operator="Equal",
                values=["x"],
            )

    def test_pre_encoded_value_no_double_encode(self, rdl_path):
        # Bug class regression: pre-encoded entity must end up as
        # &amp; on disk, not &amp;amp;.
        add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["A &amp; B"],
        )
        assert b"&amp;amp;" not in (rdl_path).read_bytes()

    def test_round_trip_safe(self, rdl_path):
        add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!ProductID.Value",
            operator="In",
            values=["1", "2", "3"],
        )
        RDLDocument.open(rdl_path).validate()


class TestRemoveDatasetFilter:
    def test_removes_by_index(self, rdl_path):
        for i in range(3):
            add_dataset_filter(
                path=str(rdl_path),
                dataset_name="MainDataset",
                expression=f"=Fields!F{i}.Value",
                operator="Equal",
                values=[str(i)],
            )
        remove_dataset_filter(
            path=str(rdl_path), dataset_name="MainDataset", filter_index=1
        )
        filters = list_dataset_filters(path=str(rdl_path), dataset_name="MainDataset")
        assert [f["expression"] for f in filters] == [
            "=Fields!F0.Value",
            "=Fields!F2.Value",
        ]

    def test_removes_empty_block_when_last_filter_removed(self, rdl_path):
        add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!X.Value",
            operator="Equal",
            values=["x"],
        )
        remove_dataset_filter(
            path=str(rdl_path), dataset_name="MainDataset", filter_index=0
        )
        # The <Filters> block itself should be gone.
        doc = RDLDocument.open(rdl_path)
        ds = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == "MainDataset"
        )
        from pbirb_mcp.core.xpath import find_child as _fc

        assert _fc(ds, "Filters") is None

    def test_out_of_range_raises(self, rdl_path):
        with pytest.raises(IndexError):
            remove_dataset_filter(
                path=str(rdl_path), dataset_name="MainDataset", filter_index=99
            )


class TestListDatasetFilters:
    def test_empty_when_no_filters(self, rdl_path):
        assert list_dataset_filters(path=str(rdl_path), dataset_name="MainDataset") == []

    def test_returns_in_document_order(self, rdl_path):
        add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!First.Value",
            operator="Equal",
            values=["1"],
        )
        add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!Second.Value",
            operator="Equal",
            values=["2"],
        )
        filters = list_dataset_filters(path=str(rdl_path), dataset_name="MainDataset")
        assert [f["expression"] for f in filters] == [
            "=Fields!First.Value",
            "=Fields!Second.Value",
        ]


# ---- v0.3 get_dataset ----------------------------------------------------


class TestGetDataset:
    def test_returns_full_shape(self, rdl_path):
        # Add a query parameter and a filter so the read-back has content.
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!ProductID.Value",
            operator="GreaterThan",
            values=["100"],
        )
        result = get_dataset(path=str(rdl_path), name="MainDataset")
        assert result["name"] == "MainDataset"
        assert result["data_source"] == "PowerBIDataset"
        assert result["command_text"] == "EVALUATE 'Sales'"
        assert len(result["fields"]) == 3  # ProductID, ProductName, Amount
        assert result["query_parameters"] == [
            {"name": "DateFrom", "value": "=Parameters!DateFrom.Value"}
        ]
        assert result["filters"][0]["operator"] == "GreaterThan"
        assert result["designer_state_present"] is False

    def test_field_shape_includes_value_for_calculated_fields(self, rdl_path):
        # Add a calculated field manually (commit 17 will introduce the
        # tool; for now, prove the reader surfaces <Value>).
        from lxml import etree as _etree

        from pbirb_mcp.core.xpath import find_child as _fc
        from pbirb_mcp.core.xpath import q as _q

        doc = RDLDocument.open(rdl_path)
        ds = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == "MainDataset"
        )
        fields_root = _fc(ds, "Fields")
        new_field = _etree.SubElement(fields_root, _q("Field"), Name="Total")
        _etree.SubElement(new_field, _q("Value")).text = (
            "=Fields!Amount.Value * Fields!ProductID.Value"
        )
        doc.save()

        result = get_dataset(path=str(rdl_path), name="MainDataset")
        total = next(f for f in result["fields"] if f["name"] == "Total")
        assert total["data_field"] is None
        assert (
            total["value"]
            == "=Fields!Amount.Value * Fields!ProductID.Value"
        )

    def test_unknown_dataset_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            get_dataset(path=str(rdl_path), name="NoSuchDataset")


# ---- v0.3 calculated fields ----------------------------------------------


class TestAddCalculatedField:
    def test_appends_calculated_field(self, rdl_path):
        result = add_calculated_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Total",
            expression="=Fields!Amount.Value * Fields!ProductID.Value",
        )
        assert result["name"] == "Total"
        assert result["kind"] == "CalculatedField"
        assert result["value"] == "=Fields!Amount.Value * Fields!ProductID.Value"
        # Verify on disk via get_dataset.
        ds = get_dataset(path=str(rdl_path), name="MainDataset")
        total = next(f for f in ds["fields"] if f["name"] == "Total")
        assert total["data_field"] is None
        assert total["value"] == "=Fields!Amount.Value * Fields!ProductID.Value"

    def test_get_datasets_surfaces_calculated_value(self, rdl_path):
        # Regression: get_datasets (the multi-dataset reader) must also
        # surface 'value' on calculated fields, not just data_field.
        from pbirb_mcp.ops.reader import get_datasets

        add_calculated_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="DoublePrice",
            expression="=Fields!Amount.Value * 2",
        )
        result = get_datasets(path=str(rdl_path))
        ds = result[0]
        double_price = next(f for f in ds["fields"] if f["name"] == "DoublePrice")
        assert double_price["value"] == "=Fields!Amount.Value * 2"
        assert double_price["data_field"] is None
        # Existing data-bound fields must still have their data_field
        # populated and value=None.
        product_id = next(f for f in ds["fields"] if f["name"] == "ProductID")
        assert product_id["data_field"] == "Sales[ProductID]"
        assert product_id["value"] is None

    def test_duplicate_field_name_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="already exists"):
            add_calculated_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="ProductID",  # already a data-bound field
                expression="=1",
            )

    def test_empty_field_name_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            add_calculated_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="",
                expression="=1",
            )

    def test_empty_expression_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            add_calculated_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="X",
                expression="   ",
            )

    def test_unknown_dataset_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_calculated_field(
                path=str(rdl_path),
                dataset_name="NoSuchDataset",
                field_name="X",
                expression="=1",
            )

    def test_pre_encoded_expression_no_double_encode(self, rdl_path):
        add_calculated_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="LabelExpr",
            expression='=IIf(Fields!ProductName.Value = "A &amp; B", 1, 0)',
        )
        assert b"&amp;amp;" not in (rdl_path).read_bytes()

    def test_round_trip_safe(self, rdl_path):
        add_calculated_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Computed",
            expression="=Fields!Amount.Value + 100",
        )
        RDLDocument.open(rdl_path).validate()


class TestRemoveCalculatedField:
    def test_removes_named_calculated_field(self, rdl_path):
        add_calculated_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Total",
            expression="=Fields!Amount.Value * 2",
        )
        result = remove_calculated_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Total",
        )
        assert result["removed"] == "Total"
        ds = get_dataset(path=str(rdl_path), name="MainDataset")
        names = [f["name"] for f in ds["fields"]]
        assert "Total" not in names

    def test_refuses_data_bound_field(self, rdl_path):
        with pytest.raises(ValueError, match="data-bound"):
            remove_calculated_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="ProductID",  # data-bound
            )

    def test_unknown_field_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_calculated_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="NoSuchField",
            )

    def test_unknown_dataset_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_calculated_field(
                path=str(rdl_path),
                dataset_name="NoSuchDataset",
                field_name="X",
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

    def test_v03_dataset_filter_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {
            "list_dataset_filters",
            "add_dataset_filter",
            "remove_dataset_filter",
            "get_dataset",
        } <= names

    def test_v03_calculated_field_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {
            "add_calculated_field",
            "remove_calculated_field",
        } <= names
