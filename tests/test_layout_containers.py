"""add_rectangle containment (issue #43).

``add_rectangle(contained_items=[...])`` rebased each moved child as
``child - rect`` without checking the child lay inside the rectangle, so a
rectangle placed below/right of a child gave it negative ``Top``/``Left``
and a rectangle smaller than its children was accepted — both rejected by
Report Builder while XSD and lint passed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core import units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.layout import add_line, add_rectangle
from pbirb_mcp.ops.validate import validate_report

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _item(path: Path, name: str):
    doc = RDLDocument.open(path)
    return doc.root.xpath(".//r:ReportItems/*[@Name=$n]", namespaces={"r": RDL_NS}, n=name)[0]


def _inches(el, local: str) -> float:
    node = find_child(el, local)
    return units.to_inches(node.text) if node is not None else 0.0


def _page_origin(path: Path, name: str) -> tuple[float, float]:
    """Absolute (top, left) of a body item, summing rectangle offsets."""
    el = _item(path, name)
    top, left = _inches(el, "Top"), _inches(el, "Left")
    for anc in el.iterancestors(q("Rectangle")):
        top += _inches(anc, "Top")
        left += _inches(anc, "Left")
    return top, left


class TestRepro:
    def test_rectangle_below_right_of_child_refused(self, rdl_path):
        add_body_textbox(str(rdl_path), "Title", "Sales", "0.1in", "0.15in", "3in", "0.3in")
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=r"'Title' extends 0\.4in above, 0\.35in left of"):
            add_rectangle(str(rdl_path), "Panel", "0.5in", "0.5in", "2in", "1in", ["Title"])
        assert rdl_path.read_bytes() == before

    def test_rectangle_smaller_than_child_refused(self, rdl_path):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=r"'MainTable' extends 0\.4in below, 3in right of"):
            add_rectangle(str(rdl_path), "Tiny", "0.5in", "0.5in", "1in", "0.1in", ["MainTable"])
        assert rdl_path.read_bytes() == before

    def test_every_offender_listed(self, rdl_path):
        add_body_textbox(str(rdl_path), "Title", "Sales", "0.1in", "0.15in", "3in", "0.3in")
        with pytest.raises(ValueError) as exc:
            add_rectangle(
                str(rdl_path), "Panel", "0.5in", "0.5in", "1in", "0.2in", ["Title", "MainTable"]
            )
        assert "'Title'" in str(exc.value) and "'MainTable'" in str(exc.value)
        assert "fit=true" in str(exc.value)


class TestFit:
    def test_fit_grows_rectangle_and_rebases_non_negative(self, rdl_path):
        add_body_textbox(str(rdl_path), "Title", "Sales", "0.1in", "0.15in", "3in", "0.3in")
        origins = {n: _page_origin(rdl_path, n) for n in ("Title", "MainTable")}
        result = add_rectangle(
            str(rdl_path),
            "Panel",
            "0.5in",
            "0.5in",
            "1in",
            "0.1in",
            ["Title", "MainTable"],
            fit=True,
        )
        assert result["fitted"] is True
        # Union: top 0.1, left 0.15, bottom max(0.4, 1.0)=1.0, right max(3.15, 4.5)=4.5.
        assert (result["top"], result["left"]) == ("0.1in", "0.15in")
        assert (result["height"], result["width"]) == ("0.9in", "4.35in")
        rect = _item(rdl_path, "Panel")
        r_h, r_w = _inches(rect, "Height"), _inches(rect, "Width")
        for n, origin in origins.items():
            child = _item(rdl_path, n)
            c_top, c_left = _inches(child, "Top"), _inches(child, "Left")
            assert c_top >= 0 and c_left >= 0
            assert c_top + _inches(child, "Height") <= r_h + 1e-9
            assert c_left + _inches(child, "Width") <= r_w + 1e-9
            assert _page_origin(rdl_path, n) == pytest.approx(origin, abs=1e-3)
        assert validate_report(str(rdl_path))["valid"]

    def test_fit_not_needed_reports_unfitted(self, rdl_path):
        result = add_rectangle(
            str(rdl_path), "Frame", "0in", "0in", "5in", "2in", ["MainTable"], fit=True
        )
        assert result["fitted"] is False
        assert (result["top"], result["left"], result["width"], result["height"]) == (
            "0in",
            "0in",
            "5in",
            "2in",
        )

    def test_result_shape_unchanged_without_fit(self, rdl_path):
        result = add_rectangle(str(rdl_path), "Frame", "0in", "0in", "5in", "2in", ["MainTable"])
        assert result == {"name": "Frame", "kind": "Rectangle", "moved": ["MainTable"]}


class TestGeometry:
    def test_mixed_units_preserve_page_position(self, rdl_path):
        add_body_textbox(str(rdl_path), "Inner", "x", "3.5in", "1in", "1in", "0.3in")
        origin = _page_origin(rdl_path, "Inner")
        add_rectangle(str(rdl_path), "Frame", "8cm", "1.5cm", "6cm", "3cm", ["Inner"])
        inner = _item(rdl_path, "Inner")
        assert find_child(inner, "Top").text.endswith("cm")
        assert _page_origin(rdl_path, "Inner") == pytest.approx(origin, abs=1e-3)
        assert validate_report(str(rdl_path))["valid"]

    def test_mixed_units_with_fit(self, rdl_path):
        add_body_textbox(str(rdl_path), "Inner", "x", "3.5in", "1in", "1in", "0.3in")
        origin = _page_origin(rdl_path, "Inner")
        result = add_rectangle(
            str(rdl_path), "Frame", "9cm", "3cm", "1cm", "1cm", ["Inner"], fit=True
        )
        assert result["top"].endswith("cm") and result["width"].endswith("cm")
        inner = _item(rdl_path, "Inner")
        assert _inches(inner, "Top") >= 0 and _inches(inner, "Left") >= 0
        assert _page_origin(rdl_path, "Inner") == pytest.approx(origin, abs=1e-3)

    def test_child_exactly_filling_rectangle_accepted(self, rdl_path):
        # MainTable: top 0.5in, left 0.5in, 4in x 0.5in.
        add_rectangle(str(rdl_path), "Frame", "0.5in", "0.5in", "4in", "0.5in", ["MainTable"])
        table = _item(rdl_path, "MainTable")
        assert find_child(table, "Top").text == "0in"
        assert find_child(table, "Left").text == "0in"

    def test_line_box_uses_start_plus_vector(self, rdl_path):
        # Line from (1.5in, 1in) up-and-right to (1.0in, 3in).
        add_line(str(rdl_path), "Rule", "1.5in", "1in", "2in", "-0.5in")
        with pytest.raises(ValueError, match=r"'Rule' extends 0\.5in above"):
            add_rectangle(str(rdl_path), "Frame", "1.5in", "1in", "2in", "0.5in", ["Rule"])
        add_rectangle(str(rdl_path), "Frame", "1in", "1in", "2in", "0.5in", ["Rule"])
        rule = _item(rdl_path, "Rule")
        assert find_child(rule, "Top").text == "0.5in"
        assert validate_report(str(rdl_path))["valid"]

    def test_missing_top_left_count_as_zero(self, rdl_path):
        add_body_textbox(str(rdl_path), "Corner", "x", "0in", "0in", "1in", "0.3in")
        doc = RDLDocument.open(rdl_path)
        tb = doc.root.find(f".//{q('Textbox')}[@Name='Corner']")
        for local in ("Top", "Left"):
            tb.remove(find_child(tb, local))
        doc.save()
        with pytest.raises(ValueError, match="'Corner' extends 0.2in above, 0.2in left of"):
            add_rectangle(str(rdl_path), "Frame", "0.2in", "0.2in", "2in", "1in", ["Corner"])
        add_rectangle(str(rdl_path), "Frame", "0in", "0in", "2in", "1in", ["Corner"])

    def test_duplicate_contained_items_moved_once(self, rdl_path):
        result = add_rectangle(
            str(rdl_path), "Frame", "0in", "0in", "5in", "2in", ["MainTable", "MainTable"]
        )
        assert result["moved"] == ["MainTable"]
        rect = _item(rdl_path, "Frame")
        assert [c.get("Name") for c in find_child(rect, "ReportItems")] == ["MainTable"]
        assert validate_report(str(rdl_path))["valid"]
