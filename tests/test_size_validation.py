"""Every size-taking tool validates its size arguments before writing (#22).

Each case calls one tool with ONE bad size argument (all other arguments
valid) and asserts:

* a ``ValueError`` whose message starts with the argument name and
  quotes the offending value;
* the file on disk is byte-identical afterwards (a failed call never
  writes).

The accepted forms (``"2cm"``, ``"0.75in"``, ``"108pt"``, ``"10mm"``,
``"1pc"``) are written verbatim, and ``=`` expressions pass through for
style sizes.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops.body import add_body_image, add_body_textbox
from pbirb_mcp.ops.chart import insert_chart_from_template
from pbirb_mcp.ops.header_footer import (
    add_footer_image,
    add_footer_textbox,
    add_header_image,
    add_header_textbox,
    set_page_footer,
    set_page_header,
)
from pbirb_mcp.ops.layout import add_line, add_list, add_rectangle
from pbirb_mcp.ops.page import set_body_size, set_page_orientation, set_page_setup
from pbirb_mcp.ops.positioning import (
    set_body_item_position,
    set_body_item_size,
    set_footer_item_position,
    set_footer_item_size,
    set_header_item_position,
    set_header_item_size,
)
from pbirb_mcp.ops.scratch import create_report
from pbirb_mcp.ops.styling import (
    set_textbox_runs,
    set_textbox_style,
    set_textbox_style_bulk,
    style_tablix_row,
)
from pbirb_mcp.ops.tablix import set_row_height, set_tablix_size
from pbirb_mcp.ops.tablix_columns import add_tablix_column, set_column_width
from pbirb_mcp.ops.tablix_static import add_static_column, add_static_row
from pbirb_mcp.ops.tablix_subtotals import add_subtotal_column
from pbirb_mcp.ops.templates import insert_tablix_from_template
from pbirb_mcp.ops.validate import validate_report

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


_BOX = {"top": "0in", "left": "0in", "width": "1in", "height": "0.25in"}
_IMG = {"image_source": "External", "value": "http://x/logo.png", **_BOX}

# tool -> (valid base kwargs (minus path), size argument names)
_TOOLS: list[tuple[Callable[..., Any], dict[str, Any], tuple[str, ...]]] = [
    (
        set_page_setup,
        {},
        (
            "page_height",
            "page_width",
            "margin_top",
            "margin_bottom",
            "margin_left",
            "margin_right",
        ),
    ),
    (set_body_size, {}, ("width", "height")),
    (set_page_header, {}, ("height",)),
    (set_page_footer, {}, ("height",)),
    (add_header_textbox, {"name": "N", "text": "t", **_BOX}, tuple(_BOX)),
    (add_footer_textbox, {"name": "N", "text": "t", **_BOX}, tuple(_BOX)),
    (add_header_image, {"name": "N", **_IMG}, tuple(_BOX)),
    (add_footer_image, {"name": "N", **_IMG}, tuple(_BOX)),
    (add_body_textbox, {"name": "N", "text": "t", **_BOX}, tuple(_BOX)),
    (add_body_image, {"name": "N", **_IMG}, tuple(_BOX)),
    (set_body_item_position, {"name": "MainTable", "top": "0in", "left": "0in"}, ("top", "left")),
    (set_header_item_position, {"name": "X", "top": "0in", "left": "0in"}, ("top", "left")),
    (set_footer_item_position, {"name": "X", "top": "0in", "left": "0in"}, ("top", "left")),
    (set_body_item_size, {"name": "MainTable"}, ("width", "height")),
    (set_header_item_size, {"name": "X"}, ("width", "height")),
    (set_footer_item_size, {"name": "X"}, ("width", "height")),
    (add_rectangle, {"name": "N", **_BOX}, tuple(_BOX)),
    (add_list, {"name": "N", "dataset_name": "MainDataset", **_BOX}, tuple(_BOX)),
    (add_line, {"name": "N", **_BOX}, ("top", "left", "width", "height", "line_thickness")),
    (set_tablix_size, {"name": "MainTable"}, ("width", "height")),
    (set_row_height, {"tablix_name": "MainTable", "row_index": 0, "height": "1in"}, ("height",)),
    (set_column_width, {"tablix_name": "MainTable", "column": 0, "width": "1in"}, ("width",)),
    (
        add_tablix_column,
        {"tablix_name": "MainTable", "column_name": "C", "expression": "=1"},
        ("width",),
    ),
    (add_static_row, {"tablix_name": "MainTable", "row_name": "R"}, ("height",)),
    (add_static_column, {"tablix_name": "MainTable", "column_name": "C"}, ("width",)),
    (
        add_subtotal_column,
        {"tablix_name": "MainTable", "group_name": "G", "aggregates": []},
        ("width",),
    ),
    (
        insert_tablix_from_template,
        {"name": "N", "dataset_name": "MainDataset", "columns": ["Amount"], **_BOX},
        tuple(_BOX),
    ),
    (
        insert_chart_from_template,
        {
            "name": "N",
            "dataset_name": "MainDataset",
            "category_field": "ProductName",
            "value_field": "Amount",
            **_BOX,
        },
        tuple(_BOX),
    ),
]

_STYLE_FIELDS = (
    "font_size",
    "border_width",
    "padding_top",
    "padding_bottom",
    "padding_left",
    "padding_right",
)
_TOOLS += [
    (set_textbox_style, {"textbox_name": "ProductID"}, _STYLE_FIELDS),
    (set_textbox_style_bulk, {"textbox_names": ["ProductID", "Amount"]}, _STYLE_FIELDS),
    (style_tablix_row, {"tablix_name": "MainTable", "row": "header"}, _STYLE_FIELDS),
]

# Malformed for every kind of size argument.
_MALFORMED = ("abc", "", "12", "1,5in", "1e3in", "200in")


def _cases():
    for tool, base, fields in _TOOLS:
        for field in fields:
            for bad in _MALFORMED:
                yield pytest.param(tool, base, field, bad, id=f"{tool.__name__}-{field}-{bad!r}")


def _assert_refused(path: Path, call: Callable[[], Any], field: str, bad: str) -> None:
    before = path.read_bytes()
    with pytest.raises(ValueError) as exc:
        call()
    msg = str(exc.value)
    assert msg.startswith(f"{field}: invalid RDL size {bad!r}"), msg
    assert path.read_bytes() == before, "failed call must not write"


@pytest.mark.parametrize("tool,base,field,bad", list(_cases()))
def test_malformed_size_refused_without_writing(rdl_path, tool, base, field, bad):
    kwargs = {**base, field: bad}
    _assert_refused(rdl_path, lambda: tool(path=str(rdl_path), **kwargs), field, bad)


def test_set_textbox_runs_font_size_refused(rdl_path):
    runs = [{"text": "a", "font_size": "10pt"}, {"text": "b", "font_size": "big"}]
    _assert_refused(
        rdl_path,
        lambda: set_textbox_runs(str(rdl_path), "ProductID", runs),
        "runs[1].font_size",
        "big",
    )


# ---- sign rules -------------------------------------------------------------

# (tool, base kwargs, field, value) — must be refused.
_SIGN_REFUSED = [
    (set_page_setup, {}, "page_width", "0in"),
    (set_page_setup, {}, "margin_left", "-1in"),
    (set_body_size, {}, "height", "0in"),
    (set_page_header, {}, "height", "-0.5in"),
    (add_body_textbox, {"name": "N", "text": "t", **_BOX}, "top", "-1in"),
    (add_body_textbox, {"name": "N", "text": "t", **_BOX}, "width", "0in"),
    (add_rectangle, {"name": "N", **_BOX}, "left", "-0.1cm"),
    (add_line, {"name": "N", **_BOX}, "top", "-1in"),
    (add_line, {"name": "N", **_BOX}, "line_thickness", "0pt"),
    (set_body_item_position, {"name": "MainTable", "top": "0in", "left": "0in"}, "left", "-5in"),
    (set_body_item_size, {"name": "MainTable"}, "width", "-1in"),
    (set_body_item_size, {"name": "MainTable"}, "height", "0in"),
    (set_tablix_size, {"name": "MainTable"}, "height", "-4in"),
    (set_row_height, {"tablix_name": "MainTable", "row_index": 0}, "height", "-1in"),
    (set_row_height, {"tablix_name": "MainTable", "row_index": 0}, "height", "0in"),
    (set_column_width, {"tablix_name": "MainTable", "column": 0}, "width", "0mm"),
    (set_textbox_style, {"textbox_name": "ProductID"}, "padding_top", "-2pt"),
    (set_textbox_style, {"textbox_name": "ProductID"}, "font_size", "0pt"),
    (set_textbox_style, {"textbox_name": "ProductID"}, "border_width", "-1pt"),
]


@pytest.mark.parametrize(
    "tool,base,field,bad",
    [pytest.param(*c, id=f"{c[0].__name__}-{c[2]}-{c[3]}") for c in _SIGN_REFUSED],
)
def test_sign_rules_refused(rdl_path, tool, base, field, bad):
    kwargs = {**base, field: bad}
    _assert_refused(rdl_path, lambda: tool(path=str(rdl_path), **kwargs), field, bad)


def test_line_vector_may_be_negative_or_zero(rdl_path):
    add_line(str(rdl_path), "L", "1in", "1in", width="-1in", height="0in")
    line = RDLDocument.open(rdl_path).root.find(f".//{{{RDL_NS}}}Line[@Name='L']")
    assert line.find(f"{{{RDL_NS}}}Width").text == "-1in"
    assert line.find(f"{{{RDL_NS}}}Height").text == "0in"
    # Resizing an existing Line keeps the vector semantics...
    set_body_item_size(str(rdl_path), "L", width="0in", height="-2cm")
    line = RDLDocument.open(rdl_path).root.find(f".//{{{RDL_NS}}}Line[@Name='L']")
    assert line.find(f"{{{RDL_NS}}}Width").text == "0in"
    assert line.find(f"{{{RDL_NS}}}Height").text == "-2cm"
    # ...but its position is still non-negative.
    _assert_refused(
        rdl_path,
        lambda: set_body_item_position(str(rdl_path), "L", "-1in", "0in"),
        "top",
        "-1in",
    )


# ---- the issue's repro ------------------------------------------------------


def test_issue_repro_all_refused(rdl_path):
    p = str(rdl_path)
    calls = [
        (lambda: set_page_setup(p, page_width="abc", margin_left="-1in", margin_top=""), "page"),
        (lambda: set_body_size(p, width="banana", height="12"), "width"),
        (lambda: set_body_item_position(p, "MainTable", "1,5in", "-5in"), "top"),
        (lambda: add_body_textbox(p, "T", "x", "abc", "0in", "0in", ""), "top"),
        (lambda: set_tablix_size(p, "MainTable", width="wide"), "width"),
        (lambda: set_row_height(p, "MainTable", 0, "-1in"), "height"),
        (lambda: set_column_width(p, "MainTable", 0, "1e3in"), "width"),
        (lambda: set_textbox_style(p, "ProductID", font_size="big", padding_top="-2pt"), "font"),
    ]
    before = rdl_path.read_bytes()
    for call, prefix in calls:
        with pytest.raises(ValueError, match=f"^{prefix}"):
            call()
    assert rdl_path.read_bytes() == before
    assert validate_report(p)["valid"]
    set_page_orientation(p, "Landscape")  # file still parseable


@pytest.mark.parametrize(
    "key,bad",
    [
        ("page_width", "abc"),
        ("page_height", "0in"),
        ("body_width", "-5in"),
        ("body_height", "2"),
        ("margin_top", "-1in"),
    ],
)
def test_create_report_page_setup_refused(tmp_path, key, bad):
    dst = tmp_path / "new.rdl"
    with pytest.raises(ValueError, match=rf"^page_setup\.{key}: invalid RDL size {bad!r}"):
        create_report(str(dst), page_setup={key: bad})
    assert not dst.exists()


def test_create_report_accepts_zero_margins_and_units(tmp_path):
    dst = tmp_path / "new.rdl"
    create_report(
        str(dst),
        page_setup={"page_width": "297mm", "page_height": "21cm", "margin_left": "0in"},
    )
    page = RDLDocument.open(dst).root.find(f".//{{{RDL_NS}}}Page")
    assert page.find(f"{{{RDL_NS}}}PageWidth").text == "297mm"
    assert page.find(f"{{{RDL_NS}}}PageHeight").text == "21cm"
    assert page.find(f"{{{RDL_NS}}}LeftMargin").text == "0in"


# ---- accepted values are written verbatim -----------------------------------


@pytest.mark.parametrize("value", ["2cm", "0.75in", "108pt", "10mm", "1pc"])
def test_accepted_units_written_unchanged(rdl_path, value):
    p = str(rdl_path)
    set_body_item_position(p, "MainTable", value, value)
    set_column_width(p, "MainTable", 0, value)
    set_textbox_style(p, "ProductID", font_size=value, padding_left=value)
    root = RDLDocument.open(rdl_path).root
    tablix = root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
    assert tablix.find(f"{{{RDL_NS}}}Top").text == value
    assert tablix.find(f"{{{RDL_NS}}}Left").text == value
    col = tablix.find(f".//{{{RDL_NS}}}TablixColumn/{{{RDL_NS}}}Width")
    assert col.text == value
    tb = root.find(f".//{{{RDL_NS}}}Textbox[@Name='ProductID']")
    assert tb.find(f"{{{RDL_NS}}}Style/{{{RDL_NS}}}PaddingLeft").text == value
    assert tb.find(f".//{{{RDL_NS}}}TextRun/{{{RDL_NS}}}Style/{{{RDL_NS}}}FontSize").text == value


def test_style_size_expressions_pass_through(rdl_path):
    expr = '=IIf(Parameters!Big.Value, "14pt", "10pt")'
    set_textbox_style(str(rdl_path), "ProductID", font_size=expr, padding_top="=Code.Pad()")
    tb = RDLDocument.open(rdl_path).root.find(f".//{{{RDL_NS}}}Textbox[@Name='ProductID']")
    assert tb.find(f".//{{{RDL_NS}}}TextRun/{{{RDL_NS}}}Style/{{{RDL_NS}}}FontSize").text == expr
    assert tb.find(f"{{{RDL_NS}}}Style/{{{RDL_NS}}}PaddingTop").text == "=Code.Pad()"


def test_whitespace_is_stripped_before_writing(rdl_path):
    set_tablix_size(str(rdl_path), "MainTable", width=" 6 in ")
    tablix = RDLDocument.open(rdl_path).root.find(f".//{{{RDL_NS}}}Tablix[@Name='MainTable']")
    assert tablix.find(f"{{{RDL_NS}}}Width").text == "6in"
