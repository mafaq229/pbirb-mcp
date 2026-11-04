"""Lightweight tokenizer for RDL (Visual Basic) expressions.

RDL expressions are VB.NET expressions prefixed with ``=``. Lint rules
that used to scan the raw text with regexes misfired on perfectly valid
content — a ``)`` inside a string literal counted as an unbalanced
paren, ``Fields!X.Value`` inside a string or comment counted as a field
reference, and a genuinely unterminated string literal passed. This
module gives those rules (and later ones: an expression walker,
field-scope checks) a shared, dependency-free lexer.

It is deliberately a *lexer*, not a parser: it splits text into
:class:`Token` objects and never raises. Malformed input surfaces as an
``error`` token (currently: an unterminated string literal) so callers
decide how to report it.

Public API::

    tokenize(code, start=0, include_whitespace=False) -> list[Token]
    tokenize_expression(text) -> list[Token] | None   # None: not an expression
    expression_body(text) -> str | None
    significant(tokens) -> list[Token]                # drop whitespace/comments
    string_value(token) -> str                        # decode a string literal
    unterminated_string(text_or_tokens) -> Token | None
    first_unbalanced_delimiter(text_or_tokens) -> Token | None
    balanced_delimiters(text_or_tokens) -> bool
    delimiter_family(token) -> str                    # "parentheses" | ...
    bang_references(text_or_tokens, collection) -> list[BangRef]

Token kinds (module constants): ``STRING``, ``DATE``, ``NUMBER``,
``IDENTIFIER``, ``OPERATOR``, ``DELIMITER``, ``COMMENT``,
``WHITESPACE``, ``UNKNOWN``, ``ERROR``.

Lexical rules covered (VB language spec, the subset that matters for
RDL):

* String literals delimited by ``"`` (or the typographic ``“``/``”``
  that VB also accepts), with ``""`` as the embedded-quote escape and an
  optional ``c`` char-literal suffix.
* Comments: ``'`` (and ``‘``/``’``) or the ``REM`` keyword, running to
  end of line. ``REM`` after ``.``, ``!`` or ``[`` is a member name, not
  a comment.
* Date literals ``#1/31/2024#`` / ``#12:00 PM#``.
* Numbers, including ``&H``/``&O``/``&B`` prefixes and type suffixes.
* Identifiers (Unicode letters, digits, ``_``; optional ``$`` type char
  as in ``Format$``). Keywords are reported as identifiers.
* Operators, including compound ``<>``, ``<=``, ``>=`` (VB allows
  whitespace between the two characters), ``:=``, ``<<``, ``>>`` and
  the ``op=`` assignment forms, plus ``.`` and the ``!`` dictionary
  access operator (``Fields!Name``).
* Delimiters ``( ) [ ] { } ,``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple, Optional, Union

# ---- token kinds ---------------------------------------------------------

STRING = "string"
DATE = "date"
NUMBER = "number"
IDENTIFIER = "identifier"
OPERATOR = "operator"
DELIMITER = "delimiter"
COMMENT = "comment"
WHITESPACE = "whitespace"
UNKNOWN = "unknown"
ERROR = "error"


class Token(NamedTuple):
    """One lexical token. ``start``/``end`` are offsets into the text that
    was tokenized (``text[start:end] == token.text``). For ``ERROR``
    tokens, ``message`` says what went wrong; it is ``""`` otherwise.
    """

    kind: str
    text: str
    start: int
    end: int
    message: str = ""


class BangRef(NamedTuple):
    """A ``Collection!Name[.Member]`` reference such as ``Fields!Sales.Value``.

    ``first``/``last`` index the first and last token of the reference in
    the *significant* token list (see :func:`significant`), so callers can
    look at the neighbouring tokens. ``member`` is ``None`` when the
    reference has no ``.Member`` suffix.
    """

    collection: str
    name: str
    member: Optional[str]
    start: int
    end: int
    first: int
    last: int


TextOrTokens = Union[str, Sequence[Token]]

# ---- lexer tables --------------------------------------------------------

_DQUOTES = frozenset('"“”')
_SQUOTES = frozenset("'‘’")
_NEWLINES = frozenset("\r\n  ")

_WHITESPACE_RE = re.compile(r"[^\S\r\n  ]+|(?:\r\n|[\r\n  ])+")
_IDENT_RE = re.compile(r"[^\W\d]\w*\$?")
_PREFIXED_NUMBER_RE = re.compile(
    r"&(?:[Hh][0-9A-Fa-f]+|[Oo][0-7]+|[Bb][01]+)(?:[Uu]?[SsIiLl])?(?!\w)"
)
_DECIMAL_NUMBER_RE = re.compile(
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?(?:[Uu][SsIiLl]|[DdFfRrSsIiLl@!#%&])?(?!\w)"
)
# Content of a date literal: digits, separators, spaces, AM/PM.
_DATE_RE = re.compile(r"#([0-9/:.\- \tAaPpMm]*\d[0-9/:.\- \tAaPpMm]*)#")
_OPERATOR_RE = re.compile(
    r"<<=|>>=|<<|>>"
    r"|<[ \t]*>|<[ \t]*=|>[ \t]*="
    r"|:=|\?\."
    r"|[-+*/\\^&]="
    r"|[-+*/\\^&=<>.!:?]"
)
_DELIMITERS = frozenset("()[]{},")
_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}
_FAMILY = {
    "(": "parentheses",
    ")": "parentheses",
    "[": "brackets",
    "]": "brackets",
    "{": "braces",
    "}": "braces",
}


def _line_end(code: str, pos: int) -> int:
    n = len(code)
    while pos < n and code[pos] not in _NEWLINES:
        pos += 1
    return pos


def _scan_string(code: str, pos: int) -> Token:
    """Scan a string literal whose opening quote is at ``pos``."""
    n = len(code)
    i = pos + 1
    while i < n:
        if code[i] in _DQUOTES:
            if i + 1 < n and code[i + 1] in _DQUOTES:
                i += 2  # "" escape
                continue
            end = i + 1
            # Char-literal suffix: "a"c
            if end < n and code[end] in "cC" and not (end + 1 < n and _is_word(code[end + 1])):
                end += 1
            return Token(STRING, code[pos:end], pos, end)
        i += 1
    return Token(
        ERROR,
        code[pos:],
        pos,
        n,
        f"unterminated string literal starting at offset {pos}",
    )


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def tokenize(code: str, start: int = 0, include_whitespace: bool = False) -> list[Token]:
    """Split VB ``code`` into tokens, beginning at offset ``start``.

    Offsets in the returned tokens are relative to ``code`` (not to
    ``start``). Whitespace tokens are dropped unless
    ``include_whitespace`` is true. Never raises: an unterminated string
    literal becomes a single ``ERROR`` token running to the end of the
    text.
    """
    tokens: list[Token] = []
    n = len(code)
    pos = start
    prev: Optional[Token] = None  # previous non-whitespace token

    def emit(tok: Token) -> None:
        nonlocal prev
        if tok.kind == WHITESPACE:
            if include_whitespace:
                tokens.append(tok)
            return
        tokens.append(tok)
        prev = tok

    while pos < n:
        ch = code[pos]

        m = _WHITESPACE_RE.match(code, pos)
        if m:
            emit(Token(WHITESPACE, m.group(0), pos, m.end()))
            pos = m.end()
            continue

        if ch in _DQUOTES:
            tok = _scan_string(code, pos)
            emit(tok)
            pos = tok.end
            continue

        if ch in _SQUOTES:
            end = _line_end(code, pos)
            emit(Token(COMMENT, code[pos:end], pos, end))
            pos = end
            continue

        m = _IDENT_RE.match(code, pos)
        if m:
            word = m.group(0)
            is_member = prev is not None and prev.text in (".", "!", "[") and prev.end == pos
            if word.lower() == "rem" and not is_member:
                end = _line_end(code, pos)
                emit(Token(COMMENT, code[pos:end], pos, end))
                pos = end
                continue
            emit(Token(IDENTIFIER, word, pos, m.end()))
            pos = m.end()
            continue

        if ch == "#":
            # A '#' glued to an identifier/number is a type character,
            # not the start of a date literal.
            glued = prev is not None and prev.end == pos and prev.kind in (IDENTIFIER, NUMBER)
            m = None if glued else _DATE_RE.match(code, pos)
            if m:
                emit(Token(DATE, m.group(0), pos, m.end()))
                pos = m.end()
                continue
            emit(Token(UNKNOWN, ch, pos, pos + 1))
            pos += 1
            continue

        m = _PREFIXED_NUMBER_RE.match(code, pos) if ch == "&" else None
        if m is None and (ch.isdigit() or (ch == "." and pos + 1 < n and code[pos + 1].isdigit())):
            m = _DECIMAL_NUMBER_RE.match(code, pos)
        if m:
            emit(Token(NUMBER, m.group(0), pos, m.end()))
            pos = m.end()
            continue

        if ch in _DELIMITERS:
            emit(Token(DELIMITER, ch, pos, pos + 1))
            pos += 1
            continue

        m = _OPERATOR_RE.match(code, pos)
        if m:
            emit(Token(OPERATOR, m.group(0), pos, m.end()))
            pos = m.end()
            continue

        emit(Token(UNKNOWN, ch, pos, pos + 1))
        pos += 1

    return tokens


_EXPR_PREFIX_RE = re.compile(r"\s*=")


def expression_body(text: Optional[str]) -> Optional[str]:
    """Return the VB code after the leading ``=``, or ``None`` when ``text``
    is not an RDL expression (a plain literal value, or empty).
    """
    if not text:
        return None
    m = _EXPR_PREFIX_RE.match(text)
    if not m:
        return None
    return text[m.end() :]


def tokenize_expression(text: Optional[str], include_whitespace: bool = False):
    """Tokenize an RDL expression (``=...``), skipping the leading ``=``.

    Returns ``None`` when ``text`` is not an expression. Token offsets
    are relative to the full ``text`` including the ``=`` prefix.
    """
    if not text:
        return None
    m = _EXPR_PREFIX_RE.match(text)
    if not m:
        return None
    return tokenize(text, start=m.end(), include_whitespace=include_whitespace)


def _as_tokens(text_or_tokens: TextOrTokens) -> Sequence[Token]:
    if isinstance(text_or_tokens, str):
        return tokenize(text_or_tokens)
    return text_or_tokens


def significant(text_or_tokens: TextOrTokens) -> list[Token]:
    """Tokens that carry meaning: drops whitespace and comments."""
    return [t for t in _as_tokens(text_or_tokens) if t.kind not in (WHITESPACE, COMMENT)]


def string_value(token: Token) -> str:
    """Decode a ``STRING`` token to its runtime value (strip the quotes,
    collapse ``""`` escapes, drop a ``c`` suffix).
    """
    if token.kind != STRING:
        raise ValueError(f"not a string token: {token.kind}")
    text = token.text
    if text[-1] in "cC":
        text = text[:-1]
    inner = text[1:-1]
    return re.sub('["“”]{2}', lambda m: m.group(0)[0], inner)


def unterminated_string(text_or_tokens: TextOrTokens) -> Optional[Token]:
    """Return the ``ERROR`` token for an unterminated string literal, or
    ``None`` when every literal is closed.
    """
    for tok in _as_tokens(text_or_tokens):
        if tok.kind == ERROR:
            return tok
    return None


def first_unbalanced_delimiter(text_or_tokens: TextOrTokens) -> Optional[Token]:
    """Find the first delimiter that breaks nesting, ignoring anything in
    string/date literals and comments.

    Returns the offending token: a closer with no (or the wrong) opener,
    or — when the text ends with openers still pending — the innermost
    unclosed opener. ``None`` when ``()``, ``[]`` and ``{}`` all nest
    correctly.
    """
    stack: list[Token] = []
    for tok in _as_tokens(text_or_tokens):
        if tok.kind != DELIMITER:
            continue
        if tok.text in _OPENERS:
            stack.append(tok)
        elif tok.text in _CLOSERS:
            if not stack or stack[-1].text != _CLOSERS[tok.text]:
                return tok
            stack.pop()
    return stack[-1] if stack else None


def balanced_delimiters(text_or_tokens: TextOrTokens) -> bool:
    """True iff ``()``, ``[]`` and ``{}`` nest correctly outside literals."""
    return first_unbalanced_delimiter(text_or_tokens) is None


def delimiter_family(token: Token) -> str:
    """``"parentheses"``, ``"brackets"`` or ``"braces"`` for a delimiter token."""
    try:
        return _FAMILY[token.text]
    except KeyError:
        raise ValueError(f"not a bracketing delimiter: {token.text!r}") from None


def bang_references(text_or_tokens: TextOrTokens, collection: str) -> list[BangRef]:
    """Find ``collection!Name`` references (optionally followed by
    ``.Member``), e.g. ``bang_references(toks, "Fields")`` for
    ``Fields!Sales.Value``.

    The collection name matches case-insensitively (VB is
    case-insensitive); ``name`` and ``member`` are returned as written.
    References inside string literals and comments are never reported.
    """
    sig = significant(text_or_tokens)
    want = collection.lower()
    out: list[BangRef] = []
    i = 0
    n = len(sig)
    while i + 2 < n:
        a, bang, name = sig[i], sig[i + 1], sig[i + 2]
        if (
            a.kind == IDENTIFIER
            and a.text.lower() == want
            and bang.kind == OPERATOR
            and bang.text == "!"
            and name.kind == IDENTIFIER
            and not (i > 0 and sig[i - 1].kind == OPERATOR and sig[i - 1].text in (".", "!"))
        ):
            member: Optional[str] = None
            last = i + 2
            if i + 4 < n:
                dot, mem = sig[i + 3], sig[i + 4]
                if dot.kind == OPERATOR and dot.text == "." and mem.kind == IDENTIFIER:
                    member = mem.text
                    last = i + 4
            out.append(
                BangRef(
                    collection=a.text,
                    name=name.text,
                    member=member,
                    start=a.start,
                    end=sig[last].end,
                    first=i,
                    last=last,
                )
            )
            i = last + 1
            continue
        i += 1
    return out


__all__ = [
    "BangRef",
    "COMMENT",
    "DATE",
    "DELIMITER",
    "ERROR",
    "IDENTIFIER",
    "NUMBER",
    "OPERATOR",
    "STRING",
    "Token",
    "UNKNOWN",
    "WHITESPACE",
    "balanced_delimiters",
    "bang_references",
    "delimiter_family",
    "expression_body",
    "first_unbalanced_delimiter",
    "significant",
    "string_value",
    "tokenize",
    "tokenize_expression",
    "unterminated_string",
]
