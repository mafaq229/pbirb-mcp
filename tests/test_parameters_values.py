"""Tests for parameter available-values and default-values tools."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.parameters import (
    set_parameter_available_values,
    set_parameter_default_values,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _param(rdl_path: Path, name: str):
    doc = RDLDocument.open(rdl_path)
    return doc.root.find(f".//{{{RDL_NS}}}ReportParameter[@Name='{name}']")


# ---- set_parameter_available_values: static ------------------------------


class TestStaticAvailableValues:
    def test_writes_static_parameter_values(self, rdl_path):
        set_parameter_available_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["2026-01-01", "2026-04-01", "2026-07-01"],
        )
        p = _param(rdl_path, "DateFrom")
        pvs = p.findall(
            f"{q('ValidValues')}/{q('ParameterValues')}/{q('ParameterValue')}"
        )
        assert [find_child(pv, "Value").text for pv in pvs] == [
            "2026-01-01", "2026-04-01", "2026-07-01"
        ]

    def test_string_entries_use_same_value_and_label(self, rdl_path):
        set_parameter_available_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["2026-01-01"],
        )
        pv = _param(rdl_path, "DateFrom").find(
            f"{q('ValidValues')}/{q('ParameterValues')}/{q('ParameterValue')}"
        )
        assert find_child(pv, "Value").text == "2026-01-01"
        assert find_child(pv, "Label").text == "2026-01-01"

    def test_dict_entries_carry_distinct_label(self, rdl_path):
        set_parameter_available_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=[
                {"value": "2026-01-01", "label": "Q1 2026"},
                {"value": "2026-04-01", "label": "Q2 2026"},
            ],
        )
        pvs = _param(rdl_path, "DateFrom").findall(
            f"{q('ValidValues')}/{q('ParameterValues')}/{q('ParameterValue')}"
        )
        labels = [find_child(pv, "Label").text for pv in pvs]
        assert labels == ["Q1 2026", "Q2 2026"]


# ---- set_parameter_available_values: query -------------------------------


class TestQueryAvailableValues:
    def test_writes_dataset_reference(self, rdl_path):
        set_parameter_available_values(
            path=str(rdl_path),
            name="DateFrom",
            source="query",
            query_dataset="MainDataset",
            query_value_field="ProductID",
            query_label_field="ProductName",
        )
        ref = _param(rdl_path, "DateFrom").find(
            f"{q('ValidValues')}/{q('DataSetReference')}"
        )
        assert ref is not None
        assert find_child(ref, "DataSetName").text == "MainDataset"
        assert find_child(ref, "ValueField").text == "ProductID"
        assert find_child(ref, "LabelField").text == "ProductName"

    def test_label_field_optional(self, rdl_path):
        set_parameter_available_values(
            path=str(rdl_path),
            name="DateFrom",
            source="query",
            query_dataset="MainDataset",
            query_value_field="ProductID",
        )
        ref = _param(rdl_path, "DateFrom").find(
            f"{q('ValidValues')}/{q('DataSetReference')}"
        )
        assert find_child(ref, "LabelField") is None


# ---- replace semantics ----------------------------------------------------


class TestReplace:
    def test_static_then_query_replaces_block(self, rdl_path):
        set_parameter_available_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["X"],
        )
        set_parameter_available_values(
            path=str(rdl_path),
            name="DateFrom",
            source="query",
            query_dataset="MainDataset",
            query_value_field="ProductID",
        )
        p = _param(rdl_path, "DateFrom")
        # Exactly one ValidValues block, holding DataSetReference (not static).
        vvs = p.findall(q("ValidValues"))
        assert len(vvs) == 1
        assert find_child(vvs[0], "ParameterValues") is None
        assert find_child(vvs[0], "DataSetReference") is not None


# ---- error paths ----------------------------------------------------------


class TestErrors:
    def test_unknown_parameter_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_parameter_available_values(
                path=str(rdl_path),
                name="Ghost",
                source="static",
                static_values=["X"],
            )

    def test_invalid_source_raises(self, rdl_path):
        with pytest.raises(ValueError):
            set_parameter_available_values(
                path=str(rdl_path),
                name="DateFrom",
                source="other",
                static_values=["X"],
            )

    def test_static_requires_values(self, rdl_path):
        with pytest.raises(ValueError):
            set_parameter_available_values(
                path=str(rdl_path),
                name="DateFrom",
                source="static",
            )

    def test_query_requires_dataset_and_value_field(self, rdl_path):
        with pytest.raises(ValueError):
            set_parameter_available_values(
                path=str(rdl_path),
                name="DateFrom",
                source="query",
                query_dataset="MainDataset",
            )
        with pytest.raises(ValueError):
            set_parameter_available_values(
                path=str(rdl_path),
                name="DateFrom",
                source="query",
                query_value_field="X",
            )

    def test_query_dataset_must_exist(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_parameter_available_values(
                path=str(rdl_path),
                name="DateFrom",
                source="query",
                query_dataset="NoSuchDataset",
                query_value_field="X",
            )


# ---- set_parameter_default_values ----------------------------------------


class TestStaticDefaultValues:
    def test_writes_static_default_value_expressions(self, rdl_path):
        set_parameter_default_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["=Today()"],
        )
        p = _param(rdl_path, "DateFrom")
        values = p.findall(
            f"{q('DefaultValue')}/{q('Values')}/{q('Value')}"
        )
        assert [v.text for v in values] == ["=Today()"]

    def test_multiple_static_default_values(self, rdl_path):
        set_parameter_default_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["A", "B"],
        )
        values = _param(rdl_path, "DateFrom").findall(
            f"{q('DefaultValue')}/{q('Values')}/{q('Value')}"
        )
        assert [v.text for v in values] == ["A", "B"]


class TestQueryDefaultValues:
    def test_writes_dataset_reference_without_label_field(self, rdl_path):
        set_parameter_default_values(
            path=str(rdl_path),
            name="DateFrom",
            source="query",
            query_dataset="MainDataset",
            query_value_field="ProductID",
        )
        ref = _param(rdl_path, "DateFrom").find(
            f"{q('DefaultValue')}/{q('DataSetReference')}"
        )
        assert find_child(ref, "DataSetName").text == "MainDataset"
        assert find_child(ref, "ValueField").text == "ProductID"
        # DefaultValue's DataSetReference has no LabelField — that only
        # makes sense for ValidValues (display vs. value distinction).
        assert find_child(ref, "LabelField") is None


class TestDefaultValuesReplace:
    def test_repeat_calls_replace_block(self, rdl_path):
        set_parameter_default_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["A"],
        )
        set_parameter_default_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["B"],
        )
        defaults = _param(rdl_path, "DateFrom").findall(q("DefaultValue"))
        assert len(defaults) == 1
        assert defaults[0].find(f"{q('Values')}/{q('Value')}").text == "B"


# ---- round-trip ----------------------------------------------------------


class TestRoundTrip:
    def test_validates_after_static_available_and_default(self, rdl_path):
        set_parameter_available_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=[
                {"value": "2026-01-01", "label": "Q1"},
                {"value": "2026-04-01", "label": "Q2"},
            ],
        )
        set_parameter_default_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["2026-01-01"],
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_two_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {
            "set_parameter_available_values",
            "set_parameter_default_values",
        } <= names
