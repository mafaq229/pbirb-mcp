"""Build the eval fixture reports by driving pbirb-mcp's own tools.

The fixture is synthetic but shaped like the real reports pbirb-mcp gets used
on (see .local/feedback): a Power BI dataset source with a DAX query, two
leftover data sources nobody uses, date parameters mistyped as String, a
title textbox, a detail tablix and a column chart on a portrait page.

Run from the repo root:  uv run python evals/tool_selection/fixtures/build_fixtures.py
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


def main() -> int:
    out = HERE / "fleet_utilization.rdl"
    if out.exists():
        out.unlink()
    build_fleet(out)
    write_logo(HERE / "logo.png")
    shutil.copy(
        HERE.parents[2] / "tests" / "fixtures" / "known_bad" / "bare_action_under_textbox.rdl",
        HERE / "bare_action_under_textbox.rdl",
    )
    print(f"wrote {out.relative_to(HERE.parents[2])}, logo.png, bare_action_under_textbox.rdl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
