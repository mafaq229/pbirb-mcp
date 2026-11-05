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
from pbirb_mcp.ops.page import set_body_size, set_page_orientation, set_page_setup
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

    def test_changed_lists_only_fields_that_differ(self, rdl_path):
        # #61: the fixture's PageWidth is already 8.5in.
        result = set_page_setup(path=str(rdl_path), page_width="8.5in", margin_top="0.5in")
        assert result["changed"] == ["TopMargin"]

    def test_identical_values_report_no_change_and_keep_file(self, rdl_path):
        st = rdl_path.stat()
        before = (rdl_path.read_bytes(), st.st_ino, st.st_mtime_ns)
        result = set_page_setup(path=str(rdl_path), page_width="8.5in", columns=1)
        assert result["changed"] == []
        st = rdl_path.stat()
        assert (rdl_path.read_bytes(), st.st_ino, st.st_mtime_ns) == before


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


# ---- #80: RDL default page sizes, body-overflow warnings ---------------------


def _drop(rdl_path: Path, *locals_: str) -> None:
    import re

    text = rdl_path.read_text(encoding="utf-8")
    for local in locals_:
        text, n = re.subn(rf"\s*<{local}>[^<]*</{local}>", "", text)
        assert n == 1, local
    rdl_path.write_text(text, encoding="utf-8")


def _wide_landscape(rdl_path: Path) -> None:
    """Case (b): an 11in x 8.5in page with 0.5in side margins and a 10in
    body that fills the 10in printable width exactly."""
    set_page_setup(
        str(rdl_path),
        page_width="11in",
        page_height="8.5in",
        margin_left="0.5in",
        margin_right="0.5in",
    )
    set_body_size(str(rdl_path), width="10in")


class TestOrientationDefaultsAndOverflow:
    @pytest.mark.parametrize(
        "missing", [("PageWidth",), ("PageHeight",), ("PageHeight", "PageWidth")]
    )
    def test_missing_size_is_the_rdl_default(self, rdl_path, missing):
        from pbirb_mcp.ops.validate import validate_report

        _drop(rdl_path, *missing)
        out = set_page_orientation(str(rdl_path), "Landscape")
        assert out["changed"] is True
        assert (out["page_height"], out["page_width"]) == ("8.5in", "11in")
        text = rdl_path.read_text(encoding="utf-8")
        assert "<PageHeight>8.5in</PageHeight>" in text
        assert "<PageWidth>11in</PageWidth>" in text
        assert text.index("<PageHeight>") < text.index("<PageWidth>") < text.index("<LeftMargin>")
        assert validate_report(str(rdl_path))["valid"]

    def test_default_size_already_portrait_is_a_no_op(self, rdl_path):
        _drop(rdl_path, "PageWidth")
        before = rdl_path.read_bytes()
        out = set_page_orientation(str(rdl_path), "Portrait")
        assert out["changed"] is False and out["page_width"] == "8.5in"
        assert rdl_path.read_bytes() == before

    def test_fixture_round_trip_restores_bytes(self, rdl_path):
        before = rdl_path.read_bytes()
        assert "warnings" not in set_page_orientation(str(rdl_path), "Landscape")
        assert "warnings" not in set_page_orientation(str(rdl_path), "Portrait")
        assert rdl_path.read_bytes() == before

    def test_interactive_size_swapped_when_present(self, rdl_path):
        text = rdl_path.read_text(encoding="utf-8")
        text = text.replace(
            "<PageWidth>8.5in</PageWidth>",
            "<PageWidth>8.5in</PageWidth>\n        <InteractiveHeight>11in</InteractiveHeight>"
            "\n        <InteractiveWidth>8.5in</InteractiveWidth>",
        )
        rdl_path.write_text(text, encoding="utf-8")
        set_page_orientation(str(rdl_path), "Landscape")
        page = _page(rdl_path)
        assert find_child(page, "InteractiveHeight").text == "8.5in"
        assert find_child(page, "InteractiveWidth").text == "11in"

    def test_landscape_to_portrait_warns_on_body_overflow(self, rdl_path):
        _wide_landscape(rdl_path)
        out = set_page_orientation(str(rdl_path), "Portrait")
        assert out["changed"] is True
        assert out["warnings"] == [
            {
                "rule": "body-wider-than-page",
                "body_width": "10in",
                "printable_width": "7.5in",
                "overflow": "2.5in",
                "suggestion": out["warnings"][0]["suggestion"],
            }
        ]
        assert "fit_page_to_body" in out["warnings"][0]["suggestion"]

    def test_set_page_setup_margin_change_warns(self, rdl_path):
        _wide_landscape(rdl_path)
        out = set_page_setup(str(rdl_path), margin_left="3in")
        assert out["changed"] == ["LeftMargin"]
        [warning] = out["warnings"]
        assert warning["rule"] == "body-wider-than-page"
        assert (warning["printable_width"], warning["overflow"]) == ("7.5in", "2.5in")

    def test_set_page_setup_columns_warn_with_count(self, rdl_path):
        # 5in body, 1in margins, 8.5in page: two columns need 2 x 5 + 0.5 + 2 = 12.5in.
        out = set_page_setup(str(rdl_path), columns=2)
        [warning] = out["warnings"]
        assert warning["columns"] == 2 and warning["overflow"] == "4in"

    def test_no_warning_when_body_fits_or_nothing_relevant_changed(self, rdl_path):
        assert "warnings" not in set_page_setup(str(rdl_path), margin_left="0.5in")
        _wide_landscape(rdl_path)
        set_page_orientation(str(rdl_path), "Portrait")  # now overflowing
        # A height-only change doesn't re-report the existing overflow.
        assert "warnings" not in set_page_setup(str(rdl_path), margin_top="0.25in")


# ---- registration ---------------------------------------------------------


class TestSetBodySize:
    """v0.4 commit 20 — set_body_size targets <Body>/<Height> and
    <ReportSection>/<Width> (sibling of Body). Distinct from
    set_page_setup (which sets <Page>/<PageWidth>/<PageHeight>, the
    paper chrome) and set_body_item_size (which sets size of items
    INSIDE the body).
    """

    def _section(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        return doc.root.find(f".//{{{RDL_NS}}}ReportSection")

    def test_sets_both_width_and_height(self, rdl_path):
        result = set_body_size(path=str(rdl_path), width="14in", height="9in")
        assert result == {"kind": "Body", "changed": ["Width", "Height"]}
        section = self._section(rdl_path)
        assert find_child(section, "Width").text == "14in"
        assert find_child(find_child(section, "Body"), "Height").text == "9in"

    def test_sets_only_width(self, rdl_path):
        result = set_body_size(path=str(rdl_path), width="16in")
        assert result == {"kind": "Body", "changed": ["Width"]}
        # Body Height untouched.
        body = find_child(self._section(rdl_path), "Body")
        assert find_child(body, "Height").text == "2in"

    def test_sets_only_height(self, rdl_path):
        result = set_body_size(path=str(rdl_path), height="10in")
        assert result == {"kind": "Body", "changed": ["Height"]}
        # ReportSection Width untouched.
        section = self._section(rdl_path)
        assert find_child(section, "Width").text == "5in"

    def test_idempotent_when_value_unchanged(self, rdl_path):
        # Set first.
        set_body_size(path=str(rdl_path), width="8in", height="3in")
        # Same values again → no change recorded.
        result = set_body_size(path=str(rdl_path), width="8in", height="3in")
        assert result == {"kind": "Body", "changed": []}

    def test_requires_at_least_one_arg(self, rdl_path):
        with pytest.raises(ValueError, match="at least one"):
            set_body_size(path=str(rdl_path))

    def test_distinct_from_set_page_setup(self, rdl_path):
        """set_body_size must NOT touch <Page>/<PageWidth>/<PageHeight>.
        Regression guard against conflating page chrome with body
        bounds."""
        set_body_size(path=str(rdl_path), width="20in", height="15in")
        page = _page(rdl_path)
        # Page chrome unchanged.
        assert find_child(page, "PageWidth").text == "8.5in"
        assert find_child(page, "PageHeight").text == "11in"

    def test_round_trip_valid(self, rdl_path):
        set_body_size(path=str(rdl_path), width="14in", height="9in")
        RDLDocument.open(rdl_path).validate()


class TestToolRegistration:
    def test_page_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {"set_page_setup", "set_page_orientation", "set_body_size"} <= names
