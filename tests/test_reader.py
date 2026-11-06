"""Reader tool tests.

The reader surface is the LLM's situational awareness — every multi-step
edit starts with one of these. Output schemas are stable: callers chain on
field names like ``datasets[0].command_text`` and ``tablixes[*].row_groups``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.ops import parameters as P
from pbirb_mcp.ops.dataset import get_dataset
from pbirb_mcp.ops.header_footer import set_page_header
from pbirb_mcp.ops.reader import (
    describe_report,
    find_textbox_by_value,
    get_datasets,
    get_parameters,
    get_tablixes,
    list_body_items,
    list_header_items,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools
from tests._designer_state import build_designer_state_report

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
FIXTURE_MULTI = Path(__file__).parent / "fixtures" / "pbi_multi_datasource.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


# ---- describe_report -------------------------------------------------------


class TestDescribeReport:
    def test_returns_top_level_inventory(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert out["path"] == str(rdl_path)
        assert out["data_sources"] == ["PowerBIDataset"]
        assert out["datasets"] == ["MainDataset"]
        assert out["parameters"] == ["DateFrom", "DateTo"]
        # v0.4 BREAKING — tablixes is a list of rich shape dicts, not
        # bare strings. Use [t["name"] for t in out["tablixes"]] to
        # recover the old shape.
        assert len(out["tablixes"]) == 1
        assert out["tablixes"][0]["name"] == "MainTable"
        assert out["page"]["height"] == "11in"
        assert out["page"]["width"] == "8.5in"

    def test_tablixes_carry_shape_hints(self, rdl_path):
        """v0.4 BREAKING — each tablix entry is now
        {name, rows, columns, has_groups, has_subtotals, has_spans}.
        Saves a follow-up get_tablixes round-trip when planning edits
        (e.g. choose style_tablix_row vs set_textbox_style).
        Minimal fixture: 1 header row + 1 details row, 3 columns, no
        named groups, no subtotals, no spans.
        """
        out = describe_report(path=str(rdl_path))
        t = out["tablixes"][0]
        assert set(t.keys()) == {
            "name",
            "rows",
            "columns",
            "has_groups",
            "has_subtotals",
            "has_spans",
            "column_details",  # #59
        }
        assert t["name"] == "MainTable"
        assert t["rows"] == 2
        assert t["columns"] == 3
        assert t["has_groups"] is False
        assert t["has_subtotals"] is False
        assert t["has_spans"] is False

    def test_has_groups_true_after_add_row_group(self, rdl_path):
        from pbirb_mcp.ops.tablix import add_row_group

        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!ProductName.Value",
        )
        out = describe_report(path=str(rdl_path))
        t = out["tablixes"][0]
        assert t["has_groups"] is True

    def test_has_spans_true_after_set_cell_span(self, rdl_path):
        from pbirb_mcp.ops.tablix_cells import set_cell_span

        set_cell_span(
            path=str(rdl_path),
            tablix_name="MainTable",
            row_index=0,
            column_name="HeaderProductID",
            col_span=2,
            force=True,
        )
        out = describe_report(path=str(rdl_path))
        assert out["tablixes"][0]["has_spans"] is True

    def test_has_subtotals_true_after_add_subtotal_row(self, rdl_path):
        from pbirb_mcp.ops.tablix import add_row_group
        from pbirb_mcp.ops.tablix_subtotals import add_subtotal_row

        add_row_group(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            group_expression="=Fields!ProductName.Value",
        )
        add_subtotal_row(
            path=str(rdl_path),
            tablix_name="MainTable",
            group_name="Region",
            position="footer",
            aggregates=[{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        out = describe_report(path=str(rdl_path))
        assert out["tablixes"][0]["has_subtotals"] is True

    def test_charts_empty_on_minimal_fixture(self, rdl_path):
        """v0.4 — charts is a top-level array parallel to tablixes.
        Minimal fixture has no Chart elements -> empty list."""
        out = describe_report(path=str(rdl_path))
        assert out["charts"] == []

    def test_charts_lists_named_charts(self, tmp_path):
        """v0.4 — chart-rich fixture surfaces every <Chart Name="..."> as
        a bare name in the new top-level ``charts`` array (same shape as
        ``tablixes``)."""
        chart_fixture = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"
        dest = tmp_path / "report.rdl"
        shutil.copy(chart_fixture, dest)
        out = describe_report(path=str(dest))
        assert "SalesByProduct" in out["charts"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            describe_report(path=str(tmp_path / "missing.rdl"))


class TestDescribeReportV03Extensions:
    """Phase 12 commit 44 — backward-compatible new fields."""

    def test_existing_fields_unchanged(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        # Spot-check the v0.2 keys still resolve.
        assert "data_sources" in out
        assert "tablixes" in out
        assert "page" in out

    def test_parameter_layout_none_without_block(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert out["parameter_layout"] is None

    def test_parameter_layout_summarises_grid(self, rdl_path):
        from pbirb_mcp.ops.parameters import set_parameter_layout

        set_parameter_layout(
            path=str(rdl_path),
            rows=1,
            columns=2,
            parameter_order=["DateFrom", "DateTo"],
        )
        out = describe_report(path=str(rdl_path))
        layout = out["parameter_layout"]
        assert layout == {
            "rows": 1,
            "columns": 2,
            "cell_count": 2,
            "parameters_count": 2,
            "in_sync": True,
        }

    def test_embedded_images_lists_inventory(self, rdl_path, tmp_path):
        # Create a tiny PNG so add_embedded_image accepts it.
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        png_path = tmp_path / "logo.png"
        png_path.write_bytes(png_bytes)
        from pbirb_mcp.ops.embedded_images import add_embedded_image

        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        out = describe_report(path=str(rdl_path))
        assert len(out["embedded_images"]) == 1
        entry = out["embedded_images"][0]
        assert entry["name"] == "Logo"
        assert entry["mime_type"] == "image/png"
        assert entry["byte_size"] == len(png_bytes)

    def test_embedded_images_empty_when_no_block(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert out["embedded_images"] == []

    def test_dataset_query_parameters_lists_bindings(self, rdl_path):
        from pbirb_mcp.ops.dataset import add_query_parameter

        add_query_parameter(
            path=str(rdl_path),
            dataset_name="MainDataset",
            name="DateFromParam",
            value_expression="=Parameters!DateFrom.Value",
        )
        out = describe_report(path=str(rdl_path))
        assert len(out["dataset_query_parameters"]) == 1
        qp = out["dataset_query_parameters"][0]
        assert qp == {
            "dataset": "MainDataset",
            "name": "DateFromParam",
            "value": "=Parameters!DateFrom.Value",
        }

    def test_designer_state_present_false_by_default(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert out["designer_state_present"] is False

    def test_designer_state_present_true_when_block_exists(self, rdl_path):
        # Inject a DesignerState block and confirm the flag flips.
        from lxml import etree

        from pbirb_mcp.core.document import RDLDocument
        from pbirb_mcp.core.xpath import RDL_NS, find_child, qrd

        doc = RDLDocument.open(rdl_path)
        ds = doc.root.find(f".//{{{RDL_NS}}}DataSet")
        query = find_child(ds, "Query")
        designer = etree.SubElement(query, qrd("DesignerState"))
        etree.SubElement(designer, qrd("Statement")).text = "EVALUATE 'Sales'"
        doc.save()
        out = describe_report(path=str(rdl_path))
        assert out["designer_state_present"] is True


# ---- #52: complete inventory ------------------------------------------------

_INVENTORY_EXTRA = (
    '<Rectangle Name="Rect1"><ReportItems>'
    '<Textbox Name="Inner"><Paragraphs><Paragraph><TextRuns><TextRun><Value>x</Value>'
    "</TextRun></TextRuns></Paragraph></Paragraphs>"
    "<Top>0.1in</Top><Left>0.2in</Left><Height>0.25in</Height><Width>1in</Width></Textbox>"
    '<Rectangle Name="Rect2"><ReportItems>'
    '<Textbox Name="Deep"><Paragraphs><Paragraph><TextRuns><TextRun><Value>y</Value>'
    "</TextRun></TextRuns></Paragraph></Paragraphs></Textbox>"
    "</ReportItems><Top>0.5in</Top><Height>0.4in</Height><Width>1in</Width></Rectangle>"
    "</ReportItems><Top>3in</Top><Height>1in</Height><Width>2in</Width></Rectangle>"
    '<Subreport Name="Sub1"><ReportName>Child</ReportName>'
    '<Parameters><Parameter Name="Region"><Value>=Parameters!DateFrom.Value</Value></Parameter>'
    "</Parameters><Top>4in</Top><Height>1in</Height><Width>2in</Width></Subreport>"
    '<GaugePanel Name="Gauge1"><Top>5in</Top><Height>1in</Height><Width>2in</Width></GaugePanel>'
    '<CustomReportItem Name="CRI1"><Type>x</Type><Top>6in</Top><Height>1in</Height>'
    "<Width>2in</Width></CustomReportItem>"
)


@pytest.fixture
def inventory_path(tmp_path: Path) -> Path:
    """The #52 repro: a Rectangle with nested items, a Subreport, a
    GaugePanel and a CustomReportItem; PageHeight / PageWidth removed so
    the RDL defaults apply."""
    src = FIXTURE.read_text(encoding="utf-8")
    marker = "</Tablix>\n        </ReportItems>"
    assert marker in src
    src = src.replace(marker, "</Tablix>" + _INVENTORY_EXTRA + "</ReportItems>")
    src = src.replace("<PageHeight>11in</PageHeight>", "").replace(
        "<PageWidth>8.5in</PageWidth>", ""
    )
    dest = tmp_path / "inventory.rdl"
    dest.write_text(src, encoding="utf-8")
    return dest


class TestDescribeReportInventory:
    def test_list_body_items_includes_gauge_and_custom_item(self, inventory_path):
        items = list_body_items(path=str(inventory_path))
        assert [(i["name"], i["type"]) for i in items] == [
            ("MainTable", "Tablix"),
            ("Rect1", "Rectangle"),
            ("Sub1", "Subreport"),
            ("Gauge1", "GaugePanel"),
            ("CRI1", "CustomReportItem"),
        ]
        assert all(i["parent"] is None for i in items)

    def test_list_body_items_recursive_lists_nested_items(self, inventory_path):
        items = list_body_items(path=str(inventory_path), recursive=True)
        assert [(i["name"], i["parent"]) for i in items] == [
            ("MainTable", None),
            ("Rect1", None),
            ("Inner", "Rect1"),
            ("Rect2", "Rect1"),
            ("Deep", "Rect2"),
            ("Sub1", None),
            ("Gauge1", None),
            ("CRI1", None),
        ]
        inner = next(i for i in items if i["name"] == "Inner")
        assert (inner["top"], inner["left"], inner["width"]) == ("0.1in", "0.2in", "1in")

    def test_describe_report_body_items_include_all_item_types(self, inventory_path):
        out = describe_report(path=str(inventory_path))
        names = [i["name"] for i in out["body_items"]]
        assert names == ["MainTable", "Rect1", "Sub1", "Gauge1", "CRI1"]

    def test_nested_items_carry_parent_and_region(self, inventory_path):
        out = describe_report(path=str(inventory_path))
        nested = {i["name"]: i for i in out["nested_items"]}
        assert set(nested) == {"Inner", "Rect2", "Deep"}
        assert nested["Inner"]["parent"] == "Rect1"
        assert nested["Deep"]["parent"] == "Rect2"
        assert nested["Inner"]["region"] == "body"
        assert nested["Inner"]["type"] == "Textbox"

    def test_subreport_gauge_map_inventories(self, inventory_path):
        out = describe_report(path=str(inventory_path))
        assert out["subreports"] == [
            {
                "name": "Sub1",
                "report_name": "Child",
                "parameters": [{"name": "Region", "value": "=Parameters!DateFrom.Value"}],
            }
        ]
        assert out["gauges"] == ["Gauge1"]
        assert out["maps"] == []

    def test_body_size_reported(self, inventory_path):
        out = describe_report(path=str(inventory_path))
        # <Body>/<Height> and the section's <Width>.
        assert out["body"] == {"height": "2in", "width": "5in"}

    def test_missing_page_size_filled_with_rdl_defaults(self, inventory_path):
        page = describe_report(path=str(inventory_path))["page"]
        assert page["height"] == "11in"
        assert page["width"] == "8.5in"
        assert page["defaulted"] == ["height", "width"]
        # Margins present in the file are reported as written.
        assert page["margin_left"] == "1in"

    def test_missing_margins_default_to_zero(self, rdl_path):
        src = rdl_path.read_text(encoding="utf-8")
        for local in ("LeftMargin", "RightMargin", "TopMargin", "BottomMargin"):
            src = src.replace(f"<{local}>1in</{local}>", "")
        rdl_path.write_text(src, encoding="utf-8")
        page = describe_report(path=str(rdl_path))["page"]
        assert page["margin_left"] == page["margin_top"] == "0in"
        assert page["defaulted"] == ["margin_top", "margin_bottom", "margin_left", "margin_right"]

    def test_fully_specified_page_has_empty_defaulted(self, rdl_path):
        out = describe_report(path=str(rdl_path))
        assert out["page"]["defaulted"] == []
        assert out["nested_items"] == []
        assert out["subreports"] == []
        assert out["gauges"] == []

    def test_header_rectangle_children(self, rdl_path):
        set_page_header(path=str(rdl_path), height="1in")
        src = rdl_path.read_text(encoding="utf-8")
        header_items = (
            '<ReportItems><Rectangle Name="HeaderBox"><ReportItems>'
            '<Textbox Name="HeaderTitle"><Paragraphs><Paragraph><TextRuns><TextRun>'
            "<Value>T</Value></TextRun></TextRuns></Paragraph></Paragraphs>"
            "<Height>0.25in</Height><Width>1in</Width></Textbox></ReportItems>"
            "<Height>0.8in</Height><Width>3in</Width></Rectangle></ReportItems>"
        )
        assert "<PageHeader>" in src
        src = src.replace("<PageHeader>", "<PageHeader>" + header_items, 1)
        rdl_path.write_text(src, encoding="utf-8")
        assert [i["name"] for i in list_header_items(path=str(rdl_path))] == ["HeaderBox"]
        deep = list_header_items(path=str(rdl_path), recursive=True)
        assert [(i["name"], i["parent"]) for i in deep] == [
            ("HeaderBox", None),
            ("HeaderTitle", "HeaderBox"),
        ]
        nested = describe_report(path=str(rdl_path))["nested_items"]
        assert [(i["name"], i["region"]) for i in nested] == [("HeaderTitle", "header")]

    def test_positioning_tools_find_gauge_and_custom_item(self, inventory_path):
        # positioning shared the same wrong tag list ("Gauge" / "List").
        from pbirb_mcp.ops.positioning import set_body_item_position

        assert set_body_item_position(str(inventory_path), "Gauge1", "5in", "1in")["changed"]
        assert set_body_item_position(str(inventory_path), "CRI1", "6in", "1in")["changed"]
        items = {i["name"]: i for i in list_body_items(path=str(inventory_path))}
        assert items["Gauge1"]["left"] == items["CRI1"]["left"] == "1in"

    def test_list_items_recursive_via_jsonrpc(self, inventory_path):
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_body_items",
                    "arguments": {"path": str(inventory_path), "recursive": True},
                },
            }
        )
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert "Inner" in [i["name"] for i in payload]


# ---- find_textbox_by_value ---------------------------------------------


class TestFindTextboxByValue:
    def test_returns_empty_when_no_match(self, rdl_path):
        out = find_textbox_by_value(path=str(rdl_path), pattern="NoSuchString")
        assert out == []

    def test_finds_literal_match(self, rdl_path):
        out = find_textbox_by_value(path=str(rdl_path), pattern="Product ID")
        # Fixture's HeaderProductID textbox has "Product ID" as its
        # literal value.
        assert len(out) >= 1
        names = {entry["textbox"] for entry in out}
        assert "HeaderProductID" in names
        for entry in out:
            assert entry["region"] in ("Body", "PageHeader", "PageFooter")

    def test_regex_matches(self, rdl_path):
        out = find_textbox_by_value(path=str(rdl_path), pattern=r"^Product")
        names = {entry["textbox"] for entry in out}
        # All header textboxes start with "Product".
        assert "HeaderProductID" in names
        assert "HeaderProductName" in names

    def test_regex_for_parameter_reference(self, rdl_path):
        # Add a body textbox with a parameter ref, then find it.
        from pbirb_mcp.ops.body import add_body_textbox

        add_body_textbox(
            path=str(rdl_path),
            name="ParamEcho",
            text="=Parameters!DateFrom.Value",
            top="3in",
            left="0.5in",
            width="2in",
            height="0.3in",
        )
        out = find_textbox_by_value(path=str(rdl_path), pattern=r"Parameters!\w+\.Value")
        names = {entry["textbox"] for entry in out}
        assert "ParamEcho" in names

    def test_invalid_regex_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="invalid regex"):
            find_textbox_by_value(path=str(rdl_path), pattern="(unclosed")


class TestFindTextboxByValueRegistration:
    def test_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "find_textbox_by_value" in names


# ---- get_datasets ----------------------------------------------------------


class TestGetDatasets:
    def test_exposes_full_dax_body(self, rdl_path):
        out = get_datasets(path=str(rdl_path))
        assert len(out) == 1
        ds = out[0]
        assert ds["name"] == "MainDataset"
        assert ds["data_source"] == "PowerBIDataset"
        # The full DAX body — not just a sproc name (the upstream rdl-mcp gap).
        assert ds["command_text"] == "EVALUATE 'Sales'"

    def test_lists_fields_with_data_field_and_type(self, rdl_path):
        out = get_datasets(path=str(rdl_path))
        fields = out[0]["fields"]
        assert {f["name"] for f in fields} == {"ProductID", "ProductName", "Amount"}
        amount = next(f for f in fields if f["name"] == "Amount")
        assert amount["data_field"] == "Sales[Amount]"
        assert amount["type_name"] == "System.Decimal"

    def test_query_parameters_default_empty(self, rdl_path):
        out = get_datasets(path=str(rdl_path))
        assert out[0]["query_parameters"] == []

    def test_filters_default_empty(self, rdl_path):
        out = get_datasets(path=str(rdl_path))
        assert out[0]["filters"] == []

    @pytest.mark.parametrize("fixture", sorted(FIXTURE.parent.glob("*.rdl")), ids=lambda p: p.name)
    def test_each_entry_equals_get_dataset(self, fixture):
        # #97: get_datasets and get_dataset used to return different keys
        # (designer_state_present was missing from the former).
        entries = get_datasets(str(fixture))
        assert entries
        for entry in entries:
            assert entry == get_dataset(str(fixture), entry["name"])

    def test_designer_state_present(self, tmp_path):
        path = build_designer_state_report(tmp_path / "ds.rdl")
        entries = get_datasets(str(path))
        assert any(e["designer_state_present"] for e in entries)
        for entry in entries:
            assert entry == get_dataset(str(path), entry["name"])

    def test_concise_view_keeps_designer_state_flag(self, rdl_path):
        from pbirb_mcp.ops.response_format import with_response_format

        reader = with_response_format("get_datasets", get_datasets)
        (ds,) = reader(path=str(rdl_path), response_format="concise")
        assert ds["designer_state_present"] is False
        assert "command_text" not in ds


# ---- get_parameters --------------------------------------------------------


class TestGetParameters:
    def test_lists_report_parameters(self, rdl_path):
        out = get_parameters(path=str(rdl_path))
        assert len(out) == 2
        names = [p["name"] for p in out]
        assert names == ["DateFrom", "DateTo"]

    def test_each_parameter_has_data_type_and_prompt(self, rdl_path):
        out = get_parameters(path=str(rdl_path))
        date_from = next(p for p in out if p["name"] == "DateFrom")
        assert date_from["data_type"] == "DateTime"
        assert date_from["prompt"] == "Date From"

    def test_values_and_layout_none_when_unset(self, rdl_path):
        for p in get_parameters(path=str(rdl_path)):
            assert p["default_values"] is None
            assert p["valid_values"] is None
            assert p["layout"] is None


class TestGetParametersReadBack:
    """#97: defaults, valid values and the layout cell are readable."""

    @pytest.fixture
    def multi_path(self, tmp_path: Path) -> Path:
        dest = tmp_path / "multi.rdl"
        shutil.copy(FIXTURE_MULTI, dest)
        return dest

    def test_issue_repro(self, multi_path):
        path = str(multi_path)
        P.set_parameter_default_values(path, "DateFrom", "static", ["=Today()"])
        P.set_parameter_available_values(
            path, "DateTo", "query", query_dataset="LookupSet", query_value_field="LookupKey"
        )
        P.set_parameter_layout(path, 1, 2, ["DateFrom", "DateTo"])
        date_from, date_to = get_parameters(path)
        assert date_from["default_values"] == {"source": "static", "values": ["=Today()"]}
        assert date_from["valid_values"] is None
        assert date_from["layout"] == {"row": 0, "column": 0}
        assert date_to["default_values"] is None
        assert date_to["valid_values"] == {
            "source": "query",
            "dataset": "LookupSet",
            "value_field": "LookupKey",
            "label_field": None,
        }
        assert date_to["layout"] == {"row": 0, "column": 1}

    def test_static_valid_values_and_query_default(self, multi_path):
        path = str(multi_path)
        P.set_parameter_type(path, "DateTo", "String")
        P.set_parameter_available_values(
            path,
            "DateTo",
            "static",
            static_values=["EU", {"value": "US", "label": "United States"}],
        )
        P.set_parameter_default_values(
            path, "DateTo", "query", query_dataset="LookupSet", query_value_field="LookupKey"
        )
        P.set_parameter_layout(path, 2, 1, ["DateFrom", "DateTo"])
        date_to = get_parameters(path)[1]
        assert date_to["valid_values"] == {
            "source": "static",
            "values": [
                {"value": "EU", "label": "EU"},
                {"value": "US", "label": "United States"},
            ],
        }
        assert date_to["default_values"] == {
            "source": "query",
            "dataset": "LookupSet",
            "value_field": "LookupKey",
        }
        assert date_to["layout"] == {"row": 1, "column": 0}

    def test_query_valid_values_with_label_field(self, multi_path):
        path = str(multi_path)
        P.set_parameter_available_values(
            path,
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
            query_label_field="LookupKey",
        )
        assert get_parameters(path)[0]["valid_values"]["label_field"] == "LookupKey"

    def test_concise_view_keeps_layout_and_sources(self, multi_path):
        from pbirb_mcp.ops.response_format import with_response_format

        path = str(multi_path)
        P.set_parameter_default_values(path, "DateFrom", "static", ["=Today()"])
        P.set_parameter_layout(path, 1, 2, ["DateFrom", "DateTo"])
        reader = with_response_format("get_parameters", get_parameters)
        date_from, date_to = reader(path=path, response_format="concise")
        assert date_from == {
            "name": "DateFrom",
            "data_type": "DateTime",
            "prompt": "Date From",
            "multi_value": False,
            "hidden": False,
            "layout": {"row": 0, "column": 0},
            "default_values_source": "static",
            "valid_values_source": None,
        }
        assert date_to["default_values_source"] is None


# ---- get_tablixes ----------------------------------------------------------


class TestGetTablixes:
    def test_returns_tablix_with_layout(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        assert len(out) == 1
        t = out[0]
        assert t["name"] == "MainTable"
        assert t["dataset"] == "MainDataset"
        assert t["top"] == "0.5in"
        assert t["left"] == "0.5in"
        assert t["width"] == "4in"
        assert t["height"] == "0.5in"

    def test_lists_columns_with_widths(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        cols = out[0]["columns"]
        assert [c["width"] for c in cols] == ["1in", "2in", "1in"]

    def test_lists_row_groups_keyed_by_name(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        groups = out[0]["row_groups"]
        # Fixture has the conventional `Details` group plus a header member.
        assert "Details" in [g["name"] for g in groups if g["name"]]

    def test_filters_default_empty(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        assert out[0]["filters"] == []

    def test_visibility_default_none(self, rdl_path):
        out = get_tablixes(path=str(rdl_path))
        # No visibility expression in fixture.
        assert out[0]["visibility"] is None


# ---- get_tablixes structure (#69) ------------------------------------------

# Shaped like the review's "RAG Report": 6 merged band cells over 28
# columns (IVMSBand ColSpan=8), 22 placeholders in the band row.
RAG_BANDS = (("IVMSBand", 8), ("B2", 5), ("B3", 4), ("B4", 4), ("B5", 3), ("B6", 4))


def _matrix(path: Path) -> Path:
    from pbirb_mcp.ops.tablix import add_row_group, convert_to_matrix, set_tablix_corner
    from pbirb_mcp.ops.tablix_columns import add_column_group

    add_row_group(str(path), "MainTable", "RG", "=Fields!ProductName.Value")
    add_column_group(str(path), "MainTable", "CG", "=Fields!ProductID.Value")
    convert_to_matrix(str(path), "MainTable", row_group="RG", column_group="CG")
    set_tablix_corner(str(path), "MainTable", text="Corner")
    return path


class TestGetTablixesStructure:
    def test_spans_read_from_cell_contents_and_placeholders_covered(self, tmp_path):
        from tests._band_fixture import build_band_report

        path = build_band_report(tmp_path / "rag.rdl", RAG_BANDS)
        cells = get_tablixes(str(path))[0]["cells"]
        assert len([c for c in cells if c["col_span"] > 1]) == 6
        (ivms,) = [c for c in cells if c["textbox_name"] == "IVMSBand"]
        assert (ivms["row"], ivms["col"], ivms["col_span"]) == (0, 0, 8)
        covered = [c for c in cells if "covered_by" in c]
        assert len(covered) == 22
        assert all(c["textbox_name"] is None and c["row"] == 0 for c in covered)
        assert [c["covered_by"] for c in covered if c["col"] < 8] == [[0, 0]] * 7
        # B2 starts at column 8: column 9 is its first placeholder.
        assert next(c for c in covered if c["col"] == 9)["covered_by"] == [0, 8]
        assert "covered_by" not in ivms

    def test_legacy_span_location_and_row_span(self, rdl_path):
        from lxml import etree

        from pbirb_mcp.core.xpath import q

        tree = etree.parse(str(rdl_path))
        rows = list(tree.getroot().iter(q("TablixRow")))
        header_cells = list(rows[0].iter(q("TablixCell")))
        # Pre-2010 layout: ColSpan directly on the TablixCell.
        etree.SubElement(header_cells[0], q("ColSpan")).text = "2"
        # RowSpan on CellContents covering the data row below.
        contents = header_cells[2].find(q("CellContents"))
        etree.SubElement(contents, q("RowSpan")).text = "2"
        tree.write(str(rdl_path), xml_declaration=True, encoding="utf-8")
        cells = get_tablixes(str(rdl_path))[0]["cells"]
        by_pos = {(c["row"], c["col"]): c for c in cells}
        assert by_pos[(0, 0)]["col_span"] == 2
        assert by_pos[(0, 1)]["covered_by"] == [0, 0]
        assert by_pos[(0, 2)]["row_span"] == 2
        assert by_pos[(1, 2)]["covered_by"] == [0, 2]

    def test_header_and_corner_cells_listed(self, rdl_path):
        t = get_tablixes(str(_matrix(rdl_path)))[0]
        assert [c["textbox_name"] for c in t["corner_cells"]] == ["MainTable_Corner"]
        assert t["corner_cells"][0]["row"] == 0 and t["corner_cells"][0]["col"] == 0
        headers = {(h["axis"], h["group"]): h for h in t["header_cells"]}
        assert headers[("column", "CG")]["textbox_name"] == "CG_Header"
        assert headers[("column", "CG")]["level"] == 0
        assert headers[("row", "RG")]["textbox_name"]
        assert headers[("row", "RG")]["size"]
        names = {c["textbox_name"] for c in t["cells"]}
        assert "MainTable_Corner" not in names  # corner/header cells aren't body cells

    def test_descending_sort_under_its_member(self, rdl_path):
        text = rdl_path.read_text(encoding="utf-8")
        rdl_path.write_text(
            text.replace(
                '<Group Name="Details" />',
                '<Group Name="Details" /><SortExpressions><SortExpression>'
                "<Value>=Fields!Amount.Value</Value><Direction>Descending</Direction>"
                "</SortExpression></SortExpressions>",
            ),
            encoding="utf-8",
        )
        t = get_tablixes(str(rdl_path))[0]
        # sort_expressions is the tablix-level sort only.
        assert t["sort_expressions"] == [] and t["sort"] == []
        details = next(m for m in t["row_members"] if m["group"] == "Details")
        assert details["kind"] == "details"
        assert details["sort"] == [{"value": "=Fields!Amount.Value", "direction": "Descending"}]
        # The old flat keys are kept.
        assert t["row_groups"] == [{"name": "Details", "expressions": []}]

    def test_tablix_level_sort(self, rdl_path):
        from lxml import etree

        from pbirb_mcp.core.xpath import q

        tree = etree.parse(str(rdl_path))
        tablix = next(tree.getroot().iter(q("Tablix")))
        block = etree.Element(q("SortExpressions"))
        sort = etree.SubElement(block, q("SortExpression"))
        etree.SubElement(sort, q("Value")).text = "=Fields!ProductName.Value"
        tablix.find(q("DataSetName")).addnext(block)
        tree.write(str(rdl_path), xml_declaration=True, encoding="utf-8")
        t = get_tablixes(str(rdl_path))[0]
        assert t["sort_expressions"] == ["=Fields!ProductName.Value"]
        assert t["sort"] == [{"value": "=Fields!ProductName.Value", "direction": "Ascending"}]

    def test_member_tree_and_leaves(self, rdl_path):
        from pbirb_mcp.ops.tablix import add_row_group

        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        t = get_tablixes(str(rdl_path))[0]
        (region,) = [m for m in t["row_members"] if m["group"] == "Region"]
        assert region["kind"] == "group"
        assert region["expressions"] == ["=Fields!ProductName.Value"]
        kinds = [c["kind"] for c in region["children"]]
        assert kinds[-1] == "details" and "static" in kinds
        assert len(t["row_leaves"]) == len(t["rows"])
        assert [leaf["index"] for leaf in t["row_leaves"]] == list(range(len(t["rows"])))
        details_leaf = next(leaf for leaf in t["row_leaves"] if leaf["kind"] == "details")
        assert details_leaf["groups"] == ["Region", "Details"]
        assert len(t["column_leaves"]) == len(t["columns"])
        assert all(m["kind"] == "static" for m in t["column_members"])
        assert [r["height"] for r in t["rows"]] and all(r["height"] for r in t["rows"])

    def test_row_leaf_roles_resolve_via_style_tablix_row(self, rdl_path):
        from pbirb_mcp.ops.styling import style_tablix_row
        from pbirb_mcp.ops.tablix import add_row_group
        from pbirb_mcp.ops.tablix_subtotals import add_subtotal_row

        add_row_group(str(rdl_path), "MainTable", "Region", "=Fields!ProductName.Value")
        add_subtotal_row(
            str(rdl_path),
            "MainTable",
            "Region",
            [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
        )
        leaves = get_tablixes(str(rdl_path))[0]["row_leaves"]
        roles = {role: leaf["index"] for leaf in leaves for role in leaf["roles"]}
        assert {"header", "details", "Region_header", "Region_footer"} <= set(roles)
        for leaf in leaves:
            assert leaf["role"] == (leaf["roles"][0] if leaf["roles"] else None)
        for role, index in roles.items():
            out = style_tablix_row(str(rdl_path), "MainTable", role, font_weight="Bold")
            assert out["row_index"] == index, role

    def test_concise_view_counts(self, rdl_path):
        from pbirb_mcp.ops.response_format import with_response_format

        reader = with_response_format("get_tablixes", get_tablixes)
        (t,) = reader(path=str(_matrix(rdl_path)), response_format="concise")
        full = get_tablixes(str(rdl_path))[0]
        assert t["row_count"] == len(full["rows"])
        assert t["header_cell_count"] == len(full["header_cells"]) == 2
        assert t["corner_cell_count"] == 1
        assert "row_members" not in t and "cells" not in t


# ---- registration / JSON-RPC integration ----------------------------------


class TestToolRegistration:
    def test_all_four_readers_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {"describe_report", "get_datasets", "get_parameters", "get_tablixes"} <= names

    def test_describe_report_callable_via_jsonrpc(self, rdl_path):
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "describe_report",
                    "arguments": {"path": str(rdl_path)},
                },
            }
        )
        # Result is wrapped as MCP content; the JSON body is in content[0].text.
        text = resp["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["datasets"] == ["MainDataset"]

    def test_input_schema_requires_path(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        describe = next(t for t in listing if t["name"] == "describe_report")
        assert describe["inputSchema"]["required"] == ["path"]
        assert describe["inputSchema"]["properties"]["path"]["type"] == "string"
