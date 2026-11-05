"""set_textbox_style_bulk resolves every name first and writes once (#106).

The tool opens the report once, sorts the de-duplicated names into
``textboxes`` / ``skipped`` (not found) / ``ambiguous`` (several
textboxes share the name), styles the resolved ones and saves once —
only when something changed. ``strict=True`` refuses the call before any
write when a name is skipped or ambiguous.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops.reader import get_textbox
from pbirb_mcp.ops.styling import set_textbox_style_bulk, style_tablix_row
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
HEADERS = ["HeaderProductID", "HeaderProductName", "HeaderAmount"]


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def dup_path(rdl_path: Path) -> Path:
    """The fixture with a second body textbox named 'ProductName' (the
    tools refuse to create one, but hand-edited reports have them)."""
    doc = RDLDocument.open(rdl_path)
    cell = doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='ProductName']")
    items = doc.root.find(f".//{{{RDL_NS}}}Body/{{{RDL_NS}}}ReportItems")
    items.append(copy.deepcopy(cell))
    doc.save()
    return rdl_path


def _run_color(path: Path, name: str):
    return ((get_textbox(str(path), name)["style"] or {}).get("run") or {}).get("Color")


def _ambiguous_run_colors(path: Path) -> list:
    doc = RDLDocument.open(path)
    return [
        (tb.find(f".//{{{RDL_NS}}}TextRun/{{{RDL_NS}}}Style/{{{RDL_NS}}}Color"))
        for tb in doc.root.iterfind(f".//{{{RDL_NS}}}Textbox[@Name='ProductName']")
    ]


class TestAmbiguousNames:
    def test_ambiguous_name_reported_others_styled(self, dup_path):
        names = ["HeaderProductID", "HeaderProductName", "ProductName", "HeaderAmount"]
        out = set_textbox_style_bulk(str(dup_path), names, color="#FF0000")
        assert out == {
            "textboxes": HEADERS,
            "skipped": [],
            "ambiguous": ["ProductName"],
            "changed": ["run.Color"],
        }
        for name in HEADERS:
            assert _run_color(dup_path, name) == "#FF0000"
        # Neither 'ProductName' textbox was touched.
        assert _ambiguous_run_colors(dup_path) == [None, None]

    def test_strict_refuses_before_any_write(self, dup_path):
        before = dup_path.read_bytes()
        names = ["HeaderProductID", "HeaderProductName", "ProductName", "HeaderAmount"]
        with pytest.raises(AmbiguousElementError, match=r"ambiguous.*\['ProductName'\]"):
            set_textbox_style_bulk(str(dup_path), names, strict=True, color="#FF0000")
        assert dup_path.read_bytes() == before

    def test_strict_refuses_missing_names(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ElementNotFoundError, match=r"not found: \['Nope'\]"):
            set_textbox_style_bulk(
                str(rdl_path), ["HeaderAmount", "Nope"], strict=True, color="Red"
            )
        assert rdl_path.read_bytes() == before

    def test_strict_with_everything_resolved_styles(self, rdl_path):
        out = set_textbox_style_bulk(str(rdl_path), HEADERS, strict=True, font_weight="Bold")
        assert out["textboxes"] == HEADERS and out["changed"] == ["run.FontWeight"]

    def test_strict_must_be_boolean(self, rdl_path):
        with pytest.raises(ValueError, match="strict"):
            set_textbox_style_bulk(str(rdl_path), HEADERS, strict="yes", color="Red")


class TestDuplicatesAndSaves:
    def test_repeated_names_count_once(self, rdl_path):
        out = set_textbox_style_bulk(
            str(rdl_path), ["HeaderAmount", "HeaderAmount", "Nope", "Nope"], font_weight="Bold"
        )
        assert out["textboxes"] == ["HeaderAmount"]
        assert out["skipped"] == ["Nope"]
        assert out["ambiguous"] == []

    def test_opened_and_saved_exactly_once(self, rdl_path, monkeypatch):
        opens, saves = [], []
        real_open, real_save = RDLDocument.open, RDLDocument.save

        def counting_open(path, *a, **kw):
            opens.append(path)
            return real_open(path, *a, **kw)

        def counting_save(self, *a, **kw):
            saves.append(1)
            return real_save(self, *a, **kw)

        monkeypatch.setattr(RDLDocument, "open", staticmethod(counting_open))
        monkeypatch.setattr(RDLDocument, "save", counting_save)
        set_textbox_style_bulk(str(rdl_path), HEADERS + ["Nope"], color="Blue", font_weight="Bold")
        assert len(opens) == 1
        assert len(saves) == 1

        saves.clear()
        before = rdl_path.read_bytes()
        out = set_textbox_style_bulk(str(rdl_path), HEADERS, color="Blue", font_weight="Bold")
        assert out["changed"] == [] and saves == [] and rdl_path.read_bytes() == before

    def test_no_style_short_circuits_with_deduplicated_names(self, rdl_path, monkeypatch):
        monkeypatch.setattr(
            RDLDocument, "open", staticmethod(lambda *a, **k: pytest.fail("file opened"))
        )
        out = set_textbox_style_bulk(str(rdl_path), ["A", "A", "B"])
        assert out == {"textboxes": ["A", "B"], "skipped": [], "ambiguous": [], "changed": []}


class TestRowStylingUsesCellElements:
    def test_row_cell_with_duplicated_name_is_styled(self, dup_path):
        out = style_tablix_row(str(dup_path), "MainTable", "details", color="Green")
        assert out["cells"] == ["ProductID", "ProductName", "Amount"]
        assert out["skipped"] == []
        # The tablix cell got it; the stray body copy did not.
        colors = [c.text if c is not None else None for c in _ambiguous_run_colors(dup_path)]
        assert colors == ["Green", None]


def test_server_accepts_strict_and_reports_ambiguous(dup_path):
    srv = MCPServer()
    register_all_tools(srv)
    resp = srv.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "set_textbox_style_bulk",
                "arguments": {
                    "path": str(dup_path),
                    "textbox_names": ["ProductName", "HeaderAmount"],
                    "strict": False,
                    "color": "Red",
                },
            },
        }
    )
    result = resp["result"]
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["ambiguous"] == ["ProductName"]
    assert payload["textboxes"] == ["HeaderAmount"]
