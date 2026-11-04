"""Real RDL ChartSeries Type/Subtype enums + compatibility (#40).

``set_chart_series_type`` and ``add_chart_series`` used invented lists:
they wrote non-RDL values (``Pie`` as a Type, ``SmoothLine`` / ``100`` /
``Spline`` as Subtypes) and refused real ones (``Shape``, ``Stepped``,
``Bubble``, ``Candlestick``, ``Radar``). The bundled XSD types both
elements as ``xsd:string``, so the output still "validated".
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import chart_types
from pbirb_mcp.core.xpath import RDL_NS, find_child
from pbirb_mcp.ops.chart import add_chart_series, set_chart_series_type
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_chart_rich.rdl"
CHART = "SalesByProduct"

ALL_PAIRS = [(t, st) for t, subs in chart_types.SUBTYPES_BY_TYPE.items() for st in subs]

_SERVER = MCPServer()
register_all_tools(_SERVER)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _series(path: Path, name: str) -> etree._Element:
    return etree.parse(str(path)).getroot().find(f".//{{{RDL_NS}}}ChartSeries[@Name='{name}']")


def _pair(path: Path, name: str) -> tuple:
    s = _series(path, name)
    t, st = find_child(s, "Type"), find_child(s, "Subtype")
    return (t.text if t is not None else None, st.text if st is not None else None)


# ---- the enum tables ---------------------------------------------------------------


def test_types_are_the_rdl_enum():
    assert set(chart_types.SERIES_TYPES) == {
        "Column",
        "Bar",
        "Line",
        "Shape",
        "Scatter",
        "Range",
        "Polar",
        "Area",
    }


def test_every_alias_resolves_to_a_valid_pair():
    for alias, (t, st) in chart_types.TYPE_ALIASES.items():
        assert alias not in chart_types.SERIES_TYPES
        assert st in chart_types.SUBTYPES_BY_TYPE[t], alias
    for alias, st in chart_types.SUBTYPE_ALIASES.items():
        assert alias not in chart_types.SERIES_SUBTYPES
        assert st in chart_types.SERIES_SUBTYPES, alias


# ---- every real pair is accepted and written verbatim ----------------------------


@pytest.mark.parametrize(("series_type", "subtype"), ALL_PAIRS)
def test_set_accepts_every_rdl_pair(path, series_type, subtype):
    result = set_chart_series_type(str(path), CHART, "Total", series_type, subtype)
    assert (result["type"], result["subtype"]) == (series_type, subtype)
    assert _pair(path, "Total") == (series_type, subtype)
    assert chart_types.series_type_problem(series_type, subtype) is None


@pytest.mark.parametrize(("series_type", "subtype"), ALL_PAIRS)
def test_add_accepts_every_rdl_pair(path, series_type, subtype):
    result = add_chart_series(str(path), CHART, "New", "Amount", series_type, subtype)
    assert (result["type"], result["subtype"]) == (series_type, subtype)
    assert _pair(path, "New") == (series_type, subtype)


# ---- every alias is normalised before writing -------------------------------------


@pytest.mark.parametrize(("alias", "canonical"), sorted(chart_types.TYPE_ALIASES.items()))
def test_set_normalises_type_alias(path, alias, canonical):
    result = set_chart_series_type(str(path), CHART, "Total", alias)
    assert (result["type"], result["subtype"]) == canonical
    assert _pair(path, "Total") == canonical


@pytest.mark.parametrize(("alias", "canonical"), sorted(chart_types.TYPE_ALIASES.items()))
def test_add_normalises_type_alias(path, alias, canonical):
    result = add_chart_series(str(path), CHART, "New", "Amount", alias)
    assert (result["type"], result["subtype"]) == canonical
    assert _pair(path, "New") == canonical


@pytest.mark.parametrize(
    ("series_type", "alias", "canonical"),
    [
        ("Line", "SmoothLine", "Smooth"),
        ("Line", "Spline", "Smooth"),
        ("Area", "Spline", "Smooth"),
        ("Column", "100", "PercentStacked"),
        ("Bar", "100", "PercentStacked"),
        ("Area", "100", "PercentStacked"),
    ],
)
def test_subtype_alias_normalised(path, series_type, alias, canonical):
    result = set_chart_series_type(str(path), CHART, "Total", series_type, alias)
    assert _pair(path, "Total") == (series_type, canonical)
    assert result["subtype"] == canonical
    add_chart_series(str(path), CHART, "New", "Amount", series_type, alias)
    assert _pair(path, "New") == (series_type, canonical)


@pytest.mark.parametrize(
    ("series_type", "canonical"),
    [("Pie", ("Shape", "ExplodedPie")), ("Doughnut", ("Shape", "ExplodedDoughnut"))],
)
def test_exploded_qualifies_pie_and_doughnut(path, series_type, canonical):
    set_chart_series_type(str(path), CHART, "Total", series_type, "Exploded")
    assert _pair(path, "Total") == canonical


def test_issue_examples(path):
    # (Shape, Pie) and (Line, Stepped) succeed; (Pie, None) aliases.
    set_chart_series_type(str(path), CHART, "Total", "Shape", "Pie")
    assert _pair(path, "Total") == ("Shape", "Pie")
    set_chart_series_type(str(path), CHART, "Total", "Line", "Stepped")
    assert _pair(path, "Total") == ("Line", "Stepped")
    set_chart_series_type(str(path), CHART, "Total", "Pie")
    assert _pair(path, "Total") == ("Shape", "Pie")
    assert validate_report(str(path))["valid"]


# ---- non-RDL values and incompatible pairs are refused -----------------------------


@pytest.mark.parametrize(
    ("series_type", "subtype", "match"),
    [
        ("NotReal", None, "not an RDL ChartSeries Type"),
        ("pie", None, "not an RDL ChartSeries Type"),  # case-sensitive enum
        ("Column", "Line", "not valid for Type 'Column'"),
        ("Column", "Smooth", "not valid for Type 'Column'"),
        ("Line", "Stacked", "not valid for Type 'Line'"),
        ("Shape", "Plain", "not valid for Type 'Shape'"),
        ("Shape", "Exploded", "not valid for Type 'Shape'"),
        ("Scatter", "Radar", "subtype of \\['Polar'\\]"),
        ("Polar", "Stock", "not valid for Type 'Polar'"),
        ("Pie", "Doughnut", "shorthand for Shape/Pie"),
        ("Bubble", "Plain", "shorthand for Scatter/Bubble"),
        ("Column", "CalloutPie", "not valid"),
    ],
)
def test_invalid_pairs_refused_without_writing(path, series_type, subtype, match):
    before = path.read_bytes()
    with pytest.raises(ValueError, match=match):
        set_chart_series_type(str(path), CHART, "Total", series_type, subtype)
    with pytest.raises(ValueError, match=match):
        add_chart_series(str(path), CHART, "New", "Amount", series_type, subtype)
    assert path.read_bytes() == before


_OLD_INVENTED_TYPES = [
    "Pie",
    "Doughnut",
    "Funnel",
    "Pyramid",
    "Bubble",
    "Stock",
    "Radar",
]
_OLD_INVENTED_SUBTYPES = ["SmoothLine", "100", "Spline", "Exploded", "Line"]


@pytest.mark.parametrize(
    "series_type", list(chart_types.SERIES_TYPES) + _OLD_INVENTED_TYPES + ["Nope"]
)
@pytest.mark.parametrize(
    "subtype", [None] + sorted(chart_types.SERIES_SUBTYPES) + _OLD_INVENTED_SUBTYPES
)
def test_no_non_rdl_string_is_ever_written(path, series_type, subtype):
    try:
        set_chart_series_type(str(path), CHART, "Total", series_type, subtype)
    except ValueError:
        return
    t, st = _pair(path, "Total")
    assert chart_types.series_type_problem(t, st) is None, (t, st)


# ---- type-only changes keep or reset the stored subtype -----------------------------


def test_stacked_to_shape_resets_subtype(path):
    # Total is Bar/Stacked in the fixture.
    result = set_chart_series_type(str(path), CHART, "Total", "Shape")
    assert result["changed"] == ["Type", "Subtype"]
    assert (result["type"], result["subtype"]) == ("Shape", "Pie")
    assert _pair(path, "Total") == ("Shape", "Pie")


def test_shape_to_line_resets_subtype(path):
    set_chart_series_type(str(path), CHART, "Total", "Shape", "Funnel")
    result = set_chart_series_type(str(path), CHART, "Total", "Line")
    assert result["changed"] == ["Type", "Subtype"]
    assert _pair(path, "Total") == ("Line", "Plain")


@pytest.mark.parametrize("new_type", ["Column", "Area"])
def test_compatible_subtype_preserved(path, new_type):
    result = set_chart_series_type(str(path), CHART, "Total", new_type)
    assert result["changed"] == ["Type"]
    assert _pair(path, "Total") == (new_type, "Stacked")


def test_no_op_reports_canonical_pair(path):
    result = set_chart_series_type(str(path), CHART, "Total", "Bar")
    assert result["changed"] == []
    assert (result["type"], result["subtype"]) == ("Bar", "Stacked")


def test_legacy_subtype_on_disk_is_canonicalised(path):
    tree = etree.parse(str(path))
    s = tree.getroot().find(f".//{{{RDL_NS}}}ChartSeries[@Name='Quantity']")
    find_child(s, "Subtype").text = "SmoothLine"  # written by pre-#40 versions
    tree.write(str(path), xml_declaration=True, encoding="utf-8")
    result = set_chart_series_type(str(path), CHART, "Quantity", "Line")
    assert result["changed"] == ["Subtype"]
    assert _pair(path, "Quantity") == ("Line", "Smooth")


def test_missing_subtype_element_written_after_type_when_needed(path):
    tree = etree.parse(str(path))
    s = tree.getroot().find(f".//{{{RDL_NS}}}ChartSeries[@Name='Quantity']")
    s.remove(find_child(s, "Subtype"))
    tree.write(str(path), xml_declaration=True, encoding="utf-8")
    # Absent Subtype = Plain, which suits Column: nothing is written.
    assert set_chart_series_type(str(path), CHART, "Quantity", "Column")["changed"] == ["Type"]
    assert _pair(path, "Quantity") == ("Column", None)
    # ... but not Shape: the default Pie lands right after <Type>.
    result = set_chart_series_type(str(path), CHART, "Quantity", "Shape")
    assert result["changed"] == ["Type", "Subtype"]
    s = _series(path, "Quantity")
    locals_ = [etree.QName(c).localname for c in s]
    assert locals_.index("Subtype") == locals_.index("Type") + 1
    assert find_child(s, "Subtype").text == "Pie"


def test_add_default_subtype_follows_type(path):
    assert add_chart_series(str(path), CHART, "A", "Amount")["subtype"] == "Plain"
    assert add_chart_series(str(path), CHART, "B", "Amount", "Shape")["subtype"] == "Pie"
    assert _pair(path, "B") == ("Shape", "Pie")


# ---- series_type_problem (for lint) ---------------------------------------------------


@pytest.mark.parametrize(
    ("t", "st", "ok"),
    [
        ("Column", "Plain", True),
        (None, None, True),
        ("Shape", None, True),
        ("Pie", "Stacked", False),
        ("Line", "SmoothLine", False),
        ("Column", "100", False),
        ("Shape", "Stacked", False),
        (" Line ", " Stepped ", True),
    ],
)
def test_series_type_problem(t, st, ok):
    assert (chart_types.series_type_problem(t, st) is None) is ok


def test_series_type_problem_suggests_canonical_pair():
    assert "Type=Shape, Subtype=Pie" in chart_types.series_type_problem("Pie", None)


# ---- tool surface -----------------------------------------------------------------------


def _tool(name: str) -> dict:
    listing = _SERVER.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return next(t for t in listing["result"]["tools"] if t["name"] == name)


def test_insert_description_points_to_set_chart_series_type():
    desc = _tool("insert_chart_from_template")["description"]
    assert "set_chart_series_type" in desc
    assert "Shape/Pie" in desc
    assert "editing the chart directly" not in desc


@pytest.mark.parametrize("tool", ["add_chart_series", "set_chart_series_type"])
def test_schema_descriptions_list_real_enums(tool):
    props = _tool(tool)["inputSchema"]["properties"]
    type_doc = props["series_type"]["description"]
    subtype_doc = props["series_subtype"]["description"]
    for t in chart_types.SERIES_TYPES:
        assert t in type_doc
    for st in chart_types.SERIES_SUBTYPES:
        assert st in subtype_doc


def test_server_call_with_alias_and_null_subtype(path):
    resp = _SERVER.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_chart_series",
                "arguments": {
                    "path": str(path),
                    "chart_name": CHART,
                    "series_name": "New",
                    "value_field": "Amount",
                    "series_type": "Bubble",
                    "series_subtype": None,
                },
            },
        }
    )
    result = resp["result"]
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert (payload["type"], payload["subtype"]) == ("Scatter", "Bubble")
    assert _pair(path, "New") == ("Scatter", "Bubble")
