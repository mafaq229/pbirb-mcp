"""Textbox-styling tool tests.

``set_textbox_style`` writes properties across the three Style nodes
nested inside a Textbox:

- ``Textbox/Style``         — box-level (BackgroundColor, Border, VerticalAlign)
- ``Paragraph/Style``       — paragraph-level (TextAlign)
- ``TextRun/Style``         — run-level (FontFamily, FontSize, FontWeight, Color, Format)

Each Property is independently optional; only fields the caller passes
are written, leaving everything else untouched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.reader import find_textboxes_by_style
from pbirb_mcp.ops.styling import (
    set_textbox_runs,
    set_textbox_style,
    set_textbox_style_bulk,
    set_textbox_value,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _textbox(rdl_path: Path, name: str):
    doc = RDLDocument.open(rdl_path)
    return doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='{name}']")


# ---- box-level properties -------------------------------------------------


class TestBoxLevelProps:
    def test_background_color_lands_on_textbox_style(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#003366",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        outer_style = find_child(tb, "Style")
        bg = find_child(outer_style, "BackgroundColor")
        assert bg is not None and bg.text == "#003366"

    def test_border_props_land_on_textbox_style_border(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            border_style="Solid",
            border_color="#000000",
            border_width="1pt",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        border = tb.find(f"{q('Style')}/{q('Border')}")
        assert border is not None
        assert find_child(border, "Style").text == "Solid"
        assert find_child(border, "Color").text == "#000000"
        assert find_child(border, "Width").text == "1pt"

    def test_vertical_align_lands_on_textbox_style(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="Amount",
            vertical_align="Middle",
        )
        tb = _textbox(rdl_path, "Amount")
        va = tb.find(f"{q('Style')}/{q('VerticalAlign')}")
        assert va is not None and va.text == "Middle"


# ---- paragraph-level properties ------------------------------------------


class TestParagraphLevelProps:
    def test_text_align_lands_on_paragraph_style(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="Amount",
            text_align="Right",
        )
        tb = _textbox(rdl_path, "Amount")
        # Paragraph/Style/TextAlign — NOT Textbox/Style/TextAlign.
        ta = tb.find(f"{q('Paragraphs')}/{q('Paragraph')}/{q('Style')}/{q('TextAlign')}")
        assert ta is not None and ta.text == "Right"
        # And specifically NOT on Textbox/Style.
        outer = tb.find(f"{q('Style')}/{q('TextAlign')}")
        assert outer is None


# ---- run-level properties (font, color, format) -------------------------


class TestRunLevelProps:
    def test_font_props_land_on_textrun_style(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            font_family="Segoe UI",
            font_size="11pt",
            font_weight="Bold",
            color="#FFFFFF",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        run_style = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Style')}"
        )
        assert find_child(run_style, "FontFamily").text == "Segoe UI"
        assert find_child(run_style, "FontSize").text == "11pt"
        assert find_child(run_style, "FontWeight").text == "Bold"
        assert find_child(run_style, "Color").text == "#FFFFFF"

    def test_format_lands_on_textrun_style(self, rdl_path):
        # Fixture's Amount cell already has a Format — overwriting it is the
        # canonical "change number format" workflow.
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="Amount",
            format="C2",
        )
        tb = _textbox(rdl_path, "Amount")
        fmt = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/"
            f"{q('TextRun')}/{q('Style')}/{q('Format')}"
        )
        assert fmt is not None and fmt.text == "C2"


# ---- partial-update / no-op behaviour ------------------------------------


class TestPartialUpdate:
    def test_only_specified_props_are_written(self, rdl_path):
        # Set background only; verify other Style children are unchanged.
        before_outer = _textbox(rdl_path, "HeaderProductID").find(q("Style"))
        before_children = [etree_node.tag for etree_node in list(before_outer)]
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#003366",
        )
        outer = _textbox(rdl_path, "HeaderProductID").find(q("Style"))
        # Old children still there, BackgroundColor newly added.
        for child_tag in before_children:
            assert any(c.tag == child_tag for c in outer)
        assert any(c.tag.endswith("}BackgroundColor") for c in outer)

    def test_no_args_is_no_op(self, rdl_path):
        before = rdl_path.read_bytes()
        set_textbox_style(path=str(rdl_path), textbox_name="HeaderProductID")
        assert rdl_path.read_bytes() == before

    def test_identical_repeat_reports_no_change_and_keeps_file(self, rdl_path):
        # #61: a second identical call must not report (or write) a change.
        kwargs = dict(
            textbox_name="HeaderAmount",
            font_weight="Bold",
            background_color="#EEEEEE",
            border_style="Solid",
            text_align="Right",
        )
        first = set_textbox_style(path=str(rdl_path), **kwargs)
        assert "run.FontWeight" in first["changed"]
        st = rdl_path.stat()
        before = (rdl_path.read_bytes(), st.st_ino, st.st_mtime_ns)
        assert set_textbox_style(path=str(rdl_path), **kwargs)["changed"] == []
        st = rdl_path.stat()
        assert (rdl_path.read_bytes(), st.st_ino, st.st_mtime_ns) == before

    def test_changed_lists_only_fields_that_differ(self, rdl_path):
        set_textbox_style(path=str(rdl_path), textbox_name="HeaderAmount", font_weight="Bold")
        result = set_textbox_style(
            path=str(rdl_path), textbox_name="HeaderAmount", font_weight="Bold", color="Red"
        )
        assert result["changed"] == ["run.Color"]

    def test_repeat_set_overwrites_in_place(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#111111",
        )
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#222222",
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        bgs = tb.findall(f"{q('Style')}/{q('BackgroundColor')}")
        # Exactly one BackgroundColor element with the latest value.
        assert len(bgs) == 1
        assert bgs[0].text == "#222222"


# ---- v0.3 box extensions: padding + writing_mode ------------------------


class TestPaddingAndWritingMode:
    def test_padding_top_lands_on_box_style(self, rdl_path):
        result = set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            padding_top="3pt",
        )
        assert "box.PaddingTop" in result["changed"]
        tb = _textbox(rdl_path, "HeaderProductID")
        node = tb.find(f"{q('Style')}/{q('PaddingTop')}")
        assert node is not None and node.text == "3pt"

    def test_all_four_paddings(self, rdl_path):
        result = set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            padding_top="2pt",
            padding_bottom="2pt",
            padding_left="4pt",
            padding_right="4pt",
        )
        assert set(result["changed"]) == {
            "box.PaddingTop",
            "box.PaddingBottom",
            "box.PaddingLeft",
            "box.PaddingRight",
        }
        tb = _textbox(rdl_path, "HeaderProductID")
        style = find_child(tb, "Style")
        assert find_child(style, "PaddingTop").text == "2pt"
        assert find_child(style, "PaddingBottom").text == "2pt"
        assert find_child(style, "PaddingLeft").text == "4pt"
        assert find_child(style, "PaddingRight").text == "4pt"

    def test_writing_mode_rotate(self, rdl_path):
        result = set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            writing_mode="Rotate270",
        )
        assert "box.WritingMode" in result["changed"]
        tb = _textbox(rdl_path, "HeaderProductID")
        wm = tb.find(f"{q('Style')}/{q('WritingMode')}")
        assert wm is not None and wm.text == "Rotate270"


# ---- v0.3 direct-Textbox-children: can_grow / can_shrink ----------------


class TestCanGrowCanShrink:
    def test_can_grow_flips_existing_value(self, rdl_path):
        # Fixture's HeaderProductID has <CanGrow>true</CanGrow> baked in
        # by the template builder. Flipping to false is a real change.
        result = set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            can_grow=False,
        )
        assert "textbox.CanGrow" in result["changed"]
        tb = _textbox(rdl_path, "HeaderProductID")
        # CanGrow is a DIRECT child of Textbox, NOT inside Style.
        cg = find_child(tb, "CanGrow")
        assert cg is not None and cg.text == "false"
        # Confirm it's NOT inside Style.
        style = find_child(tb, "Style")
        if style is not None:
            assert find_child(style, "CanGrow") is None

    def test_can_grow_no_op_when_already_set(self, rdl_path):
        # Fixture's HeaderProductID has CanGrow=true. Setting to true
        # again is a no-op short-circuit.
        result = set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            can_grow=True,
        )
        assert "textbox.CanGrow" not in result["changed"]

    def test_can_shrink_added_when_absent(self, rdl_path):
        # Fixture's HeaderProductID has CanGrow but no CanShrink — adding
        # one is a real change.
        result = set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            can_shrink=False,
        )
        assert "textbox.CanShrink" in result["changed"]
        tb = _textbox(rdl_path, "HeaderProductID")
        cs = find_child(tb, "CanShrink")
        assert cs is not None and cs.text == "false"

    def test_can_grow_position_before_paragraphs(self, rdl_path):
        """RDL XSD requires CanGrow/CanShrink/KeepTogether to come BEFORE
        <Paragraphs>. Our writer must respect that ordering."""
        from lxml import etree as _etree

        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            can_grow=True,
            can_shrink=True,
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        children_locals = [_etree.QName(c).localname for c in tb]
        cg_idx = children_locals.index("CanGrow")
        cs_idx = children_locals.index("CanShrink")
        # Paragraphs index: must be greater than both CanGrow/CanShrink.
        para_idx = children_locals.index("Paragraphs")
        assert cg_idx < para_idx
        assert cs_idx < para_idx


# ---- v0.3 set_textbox_runs (rich text / multi-run) -----------------------


class TestSetTextboxRuns:
    def test_replaces_paragraphs_with_multiple_runs(self, rdl_path):
        result = set_textbox_runs(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            runs=[
                {"text": "Asset(s): ", "font_weight": "Bold"},
                {"text": "0KLLu6BqeVkJ2ZLmXH20", "font_weight": "Normal"},
            ],
        )
        assert result["runs"] == 2
        assert result["changed"] == ["Paragraphs"]
        # Re-parse and verify the run count + values.
        tb = _textbox(rdl_path, "HeaderProductID")
        runs = tb.findall(f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}")
        assert len(runs) == 2
        values = [r.find(q("Value")).text for r in runs]
        assert values == ["Asset(s): ", "0KLLu6BqeVkJ2ZLmXH20"]

    def test_per_run_styles_applied(self, rdl_path):
        set_textbox_runs(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            runs=[
                {"text": "Bold ", "font_weight": "Bold", "color": "#FF0000"},
                {"text": "italic", "font_style": "Italic", "font_size": "9pt"},
            ],
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        runs = tb.findall(f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}")
        # First run: Bold + red
        s0 = find_child(runs[0], "Style")
        assert find_child(s0, "FontWeight").text == "Bold"
        assert find_child(s0, "Color").text == "#FF0000"
        # Second run: Italic + 9pt
        s1 = find_child(runs[1], "Style")
        assert find_child(s1, "FontStyle").text == "Italic"
        assert find_child(s1, "FontSize").text == "9pt"

    def test_single_run_replaces_existing(self, rdl_path):
        # Fixture's HeaderProductID starts with one run holding 'ProductID'.
        set_textbox_runs(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            runs=[{"text": "Replaced"}],
        )
        tb = _textbox(rdl_path, "HeaderProductID")
        runs = tb.findall(f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}")
        assert len(runs) == 1
        assert runs[0].find(q("Value")).text == "Replaced"

    def test_round_trip_via_get_textbox(self, rdl_path):
        from pbirb_mcp.ops.reader import get_textbox

        set_textbox_runs(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            runs=[
                {"text": "A", "font_weight": "Bold"},
                {"text": "B", "color": "#00FF00", "font_size": "10pt"},
            ],
        )
        gt = get_textbox(path=str(rdl_path), name="HeaderProductID")
        assert len(gt["runs"]) == 2
        assert gt["runs"][0]["value"] == "A"
        assert gt["runs"][0]["style"]["FontWeight"] == "Bold"
        assert gt["runs"][1]["value"] == "B"
        assert gt["runs"][1]["style"]["Color"] == "#00FF00"
        assert gt["runs"][1]["style"]["FontSize"] == "10pt"

    def test_idempotent_no_op_on_identical_input(self, rdl_path):
        set_textbox_runs(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            runs=[{"text": "Header"}],
        )
        before = (rdl_path).read_bytes()
        result = set_textbox_runs(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            runs=[{"text": "Header"}],
        )
        assert result["changed"] == []
        # Idempotent → file untouched.
        assert (rdl_path).read_bytes() == before

    def test_pre_encoded_text_does_not_double_encode(self, rdl_path):
        # Bug class regression: pass already-encoded entities; saved
        # bytes must contain &amp; not &amp;amp;.
        set_textbox_runs(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            runs=[{"text": "A &amp; B"}],
        )
        assert b"&amp;amp;" not in (rdl_path).read_bytes()
        assert b"A &amp; B" in (rdl_path).read_bytes()

    def test_empty_runs_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="non-empty"):
            set_textbox_runs(
                path=str(rdl_path),
                textbox_name="HeaderProductID",
                runs=[],
            )

    def test_non_dict_run_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="must be a dict"):
            set_textbox_runs(
                path=str(rdl_path),
                textbox_name="HeaderProductID",
                runs=["just a string"],  # type: ignore[list-item]
            )

    def test_missing_text_key_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="missing required key 'text'"):
            set_textbox_runs(
                path=str(rdl_path),
                textbox_name="HeaderProductID",
                runs=[{"font_weight": "Bold"}],
            )

    def test_unknown_run_key_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="unrecognised keys"):
            set_textbox_runs(
                path=str(rdl_path),
                textbox_name="HeaderProductID",
                runs=[{"text": "X", "garbage_key": "y"}],
            )

    def test_unknown_textbox_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_textbox_runs(
                path=str(rdl_path),
                textbox_name="NoSuchBox",
                runs=[{"text": "X"}],
            )

    def test_round_trip_safe(self, rdl_path):
        set_textbox_runs(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            runs=[
                {"text": "A", "font_weight": "Bold"},
                {"text": "B"},
                {"text": "C", "color": "#0000FF"},
            ],
        )
        RDLDocument.open(rdl_path).validate()


# ---- v0.3 set_textbox_value (single-run content editor) -----------------


class TestSetTextboxValue:
    def test_replaces_existing_literal(self, rdl_path):
        # Fixture's HeaderProductID Value is "ProductID".
        result = set_textbox_value(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            value="Product ID Header",
        )
        assert result["changed"] is True
        tb = _textbox(rdl_path, "HeaderProductID")
        v = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert v is not None and v.text == "Product ID Header"

    def test_replaces_with_expression(self, rdl_path):
        result = set_textbox_value(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            value="=Parameters!DateFrom.Value",
        )
        assert result["changed"] is True
        tb = _textbox(rdl_path, "HeaderProductID")
        v = tb.find(
            f"{q('Paragraphs')}/{q('Paragraph')}/{q('TextRuns')}/{q('TextRun')}/{q('Value')}"
        )
        assert v is not None and v.text == "=Parameters!DateFrom.Value"

    def test_idempotent_when_unchanged(self, rdl_path):
        # Fixture's HeaderProductID Value is "Product ID".
        before = (rdl_path).read_bytes()
        result = set_textbox_value(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            value="Product ID",
        )
        assert result["changed"] is False
        # No save when unchanged.
        assert (rdl_path).read_bytes() == before

    def test_pre_encoded_text_no_double_encode(self, rdl_path):
        # Bug class regression: pre-encoded entity must end up as
        # &amp; on disk, not &amp;amp;.
        set_textbox_value(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            value="A &amp; B",
        )
        assert b"&amp;amp;" not in (rdl_path).read_bytes()
        assert b"A &amp; B" in (rdl_path).read_bytes()

    def test_refuses_multi_run_textbox(self, rdl_path):
        # First create a multi-run textbox via set_textbox_runs.
        set_textbox_runs(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            runs=[
                {"text": "Part 1: "},
                {"text": "Part 2"},
            ],
        )
        # set_textbox_value must refuse with a clear redirect.
        with pytest.raises(ValueError, match="set_textbox_runs"):
            set_textbox_value(
                path=str(rdl_path),
                textbox_name="HeaderProductID",
                value="Replaced",
            )

    def test_unknown_textbox_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_textbox_value(
                path=str(rdl_path),
                textbox_name="NoSuchBox",
                value="X",
            )

    def test_round_trip_safe(self, rdl_path):
        set_textbox_value(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            value="Round-tripped",
        )
        RDLDocument.open(rdl_path).validate()


# ---- v0.3 set_textbox_style_bulk ----------------------------------------


class TestSetTextboxStyleBulk:
    def test_applies_to_every_named_textbox(self, rdl_path):
        result = set_textbox_style_bulk(
            path=str(rdl_path),
            textbox_names=["HeaderProductID", "HeaderProductName", "HeaderAmount"],
            background_color="#003366",
            color="#FFFFFF",
            font_weight="Bold",
        )
        assert sorted(result["textboxes"]) == sorted(
            ["HeaderProductID", "HeaderProductName", "HeaderAmount"]
        )
        assert result["skipped"] == []
        # Union of changed fields across the three textboxes.
        assert "box.BackgroundColor" in result["changed"]
        assert "run.Color" in result["changed"]
        assert "run.FontWeight" in result["changed"]
        # Verify the actual XML.
        for name in ["HeaderProductID", "HeaderProductName", "HeaderAmount"]:
            tb = _textbox(rdl_path, name)
            assert find_child(find_child(tb, "Style"), "BackgroundColor").text == "#003366"

    def test_missing_textboxes_skipped_not_raised(self, rdl_path):
        result = set_textbox_style_bulk(
            path=str(rdl_path),
            textbox_names=["HeaderProductID", "DoesNotExist", "HeaderAmount"],
            background_color="#FF0000",
        )
        assert sorted(result["textboxes"]) == sorted(["HeaderProductID", "HeaderAmount"])
        assert result["skipped"] == ["DoesNotExist"]
        # The two existing textboxes were styled.
        for name in ["HeaderProductID", "HeaderAmount"]:
            tb = _textbox(rdl_path, name)
            assert find_child(find_child(tb, "Style"), "BackgroundColor").text == "#FF0000"

    def test_empty_names_no_op(self, rdl_path):
        before = (rdl_path).read_bytes()
        result = set_textbox_style_bulk(
            path=str(rdl_path),
            textbox_names=[],
            background_color="#000000",
        )
        assert result == {"textboxes": [], "skipped": [], "changed": []}
        assert (rdl_path).read_bytes() == before

    def test_no_style_kwargs_no_op(self, rdl_path):
        before = (rdl_path).read_bytes()
        result = set_textbox_style_bulk(
            path=str(rdl_path),
            textbox_names=["HeaderProductID", "HeaderAmount"],
        )
        # Names recorded but no changes made.
        assert sorted(result["textboxes"]) == sorted(["HeaderProductID", "HeaderAmount"])
        assert result["changed"] == []
        assert (rdl_path).read_bytes() == before


# ---- v0.3 find_textboxes_by_style ----------------------------------------


class TestFindTextboxesByStyle:
    def test_finds_match_after_bulk_apply(self, rdl_path):
        # Style three textboxes red, then search.
        set_textbox_style_bulk(
            path=str(rdl_path),
            textbox_names=["HeaderProductID", "HeaderProductName"],
            color="#FF0000",
        )
        results = find_textboxes_by_style(
            path=str(rdl_path),
            color="#FF0000",
        )
        names = {r["name"] for r in results}
        assert names == {"HeaderProductID", "HeaderProductName"}
        # Each result includes location + matched_fields.
        for r in results:
            assert r["matched_fields"] == {"color": "#FF0000"}
            assert r["location"].startswith("tablix:")

    def test_returns_empty_when_no_filters(self, rdl_path):
        # No filters supplied → empty list (refusing to match all is safer).
        assert find_textboxes_by_style(path=str(rdl_path)) == []

    def test_combines_filters_with_AND(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            color="#FF0000",
            font_weight="Bold",
        )
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductName",
            color="#FF0000",  # red but not bold
        )
        # Filter on both: only HeaderProductID matches.
        results = find_textboxes_by_style(
            path=str(rdl_path),
            color="#FF0000",
            font_weight="Bold",
        )
        names = {r["name"] for r in results}
        assert names == {"HeaderProductID"}

    def test_box_level_filter(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#FFFF00",
        )
        results = find_textboxes_by_style(
            path=str(rdl_path),
            background_color="#FFFF00",
        )
        assert {r["name"] for r in results} == {"HeaderProductID"}

    def test_recolor_pattern(self, rdl_path):
        """End-to-end: find every red textbox, recolor to black except one."""
        # Set up: 3 red textboxes.
        set_textbox_style_bulk(
            path=str(rdl_path),
            textbox_names=["HeaderProductID", "HeaderProductName", "HeaderAmount"],
            color="#FF0000",
        )
        # Find them and exclude one.
        red = find_textboxes_by_style(path=str(rdl_path), color="#FF0000")
        names_to_recolor = [r["name"] for r in red if r["name"] != "HeaderAmount"]
        # Recolor in bulk.
        result = set_textbox_style_bulk(
            path=str(rdl_path),
            textbox_names=names_to_recolor,
            color="#000000",
        )
        assert "run.Color" in result["changed"]
        # Verify final state: 1 red, 2 black.
        red_after = find_textboxes_by_style(path=str(rdl_path), color="#FF0000")
        black_after = find_textboxes_by_style(path=str(rdl_path), color="#000000")
        assert {r["name"] for r in red_after} == {"HeaderAmount"}
        assert {r["name"] for r in black_after} == {"HeaderProductID", "HeaderProductName"}


# ---- error paths ----------------------------------------------------------


class TestErrors:
    def test_unknown_textbox_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_textbox_style(
                path=str(rdl_path),
                textbox_name="NoSuchBox",
                background_color="#000000",
            )

    def test_round_trip_safe(self, rdl_path):
        set_textbox_style(
            path=str(rdl_path),
            textbox_name="HeaderProductID",
            background_color="#003366",
            font_weight="Bold",
            color="#FFFFFF",
            text_align="Center",
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "set_textbox_style" in names

    def test_set_textbox_runs_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "set_textbox_runs" in names

    def test_set_textbox_value_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "set_textbox_value" in names

    def test_bulk_and_find_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {"set_textbox_style_bulk", "find_textboxes_by_style"} <= names


# ---- #82: every run / paragraph, literal validation, font_style ---------------


def _two_run_textbox(rdl_path: Path) -> str:
    from pbirb_mcp.ops.body import add_body_textbox

    add_body_textbox(str(rdl_path), "Period", "x", "0.1in", "0.5in", "4in", "0.25in")
    set_textbox_runs(
        str(rdl_path),
        "Period",
        [
            {"text": "Period: ", "font_weight": "Bold"},
            {"text": "=Parameters!DateFrom.Value"},
            {"text": "second paragraph", "paragraph_index": 1},
        ],
    )
    return "Period"


def _run_styles(rdl_path: Path, name: str) -> list[dict]:
    from pbirb_mcp.ops.reader import get_textbox

    return [r.get("style") or {} for r in get_textbox(str(rdl_path), name)["runs"]]


_BAD_VALUES = [
    ("color", "notacolor", "named color"),
    ("background_color", "#GGGGGG", "#RRGGBB"),
    ("border_color", "#12345", "#AARRGGBB"),
    ("text_align", "Middle", "General, Left, Center, Right, Justify"),
    ("vertical_align", "Center", "Top, Middle, Bottom"),
    ("writing_mode", "Vertical90", "Horizontal, Vertical, Rotate270"),
    ("font_weight", "bolder", "Thin, ExtraLight"),
    ("font_weight", "750", "100 to 900"),
    ("font_style", "Oblique", "Normal, Italic"),
    ("text_decoration", "Strike", "None, Underline, Overline, LineThrough"),
    ("border_style", "Wavy", "Solid"),
]


class TestEveryRunAndParagraph:
    def test_run_props_reach_every_run_in_every_paragraph(self, rdl_path):
        name = _two_run_textbox(rdl_path)
        out = set_textbox_style(str(rdl_path), name, color="#FF0000", font_size="14pt")
        assert out["changed"] == ["run.FontSize", "run.Color"]
        styles = _run_styles(rdl_path, name)
        assert len(styles) == 3
        for style in styles:
            assert style["Color"] == "#FF0000" and style["FontSize"] == "14pt"
        assert styles[0]["FontWeight"] == "Bold"  # untouched run style kept
        assert "FontWeight" not in styles[1]

    def test_text_align_reaches_every_paragraph(self, rdl_path):
        name = _two_run_textbox(rdl_path)
        set_textbox_style(str(rdl_path), name, text_align="Right")
        tb = _textbox(rdl_path, name)
        paragraphs = tb.findall(f"{q('Paragraphs')}/{q('Paragraph')}")
        assert len(paragraphs) == 2
        for p in paragraphs:
            assert p.find(f"{q('Style')}/{q('TextAlign')}").text == "Right"

    def test_repeat_is_a_no_op(self, rdl_path):
        name = _two_run_textbox(rdl_path)
        set_textbox_style(str(rdl_path), name, color="Red", text_align="Center")
        before = rdl_path.read_bytes()
        out = set_textbox_style(str(rdl_path), name, color="Red", text_align="Center")
        assert out["changed"] == [] and rdl_path.read_bytes() == before

    def test_bulk_and_row_style_every_run(self, rdl_path):
        from pbirb_mcp.ops.styling import style_tablix_row

        name = _two_run_textbox(rdl_path)
        out = set_textbox_style_bulk(str(rdl_path), [name, "Missing"], font_style="Italic")
        assert out == {"textboxes": [name], "skipped": ["Missing"], "changed": ["run.FontStyle"]}
        assert all(s["FontStyle"] == "Italic" for s in _run_styles(rdl_path, name))
        row = style_tablix_row(str(rdl_path), "MainTable", 0, text_decoration="Underline")
        assert row["changed"] == ["run.TextDecoration"]
        assert _run_styles(rdl_path, "HeaderProductID")[0]["TextDecoration"] == "Underline"

    def test_still_xsd_valid(self, rdl_path):
        from pbirb_mcp.ops.validate import validate_report

        name = _two_run_textbox(rdl_path)
        set_textbox_style(
            str(rdl_path), name, color="Blue", text_align="Center", font_style="Italic"
        )
        assert validate_report(str(rdl_path))["valid"]


class TestLiteralValidation:
    @pytest.mark.parametrize("field,value,hint", _BAD_VALUES)
    def test_bad_literal_refused_file_unchanged(self, rdl_path, field, value, hint):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=f"^{field}: ") as exc:
            set_textbox_style(str(rdl_path), "ProductID", **{field: value})
        assert hint in str(exc.value)
        assert rdl_path.read_bytes() == before

    @pytest.mark.parametrize("field,value,hint", _BAD_VALUES)
    def test_bulk_and_row_refuse_the_same(self, rdl_path, field, value, hint):
        from pbirb_mcp.ops.styling import style_tablix_row

        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=f"^{field}: "):
            set_textbox_style_bulk(str(rdl_path), ["ProductID"], **{field: value})
        with pytest.raises(ValueError, match=f"^{field}: "):
            style_tablix_row(str(rdl_path), "MainTable", 0, **{field: value})
        assert rdl_path.read_bytes() == before

    @pytest.mark.parametrize(
        "field",
        sorted({f for f, _v, _h in _BAD_VALUES} | {"font_size", "padding_top", "border_width"}),
    )
    def test_expression_accepted_for_every_field(self, rdl_path, field):
        expr = '=IIf(Fields!Amount.Value > 0, "a", "b")'
        out = set_textbox_style(str(rdl_path), "ProductID", **{field: expr})
        assert out["changed"]

    @pytest.mark.parametrize(
        "field,value,written",
        [
            ("color", "steelblue", "steelblue"),
            ("color", "#80FF0000", "#80FF0000"),
            ("background_color", "Transparent", "Transparent"),
            ("text_align", "center", "Center"),
            ("font_weight", "700", "Bold"),
            ("font_weight", "semibold", "SemiBold"),
            ("border_style", "dashdot", "DashDot"),
            ("writing_mode", "Rotate270", "Rotate270"),
        ],
    )
    def test_valid_literals_and_canonical_spelling(self, rdl_path, field, value, written):
        from pbirb_mcp.ops.reader import get_textbox

        set_textbox_style(str(rdl_path), "ProductID", **{field: value})
        flat: dict = {}
        for branch in get_textbox(str(rdl_path), "ProductID")["style"].values():
            flat.update(branch)
        assert written in flat.values()

    def test_runs_validate_style_literals(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=r"^runs\[0\]\.color: "):
            set_textbox_runs(str(rdl_path), "ProductID", [{"text": "x", "color": "nope"}])
        with pytest.raises(ValueError, match=r"^runs\[1\]\.font_style: "):
            set_textbox_runs(
                str(rdl_path),
                "ProductID",
                [{"text": "x"}, {"text": "y", "style": {"FontStyle": "Slanted"}}],
            )
        assert rdl_path.read_bytes() == before
        set_textbox_runs(str(rdl_path), "ProductID", [{"text": "x", "font_weight": "bold"}])
        assert _run_styles(rdl_path, "ProductID")[0]["FontWeight"] == "Bold"


class TestFontStyleAndDecoration:
    def test_font_style_writes_textrun_style(self, rdl_path):
        out = set_textbox_style(str(rdl_path), "ProductID", font_style="Italic")
        assert out["changed"] == ["run.FontStyle"]
        tb = _textbox(rdl_path, "ProductID")
        run_style = tb.find(f".//{q('TextRun')}/{q('Style')}")
        assert find_child(run_style, "FontStyle").text == "Italic"

    def test_text_decoration_in_xsd_order(self, rdl_path):
        from pbirb_mcp.ops.validate import validate_report

        set_textbox_style(
            str(rdl_path), "ProductID", text_decoration="Underline", font_family="Arial"
        )
        tb = _textbox(rdl_path, "ProductID")
        run_style = tb.find(f".//{q('TextRun')}/{q('Style')}")
        locals_ = [c.tag.split("}")[1] for c in run_style]
        assert locals_.index("FontFamily") < locals_.index("TextDecoration")
        assert validate_report(str(rdl_path))["valid"]

    def test_schemas_publish_the_new_kwargs(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        by_name = {t["name"]: t for t in listing}
        for tool in ("set_textbox_style", "set_textbox_style_bulk", "style_tablix_row"):
            props = by_name[tool]["inputSchema"]["properties"]
            assert {"font_style", "text_decoration"} <= set(props), tool

    def test_bulk_rejects_unknown_kwarg(self, rdl_path):
        with pytest.raises(TypeError, match="font_colour"):
            set_textbox_style_bulk(str(rdl_path), ["ProductID"], font_colour="Red")


# ---- #90: row roles, row-colour escaping and composition -----------------


def _patched_fixture(tmp_path: Path, old: str, new: str) -> Path:
    text = FIXTURE.read_text(encoding="utf-8")
    assert text.count(old) == 1
    dest = tmp_path / "patched.rdl"
    dest.write_text(text.replace(old, new), encoding="utf-8")
    return dest


def _bg(rdl_path: Path, name: str):
    style = find_child(_textbox(rdl_path, name), "Style")
    node = find_child(style, "BackgroundColor") if style is not None else None
    return node.text if node is not None else None


# The usual Report Builder shape for a group without details: the group
# member wraps a single static leaf that is its data row.
_GROUP_ONLY_DATA = (
    '<Group Name="Product"><GroupExpressions><GroupExpression>=1</GroupExpression>'
    "</GroupExpressions></Group><TablixMembers><TablixMember /></TablixMembers>"
)


class TestRowRoles90:
    def test_header_after_add_row_group_is_column_header(self, rdl_path):
        from pbirb_mcp.ops.styling import style_tablix_row
        from pbirb_mcp.ops.tablix import add_row_group

        add_row_group(str(rdl_path), "MainTable", "G", "=1")
        out = style_tablix_row(str(rdl_path), "MainTable", "header", font_weight="Bold")
        assert out["row_index"] == 0
        assert out["cells"] == ["HeaderProductID", "HeaderProductName", "HeaderAmount"]
        # The group's own header row (also KeepWithGroup=After) is its own role.
        group = style_tablix_row(str(rdl_path), "MainTable", "G_header", font_weight="Bold")
        alias = style_tablix_row(str(rdl_path), "MainTable", "G_group_header", color="Red")
        assert group["row_index"] == alias["row_index"] == 1
        assert group["cells"][0].startswith("G_Header_")

    def test_header_is_last_marked_static_row_above_the_groups(self, tmp_path):
        from pbirb_mcp.ops.styling import _resolve_row_index

        # A title row above the column header: both marked KeepWithGroup=After.
        path = _patched_fixture(
            tmp_path,
            "<TablixMember>\n                  <KeepWithGroup>After</KeepWithGroup>",
            "<TablixMember><KeepWithGroup>After</KeepWithGroup></TablixMember>"
            "<TablixMember>\n                  <KeepWithGroup>After</KeepWithGroup>",
        )
        tablix = RDLDocument.open(path).root.find(f".//{{{RDL_NS}}}Tablix")
        assert _resolve_row_index(tablix, "header") == 1

    def test_group_footer_on_data_only_group_raises(self, tmp_path):
        from pbirb_mcp.ops.styling import style_tablix_row

        path = _patched_fixture(tmp_path, '<Group Name="Details" />', _GROUP_ONLY_DATA)
        before = path.read_bytes()
        with pytest.raises(ElementNotFoundError, match="no footer row for group 'Product'"):
            style_tablix_row(str(path), "MainTable", "Product_footer", font_weight="Bold")
        with pytest.raises(ElementNotFoundError, match="no header row for group 'Product'"):
            style_tablix_row(str(path), "MainTable", "Product_header", font_weight="Bold")
        assert path.read_bytes() == before
        # 'details' still reaches the group's data row.
        out = style_tablix_row(str(path), "MainTable", "details", font_weight="Bold")
        assert out["cells"] == ["ProductID", "ProductName", "Amount"]

    def test_footer_after_subtotal_still_resolves(self, rdl_path):
        from pbirb_mcp.ops.styling import style_tablix_row
        from pbirb_mcp.ops.tablix import add_row_group
        from pbirb_mcp.ops.tablix_subtotals import add_subtotal_row

        add_row_group(str(rdl_path), "MainTable", "G", "=1")
        add_subtotal_row(str(rdl_path), "MainTable", "G", aggregates=[], position="footer")
        out = style_tablix_row(str(rdl_path), "MainTable", "G_footer", font_weight="Bold")
        assert out["row_index"] == 3

    def test_unknown_group_says_so(self, rdl_path):
        from pbirb_mcp.ops.styling import style_tablix_row

        with pytest.raises(ElementNotFoundError, match="no row group of that name"):
            style_tablix_row(str(rdl_path), "MainTable", "Nope_footer", font_weight="Bold")

    def test_rowheader_and_corner_roles_reach_header_cells(self, rdl_path):
        from pbirb_mcp.ops.styling import _resolve_row_index, style_tablix_row
        from pbirb_mcp.ops.tablix import add_row_group, convert_to_matrix
        from pbirb_mcp.ops.tablix_columns import add_column_group
        from pbirb_mcp.ops.validate import validate_report

        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_column_group(str(rdl_path), "MainTable", "Date", "=Fields!ProductID.Value")
        convert_to_matrix(str(rdl_path), "MainTable", "Region", "Date")

        row_hdr = style_tablix_row(
            str(rdl_path), "MainTable", "Region_rowheader", background_color="Navy"
        )
        assert row_hdr["row_index"] is None
        assert row_hdr["cells"] == ["Region_Header_0"]
        assert row_hdr["changed"] == ["box.BackgroundColor"]
        assert _bg(rdl_path, "Region_Header_0") == "Navy"

        corner = style_tablix_row(str(rdl_path), "MainTable", "corner", font_weight="Bold")
        assert corner["cells"] == ["MainTable_Corner"]
        col_hdr = style_tablix_row(str(rdl_path), "MainTable", "Date_rowheader", color="Red")
        assert col_hdr["cells"] == ["Date_Header"]
        assert validate_report(str(rdl_path))["valid"]

        before = rdl_path.read_bytes()
        again = style_tablix_row(str(rdl_path), "MainTable", "corner", font_weight="Bold")
        assert again["changed"] == [] and rdl_path.read_bytes() == before

        tablix = RDLDocument.open(rdl_path).root.find(f".//{{{RDL_NS}}}Tablix")
        with pytest.raises(ValueError, match="not a body row"):
            _resolve_row_index(tablix, "corner")
        with pytest.raises(ValueError, match="not a body row"):
            _resolve_row_index(tablix, "Region_rowheader")

    def test_rowheader_missing_raises(self, rdl_path):
        from pbirb_mcp.ops.styling import style_tablix_row
        from pbirb_mcp.ops.tablix import add_row_group

        add_row_group(str(rdl_path), "MainTable", "G", "=1")
        with pytest.raises(ElementNotFoundError, match="no row-header textbox"):
            style_tablix_row(str(rdl_path), "MainTable", "G_rowheader", color="Red")
        with pytest.raises(ElementNotFoundError, match="no TablixCorner"):
            style_tablix_row(str(rdl_path), "MainTable", "corner", color="Red")


class TestRowColourExpressions90:
    def test_alternating_validates_colours_before_opening(self, rdl_path):
        from pbirb_mcp.ops.styling import set_alternating_row_color

        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="color_a: invalid color"):
            set_alternating_row_color(str(rdl_path), "MainTable", 'Wh"ite', "White")
        with pytest.raises(ValueError, match="color_b: invalid color"):
            set_alternating_row_color(str(rdl_path), "MainTable", "White", "notacolor")
        assert rdl_path.read_bytes() == before

    def test_literal_quotes_are_doubled(self):
        from pbirb_mcp.ops.styling import _vb_string

        assert _vb_string('Wh"ite') == '"Wh""ite"'

    def test_expression_colour_is_embedded_not_quoted(self, rdl_path):
        from pbirb_mcp.ops.lint import lint_report
        from pbirb_mcp.ops.parameters import add_parameter
        from pbirb_mcp.ops.styling import set_alternating_row_color

        add_parameter(str(rdl_path), "Stripe", "String")
        out = set_alternating_row_color(
            str(rdl_path), "MainTable", "White", "=Parameters!Stripe.Value"
        )
        assert out["expression"] == (
            '=IIf(RowNumber(Nothing) Mod 2, "White", Parameters!Stripe.Value)'
        )
        assert _bg(rdl_path, "Amount") == out["expression"]
        rules = {i["rule"] for i in lint_report(str(rdl_path))["issues"]}
        assert "expression-syntax" not in rules

    def test_conditional_colours_validated_and_expressions_embedded(self, rdl_path):
        from pbirb_mcp.ops.styling import set_conditional_row_color

        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=r"color_map\['A'\]: invalid color"):
            set_conditional_row_color(str(rdl_path), "MainTable", "Fields!X.Value", {"A": "R"})
        with pytest.raises(ValueError, match="default_color: invalid color"):
            set_conditional_row_color(
                str(rdl_path), "MainTable", "Fields!X.Value", {"A": "Red"}, default_color="nope"
            )
        assert rdl_path.read_bytes() == before
        out = set_conditional_row_color(
            str(rdl_path),
            "MainTable",
            "Fields!ProductName.Value",
            {'A"B': "=Parameters!C.Value"},
        )
        assert out["expression"] == (
            '=Switch(UCase(Fields!ProductName.Value)="A""B", Parameters!C.Value, '
            'True, "Transparent")'
        )

    def test_zebra_composes_with_conditional_in_one_background(self, rdl_path):
        from pbirb_mcp.ops.styling import set_conditional_row_color
        from pbirb_mcp.ops.validate import validate_report

        out = set_conditional_row_color(
            str(rdl_path),
            "MainTable",
            "Fields!Amount.Value",
            {"0": "Red"},
            base_color="zebra:White,#EEEEEE",
        )
        expected = (
            '=Switch(UCase(Fields!Amount.Value)="0", "Red", '
            'True, IIf(RowNumber(Nothing) Mod 2, "White", "#EEEEEE"))'
        )
        assert out["expression"] == expected
        for name in ("ProductID", "ProductName", "Amount"):
            assert _bg(rdl_path, name) == expected
        assert validate_report(str(rdl_path))["valid"]
        before = rdl_path.read_bytes()
        again = set_conditional_row_color(
            str(rdl_path),
            "MainTable",
            "Fields!Amount.Value",
            {"0": "Red"},
            base_color="zebra:White,#EEEEEE",
        )
        assert not again["changed"] and rdl_path.read_bytes() == before

    def test_base_color_accepts_existing_expression(self, rdl_path):
        from pbirb_mcp.ops.styling import set_alternating_row_color, set_conditional_row_color

        zebra = set_alternating_row_color(str(rdl_path), "MainTable", "White", "Gainsboro")
        out = set_conditional_row_color(
            str(rdl_path),
            "MainTable",
            "Fields!Amount.Value",
            {"0": "Red"},
            case_sensitive=True,
            base_color=zebra["expression"],
        )
        assert out["expression"] == (
            '=Switch(Fields!Amount.Value="0", "Red", '
            'True, IIf(RowNumber(Nothing) Mod 2, "White", "Gainsboro"))'
        )

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"base_color": "zebra:White"}, "zebra:<color_a>,<color_b>"),
            ({"base_color": "zebra:White,bogus"}, "invalid color"),
            ({"base_color": "bogus"}, "base_color: invalid color"),
            ({"base_color": "White", "default_color": "Red"}, "not both"),
        ],
    )
    def test_bad_base_color_refused(self, rdl_path, kwargs, match):
        from pbirb_mcp.ops.styling import set_conditional_row_color

        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=match):
            set_conditional_row_color(
                str(rdl_path), "MainTable", "Fields!Amount.Value", {"0": "Red"}, **kwargs
            )
        assert rdl_path.read_bytes() == before

    def test_base_color_through_the_server(self, rdl_path):
        import json

        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "set_conditional_row_color",
                    "arguments": {
                        "path": str(rdl_path),
                        "tablix_name": "MainTable",
                        "value_expression": "Fields!Amount.Value",
                        "color_map": {"0": "Red"},
                        "base_color": "zebra:White,#EEEEEE",
                    },
                },
            }
        )
        result = resp["result"]
        assert not result.get("isError"), result
        payload = json.loads(result["content"][0]["text"])
        assert "IIf(RowNumber(Nothing) Mod 2" in payload["expression"]
