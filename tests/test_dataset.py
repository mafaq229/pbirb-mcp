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
from pbirb_mcp.ops.reader import get_datasets
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
