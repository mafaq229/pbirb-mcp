"""Report-wide name registry + identifier validation (#20).

Covers :mod:`pbirb_mcp.core.names`, every creator routed through it, the
generated-name patterns (one test each), and the ``duplicate-name`` /
``invalid-identifier`` lint rules.
"""

from __future__ import annotations

import collections
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import names
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, q
from pbirb_mcp.ops import (
    body,
    chart,
    dataset,
    datasource,
    embedded_images,
    layout,
    parameters,
    scratch,
    tablix,
    tablix_columns,
    tablix_static,
    tablix_subtotals,
    templates,
)
from pbirb_mcp.ops import header_footer as hf
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.styling import set_textbox_style

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "pbi_paginated_minimal.rdl"
CHART_FIXTURE = FIXTURES / "pbi_chart_rich.rdl"

BAD_NAMES = ["", "My Box", "1st", "A&B", "Bad Name & <x>"]
BOX = ("0in", "0in", "1in", "0.25in")
SCOPE_KINDS = set(names.SCOPE_KINDS)


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def chart_path(tmp_path: Path) -> Path:
    dest = tmp_path / "chart.rdl"
    shutil.copy(CHART_FIXTURE, dest)
    return dest


@pytest.fixture
def png_path(tmp_path: Path) -> Path:
    p = tmp_path / "logo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return p


def _scope_counter(path: Path) -> collections.Counter:
    tree = etree.parse(str(path))
    return collections.Counter(
        e.get("Name")
        for e in tree.iter()
        if isinstance(e.tag, str)
        and etree.QName(e).namespace == RDL_NS
        and etree.QName(e).localname in SCOPE_KINDS
        and e.get("Name") is not None
    )


def _dupes(path: Path) -> dict:
    return {k: v for k, v in _scope_counter(path).items() if v > 1}


def _rules(path: Path, rule: str) -> list[dict]:
    return lint_report(str(path), rules=[rule])["issues"]


def _textbox(path: Path, name: str) -> etree._Element:
    doc = RDLDocument.open(path)
    hits = [tb for tb in doc.root.iter(q("Textbox")) if tb.get("Name") == name]
    assert len(hits) == 1, f"{name}: {len(hits)} textboxes"
    return hits[0]


def _matrix(path: Path) -> None:
    """MainTable with a row group 'Region' and a column group 'Year'."""
    tablix.add_row_group(str(path), "MainTable", "Region", "=Fields!ProductName.Value")
    tablix_columns.add_column_group(str(path), "MainTable", "Year", "=Fields!ProductID.Value")


# ---- core helpers -----------------------------------------------------------


class TestValidateIdentifier:
    @pytest.mark.parametrize("bad", BAD_NAMES + ["_x", "x-y", "x.y", " x", "x\n", None, 5])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            names.validate_identifier(bad, "textbox")
        assert not names.is_identifier(bad)

    @pytest.mark.parametrize("good", ["A", "Textbox1", "Header_Amount", "Größe", "x_1_y"])
    def test_accepts(self, good):
        assert names.validate_identifier(good, "textbox") == good
        assert names.is_identifier(good)

    def test_length_cap(self):
        cap = names.MAX_IDENTIFIER_LENGTH
        assert names.validate_identifier("A" * cap) == "A" * cap
        with pytest.raises(ValueError, match="limit"):
            names.validate_identifier("A" * (cap + 1))

    def test_message_names_kind_and_suggests(self):
        with pytest.raises(ValueError, match=r"invalid parameter name 'My Box'.*'My_Box'"):
            names.validate_identifier("My Box", "parameter")

    @pytest.mark.parametrize(
        "raw,expected",
        [("My Box", "My_Box"), ("1st", "Item1st"), ("A&B", "A_B"), ("", "Item"), ("_x", "x")],
    )
    def test_sanitize(self, raw, expected):
        assert names.sanitize_identifier(raw) == expected
        assert names.is_identifier(names.sanitize_identifier(raw))


class TestRegistry:
    def test_report_scope_names_cover_items_groups_and_sections(self, rdl_path):
        hf.add_header_textbox(str(rdl_path), "HdrBox", "x", *BOX)
        layout.add_list(str(rdl_path), "L", "MainDataset", *BOX)
        got = names.report_scope_names(RDLDocument.open(rdl_path))
        assert {"MainTable", "Amount", "Details", "HdrBox", "L", "L_Rect", "L_Details"} <= got
        # Per-kind namespaces are not report scope.
        assert not {"MainDataset", "DateFrom", "PowerBIDataset"} & got

    def test_ensure_unique_names_the_clash_location(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        with pytest.raises(ValueError, match=r"Textbox 'Amount' in Tablix 'MainTable' in Body"):
            names.ensure_unique(doc, "Fresh", "Amount")
        with pytest.raises(ValueError, match="Group 'Details'"):
            names.ensure_unique(doc, "Details")
        with pytest.raises(ValueError, match="twice"):
            names.ensure_unique(doc, "New1", "New1")
        names.ensure_unique(doc, "New1", "New2")

    def test_unique_name_report_builder_suffixes(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        taken: set[str] = set()
        assert names.unique_name(doc, "Details", taken) == "Details1"
        assert names.unique_name(doc, "Details", taken) == "Details2"
        assert names.unique_name(doc, "Fresh", taken) == "Fresh"
        assert names.unique_name(doc, "Fresh", taken) == "Fresh1"
        assert taken == {"Details1", "Details2", "Fresh", "Fresh1"}
        # Without `taken`, nothing is remembered between calls.
        assert names.unique_name(doc, "Details") == "Details1"

    def test_unique_name_after_trailing_digit_uses_underscore(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        taken = {"Region_Header_0"}
        assert names.unique_name(doc, "Region_Header_0", taken) == "Region_Header_0_1"

    def test_unique_name_sanitizes_and_caps(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        assert names.unique_name(doc, "T_Total Sales") == "T_Total_Sales"
        long = "A" * names.MAX_IDENTIFIER_LENGTH
        taken = {long}
        got = names.unique_name(doc, long, taken)
        assert len(got) == names.MAX_IDENTIFIER_LENGTH and got.endswith("1")


# ---- every creator: identifier validation + report-wide uniqueness -----------


def _ds_add(p, n):
    return datasource.add_data_source(p, n, "Workspace", "Model")


# kind -> creator(path, name, png_path). Each emits ``name`` verbatim.
CREATORS = {
    "body_textbox": lambda p, n, png: body.add_body_textbox(p, n, "x", *BOX),
    "body_image": lambda p, n, png: body.add_body_image(p, n, "External", "u", *BOX),
    "header_textbox": lambda p, n, png: hf.add_header_textbox(p, n, "x", *BOX),
    "footer_textbox": lambda p, n, png: hf.add_footer_textbox(p, n, "x", *BOX),
    "header_image": lambda p, n, png: hf.add_header_image(p, n, "External", "u", *BOX),
    "footer_image": lambda p, n, png: hf.add_footer_image(p, n, "External", "u", *BOX),
    "rectangle": lambda p, n, png: layout.add_rectangle(p, n, *BOX),
    "list": lambda p, n, png: layout.add_list(p, n, "MainDataset", *BOX),
    "line": lambda p, n, png: layout.add_line(p, n, "0in", "0in", "1in", "0in"),
    "tablix_template": lambda p, n, png: templates.insert_tablix_from_template(
        p, n, "MainDataset", ["Amount"], *BOX
    ),
    "chart_template": lambda p, n, png: chart.insert_chart_from_template(
        p, n, "MainDataset", "ProductName", "Amount", *BOX
    ),
    "row_group": lambda p, n, png: tablix.add_row_group(p, "MainTable", n, "=1"),
    "column_group": lambda p, n, png: tablix_columns.add_column_group(p, "MainTable", n, "=1"),
    "tablix_column": lambda p, n, png: tablix_columns.add_tablix_column(
        p, "MainTable", n, "=Fields!Amount.Value"
    ),
    "static_row": lambda p, n, png: tablix_static.add_static_row(p, "MainTable", n, ["a"]),
    "static_column": lambda p, n, png: tablix_static.add_static_column(p, "MainTable", n, ["a"]),
}

# Per-kind namespaces: identifier validation only (own uniqueness rules).
PER_KIND_CREATORS = {
    "parameter": lambda p, n, png: parameters.add_parameter(p, n, "String"),
    "rename_parameter": lambda p, n, png: parameters.rename_parameter(p, "DateFrom", n),
    "calculated_field": lambda p, n, png: dataset.add_calculated_field(p, "MainDataset", n, "=1"),
    "dataset_field": lambda p, n, png: dataset.add_dataset_field(p, "MainDataset", n, "Col"),
    "data_source": lambda p, n, png: _ds_add(p, n),
    "rename_data_source": lambda p, n, png: datasource.rename_data_source(p, "PowerBIDataset", n),
    "embedded_image": lambda p, n, png: embedded_images.add_embedded_image(
        p, n, "image/png", str(png)
    ),
}

ALL_CREATORS = {**CREATORS, **PER_KIND_CREATORS}


class TestIdentifierRejectedForEveryKind:
    @pytest.mark.parametrize("bad", BAD_NAMES)
    @pytest.mark.parametrize("kind", sorted(ALL_CREATORS))
    def test_bad_name_rejected_before_any_write(self, rdl_path, png_path, kind, bad):
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError):
            ALL_CREATORS[kind](str(rdl_path), bad, png_path)
        assert rdl_path.read_bytes() == before

    @pytest.mark.parametrize("kind", sorted(ALL_CREATORS))
    def test_good_name_accepted(self, rdl_path, png_path, kind):
        ALL_CREATORS[kind](str(rdl_path), "Fresh_Name1", png_path)
        assert _dupes(rdl_path) == {}
        assert _rules(rdl_path, "invalid-identifier") == []
        assert _rules(rdl_path, "duplicate-name") == []

    @pytest.mark.parametrize("bad", BAD_NAMES)
    def test_create_report_datasource_name(self, tmp_path, bad):
        dst = tmp_path / "new.rdl"
        with pytest.raises(ValueError):
            scratch.create_report(str(dst), datasource={"name": bad})
        assert not dst.exists()


class TestReportWideUniqueness:
    # Names that already exist somewhere OTHER than the body's top level:
    # a tablix cell textbox and a tablix group, plus a header textbox.
    @pytest.mark.parametrize("taken", ["Amount", "HeaderAmount", "Details", "HdrBox"])
    @pytest.mark.parametrize("kind", sorted(CREATORS))
    def test_clash_raises_before_any_write(self, rdl_path, png_path, kind, taken):
        hf.add_header_textbox(str(rdl_path), "HdrBox", "x", *BOX)
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match="already exists"):
            CREATORS[kind](str(rdl_path), taken, png_path)
        assert rdl_path.read_bytes() == before

    def test_group_name_unique_across_tablixes(self, rdl_path):
        templates.insert_tablix_from_template(str(rdl_path), "T2", "MainDataset", ["Amount"], *BOX)
        tablix.add_row_group(str(rdl_path), "MainTable", "Region", "=1")
        with pytest.raises(ValueError, match="Group 'Region' in Tablix 'MainTable'"):
            tablix.add_row_group(str(rdl_path), "T2", "Region", "=1")
        with pytest.raises(ValueError, match="already exists"):
            tablix_columns.add_column_group(str(rdl_path), "T2", "Region", "=1")


# ---- generated-name patterns (one test per pattern) ---------------------------


class TestGeneratedNames:
    def test_template_details_group_suffixed(self, rdl_path):
        r = templates.insert_tablix_from_template(
            str(rdl_path), "T2", "MainDataset", ["Amount"], *BOX
        )
        assert r["details_group"] == "Details1"
        r = templates.insert_tablix_from_template(
            str(rdl_path), "T3", "MainDataset", ["Amount"], *BOX
        )
        assert r["details_group"] == "Details2"
        assert _dupes(rdl_path) == {}

    def test_template_cell_textboxes_suffixed(self, rdl_path):
        body.add_body_textbox(str(rdl_path), "T2_Amount", "x", "3in", "0in", "1in", "0.25in")
        body.add_body_textbox(str(rdl_path), "T2_Header_Amount", "x", "4in", "0in", "1in", "1in")
        r = templates.insert_tablix_from_template(
            str(rdl_path), "T2", "MainDataset", ["Amount", "ProductID"], *BOX
        )
        assert r["header_textboxes"] == ["T2_Header_Amount1", "T2_Header_ProductID"]
        assert r["detail_textboxes"] == ["T2_Amount1", "T2_ProductID"]
        assert r["columns"] == ["Amount", "ProductID"]
        assert _dupes(rdl_path) == {}

    def test_template_rejects_bad_or_repeated_columns(self, rdl_path):
        with pytest.raises(ValueError):
            templates.insert_tablix_from_template(
                str(rdl_path), "T2", "MainDataset", ["My Col"], *BOX
            )
        with pytest.raises(ValueError, match="more than once"):
            templates.insert_tablix_from_template(
                str(rdl_path), "T2", "MainDataset", ["Amount", "Amount"], *BOX
            )

    def test_list_rect_and_details_suffixed(self, rdl_path):
        body.add_body_textbox(str(rdl_path), "L_Rect", "x", *BOX)
        body.add_body_textbox(str(rdl_path), "L_Details", "x", *BOX)
        r = layout.add_list(str(rdl_path), "L", "MainDataset", *BOX)
        assert r["rectangle"] == "L_Rect1"
        assert r["details_group"] == "L_Details1"
        assert _dupes(rdl_path) == {}

    def test_add_list_x_then_x_rect(self, rdl_path):
        layout.add_list(str(rdl_path), "X", "MainDataset", *BOX)
        with pytest.raises(ValueError, match="Rectangle 'X_Rect' in Tablix 'X'"):
            layout.add_list(str(rdl_path), "X_Rect", "MainDataset", *BOX)
        assert _dupes(rdl_path) == {}

    def test_add_list_x_rect_then_x(self, rdl_path):
        layout.add_list(str(rdl_path), "X_Rect", "MainDataset", *BOX)
        r = layout.add_list(str(rdl_path), "X", "MainDataset", *BOX)
        assert r["rectangle"] == "X_Rect1"
        assert _dupes(rdl_path) == {}

    def test_row_group_header_textboxes_suffixed(self, rdl_path):
        body.add_body_textbox(str(rdl_path), "Region_Header_0", "x", *BOX)
        r = tablix.add_row_group(str(rdl_path), "MainTable", "Region", "=1")
        assert r["header_textboxes"] == ["Region_Header_0_1", "Region_Header_1", "Region_Header_2"]
        assert _dupes(rdl_path) == {}

    def test_column_group_header_textboxes_suffixed(self, rdl_path):
        body.add_body_textbox(str(rdl_path), "Year_HeaderCol_1", "x", *BOX)
        r = tablix_columns.add_column_group(str(rdl_path), "MainTable", "Year", "=1")
        assert r["header_textboxes"] == ["Year_HeaderCol_0", "Year_HeaderCol_1_1"]
        assert _dupes(rdl_path) == {}

    def test_tablix_column_row_cells_suffixed(self, rdl_path):
        body.add_body_textbox(str(rdl_path), "Qty_0", "x", *BOX)
        r = tablix_columns.add_tablix_column(
            str(rdl_path), "MainTable", "Qty", "=Fields!Amount.Value"
        )
        assert r["column_name"] == "Qty"
        assert r["textboxes"] == ["Qty_0_1", "Qty"]
        assert _dupes(rdl_path) == {}

    def test_static_row_cells_suffixed(self, rdl_path):
        body.add_body_textbox(str(rdl_path), "Banner_2", "x", *BOX)
        r = tablix_static.add_static_row(str(rdl_path), "MainTable", "Banner", ["a"])
        assert r["textboxes"] == ["Banner", "Banner_1", "Banner_2_1"]
        assert _dupes(rdl_path) == {}

    def test_static_column_cells_suffixed(self, rdl_path):
        body.add_body_textbox(str(rdl_path), "Note_1", "x", *BOX)
        r = tablix_static.add_static_column(str(rdl_path), "MainTable", "Note", ["a"])
        assert r["textboxes"] == ["Note", "Note_1_1"]
        assert _dupes(rdl_path) == {}

    def test_subtotal_header_row_does_not_collide_with_group_header(self, rdl_path):
        tablix.add_row_group(str(rdl_path), "MainTable", "Region", "=1")
        r = tablix_subtotals.add_subtotal_row(
            str(rdl_path),
            "MainTable",
            "Region",
            [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
            position="header",
        )
        # add_row_group already emitted Region_Header_0..2.
        assert r["textboxes"] == ["Region_Header_0_1", "Region_Header_1_1", "Region_Header_2_1"]
        assert _dupes(rdl_path) == {}

    def test_subtotal_footer_row_twice(self, rdl_path):
        tablix.add_row_group(str(rdl_path), "MainTable", "Region", "=1")
        aggs = [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}]
        r1 = tablix_subtotals.add_subtotal_row(str(rdl_path), "MainTable", "Region", aggs)
        r2 = tablix_subtotals.add_subtotal_row(str(rdl_path), "MainTable", "Region", aggs)
        assert r1["textboxes"] == ["Region_Footer_0", "Region_Footer_1", "Region_Footer_2"]
        assert r2["textboxes"] == ["Region_Footer_0_1", "Region_Footer_1_1", "Region_Footer_2_1"]
        assert _dupes(rdl_path) == {}

    def test_subtotal_column_twice(self, rdl_path):
        _matrix(rdl_path)
        r1 = tablix_subtotals.add_subtotal_column(str(rdl_path), "MainTable", "Year", [])
        r2 = tablix_subtotals.add_subtotal_column(str(rdl_path), "MainTable", "Year", [])
        assert r1["textboxes"][0] == "Year_TotalColumn_0"
        assert r2["textboxes"][0] == "Year_TotalColumn_0_1"
        assert _dupes(rdl_path) == {}

    def test_tablix_corner_suffixed_and_idempotent(self, rdl_path):
        _matrix(rdl_path)
        r1 = tablix.set_tablix_corner(str(rdl_path), "MainTable", text="a")
        assert r1["name"] == "MainTable_Corner"
        r2 = tablix.set_tablix_corner(str(rdl_path), "MainTable", text="a")
        assert r2["name"] == "MainTable_Corner"  # replaced corner isn't a clash
        assert _dupes(rdl_path) == {}

    def test_tablix_corner_suffixed_on_foreign_clash(self, rdl_path):
        _matrix(rdl_path)
        body.add_body_textbox(str(rdl_path), "MainTable_Corner", "x", *BOX)
        r = tablix.set_tablix_corner(str(rdl_path), "MainTable", text="a")
        assert r["name"] == "MainTable_Corner1"
        assert _dupes(rdl_path) == {}

    def test_chart_category_group_suffixed(self, rdl_path):
        body.add_body_textbox(str(rdl_path), "C_CategoryGroup", "x", *BOX)
        r = chart.insert_chart_from_template(
            str(rdl_path), "C", "MainDataset", "ProductName", "Amount", *BOX
        )
        assert r["category_group"] == "C_CategoryGroup1"
        assert _dupes(rdl_path) == {}

    def test_chart_series_group_suffixed(self, chart_path):
        body.add_body_textbox(str(chart_path), "Amount_Group", "x", "5in", "0in", "1in", "1in")
        r = chart.set_chart_series_grouping(
            str(chart_path), "SalesByProduct", "Amount", group_field="ProductName"
        )
        assert r["group_name"] == "Amount_Group1"
        # replace=True frees the old name first: still Amount_Group1.
        r = chart.set_chart_series_grouping(
            str(chart_path), "SalesByProduct", "Amount", group_field="ProductID", replace=True
        )
        assert r["group_name"] == "Amount_Group1"
        assert _dupes(chart_path) == {}

    def test_refresh_dataset_fields_sanitizes_alias(self, rdl_path):
        dataset.update_dataset_query(
            str(rdl_path),
            "MainDataset",
            "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], \"Total Sales\", [Amt])",
        )
        r = dataset.refresh_dataset_fields(str(rdl_path), "MainDataset")
        assert r["added"] == ["Total_Sales"]
        assert any("Total Sales" in w for w in r["warnings"])
        doc = RDLDocument.open(rdl_path)
        field = [f for f in doc.root.iter(q("Field")) if f.get("Name") == "Total_Sales"][0]
        assert field.find(q("DataField")).text == "Total Sales"
        assert _rules(rdl_path, "invalid-identifier") == []
        out_of_sync = _rules(rdl_path, "dataset-fields-out-of-sync")
        assert not [i for i in out_of_sync if "Total_Sales" in i["location"]]
        # A second refresh is a no-op (no Total_Sales1).
        r = dataset.refresh_dataset_fields(str(rdl_path), "MainDataset")
        assert r["added"] == []


# ---- the issue's repro ----------------------------------------------------------


def test_issue_repro_yields_no_duplicates(rdl_path):
    p = str(rdl_path)
    with pytest.raises(ValueError):
        body.add_body_textbox(p, "ProductID", "x", "3in", "0in", "1in", "0.25in")
    with pytest.raises(ValueError):
        hf.add_header_textbox(p, "Amount", "x", "0in", "0in", "1in", "0.25in")
    r = templates.insert_tablix_from_template(
        p, "T2", "MainDataset", ["Amount"], "2in", "0in", "1in", "0.5in"
    )
    assert r["details_group"] == "Details1"
    tablix.add_row_group(p, "MainTable", "Region", "=Fields!ProductName.Value")
    with pytest.raises(ValueError):
        tablix.add_row_group(p, "T2", "Region", "=Fields!ProductName.Value")
    with pytest.raises(ValueError):
        tablix_columns.add_column_group(p, "T2", "HeaderAmount", "=Fields!ProductID.Value")
    layout.add_list(p, "X", "MainDataset", "4in", "0in", "1in", "1in")
    with pytest.raises(ValueError):
        layout.add_list(p, "X_Rect", "MainDataset", "5in", "0in", "1in", "1in")
    assert _dupes(rdl_path) == {}
    assert _rules(rdl_path, "duplicate-name") == []
    # The name-addressed edit that used to raise AmbiguousElementError works.
    set_textbox_style(p, "Amount", font_weight="Bold")


# ---- lint rules -----------------------------------------------------------------


def _patch(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) >= count, old
    path.write_text(text.replace(old, new, count), encoding="utf-8")


ALL_FIXTURES = sorted(FIXTURES.glob("**/*.rdl"))


class TestLintRules:
    @pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda p: p.name)
    def test_clean_on_every_fixture(self, fixture):
        issues = lint_report(str(fixture), rules=["duplicate-name", "invalid-identifier"])
        assert issues["issues"] == []

    def test_duplicate_textbox_flagged(self, rdl_path):
        _patch(rdl_path, 'Name="HeaderAmount"', 'Name="Amount"')
        issues = _rules(rdl_path, "duplicate-name")
        assert len(issues) == 1
        assert issues[0]["severity"] == "error"
        assert "'Amount'" in issues[0]["message"] and "2 times" in issues[0]["message"]

    def test_duplicate_group_vs_report_item_flagged(self, rdl_path):
        _patch(rdl_path, 'Name="Details"', 'Name="MainTable"')
        issues = _rules(rdl_path, "duplicate-name")
        assert [i["rule"] for i in issues] == ["duplicate-name"]
        assert "Group 'MainTable'" in issues[0]["message"]

    def test_duplicate_across_header_and_body_flagged(self, rdl_path):
        hf.add_header_textbox(str(rdl_path), "Hdr", "x", *BOX)
        _patch(rdl_path, 'Name="Hdr"', 'Name="ProductName"')
        issues = _rules(rdl_path, "duplicate-name")
        assert len(issues) == 1 and "PageHeader" in issues[0]["message"]

    def test_duplicate_parameter_and_field_flagged(self, rdl_path):
        _patch(rdl_path, 'ReportParameter Name="DateTo"', 'ReportParameter Name="DateFrom"')
        _patch(rdl_path, 'Field Name="ProductName"', 'Field Name="ProductID"')
        locs = sorted(i["location"] for i in _rules(rdl_path, "duplicate-name"))
        assert locs == [
            "DataSet[Name='MainDataset']/Field[Name='ProductID']",
            "ReportParameter[Name='DateFrom']",
        ]

    @pytest.mark.parametrize("bad", BAD_NAMES)
    def test_invalid_textbox_name_flagged(self, rdl_path, bad):
        _patch(
            rdl_path,
            'Name="HeaderAmount"',
            f'Name="{bad.replace("&", "&amp;").replace("<", "&lt;")}"',
        )
        issues = _rules(rdl_path, "invalid-identifier")
        assert len(issues) == 1 and issues[0]["severity"] == "error"

    @pytest.mark.parametrize(
        "old",
        [
            'ReportParameter Name="DateTo"',
            'DataSet Name="MainDataset"',
            'DataSource Name="PowerBIDataset"',
            'Field Name="Amount"',
            'Group Name="Details"',
            'Tablix Name="MainTable"',
        ],
    )
    def test_invalid_identifier_per_kind(self, rdl_path, old):
        tag = old.split()[0]
        _patch(rdl_path, old, f'{tag} Name="My Box"')
        issues = _rules(rdl_path, "invalid-identifier")
        assert len(issues) == 1
        assert "'My Box'" in issues[0]["message"] and "My_Box" in issues[0]["suggestion"]

    def test_invalid_embedded_image_flagged(self, rdl_path, png_path):
        embedded_images.add_embedded_image(str(rdl_path), "Logo", "image/png", str(png_path))
        _patch(rdl_path, 'EmbeddedImage Name="Logo"', 'EmbeddedImage Name="1st"')
        issues = _rules(rdl_path, "invalid-identifier")
        assert len(issues) == 1 and "EmbeddedImage" in issues[0]["location"]
