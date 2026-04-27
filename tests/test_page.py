"""Page setup and orientation tool tests.

Targets the first ``<ReportSection>/<Page>`` block — multi-section reports
are rare in PBI paginated and the plan deliberately scopes us to one.
``set_page_setup`` mutates only the fields the caller passes (all
optional). ``set_page_orientation`` swaps height and width if the current
orientation doesn't match the requested one — idempotent when it does.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, find_child
from pbirb_mcp.ops.page import set_page_orientation, set_page_setup
from pbirb_mcp.ops.reader import describe_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _page(rdl_path: Path):
    doc = RDLDocument.open(rdl_path)
    return doc.root.find(f".//{{{RDL_NS}}}ReportSection/{{{RDL_NS}}}Page")


# ---- set_page_setup -------------------------------------------------------


class TestSetPageSetup:
    def test_partial_update_leaves_other_fields_untouched(self, rdl_path):
        before = describe_report(path=str(rdl_path))["page"]
        set_page_setup(path=str(rdl_path), margin_top="0.5in")
        after = describe_report(path=str(rdl_path))["page"]
        assert after["margin_top"] == "0.5in"
        # Everything else is unchanged.
        for key in ("height", "width", "margin_bottom", "margin_left", "margin_right"):
            assert after[key] == before[key]

    def test_full_update_writes_all_fields(self, rdl_path):
        set_page_setup(
            path=str(rdl_path),
            page_height="14in",
            page_width="8.5in",
            margin_top="0.5in",
            margin_bottom="0.5in",
            margin_left="0.75in",
            margin_right="0.75in",
        )
        after = describe_report(path=str(rdl_path))["page"]
        assert after["height"] == "14in"
        assert after["width"] == "8.5in"
        assert after["margin_top"] == "0.5in"
        assert after["margin_bottom"] == "0.5in"
        assert after["margin_left"] == "0.75in"
        assert after["margin_right"] == "0.75in"

    def test_columns_writes_columns_element(self, rdl_path):
        set_page_setup(path=str(rdl_path), columns=2)
        page = _page(rdl_path)
        cols = find_child(page, "Columns")
        assert cols is not None and cols.text == "2"

    def test_columns_one_drops_columns_element(self, rdl_path):
        set_page_setup(path=str(rdl_path), columns=2)
        set_page_setup(path=str(rdl_path), columns=1)
        # Single-column is the implicit default; no <Columns/> element wanted.
        page = _page(rdl_path)
        assert find_child(page, "Columns") is None

    def test_no_args_is_no_op(self, rdl_path):
        before = rdl_path.read_bytes()
        set_page_setup(path=str(rdl_path))
        assert rdl_path.read_bytes() == before

    def test_round_trip_safe(self, rdl_path):
        set_page_setup(path=str(rdl_path), page_height="14in")
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- set_page_orientation -------------------------------------------------


class TestSetPageOrientation:
    def test_landscape_swaps_height_and_width(self, rdl_path):
        # Fixture is portrait: 11in tall x 8.5in wide.
        set_page_orientation(path=str(rdl_path), orientation="Landscape")
        page = describe_report(path=str(rdl_path))["page"]
        assert page["height"] == "8.5in"
        assert page["width"] == "11in"

    def test_landscape_idempotent_when_already_landscape(self, rdl_path):
        set_page_orientation(path=str(rdl_path), orientation="Landscape")
        first = rdl_path.read_bytes()
        set_page_orientation(path=str(rdl_path), orientation="Landscape")
        second = rdl_path.read_bytes()
        assert first == second

    def test_portrait_idempotent_for_portrait_fixture(self, rdl_path):
        before = rdl_path.read_bytes()
        set_page_orientation(path=str(rdl_path), orientation="Portrait")
        assert rdl_path.read_bytes() == before

    def test_portrait_after_landscape_restores_dimensions(self, rdl_path):
        set_page_orientation(path=str(rdl_path), orientation="Landscape")
        set_page_orientation(path=str(rdl_path), orientation="Portrait")
        page = describe_report(path=str(rdl_path))["page"]
        assert page["height"] == "11in"
        assert page["width"] == "8.5in"

    def test_unknown_orientation_rejected(self, rdl_path):
        with pytest.raises(ValueError):
            set_page_orientation(path=str(rdl_path), orientation="Diagonal")

    def test_handles_metric_units(self, rdl_path):
        # Switch the fixture to centimetre dimensions and confirm the
        # comparison still picks the right orientation.
        set_page_setup(path=str(rdl_path), page_height="29.7cm", page_width="21cm")
        set_page_orientation(path=str(rdl_path), orientation="Landscape")
        page = describe_report(path=str(rdl_path))["page"]
        # A4 landscape: width > height now.
        assert page["height"] == "21cm"
        assert page["width"] == "29.7cm"


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_page_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {"set_page_setup", "set_page_orientation"} <= names
