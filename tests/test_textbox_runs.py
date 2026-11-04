"""set_textbox_runs preserves paragraphs and styles (issue #41).

The tool used to throw the whole ``<Paragraphs>`` subtree away and rebuild
one paragraph with an empty ``<Style/>``, silently dropping paragraphs
2..N, paragraph properties (TextAlign, ListStyle, LeftIndent, ...), the
run's MarkupType / ToolTip, and any run style the caller did not repeat.
It also did not accept the shape ``get_textbox`` returns.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.reader import get_textbox
from pbirb_mcp.ops.styling import set_textbox_runs, set_textbox_style, set_textbox_value
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "pbi_paginated_minimal.rdl"

RICH = (
    "<Paragraphs><Paragraph><TextRuns><TextRun><Value>&lt;b&gt;One&lt;/b&gt;</Value>"
    "<MarkupType>HTML</MarkupType><ToolTip>tip</ToolTip><Style><Color>#FFFFFF</Color>"
    "</Style></TextRun></TextRuns><ListStyle>Bulleted</ListStyle><LeftIndent>10pt</LeftIndent>"
    "<Style><TextAlign>Center</TextAlign></Style></Paragraph>"
    "<Paragraph><TextRuns><TextRun><Value>Two</Value><Style /></TextRun></TextRuns><Style />"
    "</Paragraph></Paragraphs>"
)


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def rich_path(rdl_path: Path) -> Path:
    """The issue's repro textbox: HTML run with tooltip + colour in a
    bulleted, indented, centred paragraph, followed by a second paragraph."""
    add_body_textbox(str(rdl_path), "Notes", "x", "1.2in", "0.5in", "4in", "0.5in")
    s = rdl_path.read_text(encoding="utf-8")
    s = re.sub(
        r'(<Textbox Name="Notes">.*?)<Paragraphs>.*?</Paragraphs>',
        lambda m: m.group(1) + RICH,
        s,
        flags=re.S,
    )
    rdl_path.write_text(s, encoding="utf-8")
    return rdl_path


def _textbox(path: Path, name: str):
    doc = RDLDocument.open(path)
    return doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='{name}']")


def _paragraphs(tb):
    return find_children(find_child(tb, "Paragraphs"), "Paragraph")


def _runs(paragraph):
    return find_children(find_child(paragraph, "TextRuns"), "TextRun")


def _path_text(el, *path):
    for local in path:
        el = find_child(el, local) if el is not None else None
    return el.text if el is not None else None


# ---- the issue's repro ----------------------------------------------------


class TestRepro:
    def test_unaddressed_paragraph_refused_file_untouched(self, rich_path):
        before = rich_path.read_bytes()
        with pytest.raises(ValueError, match="paragraph 1"):
            set_textbox_runs(str(rich_path), "Notes", [{"text": "<b>One</b>"}])
        assert rich_path.read_bytes() == before

    def test_relabel_keeps_every_property(self, rich_path):
        result = set_textbox_runs(
            str(rich_path),
            "Notes",
            [{"text": "<b>Uno</b>"}, {"text": "Dos", "paragraph_index": 1}],
        )
        assert result["changed"] == ["Paragraphs"]
        assert result["paragraphs"] == 2
        tb = _textbox(rich_path, "Notes")
        p0, p1 = _paragraphs(tb)
        assert _path_text(p0, "Style", "TextAlign") == "Center"
        assert _path_text(p0, "ListStyle") == "Bulleted"
        assert _path_text(p0, "LeftIndent") == "10pt"
        (run,) = _runs(p0)
        assert _path_text(run, "Value") == "<b>Uno</b>"
        assert _path_text(run, "MarkupType") == "HTML"
        assert _path_text(run, "ToolTip") == "tip"
        assert _path_text(run, "Style", "Color") == "#FFFFFF"
        assert _path_text(_runs(p1)[0], "Value") == "Dos"
        assert validate_report(str(rich_path))["valid"]

    def test_drop_extra_paragraphs_keeps_first_paragraph_properties(self, rich_path):
        set_textbox_runs(str(rich_path), "Notes", [{"text": "Only"}], drop_extra_paragraphs=True)
        tb = _textbox(rich_path, "Notes")
        (p0,) = _paragraphs(tb)
        assert _path_text(p0, "Style", "TextAlign") == "Center"
        assert _path_text(p0, "ListStyle") == "Bulleted"
        assert _path_text(_runs(p0)[0], "MarkupType") == "HTML"
        assert validate_report(str(rich_path))["valid"]

    def test_get_runs_fed_back_is_a_no_op(self, rich_path):
        before = rich_path.read_bytes()
        runs = get_textbox(str(rich_path), "Notes")["runs"]
        assert runs[0]["markup_type"] == "HTML"
        assert [r["paragraph_index"] for r in runs] == [0, 1]
        result = set_textbox_runs(str(rich_path), "Notes", runs)
        assert result["changed"] == []
        assert rich_path.read_bytes() == before


# ---- acceptance cases -----------------------------------------------------


class TestPreservation:
    def test_centred_header_cell_keeps_alignment_after_relabel(self, rdl_path):
        set_textbox_style(str(rdl_path), "HeaderProductID", text_align="Center", font_weight="Bold")
        set_textbox_runs(str(rdl_path), "HeaderProductID", [{"text": "Product"}])
        tb = _textbox(rdl_path, "HeaderProductID")
        (p0,) = _paragraphs(tb)
        assert _path_text(p0, "Style", "TextAlign") == "Center"
        (run,) = _runs(p0)
        assert _path_text(run, "Value") == "Product"
        assert _path_text(run, "Style", "FontWeight") == "Bold"
        assert validate_report(str(rdl_path))["valid"]

    def test_html_markup_survives_relabel(self, rich_path):
        set_textbox_runs(
            str(rich_path),
            "Notes",
            [{"text": "<i>x</i>"}, {"text": "Two", "paragraph_index": 1}],
        )
        run = _runs(_paragraphs(_textbox(rich_path, "Notes"))[0])[0]
        assert _path_text(run, "MarkupType") == "HTML"

    def test_markup_type_can_be_set_and_removed(self, rdl_path):
        set_textbox_runs(
            str(rdl_path), "HeaderProductID", [{"text": "<b>x</b>", "markup_type": "HTML"}]
        )
        assert get_textbox(str(rdl_path), "HeaderProductID")["runs"][0]["markup_type"] == "HTML"
        set_textbox_runs(
            str(rdl_path), "HeaderProductID", [{"text": "<b>x</b>", "markup_type": None}]
        )
        assert "markup_type" not in get_textbox(str(rdl_path), "HeaderProductID")["runs"][0]
        assert validate_report(str(rdl_path))["valid"]

    def test_bad_markup_type_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="markup_type"):
            set_textbox_runs(str(rdl_path), "HeaderProductID", [{"text": "x", "markup_type": "Md"}])

    def test_style_key_null_removes_it(self, rdl_path):
        set_textbox_runs(str(rdl_path), "HeaderProductID", [{"text": "A", "font_weight": "Bold"}])
        set_textbox_runs(str(rdl_path), "HeaderProductID", [{"text": "A", "font_weight": None}])
        run = _runs(_paragraphs(_textbox(rdl_path, "HeaderProductID"))[0])[0]
        assert find_child(find_child(run, "Style"), "FontWeight") is None

    def test_get_textbox_style_shape_accepted(self, rdl_path):
        set_textbox_runs(
            str(rdl_path),
            "HeaderProductID",
            [{"value": "A", "style": {"FontWeight": "Bold", "FontSize": "11pt"}}],
        )
        run = get_textbox(str(rdl_path), "HeaderProductID")["runs"][0]
        assert run["value"] == "A"
        assert run["style"]["FontWeight"] == "Bold"
        assert run["style"]["FontSize"] == "11pt"

    def test_conflicting_style_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="FontWeight twice"):
            set_textbox_runs(
                str(rdl_path),
                "HeaderProductID",
                [{"text": "A", "font_weight": "Bold", "style": {"FontWeight": "Normal"}}],
            )

    def test_text_and_value_must_agree(self, rdl_path):
        with pytest.raises(ValueError, match="both 'text' and 'value'"):
            set_textbox_runs(str(rdl_path), "HeaderProductID", [{"text": "A", "value": "B"}])

    def test_surplus_runs_removed(self, rdl_path):
        set_textbox_runs(str(rdl_path), "HeaderProductID", [{"text": "A"}, {"text": "B"}])
        set_textbox_runs(str(rdl_path), "HeaderProductID", [{"text": "A"}])
        (p0,) = _paragraphs(_textbox(rdl_path, "HeaderProductID"))
        assert [_path_text(r, "Value") for r in _runs(p0)] == ["A"]
        assert validate_report(str(rdl_path))["valid"]


class TestMultiParagraph:
    def test_new_paragraph_inherits_previous_properties(self, rich_path):
        set_textbox_runs(
            str(rich_path),
            "Notes",
            [
                {"text": "One"},
                {"text": "Two", "paragraph_index": 1},
                {"text": "Three", "paragraph_index": 2},
            ],
        )
        paragraphs = _paragraphs(_textbox(rich_path, "Notes"))
        assert len(paragraphs) == 3
        assert [_path_text(_runs(p)[0], "Value") for p in paragraphs] == ["One", "Two", "Three"]
        # Paragraph 2 is new; it copies paragraph 1's (empty) properties.
        assert find_child(paragraphs[2], "ListStyle") is None
        assert validate_report(str(rich_path))["valid"]

    def test_new_paragraph_after_centred_one_is_centred(self, rdl_path):
        set_textbox_style(str(rdl_path), "HeaderProductID", text_align="Center")
        set_textbox_runs(
            str(rdl_path),
            "HeaderProductID",
            [{"text": "Line 1"}, {"text": "Line 2", "paragraph_index": 1}],
        )
        p0, p1 = _paragraphs(_textbox(rdl_path, "HeaderProductID"))
        assert _path_text(p1, "Style", "TextAlign") == "Center"
        assert validate_report(str(rdl_path))["valid"]

    def test_gap_in_new_paragraphs_refused(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="contiguous"):
            set_textbox_runs(
                str(rdl_path),
                "HeaderProductID",
                [{"text": "A"}, {"text": "C", "paragraph_index": 2}],
            )
        assert rdl_path.read_bytes() == before

    def test_set_textbox_value_redirect_mentions_paragraph_index(self, rich_path):
        with pytest.raises(ValueError, match="paragraph_index"):
            set_textbox_value(str(rich_path), "Notes", "y")


# ---- get -> set round trip over every fixture -----------------------------


def _fixture_textboxes():
    out = []
    for fixture in sorted(FIXTURES.glob("*.rdl")):
        tree = etree.parse(str(fixture))
        seen = set()
        for tb in tree.iter(q("Textbox")):
            name = tb.get("Name")
            if name in seen:
                continue
            seen.add(name)
            out.append(pytest.param(fixture, name, id=f"{fixture.stem}:{name}"))
    return out


@pytest.mark.parametrize("fixture, name", _fixture_textboxes())
def test_get_set_round_trip_is_a_no_op(tmp_path, fixture, name):
    path = tmp_path / fixture.name
    shutil.copy(fixture, path)
    before = path.read_bytes()
    runs = get_textbox(str(path), name)["runs"]
    if not runs:
        pytest.skip("textbox has no runs")
    result = set_textbox_runs(str(path), name, runs)
    assert result["changed"] == []
    assert path.read_bytes() == before


def test_round_trip_through_server_schema(rich_path):
    """The get_textbox shape passes the tool's strict inputSchema too."""
    srv = MCPServer()
    register_all_tools(srv)
    runs = get_textbox(str(rich_path), "Notes")["runs"]
    before = rich_path.read_bytes()
    resp = srv.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "set_textbox_runs",
                "arguments": {"path": str(rich_path), "textbox_name": "Notes", "runs": runs},
            },
        }
    )
    result = resp["result"]
    assert not result.get("isError"), result
    assert json.loads(result["content"][0]["text"])["changed"] == []
    assert rich_path.read_bytes() == before
