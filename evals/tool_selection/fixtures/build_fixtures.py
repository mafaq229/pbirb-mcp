"""Build the eval fixture reports by driving pbirb-mcp's own tools.

The fixture is synthetic but shaped like the real reports pbirb-mcp gets used
on (see .local/feedback): a Power BI dataset source with a DAX query, two
leftover data sources nobody uses, date parameters mistyped as String, a
title textbox, a detail tablix and a column chart on a portrait page.

Run from the repo root:  uv run python evals/tool_selection/fixtures/build_fixtures.py [all|rb]
"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

HERE = Path(__file__).parent

FLEET_DAX = """EVALUATE
SUMMARIZECOLUMNS(
    'Assets'[AssetID],
    'Assets'[AssetName],
    'Assets'[Site],
    'Assets'[EquipmentType],
    'Usage'[ReportDate],
    "OperatingHours", SUM('Usage'[OperatingHours]),
    "IdleHours", SUM('Usage'[IdleHours]),
    "FuelLitres", SUM('Usage'[FuelLitres]),
    "Utilization", DIVIDE(SUM('Usage'[OperatingHours]), SUM('Usage'[ScheduledHours]))
)"""

FLEET_FIELDS = [
    ("AssetID", "Assets[AssetID]", "System.String"),
    ("AssetName", "Assets[AssetName]", "System.String"),
    ("Site", "Assets[Site]", "System.String"),
    ("EquipmentType", "Assets[EquipmentType]", "System.String"),
    ("ReportDate", "Usage[ReportDate]", "System.DateTime"),
    ("OperatingHours", "[OperatingHours]", "System.Decimal"),
    ("IdleHours", "[IdleHours]", "System.Decimal"),
    ("FuelLitres", "[FuelLitres]", "System.Decimal"),
    ("Utilization", "[Utilization]", "System.Decimal"),
]


class Driver:
    """Calls tools through the same JSON-RPC path an MCP client uses."""

    def __init__(self) -> None:
        self.server = MCPServer()
        register_all_tools(self.server)
        self._id = 0

    def call(self, tool: str, **arguments):
        self._id += 1
        resp = self.server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        if "error" in resp:
            raise RuntimeError(f"{tool}: {resp['error']}")
        result = resp["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise RuntimeError(f"{tool}: {text}")
        return json.loads(text)


def build_fleet(path: Path) -> None:
    d = Driver()
    p = str(path)
    d.call(
        "create_report",
        path=p,
        datasource={
            "name": "FleetModel",
            "workspace_url": "FleetAnalytics",
            "dataset_name": "FleetOps",
            "provider": "pbidataset",
        },
    )
    dataset = d.call("describe_report", path=p)["datasets"][0]

    # Leftovers from an earlier version of the report: nothing references them.
    d.call(
        "add_data_source",
        path=p,
        name="LegacySQL",
        workspace_url="OldWorkspace",
        dataset_name="FleetOpsV1",
    )
    d.call(
        "add_data_source",
        path=p,
        name="ScratchPowerQuery",
        workspace_url="Sandbox",
        dataset_name="Scratch",
    )

    d.call("update_dataset_query", path=p, dataset_name=dataset, dax_body=FLEET_DAX)
    for name, data_field, type_name in FLEET_FIELDS:
        d.call(
            "add_dataset_field",
            path=p,
            dataset_name=dataset,
            field_name=name,
            data_field=data_field,
            type_name=type_name,
        )

    # Date parameters typed as String — a real mistake seen in the wild.
    d.call("add_parameter", path=p, name="DateFrom", type="String", prompt="Date From")
    d.call("add_parameter", path=p, name="DateTo", type="String", prompt="Date To")
    d.call("add_parameter", path=p, name="Site", type="String", prompt="Site")

    d.call(
        "add_body_textbox",
        path=p,
        name="ReportTitle",
        text="Fleet Utilization Report",
        top="0in",
        left="0in",
        width="5in",
        height="0.4in",
    )
    d.call(
        "insert_tablix_from_template",
        path=p,
        name="FleetTable",
        dataset_name=dataset,
        columns=[
            "AssetName",
            "Site",
            "EquipmentType",
            "OperatingHours",
            "IdleHours",
            "Utilization",
        ],
        top="0.6in",
        left="0in",
        width="6in",
        height="0.5in",
        # The committed fixture predates #67's default; keep its header
        # non-repeating so t12 ("make the header repeat") isn't pre-solved.
        repeat_header=False,
    )
    d.call(
        "add_body_textbox",
        path=p,
        name="TotalHoursLabel",
        text="Total Hours",
        top="1.3in",
        left="3in",
        width="1in",
        height="0.25in",
    )
    d.call(
        "add_body_textbox",
        path=p,
        name="TotalHoursValue",
        text='=Sum(Fields!OperatingHours.Value, "' + dataset + '")',
        top="1.3in",
        left="4in",
        width="1in",
        height="0.25in",
    )
    d.call(
        "insert_chart_from_template",
        path=p,
        name="HoursBySite",
        dataset_name=dataset,
        category_field="Site",
        value_field="OperatingHours",
        top="2in",
        left="0in",
        width="6in",
        height="3in",
    )
    verdict = d.call("verify_report", path=p)
    print(json.dumps(verdict, indent=1)[:800])


def _report_builder_groups(path: Path, tablix: str, group: str, expression: str) -> None:
    """Reshape ``tablix``'s row hierarchy the way Report Builder writes it:
    the detail member wrapped in a dynamic ``group`` that has no
    group-header row of its own (built directly here so the fixture matches
    Report Builder's output byte-for-byte; ``add_row_group(header="none")``
    now produces the same member shape), and ``KeepWithGroup=Before`` on the
    trailing static total row."""
    from lxml import etree

    from pbirb_mcp.core.document import RDLDocument
    from pbirb_mcp.core.xpath import find_child, find_children, q

    doc = RDLDocument.open(path)
    t = next(x for x in doc.root.iter(q("Tablix")) if x.get("Name") == tablix)
    members = find_child(find_child(t, "TablixRowHierarchy"), "TablixMembers")
    top = find_children(members, "TablixMember")
    detail = next(m for m in top if find_child(m, "Group") is not None)
    wrapper = etree.Element(q("TablixMember"))
    g = etree.SubElement(wrapper, q("Group"), Name=group)
    etree.SubElement(
        etree.SubElement(g, q("GroupExpressions")), q("GroupExpression")
    ).text = expression
    sort = etree.SubElement(etree.SubElement(wrapper, q("SortExpressions")), q("SortExpression"))
    etree.SubElement(sort, q("Value")).text = expression
    inner = etree.SubElement(wrapper, q("TablixMembers"))
    detail.addprevious(wrapper)
    inner.append(detail)
    total = top[-1]
    assert total is not detail and find_child(total, "Group") is None
    etree.SubElement(total, q("KeepWithGroup")).text = "Before"
    doc.save()


def build_fleet_rb(src: Path, path: Path) -> None:
    """The fleet report after a Report Builder session.

    Starts from ``src`` (fleet_utilization.rdl) and adds the shapes the eval's
    original cases never touched: a styled column-header row, the detail rows
    wrapped in a Site group with no group-header row, a static grand-total
    row (Report Builder's "Add Total"), a two-series chart, and a Site
    parameter referenced from the title, a table filter, the query and the
    parameter-prompt layout.
    """
    shutil.copy(src, path)
    d = Driver()
    p = str(path)
    dataset = "DataSet1"
    d.call(
        "style_tablix_row",
        path=p,
        tablix_name="FleetTable",
        row="header",
        font_family="Segoe UI",
        font_weight="Bold",
        color="#FFFFFF",
        background_color="#1F3864",
        border_style="Solid",
        border_color="#BFBFBF",
        text_align="Center",
    )
    d.call(
        "add_static_row",
        path=p,
        tablix_name="FleetTable",
        row_name="FleetTable_Total",
        cells=[
            "Total",
            "",
            "",
            "=Sum(Fields!OperatingHours.Value)",
            "=Sum(Fields!IdleHours.Value)",
            "",
        ],
    )
    _report_builder_groups(path, "FleetTable", "Site", "=Fields!Site.Value")
    d.call(
        "set_textbox_runs",
        path=p,
        textbox_name="ReportTitle",
        runs=[
            {"text": "Fleet Utilization: ", "font_weight": "Bold"},
            {"text": "=Parameters!Site.Value"},
        ],
    )
    d.call(
        "add_tablix_filter",
        path=p,
        tablix_name="FleetTable",
        expression="=Fields!Site.Value",
        operator="Equal",
        values=["=Parameters!Site.Value"],
    )
    d.call(
        "add_query_parameter",
        path=p,
        dataset_name=dataset,
        name="Site",
        value_expression="=Parameters!Site.Value",
    )
    d.call(
        "set_parameter_layout",
        path=p,
        rows=1,
        columns=3,
        parameter_order=["DateFrom", "DateTo", "Site"],
    )
    d.call(
        "add_chart_series",
        path=p,
        chart_name="HoursBySite",
        series_name="IdleHours",
        value_field="IdleHours",
    )
    verdict = d.call("verify_report", path=p)
    print(json.dumps(verdict, indent=1)[:800])


def write_logo(path: Path) -> None:
    """A 64x24 solid-navy PNG, written by hand so no imaging library is needed."""
    width, height = 64, 24
    raw = b"".join(b"\x00" + b"\x1f\x38\x64" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def main(argv: list[str]) -> int:
    """``build_fixtures.py [all|rb]``. ``rb`` rebuilds only fleet_rb.rdl from
    the committed fleet_utilization.rdl: rebuilding the fleet report itself
    mints new DataSourceID / ReportID GUIDs, which changes every case's
    starting file."""
    what = argv[0] if argv else "all"
    if what not in ("all", "rb"):
        sys.exit("usage: build_fixtures.py [all|rb]")
    out = HERE / "fleet_utilization.rdl"
    rb = HERE / "fleet_rb.rdl"
    if what == "all":
        if out.exists():
            out.unlink()
        build_fleet(out)
        write_logo(HERE / "logo.png")
        shutil.copy(
            HERE.parents[2] / "tests" / "fixtures" / "known_bad" / "bare_action_under_textbox.rdl",
            HERE / "bare_action_under_textbox.rdl",
        )
    build_fleet_rb(out, rb)
    print(f"wrote fixtures ({what}) in {HERE.relative_to(HERE.parents[2])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
