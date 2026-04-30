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
        concat = next(
            e for e in ref["strings"] if e["name"] == "Concatenation"
        )
        assert "&amp;" in concat["description"]
        assert "BC30451" in concat["description"]

    def test_iif_present_in_conditionals(self):
        ref = get_expression_reference()
        iif = next(e for e in ref["conditionals"] if e["name"] == "IIf")
        assert "Both branches ALWAYS evaluate" in iif["description"]

    def test_returned_dict_is_isolated_copy(self):
        # Mutating the returned dict must not affect subsequent calls.
        ref1 = get_expression_reference()
        ref1["globals"].append({"name": "Mutated", "syntax": "x", "example": "x", "description": "x"})
        ref2 = get_expression_reference()
        names = {e["name"] for e in ref2["globals"]}
        assert "Mutated" not in names


class TestCountWhere:
    def test_emits_canonical_pattern(self):
        out = count_where('Fields!Status.Value = "Active"')
        assert out == '=Sum(IIf(Fields!Status.Value = "Active", 1, 0))'

    def test_strips_leading_eq_is_caller_responsibility(self):
        # Helper accepts the body verbatim — if caller passes an
        # expression with a leading `=`, the emitted IIf body has it
        # too. That's by design; we don't second-guess the input.
        out = count_where("=Fields!X.Value > 0")
        assert "IIf(=Fields!X.Value > 0," in out

    def test_empty_condition_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            count_where("")

    def test_whitespace_only_condition_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            count_where("   ")


class TestSumWhere:
    def test_emits_canonical_pattern(self):
        out = sum_where(
            "Fields!Amount.Value", 'Fields!Status.Value = "Active"'
        )
        assert out == (
            '=Sum(IIf(Fields!Status.Value = "Active", '
            "Fields!Amount.Value, 0))"
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

    def test_supports_nested_expressions(self):
        out = iif_format(
            "Fields!X.Value > 100",
            'Format(Fields!X.Value, "C2")',
            '"-"',
        )
        assert out == (
            '=IIf(Fields!X.Value > 100, '
            'Format(Fields!X.Value, "C2"), "-")'
        )

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
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "get_expression_reference" in names

    def test_emitter_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
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
                    "arguments": {
                        "condition": 'Fields!Status.Value = "Active"'
                    },
                },
            }
        )
        import json

        text = resp["result"]["content"][0]["text"]
        # The emitter returns a string; server JSON-encodes that.
        assert json.loads(text) == (
            '=Sum(IIf(Fields!Status.Value = "Active", 1, 0))'
        )
