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


# ---- every reference form (#73) ------------------------------------------

AMOUNT_VALUE = "<Value>=Fields!Amount.Value</Value>"


def _patch(path: Path, *subs) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in subs:
        assert old in text, old
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def _amount(expr: str):
    return (AMOUNT_VALUE, f"<Value>{expr}</Value>")


class TestEveryReferenceForm:
    FORMS = [
        "=Parameters!DateFrom.Count",
        "=Parameters!DateFrom.IsMultiValue",
        '=Parameters("DateFrom").Value',
        '=Parameters( "DateFrom" ).Label',
        "=Parameters!DateFrom",
        "=Parameters!DateFrom.Value(0)",
        "=parameters!DateFrom.Value",  # VB collection names are case-insensitive
    ]

    @pytest.mark.parametrize("expr", FORMS)
    def test_rename_rewrites_form(self, rdl_path, expr):
        _patch(rdl_path, _amount(expr))
        result = rename_parameter(str(rdl_path), "DateFrom", "StartDate")
        assert result["references_rewritten"] == 1
        text = rdl_path.read_text(encoding="utf-8")
        assert "DateFrom" not in text.replace('Name="DateFrom"', "")
        new_expr = expr.replace("DateFrom", "StartDate")
        assert f"<Value>{new_expr}</Value>" in text

    @pytest.mark.parametrize("expr", FORMS)
    def test_remove_refuses_on_form(self, rdl_path, expr):
        _patch(rdl_path, _amount(expr))
        with pytest.raises(ValueError, match="still referenced"):
            remove_parameter(str(rdl_path), "DateFrom")

    def test_issue_repro(self, rdl_path):
        from pbirb_mcp.ops.lint import lint_report

        _patch(
            rdl_path,
            _amount(
                "=Parameters!DateFrom.Count &amp; Parameters!DateFrom.IsMultiValue &amp; "
                'Parameters("DateFrom").Value'
            ),
        )
        assert rename_parameter(str(rdl_path), "DateFrom", "StartDate")["references_rewritten"] == 1
        assert "Parameters!DateFrom" not in rdl_path.read_text(encoding="utf-8")
        assert 'Parameters("DateFrom")' not in rdl_path.read_text(encoding="utf-8")
        assert lint_report(str(rdl_path), rules=["undefined-parameter"])["issues"] == []

    def test_literals_comments_and_lookalikes_untouched(self, rdl_path):
        expr = (
            '="Parameters!DateFrom.Value" &amp; Parameters!DateFromX.Value &amp; '
            "Fields!DateFrom.Value &amp; Parameters!DateTo.Value ' Parameters!DateFrom.Value"
        )
        _patch(rdl_path, _amount(expr))
        before = rdl_path.read_text(encoding="utf-8")
        result = rename_parameter(str(rdl_path), "DateFrom", "StartDate")
        assert result["references_rewritten"] == 0
        after = rdl_path.read_text(encoding="utf-8")
        assert after == before.replace(
            '<ReportParameter Name="DateFrom">', '<ReportParameter Name="StartDate">'
        )
        # Nor do they block a remove.
        remove_parameter(str(rdl_path), "StartDate")

    def test_rename_covers_queries_defaults_filters_and_code(self, rdl_path):
        from pbirb_mcp.ops.lint import lint_report
        from pbirb_mcp.ops.validate import validate_report

        add_query_parameter(str(rdl_path), "MainDataset", "From", '=Parameters("DateFrom").Value')
        set_parameter_default_values(
            str(rdl_path), "DateTo", "static", ['=DateAdd("d", 7, Parameters!DateFrom.Value)']
        )
        _patch(
            rdl_path,
            (
                "</DataSets>",
                "</DataSets><Code>Function F() As Object\n"
                "  Return Report.Parameters!DateFrom.Value ' Parameters!DateFrom\n"
                "End Function</Code>",
            ),
        )
        result = rename_parameter(str(rdl_path), "DateFrom", "StartDate")
        assert result["references_rewritten"] == 3
        text = rdl_path.read_text(encoding="utf-8")
        assert '<Value>=Parameters("StartDate").Value</Value>' in text
        assert "DateAdd(&quot;d&quot;, 7, Parameters!StartDate.Value)" in text or (
            'DateAdd("d", 7, Parameters!StartDate.Value)' in text
        )
        assert "Return Report.Parameters!StartDate.Value ' Parameters!DateFrom" in text
        assert lint_report(str(rdl_path), rules=["undefined-parameter"])["issues"] == []
        assert validate_report(str(rdl_path))["valid"]

    def test_remove_ignores_the_parameters_own_declaration(self, rdl_path):
        set_parameter_default_values(
            str(rdl_path), "DateTo", "static", ["=Parameters!DateTo.Value"]
        )
        remove_parameter(str(rdl_path), "DateTo")
        assert _param(RDLDocument.open(rdl_path), "DateTo") is None

    def test_remove_refuses_on_code_reference(self, rdl_path):
        _patch(
            rdl_path,
            (
                "</DataSets>",
                "</DataSets><Code>Function F() As Object\n"
                '  Return Report.Parameters("DateTo").Value\nEnd Function</Code>',
            ),
        )
        with pytest.raises(ValueError, match="Code"):
            remove_parameter(str(rdl_path), "DateTo")


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
