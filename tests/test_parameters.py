"""Parameter state validation at write time (#93).

The parameter writers refuse combinations Report Builder rejects (or that
fail every run) and warn about a hidden parameter nobody can fill; the
``parameter-state`` lint rule reports the same problems in legacy files.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.ops import parameters as P
from pbirb_mcp.ops.lint import ALL_RULES, lint_report
from pbirb_mcp.ops.validate import validate_report

FIXTURE_MULTI = Path(__file__).parent / "fixtures" / "pbi_multi_datasource.rdl"


@pytest.fixture
def path(tmp_path: Path) -> str:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE_MULTI, dest)
    return str(dest)


def _plant(path: str, old: str, new: str) -> None:
    """Hand-edit the file (a legacy state the writers now refuse)."""
    text = Path(path).read_text(encoding="utf-8")
    assert old in text
    Path(path).write_text(text.replace(old, new, 1), encoding="utf-8")


def _state_issues(path: str) -> list[dict]:
    return lint_report(path, rules=["parameter-state"])["issues"]


def _refused(path: str, fn, *args, match: str, **kwargs) -> None:
    before = Path(path).read_bytes()
    with pytest.raises(ValueError, match=match):
        fn(path, *args, **kwargs)
    assert Path(path).read_bytes() == before


class TestMultiValueNullable:
    def test_update_advanced_refuses(self, path):
        P.add_parameter(path, "Region", "String", prompt="Region", multi_value=True)
        _refused(path, P.update_parameter_advanced, "Region", allow_null=True, match="Nullable")

    def test_add_parameter_refuses(self, path):
        _refused(
            path,
            P.add_parameter,
            "Region",
            "String",
            multi_value=True,
            allow_null=True,
            match="MultiValue and Nullable",
        )

    def test_turning_multi_value_on_a_nullable_param_refuses(self, path):
        P.add_parameter(path, "Region", "String", prompt="Region", allow_null=True)
        _refused(path, P.update_parameter_advanced, "Region", multi_value=True, match="Nullable")

    def test_switching_both_in_one_call_is_fine(self, path):
        P.add_parameter(path, "Region", "String", prompt="Region", allow_null=True)
        out = P.update_parameter_advanced(path, "Region", multi_value=True, allow_null=False)
        assert out["changed"] == ["MultiValue", "Nullable"]


class TestMultiValueBoolean:
    def test_add_parameter_refuses(self, path):
        _refused(path, P.add_parameter, "Flag", "Boolean", multi_value=True, match="Boolean")

    def test_retype_multi_value_to_boolean_refuses(self, path):
        P.add_parameter(path, "Region", "String", prompt="Region", multi_value=True)
        _refused(path, P.set_parameter_type, "Region", "Boolean", match="multi-value Boolean")

    def test_single_value_boolean_is_fine(self, path):
        assert "warnings" not in P.add_parameter(path, "Flag", "Boolean", prompt="Flag")


class TestDefaultCount:
    def test_two_defaults_on_single_value_refuses(self, path):
        _refused(
            path,
            P.set_parameter_default_values,
            "DateTo",
            "static",
            ["2026-01-01", "2026-02-01"],
            match="2 default values but isn't MultiValue",
        )

    def test_two_defaults_on_multi_value_is_fine(self, path):
        P.update_parameter_advanced(path, "DateTo", multi_value=True)
        out = P.set_parameter_default_values(path, "DateTo", "static", ["2026-01-01", "2026-02-01"])
        assert out["changed"] is True

    def test_turning_multi_value_off_with_two_defaults_refuses(self, path):
        P.update_parameter_advanced(path, "DateTo", multi_value=True)
        P.set_parameter_default_values(path, "DateTo", "static", ["2026-01-01", "2026-02-01"])
        _refused(path, P.update_parameter_advanced, "DateTo", multi_value=False, match="2 default")


class TestLiteralTypes:
    def test_datetime_default_not_a_date_refuses(self, path):
        _refused(
            path,
            P.set_parameter_default_values,
            "DateFrom",
            "static",
            ["not a date"],
            match="incompatible with type 'DateTime'",
        )

    def test_expression_default_is_fine(self, path):
        out = P.set_parameter_default_values(path, "DateFrom", "static", ["=Today()"])
        assert out["changed"] is True

    def test_static_valid_values_incompatible_refuses(self, path):
        _refused(
            path,
            P.set_parameter_available_values,
            "DateFrom",
            "static",
            static_values=[{"value": "soon", "label": "Soon"}],
            match="static valid value",
        )

    def test_retype_with_incompatible_valid_values_refuses(self, path):
        P.add_parameter(path, "Slot", "String", prompt="Slot")
        P.set_parameter_available_values(path, "Slot", "static", static_values=["09:00", "12:00"])
        _refused(
            path,
            P.set_parameter_type,
            "Slot",
            "Integer",
            match=r"static valid value\(s\) \['09:00', '12:00'\] incompatible",
        )

    def test_retype_with_compatible_valid_values_is_fine(self, path):
        P.add_parameter(path, "Slot", "String", prompt="Slot")
        P.set_parameter_available_values(path, "Slot", "static", static_values=["9", "12"])
        assert P.set_parameter_type(path, "Slot", "Integer")["changed"] is True


class TestNoOpRetype:
    def test_no_op_retype_succeeds_unchanged_even_with_a_legacy_bad_default(self, path):
        _plant(
            path,
            "<Prompt>Date From</Prompt>",
            "<DefaultValue><Values><Value>not a date</Value></Values></DefaultValue>"
            "<Prompt>Date From</Prompt>",
        )
        before = Path(path).read_bytes()
        out = P.set_parameter_type(path, "DateFrom", "DateTime")
        assert out == {"parameter": "DateFrom", "type": "DateTime", "changed": False}
        assert Path(path).read_bytes() == before


class TestHiddenWithoutDefault:
    def test_add_hidden_warns(self, path):
        out = P.add_parameter(path, "Secret", "String", hidden=True)
        assert len(out["warnings"]) == 1
        assert "hidden, not nullable and has no default" in out["warnings"][0]

    def test_hidden_with_default_does_not_warn(self, path):
        P.add_parameter(path, "Secret", "String", prompt="Secret")
        P.set_parameter_default_values(path, "Secret", "static", ["x"])
        out = P.update_parameter_advanced(path, "Secret", hidden=True)
        assert "warnings" not in out

    def test_hidden_nullable_does_not_warn(self, path):
        out = P.add_parameter(path, "Secret", "String", hidden=True, allow_null=True)
        assert "warnings" not in out

    def test_clearing_the_default_of_a_hidden_param_warns(self, path):
        P.set_parameter_default_values(path, "DateFrom", "static", ["=Today()"])
        P.update_parameter_advanced(path, "DateFrom", hidden=True)
        out = P.set_parameter_default_values(path, "DateFrom", "static", [])
        assert out["cleared"] is True
        assert "hidden" in out["warnings"][0]


class TestQueryFields:
    def test_valid_values_missing_value_and_label_field_refuses(self, path):
        _refused(
            path,
            P.set_parameter_available_values,
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="NoSuchField",
            query_label_field="AlsoMissing",
            match="query_value_field='NoSuchField', query_label_field='AlsoMissing'",
        )

    def test_valid_values_missing_label_field_refuses(self, path):
        _refused(
            path,
            P.set_parameter_available_values,
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
            query_label_field="Nope",
            match="not a field of dataset 'LookupSet'",
        )

    def test_default_values_missing_value_field_refuses(self, path):
        _refused(
            path,
            P.set_parameter_default_values,
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="Nope",
            match="LookupKey",
        )

    def test_existing_fields_are_fine(self, path):
        out = P.set_parameter_available_values(
            path,
            "DateFrom",
            "query",
            query_dataset="LookupSet",
            query_value_field="LookupKey",
            query_label_field="LookupKey",
        )
        assert out["changed"] is True


class TestLegacyProblemsDontBlockUnrelatedEdits:
    def test_existing_error_becomes_a_warning(self, path):
        P.add_parameter(path, "Region", "String", prompt="Region", multi_value=True)
        _plant(
            path,
            "<MultiValue>true</MultiValue>",
            "<Nullable>true</Nullable><MultiValue>true</MultiValue>",
        )
        out = P.set_parameter_default_values(path, "Region", "static", ["EU", "US"])
        assert out["changed"] is True
        assert any("MultiValue and Nullable" in w for w in out["warnings"])

    def test_fixing_the_legacy_error_clears_it(self, path):
        P.add_parameter(path, "Region", "String", prompt="Region", multi_value=True)
        _plant(
            path,
            "<MultiValue>true</MultiValue>",
            "<Nullable>true</Nullable><MultiValue>true</MultiValue>",
        )
        out = P.update_parameter_advanced(path, "Region", allow_null=False)
        assert out == {"parameter": "Region", "changed": ["Nullable"]}
        assert _state_issues(path) == []


class TestRepro:
    """The #93 repro: every write is refused except the hidden one, which warns."""

    def test_issue_repro(self, path):
        P.add_parameter(path, "Region", "String", prompt="Region", multi_value=True)
        attempts = [
            (P.update_parameter_advanced, ("Region",), {"allow_null": True}),
            (P.add_parameter, ("Flag", "Boolean"), {"multi_value": True}),
            (P.set_parameter_default_values, ("DateFrom", "static", ["not a date"]), {}),
            (
                P.set_parameter_default_values,
                ("DateTo", "static", ["2026-01-01", "2026-02-01"]),
                {},
            ),
            (
                P.set_parameter_available_values,
                ("Region", "query"),
                {
                    "query_dataset": "LookupSet",
                    "query_value_field": "NoSuchField",
                    "query_label_field": "AlsoMissing",
                },
            ),
        ]
        for fn, args, kwargs in attempts:
            with pytest.raises(ValueError):
                fn(path, *args, **kwargs)
        assert "warnings" in P.add_parameter(path, "Secret", "String", hidden=True)
        assert P.set_parameter_type(path, "DateFrom", "DateTime")["changed"] is False
        P.add_parameter(path, "Slot", "String", prompt="Slot")
        P.set_parameter_available_values(path, "Slot", "static", static_values=["09:00", "12:00"])
        with pytest.raises(ValueError):
            P.set_parameter_type(path, "Slot", "Integer")
        assert validate_report(path)["valid"]
        issues = _state_issues(path)
        assert [(i["severity"], i["location"]) for i in issues] == [
            ("warning", "ReportParameter[Name='Secret']")
        ]


class TestParameterStateLint:
    def test_registered(self):
        assert "parameter-state" in ALL_RULES

    def test_clean_fixture(self, path):
        assert _state_issues(path) == []

    def test_reports_each_legacy_problem(self, path):
        _plant(
            path,
            "<Prompt>Date From</Prompt>",
            "<Nullable>true</Nullable>"
            "<DefaultValue><Values><Value>not a date</Value><Value>2026-01-01</Value>"
            "</Values></DefaultValue><Prompt>Date From</Prompt>"
            "<ValidValues><ParameterValues><ParameterValue><Value>soon</Value>"
            "</ParameterValue></ParameterValues></ValidValues>"
            "<MultiValue>true</MultiValue><Hidden>true</Hidden>",
        )
        _plant(
            path,
            "<DataType>DateTime</DataType>\n      <Prompt>Date To</Prompt>",
            "<DataType>Boolean</DataType>\n      <Prompt>Date To</Prompt>"
            "<MultiValue>true</MultiValue><Hidden>true</Hidden>",
        )
        issues = _state_issues(path)
        by_param: dict[str, list[str]] = {}
        for issue in issues:
            by_param.setdefault(issue["location"], []).append(issue["message"])
        date_from = by_param["ReportParameter[Name='DateFrom']"]
        assert any("MultiValue and Nullable" in m for m in date_from)
        assert any("default value(s) ['not a date']" in m for m in date_from)
        assert any("static valid value(s) ['soon']" in m for m in date_from)
        # MultiValue, so two defaults are fine; Nullable, so hidden is fine.
        assert not any("default values but" in m or "hidden" in m for m in date_from)
        date_to = by_param["ReportParameter[Name='DateTo']"]
        assert any("multi-value Boolean" in m for m in date_to)
        warnings = [i for i in issues if i["severity"] == "warning"]
        assert [w["location"] for w in warnings] == ["ReportParameter[Name='DateTo']"]
        assert all(i["severity"] == "error" for i in issues if i not in warnings)

    def test_two_defaults_single_value(self, path):
        _plant(
            path,
            "<Prompt>Date To</Prompt>",
            "<DefaultValue><Values><Value>2026-01-01</Value><Value>2026-02-01</Value>"
            "</Values></DefaultValue><Prompt>Date To</Prompt>",
        )
        (issue,) = _state_issues(path)
        assert issue["severity"] == "error"
        assert "2 default values but isn't MultiValue" in issue["message"]
