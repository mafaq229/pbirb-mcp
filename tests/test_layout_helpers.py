"""fit_page_to_body / center_item (#49)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.geometry import item_box, section_geometry
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import q
from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.header_footer import add_header_textbox, set_page_header
from pbirb_mcp.ops.layout_helpers import center_item, fit_page_to_body
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.page import set_body_size, set_page_setup
from pbirb_mcp.ops.positioning import set_body_item_position
from pbirb_mcp.ops.reader import describe_report
from pbirb_mcp.ops.tablix import set_tablix_size
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _rules(path: Path) -> list[str]:
    return [i["rule"] for i in lint_report(str(path))["issues"]]


def _named(path: Path, name: str):
    doc = RDLDocument.open(path)
    for el in doc.root.iter():
        if isinstance(el.tag, str) and el.get("Name") == name:
            return doc, el
    raise AssertionError(name)


def _build_repro(path: Path) -> None:
    set_tablix_size(str(path), "MainTable", width="5in")
    set_body_size(str(path), width="9in", height="2in")
    add_body_textbox(str(path), "Overlap", "x", "0.6in", "0.6in", "2in", "0.25in")
    add_body_textbox(str(path), "Outside", "x", "3in", "8.5in", "2in", "0.25in")


class TestFitPageToBody:
    def test_page_mode_clears_body_wider_than_page(self, rdl_path):
        _build_repro(rdl_path)
        assert "body-wider-than-page" in _rules(rdl_path)
        out = fit_page_to_body(str(rdl_path), mode="page")
        assert out == {
            "mode": "page",
            "changed": True,
            "page_width": "11in",
            "body_width": "9in",
            "printable_width": "9in",
        }
        assert "body-wider-than-page" not in _rules(rdl_path)
        assert describe_report(str(rdl_path))["page"]["width"] == "11in"
        assert validate_report(str(rdl_path))["valid"]

    def test_default_mode_is_page(self, rdl_path):
        set_body_size(str(rdl_path), width="7in")
        assert fit_page_to_body(str(rdl_path))["page_width"] == "9in"

    def test_page_mode_noop_when_it_fits(self, rdl_path):
        before = rdl_path.read_bytes()
        out = fit_page_to_body(str(rdl_path), mode="page")
        assert out["changed"] is False
        assert out["page_width"] == "8.5in"
        assert rdl_path.read_bytes() == before

    def test_page_mode_keeps_page_unit_and_columns(self, rdl_path):
        set_page_setup(str(rdl_path), page_width="21.59cm", columns=2)
        set_body_size(str(rdl_path), width="4in")
        # 2 x 4in + 0.5in + 2in = 10.5in = 26.67cm
        out = fit_page_to_body(str(rdl_path), mode="page")
        assert out["page_width"] == "26.67cm"
        assert "body-wider-than-page" not in _rules(rdl_path)

    def test_body_mode_shrinks_to_rightmost_item(self, rdl_path):
        set_body_size(str(rdl_path), width="9in")
        out = fit_page_to_body(str(rdl_path), mode="body")
        # MainTable: left 0.5in + width 4in.
        assert out["changed"] is True
        assert out["body_width"] == "4.5in"
        assert out["page_width"] == "8.5in"
        assert _rules(rdl_path) == []

    def test_body_mode_refuses_when_items_do_not_fit(self, rdl_path):
        _build_repro(rdl_path)  # "Outside" reaches 10.5in
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="10.5in"):
            fit_page_to_body(str(rdl_path), mode="body")
        assert rdl_path.read_bytes() == before

    def test_body_mode_noop_when_it_fits(self, rdl_path):
        out = fit_page_to_body(str(rdl_path), mode="body")
        assert out["changed"] is False
        assert out["body_width"] == "5in"

    def test_unknown_mode_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="mode"):
            fit_page_to_body(str(rdl_path), mode="sideways")


class TestCenterItem:
    def test_centres_tablix_in_body(self, rdl_path):
        # Width matches the columns (4in) in the fixture; body is 5in wide.
        set_body_item_position(str(rdl_path), "MainTable", top="0.5in", left="0in")
        out = center_item(str(rdl_path), "MainTable")
        assert out["changed"] is True
        assert out["left"] == "0.5in"
        assert out["warnings"] == []
        doc, tablix = _named(rdl_path, "MainTable")
        box = item_box(tablix)
        body_w = section_geometry(next(doc.root.iter(q("ReportSection")))).body_width
        assert abs(box.left - (body_w - box.right)) < 0.001

    def test_issue_repro_centres_after_fit(self, rdl_path):
        _build_repro(rdl_path)
        fit_page_to_body(str(rdl_path), mode="page")
        set_tablix_size(str(rdl_path), "MainTable", width="4in")  # clear the mismatch
        out = center_item(str(rdl_path), "MainTable")
        assert out["left"] == "2.5in"
        doc, tablix = _named(rdl_path, "MainTable")
        box = item_box(tablix)
        assert abs(box.left - (9.0 - box.right)) < 0.001
        assert validate_report(str(rdl_path))["valid"]

    def test_tablix_centred_on_rendered_column_width(self, rdl_path):
        # Width says 5in but the columns render 4in: centre what is drawn.
        set_body_size(str(rdl_path), width="9in")
        set_tablix_size(str(rdl_path), "MainTable", width="5in")
        out = center_item(str(rdl_path), "MainTable")
        assert out["left"] == "2.5in"
        assert out["warnings"] and "tablix-width-mismatch" in out["warnings"][0]

    def test_idempotent(self, rdl_path):
        # The fixture's MainTable already sits centred (0.5in + 4in + 0.5in).
        before = rdl_path.read_bytes()
        assert center_item(str(rdl_path), "MainTable")["changed"] is False
        assert rdl_path.read_bytes() == before

    def test_vertical_and_both(self, rdl_path):
        add_body_textbox(str(rdl_path), "Badge", "x", "0in", "0in", "1in", "0.5in")
        out = center_item(str(rdl_path), "Badge", axis="vertical")
        assert (out["top"], out["left"]) == ("0.75in", "0in")
        out = center_item(str(rdl_path), "Badge", axis="both")
        assert (out["top"], out["left"]) == ("0.75in", "2in")

    def test_header_item_uses_printable_width(self, rdl_path):
        set_page_header(str(rdl_path), height="1in")
        add_header_textbox(str(rdl_path), "Title", "x", "0in", "0in", "2.5in", "0.5in")
        out = center_item(str(rdl_path), "Title")
        # (8.5 - 1 - 1 - 2.5) / 2
        assert out["container"] == "header"
        assert out["left"] == "2in"

    def test_item_inside_rectangle(self, rdl_path):
        src = rdl_path.read_text(encoding="utf-8")
        src = src.replace(
            "</Tablix>\n        </ReportItems>",
            "</Tablix>"
            '<Rectangle Name="Frame"><ReportItems>'
            '<Textbox Name="Inner"><Paragraphs><Paragraph><TextRuns><TextRun>'
            "<Value>x</Value></TextRun></TextRuns></Paragraph></Paragraphs>"
            "<Height>0.25in</Height><Width>1cm</Width></Textbox></ReportItems>"
            "<Top>1.25in</Top><Height>0.5in</Height><Width>2in</Width></Rectangle>"
            "</ReportItems>",
            1,
        )
        rdl_path.write_text(src, encoding="utf-8")
        out = center_item(str(rdl_path), "Inner")
        assert out["container"] == "rectangle:Frame"
        # (2in - 1cm) / 2, written in the item's unit (cm).
        assert out["left"] == "2.04cm"
        assert validate_report(str(rdl_path))["valid"]

    def test_item_larger_than_container_rejected(self, rdl_path):
        add_body_textbox(str(rdl_path), "Huge", "x", "1.5in", "0in", "6in", "0.25in")
        with pytest.raises(ValueError, match="larger"):
            center_item(str(rdl_path), "Huge")

    def test_unknown_or_cell_item_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            center_item(str(rdl_path), "Nope")
        # Tablix-cell textboxes are placed by their cell.
        with pytest.raises(ElementNotFoundError, match="cell"):
            center_item(str(rdl_path), "ProductID")

    def test_unknown_axis_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="axis"):
            center_item(str(rdl_path), "MainTable", axis="diagonal")


class TestRegistration:
    def _server(self) -> MCPServer:
        srv = MCPServer()
        register_all_tools(srv)
        return srv

    def test_tools_listed_with_strict_schemas(self):
        listing = self._server().handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        by_name = {t["name"]: t for t in listing}
        fit = by_name["fit_page_to_body"]["inputSchema"]
        assert fit["properties"]["mode"]["enum"] == ["page", "body"]
        assert fit["additionalProperties"] is False
        center = by_name["center_item"]["inputSchema"]
        assert center["required"] == ["path", "name"]
        assert center["properties"]["axis"]["enum"] == ["horizontal", "vertical", "both"]
        assert center["additionalProperties"] is False

    def test_bad_mode_rejected_by_server(self, rdl_path):
        resp = self._server().handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "fit_page_to_body",
                    "arguments": {"path": str(rdl_path), "mode": "wide"},
                },
            }
        )
        assert "error" in resp or resp["result"].get("isError")

    def test_center_item_via_jsonrpc_and_apply_edits(self, rdl_path):
        srv = self._server()
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "apply_edits",
                    "arguments": {
                        "path": str(rdl_path),
                        "ops": [
                            {"tool": "set_body_size", "args": {"width": "9in"}},
                            {"tool": "fit_page_to_body", "args": {"mode": "page"}},
                            {"tool": "center_item", "args": {"name": "MainTable"}},
                        ],
                    },
                },
            }
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["committed"] is True, payload
        page = describe_report(str(rdl_path))["page"]
        assert page["width"] == "11in"
        items = {i["name"]: i for i in describe_report(str(rdl_path))["body_items"]}
        assert items["MainTable"]["left"] == "2.5in"
