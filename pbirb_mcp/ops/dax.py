"""Tokenizer and result-column parser for DAX query text.

Report Builder stores a Power BI dataset's query as DAX in
``<Query>/<CommandText>``. Several tools need to know which columns
that query *returns* (``refresh_dataset_fields``, the
``preserve_field_names`` remap in ``update_dataset_query``, the
``dataset-fields-out-of-sync`` lint rule) or need to find tokens such
as ``@Param`` without being fooled by comments and string literals.
Regexes over the raw text get both wrong (#24), so this module has two
layers:

* :func:`tokenize` — a lossless-enough lexer: comments are dropped,
  ``"strings"`` (``""`` escape), ``'quoted names'`` (``''`` escape) and
  ``[brackets]`` (``]]`` escape) come out as single atoms with their
  escapes decoded, and every token keeps its source offsets. It's the
  public building block for anything that needs to scan DAX.
* :func:`result_columns` — a small structural walk over the outermost
  table expression of the last ``EVALUATE`` that returns the
  result-set columns as :class:`Column` records, plus warnings for
  shapes it can't see into (bare ``EVALUATE 'Table'``, unknown table
  functions, unbalanced input).

This is not a DAX evaluator. It only understands the table functions
whose output columns are syntactically visible (SUMMARIZECOLUMNS,
SELECTCOLUMNS, ADDCOLUMNS, GROUPBY, SUMMARIZE, ROW, DATATABLE, table
constructors, …) and the wrappers that pass a table through unchanged
(CALCULATETABLE, FILTER, TOPN, KEEPFILTERS, …). Anything else yields a
warning instead of a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional, Union

__all__ = [
    "ALIAS",
    "COLUMN",
    "Column",
    "ResultColumns",
    "Token",
    "result_columns",
    "tokenize",
]


# ---- tokenizer -----------------------------------------------------------


# Token kinds. Kept as plain strings so callers can compare without
# importing constants.
STRING = "string"  # "text"            value = decoded text
QUOTED_NAME = "quoted_name"  # 'Table'  value = decoded name
BRACKET = "bracket"  # [Column]           value = decoded name
IDENT = "ident"  # EVALUATE / Sales / _var / PERCENTILEX.INC
NUMBER = "number"  # 1 / 1.5 / .5 / 1e3
PARAM = "param"  # @From               value = name without "@"
LPAREN = "lparen"
RPAREN = "rparen"
LBRACE = "lbrace"
RBRACE = "rbrace"
COMMA = "comma"
OP = "op"  # = <> && || <= >= == => + - * / ^ & < > ! ; etc.


@dataclass(frozen=True)
class Token:
    """One lexical atom of DAX source.

    ``value`` is the decoded payload for literal-ish tokens (string
    text, quoted name, bracket content — escapes resolved, delimiters
    stripped; a parameter's name without the ``@``) and the source text
    for everything else. ``raw`` is always the exact source spelling and
    ``dax[start:end] == raw``. ``terminated`` is False for a string /
    quoted name / bracket that runs to end-of-input without its closing
    delimiter.
    """

    kind: str
    value: str
    raw: str
    start: int
    end: int
    terminated: bool = True


_TWO_CHAR_OPS = ("&&", "||", "<=", ">=", "<>", "==", "=>")
_SINGLE = {"(": LPAREN, ")": RPAREN, "{": LBRACE, "}": RBRACE, ",": COMMA}


def _is_ident_start(ch: str) -> bool:
    return ch == "_" or ch.isalpha()


def _is_ident_char(ch: str) -> bool:
    return ch == "_" or ch == "." or ch.isalnum()


def _scan_delimited(dax: str, i: int, close: str) -> tuple[str, int, bool]:
    """Scan a literal opened at ``dax[i]`` up to its ``close`` char,
    treating a doubled ``close`` as an escaped literal one. Returns
    ``(decoded_value, end_index, terminated)``."""
    n = len(dax)
    j = i + 1
    parts: list[str] = []
    while j < n:
        ch = dax[j]
        if ch == close:
            if j + 1 < n and dax[j + 1] == close:
                parts.append(close)
                j += 2
                continue
            return "".join(parts), j + 1, True
        parts.append(ch)
        j += 1
    return "".join(parts), n, False


def tokenize(dax: str) -> list[Token]:
    """Split DAX source into :class:`Token` atoms.

    * ``// ...`` and ``-- ...`` line comments and ``/* ... */`` block
      comments are dropped (only outside literals).
    * ``"..."`` → ``string`` (``""`` is an escaped quote),
      ``'...'`` → ``quoted_name`` (``''`` escape),
      ``[...]`` → ``bracket`` (``]]`` escape).
    * ``@Name`` → ``param``.
    * Identifiers (``SUMMARIZECOLUMNS``, ``Sales``, ``_r``,
      ``PERCENTILEX.INC``) → ``ident``; numbers → ``number``.
    * ``( ) { } ,`` get their own kinds; everything else is ``op``.

    Never raises: malformed input (unterminated literals or comments)
    is tokenized as far as possible; unterminated literals carry
    ``terminated=False``.
    """
    tokens: list[Token] = []
    n = len(dax)
    i = 0
    while i < n:
        ch = dax[i]
        nxt = dax[i + 1] if i + 1 < n else ""
        if ch.isspace():
            i += 1
            continue
        if (ch == "/" and nxt == "/") or (ch == "-" and nxt == "-"):
            j = dax.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "/" and nxt == "*":
            j = dax.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        start = i
        if ch in "\"'[":
            close = "]" if ch == "[" else ch
            kind = {'"': STRING, "'": QUOTED_NAME, "[": BRACKET}[ch]
            value, i, terminated = _scan_delimited(dax, i, close)
            tokens.append(Token(kind, value, dax[start:i], start, i, terminated))
            continue
        if ch == "@" and nxt and _is_ident_start(nxt):
            i += 1
            while i < n and _is_ident_char(dax[i]):
                i += 1
            tokens.append(Token(PARAM, dax[start + 1 : i], dax[start:i], start, i))
            continue
        if ch.isdigit() or (ch == "." and nxt.isdigit()):
            while i < n and (dax[i].isdigit() or dax[i] == "."):
                i += 1
            if i < n and dax[i] in "eE":
                j = i + 1
                if j < n and dax[j] in "+-":
                    j += 1
                if j < n and dax[j].isdigit():
                    i = j
                    while i < n and dax[i].isdigit():
                        i += 1
            tokens.append(Token(NUMBER, dax[start:i], dax[start:i], start, i))
            continue
        if _is_ident_start(ch):
            while i < n and _is_ident_char(dax[i]):
                i += 1
            tokens.append(Token(IDENT, dax[start:i], dax[start:i], start, i))
            continue
        if ch in _SINGLE:
            i += 1
            tokens.append(Token(_SINGLE[ch], ch, ch, start, i))
            continue
        two = dax[i : i + 2]
        if two in _TWO_CHAR_OPS:
            i += 2
            tokens.append(Token(OP, two, two, start, i))
            continue
        i += 1
        tokens.append(Token(OP, ch, ch, start, i))
    return tokens


# ---- result columns ------------------------------------------------------


COLUMN = "column"  # a model column reference: data_field is Table[Col]
ALIAS = "alias"  # a named "name", expr pair (or unqualified [Name])


class Column(NamedTuple):
    """One result-set column of a DAX query.

    * ``name`` — the column's name as the query returns it (an alias
      for ``"name", expr`` pairs, the bare column name for a
      ``'Table'[Col]`` reference).
    * ``data_field`` — exactly what Report Builder writes into the
      Field's ``<DataField>``: ``Table[Col]`` (no quotes, ``''``
      un-escaped) for a column reference, ``[name]`` for an alias.
    * ``kind`` — :data:`COLUMN` (``data_field`` is ``Table[Col]``) or
      :data:`ALIAS` (``data_field`` is ``[name]``).

    Names are *not* deduplicated: ``SUMMARIZECOLUMNS('C'[Name],
    'P'[Name])`` returns two ``Name`` columns (Report Builder
    disambiguates the Field names as ``Name`` / ``Name1``).
    """

    name: str
    data_field: str
    kind: str


class ResultColumns(NamedTuple):
    """Return value of :func:`result_columns`; unpacks as
    ``columns, warnings = result_columns(dax)``.

    A non-empty ``warnings`` means the column list may be incomplete
    or empty because part of the query couldn't be seen into; callers
    that make destructive decisions should treat it as "unknown".
    """

    columns: list[Column]
    warnings: list[str]


@dataclass
class _Call:
    name: Token
    args: list[list[_Item]]


@dataclass
class _Group:
    brace: bool  # True for {...} table constructor, False for (...)
    args: list[list[_Item]]


_Item = Union[Token, _Call, _Group]

# Identifiers that are DAX keywords: never the name of a function call
# even when a "(" follows (``EVALUATE ( ... )``, ``RETURN ( ... )``).
_KEYWORDS = frozenset(
    {
        "DEFINE",
        "EVALUATE",
        "MEASURE",
        "VAR",
        "RETURN",
        "TABLE",
        "COLUMN",
        "FUNCTION",
        "ORDER",
        "BY",
        "START",
        "AT",
        "ASC",
        "DESC",
        "IN",
    }
)

_DEFINE_ITEM_KEYWORDS = frozenset({"MEASURE", "VAR", "TABLE", "COLUMN", "FUNCTION"})

_BARE_TABLE_WARNING = (
    "DAX table expression {expr} is a bare table reference (EVALUATE 'Table' "
    "shape) — its column list isn't extractable without a metadata fetch. Use "
    "add_dataset_field explicitly per column, or rewrite as SELECTCOLUMNS / "
    "SUMMARIZECOLUMNS."
)

_MAX_DEPTH = 64


def _kw(item: _Item) -> Optional[str]:
    """Upper-cased identifier text when ``item`` is a bare identifier."""
    if isinstance(item, Token) and item.kind == IDENT:
        return item.value.upper()
    return None


def _group_tokens(tokens: list[Token], warnings: list[str]) -> list[_Item]:
    """Fold a flat token list into nested calls / groups.

    ``f(...)`` becomes a :class:`_Call` whose ``args`` are the
    comma-separated argument item lists; a bare ``(...)`` or ``{...}``
    becomes a :class:`_Group`. Commas at the top level stay as tokens
    (they only occur in ``ORDER BY`` lists). Unbalanced brackets are
    tolerated and reported once via ``warnings``.
    """
    n = len(tokens)
    unbalanced = [False]

    def parse(i: int, closer: Optional[str]) -> tuple[list[list[_Item]], int]:
        args: list[list[_Item]] = [[]]
        while i < n:
            t = tokens[i]
            if t.kind in (RPAREN, RBRACE):
                if closer is None:
                    unbalanced[0] = True
                    i += 1
                    continue
                if t.kind != closer:
                    unbalanced[0] = True
                return args, i + 1
            if t.kind == COMMA and closer is not None:
                args.append([])
                i += 1
                continue
            if t.kind in (LPAREN, LBRACE):
                sub, i = parse(i + 1, RPAREN if t.kind == LPAREN else RBRACE)
                if sub == [[]]:
                    sub = []
                cur = args[-1]
                prev = cur[-1] if cur else None
                if (
                    t.kind == LPAREN
                    and isinstance(prev, Token)
                    and prev.kind == IDENT
                    and prev.value.upper() not in _KEYWORDS
                ):
                    cur.pop()
                    cur.append(_Call(prev, sub))
                else:
                    cur.append(_Group(t.kind == LBRACE, sub))
                continue
            args[-1].append(t)
            i += 1
        if closer is not None:
            unbalanced[0] = True
        return args, i

    top, _ = parse(0, None)
    if unbalanced[0]:
        warnings.append("DAX has unbalanced parentheses / braces; result columns are best-effort.")
    return top[0]


def _is_stop(items: list[_Item], i: int, stops: frozenset[str]) -> bool:
    kw = _kw(items[i])
    if kw is None or kw not in stops:
        return False
    if kw in ("ORDER", "START"):
        follow = "BY" if kw == "ORDER" else "AT"
        return i + 1 < len(items) and _kw(items[i + 1]) == follow
    return True


def _consume_expr(items: list[_Item], i: int, stops: frozenset[str]) -> int:
    """Return the index just past the expression starting at ``items[i]``.

    The expression ends at the first top-level keyword in ``stops``.
    A ``VAR ... RETURN ...`` block is consumed as a whole, so the
    ``VAR``/``RETURN`` keywords of a nested block don't end an outer
    expression early.
    """
    n = len(items)
    if i < n and _kw(items[i]) == "VAR":
        inner = stops | {"VAR", "RETURN"}
        while i < n and _kw(items[i]) == "VAR":
            i += 2  # VAR <name>
            if i < n and isinstance(items[i], Token) and items[i].value == "=":
                i += 1
            i = _consume_expr(items, i, inner)
        if i < n and _kw(items[i]) == "RETURN":
            i = _consume_expr(items, i + 1, stops)
        return i
    while i < n and not _is_stop(items, i, stops):
        i += 1
    return i


# An environment maps a case-folded variable / query-table name to its
# definition items and the environment it was defined in (lexical scope).
_Env = dict  # dict[str, tuple[list[_Item], "_Env"]]


def _source(items: list[_Item]) -> str:
    """Compact human-readable rendering of ``items`` for warnings."""

    def one(it: _Item) -> str:
        if isinstance(it, Token):
            return it.raw
        if isinstance(it, _Call):
            return f"{it.name.raw}(…)"
        return "{…}" if it.brace else "(…)"

    text = " ".join(one(it) for it in items)
    return repr(text if len(text) <= 60 else text[:57] + "...")


def _column_ref(arg: list[_Item]) -> Optional[Column]:
    """``'T'[C]`` / ``T[C]`` → COLUMN, a lone ``[C]`` → ALIAS, else None."""
    if (
        len(arg) == 2
        and isinstance(arg[0], Token)
        and arg[0].kind in (QUOTED_NAME, IDENT)
        and isinstance(arg[1], Token)
        and arg[1].kind == BRACKET
    ):
        table, col = arg[0].value, arg[1].value
        return Column(col, f"{table}[{col}]", COLUMN)
    if len(arg) == 1 and isinstance(arg[0], Token) and arg[0].kind == BRACKET:
        return Column(arg[0].value, f"[{arg[0].value}]", ALIAS)
    return None


def _string(arg: list[_Item]) -> Optional[str]:
    if len(arg) == 1 and isinstance(arg[0], Token) and arg[0].kind == STRING:
        return arg[0].value
    return None


def _alias(name: str) -> Column:
    return Column(name, f"[{name}]", ALIAS)


class _Walker:
    def __init__(self, warnings: list[str]):
        self.warnings = warnings

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    # -- generic table expression ---------------------------------------

    def table(self, items: list[_Item], env: _Env, depth: int = 0) -> list[Column]:
        if depth > _MAX_DEPTH:
            self.warn("DAX expression nests too deeply to analyse; result columns unknown.")
            return []
        if not items:
            self.warn("EVALUATE has no table expression; result columns unknown.")
            return []
        if _kw(items[0]) == "VAR":
            return self._var_block(items, env, depth)
        if len(items) == 1:
            it = items[0]
            if isinstance(it, _Call):
                return self._call(it, env, depth)
            if isinstance(it, _Group):
                if it.brace:
                    return self._table_constructor(it)
                if len(it.args) == 1:
                    return self.table(it.args[0], env, depth + 1)
            if isinstance(it, Token) and it.kind in (IDENT, QUOTED_NAME):
                bound = env.get(it.value.casefold())
                if bound is not None:
                    def_items, def_env = bound
                    return self.table(def_items, def_env, depth + 1)
                self.warn(_BARE_TABLE_WARNING.format(expr=_source(items)))
                return []
        col = _column_ref(items)
        if col is not None:
            # A lone column reference used as a table: one column.
            return [col]
        self.warn(f"unrecognised DAX table expression {_source(items)}; result columns unknown.")
        return []

    def _var_block(self, items: list[_Item], env: _Env, depth: int) -> list[Column]:
        n = len(items)
        i = 0
        env = dict(env)
        stops = frozenset({"VAR", "RETURN"})
        while i < n and _kw(items[i]) == "VAR":
            if i + 1 >= n or not isinstance(items[i + 1], Token):
                break
            name = items[i + 1].value
            j = i + 2
            if j < n and isinstance(items[j], Token) and items[j].value == "=":
                j += 1
            end = _consume_expr(items, j, stops)
            env[name.casefold()] = (items[j:end], dict(env))
            i = end
        if i < n and _kw(items[i]) == "RETURN":
            return self.table(items[i + 1 :], env, depth + 1)
        self.warn("DAX VAR block has no RETURN; result columns unknown.")
        return []

    # -- table functions --------------------------------------------------

    def _call(self, call: _Call, env: _Env, depth: int) -> list[Column]:
        name = call.name.value.upper()
        handler = _HANDLERS.get(name)
        if handler is None:
            self.warn(
                f"unrecognised DAX table function {call.name.value}(); result columns unknown."
            )
            return []
        return handler(self, call.args, env, depth + 1)

    def _arg_table(self, args: list[list[_Item]], idx: int, env: _Env, depth: int) -> list[Column]:
        if idx >= len(args):
            self.warn("DAX table function is missing its table argument; result columns unknown.")
            return []
        return self.table(args[idx], env, depth)

    def _pairs(
        self, args: list[list[_Item]], start: int, out: list[Column], allow_refs: bool
    ) -> None:
        """Collect ``"name", expr`` pairs (and, if ``allow_refs``, bare
        column references) from ``args[start:]`` into ``out``."""
        i = start
        while i < len(args):
            name = _string(args[i])
            if name is not None:
                if i + 1 >= len(args):
                    self.warn(f"DAX name {name!r} has no expression; column skipped.")
                    return
                out.append(_alias(name))
                i += 2
                continue
            col = _column_ref(args[i]) if allow_refs else None
            if col is not None:
                out.append(col)
            else:
                self.warn(
                    f"unrecognised DAX argument {_source(args[i])} where a "
                    '"name", expression pair was expected; skipped.'
                )
            i += 1

    def _group_refs(self, arg: list[_Item], out: list[Column]) -> bool:
        """Group-by argument: a column reference or ROLLUP*/ROLLUPGROUP
        wrapper. Returns False when ``arg`` is neither."""
        col = _column_ref(arg)
        if col is not None:
            out.append(col)
            return True
        if len(arg) == 1 and isinstance(arg[0], _Call):
            fname = arg[0].name.value.upper()
            if fname in ("ROLLUP", "ROLLUPGROUP"):
                for sub in arg[0].args:
                    self._group_refs(sub, out)
                return True
            if fname == "ROLLUPADDISSUBTOTAL":
                for sub in arg[0].args:
                    flag = _string(sub)
                    if flag is not None:
                        out.append(_alias(flag))
                    else:
                        # Group columns / ROLLUPGROUP; anything else is
                        # a filter-table argument and adds no column.
                        self._group_refs(sub, out)
                return True
        return False

    def summarizecolumns(self, args, env, depth) -> list[Column]:
        out: list[Column] = []
        i = 0
        while i < len(args):
            name = _string(args[i])
            if name is not None:
                if i + 1 >= len(args):
                    self.warn(f"DAX name {name!r} has no expression; column skipped.")
                    break
                out.append(_alias(name))
                i += 2
                continue
            # Group-by column, or a filter table (FILTER / VALUES /
            # TREATAS / KEEPFILTERS / a variable …) which adds nothing.
            self._group_refs(args[i], out)
            i += 1
        return out

    def selectcolumns(self, args, env, depth) -> list[Column]:
        out: list[Column] = []
        self._pairs(args, 1, out, allow_refs=True)
        return out

    def addcolumns(self, args, env, depth) -> list[Column]:
        out = list(self._arg_table(args, 0, env, depth))
        self._pairs(args, 1, out, allow_refs=False)
        return out

    def groupby(self, args, env, depth) -> list[Column]:
        # GROUPBY / SUMMARIZE: group-by refs then "name", expr pairs;
        # the input table's other columns are not part of the result.
        out: list[Column] = []
        i = 1
        while i < len(args):
            name = _string(args[i])
            if name is not None:
                if i + 1 >= len(args):
                    self.warn(f"DAX name {name!r} has no expression; column skipped.")
                    break
                out.append(_alias(name))
                i += 2
                continue
            if not self._group_refs(args[i], out):
                self.warn(f"unrecognised DAX group-by argument {_source(args[i])}; skipped.")
            i += 1
        return out

    def row(self, args, env, depth) -> list[Column]:
        out: list[Column] = []
        self._pairs(args, 0, out, allow_refs=False)
        return out

    def datatable(self, args, env, depth) -> list[Column]:
        # DATATABLE("Name", TYPE, "Name2", TYPE, {{...}})
        out: list[Column] = []
        for i in range(0, len(args) - 1, 2):
            name = _string(args[i])
            if name is not None:
                out.append(_alias(name))
        return out

    def columns_or_table(self, args, env, depth) -> list[Column]:
        # VALUES / DISTINCT / ALL / …: column arguments → those columns;
        # a single table argument → that table's columns.
        if len(args) == 1 and _column_ref(args[0]) is None:
            return self._arg_table(args, 0, env, depth)
        out: list[Column] = []
        for arg in args:
            col = _column_ref(arg)
            if col is None:
                self.warn(f"unrecognised DAX column argument {_source(arg)}; skipped.")
            else:
                out.append(col)
        return out

    def treatas(self, args, env, depth) -> list[Column]:
        out: list[Column] = []
        for arg in args[1:]:
            col = _column_ref(arg)
            if col is not None:
                out.append(col)
        return out

    def concat(self, args, env, depth) -> list[Column]:
        out: list[Column] = []
        for i in range(len(args)):
            out.extend(self._arg_table(args, i, env, depth))
        return out

    def natural_join(self, args, env, depth) -> list[Column]:
        out: list[Column] = []
        seen: set[str] = set()
        for col in self.concat(args, env, depth):
            if col.data_field not in seen:
                seen.add(col.data_field)
                out.append(col)
        return out

    def _table_constructor(self, group: _Group) -> list[Column]:
        # {1, 2} → [Value]; {(1, "a"), (2, "b")} → [Value1], [Value2].
        if not group.args:
            return [_alias("Value")]
        first = group.args[0]
        width = 1
        if len(first) == 1 and isinstance(first[0], _Group) and not first[0].brace:
            width = len(first[0].args)
        if width <= 1:
            return [_alias("Value")]
        return [_alias(f"Value{k}") for k in range(1, width + 1)]


def _pass_through(idx: int) -> Callable[..., list[Column]]:
    def handler(walker: _Walker, args, env, depth) -> list[Column]:
        return walker._arg_table(args, idx, env, depth)

    return handler


_HANDLERS: dict[str, Callable[..., list[Column]]] = {
    "SUMMARIZECOLUMNS": _Walker.summarizecolumns,
    "SELECTCOLUMNS": _Walker.selectcolumns,
    "ADDCOLUMNS": _Walker.addcolumns,
    "GROUPBY": _Walker.groupby,
    "SUMMARIZE": _Walker.groupby,
    "ROW": _Walker.row,
    "DATATABLE": _Walker.datatable,
    "TREATAS": _Walker.treatas,
    # Column-or-table argument.
    "VALUES": _Walker.columns_or_table,
    "DISTINCT": _Walker.columns_or_table,
    "ALL": _Walker.columns_or_table,
    "ALLNOBLANKROW": _Walker.columns_or_table,
    "ALLSELECTED": _Walker.columns_or_table,
    # Table passes through from one argument.
    "CALCULATETABLE": _pass_through(0),
    "FILTER": _pass_through(0),
    "KEEPFILTERS": _pass_through(0),
    "UNION": _pass_through(0),
    "INTERSECT": _pass_through(0),
    "EXCEPT": _pass_through(0),
    "TOPN": _pass_through(1),  # TOPN(n, table, orderBy…)
    "SAMPLE": _pass_through(1),  # SAMPLE(n, table, orderBy…)
    "TOPNSKIP": _pass_through(2),  # TOPNSKIP(rows, skip, table, …)
    # Joins: columns of every input.
    "CROSSJOIN": _Walker.concat,
    "GENERATE": _Walker.concat,
    "GENERATEALL": _Walker.concat,
    "NATURALINNERJOIN": _Walker.natural_join,
    "NATURALLEFTOUTERJOIN": _Walker.natural_join,
}


def _dedupe(msgs: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in msgs:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def result_columns(dax: str) -> ResultColumns:
    """Work out the result-set columns of a DAX query.

    * A leading ``DEFINE`` block is skipped, except that its ``VAR`` and
      ``TABLE`` definitions are remembered so ``EVALUATE _var`` resolves.
    * The last top-level ``EVALUATE`` is analysed (a warning is added
      when there is more than one); its ``ORDER BY`` / ``START AT``
      clauses are ignored.
    * ``VAR ... RETURN`` is resolved by substituting the named table
      expressions.
    * Only the outermost table expression is walked:

      - ``SUMMARIZECOLUMNS`` — group-by column references, then
        ``"name", expr`` pairs; filter-table arguments are skipped.
      - ``SELECTCOLUMNS`` / ``ADDCOLUMNS`` / ``GROUPBY`` /
        ``SUMMARIZE`` / ``ROW`` — depth-1 ``"name", expr`` pairs;
        ADDCOLUMNS keeps its input table's columns, GROUPBY /
        SUMMARIZE their group-by references.
      - ``CALCULATETABLE`` / ``FILTER`` / ``KEEPFILTERS`` (first
        argument), ``TOPN`` / ``SAMPLE`` (second), … pass their table
        through.

    * A bare ``EVALUATE 'Table'`` or any unrecognised shape yields no
      columns for that part plus a warning.

    Never raises.
    """
    warnings: list[str] = []
    for t in tokenize(dax or ""):
        if not t.terminated:
            what = {STRING: "string", QUOTED_NAME: "quoted name", BRACKET: "bracket"}[t.kind]
            warnings.append(f"DAX has an unterminated {what} at offset {t.start}.")
            break
    try:
        columns = _analyse(dax or "", warnings)
    except RecursionError:
        warnings.append("DAX expression nests too deeply to analyse; result columns unknown.")
        columns = []
    if not columns and not warnings:
        warnings.append(
            "no result-set columns could be determined from the DAX query; use "
            "add_dataset_field explicitly per column."
        )
    return ResultColumns(columns, _dedupe(warnings))


def _analyse(dax: str, warnings: list[str]) -> list[Column]:
    items = _group_tokens(tokenize(dax), warnings)
    n = len(items)
    env: _Env = {}
    i = 0
    if i < n and _kw(items[i]) == "DEFINE":
        i += 1
        define_stops = _DEFINE_ITEM_KEYWORDS | {"EVALUATE", "DEFINE"}
        while i < n and _kw(items[i]) != "EVALUATE":
            kw = _kw(items[i])
            if kw not in _DEFINE_ITEM_KEYWORDS:
                i += 1
                continue
            # Find the "=" that opens the definition body.
            j = i + 1
            while j < n and not (isinstance(items[j], Token) and items[j].value == "="):
                if _is_stop(items, j, define_stops):
                    break
                j += 1
            if j >= n or not (isinstance(items[j], Token) and items[j].value == "="):
                i = j
                continue
            end = _consume_expr(items, j + 1, define_stops)
            if kw in ("VAR", "TABLE") and isinstance(items[i + 1], Token):
                env[items[i + 1].value.casefold()] = (items[j + 1 : end], dict(env))
            i = end

    evaluates: list[int] = []
    stops = frozenset({"EVALUATE", "DEFINE", "ORDER", "START"})
    while i < n:
        if _kw(items[i]) == "EVALUATE":
            evaluates.append(i)
            i = _consume_expr(items, i + 1, stops)
        else:
            i += 1
    if not evaluates:
        warnings.append(
            "DAX has no EVALUATE statement; result columns unknown. Use "
            "add_dataset_field explicitly per column."
        )
        return []
    if len(evaluates) > 1:
        warnings.append(
            f"DAX has {len(evaluates)} EVALUATE statements; only the last one's "
            "columns are reported."
        )
    start = evaluates[-1] + 1
    end = _consume_expr(items, start, stops)
    return _Walker(warnings).table(items[start:end], env)
