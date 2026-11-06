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
from typing import Optional

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import QD_NS, RD_NS, RDL_NS, find_child
from pbirb_mcp.ops import expr_walk
from pbirb_mcp.ops.dataset import (
    add_calculated_field,
    add_dataset_field,
    add_dataset_filter,
    add_query_parameter,
    get_dataset,
    list_dataset_filters,
    refresh_dataset_fields,
    remove_calculated_field,
    remove_dataset_field,
    remove_dataset_filter,
    remove_query_parameter,
    update_dataset_query,
    update_query_parameter,
)
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.parameters import (
    set_parameter_available_values,
    set_parameter_default_values,
)
from pbirb_mcp.ops.reader import get_datasets
from pbirb_mcp.ops.styling import set_textbox_value
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools
from tests._designer_state import build_designer_state_report

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

    def test_entity_shaped_dax_stored_verbatim(self, rdl_path):
        # #75: SUBSTITUTE(col, "&amp;", "&") cleans HTML-escaped data; the
        # "&amp;" literal must survive the write (it used to be decoded to
        # "&", silently changing the query).
        dax = (
            "EVALUATE SELECTCOLUMNS('Sales', \"Clean\", "
            'SUBSTITUTE(\'Sales\'[ProductName], "&amp;", "&"))'
        )
        result = update_dataset_query(str(rdl_path), "MainDataset", dax)
        assert result["command_text"] == dax
        assert get_dataset(str(rdl_path), "MainDataset")["command_text"] == dax
        assert b'"&amp;amp;", "&amp;"' in rdl_path.read_bytes()
        assert validate_report(str(rdl_path))["valid"]

    def test_entity_shaped_dax_resubmit_is_noop(self, rdl_path):
        dax = "EVALUATE FILTER('Sales', 'Sales'[ProductName] = \"R&amp;D\")"
        update_dataset_query(str(rdl_path), "MainDataset", dax)
        before = rdl_path.read_bytes()
        again = update_dataset_query(str(rdl_path), "MainDataset", dax)
        assert not again["changed"]
        assert rdl_path.read_bytes() == before


def _inject_designer_state(rdl_path: Path, dataset_name: str, statement_text: str):
    """Add the <rd:DesignerState> block Report Builder's Query Designer
    writes (#70): rd:DesignerState/QueryDefinition/Query/Statement in the
    AnalysisServices QueryDefinition namespace."""
    doc = RDLDocument.open(rdl_path)
    ds = next(d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == dataset_name)
    query = find_child(ds, "Query")
    designer_state = etree.SubElement(query, f"{{{RD_NS}}}DesignerState")
    qdef = etree.SubElement(designer_state, f"{{{QD_NS}}}QueryDefinition", nsmap={None: QD_NS})
    etree.SubElement(qdef, f"{{{QD_NS}}}CommandType").text = "DAX"
    qd_query = etree.SubElement(qdef, f"{{{QD_NS}}}Query")
    etree.SubElement(qd_query, f"{{{QD_NS}}}Statement").text = statement_text
    etree.SubElement(qd_query, f"{{{QD_NS}}}ParameterDefinitions")
    doc.save()


def _inject_legacy_designer_state(rdl_path: Path, dataset_name: str, statement_text: str):
    """The pre-#70 assumed shape: <rd:DesignerState>/<rd:Statement>. Still
    accepted as a fallback by find_designer_statement."""
    doc = RDLDocument.open(rdl_path)
    ds = next(d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == dataset_name)
    query = find_child(ds, "Query")
    designer_state = etree.SubElement(query, f"{{{RD_NS}}}DesignerState")
    statement = etree.SubElement(designer_state, f"{{{RD_NS}}}Statement")
    statement.text = statement_text
    doc.save()


def _designer_statement_text(rdl_path: Path) -> Optional[str]:
    st = next(etree.parse(str(rdl_path)).iter(f"{{{QD_NS}}}Statement"), None)
    return None if st is None else st.text


_SALES_DAX = "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], 'Sales'[ProductName], 'Sales'[Amount])"
_ITEMS_DAX = "EVALUATE SUMMARIZECOLUMNS('Items'[ItemID], 'Items'[ItemName], 'Items'[Total])"


def _set_old_dax(rdl_path: Path, dax: str = _SALES_DAX) -> None:
    """Give the fixture a parseable "current" query (its own is the bare
    EVALUATE 'Sales', whose column order is unknowable)."""
    update_dataset_query(path=str(rdl_path), dataset_name="MainDataset", dax_body=dax)


def _preserve(rdl_path: Path, dax: str, **kw):
    return update_dataset_query(
        path=str(rdl_path),
        dataset_name="MainDataset",
        dax_body=dax,
        alias_strategy="preserve_field_names",
        **kw,
    )


def _data_fields(rdl_path: Path) -> list:
    return [f["data_field"] for f in get_datasets(str(rdl_path))[0]["fields"]]


def _alias_shaped(rdl_path: Path) -> None:
    s = rdl_path.read_text(encoding="utf-8")
    s = (
        s.replace("Sales[ProductID]", "[Pid]")
        .replace("Sales[ProductName]", "[Name]")
        .replace("Sales[Amount]", "[Amt]")
    )
    rdl_path.write_text(s, encoding="utf-8")


class TestUpdateDatasetQueryAliasStrategy:
    """preserve_field_names: identity-first remap (#38).

    The fixture ships with three data-bound fields (ProductID,
    ProductName, Amount) backed by Sales[ProductID] / [ProductName] /
    [Amount]. Each <Field Name="..."> stays stable; <DataField> cells
    are only re-aimed when the old and new queries say unambiguously
    which new column replaced which old one.
    """

    def test_default_strategy_leaves_fields_unchanged(self, rdl_path):
        before = get_datasets(str(rdl_path))[0]["fields"]
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body=_ITEMS_DAX,
        )
        after = get_datasets(str(rdl_path))[0]["fields"]
        assert before == after  # no remap when alias_strategy is None

    def test_preserve_field_names_remaps_data_fields(self, rdl_path):
        # #38: the remap is driven by the OLD query's column order, so
        # the old query must be parseable (the fixture's EVALUATE 'Sales'
        # isn't).
        _set_old_dax(rdl_path)
        result = _preserve(rdl_path, _ITEMS_DAX)
        assert result["alias_strategy"] == "preserve_field_names"
        assert result["mapped"] == [
            {"name": "ProductID", "old": "Sales[ProductID]", "new": "Items[ItemID]"},
            {"name": "ProductName", "old": "Sales[ProductName]", "new": "Items[ItemName]"},
            {"name": "Amount", "old": "Sales[Amount]", "new": "Items[Total]"},
        ]
        assert result["warnings"] == []
        # Field NAMES are unchanged; DataFields point at the new columns
        # IN THEIR QUALIFIED FORM ("Items[ItemID]").
        fields = get_datasets(str(rdl_path))[0]["fields"]
        assert [f["name"] for f in fields] == ["ProductID", "ProductName", "Amount"]
        assert _data_fields(rdl_path) == ["Items[ItemID]", "Items[ItemName]", "Items[Total]"]

    def test_reordered_columns_change_nothing(self, rdl_path):
        # Issue repro (a): same columns, different order.
        before = rdl_path.read_bytes()
        dax = "EVALUATE SUMMARIZECOLUMNS('Sales'[Amount], 'Sales'[ProductID], 'Sales'[ProductName])"
        result = _preserve(rdl_path, dax)
        assert result["mapped"] == []
        assert result["warnings"] == []
        # Only the CommandText changed.
        after = rdl_path.read_bytes()
        assert after.replace(dax.encode(), b"EVALUATE 'Sales'") == before

    def test_rerunning_own_dax_is_a_noop(self, rdl_path):
        _set_old_dax(rdl_path)
        before = rdl_path.read_bytes()
        result = _preserve(rdl_path, _SALES_DAX)
        assert result["mapped"] == []
        assert result["warnings"] == []
        assert rdl_path.read_bytes() == before

    def test_selectcolumns_alias_rename(self, rdl_path):
        # Issue repro (b): alias Amt -> Total. DataField [Amt] -> [Total].
        _alias_shaped(rdl_path)
        result = _preserve(
            rdl_path,
            "EVALUATE SELECTCOLUMNS('Sales', \"Pid\", 'Sales'[ProductID], "
            "\"Name\", 'Sales'[ProductName], \"Total\", 'Sales'[Amount])",
        )
        assert result["mapped"] == [{"name": "Amount", "old": "[Amt]", "new": "[Total]"}]
        assert _data_fields(rdl_path) == ["[Pid]", "[Name]", "[Total]"]

    def test_selectcolumns_alias_rename_in_place_with_known_old_order(self, rdl_path):
        _alias_shaped(rdl_path)
        _set_old_dax(
            rdl_path,
            "EVALUATE SELECTCOLUMNS('Sales', \"Pid\", 'Sales'[ProductID], "
            "\"Name\", 'Sales'[ProductName], \"Amt\", 'Sales'[Amount], \"Qty\", 'Sales'[Qty])",
        )
        result = _preserve(
            rdl_path,
            "EVALUATE SELECTCOLUMNS('Sales', \"Pid\", 'Sales'[ProductID], "
            "\"Name\", 'Sales'[ProductName], \"Total\", 'Sales'[Amount], \"Qty\", 'Sales'[Qty])",
        )
        assert result["mapped"] == [{"name": "Amount", "old": "[Amt]", "new": "[Total]"}]
        # [Qty] has no Field (before and after): reported, not bound.
        assert [w for w in result["warnings"] if "no Field" in w] == [
            "new DAX column '[Qty]' has no Field — call add_dataset_field or "
            "refresh_dataset_fields to add a binding."
        ]

    def test_alias_moved_to_another_slot_is_not_guessed(self, rdl_path):
        _alias_shaped(rdl_path)
        _set_old_dax(
            rdl_path,
            "EVALUATE SELECTCOLUMNS('Sales', \"Pid\", 'Sales'[ProductID], "
            "\"Name\", 'Sales'[ProductName], \"Amt\", 'Sales'[Amount])",
        )
        result = _preserve(
            rdl_path,
            "EVALUATE SELECTCOLUMNS('Sales', \"Pid\", 'Sales'[ProductID], "
            "\"Total\", 'Sales'[Amount], \"Name\", 'Sales'[ProductName])",
        )
        # Amt vanished after Name; Total arrived after Pid — different
        # slots, so no guess.
        assert result["mapped"] == []
        assert any("'Amount'" in w and "unmapped" in w for w in result["warnings"])
        assert any("[Total]" in w and "no Field" in w for w in result["warnings"])
        assert _data_fields(rdl_path) == ["[Pid]", "[Name]", "[Amt]"]

    def test_one_column_replaced_in_place(self, rdl_path):
        _set_old_dax(rdl_path)
        result = _preserve(
            rdl_path,
            "EVALUATE SUMMARIZECOLUMNS('Sales'[Amount], 'Sales'[ProductID], "
            "'Sales'[Product Label])",
        )
        # ProductName vanished after ProductID; Product Label arrived
        # after ProductID: exactly one new column in that slot.
        assert result["mapped"] == [
            {"name": "ProductName", "old": "Sales[ProductName]", "new": "Sales[Product Label]"}
        ]
        assert result["warnings"] == []
        assert _data_fields(rdl_path) == [
            "Sales[ProductID]",
            "Sales[Product Label]",
            "Sales[Amount]",
        ]
        assert validate_report(str(rdl_path))["valid"]

    def test_add_and_drop_mismatch_warns_only(self, rdl_path):
        _set_old_dax(rdl_path)
        before = _data_fields(rdl_path)
        result = _preserve(
            rdl_path,
            "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], 'Sales'[X], 'Sales'[Y], "
            "'Sales'[Amount])",
        )
        assert result["mapped"] == []
        assert any("ambiguous" in w for w in result["warnings"])
        assert len([w for w in result["warnings"] if "unmapped" in w]) == 1
        assert len([w for w in result["warnings"] if "no Field" in w]) == 2
        assert _data_fields(rdl_path) == before

    def test_count_mismatch_more_fields_than_columns_warns(self, rdl_path):
        _set_old_dax(rdl_path)
        result = _preserve(rdl_path, "EVALUATE SUMMARIZECOLUMNS('Items'[Only])")
        # Three columns vanished, one arrived: not a 1:1 replacement.
        assert result["mapped"] == []
        unmapped_warnings = [w for w in result["warnings"] if "unmapped" in w]
        assert len(unmapped_warnings) == 3

    def test_count_mismatch_more_columns_than_fields_warns(self, rdl_path):
        _set_old_dax(rdl_path)
        result = _preserve(
            rdl_path,
            "EVALUATE SUMMARIZECOLUMNS('Items'[A], 'Items'[B], 'Items'[C], 'Items'[D], 'Items'[E])",
        )
        assert result["mapped"] == []
        no_field_warnings = [w for w in result["warnings"] if "no Field" in w]
        assert len(no_field_warnings) == 5

    def test_dropped_column_warns_only(self, rdl_path):
        _set_old_dax(rdl_path)
        result = _preserve(
            rdl_path, "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], 'Sales'[Amount])"
        )
        assert result["mapped"] == []
        assert len(result["warnings"]) == 1
        assert "'ProductName'" in result["warnings"][0]

    def test_unknown_old_order_does_not_guess(self, rdl_path):
        # Fixture's own EVALUATE 'Sales' has no visible column order.
        before = _data_fields(rdl_path)
        result = _preserve(rdl_path, _ITEMS_DAX)
        assert result["mapped"] == []
        assert any("previous DAX" in w for w in result["warnings"])
        assert _data_fields(rdl_path) == before

    def test_dry_run_reports_without_saving(self, rdl_path):
        _set_old_dax(rdl_path)
        before = rdl_path.read_bytes()
        result = _preserve(rdl_path, _ITEMS_DAX, dry_run=True)
        assert result["dry_run"] is True
        assert len(result["mapped"]) == 3
        assert rdl_path.read_bytes() == before

    def test_dry_run_through_server(self, rdl_path):
        _set_old_dax(rdl_path)
        before = rdl_path.read_bytes()
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "update_dataset_query",
                    "arguments": {
                        "path": str(rdl_path),
                        "dataset_name": "MainDataset",
                        "dax_body": _ITEMS_DAX,
                        "alias_strategy": "preserve_field_names",
                        "dry_run": True,
                    },
                },
            }
        )
        assert "error" not in resp
        assert not resp["result"].get("isError")
        assert rdl_path.read_bytes() == before

    def test_calculated_fields_skipped(self, rdl_path):
        # Add a calculated field, then run preserve_field_names.
        add_calculated_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Doubled",
            expression="=Fields!Amount.Value * 2",
        )
        _set_old_dax(rdl_path)
        result = _preserve(rdl_path, _ITEMS_DAX)
        # Calculated field is not in the data-bound list — only 3 mapped.
        assert len(result["mapped"]) == 3
        fields = get_datasets(str(rdl_path))[0]["fields"]
        calc = [f for f in fields if f["name"] == "Doubled"][0]
        assert calc.get("value") == "=Fields!Amount.Value * 2"

    def test_unknown_alias_strategy_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="unknown alias_strategy"):
            update_dataset_query(
                path=str(rdl_path),
                dataset_name="MainDataset",
                dax_body="EVALUATE 'Items'",
                alias_strategy="bogus",
            )

    def test_no_op_dax_with_strategy_is_true_noop(self, rdl_path):
        """When the new DAX has the same Table[Col] columns as the
        existing DataFields, the remap is a true no-op (mapped is
        empty)."""
        before = get_datasets(str(rdl_path))[0]["fields"]
        result = _preserve(rdl_path, _SALES_DAX)
        assert result["mapped"] == []
        after = get_datasets(str(rdl_path))[0]["fields"]
        assert before == after

    def test_bare_token_dax_preserves_existing_data_fields(self, rdl_path):
        """Bare-token SUMMARIZECOLUMNS (no quoted table prefix, no
        brackets) yields no recognisable result columns: existing
        DataFields stay untouched and warnings say why."""
        before = get_datasets(str(rdl_path))[0]["fields"]
        result = _preserve(rdl_path, "EVALUATE SUMMARIZECOLUMNS(ProductID, ProductName, Amount)")
        assert result["mapped"] == []
        assert result["warnings"]
        after = get_datasets(str(rdl_path))[0]["fields"]
        assert before == after


class TestUpdateDatasetQueryDesignerStateSync:
    """update_dataset_query must rewrite Report Builder's Query Designer
    Statement (rd:DesignerState/QueryDefinition/Query/Statement, #70) in
    lockstep with <CommandText>. Without this the designer shows stale DAX
    and can regenerate the old query over the new one.
    """

    def test_report_builder_shape_synced(self, tmp_path):
        # Issue #70 repro, against the real Report Builder shape.
        path = build_designer_state_report(tmp_path / "rb.rdl")
        st = next(etree.parse(str(path)).iter(f"{{{QD_NS}}}Statement"))
        assert [etree.QName(a).localname for a in st.iterancestors()][:3] == [
            "Query",
            "QueryDefinition",
            "DesignerState",
        ]
        result = update_dataset_query(str(path), "MainDataset", 'EVALUATE ROW("x", 1)')
        assert result["designer_state_synced"] is True
        assert _designer_statement_text(path) == 'EVALUATE ROW("x", 1)'
        # The QuerySpecification graph is left alone (it can't describe the DAX).
        assert b"MDXQuerySpecification" in path.read_bytes()

    def test_report_builder_shape_resubmit_is_noop(self, tmp_path):
        path = build_designer_state_report(tmp_path / "rb.rdl")
        update_dataset_query(str(path), "MainDataset", 'EVALUATE ROW("x", 1)')
        before = path.read_bytes()
        again = update_dataset_query(str(path), "MainDataset", 'EVALUATE ROW("x", 1)')
        assert again["designer_state_synced"] is False
        assert not again["changed"]
        assert path.read_bytes() == before

    def test_designer_state_statement_synced(self, rdl_path):
        _inject_designer_state(rdl_path, "MainDataset", "OLD: EVALUATE 'OldTable'")
        result = update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="NEW: EVALUATE 'NewTable'",
        )
        assert result["designer_state_synced"] is True
        assert _designer_statement_text(rdl_path) == "NEW: EVALUATE 'NewTable'"

    def test_legacy_rd_statement_still_synced(self, rdl_path):
        _inject_legacy_designer_state(rdl_path, "MainDataset", "OLD")
        result = update_dataset_query(str(rdl_path), "MainDataset", "EVALUATE 'New'")
        assert result["designer_state_synced"] is True
        doc = RDLDocument.open(rdl_path)
        statement = next(doc.root.iter(f"{{{RD_NS}}}Statement"))
        assert statement.text == "EVALUATE 'New'"

    def test_drop_removes_designer_state(self, tmp_path):
        path = build_designer_state_report(tmp_path / "rb.rdl")
        result = update_dataset_query(
            str(path), "MainDataset", 'EVALUATE ROW("x", 1)', designer_state="drop"
        )
        assert result["designer_state_dropped"] is True
        assert result["designer_state_synced"] is False
        assert b"DesignerState" not in path.read_bytes()
        assert get_dataset(str(path), "MainDataset")["designer_state_present"] is False
        assert get_dataset(str(path), "MainDataset")["command_text"] == 'EVALUATE ROW("x", 1)'
        # Dropping the foreign QueryDefinition payload leaves a valid report.
        assert validate_report(str(path))["valid"]

    def test_drop_again_is_noop(self, tmp_path):
        path = build_designer_state_report(tmp_path / "rb.rdl")
        update_dataset_query(str(path), "MainDataset", "EVALUATE 'X'", designer_state="drop")
        before = path.read_bytes()
        again = update_dataset_query(
            str(path), "MainDataset", "EVALUATE 'X'", designer_state="drop"
        )
        assert again["designer_state_dropped"] is False
        assert not again["changed"]
        assert path.read_bytes() == before

    def test_drop_with_dry_run_does_not_write(self, tmp_path):
        path = build_designer_state_report(tmp_path / "rb.rdl")
        before = path.read_bytes()
        result = update_dataset_query(
            str(path), "MainDataset", "EVALUATE 'X'", designer_state="drop", dry_run=True
        )
        assert result["designer_state_dropped"] is True
        assert path.read_bytes() == before

    def test_unknown_designer_state_mode_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="designer_state"):
            update_dataset_query(
                str(rdl_path), "MainDataset", "EVALUATE 'X'", designer_state="keep"
            )

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

    def test_sync_writes_dax_verbatim(self, rdl_path):
        """#75: the DesignerState rewrite stores the DAX verbatim, exactly
        like CommandText — entity-shaped text in DAX is meaningful."""
        _inject_designer_state(rdl_path, "MainDataset", "old")
        dax = "EVALUATE FILTER('X', 'X'[c] = \"A &amp; B\")"
        update_dataset_query(path=str(rdl_path), dataset_name="MainDataset", dax_body=dax)
        # Escaped exactly once on disk; reads back as the input.
        assert b"&amp;amp;" in rdl_path.read_bytes()
        assert get_dataset(str(rdl_path), "MainDataset")["command_text"] == dax


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

    def test_literal_value_stored_verbatim(self, rdl_path):
        # #75: a literal (no leading '=') is not entity-decoded — 'R&amp;D'
        # is a legitimate value and must read back unchanged.
        result = add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["R&amp;D"],
        )
        assert result["values"] == ["R&amp;D"]
        assert result["warnings"] == []
        assert list_dataset_filters(str(rdl_path), "MainDataset")[0]["values"] == ["R&amp;D"]

    def test_pre_encoded_expression_normalised_with_warning(self, rdl_path):
        # #75: '='-expressions keep the double-encoding guard, but the
        # rewrite is surfaced as a warning and the response echoes what
        # was written.
        result = add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression='=Fields!ProductName.Value = "A &amp; B"',
            operator="Equal",
            values=["=True"],
        )
        assert result["expression"] == '=Fields!ProductName.Value = "A & B"'
        assert any("decoded" in w for w in result["warnings"])
        assert b"&amp;amp;" not in rdl_path.read_bytes()
        stored = list_dataset_filters(str(rdl_path), "MainDataset")[0]
        assert stored["expression"] == result["expression"]

    def test_pre_encoded_expression_value_normalised(self, rdl_path):
        result = add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=['="A &amp; B"'],
        )
        assert result["values"] == ['="A & B"']
        assert len(result["warnings"]) == 1
        assert list_dataset_filters(str(rdl_path), "MainDataset")[0]["values"] == ['="A & B"']

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
        remove_dataset_filter(path=str(rdl_path), dataset_name="MainDataset", filter_index=1)
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
        remove_dataset_filter(path=str(rdl_path), dataset_name="MainDataset", filter_index=0)
        # The <Filters> block itself should be gone.
        doc = RDLDocument.open(rdl_path)
        ds = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == "MainDataset"
        )
        from pbirb_mcp.core.xpath import find_child as _fc

        assert _fc(ds, "Filters") is None

    def test_out_of_range_raises(self, rdl_path):
        with pytest.raises(IndexError):
            remove_dataset_filter(path=str(rdl_path), dataset_name="MainDataset", filter_index=99)


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
        _etree.SubElement(
            new_field, _q("Value")
        ).text = "=Fields!Amount.Value * Fields!ProductID.Value"
        doc.save()

        result = get_dataset(path=str(rdl_path), name="MainDataset")
        total = next(f for f in result["fields"] if f["name"] == "Total")
        assert total["data_field"] is None
        assert total["value"] == "=Fields!Amount.Value * Fields!ProductID.Value"

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


class TestRemoveDatasetField:
    """Symmetric with ``remove_calculated_field`` but for data-bound
    fields (``<DataField>``, not ``<Value>``). Closes the cookbook
    flow ``refresh_dataset_fields`` (lists orphans) → review →
    ``remove_dataset_field`` (drops them)."""

    def test_removes_data_bound_field(self, rdl_path):
        # Add a data-bound field, then remove it.
        add_dataset_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="ExtraColumn",
            data_field="ExtraColumn",
        )
        result = remove_dataset_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="ExtraColumn",
        )
        assert result == {
            "dataset": "MainDataset",
            "removed": "ExtraColumn",
            "kind": "DataBoundField",
        }
        ds = get_dataset(path=str(rdl_path), name="MainDataset")
        assert "ExtraColumn" not in [f["name"] for f in ds["fields"]]

    def test_refuses_calculated_field_with_hint(self, rdl_path):
        add_calculated_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Doubled",
            expression="=Fields!Amount.Value * 2",
        )
        with pytest.raises(ValueError, match="remove_calculated_field"):
            remove_dataset_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="Doubled",
            )

    def test_refuses_when_field_still_referenced(self, rdl_path):
        # The fixture's tablix already references Fields!Amount.Value.
        with pytest.raises(ValueError) as excinfo:
            remove_dataset_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="Amount",
            )
        msg = str(excinfo.value)
        assert "Amount" in msg
        assert "force" in msg.lower()  # mentions force=True override

    def test_force_removes_referenced_field(self, rdl_path):
        # force=True bypasses the reference check.
        result = remove_dataset_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Amount",
            force=True,
        )
        assert result["removed"] == "Amount"

    def test_unknown_dataset_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_dataset_field(
                path=str(rdl_path),
                dataset_name="NoSuchDataset",
                field_name="X",
            )

    def test_unknown_field_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_dataset_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="NoSuchField",
            )

    def test_cleans_empty_fields_block(self, rdl_path):
        # Remove all data-bound fields; confirm the empty <Fields> is dropped.
        for fname in ("ProductID", "ProductName", "Amount"):
            remove_dataset_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name=fname,
                force=True,
            )
        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(f".//{{{RDL_NS}}}DataSet[@Name='MainDataset']")
        assert find_child(ds, "Fields") is None


# ---- scope-aware field-reference guards (#72) -----------------------------

MULTI_FIXTURE = Path(__file__).parent / "fixtures" / "pbi_multi_datasource.rdl"


@pytest.fixture
def multi_path(tmp_path: Path) -> Path:
    dest = tmp_path / "multi.rdl"
    shutil.copy(MULTI_FIXTURE, dest)
    return dest


def _refusal(fn, *args, **kw) -> str:
    with pytest.raises(ValueError) as excinfo:
        fn(*args, **kw)
    msg = str(excinfo.value)
    assert "(unnamed)" not in msg
    assert "force=True" in msg
    return msg


class TestFieldReferenceGuards:
    """Issue #72 acceptance (a)-(d) on pbi_multi_datasource.rdl, plus the
    other consumer shapes find_field_references covers."""

    def test_a_parameter_valid_values_blocks_removal(self, multi_path):
        set_parameter_available_values(
            str(multi_path),
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
        )
        msg = _refusal(remove_dataset_field, str(multi_path), "LookupSet", "LookupKey")
        assert "ReportParameter[Name='DateFrom']/ValidValues" in msg
        assert "ValueField" in msg

    def test_a_label_field_and_default_value_block_removal(self, multi_path):
        add_dataset_field(str(multi_path), "LookupSet", "LookupLabel", "Lookup[Label]")
        set_parameter_available_values(
            str(multi_path),
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
            query_label_field="LookupLabel",
        )
        msg = _refusal(remove_dataset_field, str(multi_path), "LookupSet", "LookupLabel")
        assert "ValidValues/DataSetReference/LabelField" in msg
        set_parameter_default_values(
            str(multi_path),
            "DateTo",
            "query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
        )
        msg = _refusal(remove_dataset_field, str(multi_path), "LookupSet", "LookupKey")
        assert "ReportParameter[Name='DateTo']/DefaultValue" in msg

    def test_b_same_name_in_other_dataset_does_not_block(self, multi_path):
        # MainTable (bound to MainDataset) uses Fields!ProductID.Value; that
        # is not a consumer of LookupSet.ProductID.
        add_dataset_field(str(multi_path), "LookupSet", "ProductID", "Lookup[ProductID]")
        result = remove_dataset_field(str(multi_path), "LookupSet", "ProductID")
        assert result["removed"] == "ProductID"
        # ...while MainDataset.ProductID is still guarded.
        msg = _refusal(remove_dataset_field, str(multi_path), "MainDataset", "ProductID")
        assert "Textbox[Name='ProductID']" in msg
        assert validate_report(str(multi_path))["valid"]

    def test_c_fields_string_form_blocks_removal(self, multi_path):
        add_dataset_field(str(multi_path), "MainDataset", "Extra", "Sales[Extra]")
        set_textbox_value(str(multi_path), "Amount", '=Fields("Extra").Value')
        msg = _refusal(remove_dataset_field, str(multi_path), "MainDataset", "Extra")
        assert "Textbox[Name='Amount']" in msg

    def test_d_calculated_field_guarded(self, multi_path):
        add_calculated_field(str(multi_path), "MainDataset", "Calc", "=1")
        set_textbox_value(str(multi_path), "ProductName", "=Sum(Fields!Calc.Value)")
        msg = _refusal(remove_calculated_field, str(multi_path), "MainDataset", "Calc")
        assert "Textbox[Name='ProductName']" in msg

    def test_d_calculated_field_force(self, multi_path):
        add_calculated_field(str(multi_path), "MainDataset", "Calc", "=1")
        set_textbox_value(str(multi_path), "ProductName", "=Sum(Fields!Calc.Value)")
        result = remove_calculated_field(str(multi_path), "MainDataset", "Calc", force=True)
        assert result["removed"] == "Calc"

    def test_unreferenced_calculated_field_still_removable(self, multi_path):
        add_calculated_field(str(multi_path), "MainDataset", "Calc", "=1")
        assert remove_calculated_field(str(multi_path), "MainDataset", "Calc")["removed"] == "Calc"

    def test_other_calculated_field_is_a_consumer(self, multi_path):
        add_calculated_field(str(multi_path), "MainDataset", "Base", "=1")
        add_calculated_field(str(multi_path), "MainDataset", "Twice", "=Fields!Base.Value * 2")
        msg = _refusal(remove_calculated_field, str(multi_path), "MainDataset", "Base")
        assert "Field[Name='Twice']" in msg
        # A same-named reference in another dataset's calculated field is
        # evaluated against that dataset, so it doesn't block.
        add_calculated_field(str(multi_path), "LookupSet", "Base", "=2")
        add_calculated_field(str(multi_path), "LookupSet", "Other", "=Fields!Base.Value")
        assert remove_calculated_field(str(multi_path), "MainDataset", "Twice")["removed"]
        assert remove_calculated_field(str(multi_path), "MainDataset", "Base")["removed"]

    def test_dataset_filter_is_a_consumer(self, multi_path):
        add_dataset_field(str(multi_path), "LookupSet", "Region", "Lookup[Region]")
        add_dataset_filter(str(multi_path), "LookupSet", "=Fields!Region.Value", "Equal", ["North"])
        msg = _refusal(remove_dataset_field, str(multi_path), "LookupSet", "Region")
        assert "DataSet[Name='LookupSet']" in msg

    def test_explicit_aggregate_scope_resolves_dataset(self, multi_path):
        # Inside MainTable, but aggregated over LookupSet explicitly.
        add_dataset_field(str(multi_path), "LookupSet", "Weight", "Lookup[Weight]")
        set_textbox_value(str(multi_path), "Amount", '=Sum(Fields!Weight.Value, "LookupSet")')
        msg = _refusal(remove_dataset_field, str(multi_path), "LookupSet", "Weight")
        assert "Textbox[Name='Amount']" in msg

    def test_lookup_destination_resolves_dataset(self, multi_path):
        set_textbox_value(
            str(multi_path),
            "Amount",
            "=Lookup(Fields!ProductID.Value, Fields!LookupKey.Value, "
            'Fields!LookupKey.Value, "LookupSet")',
        )
        msg = _refusal(remove_dataset_field, str(multi_path), "LookupSet", "LookupKey")
        assert "Textbox[Name='Amount']" in msg

    def test_reference_inside_string_literal_does_not_block(self, multi_path):
        add_dataset_field(str(multi_path), "MainDataset", "Extra", "Sales[Extra]")
        set_textbox_value(str(multi_path), "Amount", '="Fields!Extra.Value"')
        assert remove_dataset_field(str(multi_path), "MainDataset", "Extra")["removed"]

    def test_find_field_references_is_public(self, multi_path):
        doc = RDLDocument.open(multi_path)
        refs = expr_walk.find_field_references(doc.root, "MainDataset", "Amount")
        assert [r.kind for r in refs] == ["expression"]
        assert refs[0].text == "Fields!Amount.Value"
        assert refs[0].scope == "MainDataset"
        assert refs[0].locator.startswith("Tablix[Name='MainTable']>Textbox[Name='Amount']")
        assert expr_walk.find_field_references(doc.root, "LookupSet", "Amount") == []


class TestDanglingDatasetReferenceFieldLint:
    """#72: lint rule for parameter DataSetReference fields that don't exist."""

    def test_clean_when_field_exists(self, multi_path):
        set_parameter_available_values(
            str(multi_path),
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
        )
        result = lint_report(str(multi_path), rules=["dangling-dataset-reference-field"])
        assert result["issues"] == []

    def test_fires_after_forced_removal(self, multi_path):
        set_parameter_available_values(
            str(multi_path),
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
            query_label_field="LookupKey",
        )
        # #93: the writer refuses a missing field, so plant one by hand.
        text = multi_path.read_text(encoding="utf-8")
        multi_path.write_text(
            text.replace("<LabelField>LookupKey</LabelField>", "<LabelField>Missing</LabelField>"),
            encoding="utf-8",
        )
        remove_dataset_field(str(multi_path), "LookupSet", "LookupKey", force=True)
        issues = lint_report(str(multi_path), rules=["dangling-dataset-reference-field"])["issues"]
        assert sorted(i["message"] for i in issues) == [
            "DataSetReference LabelField 'Missing' is not a field of dataset 'LookupSet'",
            "DataSetReference ValueField 'LookupKey' is not a field of dataset 'LookupSet'",
        ]
        assert all(i["severity"] == "error" for i in issues)
        assert all("ReportParameter[Name='DateFrom']" in i["location"] for i in issues)

    def test_silent_on_unknown_dataset(self, multi_path):
        # A DataSetName naming no dataset is dangling-dataset-reference's job.
        set_parameter_available_values(
            str(multi_path),
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
        )
        text = multi_path.read_text(encoding="utf-8")
        multi_path.write_text(
            text.replace("<DataSetName>LookupSet</DataSetName>", "<DataSetName>Nope</DataSetName>"),
            encoding="utf-8",
        )
        result = lint_report(str(multi_path), rules=["dangling-dataset-reference-field"])
        assert result["issues"] == []


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
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
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
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {
            "add_calculated_field",
            "remove_calculated_field",
            "remove_dataset_field",
        } <= names

    def test_v03_phase6_dataset_field_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {"add_dataset_field", "refresh_dataset_fields"} <= names


# ---- v0.3 Phase 6: add_dataset_field --------------------------------------


class TestAddDatasetField:
    def test_appends_data_bound_field(self, rdl_path):
        result = add_dataset_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Region",
            data_field="Sales[Region]",
        )
        assert result["name"] == "Region"
        assert result["kind"] == "DataBoundField"
        assert result["data_field"] == "Sales[Region]"
        ds = get_dataset(path=str(rdl_path), name="MainDataset")
        region = next(f for f in ds["fields"] if f["name"] == "Region")
        assert region["data_field"] == "Sales[Region]"
        assert region["value"] is None

    def test_writes_type_name_when_supplied(self, rdl_path):
        add_dataset_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="OrderDate",
            data_field="Sales[OrderDate]",
            type_name="System.DateTime",
        )
        ds = get_dataset(path=str(rdl_path), name="MainDataset")
        order = next(f for f in ds["fields"] if f["name"] == "OrderDate")
        assert order["type_name"] == "System.DateTime"

    def test_omits_type_name_when_none(self, rdl_path):
        add_dataset_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Plain",
            data_field="Sales[Plain]",
        )
        ds = get_dataset(path=str(rdl_path), name="MainDataset")
        plain = next(f for f in ds["fields"] if f["name"] == "Plain")
        assert plain["type_name"] is None

    def test_duplicate_name_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="already exists"):
            add_dataset_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="ProductID",
                data_field="Sales[ProductID]",
            )

    def test_empty_field_name_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            add_dataset_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="",
                data_field="Sales[X]",
            )

    def test_empty_data_field_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            add_dataset_field(
                path=str(rdl_path),
                dataset_name="MainDataset",
                field_name="X",
                data_field="   ",
            )

    def test_unknown_dataset_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            add_dataset_field(
                path=str(rdl_path),
                dataset_name="NoSuchDataset",
                field_name="X",
                data_field="A[X]",
            )

    def test_pre_encoded_data_field_no_double_encode(self, rdl_path):
        add_dataset_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Mixed",
            data_field="Sales[A &amp; B]",
        )
        assert b"&amp;amp;" not in (rdl_path).read_bytes()

    def test_round_trip_safe(self, rdl_path):
        add_dataset_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Total",
            data_field="Sales[Total]",
            type_name="System.Decimal",
        )
        RDLDocument.open(rdl_path).validate()


# ---- v0.3 Phase 6: refresh_dataset_fields --------------------------------


class TestRefreshDatasetFieldsSummarizeColumns:
    """SUMMARIZECOLUMNS / Table[Col] token extraction."""

    def test_adds_missing_fields_from_summarize(self, rdl_path):
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body=(
                "EVALUATE SUMMARIZECOLUMNS("
                "'Sales'[Region], 'Sales'[Customer], "
                "\"TotalAmount\", SUM('Sales'[Amount])"
                ")"
            ),
        )
        # Fixture's existing fields: ProductID, ProductName, Amount.
        # The new DAX produces Region, Customer, TotalAmount.
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        # Region + Customer are group-by columns; TotalAmount is the
        # SUMMARIZECOLUMNS "name", expr alias.
        # #37: added entries carry the qualified DataField.
        assert result["added"] == [
            {"name": "Region", "data_field": "Sales[Region]"},
            {"name": "Customer", "data_field": "Sales[Customer]"},
            {"name": "TotalAmount", "data_field": "[TotalAmount]"},
        ]
        # Existing fields not in the new DAX become orphans (document
        # order, keyed on DataField). Amount is only an *argument* of
        # SUM('Sales'[Amount]), not a result column, so it is an orphan
        # too (#24: the regex extractor used to count it as returned).
        assert result["orphans"] == [
            {"name": "ProductID", "data_field": "Sales[ProductID]"},
            {"name": "ProductName", "data_field": "Sales[ProductName]"},
            {"name": "Amount", "data_field": "Sales[Amount]"},
        ]
        assert result["unchanged"] == []

    def test_added_fields_have_data_field_text(self, rdl_path):
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE 'Sales'[Region]",
        )
        refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        ds = get_dataset(path=str(rdl_path), name="MainDataset")
        region = next((f for f in ds["fields"] if f["name"] == "Region"), None)
        assert region is not None
        # #37: the qualified binding, not the bare column name.
        assert region["data_field"] == "Sales[Region]"

    def test_no_changes_when_already_in_sync(self, rdl_path):
        # DAX returning exactly the fixture's existing fields. The
        # FILTER-only column ('Sales'[Date]) is not a result column and
        # must not be added (#24).
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body=(
                "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS("
                "'Sales'[ProductID], 'Sales'[ProductName], 'Sales'[Amount]), "
                "FILTER(VALUES('Sales'[Date]), 'Sales'[Date] >= @From))"
            ),
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == []
        assert result["orphans"] == []
        assert result["warnings"] == []
        assert sorted(result["unchanged"]) == ["Amount", "ProductID", "ProductName"]

    def test_duplicate_column_names_get_rb_suffix(self, rdl_path):
        # Two tables each return a Name column. Report Builder names
        # them Name / Name1, each bound to its own table (#37); never
        # two <Field Name="Name"> elements.
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE SUMMARIZECOLUMNS('C'[Name], 'P'[Name])",
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == [
            {"name": "Name", "data_field": "C[Name]"},
            {"name": "Name1", "data_field": "P[Name]"},
        ]
        names = [f["name"] for f in get_dataset(path=str(rdl_path), name="MainDataset")["fields"]]
        assert names.count("Name") == 1

    def test_comment_and_filter_columns_not_added(self, rdl_path):
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body=(
                "// SELECTCOLUMNS('Sales', \"Bogus\", 1)\n"
                "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], "
                "FILTER(VALUES('Sales'[Date]), 'Sales'[Date] >= @From)) -- 'Sales'[Old]"
            ),
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == []
        assert result["unchanged"] == ["ProductID"]


class TestRefreshDatasetFieldsSelectColumns:
    def test_extracts_quoted_aliases(self, rdl_path):
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body=(
                "EVALUATE SELECTCOLUMNS("
                "'Sales', "
                "\"OrderID\", 'Sales'[ProductID], "
                "\"Customer\", 'Sales'[ProductName], "
                "\"Total\", 'Sales'[Amount] * 1.1"
                ")"
            ),
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == [
            {"name": "OrderID", "data_field": "[OrderID]"},
            {"name": "Customer", "data_field": "[Customer]"},
            {"name": "Total", "data_field": "[Total]"},
        ]


class TestRefreshDatasetFieldsWarnings:
    def test_bare_evaluate_table_warns(self, rdl_path):
        # Fixture's default DAX is "EVALUATE 'Sales'" — no parens, so
        # bracket-token detection finds nothing.
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE 'Sales'",
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == []
        assert any("EVALUATE" in w for w in result["warnings"])

    def test_unparseable_shape_warns(self, rdl_path):
        # Empty-ish DAX that doesn't match any known pattern.
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="DEFINE",
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["warnings"]

    def test_selectcolumns_does_not_pull_source_columns(self, rdl_path):
        # Regression: a live MCP smoke-test on a real XMLA-backed report
        # had DAX shaped like:
        #   SELECTCOLUMNS('Sales', "Distance__Km_", 'Sales'[Distance (Km)],
        #                          "DDO", 'Sales'[DDO])
        # The refresher used to add BOTH the alias names ("Distance__Km_",
        # "DDO") AND the bracketed source-column names ("Distance (Km)") —
        # producing phantom fields on the real fixture. The aliases are
        # the result-set field names; the bracketed tokens are source
        # references and must not pollute <Fields>.
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body=(
                "EVALUATE SELECTCOLUMNS('Sales', "
                "\"Distance__Km_\", 'Sales'[Distance (Km)], "
                "\"DDO\", 'Sales'[DDO])"
            ),
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        added = [a["name"] for a in result["added"]]
        # Only the quoted aliases should be added.
        assert "Distance__Km_" in added
        assert "DDO" in added
        # Source columns must NOT appear.
        assert "Distance (Km)" not in added
        # The bracketed 'Sales'[DDO] form bare 'DDO' is the one we want
        # via alias, not via bracket extraction.
        # Sanity: no SELECTCOLUMNS run on this DAX should ever surface
        # parens/spaces (those are source-column hallmarks).
        for f in added:
            assert "(" not in f, f"phantom source-column field {f!r}"


class TestRefreshDatasetFieldsErrors:
    def test_unknown_dataset_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            refresh_dataset_fields(path=str(rdl_path), dataset_name="NoSuchDataset")


# ---- #37: match on DataField, emit Report Builder names ------------------


_RB_DAX = (
    "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS('Sales'[ProductID], "
    "'Sales'[Product Name], 'Sales'[Amount (USD)]), "
    "FILTER(VALUES('Sales'[Date]), 'Sales'[Date] >= @From))"
)


def _rb_shaped(rdl_path: Path, dax: str = _RB_DAX) -> Path:
    """Patch the fixture into Report Builder's own shape: sanitised Field
    Names bound to the real (non-identifier) column in <DataField>, and a
    DAX query that returns exactly those columns."""
    s = rdl_path.read_text(encoding="utf-8")
    s = s.replace('"ProductName"', '"Product_Name"').replace(
        "Sales[ProductName]", "Sales[Product Name]"
    )
    s = s.replace('"Amount"', '"Amount__USD_"').replace("Sales[Amount]", "Sales[Amount (USD)]")
    s = s.replace(
        "<CommandText>EVALUATE 'Sales'</CommandText>", f"<CommandText>{dax}</CommandText>"
    )
    rdl_path.write_text(s, encoding="utf-8")
    return rdl_path


def _field_pairs(rdl_path: Path) -> list[tuple]:
    return [
        (f["name"], f["data_field"])
        for f in get_dataset(path=str(rdl_path), name="MainDataset")["fields"]
    ]


class TestRefreshDatasetFieldsReportBuilderShape:
    def test_rb_shaped_fixture_is_a_byte_for_byte_noop(self, rdl_path):
        from pbirb_mcp.ops.lint import lint_report

        _rb_shaped(rdl_path)
        before = rdl_path.read_bytes()
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result == {
            "added": [],
            "orphans": [],
            "unchanged": ["ProductID", "Product_Name", "Amount__USD_"],
            "warnings": [],
            "changed": False,
        }
        assert rdl_path.read_bytes() == before
        # The lint rule agrees the dataset is in sync.
        lint = lint_report(str(rdl_path), rules=["dataset-fields-out-of-sync"])
        assert lint["issues"] == []

    def test_new_column_gets_rb_name_and_qualified_data_field(self, rdl_path):
        _rb_shaped(
            rdl_path,
            _RB_DAX.replace("'Sales'[ProductID], ", "'Sales'[ProductID], 'Sales'[Region (EU)], "),
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == [{"name": "Region__EU_", "data_field": "Sales[Region (EU)]"}]
        assert result["orphans"] == []
        assert ("Region__EU_", "Sales[Region (EU)]") in _field_pairs(rdl_path)
        text = rdl_path.read_text(encoding="utf-8")
        assert '<Field Name="Region__EU_">' in text
        assert "<DataField>Sales[Region (EU)]</DataField>" in text
        assert validate_report(str(rdl_path))["valid"]
        # A second refresh is a no-op.
        again = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert again["added"] == []

    def test_selectcolumns_alias_binds_as_bracketed_alias(self, rdl_path):
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body=(
                "EVALUATE SELECTCOLUMNS('Sales', \"ProductID\", 'Sales'[ProductID], "
                "\"Total\", 'Sales'[Amount] * 2)"
            ),
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == [
            {"name": "ProductID1", "data_field": "[ProductID]"},
            {"name": "Total", "data_field": "[Total]"},
        ]
        assert "<DataField>[Total]</DataField>" in rdl_path.read_text(encoding="utf-8")

    def test_name_collision_is_suffixed(self, rdl_path):
        # Field "Amount" exists, bound to Sales[Amount]; a different
        # table's Amount column must not reuse the name.
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body=(
                "EVALUATE SUMMARIZECOLUMNS('Sales'[Amount], 'Budget'[Amount], 'Plan'[Amount])"
            ),
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == [
            {"name": "Amount1", "data_field": "Budget[Amount]"},
            {"name": "Amount2", "data_field": "Plan[Amount]"},
        ]
        assert result["unchanged"] == ["Amount"]
        assert [o["name"] for o in result["orphans"]] == ["ProductID", "ProductName"]

    def test_quoted_and_case_variant_data_fields_match(self, rdl_path):
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body=(
                "EVALUATE SUMMARIZECOLUMNS('SALES'[productid], 'Sales'[ProductName], Sales[Amount])"
            ),
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == []
        assert result["orphans"] == []

    def test_legacy_bare_data_field_counts_as_bound(self, rdl_path):
        # Older versions wrote <DataField>Total Sales</DataField> for an
        # alias; that field must not be re-added as Total_Sales1.
        add_dataset_field(
            path=str(rdl_path),
            dataset_name="MainDataset",
            field_name="Total_Sales",
            data_field="Total Sales",
        )
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], \"Total Sales\", [M])",
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["added"] == []
        assert "Total_Sales" in result["unchanged"]

    def test_parser_warnings_refuse_to_add(self, rdl_path):
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body="EVALUATE CROSSJOIN(SUMMARIZECOLUMNS('Sales'[New]), MYSTERY('Sales'))",
        )
        result = refresh_dataset_fields(path=str(rdl_path), dataset_name="MainDataset")
        assert result["warnings"]
        assert result["added"] == []
        assert result["orphans"] == []
        names = [f["name"] for f in get_dataset(path=str(rdl_path), name="MainDataset")["fields"]]
        assert "New" not in names


class TestRbFieldName:
    @pytest.mark.parametrize(
        "column, expected",
        [
            ("Amount", "Amount"),
            ("Amount (USD)", "Amount__USD_"),
            ("Region (EU)", "Region__EU_"),
            ("Total Sales", "Total_Sales"),
            ("% Margin", "Field__Margin"),
            ("2024 Sales", "Field2024_Sales"),
            ("_D", "Field_D"),
            ("", "Field"),
        ],
    )
    def test_sanitises(self, column, expected):
        from pbirb_mcp.core.names import is_identifier
        from pbirb_mcp.ops.dataset import rb_field_name

        assert rb_field_name(column) == expected
        assert is_identifier(expected)

    def test_suffixes_until_unique(self):
        from pbirb_mcp.ops.dataset import rb_field_name

        assert rb_field_name("Name", {"Name"}) == "Name1"
        assert rb_field_name("Name", {"Name", "Name1"}) == "Name2"
        assert rb_field_name("Amount (USD)", {"Amount__USD_"}) == "Amount__USD_1"


# ---- v0.3 PBIDATASET @-prefix defence ------------------------------------


def _set_provider_pbidataset(rdl_path: Path):
    """Edit the fixture's <DataProvider> to PBIDATASET so the defence
    triggers. The fixture writes 'SQL' (AS-provider wire identifier)
    by default, which our detector recognises via the powerbi:// in
    ConnectString — but a real PBIDATASET-authored report uses
    'PBIDATASET' explicitly, so we cover both shapes in tests."""

    from pbirb_mcp.core.xpath import find_child as _fc

    doc = RDLDocument.open(rdl_path)
    ds = doc.root.find(f"{{{RDL_NS}}}DataSources/{{{RDL_NS}}}DataSource")
    cp = _fc(ds, "ConnectionProperties")
    provider = _fc(cp, "DataProvider")
    provider.text = "PBIDATASET"
    doc.save()


class TestAddQueryParameterPbiDatasetDefence:
    def test_strips_at_prefix_for_pbidataset_provider(self, rdl_path):
        _set_provider_pbidataset(rdl_path)
        result = add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        assert result["name"] == "DateFrom"  # stripped
        assert result["normalised"] is True
        assert "warning" in result
        assert "PBIDATASET" in result["warning"]
        # On disk, the QueryParameter Name attribute is bare.
        from pbirb_mcp.ops.reader import get_datasets

        ds = get_datasets(path=str(rdl_path))[0]
        names = [p["name"] for p in ds["query_parameters"]]
        assert names == ["DateFrom"]
        assert "@DateFrom" not in names

    def test_strips_at_prefix_for_legacy_powerbi_xmla(self, rdl_path):
        # Default fixture has DataProvider=SQL + powerbi:// connect
        # string — the legacy AS-provider shape our own writers emit.
        # The detector must recognise this as PBIDATASET-equivalent.
        result = add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        assert result["name"] == "DateFrom"
        assert result["normalised"] is True

    def test_keeps_at_prefix_when_force(self, rdl_path):
        _set_provider_pbidataset(rdl_path)
        result = add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@DateFrom",
            value_expression="=Parameters!DateFrom.Value",
            force_at_prefix=True,
        )
        assert result["name"] == "@DateFrom"
        assert result["normalised"] is False
        assert "warning" not in result

    def test_passes_through_unchanged_for_non_pbi_dataset(self, rdl_path):
        # Switch the provider to a non-PBI shape (no powerbi:// in the
        # connect string).

        from pbirb_mcp.core.xpath import find_child as _fc

        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(f"{{{RDL_NS}}}DataSources/{{{RDL_NS}}}DataSource")
        cp = _fc(ds, "ConnectionProperties")
        cs = _fc(cp, "ConnectString")
        cs.text = "Data Source=localhost;Initial Catalog=Sales"
        doc.save()

        # On a non-PBIDATASET dataset, @ is preserved (SQL convention).
        result = add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        assert result["name"] == "@DateFrom"
        assert result["normalised"] is False

    def test_no_op_when_name_has_no_at_prefix(self, rdl_path):
        _set_provider_pbidataset(rdl_path)
        result = add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        assert result["name"] == "DateFrom"
        assert result["normalised"] is False

    def test_pathological_at_only_name_not_normalised(self, rdl_path):
        # Name is just "@" — stripping would leave empty. Don't strip.
        _set_provider_pbidataset(rdl_path)
        result = add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@",
            value_expression="=1",
        )
        assert result["name"] == "@"
        assert result["normalised"] is False


class TestUpdateQueryParameterPbiDatasetDefence:
    def test_lookup_via_at_prefix_strips(self, rdl_path):
        _set_provider_pbidataset(rdl_path)
        # Add as bare 'DateFrom' (the auto-strip path).
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        # Now update via "@DateFrom" — the strip kicks in, looks up
        # 'DateFrom', updates.
        result = update_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@DateFrom",
            value_expression="=Today()",
        )
        assert result["name"] == "DateFrom"
        assert result["normalised"] is True

    def test_legacy_at_prefix_addressable(self, rdl_path):
        # Manually inject a legacy '@DateFrom' parameter in a PBIDATASET
        # context — simulate a v0.2-authored file.
        from lxml import etree as _etree

        from pbirb_mcp.core.xpath import find_child as _fc
        from pbirb_mcp.core.xpath import q as _q

        _set_provider_pbidataset(rdl_path)
        doc = RDLDocument.open(rdl_path)
        ds = next(
            d for d in doc.root.iter(f"{{{RDL_NS}}}DataSet") if d.get("Name") == "MainDataset"
        )
        query = _fc(ds, "Query")
        qp_root = _etree.SubElement(query, _q("QueryParameters"))
        qp = _etree.SubElement(qp_root, _q("QueryParameter"), Name="@DateFrom")
        _etree.SubElement(qp, _q("Value")).text = "=Parameters!DateFrom.Value"
        doc.save()

        # Update via '@DateFrom' — the normalised lookup misses, so the
        # fallback finds the legacy '@DateFrom'.
        result = update_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@DateFrom",
            value_expression="=Today()",
        )
        # Falls back to raw lookup; no warning emitted.
        assert result["name"] == "@DateFrom"
        assert result["normalised"] is False
        # Verify the rewrite landed on the legacy entry.
        from pbirb_mcp.ops.reader import get_datasets

        ds_view = get_datasets(path=str(rdl_path))[0]
        legacy = next(p for p in ds_view["query_parameters"] if p["name"] == "@DateFrom")
        assert legacy["value"] == "=Today()"


# ---- #94: filter arity, FilterValue DataType, field_format warning ---------


def _typed_filter_fixture(rdl_path: Path) -> None:
    from pbirb_mcp.ops.parameters import add_parameter

    add_dataset_field(str(rdl_path), "MainDataset", "Qty", "Sales[Qty]", "System.Int64")
    add_dataset_field(
        str(rdl_path), "MainDataset", "OrderDate", "Sales[OrderDate]", "System.DateTime"
    )
    add_parameter(str(rdl_path), "Month", "String", prompt="Month")


class TestDatasetFilterArity:
    @pytest.mark.parametrize(
        "operator,values",
        [
            ("Between", ["10"]),
            ("Between", ["1", "2", "3"]),
            ("Equal", ["a", "b", "c"]),
            ("TopN", ["ten"]),
            ("TopN", ["5", "6"]),
            ("TopPercent", ["half"]),
            ("NotEqual", ["1", "2"]),
        ],
    )
    def test_wrong_arity_or_count_refused_without_writing(self, rdl_path, operator, values):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError):
            add_dataset_filter(
                str(rdl_path), "MainDataset", "=Fields!ProductID.Value", operator, values
            )
        assert rdl_path.read_bytes() == before

    @pytest.mark.parametrize(
        "operator,values",
        [
            ("Between", ["1", "10"]),
            ("In", ["a"]),
            ("In", ["a", "b", "c"]),
            ("TopN", ["5"]),
            ("TopN", ["=Parameters!DateFrom.Value"]),
            ("TopPercent", ["12.5"]),
        ],
    )
    def test_valid_arity_accepted(self, rdl_path, operator, values):
        add_dataset_filter(
            str(rdl_path), "MainDataset", "=Fields!ProductID.Value", operator, values
        )
        assert list_dataset_filters(str(rdl_path), "MainDataset")[0]["values"] == values
        assert validate_report(str(rdl_path))["valid"]

    def test_wrong_case_operator_gets_a_hint(self, rdl_path):
        with pytest.raises(ValueError, match="did you mean 'Equal'"):
            add_dataset_filter(str(rdl_path), "MainDataset", "=1", "equal", ["1"])

    def test_schema_enumerates_operators_and_data_types(self):
        from pbirb_mcp.ops.filter_types import FILTER_OPERATORS

        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in listing["result"]["tools"]}
        for name in ("add_dataset_filter", "add_tablix_filter"):
            props = tools[name]["inputSchema"]["properties"]
            assert props["operator"]["enum"] == list(FILTER_OPERATORS)
            assert "Integer" in props["value_data_type"]["enum"]


class TestDatasetFilterValueDataType:
    def test_writes_data_type_attribute_and_reads_it_back(self, rdl_path):
        _typed_filter_fixture(rdl_path)
        result = add_dataset_filter(
            str(rdl_path),
            "MainDataset",
            "=Fields!Qty.Value",
            "GreaterThan",
            ["50"],
            value_data_type="Integer",
        )
        assert result["warnings"] == []
        assert result["data_type"] == "Integer"
        assert '<FilterValue DataType="Integer">50</FilterValue>' in rdl_path.read_text("utf-8")
        assert validate_report(str(rdl_path))["valid"]
        assert list_dataset_filters(str(rdl_path), "MainDataset")[0]["data_type"] == "Integer"
        assert get_dataset(str(rdl_path), "MainDataset")["filters"][0]["data_type"] == "Integer"

    def test_data_type_unset_reads_none(self, rdl_path):
        add_dataset_filter(
            str(rdl_path), "MainDataset", "=Fields!ProductName.Value", "Equal", ["x"]
        )
        assert list_dataset_filters(str(rdl_path), "MainDataset")[0]["data_type"] is None
        assert "DataType=" not in rdl_path.read_text("utf-8")

    def test_literal_must_match_data_type(self, rdl_path):
        with pytest.raises(ValueError, match="not a valid Integer"):
            add_dataset_filter(
                str(rdl_path),
                "MainDataset",
                "=Fields!ProductID.Value",
                "Equal",
                ["fifty"],
                value_data_type="Integer",
            )

    def test_int_field_vs_string_literal_warns(self, rdl_path):
        _typed_filter_fixture(rdl_path)
        result = add_dataset_filter(
            str(rdl_path), "MainDataset", "=Fields!Qty.Value", "GreaterThan", ["50"]
        )
        assert len(result["warnings"]) == 1
        assert "value_data_type='Integer'" in result["warnings"][0]

    def test_date_field_vs_literal_suggests_datetime(self, rdl_path):
        _typed_filter_fixture(rdl_path)
        result = add_dataset_filter(
            str(rdl_path), "MainDataset", "=Fields!OrderDate.Value", "GreaterThan", ["2024-01-01"]
        )
        assert "value_data_type='DateTime'" in result["warnings"][0]

    def test_expression_value_string_field_and_topn_do_not_warn(self, rdl_path):
        _typed_filter_fixture(rdl_path)
        r1 = add_dataset_filter(
            str(rdl_path), "MainDataset", "=Fields!Qty.Value", "GreaterThan", ["=50"]
        )
        r2 = add_dataset_filter(
            str(rdl_path), "MainDataset", "=Fields!ProductName.Value", "Equal", ["x"]
        )
        r3 = add_dataset_filter(str(rdl_path), "MainDataset", "=Fields!Qty.Value", "TopN", ["5"])
        assert r1["warnings"] == r2["warnings"] == r3["warnings"] == []

    def test_field_format_clears_the_mismatch_warning(self, rdl_path):
        _typed_filter_fixture(rdl_path)
        result = add_dataset_filter(
            str(rdl_path),
            "MainDataset",
            "=Fields!OrderDate.Value",
            "Equal",
            ["=Parameters!Month.Value"],
            field_format="MMM yyyy",
        )
        assert result["expression"] == '=Format(Fields!OrderDate.Value, "MMM yyyy")'
        assert result["warnings"] == []

    def test_unwrapped_mismatch_still_warns(self, rdl_path):
        _typed_filter_fixture(rdl_path)
        result = add_dataset_filter(
            str(rdl_path),
            "MainDataset",
            "=Fields!OrderDate.Value",
            "Equal",
            ["=Parameters!Month.Value"],
        )
        assert len(result["warnings"]) == 1
        assert "filter type mismatch" in result["warnings"][0]

    @pytest.mark.parametrize(
        "expression",
        ["=CStr(Fields!OrderDate.Value)", "=Fields!OrderDate.Value.ToString()"],
    )
    def test_other_string_coercions_skip_the_check(self, rdl_path, expression):
        _typed_filter_fixture(rdl_path)
        result = add_dataset_filter(
            str(rdl_path), "MainDataset", expression, "Equal", ["=Parameters!Month.Value"]
        )
        assert result["warnings"] == []


# ---- #95: query parameters vs the DAX -------------------------------------

_DAX_YEAR_MONTH = (
    "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], 'Sales'[ProductName], "
    "FILTER('Sales', 'Sales'[Year] = @Year && 'Sales'[Month] = @MonthNum), "
    '"Amount", SUM(Sales[Amount]))'
)


def _bound_dax(rdl_path: Path) -> None:
    from pbirb_mcp.ops.parameters import add_parameter

    update_dataset_query(str(rdl_path), "MainDataset", _DAX_YEAR_MONTH)
    add_parameter(str(rdl_path), "Year", "Integer", prompt="Year")
    add_parameter(str(rdl_path), "MonthNum", "Integer", prompt="Month")
    add_query_parameter(str(rdl_path), "MainDataset", "Year", "=Parameters!Year.Value")
    add_query_parameter(str(rdl_path), "MainDataset", "MonthNum", "=Parameters!MonthNum.Value")


class TestQueryParameterCrossChecks:
    def test_remove_accepts_the_at_form_add_stripped(self, rdl_path):
        _bound_dax(rdl_path)
        added = add_query_parameter(str(rdl_path), "MainDataset", "@Extra", "=1")
        assert added["name"] == "Extra"
        result = remove_query_parameter(str(rdl_path), "MainDataset", "@Extra")
        assert result["removed"] == "Extra"
        names = [p["name"] for p in get_dataset(str(rdl_path), "MainDataset")["query_parameters"]]
        assert names == ["Year", "MonthNum"]

    def test_remove_falls_back_to_a_legacy_at_name(self, rdl_path):
        _bound_dax(rdl_path)
        add_query_parameter(str(rdl_path), "MainDataset", "@Legacy", "=1", force_at_prefix=True)
        assert remove_query_parameter(str(rdl_path), "MainDataset", "@Legacy")["removed"] == (
            "@Legacy"
        )

    def test_clean_binding_has_no_warnings(self, rdl_path):
        _bound_dax(rdl_path)
        result = update_query_parameter(
            str(rdl_path), "MainDataset", "Year", "=Parameters!Year.Value"
        )
        assert result["warnings"] == []
        assert result["changed"] is False

    def test_add_warns_about_unused_and_undeclared(self, rdl_path):
        _bound_dax(rdl_path)
        result = add_query_parameter(
            str(rdl_path), "MainDataset", "Ghost", "=Parameters!NoSuchParam.Value"
        )
        joined = " ".join(result["warnings"])
        assert "query-parameter-mismatch" in joined and "@Ghost" in joined
        assert "undefined-parameter" in joined and "NoSuchParam" in joined

    def test_remove_warns_when_the_dax_still_uses_it(self, rdl_path):
        _bound_dax(rdl_path)
        result = remove_query_parameter(str(rdl_path), "MainDataset", "Year")
        assert len(result["warnings"]) == 1
        assert "@Year" in result["warnings"][0]

    def test_repro_lint_after_removals_and_multi_value_toggle(self, rdl_path):
        from pbirb_mcp.ops.parameters import update_parameter_advanced

        _bound_dax(rdl_path)
        base = {(i["rule"], i["location"]) for i in lint_report(str(rdl_path))["issues"]}

        def new_issues():
            return [
                i
                for i in lint_report(str(rdl_path))["issues"]
                if (i["rule"], i["location"]) not in base
            ]

        add_query_parameter(str(rdl_path), "MainDataset", "@Extra", "=Parameters!Year.Value")
        remove_query_parameter(str(rdl_path), "MainDataset", "@Extra")
        remove_query_parameter(str(rdl_path), "MainDataset", "Year")
        add_query_parameter(str(rdl_path), "MainDataset", "Ghost", "=Parameters!NoSuchParam.Value")
        messages = [(i["rule"], i["message"]) for i in new_issues()]
        assert any(r == "query-parameter-mismatch" and "@Year" in m for r, m in messages)
        assert any(r == "query-parameter-mismatch" and "'Ghost'" in m for r, m in messages)
        assert any(r == "undefined-parameter" and "NoSuchParam" in m for r, m in messages)

        update_parameter_advanced(str(rdl_path), "MonthNum", multi_value=True)
        result = update_query_parameter(
            str(rdl_path), "MainDataset", "MonthNum", "=Parameters!MonthNum.Value"
        )
        assert any("multi-value-query-binding" in w for w in result["warnings"])
        add_dataset_filter(
            str(rdl_path),
            "MainDataset",
            "=Globals!ReportName",
            "Equal",
            ["=Parameters!MonthNum.Value"],
        )
        rules = [i["rule"] for i in new_issues()]
        assert "multi-value-query-binding" in rules
        assert "multi-value-eq" in rules


# ---- #98: dataset-authoring tools ------------------------------------------


def _lint_keys(path) -> set:
    return {(i["rule"], i["location"]) for i in lint_report(str(path))["issues"]}


def _assert_noop_repeat(path: Path, fn, *args, **kwargs) -> None:
    before = path.read_bytes()
    result = fn(str(path), *args, **kwargs)
    assert result["changed"] is False
    assert path.read_bytes() == before


class TestSetDatasetDataSource:
    def test_rebinds_and_validates(self, multi_path):
        from pbirb_mcp.ops.dataset import set_dataset_data_source

        result = set_dataset_data_source(str(multi_path), "MainDataset", "LookupDataset")
        assert result["changed"] is True
        assert result["previous"] == "PowerBIDataset"
        assert get_dataset(str(multi_path), "MainDataset")["data_source"] == "LookupDataset"
        assert validate_report(str(multi_path))["valid"]
        _assert_noop_repeat(multi_path, set_dataset_data_source, "MainDataset", "LookupDataset")

    def test_unknown_source_refused(self, multi_path):
        from pbirb_mcp.ops.dataset import set_dataset_data_source

        before = multi_path.read_bytes()
        with pytest.raises(ElementNotFoundError, match="LookupDataset"):
            set_dataset_data_source(str(multi_path), "MainDataset", "Nope")
        assert multi_path.read_bytes() == before

    def test_provider_family_change_warns(self, multi_path):
        from pbirb_mcp.ops.dataset import set_dataset_data_source

        doc = RDLDocument.open(multi_path)
        for ds in doc.root.iter(f"{{{RDL_NS}}}DataSource"):
            if ds.get("Name") == "LookupDataset":
                ds.find(
                    f".//{{{RDL_NS}}}ConnectString"
                ).text = "Data Source=sql01;Initial Catalog=x"
        doc.save()
        result = set_dataset_data_source(str(multi_path), "MainDataset", "LookupDataset")
        assert len(result["warnings"]) == 1
        assert "Power BI" in result["warnings"][0] and "SQL" in result["warnings"][0]


class TestAddDataset:
    def test_creates_dataset_with_seeded_fields(self, rdl_path):
        from pbirb_mcp.ops.dataset import add_dataset

        result = add_dataset(
            str(rdl_path),
            "Regions",
            "PowerBIDataset",
            "EVALUATE SUMMARIZECOLUMNS('Geo'[Region (EU)], \"Total\", [Sales])",
            fields=["'Geo'[Region (EU)]", "[Total]"],
        )
        assert result["changed"] is True
        assert result["fields"] == [
            {"name": "Region__EU_", "data_field": "'Geo'[Region (EU)]"},
            {"name": "Total", "data_field": "[Total]"},
        ]
        ds = get_dataset(str(rdl_path), "Regions")
        assert ds["data_source"] == "PowerBIDataset"
        assert [f["name"] for f in ds["fields"]] == ["Region__EU_", "Total"]
        assert validate_report(str(rdl_path))["valid"]

    def test_without_fields_and_unbound_param_warning(self, rdl_path):
        from pbirb_mcp.ops.dataset import add_dataset

        result = add_dataset(
            str(rdl_path), "Years", "PowerBIDataset", "EVALUATE FILTER('D', 'D'[y] = @Year)"
        )
        assert result["fields"] == []
        assert any("@Year" in w for w in result["warnings"])
        assert validate_report(str(rdl_path))["valid"]

    @pytest.mark.parametrize(
        "name,source,text,match",
        [
            ("MainDataset", "PowerBIDataset", "EVALUATE 'T'", "already exists"),
            ("MainTable", "PowerBIDataset", "EVALUATE 'T'", "report item or group"),
            ("Bad Name", "PowerBIDataset", "EVALUATE 'T'", "invalid dataset name"),
            ("New", "NoSource", "EVALUATE 'T'", "not found"),
            ("New", "PowerBIDataset", "  ", "non-empty"),
        ],
    )
    def test_refusals_leave_the_file_alone(self, rdl_path, name, source, text, match):
        from pbirb_mcp.ops.dataset import add_dataset

        before = rdl_path.read_bytes()
        with pytest.raises((ValueError, ElementNotFoundError), match=match):
            add_dataset(str(rdl_path), name, source, text)
        assert rdl_path.read_bytes() == before

    def test_repeat_refuses(self, rdl_path):
        from pbirb_mcp.ops.dataset import add_dataset

        add_dataset(str(rdl_path), "L", "PowerBIDataset", "EVALUATE 'L'")
        with pytest.raises(ValueError, match="already exists"):
            add_dataset(str(rdl_path), "L", "PowerBIDataset", "EVALUATE 'L'")


class TestRemoveDataset:
    def test_removes_an_unused_dataset(self, multi_path):
        from pbirb_mcp.ops.dataset import remove_dataset

        result = remove_dataset(str(multi_path), "LookupSet")
        assert result["changed"] is True
        assert "LookupSet" not in multi_path.read_text("utf-8")
        assert validate_report(str(multi_path))["valid"]
        with pytest.raises(ElementNotFoundError):
            remove_dataset(str(multi_path), "LookupSet")

    def test_refuses_while_a_region_uses_it(self, multi_path):
        from pbirb_mcp.ops.dataset import remove_dataset

        before = multi_path.read_bytes()
        with pytest.raises(ValueError, match="Tablix"):
            remove_dataset(str(multi_path), "MainDataset")
        assert multi_path.read_bytes() == before

    def test_refuses_while_a_parameter_or_scope_string_uses_it(self, multi_path):
        from pbirb_mcp.ops.dataset import remove_dataset
        from pbirb_mcp.ops.parameters import add_parameter

        add_parameter(str(multi_path), "Key", "String", prompt="Key")
        set_parameter_available_values(
            str(multi_path),
            "Key",
            source="query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
        )
        with pytest.raises(ValueError, match="DataSetReference"):
            remove_dataset(str(multi_path), "LookupSet")
        set_parameter_available_values(str(multi_path), "Key", source="static", static_values=[])
        set_textbox_value(str(multi_path), "HeaderAmount", '=CountRows("LookupSet")')
        with pytest.raises(ValueError, match="HeaderAmount"):
            remove_dataset(str(multi_path), "LookupSet")
        assert remove_dataset(str(multi_path), "LookupSet", force=True)["references"] == 1

    def test_last_dataset_drops_the_block(self, rdl_path):
        from pbirb_mcp.ops.dataset import remove_dataset

        remove_dataset(str(rdl_path), "MainDataset", force=True)
        assert "<DataSets" not in rdl_path.read_text("utf-8")


class TestRenameDataset:
    def test_multi_datasource_fixture_main_to_sales(self, multi_path):
        from pbirb_mcp.ops.dataset import rename_dataset

        base = _lint_keys(multi_path)
        result = rename_dataset(str(multi_path), "MainDataset", "Sales")
        assert result["changed"] is True
        assert "MainDataset" not in multi_path.read_text("utf-8")
        assert validate_report(str(multi_path))["valid"]
        assert _lint_keys(multi_path) == base
        with pytest.raises(ElementNotFoundError):
            rename_dataset(str(multi_path), "MainDataset", "Sales")

    def test_rewrites_scope_strings_and_dataset_references(self, multi_path):
        from pbirb_mcp.ops.dataset import rename_dataset
        from pbirb_mcp.ops.parameters import add_parameter

        add_parameter(str(multi_path), "Key", "String", prompt="Key")
        set_parameter_available_values(
            str(multi_path),
            "Key",
            source="query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
        )
        set_textbox_value(
            str(multi_path),
            "HeaderAmount",
            '=CountRows("LookupSet") & " LookupSet " & Lookup(1, 1, 1, "LookupSet")',
        )
        result = rename_dataset(str(multi_path), "LookupSet", "Keys")
        assert result["references_rewritten"] == 2
        text = multi_path.read_text("utf-8")
        assert 'CountRows("Keys")' in text and 'Lookup(1, 1, 1, "Keys")' in text
        assert '" LookupSet "' in text  # a plain string literal is left alone
        assert "<DataSetName>Keys</DataSetName>" in text
        assert validate_report(str(multi_path))["valid"]

    def test_refusals(self, multi_path):
        from pbirb_mcp.ops.dataset import rename_dataset

        with pytest.raises(ValueError, match="already exists"):
            rename_dataset(str(multi_path), "MainDataset", "LookupSet")
        with pytest.raises(ValueError, match="identical"):
            rename_dataset(str(multi_path), "MainDataset", "MainDataset")
        with pytest.raises(ValueError, match="report item or group"):
            rename_dataset(str(multi_path), "MainDataset", "MainTable")


class TestRenameDatasetField:
    def test_rewrites_scope_aware_references(self, multi_path):
        from pbirb_mcp.ops.dataset import rename_dataset_field

        add_calculated_field(str(multi_path), "MainDataset", "Doubled", "=Fields!Amount.Value * 2")
        set_textbox_value(
            str(multi_path), "HeaderAmount", '=Sum(Fields!Amount.Value, "MainDataset") & "Amount"'
        )
        base = _lint_keys(multi_path)
        result = rename_dataset_field(str(multi_path), "MainDataset", "Amount", "Revenue")
        assert result["changed"] is True
        assert result["references_rewritten"] == 3  # cell, calculated field, header
        text = multi_path.read_text("utf-8")
        assert "Fields!Amount" not in text
        assert '<Field Name="Revenue">' in text
        assert "<DataField>Sales[Amount]</DataField>" in text  # binding unchanged
        assert '"MainDataset") &amp; "Amount"' in text  # string literal untouched
        assert validate_report(str(multi_path))["valid"]
        assert _lint_keys(multi_path) == base
        with pytest.raises(ElementNotFoundError):
            rename_dataset_field(str(multi_path), "MainDataset", "Amount", "Revenue")

    def test_other_datasets_field_is_untouched(self, multi_path):
        from pbirb_mcp.ops.dataset import rename_dataset_field

        add_calculated_field(str(multi_path), "LookupSet", "Amount", "=1")
        add_calculated_field(str(multi_path), "LookupSet", "UsesIt", "=Fields!Amount.Value")
        rename_dataset_field(str(multi_path), "MainDataset", "Amount", "Revenue")
        lookup = get_dataset(str(multi_path), "LookupSet")
        assert {f["name"]: f["value"] for f in lookup["fields"]}["UsesIt"] == (
            "=Fields!Amount.Value"
        )

    def test_rewrites_parameter_value_field(self, multi_path):
        from pbirb_mcp.ops.dataset import rename_dataset_field
        from pbirb_mcp.ops.parameters import add_parameter

        add_parameter(str(multi_path), "Key", "String", prompt="Key")
        set_parameter_available_values(
            str(multi_path),
            "Key",
            source="query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
            query_label_field="LookupKey",
        )
        result = rename_dataset_field(str(multi_path), "LookupSet", "LookupKey", "KeyCode")
        assert result["references_rewritten"] == 2
        text = multi_path.read_text("utf-8")
        assert "<ValueField>KeyCode</ValueField>" in text
        assert "<LabelField>KeyCode</LabelField>" in text

    def test_refusals(self, multi_path):
        from pbirb_mcp.ops.dataset import rename_dataset_field

        before = multi_path.read_bytes()
        with pytest.raises(ValueError, match="already exists"):
            rename_dataset_field(str(multi_path), "MainDataset", "Amount", "ProductID")
        with pytest.raises(ElementNotFoundError):
            rename_dataset_field(str(multi_path), "MainDataset", "Nope", "X")
        with pytest.raises(ValueError):
            rename_dataset_field(str(multi_path), "MainDataset", "Amount", "1bad")
        assert multi_path.read_bytes() == before


class TestSetFieldType:
    def test_sets_normalises_and_is_idempotent(self, rdl_path):
        from pbirb_mcp.ops.dataset import set_field_type

        result = set_field_type(str(rdl_path), "MainDataset", "Amount", "double")
        assert result == {
            "dataset": "MainDataset",
            "field": "Amount",
            "type_name": "System.Double",
            "changed": True,
        }
        fields = {f["name"]: f for f in get_dataset(str(rdl_path), "MainDataset")["fields"]}
        assert fields["Amount"]["type_name"] == "System.Double"
        assert validate_report(str(rdl_path))["valid"]
        _assert_noop_repeat(rdl_path, set_field_type, "MainDataset", "Amount", "System.Double")

    def test_none_removes_and_adds_when_missing(self, rdl_path):
        from pbirb_mcp.ops.dataset import set_field_type

        assert set_field_type(str(rdl_path), "MainDataset", "Amount", None)["changed"] is True
        _assert_noop_repeat(rdl_path, set_field_type, "MainDataset", "Amount", None)
        assert set_field_type(str(rdl_path), "MainDataset", "Amount", "Int64")["changed"] is True
        assert "<rd:TypeName>System.Int64</rd:TypeName>" in rdl_path.read_text("utf-8")

    def test_unknown_type_refused(self, rdl_path):
        from pbirb_mcp.ops.dataset import set_field_type

        with pytest.raises(ValueError, match="unknown .NET type"):
            set_field_type(str(rdl_path), "MainDataset", "Amount", "System.Money")
        with pytest.raises(ElementNotFoundError):
            set_field_type(str(rdl_path), "MainDataset", "Nope", "System.String")


class TestSetDatasetOptions:
    def test_sets_options_validates_and_is_idempotent(self, rdl_path):
        from pbirb_mcp.ops.dataset import set_dataset_options

        result = set_dataset_options(
            str(rdl_path),
            "MainDataset",
            timeout=120,
            case_sensitivity="True",
            collation="Latin1_General",
            accent_sensitivity="False",
            kanatype_sensitivity="Auto",
            width_sensitivity="False",
        )
        assert result["changed"] is True
        assert result["options"] == {
            "timeout": 120,
            "case_sensitivity": "True",
            "collation": "Latin1_General",
            "accent_sensitivity": "False",
            "kanatype_sensitivity": "Auto",
            "width_sensitivity": "False",
        }
        assert "<Timeout>120</Timeout>" in rdl_path.read_text("utf-8")
        assert validate_report(str(rdl_path))["valid"]
        _assert_noop_repeat(
            rdl_path, set_dataset_options, "MainDataset", timeout=120, collation="Latin1_General"
        )

    def test_empty_collation_removes_it(self, rdl_path):
        from pbirb_mcp.ops.dataset import set_dataset_options

        set_dataset_options(str(rdl_path), "MainDataset", collation="Latin1_General")
        result = set_dataset_options(str(rdl_path), "MainDataset", collation="")
        assert result["changed"] is True and result["options"]["collation"] is None

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            ({}, "at least one"),
            ({"timeout": -1}, ">= 0"),
            ({"case_sensitivity": "yes"}, "CaseSensitivity"),
        ],
    )
    def test_refusals(self, rdl_path, kwargs, match):
        from pbirb_mcp.ops.dataset import set_dataset_options

        with pytest.raises(ValueError, match=match):
            set_dataset_options(str(rdl_path), "MainDataset", **kwargs)


class TestDatasetAuthoringRegistration:
    NAMES = {
        "set_dataset_data_source",
        "add_dataset",
        "remove_dataset",
        "rename_dataset",
        "rename_dataset_field",
        "set_field_type",
        "set_dataset_options",
    }

    def test_registered_in_the_data_toolset(self):
        from pbirb_mcp.core.toolsets import TOOLSETS

        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in listing["result"]["tools"]}
        assert names >= self.NAMES
        assert TOOLSETS["data"] >= self.NAMES

    def test_set_dataset_options_via_jsonrpc(self, rdl_path):
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "set_dataset_options",
                    "arguments": {
                        "path": str(rdl_path),
                        "dataset_name": "MainDataset",
                        "timeout": 5,
                    },
                },
            }
        )
        assert resp["result"].get("isError") is not True
        assert "<Timeout>5</Timeout>" in rdl_path.read_text("utf-8")
