"""Tests for the Phase 4 tablix-sizing tools.

``set_column_width`` (in pbirb_mcp.ops.tablix_columns) and
``set_tablix_size`` (in pbirb_mcp.ops.tablix) close the v0.2 gap where
the only way to resize a column or tablix was to remove and re-add it.
RAG-Report session feedback bugs #9 + #10.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children
from pbirb_mcp.ops.tablix import set_tablix_size
from pbirb_mcp.ops.tablix_columns import set_column_width
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _tablix(rdl_path: Path, name: str):
    doc = RDLDocument.open(rdl_path)
    return doc.root.find(f".//{{{RDL_NS}}}Tablix[@Name='{name}']")


def _column_width(rdl_path: Path, tablix_name: str, idx: int) -> str:
    t = _tablix(rdl_path, tablix_name)
    body = find_child(t, "TablixBody")
    cols_root = find_child(body, "TablixColumns")
    cols = find_children(cols_root, "TablixColumn")
    return find_child(cols[idx], "Width").text


# ---- set_column_width ----------------------------------------------------


class TestSetColumnWidthByIndex:
    def test_changes_existing_width(self, rdl_path):
        # Fixture's MainTable has 3 columns; column 0 is ProductID.
        result = set_column_width(
            path=str(rdl_path),
            tablix_name="MainTable",
            column=0,
            width="2.5in",
        )
        assert result["column_index"] == 0
        assert result["width"] == "2.5in"
        assert result["kind"] == "TablixColumn"
        assert result["changed"] is True
        assert _column_width(rdl_path, "MainTable", 0) == "2.5in"

    def test_idempotent_when_unchanged(self, rdl_path):
        # First set a known width, then reset to the same value.
        set_column_width(
            path=str(rdl_path),
            tablix_name="MainTable",
            column=0,
            width="3in",
        )
        before = (rdl_path).read_bytes()
        result = set_column_width(
            path=str(rdl_path),
            tablix_name="MainTable",
            column=0,
            width="3in",
        )
        assert result["changed"] is False
        assert (rdl_path).read_bytes() == before

    def test_index_out_of_range(self, rdl_path):
        with pytest.raises(IndexError, match="out of range"):
            set_column_width(
                path=str(rdl_path),
                tablix_name="MainTable",
                column=99,
                width="1in",
            )

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_column_width(
                path=str(rdl_path),
                tablix_name="NoSuchTablix",
                column=0,
                width="1in",
            )

    def test_empty_width_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            set_column_width(
                path=str(rdl_path),
                tablix_name="MainTable",
                column=0,
                width="",
            )


class TestSetColumnWidthByName:
    def test_resolves_textbox_name_to_column_index(self, rdl_path):
        # MainTable column 1 contains the ProductName cell. Use the
        # textbox name as the handle.
        result = set_column_width(
            path=str(rdl_path),
            tablix_name="MainTable",
            column="ProductName",
            width="3in",
        )
        assert result["column_index"] == 1
        assert _column_width(rdl_path, "MainTable", 1) == "3in"

    def test_resolves_via_header_textbox_name(self, rdl_path):
        # HeaderProductID lives in row 0, column 0. The resolver should
        # find it on the first row scan.
        result = set_column_width(
            path=str(rdl_path),
            tablix_name="MainTable",
            column="HeaderProductID",
            width="0.5in",
        )
        assert result["column_index"] == 0
        assert _column_width(rdl_path, "MainTable", 0) == "0.5in"

    def test_unknown_textbox_name_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError, match="no cell with textbox"):
            set_column_width(
                path=str(rdl_path),
                tablix_name="MainTable",
                column="NoSuchTextbox",
                width="1in",
            )


# ---- set_tablix_size -----------------------------------------------------


class TestSetTablixSize:
    def test_sets_height_only(self, rdl_path):
        result = set_tablix_size(
            path=str(rdl_path),
            name="MainTable",
            height="3.5in",
        )
        assert result["changed"] == ["Height"]
        t = _tablix(rdl_path, "MainTable")
        assert find_child(t, "Height").text == "3.5in"

    def test_sets_width_only(self, rdl_path):
        result = set_tablix_size(
            path=str(rdl_path),
            name="MainTable",
            width="6in",
        )
        assert result["changed"] == ["Width"]
        t = _tablix(rdl_path, "MainTable")
        assert find_child(t, "Width").text == "6in"

    def test_sets_both(self, rdl_path):
        result = set_tablix_size(
            path=str(rdl_path),
            name="MainTable",
            height="3in",
            width="7in",
        )
        assert sorted(result["changed"]) == ["Height", "Width"]

    def test_no_args_no_op(self, rdl_path):
        before = (rdl_path).read_bytes()
        result = set_tablix_size(path=str(rdl_path), name="MainTable")
        assert result["changed"] == []
        assert (rdl_path).read_bytes() == before

    def test_idempotent_when_unchanged(self, rdl_path):
        # Set known size then re-set.
        set_tablix_size(path=str(rdl_path), name="MainTable", height="2in")
        before = (rdl_path).read_bytes()
        result = set_tablix_size(path=str(rdl_path), name="MainTable", height="2in")
        assert result["changed"] == []
        assert (rdl_path).read_bytes() == before

    def test_partial_change_only_lists_changed_fields(self, rdl_path):
        # First set both; then re-set only width with same height to
        # confirm only the changed field appears.
        set_tablix_size(path=str(rdl_path), name="MainTable", height="2in", width="5in")
        result = set_tablix_size(path=str(rdl_path), name="MainTable", height="2in", width="6in")
        assert result["changed"] == ["Width"]

    def test_empty_height_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            set_tablix_size(path=str(rdl_path), name="MainTable", height="")

    def test_empty_width_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            set_tablix_size(path=str(rdl_path), name="MainTable", width="   ")

    def test_unknown_tablix_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_tablix_size(path=str(rdl_path), name="NoSuchTablix", height="2in")

    def test_round_trip_safe(self, rdl_path):
        set_tablix_size(path=str(rdl_path), name="MainTable", height="3in", width="6in")
        RDLDocument.open(rdl_path).validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_set_column_width_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "set_column_width" in names

    def test_set_tablix_size_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "set_tablix_size" in names
