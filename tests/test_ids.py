"""Tests for the stable-ID element resolver.

Tools must address elements by their RDL ``Name`` attribute (Tablix, Textbox,
DataSet, ReportParameter) or by ``Group/@Name`` for tablix groups. Indices
break across multi-step edits, so the resolver is the only sanctioned way to
get from a user-facing identifier to a live lxml element.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import (
    AmbiguousElementError,
    ElementNotFoundError,
    resolve_dataset,
    resolve_group,
    resolve_parameter,
    resolve_tablix,
    resolve_textbox,
)
from pbirb_mcp.core.xpath import RDL_NS, q

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def doc(tmp_path: Path) -> RDLDocument:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return RDLDocument.open(dest)


class TestResolveTablix:
    def test_resolves_existing_tablix(self, doc):
        node = resolve_tablix(doc, "MainTable")
        assert node.tag == q("Tablix")
        assert node.get("Name") == "MainTable"

    def test_missing_tablix_raises(self, doc):
        with pytest.raises(ElementNotFoundError) as excinfo:
            resolve_tablix(doc, "NoSuchTable")
        assert "Tablix" in str(excinfo.value)
        assert "NoSuchTable" in str(excinfo.value)

    def test_ambiguous_tablix_raises(self, doc):
        # Inject a duplicate tablix to simulate a malformed report.
        body = doc.root.find(f".//{{{RDL_NS}}}Body/{{{RDL_NS}}}ReportItems")
        original = body.find(q("Tablix"))
        from copy import deepcopy
        dup = deepcopy(original)
        body.append(dup)
        with pytest.raises(AmbiguousElementError):
            resolve_tablix(doc, "MainTable")


class TestResolveDataset:
    def test_resolves_existing_dataset(self, doc):
        node = resolve_dataset(doc, "MainDataset")
        assert node.tag == q("DataSet")
        assert node.get("Name") == "MainDataset"

    def test_missing_dataset_raises(self, doc):
        with pytest.raises(ElementNotFoundError):
            resolve_dataset(doc, "NoSuch")


class TestResolveParameter:
    def test_resolves_existing_parameter(self, doc):
        node = resolve_parameter(doc, "DateFrom")
        assert node.tag == q("ReportParameter")
        assert node.get("Name") == "DateFrom"

    def test_missing_parameter_raises(self, doc):
        with pytest.raises(ElementNotFoundError):
            resolve_parameter(doc, "Nope")


class TestResolveTextbox:
    def test_resolves_header_textbox(self, doc):
        node = resolve_textbox(doc, "HeaderProductID")
        assert node.tag == q("Textbox")
        assert node.get("Name") == "HeaderProductID"

    def test_resolves_data_textbox_anywhere(self, doc):
        # `Amount` lives nested several levels deep inside the tablix; the
        # resolver must reach it without caller-supplied paths.
        node = resolve_textbox(doc, "Amount")
        assert node.get("Name") == "Amount"

    def test_missing_textbox_raises(self, doc):
        with pytest.raises(ElementNotFoundError):
            resolve_textbox(doc, "Ghost")


class TestResolveGroup:
    def test_resolves_row_group_by_name(self, doc):
        # Fixture's tablix has a single row group called "Details".
        node = resolve_group(doc, "MainTable", "Details")
        assert node.tag == q("Group")
        assert node.get("Name") == "Details"

    def test_missing_group_raises(self, doc):
        with pytest.raises(ElementNotFoundError):
            resolve_group(doc, "MainTable", "NoSuchGroup")

    def test_group_lookup_scoped_to_named_tablix(self, doc):
        with pytest.raises(ElementNotFoundError):
            resolve_group(doc, "OtherTable", "Details")
