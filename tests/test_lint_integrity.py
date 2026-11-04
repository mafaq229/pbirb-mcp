"""Reference-integrity lint rules (#47).

``pbirb_mcp/ops/lint_rules/references.py`` plus the ``Style/BackgroundImage``
extension of ``dangling-embedded-image``. Duplicate names /
invalid identifiers (#20) and ToggleItem targets (#42) have their own
tests; ``tablix-structure`` belongs to the tablix-structure lint issue.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.ops import parameters, tablix, tablix_columns, tablix_subtotals
from pbirb_mcp.ops.lint import ALL_RULES, lint_report
from pbirb_mcp.ops.lint_rules.references import RULES as REFERENCE_RULES
from pbirb_mcp.ops.validate import validate_report

FIXTURES = Path(__file__).parent / "fixtures"
MIN_PATH = FIXTURES / "pbi_paginated_minimal.rdl"
MIN = MIN_PATH.read_text(encoding="utf-8")

V = "<Value>=Fields!Amount.Value</Value>"
ST = "<rd:DefaultName>Amount</rd:DefaultName>\n                          <Style />"
TABLIX_DS = "<DataSetName>MainDataset</DataSetName>"
DATE_FROM_PROMPT = "<Prompt>Date From</Prompt>"
DATE_TO_PROMPT = "<Prompt>Date To</Prompt>"
NEW_RULES = sorted(REFERENCE_RULES)


def _action(inner: str) -> str:
    return (
        "<rd:DefaultName>Amount</rd:DefaultName><ActionInfo><Actions><Action>"
        f"{inner}</Action></Actions></ActionInfo><Style />"
    )


def _background(source: str, value: str) -> str:
    return (
        "<rd:DefaultName>Amount</rd:DefaultName><Style><BackgroundImage>"
        f"<Source>{source}</Source><Value>{value}</Value></BackgroundImage></Style>"
    )


def _default(value: str) -> str:
    return f"<DefaultValue><Values><Value>{value}</Value></Values></DefaultValue>"


def _write(tmp_path: Path, subs, src: str = MIN) -> Path:
    for old, new in subs:
        assert old in src, old
        src = src.replace(old, new, 1)
    p = tmp_path / "x.rdl"
    p.write_text(src, encoding="utf-8")
    return p


def _rules(path: Path) -> set[str]:
    return {i["rule"] for i in lint_report(str(path))["issues"]}


def _issues(path: Path, rule: str) -> list[dict]:
    return lint_report(str(path), rules=[rule])["issues"]


# ---- the issue's repro ------------------------------------------------------

REPRO = {
    "undefined Parameters!": (
        [(V, "<Value>=Parameters!Nope.Value</Value>")],
        "undefined-parameter",
    ),
    "undefined ReportItems!": (
        [(V, "<Value>=ReportItems!Nope.Value</Value>")],
        "undefined-report-item",
    ),
    "aggregate scope unknown": (
        [(V, '<Value>=Sum(Fields!Amount.Value, "NoSuchDS")</Value>')],
        "unknown-aggregate-scope",
    ),
    "tablix DataSetName unknown": (
        [(TABLIX_DS, "<DataSetName>NoSuchDS</DataSetName>")],
        "dangling-dataset-reference",
    ),
    "Query w/o DataSourceName": (
        [("<DataSourceName>PowerBIDataset</DataSourceName>", "")],
        "missing-data-source-name",
    ),
    "param forward dependency": (
        [(DATE_FROM_PROMPT, _default("=Parameters!DateTo.Value") + DATE_FROM_PROMPT)],
        "parameter-forward-dependency",
    ),
    "dangling BackgroundImage": (
        [(ST, _background("Embedded", "Nope"))],
        "dangling-embedded-image",
    ),
    "dangling BookmarkLink": (
        [(ST, _action("<BookmarkLink>Nope</BookmarkLink>"))],
        "dangling-bookmark-link",
    ),
    "empty Hyperlink": ([(ST, _action("<Hyperlink></Hyperlink>"))], "empty-hyperlink"),
    # Owned by #20 / #42 — already caught; kept here as regression guards.
    "duplicate textbox names": (
        [('<Textbox Name="ProductName">', '<Textbox Name="ProductID">')],
        "duplicate-name",
    ),
    "dangling ToggleItem": (
        [
            (
                '<Group Name="Details" />',
                '<Group Name="Details" /><Visibility><Hidden>true</Hidden>'
                "<ToggleItem>Nope</ToggleItem></Visibility>",
            )
        ],
        "dangling-toggle-item",
    ),
}


@pytest.mark.parametrize("case", sorted(REPRO))
def test_repro_case_reports_its_rule(tmp_path, case):
    subs, rule = REPRO[case]
    p = _write(tmp_path, subs)
    assert validate_report(str(p))["valid"]  # the XSD accepts every one
    assert rule in _rules(p)


@pytest.mark.parametrize("case", sorted(REPRO))
def test_repro_case_has_a_located_issue_with_a_suggestion(tmp_path, case):
    subs, rule = REPRO[case]
    [issue, *_] = _issues(_write(tmp_path, subs), rule)
    assert issue["location"] and issue["location"] != "(unnamed)"
    assert issue.get("suggestion")


def test_new_rules_are_registered():
    assert set(NEW_RULES) <= set(ALL_RULES)


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURES.glob("**/*.rdl")))
def test_no_false_positives_on_fixtures(fixture):
    path = next(FIXTURES.glob(f"**/{fixture}"))
    hits = [
        i for i in lint_report(str(path), rules=NEW_RULES + ["dangling-embedded-image"])["issues"]
    ]
    assert hits == []


# ---- undefined-parameter / undefined-report-item ------------------------------


@pytest.mark.parametrize(
    "value, fires",
    [
        ("=Parameters!DateFrom.Value", False),
        ("=Parameters!DateFrom.Label", False),
        ('=Parameters("DateTo").Value', False),
        ('=Parameters("Nope").Value', True),
        ("=Parameters!datefrom.Value", True),  # case-sensitive
        ('="Parameters!Nope.Value"', False),  # string literal
        ("=Code.Parameters!Nope", False),
    ],
)
def test_undefined_parameter(tmp_path, value, fires):
    p = _write(tmp_path, [(V, f"<Value>{value}</Value>")])
    assert bool(_issues(p, "undefined-parameter")) is fires


def test_undefined_parameter_in_style_and_query_parameter(tmp_path):
    p = _write(
        tmp_path,
        [
            (
                ST,
                "<rd:DefaultName>Amount</rd:DefaultName><Style><Color>"
                '=IIF(Parameters!Gone.Value, "Red", "Black")</Color></Style>',
            ),
            (
                "<CommandText>",
                '<QueryParameters><QueryParameter Name="P"><Value>=Parameters!Gone2.Value'
                "</Value></QueryParameter></QueryParameters><CommandText>",
            ),
        ],
    )
    msgs = " ".join(i["message"] for i in _issues(p, "undefined-parameter"))
    assert "'Gone'" in msgs and "'Gone2'" in msgs


@pytest.mark.parametrize(
    "value, fires",
    [
        ("=ReportItems!ProductName.Value", False),
        ('=ReportItems("Amount").Value', False),
        ("=ReportItems!Nope.Value", True),
        ("=ReportItems!MainTable.Value", False),  # any report item name resolves
    ],
)
def test_undefined_report_item(tmp_path, value, fires):
    p = _write(tmp_path, [(V, f"<Value>{value}</Value>")])
    assert bool(_issues(p, "undefined-report-item")) is fires


# ---- unknown-aggregate-scope ------------------------------------------------------


@pytest.mark.parametrize(
    "value, fires",
    [
        ('=Sum(Fields!Amount.Value, "MainDataset")', False),
        ('=Sum(Fields!Amount.Value, "MainTable")', False),  # data region
        ('=Sum(Fields!Amount.Value, "Details")', False),  # group
        ("=Sum(Fields!Amount.Value)", False),
        ("=Sum(Fields!Amount.Value, Nothing)", False),
        ('=Sum(Fields!Amount.Value, "maindataset")', True),  # case-sensitive
        ('=RunningValue(Fields!Amount.Value, Sum, "Nope")', True),
        ('=RowNumber("Nope")', True),
        ('=CountRows("MainTable")', False),
        ('=IIF(InScope("Nope"), 1, 0)', True),
        (
            '=Lookup(Fields!ProductID.Value, Fields!ProductID.Value, Fields!Amount.Value, "MainDataset")',
            False,
        ),
        # Lookup's last argument must be a dataset, not a region.
        (
            '=Lookup(Fields!ProductID.Value, Fields!ProductID.Value, Fields!Amount.Value, "MainTable")',
            True,
        ),
        ('="Sum(x, ""Nope"")"', False),  # string literal
    ],
)
def test_unknown_aggregate_scope(tmp_path, value, fires):
    p = _write(tmp_path, [(V, f"<Value>{value}</Value>")])
    assert bool(_issues(p, "unknown-aggregate-scope")) is fires


# ---- dangling-dataset-reference / missing-data-source-name --------------------------


def test_parameter_dataset_reference_is_checked(tmp_path):
    p = _write(
        tmp_path,
        [
            (
                DATE_TO_PROMPT,
                "<ValidValues><DataSetReference><DataSetName>Gone</DataSetName>"
                "<ValueField>ProductID</ValueField></DataSetReference></ValidValues>"
                + DATE_TO_PROMPT,
            )
        ],
    )
    [issue] = _issues(p, "dangling-dataset-reference")
    assert "ReportParameter[Name='DateTo']" in issue["location"]
    assert "'Gone'" in issue["message"]


def test_empty_dataset_name_is_dangling(tmp_path):
    p = _write(tmp_path, [(TABLIX_DS, "<DataSetName></DataSetName>")])
    [issue] = _issues(p, "dangling-dataset-reference")
    assert "empty" in issue["message"]


def test_empty_data_source_name_is_missing(tmp_path):
    p = _write(
        tmp_path,
        [("<DataSourceName>PowerBIDataset</DataSourceName>", "<DataSourceName> </DataSourceName>")],
    )
    [issue] = _issues(p, "missing-data-source-name")
    assert issue["location"] == "DataSet[Name='MainDataset']/Query"


# ---- parameter-forward-dependency -------------------------------------------------


def test_backward_dependency_is_fine(tmp_path):
    p = _write(
        tmp_path, [(DATE_TO_PROMPT, _default("=Parameters!DateFrom.Value") + DATE_TO_PROMPT)]
    )
    assert _issues(p, "parameter-forward-dependency") == []


def test_self_dependency(tmp_path):
    p = _write(tmp_path, [(DATE_TO_PROMPT, _default("=Parameters!DateTo.Value") + DATE_TO_PROMPT)])
    [issue] = _issues(p, "parameter-forward-dependency")
    assert "itself" in issue["message"]


def test_forward_dependency_via_valid_values_dataset_query(tmp_path):
    """DateFrom's available values come from a dataset whose query
    parameter reads DateTo — declared later."""
    lookup_ds = (
        '<DataSet Name="Dates"><Query><DataSourceName>PowerBIDataset</DataSourceName>'
        '<QueryParameters><QueryParameter Name="To"><Value>=Parameters!DateTo.Value</Value>'
        "</QueryParameter></QueryParameters><CommandText>EVALUATE 'Dates'</CommandText></Query>"
        '<Fields><Field Name="D"><DataField>Dates[D]</DataField></Field></Fields></DataSet>'
        "</DataSets>"
    )
    valid = (
        "<ValidValues><DataSetReference><DataSetName>Dates</DataSetName>"
        "<ValueField>D</ValueField></DataSetReference></ValidValues>"
    )
    p = _write(tmp_path, [("</DataSets>", lookup_ds), (DATE_FROM_PROMPT, valid + DATE_FROM_PROMPT)])
    [issue] = _issues(p, "parameter-forward-dependency")
    assert "'Dates'" in issue["message"] and "'DateTo'" in issue["message"]
    assert "reorder_parameters" in issue["suggestion"]


def test_forward_dependency_on_undefined_parameter_is_not_double_reported(tmp_path):
    p = _write(
        tmp_path, [(DATE_FROM_PROMPT, _default("=Parameters!Nope.Value") + DATE_FROM_PROMPT)]
    )
    assert _issues(p, "parameter-forward-dependency") == []
    assert len(_issues(p, "undefined-parameter")) == 1


# ---- bookmarks / hyperlinks / background images -------------------------------------


def test_bookmark_link_to_existing_bookmark(tmp_path):
    p = _write(
        tmp_path,
        [
            (ST, _action("<BookmarkLink>Top</BookmarkLink>")),
            (TABLIX_DS, TABLIX_DS + "<Bookmark>Top</Bookmark>"),
        ],
    )
    assert _issues(p, "dangling-bookmark-link") == []


@pytest.mark.parametrize(
    "subs",
    [
        # Expression bookmark: could be any target.
        [
            (ST, _action("<BookmarkLink>Nope</BookmarkLink>")),
            (TABLIX_DS, TABLIX_DS + "<Bookmark>=Fields!ProductID.Value</Bookmark>"),
        ],
        # Expression link.
        [(ST, _action("<BookmarkLink>=Fields!ProductID.Value</BookmarkLink>"))],
    ],
)
def test_bookmark_link_expressions_are_not_flagged(tmp_path, subs):
    assert _issues(_write(tmp_path, subs), "dangling-bookmark-link") == []


@pytest.mark.parametrize(
    "url, fires",
    [("", True), ("   ", True), ("https://x", False), ("=Fields!ProductName.Value", False)],
)
def test_empty_hyperlink(tmp_path, url, fires):
    p = _write(tmp_path, [(ST, _action(f"<Hyperlink>{url}</Hyperlink>"))])
    assert bool(_issues(p, "empty-hyperlink")) is fires


@pytest.mark.parametrize(
    "source, value, fires",
    [
        ("Embedded", "Nope", True),
        ("Embedded", "Logo", False),
        ("Embedded", "=Fields!ProductName.Value", False),
        ("External", "https://x/y.png", False),
    ],
)
def test_background_image(tmp_path, source, value, fires):
    logo = (
        '<EmbeddedImages><EmbeddedImage Name="Logo"><MIMEType>image/png</MIMEType>'
        "<ImageData>iVBORw0KGgo=</ImageData></EmbeddedImage></EmbeddedImages>"
        "<rd:ReportUnitType>"
    )
    p = _write(tmp_path, [(ST, _background(source, value)), ("<rd:ReportUnitType>", logo)])
    issues = _issues(p, "dangling-embedded-image")
    assert bool(issues) is fires
    if fires:
        assert "Textbox[Name='Amount']" in issues[0]["location"]
        assert "BackgroundImage" in issues[0]["message"]


# ---- tool happy paths stay clean -----------------------------------------------------


def test_tablix_and_parameter_tools_leave_reference_rules_clean(tmp_path):
    p = tmp_path / "r.rdl"
    shutil.copy(MIN_PATH, p)
    path = str(p)
    tablix_columns.add_tablix_column(path, "MainTable", "Extra", "=Fields!ProductID.Value")
    tablix_columns.remove_tablix_column(path, "MainTable", "Extra")
    tablix.add_row_group(path, "MainTable", "ByName", "=Fields!ProductName.Value")
    tablix_subtotals.add_subtotal_row(
        path,
        "MainTable",
        "ByName",
        [{"column": "Amount", "expression": "=Sum(Fields!Amount.Value)"}],
    )
    parameters.add_parameter(path, "Region", "String", prompt="Region", multi_value=True)
    parameters.set_parameter_default_values(path, "Region", "static", static_values=["A"])
    parameters.rename_parameter(path, "Region", "Area")
    result = lint_report(path, rules=NEW_RULES + ["field-out-of-scope", "missing-field-reference"])
    assert result["issues"] == []
    assert validate_report(path)["valid"]
