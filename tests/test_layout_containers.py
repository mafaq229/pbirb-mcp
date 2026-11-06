"""Layout containers: add_rectangle containment (#43) and nested items (#83).

#43: ``add_rectangle(contained_items=[...])`` rebased each moved child as
``child - rect`` without checking the child lay inside the rectangle, so a
rectangle placed below/right of a child gave it negative ``Top``/``Left``
and a rectangle smaller than its children was accepted — both rejected by
Report Builder while XSD and lint passed.

#83: the position / size / remove tools only saw direct children of the
body / header / footer, no add tool took a container (so a List's
``<name>_Rect`` could never be filled), and nothing moved an item between
the body, the bands and rectangles, or ungrouped a rectangle.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import (
    AmbiguousElementError,
    ElementNotFoundError,
    resolve_report_item,
)
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.body import add_body_image, add_body_textbox, remove_body_item
from pbirb_mcp.ops.chart import insert_chart_from_template
from pbirb_mcp.ops.containers import move_report_item, ungroup_rectangle
from pbirb_mcp.ops.header_footer import (
    add_header_textbox,
    remove_header_item,
    set_page_header,
)
from pbirb_mcp.ops.layout import add_line, add_list, add_rectangle
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.positioning import (
    set_body_item_position,
    set_body_item_size,
    set_header_item_position,
    set_header_item_size,
)
from pbirb_mcp.ops.reader import get_rectangle, get_textbox, list_body_items, list_header_items
from pbirb_mcp.ops.styling import set_textbox_style
from pbirb_mcp.ops.templates import insert_tablix_from_template
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.ops.visibility import set_element_visibility

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "pbi_paginated_minimal.rdl"


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


# ---- #83: nested items -------------------------------------------------------

MARGIN = 1.0  # the fixture's top / left margins, inches


def _header_height(path: Path) -> float:
    doc = RDLDocument.open(path)
    header = doc.root.find(f".//{q('PageHeader')}")
    return _inches(header, "Height") if header is not None else 0.0


def _abs(path: Path, name: str) -> tuple[float, float]:
    """Design-time page position (top, left) of a report item: its own
    Top/Left plus every enclosing Rectangle's / Tablix's, plus the region
    origin (header: top margin; body: below the header). List cells here
    have no row / column headers, so the cell origin is the tablix's."""
    el = _named(path, name)
    top, left = _inches(el, "Top"), _inches(el, "Left")
    region = None
    for anc in el.iterancestors():
        local = etree.QName(anc).localname
        if local in ("Rectangle", "Tablix"):
            top += _inches(anc, "Top")
            left += _inches(anc, "Left")
        if local in ("Body", "PageHeader", "PageFooter"):
            region = local
            break
    assert region in ("Body", "PageHeader")
    top += MARGIN + (_header_height(path) if region == "Body" else 0.0)
    return top, left + MARGIN


def _named(path: Path, name: str):
    doc = RDLDocument.open(path)
    return doc.root.xpath(
        ".//*[@Name=$n][parent::r:ReportItems or parent::r:CellContents]",
        namespaces={"r": RDL_NS},
        n=name,
    )[0]


def _container_of(path: Path, name: str) -> str:
    el = _named(path, name)
    holder = el.getparent().getparent()
    local = etree.QName(holder).localname
    return holder.get("Name") if local == "Rectangle" else local


@pytest.fixture
def grouped(rdl_path: Path) -> Path:
    """The issue's repro: Title moved into rectangle Panel."""
    add_body_textbox(str(rdl_path), "Title", "Sales", "0.1in", "0.5in", "3in", "0.3in")
    add_rectangle(str(rdl_path), "Panel", "0in", "0.4in", "4in", "0.45in", ["Title"])
    return rdl_path


@pytest.fixture
def with_list(rdl_path: Path) -> Path:
    add_list(str(rdl_path), "Cards", "MainDataset", "1.2in", "0.5in", "4in", "0.6in")
    return rdl_path


class TestResolveReportItem:
    def test_resolves_nested_and_cell_items(self, grouped):
        add_list(str(grouped), "Cards", "MainDataset", "1.2in", "0.5in", "4in", "0.6in")
        doc = RDLDocument.open(grouped)
        item, container, region = resolve_report_item(doc, "Title")
        assert container.get("Name") == "Panel" and region == "body"
        rect, cell, region = resolve_report_item(doc, "Cards_Rect")
        assert etree.QName(cell).localname == "CellContents" and region == "body"
        table, body, _ = resolve_report_item(doc, "MainTable")
        assert etree.QName(body).localname == "Body"

    def test_region_filter_names_where_the_item_is(self, grouped):
        doc = RDLDocument.open(grouped)
        with pytest.raises(
            ElementNotFoundError, match=r"header has no named item 'Title' \(it is in the body\)"
        ):
            resolve_report_item(doc, "Title", region="header")
        with pytest.raises(ElementNotFoundError, match="report item 'Nope' not found"):
            resolve_report_item(doc, "Nope")

    def test_duplicates_are_ambiguous(self, grouped):
        doc = RDLDocument.open(grouped)
        dup = etree.fromstring(etree.tostring(_named(grouped, "Title")))
        doc.root.find(f".//{q('Body')}/{q('ReportItems')}").append(dup)
        with pytest.raises(AmbiguousElementError):
            resolve_report_item(doc, "Title")

    def test_ignores_non_report_item_names(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        with pytest.raises(ElementNotFoundError):
            resolve_report_item(doc, "MainDataset")


class TestNestedPositionSizeRemove:
    """Acceptance: in the repro, position, size and remove all work on Title."""

    def test_position(self, grouped):
        result = set_body_item_position(str(grouped), "Title", "0.05in", "0.2in")
        assert result["changed"] is True and result["parent"] == "Panel"
        title = _named(grouped, "Title")
        assert (find_child(title, "Top").text, find_child(title, "Left").text) == (
            "0.05in",
            "0.2in",
        )
        assert _container_of(grouped, "Title") == "Panel"
        assert validate_report(str(grouped))["valid"]

    def test_size(self, grouped):
        result = set_body_item_size(str(grouped), "Title", "2in")
        assert result["changed"] is True and result["parent"] == "Panel"
        assert find_child(_named(grouped, "Title"), "Width").text == "2in"
        assert "warnings" not in result

    def test_overflowing_the_rectangle_warns(self, grouped):
        result = set_body_item_size(str(grouped), "Title", "5in")
        assert any("extends outside rectangle 'Panel'" in w for w in result["warnings"])

    def test_remove(self, grouped):
        result = remove_body_item(str(grouped), "Title")
        assert result == {
            "removed": "Title",
            "kind": "Textbox",
            "parent": "Panel",
            "also_removed": [],
            "dangling_refs": [],
        }
        assert get_rectangle(str(grouped), "Panel")["contained_items"] == []
        # The emptied <ReportItems> goes with it.
        assert find_child(_named(grouped, "Panel"), "ReportItems") is None
        assert validate_report(str(grouped))["valid"]

    def test_top_level_unchanged(self, rdl_path):
        result = set_body_item_position(str(rdl_path), "MainTable", "0.6in", "0.5in")
        assert result["parent"] is None and result["container"] == "body"

    def test_wrong_region_names_the_right_one(self, grouped):
        set_page_header(str(grouped), height="1in")
        with pytest.raises(
            ElementNotFoundError, match=r"header has no named item 'Title' \(it is in the body\)"
        ):
            set_header_item_position(str(grouped), "Title", "0in", "0in")

    def test_tablix_cell_content_refused(self, with_list):
        before = with_list.read_bytes()
        with pytest.raises(ValueError, match="fills a tablix cell"):
            set_body_item_size(str(with_list), "Cards_Rect", "1in")
        with pytest.raises(ValueError, match="fills a tablix cell"):
            remove_body_item(str(with_list), "Cards_Rect")
        with pytest.raises(ValueError, match="fills a tablix cell"):
            remove_body_item(str(with_list), "HeaderAmount")  # a MainTable cell
        assert with_list.read_bytes() == before

    def test_header_nested_item(self, rdl_path):
        set_page_header(str(rdl_path), height="1in")
        add_header_textbox(str(rdl_path), "Logo", "ACME", "0.1in", "0.1in", "1in", "0.3in")
        move_report_item(str(rdl_path), "Logo", "header")  # already there: no-op
        add_header_textbox(str(rdl_path), "Sub", "x", "0.5in", "0.1in", "1in", "0.3in")
        doc = RDLDocument.open(rdl_path)
        header = doc.root.find(f".//{q('PageHeader')}")
        rect = etree.SubElement(find_child(header, "ReportItems"), q("Rectangle"), Name="Band")
        for local, value in (
            ("Top", "0in"),
            ("Left", "2in"),
            ("Height", "0.8in"),
            ("Width", "2in"),
        ):
            etree.SubElement(rect, q(local)).text = value
        doc.save()
        move_report_item(str(rdl_path), "Sub", "Band", top="0.1in", left="0.1in")
        # The band check measures the top-level rectangle, not the nested item.
        result = set_header_item_position(str(rdl_path), "Sub", "0.2in", "0.1in")
        assert result["parent"] == "Band" and "warnings" not in result
        result = set_header_item_size(str(rdl_path), "Sub", height="2in")
        assert any("extends outside rectangle 'Band'" in w for w in result["warnings"])
        assert remove_header_item(str(rdl_path), "Sub") == {
            "region": "header",
            "removed": "Sub",
            "kind": "Textbox",
            "parent": "Band",
            "also_removed": [],
            "dangling_refs": [],
        }
        assert validate_report(str(rdl_path))["valid"]


class TestContainerArgument:
    """Acceptance: add_body_textbox(..., container="Cards_Rect") fills the list."""

    def test_textbox_into_list_rectangle(self, with_list):
        result = add_body_textbox(
            str(with_list),
            "CardName",
            "=Fields!ProductName.Value",
            "0in",
            "0in",
            "2in",
            "0.25in",
            container="Cards_Rect",
        )
        assert result == {"name": "CardName", "kind": "Textbox", "container": "Cards_Rect"}
        assert get_rectangle(str(with_list), "Cards_Rect")["contained_items"] == ["CardName"]
        assert validate_report(str(with_list))["valid"]
        # Inside the list the field is in scope: no field-out-of-scope error.
        rules = {i["rule"] for i in lint_report(str(with_list))["issues"]}
        assert "field-out-of-scope" not in rules

    def test_every_add_tool_takes_a_container(self, with_list):
        p = str(with_list)
        add_body_image(
            p,
            "Pic",
            "External",
            "http://x/y.png",
            "0in",
            "2in",
            "0.5in",
            "0.5in",
            container="Cards_Rect",
        )
        add_line(p, "Rule", "0.5in", "0in", "4in", "0in", container="Cards_Rect")
        add_rectangle(p, "Inner", "0.05in", "0.05in", "1in", "0.3in", container="Cards_Rect")
        add_body_textbox(p, "Deep", "x", "0in", "0in", "0.5in", "0.2in", container="Inner")
        insert_tablix_from_template(
            p,
            "Nested",
            "MainDataset",
            ["ProductName"],
            "0in",
            "2.6in",
            "1in",
            "0.5in",
            container="Cards_Rect",
        )
        insert_chart_from_template(
            p,
            "Spark",
            "MainDataset",
            "ProductName",
            "Amount",
            "0in",
            "3.5in",
            "0.5in",
            "0.5in",
            container="Cards_Rect",
        )
        assert get_rectangle(p, "Cards_Rect")["contained_items"] == [
            "Pic",
            "Rule",
            "Inner",
            "Nested",
            "Spark",
        ]
        assert get_rectangle(p, "Inner")["contained_items"] == ["Deep"]
        assert validate_report(p)["valid"]

    def test_rectangle_in_container_with_contained_items(self, grouped):
        add_body_textbox(
            str(grouped), "Note", "n", "0.1in", "3.2in", "0.5in", "0.2in", container="Panel"
        )
        add_rectangle(
            str(grouped), "Sub", "0in", "3in", "1in", "0.4in", ["Note"], container="Panel"
        )
        assert _container_of(grouped, "Note") == "Sub"
        note = _named(grouped, "Note")
        assert (find_child(note, "Top").text, find_child(note, "Left").text) == ("0.1in", "0.2in")
        with pytest.raises(ElementNotFoundError, match="items not in rectangle 'Panel'"):
            add_rectangle(
                str(grouped), "Sub2", "0in", "0in", "4in", "1in", ["MainTable"], container="Panel"
            )

    def test_overflow_warns(self, with_list):
        result = add_body_textbox(
            str(with_list), "Wide", "x", "0in", "0in", "5in", "0.25in", container="Cards_Rect"
        )
        assert any(
            "extends outside rectangle 'Cards_Rect'" in w and "width 4in" in w
            for w in result["warnings"]
        )

    def test_bad_containers_refused(self, grouped):
        p = str(grouped)
        before = grouped.read_bytes()
        with pytest.raises(ElementNotFoundError, match="container rectangle 'Nope' not found"):
            add_body_textbox(p, "A", "x", "0in", "0in", "1in", "0.2in", container="Nope")
        with pytest.raises(ValueError, match="'MainTable' is a Tablix, not a Rectangle"):
            add_line(p, "L", "0in", "0in", "1in", "0in", container="MainTable")
        set_page_header(p, height="1in")
        doc = RDLDocument.open(grouped)
        header = doc.root.find(f".//{q('PageHeader')}")
        items = etree.SubElement(header, q("ReportItems"))
        etree.SubElement(items, q("Rectangle"), Name="HdrRect")
        doc.save()
        before = grouped.read_bytes()
        with pytest.raises(ValueError, match="'HdrRect' is in the page header"):
            add_body_textbox(p, "A", "x", "0in", "0in", "1in", "0.2in", container="HdrRect")
        assert grouped.read_bytes() == before

    def test_data_region_in_list_of_another_dataset_refused(self, tmp_path):
        p = tmp_path / "multi.rdl"
        shutil.copy(FIXTURES / "pbi_multi_datasource.rdl", p)
        add_list(str(p), "Cards", "MainDataset", "3in", "0in", "4in", "1in")
        before = p.read_bytes()
        with pytest.raises(ValueError, match="bound to dataset 'MainDataset'"):
            insert_tablix_from_template(
                str(p),
                "Inner",
                "LookupSet",
                ["LookupKey"],
                "0in",
                "0in",
                "1in",
                "0.5in",
                container="Cards_Rect",
            )
        assert p.read_bytes() == before


class TestMoveReportItem:
    def test_into_header_keeps_style_and_page_position(self, grouped):
        """Acceptance: move_report_item(p, 'Title', 'header') keeps its style
        and its absolute position within 0.001in."""
        p = str(grouped)
        set_textbox_style(p, "Title", font_weight="Bold", color="#336699")
        set_element_visibility(
            p, "Title", hidden_expression="=Parameters!DateFrom.Value Is Nothing"
        )
        before_style = get_textbox(p, "Title")["style"]
        before_vis = get_textbox(p, "Title")["visibility"]
        origin = _abs(grouped, "Title")
        result = move_report_item(p, "Title", "header")
        assert result["from"] == "rectangle:Panel" and result["to"] == "header"
        assert result["changed"] is True and result["band_height"] == "0.5in"
        assert any("created the page header" in w for w in result["warnings"])
        assert [i["name"] for i in list_header_items(p)] == ["Title"]
        assert _abs(grouped, "Title") == pytest.approx(origin, abs=1e-3)
        after = get_textbox(p, "Title")
        assert after["style"] == before_style and after["visibility"] == before_vis
        assert validate_report(p)["valid"]

    def test_into_existing_header_grows_band(self, grouped):
        p = str(grouped)
        set_page_header(p, height="0.5in")
        origin = _abs(grouped, "Title")  # body now starts 0.5in lower
        result = move_report_item(p, "Title", "header")
        assert result["top"] == "0.6in"
        assert result["band_height"] == "0.9in"
        assert any("grown from '0.5in' to '0.9in'" in w for w in result["warnings"])
        # The header kept its height until the item grew it; the item's own
        # page position is unchanged.
        title = _named(grouped, "Title")
        assert units.to_inches(find_child(title, "Top").text) + MARGIN == pytest.approx(
            origin[0], abs=1e-3
        )

    def test_into_list_and_back_keeps_page_position(self, grouped):
        p = str(grouped)
        add_list(p, "Cards", "MainDataset", "0in", "0.3in", "4in", "1in")
        origin = _abs(grouped, "Title")
        result = move_report_item(p, "Title", "Cards_Rect")
        assert result["to"] == "rectangle:Cards_Rect"
        assert (result["top"], result["left"]) == ("0.1in", "0.2in")
        assert _abs(grouped, "Title") == pytest.approx(origin, abs=1e-3)
        assert get_rectangle(p, "Cards_Rect")["contained_items"] == ["Title"]
        move_report_item(p, "Title", "body")
        assert _container_of(grouped, "Title") == "Body"
        assert _abs(grouped, "Title") == pytest.approx(origin, abs=1e-3)
        assert validate_report(p)["valid"]

    def test_mixed_units(self, grouped):
        p = str(grouped)
        add_rectangle(p, "Metric", "1.5in", "0in", "10cm", "2cm")
        add_body_textbox(p, "Cm", "x", "4cm", "1cm", "2cm", "1cm")
        origin = _abs(grouped, "Cm")
        result = move_report_item(p, "Cm", "Metric")
        assert result["top"].endswith("cm") and result["left"].endswith("cm")
        assert _abs(grouped, "Cm") == pytest.approx(origin, abs=1e-3)

    def test_explicit_position_overrides(self, grouped):
        result = move_report_item(str(grouped), "Title", "body", top="2in")
        title = _named(grouped, "Title")
        assert result["top"] == "2in" and find_child(title, "Top").text == "2in"
        assert find_child(title, "Left").text == "0.5in"  # kept on the page

    def test_negative_position_refused(self, grouped):
        p = str(grouped)
        add_rectangle(p, "Low", "3in", "0in", "4in", "1in")
        before = grouped.read_bytes()
        with pytest.raises(ValueError, match=r"2\.9in above rectangle 'Low'.*pass top"):
            move_report_item(p, "Title", "Low")
        assert grouped.read_bytes() == before
        move_report_item(p, "Title", "Low", top="0.1in")
        assert _container_of(grouped, "Title") == "Low"

    def test_same_container_is_a_no_op(self, grouped):
        before = grouped.read_bytes()
        result = move_report_item(str(grouped), "Title", "Panel")
        assert result["changed"] is False and result["from"] == result["to"] == "rectangle:Panel"
        assert grouped.read_bytes() == before
        result = move_report_item(str(grouped), "Title", "Panel", left="0.3in")
        assert result["changed"] is True and result["left"] == "0.3in"

    def test_keeps_subtree_byte_for_byte(self, grouped):
        p = str(grouped)
        set_textbox_style(p, "Title", font_size="14pt", background_color="Yellow")

        def subtree() -> bytes:
            el = _named(grouped, "Title")
            for local in ("Top", "Left"):
                el.remove(find_child(el, local))
            for node in el.iter():  # re-indentation is not a change
                node.tail = None
                if len(node) and node.text is not None and not node.text.strip():
                    node.text = None
            return etree.tostring(el)

        before = subtree()
        move_report_item(p, "Title", "header")
        assert subtree() == before

    def test_data_region_into_header_refused(self, grouped):
        p = str(grouped)
        with pytest.raises(ValueError, match="can't hold data regions"):
            move_report_item(p, "MainTable", "header")
        add_rectangle(p, "Wrap", "0.4in", "0.4in", "4.2in", "0.7in", ["MainTable"])
        with pytest.raises(ValueError, match="holds Tablix 'MainTable'"):
            move_report_item(p, "Wrap", "footer")
        add_list(p, "Cards", "MainDataset", "2in", "0.5in", "4in", "0.6in")
        with pytest.raises(ValueError, match="can't hold data regions"):
            move_report_item(p, "Cards", "header")
        assert "PageHeader" not in grouped.read_text()  # nothing created on refusal

    def test_fields_into_header_need_a_scoped_aggregate(self, with_list):
        p = str(with_list)
        add_body_textbox(
            p,
            "Bare",
            "=Fields!ProductName.Value",
            "0in",
            "0in",
            "2in",
            "0.25in",
            container="Cards_Rect",
        )
        add_body_textbox(
            p,
            "Agg",
            "=Sum(Fields!Amount.Value)",
            "0.3in",
            "0in",
            "2in",
            "0.25in",
            container="Cards_Rect",
        )
        add_body_textbox(
            p,
            "Scoped",
            '=First(Fields!ProductName.Value, "MainDataset")',
            "0in",
            "2in",
            "2in",
            "0.25in",
            container="Cards_Rect",
        )
        before = with_list.read_bytes()
        with pytest.raises(ValueError, match=r"Fields!ProductName\.Value .*header / footer"):
            move_report_item(p, "Bare", "header", top="0in", left="0in")
        with pytest.raises(ValueError, match=r"Fields!Amount\.Value"):
            move_report_item(p, "Agg", "header", top="0in", left="0in")
        # Leaving the list's dataset scope for the body is refused too.
        with pytest.raises(ValueError, match="lose dataset 'MainDataset'"):
            move_report_item(p, "Bare", "body")
        assert with_list.read_bytes() == before
        move_report_item(p, "Scoped", "header", top="0in", left="0in")
        assert [i["name"] for i in list_header_items(p)] == ["Scoped"]
        assert validate_report(p)["valid"]

    def test_body_item_moved_into_a_list_gains_its_scope(self, with_list):
        p = str(with_list)
        add_body_textbox(p, "Row", "=Fields!ProductName.Value", "1.25in", "0.6in", "2in", "0.25in")
        move_report_item(p, "Row", "Cards_Rect")
        assert _container_of(with_list, "Row") == "Cards_Rect"
        row = _named(with_list, "Row")
        assert (find_child(row, "Top").text, find_child(row, "Left").text) == ("0.05in", "0.1in")
        rules = {i["rule"] for i in lint_report(p)["issues"]}
        assert "field-out-of-scope" not in rules

    def test_refusals(self, grouped):
        p = str(grouped)
        add_list(p, "Cards", "MainDataset", "2in", "0.5in", "4in", "0.6in")
        before = grouped.read_bytes()
        with pytest.raises(ValueError, match="into itself"):
            move_report_item(p, "Panel", "Panel")
        with pytest.raises(ValueError, match="fills a tablix cell"):
            move_report_item(p, "Cards_Rect", "body")
        with pytest.raises(ValueError, match="'MainTable' is a Tablix, not a Rectangle"):
            move_report_item(p, "Title", "MainTable")
        with pytest.raises(ElementNotFoundError, match="neither 'body'"):
            move_report_item(p, "Title", "Nowhere")
        with pytest.raises(ElementNotFoundError):
            move_report_item(p, "Ghost", "body")
        with pytest.raises(ValueError):
            move_report_item(p, "Title", "body", top="-1in")
        assert grouped.read_bytes() == before

    def test_nested_rectangle_into_its_child_refused(self, grouped):
        p = str(grouped)
        add_rectangle(p, "Child", "0.05in", "0.05in", "0.2in", "0.2in", container="Panel")
        with pytest.raises(ValueError, match="into itself or its own contents"):
            move_report_item(p, "Panel", "Child")

    def test_list_body_items_recursive_reflects_moves(self, grouped):
        p = str(grouped)
        move_report_item(p, "Title", "body")
        assert [(i["name"], i["parent"]) for i in list_body_items(p, recursive=True)] == [
            ("MainTable", None),
            ("Panel", None),
            ("Title", None),
        ]


class TestUngroupRectangle:
    def test_restores_pre_grouping_coordinates(self, rdl_path):
        """Acceptance: ungroup_rectangle restores the pre-grouping body
        coordinates."""
        p = str(rdl_path)
        add_body_textbox(p, "Title", "Sales", "0.1in", "0.5in", "3in", "0.3in")
        add_body_textbox(p, "Sub", "x", "1.2cm", "1.5cm", "2cm", "0.5cm")
        before = {
            n: (
                find_child(_named(rdl_path, n), "Top").text,
                find_child(_named(rdl_path, n), "Left").text,
            )
            for n in ("Title", "Sub", "MainTable")
        }
        add_rectangle(p, "Panel", "0in", "0.4in", "4.5in", "1.1in", ["Title", "Sub", "MainTable"])
        result = ungroup_rectangle(p, "Panel")
        assert result == {
            "name": "Panel",
            "moved": ["Title", "Sub", "MainTable"],
            "to": "body",
            "changed": True,
        }
        for n, (top, left) in before.items():
            el = _named(rdl_path, n)
            assert units.to_inches(find_child(el, "Top").text) == pytest.approx(
                units.to_inches(top), abs=1e-3
            )
            assert units.to_inches(find_child(el, "Left").text) == pytest.approx(
                units.to_inches(left), abs=1e-3
            )
            assert _container_of(rdl_path, n) == "Body"
        with pytest.raises(ElementNotFoundError):
            get_rectangle(p, "Panel")
        assert validate_report(p)["valid"]

    def test_children_take_the_rectangles_place(self, grouped):
        p = str(grouped)
        add_body_textbox(p, "After", "x", "1.5in", "0in", "1in", "0.2in")
        ungroup_rectangle(p, "Panel")
        assert [i["name"] for i in list_body_items(p)] == ["MainTable", "Title", "After"]

    def test_nested_rectangle_ungroups_into_its_parent(self, grouped):
        p = str(grouped)
        add_rectangle(
            p, "Inner", "0.05in", "0.05in", "3.2in", "0.35in", ["Title"], container="Panel"
        )
        origin = _abs(grouped, "Title")
        result = ungroup_rectangle(p, "Inner")
        assert result["to"] == "rectangle:Panel"
        assert _container_of(grouped, "Title") == "Panel"
        assert _abs(grouped, "Title") == pytest.approx(origin, abs=1e-3)

    def test_warns_about_what_the_rectangle_carried(self, grouped):
        p = str(grouped)
        set_element_visibility(p, "Panel", hidden_expression="true")
        result = ungroup_rectangle(p, "Panel")
        assert "Visibility" in result["warnings"][0] and "'Title'" in result["warnings"][0]

    def test_list_rectangle_refused(self, with_list):
        before = with_list.read_bytes()
        with pytest.raises(ValueError, match="fills a tablix cell"):
            ungroup_rectangle(str(with_list), "Cards_Rect")
        with pytest.raises(ValueError, match="not a Rectangle"):
            ungroup_rectangle(str(with_list), "MainTable")
        assert with_list.read_bytes() == before

    def test_empty_rectangle_just_goes(self, rdl_path):
        add_rectangle(str(rdl_path), "Frame", "3in", "0in", "1in", "1in")
        assert ungroup_rectangle(str(rdl_path), "Frame")["moved"] == []
        assert validate_report(str(rdl_path))["valid"]
