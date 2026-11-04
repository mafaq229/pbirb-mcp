"""Tests for the tokenizer-based DAX result-column parser (#24).

``pbirb_mcp.ops.dax`` replaces the regex scan that used to drive
``refresh_dataset_fields``, ``update_dataset_query(alias_strategy=
'preserve_field_names')`` and the ``dataset-fields-out-of-sync`` lint
rule. These tests pin:

* the tokenizer (comments dropped, escaped strings / quoted names /
  brackets emitted as single atoms, ``@param`` tokens);
* ``result_columns`` on the shapes Report Builder actually authors;
* the legacy ``_extract_dax_*`` wrappers in ``ops/dataset.py`` that
  now sit on top of the parser.
"""

from __future__ import annotations

import pytest

from pbirb_mcp.ops.dataset import (
    _extract_dax_field_names,
    _extract_dax_qualified_data_fields,
)
from pbirb_mcp.ops.dax import Column, ResultColumns, Token, result_columns, tokenize

# ---- tokenizer -----------------------------------------------------------


def _kv(tokens):
    return [(t.kind, t.value) for t in tokens]


class TestTokenize:
    def test_basic_atoms(self):
        toks = tokenize("EVALUATE SUMMARIZECOLUMNS('T'[A], \"x\", 1.5)")
        assert _kv(toks) == [
            ("ident", "EVALUATE"),
            ("ident", "SUMMARIZECOLUMNS"),
            ("lparen", "("),
            ("quoted_name", "T"),
            ("bracket", "A"),
            ("comma", ","),
            ("string", "x"),
            ("comma", ","),
            ("number", "1.5"),
            ("rparen", ")"),
        ]

    def test_comments_dropped(self):
        dax = "// SELECTCOLUMNS(\"X\")\nEVALUATE -- 'T'[Old]\n/* 'T'[Older] */ 'T'"
        assert _kv(tokenize(dax)) == [("ident", "EVALUATE"), ("quoted_name", "T")]

    def test_comment_markers_inside_literals_are_not_comments(self):
        toks = tokenize("\"a // b -- c /* d */\" 'x--y'[p//q]")
        assert _kv(toks) == [
            ("string", "a // b -- c /* d */"),
            ("quoted_name", "x--y"),
            ("bracket", "p//q"),
        ]

    def test_escapes_are_decoded(self):
        toks = tokenize('"say ""hi""" \'O\'\'Neil\'[a]]b]')
        assert _kv(toks) == [
            ("string", 'say "hi"'),
            ("quoted_name", "O'Neil"),
            ("bracket", "a]b"),
        ]
        # raw keeps the source spelling, offsets index into the source.
        assert toks[1].raw == "'O''Neil'"

    def test_offsets_and_raw(self):
        dax = "  FILTER ( 'T' )"
        toks = tokenize(dax)
        for t in toks:
            assert dax[t.start : t.end] == t.raw

    def test_parameters(self):
        toks = tokenize("'T'[D] >= @From && \"@NotAParam\" <> @To_2 -- @Nope")
        params = [t.value for t in toks if t.kind == "param"]
        assert params == ["From", "To_2"]

    def test_operators(self):
        kinds = [
            t.value for t in tokenize("a && b || c <= d >= e <> f == g => h") if t.kind == "op"
        ]
        assert kinds == ["&&", "||", "<=", ">=", "<>", "==", "=>"]

    def test_unterminated_literals_are_flagged(self):
        for dax in ('"abc', "'abc", "[abc", "/* abc"):
            toks = tokenize(dax)
            if dax.startswith("/*"):
                assert toks == []
            else:
                assert len(toks) == 1
                assert toks[0].terminated is False
        assert tokenize('"ok"')[0].terminated is True

    def test_token_is_public_and_immutable(self):
        t = tokenize("x")[0]
        assert isinstance(t, Token)
        with pytest.raises(AttributeError):
            t.kind = "y"  # type: ignore[misc]


# ---- result_columns: the issue's repro table -----------------------------


ISSUE_CASES = [
    (
        "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS('T'[A], 'T'[B]), "
        "FILTER(VALUES('T'[D]), 'T'[D] >= @From))",
        ["A", "B"],
    ),
    ("EVALUATE SUMMARIZECOLUMNS('T'[A], \"Total X\", SUM('T'[X]))", ["A", "Total X"]),
    (
        "EVALUATE SELECTCOLUMNS(FILTER('T', 'T'[K] = \"Car\"), \"Name\", 'T'[N], "
        '"Flag", IF(\'T\'[F], "Yes", "No"))',
        ["Name", "Flag"],
    ),
    (
        "EVALUATE VAR _r = ADDCOLUMNS('T', \"_D\", DATE(YEAR('T'[ts]),1,1)) "
        "RETURN GROUPBY(_r, [_D], 'T'[type], \"Count\", COUNTX(CURRENTGROUP(), 1))",
        ["_D", "type", "Count"],
    ),
    ("EVALUATE ADDCOLUMNS(SELECTCOLUMNS('T', \"A\", 'T'[A]), \"B\", [A] * 2)", ["A", "B"]),
    (
        "// SELECTCOLUMNS(\"X\")\nEVALUATE SUMMARIZECOLUMNS('T'[A]) -- 'T'[Old]\n/* 'T'[Older] */",
        ["A"],
    ),
    (
        "DEFINE MEASURE 'T'[M] = SUM('T'[X])\nEVALUATE SUMMARIZECOLUMNS('T'[A], \"M\", [M])",
        ["A", "M"],
    ),
    ("EVALUATE SUMMARIZECOLUMNS('C'[Name], 'P'[Name])", ["Name", "Name"]),
    ("EVALUATE SUMMARIZECOLUMNS('O''Neil'[A])", ["A"]),
    ("EVALUATE 'T' ORDER BY 'T'[D]", []),
]


@pytest.mark.parametrize("dax,expected", ISSUE_CASES)
def test_issue_repro_result_columns(dax, expected):
    assert [c.name for c in result_columns(dax).columns] == expected


@pytest.mark.parametrize("dax,expected", ISSUE_CASES)
def test_issue_repro_legacy_field_names(dax, expected):
    names, _warnings = _extract_dax_field_names(dax)
    assert names == expected


def test_oneil_data_field_is_unquoted_and_unescaped():
    (col,) = result_columns("EVALUATE SUMMARIZECOLUMNS('O''Neil'[A])").columns
    assert col == Column("A", "O'Neil[A]", "column")


def test_bare_evaluate_table_warns():
    res = result_columns("EVALUATE 'T' ORDER BY 'T'[D]")
    assert res.columns == []
    assert any("EVALUATE 'Table'" in w for w in res.warnings)


def test_duplicate_names_from_different_tables_are_kept():
    res = result_columns("EVALUATE SUMMARIZECOLUMNS('C'[Name], 'P'[Name])")
    assert [c.data_field for c in res.columns] == ["C[Name]", "P[Name]"]
    assert res.warnings == []


# ---- result_columns: acceptance shapes -----------------------------------


SHAPES = [
    pytest.param(
        "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS('Sales'[Region], 'Sales'[Product Name]), "
        "FILTER(ALL('Sales'[Date]), 'Sales'[Date] >= @From), "
        "KEEPFILTERS(TREATAS({\"EU\"}, 'Geo'[Zone])))",
        [("Region", "Sales[Region]", "column"), ("Product Name", "Sales[Product Name]", "column")],
        id="calculatetable-summarizecolumns-filter",
    ),
    pytest.param(
        "EVALUATE\nVAR _data =\n    SELECTCOLUMNS('Trips', \"x\", 'Trips'[Id], "
        "\"Distance__Km_\", 'Trips'[Distance (Km)])\n"
        "RETURN FILTER(_data, [x] > 10)\nORDER BY [x]",
        [("x", "[x]", "alias"), ("Distance__Km_", "[Distance__Km_]", "alias")],
        id="var-selectcolumns-return-filter-order-by",
    ),
    pytest.param(
        "EVALUATE VAR _rows = ADDCOLUMNS('Events', \"_Year\", YEAR('Events'[ts])) "
        "RETURN GROUPBY(_rows, [_Year], 'Events'[type], "
        '"Count", COUNTX(CURRENTGROUP(), 1))',
        [
            ("_Year", "[_Year]", "alias"),
            ("type", "Events[type]", "column"),
            ("Count", "[Count]", "alias"),
        ],
        id="addcolumns-groupby-countx",
    ),
    pytest.param(
        "EVALUATE SUMMARIZECOLUMNS('Date'[Year], 'Product'[Category], "
        "FILTER(VALUES('Date'[Month]), 'Date'[Month] > 3), "
        '"Total Sales", [Total Sales], "Margin %", DIVIDE([Profit], [Revenue]))',
        [
            ("Year", "Date[Year]", "column"),
            ("Category", "Product[Category]", "column"),
            ("Total Sales", "[Total Sales]", "alias"),
            ("Margin %", "[Margin %]", "alias"),
        ],
        id="summarizecolumns-measure-alias",
    ),
    pytest.param(
        "/* header comment: SELECTCOLUMNS('X', \"Bogus\", 1) */\n"
        "DEFINE\n"
        "    MEASURE 'Sales'[Big] = CALCULATE(SUM('Sales'[Amount]), 'Sales'[Amount] > 100)\n"
        "    VAR __flt = TREATAS({\"Car\"}, 'Sales'[Kind])  // 'Sales'[Ignored]\n"
        "EVALUATE\n"
        '    SUMMARIZECOLUMNS(\'Sales\'[Kind], __flt, "Big ""one""", [Big]) -- trailing\n',
        [("Kind", "Sales[Kind]", "column"), ('Big "one"', '[Big "one"]', "alias")],
        id="comments-escaped-quotes-define-measure",
    ),
    pytest.param(
        "EVALUATE TOPN(10, SUMMARIZECOLUMNS('C'[Customer], \"Revenue\", [Revenue]), "
        "[Revenue], DESC) ORDER BY [Revenue] DESC, 'C'[Customer]",
        [("Customer", "C[Customer]", "column"), ("Revenue", "[Revenue]", "alias")],
        id="topn-order-by",
    ),
    pytest.param(
        "EVALUATE ( SELECTCOLUMNS ( 'T', \"A\", 'T'[A] ) )",
        [("A", "[A]", "alias")],
        id="parenthesised-evaluate",
    ),
    pytest.param(
        "evaluate selectcolumns('T', \"A\", 'T'[A]) order by [A]",
        [("A", "[A]", "alias")],
        id="lower-case-keywords",
    ),
    pytest.param(
        "EVALUATE SUMMARIZE('Sales', 'Sales'[Region], \"Qty\", SUM('Sales'[Qty]))",
        [("Region", "Sales[Region]", "column"), ("Qty", "[Qty]", "alias")],
        id="summarize",
    ),
    pytest.param(
        "EVALUATE ADDCOLUMNS(VALUES('Sales'[Region]), \"Qty\", [Total Qty])",
        [("Region", "Sales[Region]", "column"), ("Qty", "[Qty]", "alias")],
        id="addcolumns-over-values",
    ),
    pytest.param(
        'EVALUATE SELECTCOLUMNS(Sales, "Id", Sales[Id], "Label", '
        'Sales[Name] & " (" & Sales[Code] & ")")',
        [("Id", "[Id]", "alias"), ("Label", "[Label]", "alias")],
        id="unquoted-table-names-and-string-literals-in-expressions",
    ),
    pytest.param(
        "DEFINE VAR _base = SUMMARIZECOLUMNS('T'[A], \"S\", [S])\n"
        'TABLE Extra = ROW("One", 1)\n'
        "EVALUATE _base",
        [("A", "T[A]", "column"), ("S", "[S]", "alias")],
        id="define-var-table-reference",
    ),
    pytest.param(
        "EVALUATE SUMMARIZECOLUMNS(ROLLUPADDISSUBTOTAL('G'[Country], \"IsTotal\"), "
        '"Sales", [Sales])',
        [
            ("Country", "G[Country]", "column"),
            ("IsTotal", "[IsTotal]", "alias"),
            ("Sales", "[Sales]", "alias"),
        ],
        id="rollupaddissubtotal",
    ),
    pytest.param(
        'EVALUATE ROW("Total", [Total], "Count", COUNTROWS(\'T\'))',
        [("Total", "[Total]", "alias"), ("Count", "[Count]", "alias")],
        id="row",
    ),
    pytest.param(
        "EVALUATE VAR a = VAR inner = SELECTCOLUMNS('T', \"In\", 1) RETURN inner "
        "VAR b = 2 RETURN FILTER(a, b > 1)",
        [("In", "[In]", "alias")],
        id="nested-var-blocks",
    ),
    pytest.param(
        "EVALUATE SELECTCOLUMNS('T', \"A\", 1)\nEVALUATE SELECTCOLUMNS('T', \"B\", 2)",
        [("B", "[B]", "alias")],
        id="last-evaluate-wins",
    ),
]


@pytest.mark.parametrize("dax,expected", SHAPES)
def test_result_columns_shapes(dax, expected):
    res = result_columns(dax)
    assert [tuple(c) for c in res.columns] == expected


def test_shapes_without_ambiguity_have_no_warnings():
    dax = (
        "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS('T'[A], \"M\", [M]), "
        "FILTER(VALUES('T'[D]), 'T'[D] >= @From)) ORDER BY 'T'[A]"
    )
    assert result_columns(dax).warnings == []


def test_result_is_unpackable_named_tuple():
    res = result_columns("EVALUATE SUMMARIZECOLUMNS('T'[A])")
    assert isinstance(res, ResultColumns)
    cols, warnings = res
    assert cols == [Column(name="A", data_field="T[A]", kind="column")]
    assert warnings == []


# ---- result_columns: failure modes ---------------------------------------


@pytest.mark.parametrize(
    "dax",
    [
        "",
        "   -- only a comment",
        "DEFINE",
        "DEFINE MEASURE 'T'[M] = 1",
        "SELECT * FROM Sales",
        "EVALUATE",
        "EVALUATE 'Sales'",
        "EVALUATE Sales",
        "EVALUATE FILTER('Sales', 'Sales'[A] > 1)",
        "EVALUATE CALCULATETABLE('Sales', 'Sales'[A] > 1)",
        "EVALUATE SOMEFUTUREFUNCTION('T'[A], \"x\", 1)",
        "EVALUATE SUMMARIZECOLUMNS(ProductID, ProductName)",
    ],
)
def test_unrecognised_shapes_return_no_columns_and_warn(dax):
    res = result_columns(dax)
    assert res.columns == []
    assert res.warnings


def test_addcolumns_over_bare_table_keeps_aliases_but_warns():
    res = result_columns("EVALUATE ADDCOLUMNS('Sales', \"Double\", 'Sales'[Amount] * 2)")
    assert [c.name for c in res.columns] == ["Double"]
    assert res.warnings  # Sales' own columns are unknown


def test_unbalanced_parens_warn_but_do_not_raise():
    res = result_columns("EVALUATE SUMMARIZECOLUMNS('T'[A], \"X\", SUM('T'[B])")
    assert [c.name for c in res.columns] == ["A", "X"]
    assert any("unbalanced" in w for w in res.warnings)


def test_unterminated_string_warns():
    res = result_columns("EVALUATE SELECTCOLUMNS('T', \"A")
    assert any("unterminated" in w for w in res.warnings)


def test_multiple_evaluate_warns():
    res = result_columns('EVALUATE ROW("a", 1) EVALUATE ROW("b", 2)')
    assert [c.name for c in res.columns] == ["b"]
    assert any("EVALUATE" in w for w in res.warnings)


def test_self_referencing_variable_does_not_recurse_forever():
    res = result_columns("DEFINE VAR x = x EVALUATE x")
    assert res.columns == []
    assert res.warnings


# ---- legacy wrappers in ops/dataset.py -----------------------------------


class TestLegacyWrappers:
    def test_qualified_targets_for_column_refs(self):
        targets, warnings = _extract_dax_qualified_data_fields(
            "EVALUATE CALCULATETABLE(SUMMARIZECOLUMNS('T'[A], 'O''Neil'[B]), "
            "FILTER(VALUES('T'[D]), 'T'[D] >= @From))"
        )
        assert targets == ["T[A]", "O'Neil[B]"]
        assert warnings == []

    def test_qualified_targets_bracket_alias_positions(self):
        # #38: an alias position's target is Report Builder's ``[alias]``.
        targets, _ = _extract_dax_qualified_data_fields(
            "EVALUATE SUMMARIZECOLUMNS('T'[A], \"Total X\", SUM('T'[X]))"
        )
        assert targets == ["T[A]", "[Total X]"]

    def test_qualified_targets_keep_duplicate_column_names(self):
        targets, _ = _extract_dax_qualified_data_fields(
            "EVALUATE SUMMARIZECOLUMNS('C'[Name], 'P'[Name])"
        )
        assert targets == ["C[Name]", "P[Name]"]

    def test_qualified_targets_warn_when_nothing_recoverable(self):
        targets, warnings = _extract_dax_qualified_data_fields("EVALUATE 'Sales'")
        assert targets == []
        assert any("qualified" in w for w in warnings)

    def test_field_names_warn_for_bare_table(self):
        names, warnings = _extract_dax_field_names("EVALUATE 'Sales'")
        assert names == []
        assert any("EVALUATE" in w for w in warnings)
