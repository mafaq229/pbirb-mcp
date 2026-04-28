"""Tests for v0.2 parameter CRUD: set_parameter_prompt, set_parameter_type,
add_parameter, remove_parameter, rename_parameter.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child
from pbirb_mcp.ops.dataset import add_query_parameter
from pbirb_mcp.ops.parameters import (
    add_parameter,
    remove_parameter,
    rename_parameter,
    set_parameter_default_values,
    set_parameter_prompt,
    set_parameter_type,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _param(doc, name):
    return doc.root.find(f".//{{{RDL_NS}}}ReportParameter[@Name='{name}']")


# ---- set_parameter_prompt ------------------------------------------------


class TestSetParameterPrompt:
    def test_overwrites_existing_prompt(self, rdl_path):
        set_parameter_prompt(path=str(rdl_path), name="DateFrom", prompt="Start Date")
        doc = RDLDocument.open(rdl_path)
        prompt = find_child(_param(doc, "DateFrom"), "Prompt")
        assert prompt is not None and prompt.text == "Start Date"

    def test_empty_string_clears(self, rdl_path):
        set_parameter_prompt(path=str(rdl_path), name="DateFrom", prompt="")
        doc = RDLDocument.open(rdl_path)
        assert find_child(_param(doc, "DateFrom"), "Prompt") is None

    def test_unknown_param_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_parameter_prompt(path=str(rdl_path), name="NoSuch", prompt="X")


# ---- set_parameter_type --------------------------------------------------


class TestSetParameterType:
    def test_changes_data_type(self, rdl_path):
        set_parameter_type(path=str(rdl_path), name="DateFrom", type="String")
        doc = RDLDocument.open(rdl_path)
        dt = find_child(_param(doc, "DateFrom"), "DataType")
        assert dt.text == "String"

    def test_invalid_type_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            set_parameter_type(path=str(rdl_path), name="DateFrom", type="Currency")

    def test_incompatible_default_rejected(self, rdl_path):
        # Force a String default first, then try to switch to Integer.
        set_parameter_default_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["not_an_integer"],
        )
        with pytest.raises(ValueError):
            set_parameter_type(path=str(rdl_path), name="DateFrom", type="Integer")

    def test_compatible_default_passes(self, rdl_path):
        set_parameter_default_values(
            path=str(rdl_path),
            name="DateFrom",
            source="static",
            static_values=["42"],
        )
        set_parameter_type(path=str(rdl_path), name="DateFrom", type="Integer")
        doc = RDLDocument.open(rdl_path)
        assert find_child(_param(doc, "DateFrom"), "DataType").text == "Integer"


# ---- add_parameter -------------------------------------------------------


class TestAddParameter:
    def test_appends_minimal_parameter(self, rdl_path):
        add_parameter(
            path=str(rdl_path),
            name="StoreId",
            type="Integer",
        )
        doc = RDLDocument.open(rdl_path)
        p = _param(doc, "StoreId")
        assert p is not None
        assert find_child(p, "DataType").text == "Integer"

    def test_with_prompt_and_flags(self, rdl_path):
        add_parameter(
            path=str(rdl_path),
            name="StoreId",
            type="Integer",
            prompt="Pick a store",
            allow_null=True,
            multi_value=True,
            hidden=False,
        )
        doc = RDLDocument.open(rdl_path)
        p = _param(doc, "StoreId")
        assert find_child(p, "Prompt").text == "Pick a store"
        assert find_child(p, "Nullable").text == "true"
        assert find_child(p, "MultiValue").text == "true"
        assert find_child(p, "Hidden").text == "false"

    def test_duplicate_name_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            add_parameter(path=str(rdl_path), name="DateFrom", type="String")

    def test_invalid_type_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            add_parameter(path=str(rdl_path), name="X", type="Currency")


# ---- remove_parameter ----------------------------------------------------


class TestRemoveParameter:
    def test_removes_unreferenced_parameter(self, rdl_path):
        add_parameter(path=str(rdl_path), name="Spare", type="String")
        remove_parameter(path=str(rdl_path), name="Spare")
        doc = RDLDocument.open(rdl_path)
        assert _param(doc, "Spare") is None

    def test_refuses_when_referenced(self, rdl_path):
        # Seed a reference: add a QueryParameter pointing at DateFrom.
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        with pytest.raises(ValueError):
            remove_parameter(path=str(rdl_path), name="DateFrom")

    def test_force_removes_anyway(self, rdl_path):
        remove_parameter(path=str(rdl_path), name="DateFrom", force=True)
        doc = RDLDocument.open(rdl_path)
        assert _param(doc, "DateFrom") is None

    def test_unknown_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_parameter(path=str(rdl_path), name="NoSuch")


# ---- rename_parameter ----------------------------------------------------


class TestRenameParameter:
    def test_renames_declaration(self, rdl_path):
        rename_parameter(path=str(rdl_path), old_name="DateFrom", new_name="StartDate")
        doc = RDLDocument.open(rdl_path)
        assert _param(doc, "DateFrom") is None
        assert _param(doc, "StartDate") is not None

    def test_rewrites_query_parameter_reference(self, rdl_path):
        # Seed a reference, then rename and verify rewrite.
        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="@DateFrom",
            value_expression="=Parameters!DateFrom.Value",
        )
        result = rename_parameter(path=str(rdl_path), old_name="DateFrom", new_name="StartDate")
        assert result["references_rewritten"] >= 1
        # Confirm in the file: no stray Parameters!DateFrom.Value remains.
        text = Path(rdl_path).read_text()
        assert "Parameters!DateFrom.Value" not in text
        assert "Parameters!StartDate.Value" in text

    def test_collision_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            rename_parameter(path=str(rdl_path), old_name="DateFrom", new_name="DateTo")

    def test_same_name_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            rename_parameter(path=str(rdl_path), old_name="DateFrom", new_name="DateFrom")

    def test_unknown_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            rename_parameter(path=str(rdl_path), old_name="NoSuch", new_name="X")

    def test_round_trip_safe(self, rdl_path):
        rename_parameter(path=str(rdl_path), old_name="DateFrom", new_name="StartDate")
        RDLDocument.open(rdl_path).validate()


# ---- registration --------------------------------------------------------


class TestToolRegistration:
    def test_all_five_tools_registered(self):
        server = MCPServer()
        register_all_tools(server)
        assert {
            "set_parameter_prompt",
            "set_parameter_type",
            "add_parameter",
            "remove_parameter",
            "rename_parameter",
        }.issubset(server._tools.keys())
