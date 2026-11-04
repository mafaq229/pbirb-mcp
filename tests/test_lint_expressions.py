"""Expression walker + dataset-scoped field checks (#46; #45 items 1-2).

Every element whose text starts with ``=`` is an expression
(:func:`pbirb_mcp.ops.expr_walk.iter_expressions`); field references are
resolved against the dataset in scope (``field-out-of-scope``) as well as
against the report's datasets (``missing-field-reference``).
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.xpath import RDL_NS
from pbirb_mcp.ops import expr_walk, vbexpr
from pbirb_mcp.ops.lint import ALL_RULES, lint_report
from pbirb_mcp.ops.validate import validate_report

FIXTURES = Path(__file__).parent / "fixtures"
MIN = (FIXTURES / "pbi_paginated_minimal.rdl").read_text(encoding="utf-8")
CHART = (FIXTURES / "pbi_chart_rich.rdl").read_text(encoding="utf-8")
N = f"{{{RDL_NS}}}"

V = "<Value>=Fields!Amount.Value</Value>"
ST = "<rd:DefaultName>Amount</rd:DefaultName>\n                          <Style />"
MV = (
    '<ReportParameter Name="Region"><DataType>String</DataType><Prompt>R</Prompt>'
    "<MultiValue>true</MultiValue></ReportParameter></ReportParameters>"
)
OTHER = (
    '<DataSet Name="Other"><Query><DataSourceName>PowerBIDataset</DataSourceName>'
    '<CommandText>X</CommandText></Query><Fields><Field Name="OtherOnly">'
    "<DataField>X</DataField></Field></Fields></DataSet></DataSets>"
)
TABLIX_DS = "<DataSetName>MainDataset</DataSetName>"


def _filter(op: str, value: str) -> str:
    return (
        f"{TABLIX_DS}<Filters><Filter><FilterExpression>=Fields!ProductName.Value"
        f"</FilterExpression><Operator>{op}</Operator><FilterValues><FilterValue>{value}"
        "</FilterValue></FilterValues></Filter></Filters>"
    )


def _body_textbox(value: str, name: str = "Loose") -> str:
    return (
        f'<Textbox Name="{name}"><Paragraphs><Paragraph><TextRuns><TextRun><Value>{value}'
        "</Value><Style /></TextRun></TextRuns><Style /></Paragraph></Paragraphs>"
        "<Top>3in</Top><Left>0in</Left><Height>0.25in</Height><Width>1in</Width>"
        "<Style /></Textbox></ReportItems>\n        <Height>2in</Height>"
    )


BODY_END = "</ReportItems>\n        <Height>2in</Height>"


def _write(tmp_path: Path, src: str, subs) -> Path:
    for old, new in subs:
        assert old in src, old
        src = src.replace(old, new, 1)
    p = tmp_path / "x.rdl"
    p.write_text(src, encoding="utf-8")
    return p


def _rules(path: Path) -> list[str]:
    return sorted({i["rule"] for i in lint_report(str(path))["issues"]})


def _issues(path: Path, rule: str) -> list[dict]:
    return [i for i in lint_report(str(path), rules=[rule])["issues"]]


# ---- the issue's repro ------------------------------------------------------

REPRO = {
    "field of another dataset": (
        MIN,
        [("</DataSets>", OTHER), (V, "<Value>=Fields!OtherOnly.Value</Value>")],
        "field-out-of-scope",
    ),
    "Fields!X.Label": (MIN, [(V, "<Value>=Fields!Nope.Label</Value>")], "missing-field-reference"),
    'Fields("X")': (MIN, [(V, '<Value>=Fields("Nope").Value</Value>')], "missing-field-reference"),
    "chart <Y>": (
        CHART,
        [("<Y>=Sum(Fields!Amount.Value)</Y>", "<Y>=Sum(Fields!Nope.Value)</Y>")],
        "missing-field-reference",
    ),
    "Style/Color": (
        MIN,
        [
            (
                ST,
                "<rd:DefaultName>Amount</rd:DefaultName><Style><Color>"
                '=IIF(Fields!Nope.Value &gt; 0,"Red","Black")</Color></Style>',
            )
        ],
        "missing-field-reference",
    ),
    "OverallPageNumber in body": (
        MIN,
        [(V, "<Value>=Globals!OverallPageNumber</Value>")],
        "page-number-out-of-chrome",
    ),
    "unterminated string": (
        MIN,
        [(V, '<Value>="abc &amp; Fields!Amount.Value</Value>')],
        "expression-syntax",
    ),
    "MV param, Filter Equal": (
        MIN,
        [("</ReportParameters>", MV), (TABLIX_DS, _filter("Equal", "=Parameters!Region.Value"))],
        "multi-value-eq",
    ),
}


@pytest.mark.parametrize("case", sorted(REPRO))
def test_repro_case_reports_expected_rule(tmp_path, case):
    src, subs, rule = REPRO[case]
    p = _write(tmp_path, src, subs)
    fired = _rules(p)
    assert rule in fired, fired
    # Nothing else of error severity rides along.
    errors = {i["rule"] for i in lint_report(str(p))["issues"] if i["severity"] == "error"}
    assert errors <= {rule}, errors


def test_repro_paren_in_string_is_clean(tmp_path):
    p = _write(tmp_path, MIN, [(V, '<Value>="Speed (km/h" &amp; Fields!Amount.Value</Value>')])
    assert _rules(p) == []


def test_repro_fixtures_stay_clean_of_errors():
    for f in sorted(FIXTURES.glob("**/*.rdl")):
        errors = [i for i in lint_report(str(f))["issues"] if i["severity"] == "error"]
        assert errors == [], (f.name, errors)


# ---- the walker -------------------------------------------------------------


def _root(xml: str) -> etree._Element:
    return etree.fromstring(
        f'<Report xmlns="{RDL_NS}" '
        'xmlns:rd="http://schemas.microsoft.com/SQLServer/reporting/reportdesigner">'
        f"{xml}</Report>"
    )


def _walked(root) -> list[str]:
    return [etree.QName(e).localname for e, _t in expr_walk.iter_expressions(root)]


@pytest.mark.parametrize(
    "tag",
    [
        "Y",
        "X",
        "Size",
        "Color",
        "BackgroundColor",
        "Format",
        "FilterValue",
        "Hyperlink",
        "DocumentMapLabel",
        "Bookmark",
        "Caption",
        "Visible",
        "Minimum",
        "Maximum",
        "Interval",
        "Value",
        "Hidden",
        "ToolTip",
    ],
)
def test_walker_yields_every_expression_element(tag):
    root = _root(f"<Wrap><{tag}>=Fields!A.Value</{tag}></Wrap>")
    assert _walked(root) == [tag]


@pytest.mark.parametrize("text", ["Red", "", "  ", "a = b"])
def test_walker_ignores_literals(text):
    assert _walked(_root(f"<Wrap><Color>{text}</Color></Wrap>")) == []


def test_walker_excludes_command_text_code_and_foreign_namespaces():
    root = _root(
        "<CommandText>=EVALUATE x</CommandText><Code>=Function</Code>"
        "<rd:DesignerState><rd:Statement>=x</rd:Statement></rd:DesignerState>"
        "<Value>  =1</Value>"
    )
    assert _walked(root) == ["Value"]


def test_walker_yields_leading_whitespace_expressions_with_text():
    root = _root("<Value>\n  =Fields!A.Value</Value>")
    [(elem, text)] = list(expr_walk.iter_expressions(root))
    assert text == "\n  =Fields!A.Value"
    assert expr_walk.expression_tokens(text)[0].text == "Fields"


# ---- dataset scope ------------------------------------------------------------


def _doc_root(src: str) -> etree._Element:
    return etree.fromstring(src.encode("utf-8"))


def test_dataset_scope_of_tablix_cell_chart_point_and_body():
    root = _doc_root(CHART)
    by_tag = {}
    for elem, _t in expr_walk.iter_expressions(root):
        by_tag.setdefault(etree.QName(elem).localname, elem)
    assert expr_walk.dataset_scope(by_tag["Value"]) == "MainDataset"
    assert expr_walk.dataset_scope(by_tag["Y"]) == "MainDataset"
    assert expr_walk.dataset_scope(by_tag["GroupExpression"]) == "MainDataset"
    body = root.find(f".//{N}Body")
    assert expr_walk.dataset_scope(body) is None


def test_dataset_scope_inside_a_dataset_is_that_dataset():
    root = _doc_root(MIN.replace("<DataField>Sales[Amount]</DataField>", "<Value>=1</Value>"))
    value = root.find(f".//{N}Field[@Name='Amount']/{N}Value")
    assert expr_walk.dataset_scope(value) == "MainDataset"


def test_scope_index_resolves_datasets_regions_and_groups():
    index = expr_walk.ScopeIndex(_doc_root(CHART))
    assert index.resolve("MainDataset") == "MainDataset"
    assert index.resolve("MainTable") == "MainDataset"
    assert index.resolve("SalesByProduct") == "MainDataset"
    assert index.resolve("Details") == "MainDataset"
    assert index.resolve("SalesByProduct_CategoryGroup") == "MainDataset"
    assert index.resolve("Nope") is None
    assert index.known("Details") and not index.known("Nope")
    assert index.all_fields == {"ProductID", "ProductName", "Amount"}


# ---- references + aggregate scope parsing -------------------------------------


def _sig(text: str):
    return expr_walk.expression_tokens(text)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("=Fields!A.Value", [("A", "Value")]),
        ("=Fields!A.Label & Fields!B.IsMissing", [("A", "Label"), ("B", "IsMissing")]),
        ('=Fields("A").Value', [("A", "Value")]),
        ('=Fields("A")', [("A", None)]),
        ('="Fields!A.Value" & Fields!B.Value', [("B", "Value")]),
        ("=Code.Fields!A.Value", []),
        ("=fields!a.value", [("a", "value")]),
    ],
)
def test_field_references(text, expected):
    refs = expr_walk.references(_sig(text), "Fields")
    assert [(r.name, r.member) for r in refs] == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ('=Sum(Fields!A.Value, "DS")', [("Sum", "DS")]),
        ("=Sum(Fields!A.Value)", [("Sum", None)]),
        ("=Count(Fields!A.Value, Nothing)", [("Count", None)]),
        ('=RunningValue(Fields!A.Value, Sum, "G")', [("RunningValue", "G")]),
        ('=CountRows("T")', [("CountRows", "T")]),
        ('=RowNumber("T") + InScope("G")', [("RowNumber", "T"), ("InScope", "G")]),
        (
            '=Lookup(Fields!A.Value, Fields!B.Value, Fields!C.Value, "DS2")',
            [("Lookup", "DS2")],
        ),
        ("=Math.Max(1, 2)", []),
        ('=Sum(Max(Fields!A.Value), "DS")', [("Sum", "DS"), ("Max", None)]),
    ],
)
def test_aggregate_calls(text, expected):
    calls = expr_walk.aggregate_calls(_sig(text))
    assert [(c.function, c.scope) for c in calls] == expected


def test_lookup_evaluates_destination_and_result_in_its_dataset():
    sig = _sig('=Lookup(Fields!A.Value, Fields!B.Value, Fields!C.Value, "DS2")')
    calls = expr_walk.aggregate_calls(sig)
    refs = {r.name: r for r in expr_walk.references(sig, "Fields")}
    assert expr_walk.enclosing_calls(calls, refs["A"].first) == []
    assert [c.scope for c in expr_walk.enclosing_calls(calls, refs["B"].first)] == ["DS2"]
    assert [c.scope for c in expr_walk.enclosing_calls(calls, refs["C"].first)] == ["DS2"]


def test_enclosing_calls_innermost_first():
    sig = _sig('=Sum(Max(Fields!A.Value), "DS")')
    calls = expr_walk.aggregate_calls(sig)
    [ref] = expr_walk.references(sig, "Fields")
    assert [c.function for c in expr_walk.enclosing_calls(calls, ref.first)] == ["Max", "Sum"]


def test_unclosed_call_does_not_raise():
    sig = _sig("=Sum(Fields!A.Value")
    [call] = expr_walk.aggregate_calls(sig)
    assert call.close_index == len(sig)
    assert vbexpr.first_unbalanced_delimiter(sig) is not None


# ---- field-out-of-scope -------------------------------------------------------


def test_field_out_of_scope_is_registered():
    assert "field-out-of-scope" in ALL_RULES


@pytest.mark.parametrize(
    "value, fires",
    [
        ("=Fields!Amount.Value", True),  # bare field outside any data region
        ("=First(Fields!Amount.Value)", False),  # one dataset: unscoped aggregate OK
        ('=First(Fields!Amount.Value, "MainDataset")', False),
        ('=Sum(Fields!Amount.Value, "MainTable")', False),  # data-region scope
        ('="Fields!Amount.Value"', False),  # string literal
        ("=Globals!ReportName", False),
    ],
)
def test_body_textbox_field_scope(tmp_path, value, fires):
    p = _write(tmp_path, MIN, [(BODY_END, _body_textbox(value))])
    issues = _issues(p, "field-out-of-scope")
    assert bool(issues) is fires, issues
    if fires:
        assert "Textbox[Name='Loose']" in issues[0]["location"]


def test_unscoped_aggregate_outside_region_with_two_datasets(tmp_path):
    p = _write(
        tmp_path,
        MIN,
        [("</DataSets>", OTHER), (BODY_END, _body_textbox("=First(Fields!Amount.Value)"))],
    )
    [issue] = _issues(p, "field-out-of-scope")
    assert "without a scope" in issue["message"]


def test_explicit_scope_of_the_wrong_dataset(tmp_path):
    p = _write(
        tmp_path,
        MIN,
        [("</DataSets>", OTHER), (V, '<Value>=First(Fields!Amount.Value, "Other")</Value>')],
    )
    [issue] = _issues(p, "field-out-of-scope")
    assert "'Other'" in issue["message"]


def test_explicit_scope_reaches_another_dataset(tmp_path):
    p = _write(
        tmp_path,
        MIN,
        [("</DataSets>", OTHER), (V, '<Value>=First(Fields!OtherOnly.Value, "Other")</Value>')],
    )
    assert _issues(p, "field-out-of-scope") == []


@pytest.mark.parametrize(
    "value, fires",
    [
        (
            '=Lookup(Fields!ProductID.Value, Fields!OtherOnly.Value, Fields!OtherOnly.Value, "Other")',
            False,
        ),
        (
            '=Lookup(Fields!OtherOnly.Value, Fields!OtherOnly.Value, Fields!OtherOnly.Value, "Other")',
            True,
        ),
        (
            '=Lookup(Fields!ProductID.Value, Fields!Amount.Value, Fields!OtherOnly.Value, "Other")',
            True,
        ),
    ],
)
def test_lookup_scopes(tmp_path, value, fires):
    p = _write(tmp_path, MIN, [("</DataSets>", OTHER), (V, f"<Value>{value}</Value>")])
    assert bool(_issues(p, "field-out-of-scope")) is fires


def test_field_out_of_scope_silent_when_field_is_missing_everywhere(tmp_path):
    p = _write(tmp_path, MIN, [(V, "<Value>=Fields!Nope.Value</Value>")])
    assert _issues(p, "field-out-of-scope") == []
    assert len(_issues(p, "missing-field-reference")) == 1


def test_calculated_field_resolves_in_its_own_dataset(tmp_path):
    p = _write(
        tmp_path,
        MIN,
        [("<DataField>Sales[Amount]</DataField>", "<Value>=Fields!ProductID.Value * 2</Value>")],
    )
    assert _issues(p, "field-out-of-scope") == []


def test_parameter_default_cannot_reference_a_field(tmp_path):
    p = _write(
        tmp_path,
        MIN,
        [
            (
                "<Prompt>Date To</Prompt>",
                "<DefaultValue><Values><Value>=Fields!Amount.Value</Value></Values>"
                "</DefaultValue><Prompt>Date To</Prompt>",
            )
        ],
    )
    assert len(_issues(p, "field-out-of-scope")) == 1


def test_chart_bound_to_dataset_without_its_fields(tmp_path):
    """#45 repro (items 1-2): the chart is rebound to a dataset that only
    has ProductID, and a series points at a field no dataset has."""
    root = _doc_root(CHART)
    ds = root.find(f".//{N}DataSet")
    other = copy.deepcopy(ds)
    other.set("Name", "Other")
    for f in other.findall(f"{N}Fields/{N}Field")[1:]:
        f.getparent().remove(f)
    ds.addnext(other)
    root.find(f".//{N}Chart/{N}DataSetName").text = "Other"
    root.find(f".//{N}ChartSeries[@Name='Total']//{N}Y").text = "=Sum(Fields!NoSuchField.Value)"
    p = tmp_path / "c.rdl"
    p.write_bytes(etree.tostring(root, xml_declaration=True, encoding="utf-8"))
    assert validate_report(str(p))["valid"]

    missing = _issues(p, "missing-field-reference")
    assert len(missing) == 1 and "NoSuchField" in missing[0]["message"]
    scoped = _issues(p, "field-out-of-scope")
    assert {i["location"].split(">")[-1] for i in scoped} >= {"Y", "GroupExpression", "Label"}
    assert all("'Other'" in i["message"] for i in scoped)
    assert not any("ProductID" in i["message"] for i in scoped)


# ---- page-number-out-of-chrome ------------------------------------------------


@pytest.mark.parametrize(
    "value, fires",
    [
        ("=Globals!PageNumber", True),
        ("=Globals!TotalPages", True),
        ("=Globals!OverallPageNumber", True),
        ("=Globals!OverallTotalPages", True),
        ("=Globals!PageName", True),
        ('="Page " &amp; Globals!PageNumber', True),
        ('="Globals!PageNumber"', False),
        ("=Globals!ReportName", False),
        ("=Globals!ExecutionTime", False),
    ],
)
def test_page_globals_in_body(tmp_path, value, fires):
    p = _write(tmp_path, MIN, [(V, f"<Value>{value}</Value>")])
    assert bool(_issues(p, "page-number-out-of-chrome")) is fires


def test_page_globals_in_header_are_fine(tmp_path):
    textbox = _body_textbox("=Globals!OverallPageNumber", "PageNo")
    textbox = textbox[: textbox.index("</ReportItems>")]
    header = f"<PageHeader><Height>0.5in</Height><ReportItems>{textbox}</ReportItems></PageHeader>"
    p = _write(tmp_path, MIN, [("<PageHeight>", header + "<PageHeight>")])
    assert validate_report(str(p))["valid"]
    assert _issues(p, "page-number-out-of-chrome") == []
    assert _issues(p, "field-out-of-scope") == []


# ---- expression-syntax on every expression element ----------------------------


def test_expression_syntax_checks_style_expressions(tmp_path):
    p = _write(
        tmp_path,
        MIN,
        [
            (
                ST,
                '<rd:DefaultName>Amount</rd:DefaultName><Style><Format>=IIF(1 &gt; 0, "N0"</Format></Style>',
            )
        ],
    )
    assert len(_issues(p, "expression-syntax")) == 1


# ---- multi-value-eq on filters ------------------------------------------------


@pytest.mark.parametrize(
    "op, value, fires",
    [
        ("Equal", "=Parameters!Region.Value", True),
        ("NotEqual", "=Parameters!Region.Value", True),
        ("Equal", "=(Parameters!Region.Value)", True),
        ("In", "=Parameters!Region.Value", False),
        ("Equal", "=Parameters!Region.Value(0)", False),
        ("Equal", '=Join(Parameters!Region.Value, ",")', False),
        ("Equal", "=Parameters!DateFrom.Value", False),  # single-value parameter
        ("Equal", "Parameters!Region.Value", False),  # a literal, not an expression
    ],
)
def test_multi_value_filter_operator(tmp_path, op, value, fires):
    p = _write(tmp_path, MIN, [("</ReportParameters>", MV), (TABLIX_DS, _filter(op, value))])
    issues = _issues(p, "multi-value-eq")
    assert bool(issues) is fires, issues
    if fires:
        assert "In" in issues[0]["suggestion"]
        assert issues[0]["location"].endswith("FilterValue")


def test_multi_value_dataset_filter(tmp_path):
    dataset_filter = (
        "</Fields><Filters><Filter><FilterExpression>=Fields!ProductName.Value"
        "</FilterExpression><Operator>Equal</Operator><FilterValues><FilterValue>"
        "=Parameters!Region.Value</FilterValue></FilterValues></Filter></Filters>"
    )
    p = _write(tmp_path, MIN, [("</ReportParameters>", MV), ("</Fields>", dataset_filter)])
    assert len(_issues(p, "multi-value-eq")) == 1


# ---- existing fixtures copy + full lint stays clean -------------------------


def test_minimal_fixture_copy_is_clean(tmp_path):
    dest = tmp_path / "m.rdl"
    shutil.copy(FIXTURES / "pbi_paginated_minimal.rdl", dest)
    assert lint_report(str(dest))["issues"] == []
