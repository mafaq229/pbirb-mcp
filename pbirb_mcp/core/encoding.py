"""Idempotent text encoding for RDL writes.

lxml automatically encodes XML entities (``&`` → ``&amp;`` etc.) on
serialization. That's correct for raw text input. The bug surfaces when
callers — usually LLMs trying to be helpful — supply *already-encoded*
text like ``"A &amp; B"``: lxml encodes the leading ``&`` again,
producing ``"A &amp;amp; B"`` on disk, which Report Builder unescapes
once and surfaces as ``'amp' is not declared`` in the VB.NET expression
parser.

This module exposes :func:`encode_text` — an idempotent normaliser that
detects supplied entities and decodes them to their literal characters
before lxml gets a chance to re-encode. The end result on disk is always
correct: one encoding pass, regardless of the input shape.

The fix originally landed in v0.1.3 for textbox/expression fields. v0.2
added new writers (chart text, parameter prompts, run values, etc.) that
bypassed it. v0.3.0 centralises the helper and routes every text-writing
op through it so the bug class can't regress.

Usage::

    from pbirb_mcp.core.encoding import encode_text
    value_node.text = encode_text(user_supplied_text)

The helper is a no-op for any text without numeric or named entities; it
preserves leading/trailing whitespace and never strips empty strings.
"""

from __future__ import annotations

import re

# Five core XML named entities. ``&apos;`` is technically only required
# inside single-quoted attribute values, but lxml emits it sometimes;
# normalising it here keeps round-trips clean.
_NAMED_ENTITIES: tuple[tuple[str, str], ...] = (
    ("&amp;", "&"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&apos;", "'"),
)

# Numeric character references — decimal (``&#38;``) and hex (``&#x26;``).
_NUMERIC_ENTITY_RE = re.compile(r"&#(?:x[0-9a-fA-F]+|[0-9]+);")


def _decode_numeric_entity(match: re.Match[str]) -> str:
    raw = match.group(0)
    body = raw[2:-1]
    try:
        codepoint = int(body[1:], 16) if body.startswith(("x", "X")) else int(body)
        return chr(codepoint)
    except (ValueError, OverflowError):
        # If we can't decode, leave the entity alone — lxml will encode
        # the leading ``&`` and we'll have ``&amp;#xZZ;`` on disk, which
        # is wrong but no worse than before. Surfacing a hard error from a
        # purely defensive helper would break callers passing literal
        # ``&#x...;``-shaped strings that aren't actually entities.
        return raw


def encode_text(value: str) -> str:
    """Normalise user-supplied text so lxml's auto-encoding produces
    correct output regardless of whether the input was already encoded.

    Idempotent: calling with already-decoded text returns it unchanged.
    Specifically, this function decodes:

    - Named entities ``&amp; &lt; &gt; &quot; &apos;``
    - Numeric character references ``&#NN;`` (decimal) and ``&#xHH;`` (hex)

    so lxml will re-encode them exactly once on serialization.

    Edge cases:
    - ``None`` or empty string → returned as-is
    - Non-string input → returned unchanged (caller's responsibility)
    """
    if not isinstance(value, str) or not value:
        return value

    out = value
    # Numeric entities first so a numeric ``&#38;`` is decoded to literal
    # ``&`` rather than colliding with ``&amp;`` decoding below.
    out = _NUMERIC_ENTITY_RE.sub(_decode_numeric_entity, out)
    # ``&amp;`` MUST be decoded last; otherwise an input ``&amp;lt;`` becomes
    # literal ``&lt;`` which then matches the &lt; rule and decodes to ``<``,
    # which is double-decoding. The right order is: decode the others first
    # (they only fire on truly-encoded input), then ``&amp;`` last.
    for encoded, literal in reversed(_NAMED_ENTITIES):
        out = out.replace(encoded, literal)
    return out


__all__ = ["encode_text"]
