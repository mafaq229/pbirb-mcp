"""Tests for Phase 9 — expression reference + emitters.

The reference is static data, so the tests focus on shape (categories
present, entry schema honoured) and on the bits that close documented
session feedback (the `&` concat-operator encoding note).
"""

from __future__ import annotations

import pytest

from pbirb_mcp.ops.expressions import (
    count_where,
    get_expression_reference,
    iif_format,
    sum_where,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools


class TestGetExpressionReference:
    def test_returns_dict(self):
        ref = get_expression_reference()
        assert isinstance(ref, dict)

    def test_has_expected_categories(self):
        ref = get_expression_reference()
        assert set(ref.keys()) >= {
            "globals",
            "parameters",
            "fields",
            "aggregates",
            "conditionals",
            "strings",
            "dates",
        }

    def test_each_entry_has_required_keys(self):
        ref = get_expression_reference()
        for category, entries in ref.items():
            assert isinstance(entries, list), category
            assert entries, f"category {category!r} has no entries"
            for entry in entries:
                assert set(entry.keys()) >= {"name", "syntax", "example", "description"}, (
                    f"{category} entry {entry!r} missing required keys"
                )

    def test_concat_operator_documents_encoding_gotcha(self):
        # The user's recent pain: `&` vs `&amp;`. The cheat-sheet must
        # explicitly call this out so a future LLM doesn't hit
        # BC30451 ''amp' is not declared'.
        ref = get_expression_reference()
        concat = next(e for e in ref["strings"] if e["name"] == "Concatenation")
        assert "&amp;" in concat["description"]
        assert "BC30451" in concat["description"]

    def test_iif_present_in_conditionals(self):
        ref = get_expression_reference()
        iif = next(e for e in ref["conditionals"] if e["name"] == "IIf")
        assert "Both branches ALWAYS evaluate" in iif["description"]

    def test_returned_dict_is_isolated_copy(self):
        # Mutating the returned dict must not affect subsequent calls.
        ref1 = get_expression_reference()
        ref1["globals"].append(
            {"name": "Mutated", "syntax": "x", "example": "x", "description": "x"}
        )
        ref2 = get_expression_reference()
        names = {e["name"] for e in ref2["globals"]}
        assert "Mutated" not in names


class TestCountWhere:
    def test_emits_canonical_pattern(self):
        out = count_where('Fields!Status.Value = "Active"')
        assert out == '=Count(IIf(Fields!Status.Value = "Active", 1, Nothing))'

    def test_leading_eq_is_dropped(self):
        # #96: LLMs routinely pass '=...'; IIf(=... is invalid.
        assert count_where("=Fields!X.Value > 0") == count_where("Fields!X.Value > 0")
        assert count_where("  = Fields!X.Value > 0") == count_where("Fields!X.Value > 0")

    def test_scope_argument(self):
        assert count_where("Fields!X.Value > 0", scope="DataSet1") == (
            '=Count(IIf(Fields!X.Value > 0, 1, Nothing), "DataSet1")'
        )
        doubled = ', "My ' + '""' + "Q" + '""' + '")'
        assert count_where("c", scope='My "Q"').endswith(doubled)

    def test_blank_scope_rejected(self):
        with pytest.raises(ValueError, match="scope"):
            count_where("c", scope=" ")

    def test_lone_eq_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            count_where("=")

    def test_empty_condition_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            count_where("")

    def test_whitespace_only_condition_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            count_where("   ")


class TestSumWhere:
    def test_emits_canonical_pattern(self):
        out = sum_where("Fields!Amount.Value", 'Fields!Status.Value = "Active"')
        assert out == ('=Sum(IIf(Fields!Status.Value = "Active", Fields!Amount.Value, Nothing))')

    def test_no_int32_zero_branch(self):
        # #96: Int32 0 mixed with Double/Decimal fails the aggregate.
        out = sum_where("Fields!Amount.Value", "Fields!Ok.Value")
        assert ", 0)" not in out and "Nothing" in out

    def test_leading_eq_is_dropped_from_both_args(self):
        assert sum_where("=Fields!Amount.Value", "=Fields!Ok.Value") == (
            "=Sum(IIf(Fields!Ok.Value, Fields!Amount.Value, Nothing))"
        )

    def test_scope_argument(self):
        assert sum_where("Fields!A.Value", "Fields!Ok.Value", scope="DataSet1") == (
            '=Sum(IIf(Fields!Ok.Value, Fields!A.Value, Nothing), "DataSet1")'
        )

    def test_empty_field_rejected(self):
        with pytest.raises(ValueError, match="field_expression"):
            sum_where("", "x")

    def test_empty_condition_rejected(self):
        with pytest.raises(ValueError, match="condition"):
            sum_where("Fields!X.Value", "")


class TestIIfFormat:
    def test_emits_canonical_pattern(self):
        out = iif_format("Fields!Active.Value", '"Yes"', '"No"')
        assert out == '=IIf(Fields!Active.Value, "Yes", "No")'

    def test_leading_eq_is_dropped(self):
        assert iif_format("=Fields!Ok.Value", '="Y"', '"N"') == '=IIf(Fields!Ok.Value, "Y", "N")'

    def test_supports_nested_expressions(self):
        out = iif_format(
            "Fields!X.Value > 100",
            'Format(Fields!X.Value, "C2")',
            '"-"',
        )
        assert out == ('=IIf(Fields!X.Value > 100, Format(Fields!X.Value, "C2"), "-")')

    def test_empty_args_rejected(self):
        with pytest.raises(ValueError, match="condition"):
            iif_format("", "1", "2")
        with pytest.raises(ValueError, match="true_value"):
            iif_format("c", "", "2")
        with pytest.raises(ValueError, match="false_value"):
            iif_format("c", "1", "")


class TestToolRegistration:
    def test_get_expression_reference_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "get_expression_reference" in names

    def test_emitter_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {"count_where", "sum_where", "iif_format"} <= names

    def test_callable_via_jsonrpc(self):
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_expression_reference", "arguments": {}},
            }
        )
        import json

        text = resp["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert "globals" in payload
        assert "strings" in payload

    def test_count_where_callable_via_jsonrpc(self):
        srv = MCPServer()
        register_all_tools(srv)
        resp = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "count_where",
                    "arguments": {"condition": 'Fields!Status.Value = "Active"'},
                },
            }
        )
        import json

        text = resp["result"]["content"][0]["text"]
        # The emitter returns a string; server JSON-encodes that.
        assert json.loads(text) == ('=Count(IIf(Fields!Status.Value = "Active", 1, Nothing))')


# ---- #96: reference corrections and additions -----------------------------


class TestReferenceCorrections:
    def _entries(self):
        ref = get_expression_reference()
        return {e["name"]: e for entries in ref.values() for e in entries}

    def test_is_missing_means_absent_not_null(self):
        ref = get_expression_reference()
        desc = next(e for e in ref["fields"] if e["name"] == "IsMissing")["description"]
        assert "absent" in desc
        assert "IsNothing" in desc

    def test_label_does_not_cite_a_label_expression(self):
        ref = get_expression_reference()
        desc = next(e for e in ref["parameters"] if e["name"] == "Label")["description"]
        assert "LabelExpression" not in desc

    @pytest.mark.parametrize(
        "name",
        [
            "IsNothing",
            "Lookup",
            "LookupSet",
            "MultiLookup",
            "Scope argument",
            "IsMultiValue",
            "RowNumber",
            "RunningValue",
            "CountRows",
            "InScope",
        ],
    )
    def test_entry_exists_with_example(self, name):
        entry = self._entries()[name]
        assert entry["example"].startswith("=")
        assert entry["syntax"] and entry["description"]

    def test_scope_argument_example_quotes_the_scope(self):
        assert ', "' in self._entries()["Scope argument"]["example"]

    def test_conditional_idioms_match_the_emitters(self):
        entries = self._entries()
        assert "Nothing" in entries["Conditional Sum"]["syntax"]
        assert entries["Conditional Count"]["syntax"].startswith("Count(")

    def test_no_release_history_in_descriptions(self):
        for entry in self._entries().values():
            assert "Phase" not in entry["description"]
