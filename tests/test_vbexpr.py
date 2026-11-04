"""Tests for the VB expression tokenizer (issue #21)."""

from __future__ import annotations

import pytest

from pbirb_mcp.ops import vbexpr as V


def _kinds(tokens):
    return [(t.kind, t.text) for t in tokens]


# ---- tokenize -------------------------------------------------------------


class TestTokenize:
    def test_field_reference_and_concat(self):
        toks = V.tokenize('Fields!A.Value & ") "')
        assert _kinds(toks) == [
            (V.IDENTIFIER, "Fields"),
            (V.OPERATOR, "!"),
            (V.IDENTIFIER, "A"),
            (V.OPERATOR, "."),
            (V.IDENTIFIER, "Value"),
            (V.OPERATOR, "&"),
            (V.STRING, '") "'),
        ]

    def test_offsets_slice_back_to_text(self):
        code = '  Sum(Fields!X.Value) & "a""b"  '
        for t in V.tokenize(code, include_whitespace=True):
            assert code[t.start : t.end] == t.text

    def test_whitespace_dropped_by_default(self):
        assert all(t.kind != V.WHITESPACE for t in V.tokenize("a +  b"))
        assert any(t.kind == V.WHITESPACE for t in V.tokenize("a + b", include_whitespace=True))

    def test_doubled_quote_escape_stays_in_one_string(self):
        toks = V.tokenize('"say ""hi"" (" & x')
        assert toks[0].kind == V.STRING
        assert toks[0].text == '"say ""hi"" ("'
        assert V.string_value(toks[0]) == 'say "hi" ('

    def test_char_literal_suffix(self):
        toks = V.tokenize('"a"c & "b"')
        assert toks[0] == V.Token(V.STRING, '"a"c', 0, 4)
        assert V.string_value(toks[0]) == "a"

    def test_typographic_quotes_delimit_strings(self):
        toks = V.tokenize("“(” & x")
        assert toks[0].kind == V.STRING
        assert V.balanced_delimiters(toks)

    def test_unterminated_string_is_error_token(self):
        toks = V.tokenize('"Total (" & x & ")')
        assert toks[-1].kind == V.ERROR
        assert toks[-1].text == '")'
        assert "unterminated string literal" in toks[-1].message

    def test_apostrophe_comment_runs_to_end_of_line(self):
        toks = V.tokenize("a ' comment ( with paren\n+ b")
        assert _kinds(toks) == [
            (V.IDENTIFIER, "a"),
            (V.COMMENT, "' comment ( with paren"),
            (V.OPERATOR, "+"),
            (V.IDENTIFIER, "b"),
        ]

    def test_apostrophe_inside_string_is_not_comment(self):
        toks = V.tokenize('"it\'s" & x')
        assert [t.kind for t in toks] == [V.STRING, V.OPERATOR, V.IDENTIFIER]

    def test_rem_comment_case_insensitive(self):
        toks = V.tokenize("a Rem ( note")
        assert _kinds(toks) == [(V.IDENTIFIER, "a"), (V.COMMENT, "Rem ( note")]

    def test_rem_as_member_name_is_identifier(self):
        toks = V.tokenize("Fields!REM.Value + x.Rem")
        assert all(t.kind != V.COMMENT for t in toks)
        assert [t.text for t in toks if t.kind == V.IDENTIFIER] == [
            "Fields",
            "REM",
            "Value",
            "x",
            "Rem",
        ]

    def test_rem_prefix_of_identifier_is_not_comment(self):
        toks = V.tokenize("Remainder(1)")
        assert toks[0] == V.Token(V.IDENTIFIER, "Remainder", 0, 9)

    def test_date_literal(self):
        toks = V.tokenize("#1/31/2024 12:00 PM# + x")
        assert toks[0] == V.Token(V.DATE, "#1/31/2024 12:00 PM#", 0, 20)

    def test_hash_type_char_is_not_date(self):
        toks = V.tokenize("x# + 1#")
        assert V.DATE not in [t.kind for t in toks]

    @pytest.mark.parametrize(
        "num", ["42", "1.5", ".5", "1e10", "2.5E-3", "&HFF", "&O17", "10D", "3L"]
    )
    def test_numbers(self, num):
        toks = V.tokenize(num)
        assert _kinds(toks) == [(V.NUMBER, num)]

    def test_ampersand_concat_not_hex(self):
        toks = V.tokenize('"a" &Header')
        assert _kinds(toks) == [(V.STRING, '"a"'), (V.OPERATOR, "&"), (V.IDENTIFIER, "Header")]

    @pytest.mark.parametrize("op", ["<>", "<=", ">=", "< =", "> =", ":=", "<<", ">>", "&="])
    def test_compound_operators(self, op):
        toks = V.tokenize(f"a {op} b")
        assert toks[1] == V.Token(V.OPERATOR, op, 2, 2 + len(op))

    def test_delimiters(self):
        toks = V.tokenize("f(a, b[0], {1})")
        assert [t.text for t in toks if t.kind == V.DELIMITER] == [
            "(",
            ",",
            "[",
            "]",
            ",",
            "{",
            "}",
            ")",
        ]

    def test_dollar_type_char_identifier(self):
        assert V.tokenize("Format$(x)")[0] == V.Token(V.IDENTIFIER, "Format$", 0, 7)

    def test_unknown_character(self):
        toks = V.tokenize("a ` b")
        assert toks[1].kind == V.UNKNOWN

    def test_empty(self):
        assert V.tokenize("") == []


# ---- expression helpers ---------------------------------------------------


class TestExpressionHelpers:
    def test_expression_body(self):
        assert V.expression_body("=Sum(x)") == "Sum(x)"
        assert V.expression_body("  =x") == "x"
        assert V.expression_body("plain text (") is None
        assert V.expression_body("") is None
        assert V.expression_body(None) is None

    def test_tokenize_expression_skips_prefix_and_keeps_offsets(self):
        text = ' =Fields!A.Value = "x"'
        toks = V.tokenize_expression(text)
        assert toks[0] == V.Token(V.IDENTIFIER, "Fields", 2, 8)
        # The comparison '=' is still there; the prefix '=' is not.
        assert [t.text for t in toks if t.kind == V.OPERATOR].count("=") == 1
        for t in toks:
            assert text[t.start : t.end] == t.text

    def test_tokenize_expression_none_for_literal(self):
        assert V.tokenize_expression("Sales (USD)") is None
        assert V.tokenize_expression(None) is None

    def test_significant_drops_comments_and_whitespace(self):
        toks = V.tokenize("a ' c", include_whitespace=True)
        assert _kinds(V.significant(toks)) == [(V.IDENTIFIER, "a")]
        assert _kinds(V.significant("a ' c")) == [(V.IDENTIFIER, "a")]

    def test_string_value_rejects_non_string(self):
        with pytest.raises(ValueError):
            V.string_value(V.tokenize("x")[0])


class TestUnterminatedString:
    def test_none_when_closed(self):
        assert V.unterminated_string('="a" & "b"') is None

    def test_detects_open_literal(self):
        tok = V.unterminated_string('="Total (" & Fields!N.Value & ")')
        assert tok is not None and tok.kind == V.ERROR
        assert tok.start == len('="Total (" & Fields!N.Value & ')

    def test_accepts_token_list(self):
        assert V.unterminated_string(V.tokenize('"abc')) is not None

    def test_quote_in_comment_ignored(self):
        assert V.unterminated_string("x ' don\"t") is None


class TestBalancedDelimiters:
    @pytest.mark.parametrize(
        "expr",
        [
            '=Fields!ProductID.Value & ") " & Fields!ProductName.Value',
            '="[" & Fields!A.Value & "]"',
            "=Sum(Fields!A.Value) ' trailing (comment",
            '=IIf(x > 0, Format(x, "(#,##0)"), "")',
            "=#1/1/2024# ",
            "=New Integer() {1, 2}",
            "=a REM )",
        ],
    )
    def test_balanced(self, expr):
        assert V.balanced_delimiters(V.tokenize_expression(expr))

    def test_missing_close_paren_returns_opener(self):
        tok = V.first_unbalanced_delimiter("Sum(Fields!X.Value")
        assert tok.text == "(" and tok.start == 3
        assert V.delimiter_family(tok) == "parentheses"

    def test_extra_close_returns_closer(self):
        tok = V.first_unbalanced_delimiter("a) + (b")
        assert tok.text == ")" and tok.start == 1

    def test_mismatched_pair(self):
        tok = V.first_unbalanced_delimiter("f(a]")
        assert tok.text == "]"
        assert V.delimiter_family(tok) == "brackets"

    def test_unclosed_brace(self):
        assert V.delimiter_family(V.first_unbalanced_delimiter("{1, 2")) == "braces"

    def test_delimiter_family_rejects_comma(self):
        with pytest.raises(ValueError):
            V.delimiter_family(V.tokenize(",")[0])


class TestBangReferences:
    def test_fields_value(self):
        refs = V.bang_references("Fields!A.Value + Fields!B.IsMissing + Fields!C", "Fields")
        assert [(r.name, r.member) for r in refs] == [
            ("A", "Value"),
            ("B", "IsMissing"),
            ("C", None),
        ]

    def test_collection_case_insensitive(self):
        refs = V.bang_references("fields!A.value", "Fields")
        assert [(r.collection, r.name, r.member) for r in refs] == [("fields", "A", "value")]

    def test_ignores_strings_and_comments(self):
        code = '"Fields!A.Value" & x \' Fields!B.Value'
        assert V.bang_references(code, "Fields") == []

    def test_other_collection_not_matched(self):
        assert V.bang_references("Parameters!P.Value", "Fields") == []

    def test_indices_point_into_significant_tokens(self):
        code = "x = Parameters!P.Value"
        sig = V.significant(code)
        (ref,) = V.bang_references(sig, "Parameters")
        assert sig[ref.first].text == "Parameters"
        assert sig[ref.last].text == "Value"
        assert sig[ref.first - 1].text == "="
        assert code[ref.start : ref.end] == "Parameters!P.Value"
