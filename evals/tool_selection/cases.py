"""Eval cases: realistic report-editing requests with end-state checks.

Each case has
  prompt   what a report author would actually type (never names a tool)
  oracle   a reference solution as tool calls; proves the case is solvable
           and documents which tools a good agent would reach for
  check    reads the edited .rdl with lxml (not through pbirb-mcp, so the
           grader never trusts the system under test) and returns
           (passed, reason)

Categories (tags[0] in the report): data, parameters, tablix, style, layout,
chart, interactivity, question (read-only: must answer, must not edit),
decline (out of scope: must not edit).

`source` records where a case came from: "feedback" cases are rewritten from
real sessions in .local/feedback, "synth" cases were written from the docs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from lxml import etree

FIXTURES = Path(__file__).parent / "fixtures"
FLEET = "fleet_utilization.rdl"
BAD_ACTION = "bare_action_under_textbox.rdl"

NS = {
    "r": "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition",
    "rd": "http://schemas.microsoft.com/SQLServer/reporting/reportdesigner",
}

CheckResult = tuple[bool, str]


class Rdl:
    """Minimal read-only view of an .rdl for grading."""

    def __init__(self, path: Path):
        self.raw = path.read_text(encoding="utf-8")
        self.tree = etree.fromstring(self.raw.encode("utf-8"))

    def x(self, xpath: str, node=None) -> list:
        return (node if node is not None else self.tree).xpath(xpath, namespaces=NS)

    def texts(self, xpath: str, node=None) -> list[str]:
        return [(e.text or "").strip() for e in self.x(xpath, node)]

    def text(self, xpath: str, node=None) -> Optional[str]:
        t = self.texts(xpath, node)
        return t[0] if t else None

    def textbox(self, name: str):
        found = self.x(f"//r:Textbox[@Name='{name}']")
        return found[0] if found else None

    def textbox_value(self, name: str) -> str:
        tb = self.textbox(name)
        if tb is None:
            return ""
        return "".join(self.texts(".//r:TextRun/r:Value", tb))

    def run_style(self, tb, prop: str) -> list[str]:
        return self.texts(f".//r:TextRun/r:Style/r:{prop}", tb)

    def box_style(self, tb, prop: str) -> Optional[str]:
        return self.text(f"./r:Style/r:{prop}", tb)

    def param(self, name: str):
        found = self.x(f"//r:ReportParameter[@Name='{name}']")
        return found[0] if found else None

    def field(self, name: str):
        found = self.x(f"//r:DataSet/r:Fields/r:Field[@Name='{name}']")
        return found[0] if found else None

    def tablix_textboxes(self, tablix: str, row: Optional[int] = None) -> list:
        rows = self.x(f"//r:Tablix[@Name='{tablix}']/r:TablixBody/r:TablixRows/r:TablixRow")
        if row is not None:
            rows = rows[row : row + 1]
        out = []
        for r in rows:
            out.extend(self.x(".//r:Textbox", r))
        return out

    def chart(self, name: str = "HoursBySite"):
        found = self.x(f"//r:Chart[@Name='{name}']")
        return found[0] if found else None


def inches(size: Optional[str]) -> Optional[float]:
    if not size:
        return None
    m = re.fullmatch(r"\s*([0-9.]+)\s*(in|cm|mm|pt)\s*", size)
    if not m:
        return None
    v, unit = float(m.group(1)), m.group(2)
    return {"in": v, "cm": v / 2.54, "mm": v / 25.4, "pt": v / 72}[unit]


def ok(cond: bool, reason: str) -> CheckResult:
    return (bool(cond), "ok" if cond else reason)


def all_of(*results: CheckResult) -> CheckResult:
    for passed, reason in results:
        if not passed:
            return (False, reason)
    return (True, "ok")


def norm_color(c: Optional[str]) -> str:
    c = (c or "").strip().lower()
    return {"white": "#ffffff", "black": "#000000"}.get(c, c)


def no_double_encoding(rdl: Rdl) -> CheckResult:
    return ok(
        "&amp;amp;" not in rdl.raw and "&amp;gt;" not in rdl.raw and "&amp;lt;" not in rdl.raw,
        "expression was double XML-encoded (&amp;amp; / &amp;gt; in file)",
    )


@dataclass
class Case:
    id: str
    category: str
    difficulty: str  # easy | medium | hard
    source: str  # feedback | synth
    prompt: str
    oracle: list[tuple[str, dict]]
    check: Optional[Callable[[Rdl], CheckResult]] = None
    fixture: str = FLEET
    must_not_edit: bool = False
    answer_must_mention: list[list[str]] = field(
        default_factory=list
    )  # AND of ORs, case-insensitive

    @property
    def tags(self) -> list[str]:
        return [self.category, self.difficulty, self.source]


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


def _d01(r: Rdl):
    names = set(e.get("Name") for e in r.x("//r:DataSource"))
    return ok(names == {"FleetModel"}, f"data sources left: {sorted(names)}")


def _d02(r: Rdl):
    names = set(e.get("Name") for e in r.x("//r:DataSource"))
    return all_of(
        ok(
            "FleetSemanticModel" in names and "FleetModel" not in names,
            f"data sources: {sorted(names)}",
        ),
        ok(
            r.texts("//r:DataSet/r:Query/r:DataSourceName") == ["FleetSemanticModel"],
            "dataset not repointed to renamed source",
        ),
    )


def _d03(r: Rdl):
    f = r.field("IdlePct")
    v = r.text("./r:Value", f) if f is not None else ""
    return ok(
        f is not None and "IdleHours" in (v or "") and "OperatingHours" in (v or ""),
        f"IdlePct calculated field missing or wrong: {v!r}",
    )


def _d04(r: Rdl):
    flt = r.x("//r:DataSet/r:Filters/r:Filter")
    hits = [
        f
        for f in flt
        if "EquipmentType" in (r.text("./r:FilterExpression", f) or "")
        and any("Excavator" in v for v in r.texts(".//r:FilterValue", f))
    ]
    return ok(hits, "no dataset-level filter EquipmentType = Excavator")


def _d05(r: Rdl):
    cmd = r.text("//r:DataSet/r:Query/r:CommandText") or ""
    f = r.field("Manufacturer")
    return all_of(
        ok("Manufacturer" in cmd, "DAX query does not select Manufacturer"),
        ok(
            f is not None and "Manufacturer" in (r.text("./r:DataField", f) or ""),
            "no Manufacturer field bound to the new column",
        ),
        ok(
            r.text("./r:DataField", r.field("AssetName")) == "Assets[AssetName]",
            "existing field bindings were rewritten",
        ),
        ok("SUM('Usage'[OperatingHours])" in cmd, "rest of the DAX query was lost"),
    )


def _d06(r: Rdl):
    cs = r.text("//r:DataSource[@Name='FleetModel']//r:ConnectString") or ""
    return ok("FleetAnalytics-Prod" in cs and "FleetOps" in cs, f"connect string: {cs!r}")


def _d07(r: Rdl):
    vals = r.texts("//r:DataSet/r:Query/r:QueryParameters/r:QueryParameter/r:Value")
    joined = " ".join(vals)
    return ok(
        "Parameters!DateFrom" in joined and "Parameters!DateTo" in joined,
        f"query parameter values: {vals}",
    )


def _d08(r: Rdl):
    return ok(
        r.field("FuelLitres") is None and r.field("Utilization") is not None,
        "FuelLitres still present (or other fields lost)",
    )


DATA = [
    Case(
        "d01",
        "data",
        "easy",
        "feedback",
        "Clean up this report: remove the two data sources that nothing uses.",
        [
            ("remove_data_source", {"name": "LegacySQL"}),
            ("remove_data_source", {"name": "ScratchPowerQuery"}),
        ],
        _d01,
    ),
    Case(
        "d02",
        "data",
        "medium",
        "synth",
        "Rename the FleetModel data source to FleetSemanticModel.",
        [("rename_data_source", {"old_name": "FleetModel", "new_name": "FleetSemanticModel"})],
        _d02,
    ),
    Case(
        "d03",
        "data",
        "easy",
        "synth",
        "Add a calculated field called IdlePct: idle hours divided by the sum of operating and idle hours.",
        [
            (
                "add_calculated_field",
                {
                    "dataset_name": "DataSet1",
                    "field_name": "IdlePct",
                    "expression": "=Fields!IdleHours.Value / (Fields!OperatingHours.Value + Fields!IdleHours.Value)",
                },
            )
        ],
        _d03,
    ),
    Case(
        "d04",
        "data",
        "medium",
        "synth",
        "Filter the dataset itself so only Excavator equipment comes through.",
        [
            (
                "add_dataset_filter",
                {
                    "dataset_name": "DataSet1",
                    "expression": "=Fields!EquipmentType.Value",
                    "operator": "Equal",
                    "values": ["Excavator"],
                },
            )
        ],
        _d04,
    ),
    Case(
        "d05",
        "data",
        "hard",
        "feedback",
        "The query also needs 'Assets'[Manufacturer]. Add it to the DAX and expose it as a Manufacturer field, "
        "without breaking the existing fields.",
        [
            (
                "update_dataset_query",
                {
                    "dataset_name": "DataSet1",
                    "dax_body": "EVALUATE\nSUMMARIZECOLUMNS(\n    'Assets'[AssetID],\n    'Assets'[AssetName],\n    'Assets'[Site],\n    'Assets'[EquipmentType],\n    'Assets'[Manufacturer],\n    'Usage'[ReportDate],\n    \"OperatingHours\", SUM('Usage'[OperatingHours]),\n    \"IdleHours\", SUM('Usage'[IdleHours]),\n    \"FuelLitres\", SUM('Usage'[FuelLitres]),\n    \"Utilization\", DIVIDE(SUM('Usage'[OperatingHours]), SUM('Usage'[ScheduledHours]))\n)",
                },
            ),
            (
                "add_dataset_field",
                {
                    "dataset_name": "DataSet1",
                    "field_name": "Manufacturer",
                    "data_field": "Assets[Manufacturer]",
                    "type_name": "System.String",
                },
            ),
        ],
        _d05,
    ),
    Case(
        "d06",
        "data",
        "easy",
        "synth",
        "Point the FleetModel connection at the 'FleetAnalytics-Prod' workspace instead. Same semantic model.",
        [
            (
                "set_datasource_connection",
                {
                    "name": "FleetModel",
                    "workspace_url": "FleetAnalytics-Prod",
                    "dataset_name": "FleetOps",
                },
            )
        ],
        _d06,
    ),
    Case(
        "d07",
        "data",
        "medium",
        "synth",
        "Pass the DateFrom and DateTo report parameters through to the dataset query as query parameters.",
        [
            (
                "add_query_parameter",
                {
                    "dataset_name": "DataSet1",
                    "name": "DateFrom",
                    "value_expression": "=Parameters!DateFrom.Value",
                },
            ),
            (
                "add_query_parameter",
                {
                    "dataset_name": "DataSet1",
                    "name": "DateTo",
                    "value_expression": "=Parameters!DateTo.Value",
                },
            ),
        ],
        _d07,
    ),
    Case(
        "d08",
        "data",
        "easy",
        "synth",
        "Nothing uses FuelLitres any more. Remove that field from the dataset.",
        [("remove_dataset_field", {"dataset_name": "DataSet1", "field_name": "FuelLitres"})],
        _d08,
    ),
]


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def _p01(r: Rdl):
    types = {n: r.text("./r:DataType", r.param(n)) for n in ("DateFrom", "DateTo")}
    return ok(all(t == "DateTime" for t in types.values()), f"types: {types}")


def _p03(r: Rdl):
    vals = r.texts(
        "//r:ReportParameter[@Name='Site']/r:ValidValues/r:ParameterValues/r:ParameterValue/r:Value"
    )
    return ok(sorted(vals) == ["Das Island", "Habshan", "Ruwais"], f"available values: {vals}")


def _p05(r: Rdl):
    f = " ".join(r.texts("//r:ReportParameter[@Name='DateFrom']/r:DefaultValue//r:Value"))
    t = " ".join(r.texts("//r:ReportParameter[@Name='DateTo']/r:DefaultValue//r:Value"))
    return all_of(
        ok("Today" in t or "Now" in t, f"DateTo default: {t!r}"),
        ok(("Today" in f or "Now" in f) and "30" in f, f"DateFrom default: {f!r}"),
    )


def _p06(r: Rdl):
    p = r.param("TopN")
    if p is None:
        return (False, "no TopN parameter")
    return all_of(
        ok(r.text("./r:DataType", p) == "Integer", "TopN is not Integer"),
        ok((r.text("./r:Hidden", p) or "").lower() == "true", "TopN is not hidden"),
        ok("10" in r.texts("./r:DefaultValue//r:Value", p), "TopN default is not 10"),
    )


def _p08(r: Rdl):
    cols = r.text("//r:ReportParametersLayout/r:GridLayoutDefinition/r:NumberOfColumns")
    rows = r.text("//r:ReportParametersLayout/r:GridLayoutDefinition/r:NumberOfRows")
    return ok(cols == "3" and rows == "1", f"layout rows={rows} cols={cols}")


PARAMETERS = [
    Case(
        "p01",
        "parameters",
        "easy",
        "feedback",
        "DateFrom and DateTo should be proper date pickers, not free-text boxes.",
        [
            ("set_parameter_type", {"name": "DateFrom", "type": "DateTime"}),
            ("set_parameter_type", {"name": "DateTo", "type": "DateTime"}),
        ],
        _p01,
    ),
    Case(
        "p02",
        "parameters",
        "easy",
        "synth",
        "Change the Site parameter's prompt to 'Select site'.",
        [("set_parameter_prompt", {"name": "Site", "prompt": "Select site"})],
        lambda r: ok(r.text("./r:Prompt", r.param("Site")) == "Select site", "prompt not updated"),
    ),
    Case(
        "p03",
        "parameters",
        "medium",
        "synth",
        "Users should pick the site from a fixed list: Ruwais, Habshan, Das Island.",
        [
            (
                "set_parameter_available_values",
                {
                    "name": "Site",
                    "source": "static",
                    "static_values": ["Ruwais", "Habshan", "Das Island"],
                },
            )
        ],
        _p03,
    ),
    Case(
        "p04",
        "parameters",
        "easy",
        "synth",
        "Let people select more than one site at a time.",
        [("update_parameter_advanced", {"name": "Site", "multi_value": True})],
        lambda r: ok(
            (r.text("./r:MultiValue", r.param("Site")) or "").lower() == "true",
            "Site is not multi-value",
        ),
    ),
    Case(
        "p05",
        "parameters",
        "medium",
        "synth",
        "Default the date range to the last 30 days: DateFrom 30 days ago, DateTo today.",
        [
            (
                "set_parameter_default_values",
                {
                    "name": "DateFrom",
                    "source": "static",
                    "static_values": ['=DateAdd("d", -30, Today())'],
                },
            ),
            (
                "set_parameter_default_values",
                {"name": "DateTo", "source": "static", "static_values": ["=Today()"]},
            ),
        ],
        _p05,
    ),
    Case(
        "p06",
        "parameters",
        "medium",
        "synth",
        "Add a hidden Integer parameter TopN with a default of 10.",
        [
            ("add_parameter", {"name": "TopN", "type": "Integer", "hidden": True}),
            (
                "set_parameter_default_values",
                {"name": "TopN", "source": "static", "static_values": ["10"]},
            ),
        ],
        _p06,
    ),
    Case(
        "p07",
        "parameters",
        "easy",
        "synth",
        "Rename the Site parameter to SiteName.",
        [("rename_parameter", {"old_name": "Site", "new_name": "SiteName"})],
        lambda r: ok(
            r.param("SiteName") is not None and r.param("Site") is None, "rename not applied"
        ),
    ),
    Case(
        "p08",
        "parameters",
        "medium",
        "synth",
        "Lay the parameter prompts out on a single row: DateFrom, DateTo, then Site.",
        [
            (
                "set_parameter_layout",
                {"rows": 1, "columns": 3, "parameter_order": ["DateFrom", "DateTo", "Site"]},
            )
        ],
        _p08,
    ),
]


# ---------------------------------------------------------------------------
# tablix
# ---------------------------------------------------------------------------


def _t01(r: Rdl):
    grp = r.x(
        "//r:Tablix[@Name='FleetTable']/r:TablixRowHierarchy//r:Group[r:GroupExpressions/r:GroupExpression[contains(., 'Fields!Site.Value')]]"
    )
    vals = " ".join(r.texts("//r:Tablix[@Name='FleetTable']//r:TextRun/r:Value"))
    return all_of(
        ok(grp, "no row group on Site"),
        ok(
            re.search(r"Sum\(Fields!OperatingHours\.Value", vals)
            and re.search(r"Sum\(Fields!IdleHours\.Value", vals),
            "no Sum() subtotals for operating and idle hours",
        ),
    )


def _t02(r: Rdl):
    vals = r.texts("//r:Tablix[@Name='FleetTable']//r:TextRun/r:Value")
    ncols = len(r.x("//r:Tablix[@Name='FleetTable']/r:TablixBody/r:TablixColumns/r:TablixColumn"))
    return all_of(
        ok("=Fields!FuelLitres.Value" in vals, "no cell bound to FuelLitres"),
        ok("Fuel (L)" in vals, "no 'Fuel (L)' header"),
        ok(ncols == 7, f"expected 7 columns, found {ncols}"),
    )


def _t03(r: Rdl):
    vals = r.texts("//r:Tablix[@Name='FleetTable']//r:TextRun/r:Value")
    ncols = len(r.x("//r:Tablix[@Name='FleetTable']/r:TablixBody/r:TablixColumns/r:TablixColumn"))
    return ok(
        "=Fields!EquipmentType.Value" not in vals and ncols == 5,
        f"EquipmentType still present or {ncols} columns",
    )


def _t05(r: Rdl):
    # set_group_sort has no Direction argument, so a negated expression is an
    # equally valid way to sort highest-first.
    sorts = r.x("//r:Tablix[@Name='FleetTable']//r:SortExpression")
    hit = [
        s
        for s in sorts
        if "Utilization" in (r.text("./r:Value", s) or "")
        and (
            (r.text("./r:Direction", s) or "") == "Descending"
            or re.match(r"=\s*-|=.*\*\s*-1", r.text("./r:Value", s) or "")
        )
    ]
    return ok(hit, "no highest-first sort on Utilization")


def _t06(r: Rdl):
    row = r.x(
        "//r:Tablix[@Name='FleetTable']/r:TablixRowHierarchy//r:Group[r:GroupExpressions/r:GroupExpression[contains(., 'Fields!Site.Value')]]"
    )
    col = r.x(
        "//r:Tablix[@Name='FleetTable']/r:TablixColumnHierarchy//r:Group[r:GroupExpressions/r:GroupExpression[contains(., 'Fields!EquipmentType.Value')]]"
    )
    details = r.x("//r:Tablix[@Name='FleetTable']//r:Group[@Name='Details']")
    corner = " ".join(r.texts("//r:Tablix[@Name='FleetTable']/r:TablixCorner//r:TextRun/r:Value"))
    return all_of(
        ok(row, "no Site row group"),
        ok(col, "no EquipmentType column group"),
        ok(not details, "Details group still present (not a matrix)"),
        ok("Site" in corner, f"corner text: {corner!r}"),
    )


def _t07(r: Rdl):
    flt = r.x("//r:Tablix[@Name='FleetTable']/r:Filters/r:Filter")
    hit = [
        f
        for f in flt
        if "Utilization" in (r.text("./r:FilterExpression", f) or "")
        and r.text("./r:Operator", f) in ("LessThan", "LessThanOrEqual")
    ]
    return all_of(
        ok(hit, "no table filter Utilization < 0.5"),
        ok(not r.x("//r:DataSet/r:Filters"), "filtered the dataset instead of the table"),
    )


TABLIX = [
    Case(
        "t01",
        "tablix",
        "hard",
        "synth",
        "Group the table by site, with a subtotal row per site showing total operating hours and idle hours.",
        [
            (
                "add_row_group",
                {
                    "tablix_name": "FleetTable",
                    "group_name": "SiteGroup",
                    "group_expression": "=Fields!Site.Value",
                },
            ),
            (
                "add_subtotal_row",
                {
                    "tablix_name": "FleetTable",
                    "group_name": "SiteGroup",
                    "aggregates": [
                        {
                            "column": "FleetTable_OperatingHours",
                            "expression": "=Sum(Fields!OperatingHours.Value)",
                        },
                        {
                            "column": "FleetTable_IdleHours",
                            "expression": "=Sum(Fields!IdleHours.Value)",
                        },
                    ],
                },
            ),
        ],
        _t01,
    ),
    Case(
        "t02",
        "tablix",
        "medium",
        "synth",
        "Add a 'Fuel (L)' column to the table showing fuel litres, right after Idle Hours.",
        [
            (
                "add_tablix_column",
                {
                    "tablix_name": "FleetTable",
                    "column_name": "FuelLitres",
                    "expression": "=Fields!FuelLitres.Value",
                    "position": 5,
                    "header_text": "Fuel (L)",
                },
            )
        ],
        _t02,
    ),
    Case(
        "t03",
        "tablix",
        "easy",
        "synth",
        "Drop the Equipment Type column from the table.",
        [
            (
                "remove_tablix_column",
                {"tablix_name": "FleetTable", "column_name": "FleetTable_EquipmentType"},
            )
        ],
        _t03,
    ),
    Case(
        "t04",
        "tablix",
        "easy",
        "synth",
        "Make the Asset Name column in the table 2 inches wide.",
        [("set_column_width", {"tablix_name": "FleetTable", "column": 0, "width": "2in"})],
        lambda r: ok(
            inches(
                r.text(
                    "//r:Tablix[@Name='FleetTable']/r:TablixBody/r:TablixColumns/r:TablixColumn[1]/r:Width"
                )
            )
            == 2.0,
            "first column is not 2in",
        ),
    ),
    Case(
        "t05",
        "tablix",
        "medium",
        "synth",
        "Sort the table rows by utilization, highest first.",
        [
            (
                "set_group_sort",
                {
                    "tablix_name": "FleetTable",
                    "group_name": "Details",
                    "sort_expressions": ["=-Fields!Utilization.Value"],
                },
            )
        ],
        _t05,
    ),
    Case(
        "t06",
        "tablix",
        "hard",
        "feedback",
        "Turn the table into a matrix: sites down the side, equipment types across the top, operating hours in the cells, "
        "and 'Site' in the top-left corner.",
        [
            (
                "add_row_group",
                {
                    "tablix_name": "FleetTable",
                    "group_name": "SiteGroup",
                    "group_expression": "=Fields!Site.Value",
                },
            ),
            (
                "add_column_group",
                {
                    "tablix_name": "FleetTable",
                    "group_name": "TypeGroup",
                    "group_expression": "=Fields!EquipmentType.Value",
                },
            ),
            (
                "convert_to_matrix",
                {
                    "tablix_name": "FleetTable",
                    "row_group": "SiteGroup",
                    "column_group": "TypeGroup",
                },
            ),
            ("set_tablix_corner", {"tablix_name": "FleetTable", "text": "Site"}),
        ],
        _t06,
    ),
    Case(
        "t07",
        "tablix",
        "medium",
        "synth",
        "In the table only (not the dataset), show just the assets with utilization below 50%.",
        [
            (
                "add_tablix_filter",
                {
                    "tablix_name": "FleetTable",
                    "expression": "=Fields!Utilization.Value",
                    "operator": "LessThan",
                    "values": ["=0.5"],
                },
            )
        ],
        _t07,
    ),
]


# ---------------------------------------------------------------------------
# style
# ---------------------------------------------------------------------------

HEADER_CELLS = [
    "FleetTable_Header_" + c
    for c in ("AssetName", "Site", "EquipmentType", "OperatingHours", "IdleHours", "Utilization")
]
DETAIL_CELLS = [
    "FleetTable_" + c
    for c in ("AssetName", "Site", "EquipmentType", "OperatingHours", "IdleHours", "Utilization")
]


def _s01(r: Rdl):
    bad = []
    for n in HEADER_CELLS:
        tb = r.textbox(n)
        if tb is None:
            bad.append(f"{n} missing")
            continue
        if "Bold" not in r.run_style(tb, "FontWeight"):
            bad.append(f"{n} not bold")
        if norm_color(r.box_style(tb, "BackgroundColor")) != "#1f3864":
            bad.append(f"{n} background {r.box_style(tb, 'BackgroundColor')}")
        if norm_color((r.run_style(tb, "Color") or [""])[0]) != "#ffffff":
            bad.append(f"{n} text colour")
    return ok(not bad, "; ".join(bad[:3]))


def _s02(r: Rdl):
    bg = [r.box_style(r.textbox(n), "BackgroundColor") or "" for n in DETAIL_CELLS]
    return ok(
        all("RowNumber" in b and "F2F2F2" in b.upper() for b in bg),
        f"detail backgrounds: {bg[0]!r}",
    )


def _s03(r: Rdl):
    bg = [r.box_style(r.textbox(n), "BackgroundColor") or "" for n in DETAIL_CELLS]
    return ok(
        all("Utilization" in b and ("<" in b or "0.5" in b) for b in bg),
        f"detail backgrounds: {bg[0]!r}",
    )


def _s04(r: Rdl):
    fmts = [
        (r.run_style(r.textbox(n), "Format") or [""])[0]
        for n in ("FleetTable_OperatingHours", "FleetTable_IdleHours")
    ]
    return ok(all(f in ("#,##0.0", "N1", "n1", "#,0.0") for f in fmts), f"formats: {fmts}")


def _s05(r: Rdl):
    f = (r.run_style(r.textbox("FleetTable_Utilization"), "Format") or [""])[0]
    return ok(re.fullmatch(r"[Pp]\d?|0(\.0+)?%", f or "") is not None, f"format: {f!r}")


def _s06(r: Rdl):
    tb = r.textbox("ReportTitle")
    return all_of(
        ok(
            inches(r.run_style(tb, "FontSize")[0] if r.run_style(tb, "FontSize") else None)
            == inches("16pt"),
            "title not 16pt",
        ),
        ok("Bold" in r.run_style(tb, "FontWeight"), "title not bold"),
        ok("Segoe UI" in r.run_style(tb, "FontFamily"), "title not Segoe UI"),
    )


def _s07(r: Rdl):
    tb = r.textbox("TotalHoursLabel")
    return all_of(
        ok(
            r.textbox_value("TotalHoursLabel") == "Total Operating Hours",
            f"label text: {r.textbox_value('TotalHoursLabel')!r}",
        ),
        ok("Italic" in r.run_style(tb, "FontStyle"), "label not italic"),
    )


def _s08(r: Rdl):
    tb = r.textbox("ReportTitle")
    runs = r.x(".//r:TextRun", tb)
    bold_title = [
        x
        for x in runs
        if "Fleet Utilization" in (r.text("./r:Value", x) or "")
        and r.text("./r:Style/r:FontWeight", x) == "Bold"
    ]
    site = [
        x
        for x in runs
        if "Parameters!Site" in (r.text("./r:Value", x) or "")
        and r.text("./r:Style/r:FontWeight", x) in (None, "Normal")
    ]
    return all_of(
        ok(len(runs) >= 2, "title is a single run"),
        ok(bold_title, "no bold 'Fleet Utilization' run"),
        ok(site, "no normal-weight Site parameter run"),
    )


def _s09(r: Rdl):
    aligns = [
        r.text(".//r:Paragraph/r:Style/r:TextAlign", r.textbox(n))
        for n in ("FleetTable_OperatingHours", "FleetTable_IdleHours", "FleetTable_Utilization")
    ]
    return ok(all(a == "Center" for a in aligns), f"alignments: {aligns}")


STYLE = [
    Case(
        "s01",
        "style",
        "medium",
        "synth",
        "Make the table's header row bold white text on a dark navy (#1F3864) background.",
        [
            (
                "style_tablix_row",
                {
                    "tablix_name": "FleetTable",
                    "row": "header",
                    "font_weight": "Bold",
                    "color": "#FFFFFF",
                    "background_color": "#1F3864",
                },
            )
        ],
        _s01,
    ),
    Case(
        "s02",
        "style",
        "medium",
        "synth",
        "Shade alternate data rows in the table light grey (#F2F2F2) and white.",
        [
            (
                "set_alternating_row_color",
                {"tablix_name": "FleetTable", "color_a": "#F2F2F2", "color_b": "White"},
            )
        ],
        _s02,
    ),
    Case(
        "s03",
        "style",
        "hard",
        "synth",
        "Highlight table rows in light red where utilization is under 50%.",
        [
            (
                "set_textbox_style_bulk",
                {
                    "textbox_names": DETAIL_CELLS,
                    "background_color": '=IIf(Fields!Utilization.Value < 0.5, "#F8D7DA", "Transparent")',
                },
            )
        ],
        _s03,
    ),
    Case(
        "s04",
        "style",
        "easy",
        "synth",
        "Show Operating Hours and Idle Hours in the table with thousands separators and one decimal place.",
        [
            (
                "set_textbox_style_bulk",
                {
                    "textbox_names": ["FleetTable_OperatingHours", "FleetTable_IdleHours"],
                    "format": "#,##0.0",
                },
            )
        ],
        _s04,
    ),
    Case(
        "s05",
        "style",
        "easy",
        "synth",
        "Format the Utilization values in the table as percentages.",
        [("set_textbox_style", {"textbox_name": "FleetTable_Utilization", "format": "P0"})],
        _s05,
    ),
    Case(
        "s06",
        "style",
        "easy",
        "synth",
        "Make the report title 16pt bold Segoe UI.",
        [
            (
                "set_textbox_style",
                {
                    "textbox_name": "ReportTitle",
                    "font_size": "16pt",
                    "font_weight": "Bold",
                    "font_family": "Segoe UI",
                },
            )
        ],
        _s06,
    ),
    Case(
        "s07",
        "style",
        "medium",
        "synth",
        "Change the 'Total Hours' label to read 'Total Operating Hours' and italicise it.",
        # set_textbox_style has no font_style; italics only exist on set_textbox_runs.
        [
            (
                "set_textbox_runs",
                {
                    "textbox_name": "TotalHoursLabel",
                    "runs": [{"text": "Total Operating Hours", "font_style": "Italic"}],
                },
            )
        ],
        _s07,
    ),
    Case(
        "s08",
        "style",
        "hard",
        "feedback",
        "In the report title, show 'Fleet Utilization: ' in bold followed by the selected Site parameter value in normal weight, "
        "all in the one textbox.",
        [
            (
                "set_textbox_runs",
                {
                    "textbox_name": "ReportTitle",
                    "runs": [
                        {"text": "Fleet Utilization: ", "font_weight": "Bold"},
                        {"text": "=Parameters!Site.Value", "font_weight": "Normal"},
                    ],
                },
            )
        ],
        _s08,
    ),
    Case(
        "s09",
        "style",
        "easy",
        "synth",
        "Centre-align the numbers in the Operating Hours, Idle Hours and Utilization columns of the table.",
        [
            (
                "set_textbox_style_bulk",
                {
                    "textbox_names": [
                        "FleetTable_OperatingHours",
                        "FleetTable_IdleHours",
                        "FleetTable_Utilization",
                    ],
                    "text_align": "Center",
                },
            )
        ],
        _s09,
    ),
]


# ---------------------------------------------------------------------------
# layout / page
# ---------------------------------------------------------------------------


def _l01(r: Rdl):
    w, h = inches(r.text("//r:Page/r:PageWidth")), inches(r.text("//r:Page/r:PageHeight"))
    return ok(w and h and w > h, f"page {w}x{h}in")


def _l02(r: Rdl):
    top = inches(r.text("//r:Body//r:Tablix[@Name='FleetTable']/r:Top"))
    return ok(top is not None and abs(top - 1.6) < 0.01, f"table top: {top}in")


def _l03(r: Rdl):
    vals = " ".join(r.texts("//r:PageFooter//r:TextRun/r:Value"))
    return all_of(
        ok("Globals!PageNumber" in vals and "Globals!TotalPages" in vals, f"footer text: {vals!r}"),
        no_double_encoding(r),
    )


def _l04(r: Rdl):
    vals = " ".join(r.texts("//r:PageHeader//r:TextRun/r:Value"))
    return all_of(
        ok(
            (r.text("//r:PageHeader/r:PrintOnFirstPage") or "").lower() == "false",
            "header still prints on page 1",
        ),
        ok("Parameters!DateFrom" in vals and "Parameters!DateTo" in vals, f"header text: {vals!r}"),
        no_double_encoding(r),
    )


def _l05(r: Rdl):
    page = {
        k: inches(r.text(f"//r:Page/r:{k}"))
        for k in (
            "PageWidth",
            "PageHeight",
            "LeftMargin",
            "RightMargin",
            "TopMargin",
            "BottomMargin",
        )
    }
    body_w = inches(r.text("//r:Body/r:Width")) or inches(r.text("//r:ReportSection/r:Width"))
    return all_of(
        ok(
            page["PageWidth"] == 16 and page["PageHeight"] == 12,
            f"page {page['PageWidth']}x{page['PageHeight']}",
        ),
        ok(
            all(
                abs((page[m] or 0) - 0.3) < 0.01
                for m in ("LeftMargin", "RightMargin", "TopMargin", "BottomMargin")
            ),
            "margins not 0.3in",
        ),
        ok(body_w is not None and 14 <= body_w <= 15.4, f"body width {body_w}in"),
    )


def _l07(r: Rdl):
    hidden = r.text("//r:Chart[@Name='HoursBySite']/r:Visibility/r:Hidden") or ""
    return all_of(
        ok(
            "Parameters!Site" in hidden and "Das Island" in hidden,
            f"chart hidden expression: {hidden!r}",
        ),
        no_double_encoding(r),
    )


def _l08(r: Rdl):
    imgs = r.x("//r:EmbeddedImages/r:EmbeddedImage")
    names = [i.get("Name") for i in imgs]
    hdr = r.x("//r:PageHeader//r:Image")
    hdr_ok = [
        i for i in hdr if r.text("./r:Source", i) == "Embedded" and r.text("./r:Value", i) in names
    ]
    return all_of(
        ok(imgs and r.text("./r:ImageData", imgs[0]), "logo not embedded"),
        ok(hdr_ok, "no header image pointing at the embedded logo"),
    )


LAYOUT = [
    Case(
        "l01",
        "layout",
        "easy",
        "synth",
        "Switch the report to landscape.",
        [("set_page_orientation", {"orientation": "Landscape"})],
        _l01,
    ),
    Case(
        "l02",
        "layout",
        "easy",
        "feedback",
        "Move the table down by an inch so there's breathing room under the title. Keep it at the left edge.",
        [("set_body_item_position", {"name": "FleetTable", "top": "1.6in", "left": "0in"})],
        _l02,
    ),
    Case(
        "l03",
        "layout",
        "medium",
        "feedback",
        "Add a page footer with 'Page X of Y'.",
        [
            ("set_page_footer", {"height": "0.4in"}),
            (
                "add_footer_textbox",
                {
                    "name": "PageNumberText",
                    "text": '="Page " & Globals!PageNumber & " of " & Globals!TotalPages',
                    "top": "0.05in",
                    "left": "2.5in",
                    "width": "1.5in",
                    "height": "0.25in",
                },
            ),
        ],
        _l03,
    ),
    Case(
        "l04",
        "layout",
        "hard",
        "feedback",
        "Add a page header showing the selected date range (DateFrom to DateTo). It should not print on the first page.",
        [
            ("set_page_header", {"height": "0.4in", "print_on_first_page": False}),
            (
                "add_header_textbox",
                {
                    "name": "DateRangeText",
                    "text": '="From " & Format(Parameters!DateFrom.Value) & " to " & Format(Parameters!DateTo.Value)',
                    "top": "0.05in",
                    "left": "0in",
                    "width": "4in",
                    "height": "0.25in",
                },
            ),
        ],
        _l04,
    ),
    Case(
        "l05",
        "layout",
        "hard",
        "feedback",
        "Make the page 16in wide by 12in tall with 0.3in margins all round, and widen the report body to use the full width.",
        [
            (
                "set_page_setup",
                {
                    "page_width": "16in",
                    "page_height": "12in",
                    "margin_top": "0.3in",
                    "margin_bottom": "0.3in",
                    "margin_left": "0.3in",
                    "margin_right": "0.3in",
                },
            ),
            ("set_body_size", {"width": "15.4in"}),
        ],
        _l05,
    ),
    Case(
        "l06",
        "layout",
        "easy",
        "synth",
        "Put a thin horizontal line under the report title.",
        [
            (
                "add_line",
                {
                    "name": "TitleRule",
                    "top": "0.45in",
                    "left": "0in",
                    "width": "6in",
                    "height": "0in",
                },
            )
        ],
        lambda r: ok(r.x("//r:Body//r:Line"), "no line in the body"),
    ),
    Case(
        "l07",
        "layout",
        "medium",
        "synth",
        "Hide the chart when the Site parameter is 'Das Island'.",
        [
            (
                "set_element_visibility",
                {
                    "element_name": "HoursBySite",
                    "hidden_expression": '=Parameters!Site.Value = "Das Island"',
                },
            )
        ],
        _l07,
    ),
    Case(
        "l08",
        "layout",
        "hard",
        "synth",
        "Add our logo to the top-right of a page header. The logo file is {LOGO_PATH}.",
        [
            (
                "add_embedded_image",
                {"name": "CompanyLogo", "mime_type": "image/png", "image_path": "{LOGO_PATH}"},
            ),
            ("set_page_header", {"height": "0.8in"}),
            (
                "add_header_image",
                {
                    "name": "HeaderLogo",
                    "image_source": "Embedded",
                    "value": "CompanyLogo",
                    "top": "0.05in",
                    "left": "5in",
                    "width": "1.4in",
                    "height": "0.6in",
                },
            ),
        ],
        _l08,
    ),
]


# ---------------------------------------------------------------------------
# chart
# ---------------------------------------------------------------------------


def _series(r: Rdl):
    return r.x("//r:Chart[@Name='HoursBySite']//r:ChartSeriesCollection/r:ChartSeries")


def _c01(r: Rdl):
    s = _series(r)
    return ok(
        s
        and all(
            r.text("./r:Type", x) == "Bar" and r.text("./r:Subtype", x) == "Stacked" for x in s
        ),
        f"series types: {[(r.text('./r:Type', x), r.text('./r:Subtype', x)) for x in s]}",
    )


def _c03(r: Rdl):
    vis = " ".join(r.texts("//r:Chart[@Name='HoursBySite']//r:ChartDataLabel/r:Visible"))
    return all_of(
        ok(">=" in vis and "100" in vis, f"data label visibility: {vis!r}"), no_double_encoding(r)
    )


def _c04(r: Rdl):
    cap = r.texts("//r:Chart[@Name='HoursBySite']/r:ChartTitles/r:ChartTitle/r:Caption")
    pos = r.text("//r:Chart[@Name='HoursBySite']/r:ChartLegends/r:ChartLegend/r:Position") or ""
    return all_of(
        ok("Operating Hours by Site" in cap, f"titles: {cap}"),
        ok(pos.startswith("Bottom"), f"legend position: {pos!r}"),
    )


def _c06(r: Rdl):
    ax = r.x("//r:Chart[@Name='HoursBySite']/r:ChartAreas//r:ChartValueAxes/r:ChartAxis")
    title = " ".join(r.texts(".//r:ChartAxisTitle/r:Caption", ax[0])) if ax else ""
    fmt = " ".join(r.texts(".//r:Style/r:Format", ax[0])) if ax else ""
    return all_of(
        ok("Hours" in title, f"value axis title: {title!r}"),
        ok(fmt in ("#,##0", "N0", "n0", "#,0"), f"axis format: {fmt!r}"),
    )


CHART = [
    Case(
        "c01",
        "chart",
        "medium",
        "feedback",
        "Make the chart a stacked bar chart.",
        [
            (
                "set_chart_series_type",
                {
                    "chart_name": "HoursBySite",
                    "series_name": "OperatingHours",
                    "series_type": "Bar",
                    "series_subtype": "Stacked",
                },
            )
        ],
        _c01,
    ),
    Case(
        "c02",
        "chart",
        "hard",
        "feedback",
        "Split the chart into one series per equipment type; the types aren't known in advance.",
        [
            (
                "set_chart_series_grouping",
                {
                    "chart_name": "HoursBySite",
                    "series_name": "OperatingHours",
                    "group_field": "EquipmentType",
                },
            )
        ],
        lambda r: ok(
            r.x(
                "//r:Chart[@Name='HoursBySite']/r:ChartSeriesHierarchy//r:Group/r:GroupExpressions/r:GroupExpression[contains(., 'EquipmentType')]"
            ),
            "no series group on EquipmentType",
        ),
    ),
    Case(
        "c03",
        "chart",
        "hard",
        "feedback",
        "Show data labels on the chart, but only when the value is at least 100 hours.",
        [
            (
                "set_chart_data_labels",
                {
                    "chart_name": "HoursBySite",
                    "visible_expression": "=IIf(Sum(Fields!OperatingHours.Value) >= 100, true, false)",
                },
            )
        ],
        _c03,
    ),
    Case(
        "c04",
        "chart",
        "easy",
        "synth",
        "Title the chart 'Operating Hours by Site' and move its legend to the bottom.",
        [
            ("set_chart_title", {"chart_name": "HoursBySite", "text": "Operating Hours by Site"}),
            ("set_chart_legend", {"chart_name": "HoursBySite", "position": "BottomCenter"}),
        ],
        _c04,
    ),
    Case(
        "c05",
        "chart",
        "easy",
        "synth",
        "Add idle hours to the chart as a second series.",
        [
            (
                "add_chart_series",
                {
                    "chart_name": "HoursBySite",
                    "series_name": "IdleHours",
                    "value_field": "IdleHours",
                },
            )
        ],
        lambda r: ok(
            len(_series(r)) == 2
            and "IdleHours"
            in " ".join(r.texts("//r:Chart[@Name='HoursBySite']//r:ChartDataPoint//r:Y")),
            "no second IdleHours series",
        ),
    ),
    Case(
        "c06",
        "chart",
        "medium",
        "synth",
        "Label the chart's value axis 'Hours' and format it with thousands separators, no decimals.",
        [
            (
                "set_chart_axis",
                {"chart_name": "HoursBySite", "axis": "Value", "title": "Hours", "format": "#,##0"},
            )
        ],
        _c06,
    ),
]


# ---------------------------------------------------------------------------
# interactivity
# ---------------------------------------------------------------------------


def _i01(r: Rdl):
    tb = r.textbox("FleetTable_AssetName")
    dt = r.x("./r:ActionInfo/r:Actions/r:Action/r:Drillthrough", tb) if tb is not None else []
    if not dt:
        return (False, "no drillthrough on the asset name cell")
    params = r.x("./r:Parameters/r:Parameter", dt[0])
    return all_of(
        ok(r.text("./r:ReportName", dt[0]) == "AssetDetail", "wrong target report"),
        ok(any("AssetID" in (r.text("./r:Value", p) or "") for p in params), "AssetID not passed"),
    )


INTERACTIVITY = [
    Case(
        "i01",
        "interactivity",
        "hard",
        "synth",
        "Clicking an asset name in the table should open the 'AssetDetail' report for that asset (pass its AssetID).",
        [
            (
                "set_textbox_action",
                {
                    "textbox_name": "FleetTable_AssetName",
                    "action_type": "Drillthrough",
                    "target_expression": "AssetDetail",
                    "drillthrough_parameters": [
                        {"name": "AssetID", "value": "=Fields!AssetID.Value"}
                    ],
                },
            )
        ],
        _i01,
    ),
    Case(
        "i02",
        "interactivity",
        "easy",
        "synth",
        "Show the asset's ID as a tooltip when hovering over its name in the table.",
        [
            (
                "set_textbox_tooltip",
                {
                    "textbox_name": "FleetTable_AssetName",
                    "text_or_expression": "=Fields!AssetID.Value",
                },
            )
        ],
        lambda r: ok(
            "AssetID" in (r.text("//r:Textbox[@Name='FleetTable_AssetName']/r:ToolTip") or ""),
            "no AssetID tooltip",
        ),
    ),
]


# ---------------------------------------------------------------------------
# read-only questions and out-of-scope requests
# ---------------------------------------------------------------------------

QUESTION = [
    Case(
        "q01",
        "question",
        "easy",
        "synth",
        "What parameters does this report take, and what data type is each one?",
        [("get_parameters", {})],
        must_not_edit=True,
        answer_must_mention=[["DateFrom"], ["DateTo"], ["Site"], ["String"]],
    ),
    Case(
        "q02",
        "question",
        "medium",
        "synth",
        "Which of this report's data sources are actually used by a dataset?",
        [("list_data_sources", {})],
        must_not_edit=True,
        answer_must_mention=[["FleetModel"]],
    ),
    Case(
        "q03",
        "question",
        "medium",
        "feedback",
        "Report Builder refuses to open this report. What's wrong with it? Don't change anything yet.",
        [("verify_report", {})],
        fixture=BAD_ACTION,
        must_not_edit=True,
        answer_must_mention=[
            ["Action"],
            ["ActionInfo", "not expected", "not allowed", "invalid", "schema"],
        ],
    ),
    Case(
        "q04",
        "question",
        "easy",
        "synth",
        "What DAX query does the dataset run?",
        [("get_datasets", {})],
        must_not_edit=True,
        answer_must_mention=[["SUMMARIZECOLUMNS"]],
    ),
]

DECLINE = [
    Case(
        "x01",
        "decline",
        "easy",
        "synth",
        "Publish this report to the Finance workspace in the Power BI service.",
        [],
        must_not_edit=True,
    ),
    Case(
        "x02",
        "decline",
        "easy",
        "synth",
        "Export page 1 of this report as a PDF for me.",
        [],
        must_not_edit=True,
    ),
    Case(
        "x03",
        "decline",
        "easy",
        "synth",
        "Set up an email subscription that sends this report every Monday at 8am.",
        [],
        must_not_edit=True,
    ),
]


CASES: list[Case] = (
    DATA + PARAMETERS + TABLIX + STYLE + LAYOUT + CHART + INTERACTIVITY + QUESTION + DECLINE
)
BY_ID = {c.id: c for c in CASES}
assert len(BY_ID) == len(CASES), "duplicate case ids"
