"""Repeat-call idempotency for every mutating tool (#61).

Every tool registered ``mutating=True`` is called twice with identical
arguments. The first call may change the report; the second must either

* ``"noop"`` — succeed, report ``changed`` falsy (``False`` / ``[]`` /
  ``{}``), and leave the file alone: same bytes, same inode, same mtime; or
* ``"refuses"`` — raise (a creator refusing a duplicate name, a remover
  whose target is gone, …) and leave the file alone just the same.

The table below is keyed by tool name and built against the registry:
:func:`test_every_mutating_tool_is_covered` fails when a new mutating tool
lands without an entry (a case, or an exclusion with its reason).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = "pbi_paginated_minimal.rdl"
CHART = "pbi_chart_rich.rdl"
MULTI_DS = "pbi_multi_datasource.rdl"

# A 1x1 transparent PNG.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63faffff3f000005fe02fea3796300000000049454e4ae"
    "426082"
)
PNG = "{png}"  # placeholder replaced with a real PNG path per test


class _Registry:
    """Collects register_all_tools' entries without a server."""

    def __init__(self) -> None:
        self.tools: dict[str, tuple[dict, Callable[..., Any], bool]] = {}

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[..., Any],
        *,
        batchable: Optional[bool] = None,
        mutating: bool = False,
    ) -> None:
        self.tools[name] = (input_schema, handler, mutating)


_REG = _Registry()
register_all_tools(_REG)
MUTATING = sorted(n for n, (_s, _h, m) in _REG.tools.items() if m)


@dataclass
class Case:
    args: dict
    second: str = "noop"  # or "refuses"
    fixture: str = MINIMAL
    setup: list = field(default_factory=list)  # [(tool, args)] run first


# ---- setup steps reused by several cases ------------------------------------

ROW_GROUP = (
    "add_row_group",
    {
        "tablix_name": "MainTable",
        "group_name": "ProductGroup",
        "group_expression": "=Fields!ProductName.Value",
    },
)
COL_GROUP = (
    "add_column_group",
    {
        "tablix_name": "MainTable",
        "group_name": "ColGroup",
        "group_expression": "=Fields!ProductID.Value",
    },
)
MATRIX = (
    "convert_to_matrix",
    {"tablix_name": "MainTable", "row_group": "ProductGroup", "column_group": "ColGroup"},
)
BODY_IMAGE = (
    "add_body_image",
    {
        "name": "Logo",
        "image_source": "External",
        "value": "http://example.com/logo.png",
        "top": "3in",
        "left": "0in",
        "width": "1in",
        "height": "1in",
    },
)
EMBEDDED = ("add_embedded_image", {"name": "LogoData", "mime_type": "image/png", "image_path": PNG})
HEADER = ("set_page_header", {"height": "1in"})
FOOTER = ("set_page_footer", {"height": "1in"})
HEADER_BOX = (
    "add_header_textbox",
    {
        "name": "HdrBox",
        "text": "Title",
        "top": "0in",
        "left": "0in",
        "width": "2in",
        "height": "0.25in",
    },
)
FOOTER_BOX = (
    "add_footer_textbox",
    {
        "name": "FtrBox",
        "text": "Footer",
        "top": "0in",
        "left": "0in",
        "width": "2in",
        "height": "0.25in",
    },
)
BODY_BOX = (
    "add_body_textbox",
    {
        "name": "Note",
        "text": "Note",
        "top": "3in",
        "left": "0in",
        "width": "2in",
        "height": "0.25in",
    },
)
# A rectangle holding the BODY_BOX textbox (#83).
PANEL = (
    "add_rectangle",
    {
        "name": "Panel",
        "top": "3in",
        "left": "0in",
        "width": "2.5in",
        "height": "0.5in",
        "contained_items": ["Note"],
    },
)
TB = "HeaderAmount"
CH = "SalesByProduct"

CASES: dict[str, Case] = {
    # ---- datasets / data sources ----
    "update_dataset_query": Case(
        {"dataset_name": "MainDataset", "dax_body": "EVALUATE TOPN(10, 'Sales')"}
    ),
    "add_query_parameter": Case(
        {
            "dataset_name": "MainDataset",
            "name": "DateFrom",
            "value_expression": "=Parameters!DateFrom.Value",
        },
        second="refuses",
    ),
    "update_query_parameter": Case(
        {
            "dataset_name": "MainDataset",
            "name": "DateFrom",
            "value_expression": "=Parameters!DateTo.Value",
        },
        setup=[
            (
                "add_query_parameter",
                {
                    "dataset_name": "MainDataset",
                    "name": "DateFrom",
                    "value_expression": "=Parameters!DateFrom.Value",
                },
            )
        ],
    ),
    "remove_query_parameter": Case(
        {"dataset_name": "MainDataset", "name": "DateFrom"},
        second="refuses",
        setup=[
            (
                "add_query_parameter",
                {
                    "dataset_name": "MainDataset",
                    "name": "DateFrom",
                    "value_expression": "=Parameters!DateFrom.Value",
                },
            )
        ],
    ),
    "set_datasource_connection": Case(
        {
            "name": "PowerBIDataset",
            "workspace_url": "powerbi://api.powerbi.com/v1.0/myorg/Other",
            "dataset_name": "OtherDS",
        }
    ),
    "add_data_source": Case(
        {
            "name": "Second",
            "workspace_url": "powerbi://api.powerbi.com/v1.0/myorg/W2",
            "dataset_name": "D2",
        },
        second="refuses",
    ),
    "remove_data_source": Case(
        {"name": "LookupDataset", "force": True}, second="refuses", fixture=MULTI_DS
    ),
    "rename_data_source": Case(
        {"old_name": "PowerBIDataset", "new_name": "MainSource"}, second="refuses"
    ),
    "remove_dataset_filter": Case(
        {"dataset_name": "MainDataset", "filter_index": 0},
        second="refuses",
        setup=[
            (
                "add_dataset_filter",
                {
                    "dataset_name": "MainDataset",
                    "expression": "=Fields!Amount.Value",
                    "operator": "GreaterThan",
                    "values": ["0"],
                },
            )
        ],
    ),
    "add_calculated_field": Case(
        {
            "dataset_name": "MainDataset",
            "field_name": "Double",
            "expression": "=Fields!Amount.Value * 2",
        },
        second="refuses",
    ),
    "remove_calculated_field": Case(
        {"dataset_name": "MainDataset", "field_name": "Double"},
        second="refuses",
        setup=[
            (
                "add_calculated_field",
                {
                    "dataset_name": "MainDataset",
                    "field_name": "Double",
                    "expression": "=Fields!Amount.Value * 2",
                },
            )
        ],
    ),
    "add_dataset_field": Case(
        {"dataset_name": "MainDataset", "field_name": "Qty", "data_field": "Sales[Qty]"},
        second="refuses",
    ),
    "remove_dataset_field": Case(
        {"dataset_name": "MainDataset", "field_name": "Qty"},
        second="refuses",
        setup=[
            (
                "add_dataset_field",
                {"dataset_name": "MainDataset", "field_name": "Qty", "data_field": "Sales[Qty]"},
            )
        ],
    ),
    "refresh_dataset_fields": Case(
        {"dataset_name": "MainDataset"},
        setup=[
            (
                "update_dataset_query",
                {
                    "dataset_name": "MainDataset",
                    "dax_body": "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], \"Total\", SUM('Sales'[Amount]))",
                },
            )
        ],
    ),
    # ---- images ----
    "set_image_sizing": Case({"image_name": "Logo", "sizing": "Fit"}, setup=[BODY_IMAGE]),
    "set_image_source": Case(
        {"image_name": "Logo", "embedded_name": "LogoData"}, setup=[BODY_IMAGE, EMBEDDED]
    ),
    "add_embedded_image": Case(
        {"name": "LogoData", "mime_type": "image/png", "image_path": PNG}, second="refuses"
    ),
    "remove_embedded_image": Case({"name": "LogoData"}, second="refuses", setup=[EMBEDDED]),
    # ---- tablix ----
    "set_column_width": Case({"tablix_name": "MainTable", "column": 0, "width": "1.5in"}),
    "set_tablix_size": Case({"name": "MainTable", "width": "4.5in", "distribute": True}),
    "set_row_height": Case({"tablix_name": "MainTable", "row_index": 0, "height": "0.4in"}),
    "remove_tablix_filter": Case(
        {"tablix_name": "MainTable", "filter_index": 0},
        second="refuses",
        setup=[
            (
                "add_tablix_filter",
                {
                    "tablix_name": "MainTable",
                    "expression": "=Fields!Amount.Value",
                    "operator": "GreaterThan",
                    "values": ["0"],
                },
            )
        ],
    ),
    "add_row_group": Case(ROW_GROUP[1], second="refuses"),
    "remove_row_group": Case(
        {"tablix_name": "MainTable", "group_name": "ProductGroup"},
        second="refuses",
        setup=[ROW_GROUP],
    ),
    "convert_to_matrix": Case(MATRIX[1], second="refuses", setup=[ROW_GROUP, COL_GROUP]),
    "set_tablix_corner": Case(
        {"tablix_name": "MainTable", "text": "Corner"}, setup=[ROW_GROUP, COL_GROUP, MATRIX]
    ),
    "set_group_sort": Case(
        {
            "tablix_name": "MainTable",
            "group_name": "ProductGroup",
            "sort_expressions": ["=Fields!ProductName.Value"],
        },
        setup=[ROW_GROUP],
    ),
    "set_group_visibility": Case(
        {
            "tablix_name": "MainTable",
            "group_name": "ProductGroup",
            "visibility_expression": "=False",
        },
        setup=[ROW_GROUP],
    ),
    "set_detail_row_visibility": Case({"tablix_name": "MainTable", "expression": "=True"}),
    "add_column_group": Case(COL_GROUP[1], second="refuses"),
    "remove_column_group": Case(
        {"tablix_name": "MainTable", "group_name": "ColGroup"}, second="refuses", setup=[COL_GROUP]
    ),
    "set_column_group_sort": Case(
        {
            "tablix_name": "MainTable",
            "group_name": "ColGroup",
            "sort_expressions": ["=Fields!ProductID.Value"],
        },
        setup=[COL_GROUP],
    ),
    "set_column_group_visibility": Case(
        {"tablix_name": "MainTable", "group_name": "ColGroup", "visibility_expression": "=False"},
        setup=[COL_GROUP],
    ),
    "add_tablix_column": Case(
        {"tablix_name": "MainTable", "column_name": "Qty", "expression": "=Fields!ProductID.Value"},
        second="refuses",
    ),
    "remove_tablix_column": Case(
        {"tablix_name": "MainTable", "column_name": "Amount"}, second="refuses"
    ),
    "set_cell_span": Case(
        {
            "tablix_name": "MainTable",
            "row_index": 0,
            "column_name": "HeaderProductID",
            "col_span": 2,
            "force": True,
        }
    ),
    "add_static_row": Case({"tablix_name": "MainTable", "row_name": "Totals"}, second="refuses"),
    "add_static_column": Case(
        {"tablix_name": "MainTable", "column_name": "Extra"}, second="refuses"
    ),
    "insert_tablix_from_template": Case(
        {
            "name": "Second",
            "dataset_name": "MainDataset",
            "columns": ["ProductName", "Amount"],
            "top": "3in",
            "left": "0in",
            "width": "3in",
            "height": "0.5in",
        },
        second="refuses",
    ),
    # ---- layout / grouping ----
    "set_group_page_break": Case(
        {"tablix_name": "MainTable", "group_name": "ProductGroup", "location": "Between"},
        setup=[ROW_GROUP],
    ),
    "set_repeat_on_new_page": Case(
        {"tablix_name": "MainTable", "group_name": "ProductGroup", "repeat": True},
        setup=[ROW_GROUP],
    ),
    "set_keep_together": Case({"name": "MainTable", "keep": True}),
    # KeepWithGroup targets static rows (#67); the fixture's header has After.
    "set_keep_with_group": Case(
        {"tablix_name": "MainTable", "member": "header", "value": "Before"}
    ),
    "set_repeat_header_rows": Case({"tablix_name": "MainTable"}),
    "add_rectangle": Case(
        {"name": "Box", "top": "3in", "left": "0in", "width": "1in", "height": "1in"},
        second="refuses",
    ),
    "add_list": Case(
        {
            "name": "Repeater",
            "dataset_name": "MainDataset",
            "top": "3in",
            "left": "0in",
            "width": "2in",
            "height": "1in",
        },
        second="refuses",
    ),
    "add_line": Case(
        {"name": "Rule", "top": "3in", "left": "0in", "width": "2in", "height": "0in"},
        second="refuses",
    ),
    "move_report_item": Case({"name": "Note", "to": "header"}, setup=[BODY_BOX]),
    "ungroup_rectangle": Case({"name": "Panel"}, second="refuses", setup=[BODY_BOX, PANEL]),
    # #111: the repeat finds old_name gone and new_name present — already renamed.
    "rename_report_item": Case(
        {"old_name": "Note", "new_name": "Remark"},
        setup=[
            BODY_BOX,
            (
                "add_body_textbox",
                {**BODY_BOX[1], "name": "Echo", "text": "=ReportItems!Note.Value"},
            ),
        ],
    ),
    "raw_xml_replace": Case(
        {
            "xpath": "r:ReportSections/r:ReportSection/r:Body/r:Height",
            "content": "<Height>3in</Height>",
        }
    ),
    # ---- actions ----
    "set_textbox_action": Case(
        {"textbox_name": TB, "action_type": "Hyperlink", "target_expression": "http://example.com"}
    ),
    "set_image_action": Case(
        {
            "image_name": "Logo",
            "action_type": "Hyperlink",
            "target_expression": "http://example.com",
        },
        setup=[BODY_IMAGE],
    ),
    "set_textbox_tooltip": Case({"textbox_name": TB, "text_or_expression": "Total amount"}),
    "set_item_action": Case(
        {
            "name": "Logo",
            "action_type": "Drillthrough",
            "target_expression": "Detail",
            "drillthrough_parameters": [{"name": "Id", "value": "1", "omit": True}],
        },
        setup=[BODY_IMAGE],
    ),
    "set_item_tooltip": Case({"name": "MainTable", "text_or_expression": "Sales"}),
    "set_bookmark": Case({"name": "MainTable", "bookmark_or_expression": "Top"}),
    "set_document_map_label": Case({"element_name": TB, "label_or_expression": "Amount"}),
    "set_chart_series_action": Case(
        {
            "chart_name": CH,
            "series_name": "Amount",
            "action_type": "Hyperlink",
            "target_expression": "http://example.com",
        },
        fixture=CHART,
    ),
    # ---- positioning ----
    "set_body_item_position": Case({"name": "MainTable", "top": "0.75in", "left": "0.25in"}),
    "set_body_item_size": Case({"name": "MainTable", "width": "4.5in"}),
    "set_header_item_position": Case(
        {"name": "HdrBox", "top": "0.1in", "left": "0.1in"}, setup=[HEADER, HEADER_BOX]
    ),
    "set_footer_item_position": Case(
        {"name": "FtrBox", "top": "0.1in", "left": "0.1in"}, setup=[FOOTER, FOOTER_BOX]
    ),
    "set_header_item_size": Case({"name": "HdrBox", "width": "3in"}, setup=[HEADER, HEADER_BOX]),
    "set_footer_item_size": Case({"name": "FtrBox", "width": "3in"}, setup=[FOOTER, FOOTER_BOX]),
    "center_item": Case({"name": "MainTable", "axis": "vertical"}),
    # ---- parameters ----
    "reorder_parameters": Case({"names": ["DateTo", "DateFrom"]}),
    "set_parameter_layout": Case(
        {"rows": 1, "columns": 2, "parameter_order": ["DateTo", "DateFrom"]}
    ),
    "sync_parameter_layout": Case(
        {},
        setup=[
            (
                "set_parameter_layout",
                {"rows": 1, "columns": 2, "parameter_order": ["DateFrom", "DateTo"]},
            ),
            # A third parameter the layout doesn't know about yet.
            (
                "raw_xml_replace",
                {
                    "xpath": "r:ReportParameters",
                    "content": (
                        '<ReportParameters><ReportParameter Name="DateFrom"><DataType>DateTime</DataType>'
                        '<Prompt>Date From</Prompt></ReportParameter><ReportParameter Name="DateTo">'
                        "<DataType>DateTime</DataType><Prompt>Date To</Prompt></ReportParameter>"
                        '<ReportParameter Name="Region"><DataType>String</DataType><Prompt>Region</Prompt>'
                        "</ReportParameter></ReportParameters>"
                    ),
                },
            ),
        ],
    ),
    "set_parameter_prompt": Case({"name": "DateFrom", "prompt": "Start date"}),
    "set_parameter_type": Case({"name": "DateFrom", "type": "String"}),
    "add_parameter": Case({"name": "Region", "type": "String"}, second="refuses"),
    "remove_parameter": Case({"name": "DateTo"}, second="refuses"),
    "rename_parameter": Case({"old_name": "DateTo", "new_name": "DateUntil"}, second="refuses"),
    "set_parameter_available_values": Case(
        {"name": "DateFrom", "source": "static", "static_values": ["2024-01-01", "2025-01-01"]},
        setup=[("set_parameter_type", {"name": "DateFrom", "type": "String"})],
    ),
    "set_parameter_default_values": Case(
        {"name": "DateFrom", "source": "static", "static_values": ["2024-01-01"]},
        setup=[("set_parameter_type", {"name": "DateFrom", "type": "String"})],
    ),
    "update_parameter_advanced": Case({"name": "DateFrom", "allow_null": True}),
    # ---- page / body / header / footer ----
    "set_page_setup": Case({"page_width": "8in", "margin_top": "0.5in", "columns": 1}),
    "set_page_orientation": Case({"orientation": "Landscape"}),
    "set_body_size": Case({"height": "3in"}),
    "fit_page_to_body": Case({"mode": "page"}, setup=[("set_body_size", {"width": "7in"})]),
    "set_page_header": Case({"height": "1in", "print_on_first_page": True}),
    "set_page_footer": Case({"height": "1in", "print_on_last_page": True}),
    "add_header_textbox": Case(HEADER_BOX[1], second="refuses", setup=[HEADER]),
    "add_footer_textbox": Case(FOOTER_BOX[1], second="refuses", setup=[FOOTER]),
    "add_header_image": Case(
        {
            "name": "HdrLogo",
            "image_source": "External",
            "value": "http://example.com/logo.png",
            "top": "0in",
            "left": "0in",
            "width": "0.5in",
            "height": "0.5in",
        },
        second="refuses",
        setup=[HEADER],
    ),
    "add_footer_image": Case(
        {
            "name": "FtrLogo",
            "image_source": "External",
            "value": "http://example.com/logo.png",
            "top": "0in",
            "left": "0in",
            "width": "0.5in",
            "height": "0.5in",
        },
        second="refuses",
        setup=[FOOTER],
    ),
    "remove_header_item": Case({"name": "HdrBox"}, second="refuses", setup=[HEADER, HEADER_BOX]),
    "remove_footer_item": Case({"name": "FtrBox"}, second="refuses", setup=[FOOTER, FOOTER_BOX]),
    # ---- body items ----
    "add_body_textbox": Case(BODY_BOX[1], second="refuses"),
    "add_body_image": Case(BODY_IMAGE[1], second="refuses"),
    "remove_body_item": Case({"name": "Note"}, second="refuses", setup=[BODY_BOX]),
    # ---- styling ----
    "set_textbox_style": Case(
        {
            "textbox_name": TB,
            "font_weight": "Bold",
            "font_size": "12pt",
            "color": "#333333",
            "background_color": "#EEEEEE",
            "border_style": "Solid",
            "text_align": "Right",
            "padding_left": "4pt",
            "can_grow": False,
        }
    ),
    "set_textbox_runs": Case(
        {
            "textbox_name": TB,
            "runs": [{"text": "Total "}, {"text": "Amount", "font_weight": "Bold"}],
        }
    ),
    "set_textbox_value": Case({"textbox_name": TB, "value": "Total"}),
    "set_textbox_style_bulk": Case(
        {
            "textbox_names": ["HeaderProductID", "HeaderAmount"],
            "font_weight": "Bold",
            "color": "Blue",
        }
    ),
    "style_tablix_row": Case(
        {"tablix_name": "MainTable", "row": 0, "font_weight": "Bold", "background_color": "#DDDDDD"}
    ),
    "set_alternating_row_color": Case(
        {"tablix_name": "MainTable", "color_a": "White", "color_b": "#F0F0F0"}
    ),
    "set_conditional_row_color": Case(
        {
            "tablix_name": "MainTable",
            "value_expression": "=Fields!ProductName.Value",
            "color_map": {"A": "Red", "B": "Green"},
        }
    ),
    "set_element_visibility": Case({"element_name": TB, "hidden_expression": "=False"}),
    # ---- charts ----
    "insert_chart_from_template": Case(
        {
            "name": "Chart2",
            "dataset_name": "MainDataset",
            "category_field": "ProductName",
            "value_field": "Amount",
            "top": "3in",
            "left": "0in",
            "width": "4in",
            "height": "3in",
        },
        second="refuses",
    ),
    "add_chart_series": Case(
        {"chart_name": CH, "series_name": "Extra", "value_field": "Amount"},
        second="refuses",
        fixture=CHART,
    ),
    "remove_chart_series": Case(
        {"chart_name": CH, "series_name": "Total"}, second="refuses", fixture=CHART
    ),
    "set_chart_series_type": Case(
        {"chart_name": CH, "series_name": "Amount", "series_type": "Line"}, fixture=CHART
    ),
    "set_chart_series_grouping": Case(
        {"chart_name": CH, "series_name": "Amount", "group_field": "ProductID", "replace": True},
        fixture=CHART,
    ),
    "set_chart_axis": Case(
        {
            "chart_name": CH,
            "axis": "Value",
            "title": "Sales",
            "format": "#,0",
            "min": "0",
            "visible": True,
        },
        fixture=CHART,
    ),
    "set_chart_legend": Case(
        {"chart_name": CH, "position": "BottomCenter", "visible": True}, fixture=CHART
    ),
    "set_chart_data_labels": Case(
        {"chart_name": CH, "visible": True, "format": "#,0", "position": "Top"}, fixture=CHART
    ),
    "set_chart_palette": Case({"chart_name": CH, "palette": "Pastel"}, fixture=CHART),
    "set_series_color": Case(
        {"chart_name": CH, "series_name": "Amount", "color": "#FF0000"}, fixture=CHART
    ),
    "set_chart_title": Case({"chart_name": CH, "text": "Revenue"}, fixture=CHART),
    "set_chart_series_properties": Case(
        {
            "chart_name": CH,
            "series_name": "Quantity",
            "legend_text": "Units",
            "hide_in_legend": False,
            "value_axis": "Secondary",
            "hidden": False,
            "position": 0,
            "tooltip": "=Fields!Amount.Value",
            "marker_type": "Circle",
            "marker_size": "6pt",
            "empty_point_value": "Zero",
            "empty_point_color": "Gray",
        },
        fixture=CHART,
    ),
    "set_chart_category_group": Case(
        {
            "chart_name": CH,
            "label_expression": "=UCase(Fields!ProductName.Value)",
            "sort": "Descending",
        },
        fixture=CHART,
    ),
}

# Further argument sets for branches the main case doesn't reach
# (clearing, alternative sources, …). Same contract.
VARIANTS: list[tuple[str, Case]] = [
    # Row selectors for the static header row (#67).
    (
        "set_repeat_on_new_page",
        Case({"tablix_name": "MainTable", "member": "header", "repeat": True}),
    ),
    ("set_repeat_on_new_page", Case({"tablix_name": "MainTable", "member": 0, "repeat": True})),
    ("set_repeat_header_rows", Case({"tablix_name": "MainTable", "fixed": True})),
    (
        "insert_tablix_from_template",
        Case(
            {
                "name": "Second",
                "dataset_name": "MainDataset",
                "columns": ["ProductName"],
                "top": "3in",
                "left": "0in",
                "width": "1in",
                "height": "0.5in",
                "repeat_header": False,
            },
            second="refuses",
        ),
    ),
    (
        "set_repeat_header_rows",
        Case(
            {"tablix_name": "MainTable", "repeat": False, "fixed": False},
            setup=[("set_repeat_header_rows", {"tablix_name": "MainTable", "fixed": True})],
        ),
    ),
    # add_row_group's wrap / parent_group / header (#65): a repeat refuses
    # the duplicate group name before touching the file.
    ("add_row_group", Case({**ROW_GROUP[1], "header": "column"}, second="refuses")),
    ("add_row_group", Case({**ROW_GROUP[1], "wrap": "all", "header": "none"}, second="refuses")),
    (
        "add_row_group",
        Case(
            {
                "tablix_name": "MainTable",
                "group_name": "SubGroup",
                "group_expression": "=Fields!ProductID.Value",
                "parent_group": "ProductGroup",
            },
            second="refuses",
            setup=[ROW_GROUP],
        ),
    ),
    (
        "set_datasource_connection",
        Case(
            {
                "name": "PowerBIDataset",
                "workspace_url": "Sales WS",
                "dataset_name": "Sales; Model",
                "provider": "pbidataset",
                "catalog_id": "sobe_wowvirtualserver-1",
                "identity_provider": "https://login.microsoftonline.com/organizations, x",
            }
        ),
    ),
    (
        # A PBIDATASET source repointed without catalog_id: the second call
        # keeps the (display-name) catalog and changes nothing.
        "set_datasource_connection",
        Case(
            {"name": "Pbi", "workspace_url": "Other WS", "dataset_name": "Model 2"},
            setup=[
                (
                    "add_data_source",
                    {
                        "name": "Pbi",
                        "workspace_url": "WS",
                        "dataset_name": "Model",
                        "provider": "pbidataset",
                    },
                )
            ],
        ),
    ),
    (
        "add_data_source",
        Case(
            {
                "name": "Pbi",
                "workspace_url": "powerbi://api.powerbi.com/v1.0/myorg/WS",
                "dataset_name": "Model",
                "provider": "pbidataset",
                "catalog_id": "sobe-1",
                "identity_provider": "idp",
            },
            second="refuses",
        ),
    ),
    (
        "set_group_sort",
        Case(
            {
                "tablix_name": "MainTable",
                "group_name": "ProductGroup",
                "sort_expressions": [
                    {"value": "=Fields!ProductName.Value", "direction": "Descending"},
                    "=Fields!ProductID.Value",
                ],
            },
            setup=[ROW_GROUP],
        ),
    ),
    (
        "set_group_sort",
        Case(
            {"tablix_name": "MainTable", "group_name": "ProductGroup", "sort_expressions": []},
            setup=[
                ROW_GROUP,
                (
                    "set_group_sort",
                    {
                        "tablix_name": "MainTable",
                        "group_name": "ProductGroup",
                        "sort_expressions": ["=Fields!ProductName.Value"],
                    },
                ),
            ],
        ),
    ),
    (
        "add_tablix_column",
        Case(
            {
                "tablix_name": "MainTable",
                "column_name": "Qty",
                "expression": "=Fields!ProductID.Value",
                "header_name": "QtyHeader",
                "copy_style_from": "right",
                "format": "#,0",
            },
            second="refuses",
        ),
    ),
    # ---- #83: nested items, container argument, moves ----
    (
        "set_body_item_position",
        Case({"name": "Note", "top": "0.1in", "left": "0.1in"}, setup=[BODY_BOX, PANEL]),
    ),
    ("set_body_item_size", Case({"name": "Note", "width": "1in"}, setup=[BODY_BOX, PANEL])),
    ("remove_body_item", Case({"name": "Note"}, second="refuses", setup=[BODY_BOX, PANEL])),
    # ---- #105: removing a referenced item needs force ----
    (
        "remove_body_item",
        Case(
            {"name": "Note", "force": True},
            second="refuses",
            setup=[
                BODY_BOX,
                (
                    "add_body_textbox",
                    {**BODY_BOX[1], "name": "Echo", "text": "=ReportItems!Note.Value"},
                ),
            ],
        ),
    ),
    (
        "remove_header_item",
        Case({"name": "HdrBox", "force": True}, second="refuses", setup=[HEADER, HEADER_BOX]),
    ),
    (
        "add_body_textbox",
        Case(
            {**BODY_BOX[1], "name": "Inner", "top": "0in", "container": "Panel"},
            second="refuses",
            setup=[BODY_BOX, PANEL],
        ),
    ),
    (
        "add_line",
        Case(
            {
                "name": "Rule",
                "top": "0in",
                "left": "0in",
                "width": "1in",
                "height": "0in",
                "container": "Panel",
            },
            second="refuses",
            setup=[BODY_BOX, PANEL],
        ),
    ),
    ("move_report_item", Case({"name": "Note", "to": "body"}, setup=[BODY_BOX, PANEL])),
    (
        "move_report_item",
        Case(
            {"name": "Note", "to": "Panel", "top": "0in", "left": "0.5in"}, setup=[BODY_BOX, PANEL]
        ),
    ),
    ("set_page_setup", Case({"columns": 2, "margin_left": "0.5in"})),
    ("set_page_setup", Case({"columns": 1}, setup=[("set_page_setup", {"columns": 2})])),
    (
        "set_textbox_style",
        Case(
            {"textbox_name": TB, "border_color": "Red", "border_width": "1pt", "can_shrink": True}
        ),
    ),
    (
        "set_textbox_style",
        Case({"textbox_name": TB, "font_style": "Italic", "text_decoration": "underline"}),
    ),
    (
        "set_textbox_style_bulk",
        Case({"textbox_names": ["HeaderProductID"], "font_weight": "700", "font_style": "Italic"}),
    ),
    ("style_tablix_row", Case({"tablix_name": "MainTable", "row": 0, "text_decoration": "None"})),
    (
        "set_image_source",
        Case(
            {
                "image_name": "Logo",
                "source": "Database",
                "value": "=First(Fields!ProductName.Value)",
                "mime_type": "image/png",
            },
            setup=[BODY_IMAGE],
        ),
    ),
    (
        "set_image_source",
        Case(
            {"image_name": "Logo", "source": "External", "value": "http://example.com/b.png"},
            setup=[BODY_IMAGE],
        ),
    ),
    (
        "add_body_image",
        Case(
            {
                **BODY_IMAGE[1],
                "image_source": "Database",
                "value": "=First(Fields!ProductName.Value)",
                "mime_type": "image/jpeg",
            },
            second="refuses",
        ),
    ),
    ("set_parameter_prompt", Case({"name": "DateFrom", "prompt": ""})),
    (
        "reorder_parameters",
        Case(
            {"names": ["DateTo", "DateFrom"], "reflow_layout": True},
            setup=[
                (
                    "set_parameter_layout",
                    {"rows": 1, "columns": 2, "parameter_order": ["DateFrom", "DateTo"]},
                )
            ],
        ),
    ),
    (
        "remove_parameter",
        Case(
            {"name": "DateFrom", "compact_layout": True},
            second="refuses",
            setup=[
                (
                    "set_parameter_layout",
                    {"rows": 1, "columns": 2, "parameter_order": ["DateFrom", "DateTo"]},
                )
            ],
        ),
    ),
    (
        "set_parameter_available_values",
        Case(
            {
                "name": "DateFrom",
                "source": "query",
                "query_dataset": "MainDataset",
                "query_value_field": "ProductName",
            }
        ),
    ),
    (
        "set_parameter_default_values",
        Case(
            {"name": "DateFrom", "source": "static", "static_values": []},
            setup=[
                ("set_parameter_type", {"name": "DateFrom", "type": "String"}),
                (
                    "set_parameter_default_values",
                    {"name": "DateFrom", "source": "static", "static_values": ["x"]},
                ),
            ],
        ),
    ),
    ("set_chart_axis", Case({"chart_name": CH, "axis": "Category", "title": ""}, fixture=CHART)),
    ("set_chart_axis", Case({"chart_name": CH, "axis": "Value", "log_scale": True}, fixture=CHART)),
    (
        "set_chart_palette",
        Case(
            {
                "chart_name": CH,
                "palette": "Custom",
                "colors": ["#112233", "Red"],
                "hatch_behavior": "Always",
            },
            fixture=CHART,
        ),
    ),
    ("set_chart_palette", Case({"chart_name": CH, "palette": ""}, fixture=CHART)),
    (
        "set_series_color",
        Case(
            {"chart_name": CH, "series_name": "Amount", "color": "Green", "target": "point"},
            fixture=CHART,
        ),
    ),
    (
        "set_chart_series_action",
        Case(
            {"chart_name": CH, "series_name": "Amount", "action_type": "None"},
            fixture=CHART,
            setup=[
                (
                    "set_chart_series_action",
                    {
                        "chart_name": CH,
                        "series_name": "Amount",
                        "action_type": "Hyperlink",
                        "target_expression": "http://example.com",
                    },
                )
            ],
        ),
    ),
    (
        "insert_chart_from_template",
        Case(
            {
                "name": "Chart3",
                "dataset_name": "MainDataset",
                "category_expression": "=Fields!ProductName.Value",
                "value_expression": "=Count(Fields!ProductName.Value)",
                "top": "3in",
                "left": "0in",
                "width": "4in",
                "height": "3in",
            },
            second="refuses",
        ),
    ),
    (
        "add_chart_series",
        Case(
            {
                "chart_name": CH,
                "series_name": "Ratio",
                "value_expression": "=Avg(Fields!Amount.Value)",
            },
            second="refuses",
            fixture=CHART,
        ),
    ),
    (
        "set_chart_axis",
        Case(
            {"chart_name": CH, "axis": "Value", "chart_area": "Default", "visible": "Auto"},
            fixture=CHART,
        ),
    ),
    (
        "set_chart_category_group",
        Case(
            {"chart_name": CH, "add_level": "outer", "group_field": "ProductID"},
            fixture=CHART,
        ),
    ),
    (
        "set_chart_category_group",
        Case(
            {"chart_name": CH, "add_level": "inner", "group_field": "ProductID"},
            fixture=CHART,
        ),
    ),
    (
        "set_chart_series_properties",
        Case(
            {"chart_name": CH, "series_name": "Amount", "legend_text": "", "marker_type": ""},
            fixture=CHART,
            setup=[
                (
                    "set_chart_series_properties",
                    {
                        "chart_name": CH,
                        "series_name": "Amount",
                        "legend_text": "Sales",
                        "marker_type": "Square",
                    },
                )
            ],
        ),
    ),
    (
        "set_textbox_tooltip",
        Case(
            {"textbox_name": TB, "text_or_expression": ""},
            setup=[("set_textbox_tooltip", {"textbox_name": TB, "text_or_expression": "Tip"})],
        ),
    ),
    # clear=true (#109): the second clear finds nothing to remove.
    (
        "set_textbox_action",
        Case(
            {"textbox_name": TB, "clear": True},
            setup=[
                (
                    "set_textbox_action",
                    {"textbox_name": TB, "action_type": "Hyperlink", "target_expression": "x"},
                )
            ],
        ),
    ),
    (
        "set_bookmark",
        Case(
            {"name": TB, "bookmark_or_expression": ""},
            setup=[("set_bookmark", {"name": TB, "bookmark_or_expression": "Top"})],
        ),
    ),
    (
        "set_textbox_action",
        Case(
            {"textbox_name": TB, "action_type": "BookmarkLink", "target_expression": "Top"},
            setup=[("set_bookmark", {"name": "MainTable", "bookmark_or_expression": "Top"})],
        ),
    ),
    (
        "set_element_visibility",
        Case(
            {"element_name": TB, "clear": True},
            setup=[("set_element_visibility", {"element_name": TB, "hidden_expression": "=True"})],
        ),
    ),
    (
        "set_datasource_connection",
        Case(
            {
                "name": "PowerBIDataset",
                "workspace_url": "SampleWorkspace",
                "dataset_name": "SampleDataset",
                "integrated_security": False,
            }
        ),
    ),
    (
        "remove_calculated_field",
        Case(
            {"dataset_name": "MainDataset", "field_name": "Double", "force": True},
            second="refuses",
            setup=[
                (
                    "add_calculated_field",
                    {
                        "dataset_name": "MainDataset",
                        "field_name": "Double",
                        "expression": "=Fields!Amount.Value * 2",
                    },
                ),
                ("set_textbox_value", {"textbox_name": "Amount", "value": "=Fields!Double.Value"}),
            ],
        ),
    ),
    (
        "update_dataset_query",
        Case(
            {"dataset_name": "MainDataset", "dax_body": "EVALUATE 'X'", "designer_state": "drop"},
            setup=[
                (
                    "raw_xml_replace",
                    {
                        "xpath": ".//r:DataSet[@Name='MainDataset']/r:Query/rd:UseGenericDesigner",
                        "content": (
                            '<rd:DesignerState xmlns:rd="http://schemas.microsoft.com/'
                            'SQLServer/reporting/reportdesigner"><QueryDefinition xmlns='
                            '"http://schemas.microsoft.com/AnalysisServices/QueryDefinition">'
                            "<Query><Statement>EVALUATE 'Sales'</Statement></Query>"
                            "</QueryDefinition></rd:DesignerState>"
                        ),
                    },
                )
            ],
        ),
    ),
    (
        "raw_xml_replace",
        Case(
            {
                "xpath": ".//r:Tablix[@Name='MainTable']/r:Style",
                "content": "<Style><Border><Style>Solid</Style></Border></Style>",
            }
        ),
    ),
    (
        "update_dataset_query",
        Case(
            {
                "dataset_name": "MainDataset",
                "dax_body": "EVALUATE SUMMARIZECOLUMNS('Sales'[ProductID], \"Amount\", SUM('Sales'[Amount]))",
                "alias_strategy": "preserve_field_names",
            }
        ),
    ),
]

# Tools that can't meaningfully be called twice with the same arguments.
EXCLUDED: dict[str, str] = {
    "create_report": "refuses an existing path by design; nothing to re-run on",
    "duplicate_report": "refuses an existing dst_path by design (writes a new file)",
    "restore_from_backup": "copies a backup over the target; not an edit of the tree",
    "apply_edits": "runs other tools (each covered here) inside a transaction",
    # Append semantics: each call adds one more entry to a list, so an
    # identical second call is a second, distinct entry.
    "add_dataset_filter": "appends another <Filter> by design",
    "add_tablix_filter": "appends another <Filter> by design",
    "add_subtotal_row": "appends another subtotal row by design",
    "add_subtotal_column": "appends another subtotal column by design",
}


def _fill(value: Any, png: str) -> Any:
    if value == PNG:
        return png
    if isinstance(value, list):
        return [_fill(v, png) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, png) for k, v in value.items()}
    return value


def _call(tool: str, path: Path, args: dict, png: str) -> Any:
    _schema, handler, _mut = _REG.tools[tool]
    return handler(path=str(path), **_fill(args, png))


def _file_state(path: Path) -> tuple:
    st = os.stat(path)
    return (path.read_bytes(), st.st_ino, st.st_mtime_ns)


def _is_noop(changed: Any) -> bool:
    return changed is False or changed == [] or changed == {}


def test_every_mutating_tool_is_covered():
    covered = set(CASES) | set(EXCLUDED)
    assert not set(CASES) & set(EXCLUDED)
    missing = sorted(set(MUTATING) - covered)
    assert not missing, f"add an idempotency case (or exclusion) for: {missing}"
    stale = sorted(covered - set(MUTATING))
    assert not stale, f"not mutating tools any more: {stale}"


ALL_CASES = [(tool, CASES[tool]) for tool in sorted(CASES)] + VARIANTS
_IDS = [tool for tool, _case in ALL_CASES]
_IDS = [f"{t}#{_IDS[:i].count(t)}" if _IDS.count(t) > 1 else t for i, t in enumerate(_IDS)]


@pytest.mark.parametrize(("tool", "case"), ALL_CASES, ids=_IDS)
def test_second_identical_call_is_a_noop(tool, case, tmp_path):
    rdl = tmp_path / "report.rdl"
    shutil.copy(FIXTURES / case.fixture, rdl)
    png = tmp_path / "logo.png"
    png.write_bytes(_TINY_PNG)
    for step_tool, step_args in case.setup:
        _call(step_tool, rdl, step_args, str(png))
    initial = rdl.read_bytes()

    # The first call must really edit the report, or the case proves nothing.
    first = _call(tool, rdl, case.args, str(png))
    if isinstance(first, dict) and "changed" in first:
        assert not _is_noop(first["changed"]), f"first call changed nothing: {first}"
    before = _file_state(rdl)
    assert before[0] != initial, "first call didn't change the file; pick other arguments"

    if case.second == "refuses":
        with pytest.raises(Exception):  # noqa: B017 — any refusal will do
            _call(tool, rdl, case.args, str(png))
    else:
        second = _call(tool, rdl, case.args, str(png))
        assert isinstance(second, dict) and "changed" in second, second
        assert _is_noop(second["changed"]), f"second identical call reported a change: {second}"
    assert _file_state(rdl) == before, "second identical call rewrote the file"
