"""Tests for Phase 8 commit 36: field_format + type-aware filter warnings.

The fixture ships with three String-typed fields (no rd:TypeName) and
DateTime parameters DateFrom/DateTo. To exercise the type-mismatch
check we inject an ``<rd:TypeName>System.DateTime</rd:TypeName>`` into
one field and one ``<DataType>String</DataType>`` parameter via etree
before driving the tool — small, local, deterministic.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import q, qrd
from pbirb_mcp.ops.dataset import add_dataset_filter
from pbirb_mcp.ops.filter_types import wrap_with_format
from pbirb_mcp.ops.tablix import add_tablix_filter

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _annotate_field_type(rdl_path: Path, field_name: str, type_name: str) -> None:
    """Add <rd:TypeName>type_name</rd:TypeName> to the named field."""
    doc = RDLDocument.open(rdl_path)
    for f in doc.root.iter(q("Field")):
        if f.get("Name") != field_name:
            continue
        existing = f.find(qrd("TypeName"))
        if existing is not None:
            existing.text = type_name
        else:
            tn = etree.SubElement(f, qrd("TypeName"))
            tn.text = type_name
        break
    doc.save()


def _add_string_param(rdl_path: Path, name: str) -> None:
    doc = RDLDocument.open(rdl_path)
    rp_block = doc.root.find(q("ReportParameters"))
    rp = etree.SubElement(rp_block, q("ReportParameter"), Name=name)
    etree.SubElement(rp, q("DataType")).text = "String"
    doc.save()


# ---- wrap_with_format unit tests ----------------------------------------


class TestWrapWithFormat:
    def test_adds_format_call_keeping_leading_eq(self):
        out = wrap_with_format("=Fields!Date.Value", "MMM, yyyy")
        assert out == '=Format(Fields!Date.Value, "MMM, yyyy")'

    def test_no_leading_eq_passes_through(self):
        out = wrap_with_format("Fields!Date.Value", "yyyy-MM-dd")
        assert out == 'Format(Fields!Date.Value, "yyyy-MM-dd")'

    def test_format_with_quotes_is_escaped(self):
        out = wrap_with_format("=Fields!X.Value", 'My "fmt"')
        # RDL escapes embedded double quotes by doubling them.
        assert out == '=Format(Fields!X.Value, "My ""fmt""")'


# ---- add_tablix_filter integration --------------------------------------


class TestAddTablixFilterFieldFormat:
    def test_field_format_wraps_expression(self, rdl_path):
        result = add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["X"],
            field_format="MMM, yyyy",
        )
        assert result["formatted"] is True
        assert result["expression"].startswith("=Format(")
        assert "MMM, yyyy" in result["expression"]

    def test_no_field_format_leaves_expression(self, rdl_path):
        result = add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["X"],
        )
        assert result["formatted"] is False
        assert result["expression"] == "=Fields!ProductName.Value"


class TestAddTablixFilterTypeWarnings:
    def test_match_no_warning(self, rdl_path):
        # ProductName is rd:TypeName=System.String; pair with a String
        # parameter so groups match → no warning.
        _add_string_param(rdl_path, "ProductFilter")
        result = add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["=Parameters!ProductFilter.Value"],
        )
        assert result["warnings"] == []

    def test_datetime_field_vs_string_param_warns(self, rdl_path):
        _annotate_field_type(rdl_path, "ProductName", "System.DateTime")
        _add_string_param(rdl_path, "Branch")
        result = add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["=Parameters!Branch.Value"],
        )
        assert len(result["warnings"]) == 1
        msg = result["warnings"][0]
        assert "ProductName" in msg
        assert "Branch" in msg
        assert "datetime" in msg
        assert "string" in msg

    def test_field_format_does_not_suppress_warning(self, rdl_path):
        # Even with a field_format wrap, the underlying types still
        # mismatch — emit the warning so the user knows the runtime
        # comparison is string vs string only after Format() runs.
        _annotate_field_type(rdl_path, "ProductName", "System.DateTime")
        _add_string_param(rdl_path, "Branch")
        result = add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["=Parameters!Branch.Value"],
            field_format="MMM, yyyy",
        )
        assert len(result["warnings"]) == 1


# ---- add_dataset_filter integration -------------------------------------


class TestAddDatasetFilterFieldFormat:
    def test_field_format_wraps_expression(self, rdl_path):
        result = add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["X"],
            field_format="yyyy-MM",
        )
        assert result["formatted"] is True
        assert "Format(" in result["expression"]
        assert "yyyy-MM" in result["expression"]

    def test_warnings_passthrough(self, rdl_path):
        _annotate_field_type(rdl_path, "Amount", "System.Decimal")
        _add_string_param(rdl_path, "PriceTier")
        result = add_dataset_filter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            expression="=Fields!Amount.Value",
            operator="Equal",
            values=["=Parameters!PriceTier.Value"],
        )
        assert len(result["warnings"]) == 1
        assert "Amount" in result["warnings"][0]
        assert "PriceTier" in result["warnings"][0]
