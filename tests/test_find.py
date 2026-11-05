"""find_textboxes_by_style compares normalised values; find_textbox_by_value
searches run values only (#107).

* Colours compare by value (``White`` == ``#FFFFFF`` == ``#ffffff``),
  sizes in points (``10pt`` == ``10.0pt``), enums case-insensitively.
* Any paragraph / run can match, and ``matched_at`` says which.
* ``effective=True`` lets an unset property match the RDL default.
* ``find_textbox_by_value`` no longer reports a drillthrough parameter's
  ``<Value>`` as the textbox's value; ``include`` opts other expressions in.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core import style_values
from pbirb_mcp.ops.actions import set_textbox_action, set_textbox_tooltip
from pbirb_mcp.ops.reader import find_textbox_by_value, find_textboxes_by_style
from pbirb_mcp.ops.styling import set_textbox_runs, set_textbox_style
from pbirb_mcp.ops.visibility import set_element_visibility
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
HEADERS = ["HeaderProductID", "HeaderProductName", "HeaderAmount"]
ALL = HEADERS + ["ProductID", "ProductName", "Amount"]


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def white_headers(rdl_path: Path) -> Path:
    for name in HEADERS:
        set_textbox_style(str(rdl_path), name, color="#ffffff", font_size="10.0pt")
    return rdl_path


def _names(hits) -> list[str]:
    return [h["name"] for h in hits]


class TestNormalisedComparison:
    @pytest.mark.parametrize("query", ["#ffffff", "#FFFFFF", "White", "white", "#FFFFFFFF"])
    def test_colour_spellings_all_match(self, white_headers, query):
        assert _names(find_textboxes_by_style(str(white_headers), color=query)) == HEADERS

    def test_other_colour_does_not_match(self, white_headers):
        assert find_textboxes_by_style(str(white_headers), color="WhiteSmoke") == []

    @pytest.mark.parametrize("query", ["10pt", "10.0pt", "10 pt"])
    def test_sizes_compare_in_points(self, white_headers, query):
        assert _names(find_textboxes_by_style(str(white_headers), font_size=query)) == HEADERS

    def test_size_in_other_unit(self, rdl_path):
        set_textbox_style(str(rdl_path), "Amount", padding_left="0.5in")
        hits = find_textboxes_by_style(str(rdl_path), padding_left="36pt")
        assert _names(hits) == ["Amount"]
        assert hits[0]["matched_fields"] == {"padding_left": "0.5in"}

    def test_enums_case_insensitive_and_numeric_weight(self, rdl_path):
        set_textbox_style(str(rdl_path), "Amount", font_weight="Bold", text_align="Right")
        assert _names(find_textboxes_by_style(str(rdl_path), font_weight="bold")) == ["Amount"]
        assert _names(find_textboxes_by_style(str(rdl_path), font_weight="700")) == ["Amount"]
        assert _names(find_textboxes_by_style(str(rdl_path), text_align="RIGHT")) == ["Amount"]

    def test_named_colour_table_covers_every_named_colour(self):
        for name in style_values.NAMED_COLORS:
            assert style_values.normalize_color(name).startswith("#"), name
        assert style_values.normalize_color("Transparent") == "#00ffffff"
        assert style_values.normalize_color("=Fields!C.Value") == "=Fields!C.Value"


class TestAnyRunOrParagraph:
    def test_second_run_matches_and_is_reported(self, rdl_path):
        set_textbox_runs(
            str(rdl_path),
            "HeaderAmount",
            [
                {"text": "Total: "},
                {"text": "=Sum(Fields!Amount.Value)", "color": "Red", "font_weight": "Bold"},
            ],
        )
        hits = find_textboxes_by_style(str(rdl_path), color="#FF0000", font_weight="Bold")
        assert _names(hits) == ["HeaderAmount"]
        assert hits[0]["matched_fields"] == {"font_weight": "Bold", "color": "Red"}
        assert hits[0]["matched_at"] == {
            "font_weight": {"paragraph_index": 0, "run_index": 1},
            "color": {"paragraph_index": 0, "run_index": 1},
        }

    def test_run_filters_must_hold_on_the_same_run(self, rdl_path):
        set_textbox_runs(
            str(rdl_path),
            "HeaderAmount",
            [{"text": "a", "color": "Red"}, {"text": "b", "font_weight": "Bold"}],
        )
        assert find_textboxes_by_style(str(rdl_path), color="Red", font_weight="Bold") == []

    def test_second_paragraph_matches(self, rdl_path):
        set_textbox_runs(
            str(rdl_path),
            "HeaderAmount",
            [{"text": "a"}, {"text": "b", "paragraph_index": 1, "color": "Blue"}],
        )
        hits = find_textboxes_by_style(str(rdl_path), color="blue")
        assert hits[0]["matched_at"] == {"color": {"paragraph_index": 1, "run_index": 0}}


class TestEffective:
    def test_unset_font_size_matches_default(self, white_headers):
        hits = find_textboxes_by_style(str(white_headers), font_size="10pt", effective=True)
        assert _names(hits) == ALL
        by_name = {h["name"]: h["matched_fields"]["font_size"] for h in hits}
        assert by_name["HeaderAmount"] == "10.0pt" and by_name["Amount"] == "10pt"

    def test_without_effective_unset_never_matches(self, rdl_path):
        assert find_textboxes_by_style(str(rdl_path), font_size="10pt") == []
        assert find_textboxes_by_style(str(rdl_path), color="Black") == []

    def test_effective_defaults(self, rdl_path):
        path = str(rdl_path)
        for kwargs in (
            {"font_family": "arial"},
            {"color": "#000000"},
            {"font_weight": "Normal"},
            {"text_align": "General"},
            {"vertical_align": "Top"},
            {"padding_top": "0in"},
            {"border_style": "None"},
        ):
            assert _names(find_textboxes_by_style(path, effective=True, **kwargs)) == ALL, kwargs

    def test_explicit_value_beats_default(self, white_headers):
        hits = find_textboxes_by_style(str(white_headers), color="Black", effective=True)
        assert _names(hits) == ["ProductID", "ProductName", "Amount"]

    def test_effective_must_be_boolean(self, rdl_path):
        with pytest.raises(ValueError, match="effective"):
            find_textboxes_by_style(str(rdl_path), color="Red", effective="yes")


class TestFindByValue:
    def test_drillthrough_parameter_is_not_the_value(self, rdl_path):
        set_textbox_action(
            str(rdl_path),
            "ProductID",
            "Drillthrough",
            "Detail",
            [{"name": "Id", "value": "=Fields!ProductID.Value"}],
        )
        hits = find_textbox_by_value(str(rdl_path), r"Fields!ProductID\.Value")
        assert hits == [
            {
                "textbox": "ProductID",
                "value": "=Fields!ProductID.Value",
                "region": "Body",
                "kind": "value",
            }
        ]
        with_action = find_textbox_by_value(
            str(rdl_path), r"Fields!ProductID\.Value", include=["action"]
        )
        assert [h["kind"] for h in with_action] == ["value", "action"]
        assert find_textbox_by_value(str(rdl_path), "^Detail$", include=["action"])[0]["kind"] == (
            "action"
        )

    def test_tooltip_and_visibility_opt_in(self, rdl_path):
        set_textbox_tooltip(str(rdl_path), "Amount", "=Parameters!Tip.Value")
        set_element_visibility(str(rdl_path), "Amount", "=Parameters!Hide.Value")
        assert find_textbox_by_value(str(rdl_path), "Parameters!") == []
        hits = find_textbox_by_value(
            str(rdl_path), "Parameters!", include=["tooltip", "visibility"]
        )
        assert [(h["textbox"], h["kind"]) for h in hits] == [
            ("Amount", "tooltip"),
            ("Amount", "visibility"),
        ]

    def test_unknown_include_refused(self, rdl_path):
        with pytest.raises(ValueError, match="include"):
            find_textbox_by_value(str(rdl_path), "x", include=["label"])

    def test_multi_run_textbox_once_per_run(self, rdl_path):
        set_textbox_runs(str(rdl_path), "Amount", [{"text": "=A"}, {"text": "=AB"}])
        hits = find_textbox_by_value(str(rdl_path), "^=A")
        assert [h["value"] for h in hits] == ["=A", "=AB"]


def test_server_accepts_new_arguments(white_headers):
    srv = MCPServer()
    register_all_tools(srv)

    def call(name, **arguments):
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": {"path": str(white_headers), **arguments}},
            }
        )
        result = resp["result"]
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    style_hits = call("find_textboxes_by_style", font_size="10pt", effective=True)
    assert len(style_hits) == 6
    call("find_textbox_by_value", pattern="x", include=["tooltip", "action"])
