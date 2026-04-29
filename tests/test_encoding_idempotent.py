"""Tests for the idempotent text-encoding helper plus regression tests
covering every text-writing tool that previously double-encoded.

The bug class (RAG-Report feedback #4): a tool that takes user text via
parameter and writes it to an XML element via ``element.text = value``
double-encodes when the user supplies already-encoded entities. v0.1.3
fixed this for textbox/expression fields; v0.2 added new writers that
bypassed the helper. v0.3 centralises the fix in
``pbirb_mcp.core.encoding`` and routes every text writer through it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.dataset import update_dataset_query
from pbirb_mcp.ops.header_footer import add_header_textbox
from pbirb_mcp.ops.parameters import set_parameter_prompt
from pbirb_mcp.ops.tablix import add_tablix_filter
from pbirb_mcp.ops.visibility import set_element_visibility

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


# ---- unit tests for encode_text ------------------------------------------


class TestEncodeText:
    def test_passes_raw_text_unchanged(self):
        assert encode_text("A & B") == "A & B"
        assert encode_text("foo<bar>") == "foo<bar>"
        assert encode_text("she said \"hi\"") == "she said \"hi\""

    def test_decodes_named_entities(self):
        assert encode_text("A &amp; B") == "A & B"
        assert encode_text("foo &lt;bar&gt;") == "foo <bar>"
        assert encode_text("&quot;hi&quot;") == '"hi"'
        assert encode_text("it&apos;s") == "it's"

    def test_decodes_numeric_decimal_entities(self):
        # &#38; = '&'
        assert encode_text("A &#38; B") == "A & B"
        # &#60; = '<'
        assert encode_text("&#60;tag&#62;") == "<tag>"

    def test_decodes_numeric_hex_entities(self):
        # &#x26; = '&'
        assert encode_text("A &#x26; B") == "A & B"
        # &#x3C; = '<'
        assert encode_text("&#x3C;tag&#x3E;") == "<tag>"

    def test_idempotent_decoded_output(self):
        # Result of encode_text(encode_text(x)) must equal encode_text(x).
        for original in [
            "A &amp; B",
            "&lt;foo&gt;",
            "raw & text",
            "&#38; numeric",
            "&#x26; hex",
            "",
            "no entities here",
        ]:
            once = encode_text(original)
            twice = encode_text(once)
            assert once == twice, f"not idempotent on {original!r}: {once!r} → {twice!r}"

    def test_empty_and_none(self):
        assert encode_text("") == ""
        # None passes through untouched (caller's contract).
        assert encode_text(None) is None  # type: ignore[arg-type]

    def test_does_not_double_decode_amp_lt(self):
        # &amp;lt; was a literal ``&lt;`` in the source; it must NOT decode
        # to ``<`` (that would be double-decoding).
        assert encode_text("&amp;lt;") == "&lt;"
        assert encode_text("&amp;amp;") == "&amp;"


# ---- regression tests: write user-supplied text via tools ---------------


def _saved_disk_bytes(path: Path) -> bytes:
    """Return raw on-disk bytes of the saved RDL — what Report Builder
    actually reads. We assert against bytes (not parsed XML) so we catch
    over-encoding even when the parser would 'unbreak' it."""
    return path.read_bytes()


class TestRegressionAddBodyTextbox:
    def test_raw_ampersand_encoded_once(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="AmpRaw",
            text="A & B",
            top="0in",
            left="0in",
            width="2in",
            height="0.25in",
        )
        assert b"&amp;amp;" not in _saved_disk_bytes(rdl_path)
        assert b"A &amp; B" in _saved_disk_bytes(rdl_path)

    def test_pre_encoded_ampersand_not_double_encoded(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="AmpPre",
            text="A &amp; B",
            top="0in",
            left="0in",
            width="2in",
            height="0.25in",
        )
        # Disk must show A &amp; B (single encoding), never &amp;amp;.
        assert b"&amp;amp;" not in _saved_disk_bytes(rdl_path)
        assert b"A &amp; B" in _saved_disk_bytes(rdl_path)

    def test_pre_encoded_lt_gt_not_double_encoded(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="LtGt",
            text="&lt;tag&gt;",
            top="0in",
            left="0in",
            width="2in",
            height="0.25in",
        )
        assert b"&amp;lt;" not in _saved_disk_bytes(rdl_path)
        assert b"&amp;gt;" not in _saved_disk_bytes(rdl_path)
        assert b"&lt;tag&gt;" in _saved_disk_bytes(rdl_path)


class TestRegressionAddHeaderTextbox:
    def test_pre_encoded_ampersand(self, rdl_path):
        add_header_textbox(
            path=str(rdl_path),
            name="HeaderAmp",
            text="From: A &amp; To: B",
            top="0in",
            left="0in",
            width="3in",
            height="0.25in",
        )
        assert b"&amp;amp;" not in _saved_disk_bytes(rdl_path)


class TestRegressionParameterPrompt:
    def test_pre_encoded_prompt(self, rdl_path):
        # DateFrom is the first parameter in the fixture.
        set_parameter_prompt(
            path=str(rdl_path),
            name="DateFrom",
            prompt="Range &amp; Selection",
        )
        assert b"&amp;amp;" not in _saved_disk_bytes(rdl_path)
        assert b"Range &amp; Selection" in _saved_disk_bytes(rdl_path)


class TestRegressionDatasetQuery:
    def test_pre_encoded_command_text(self, rdl_path):
        # DAX queries CAN contain ampersands in string literals (e.g.
        # FILTER('Sales', 'Sales'[Region] = "A&B")). If the LLM sends
        # &amp; in the DAX, we must not double-encode it.
        update_dataset_query(
            path=str(rdl_path),
            dataset_name="MainDataset",
            dax_body='EVALUATE FILTER(\'Sales\', \'Sales\'[Region] = "A &amp; B")',
        )
        assert b"&amp;amp;" not in _saved_disk_bytes(rdl_path)
        assert b'"A &amp; B"' in _saved_disk_bytes(rdl_path)


class TestRegressionVisibilityExpression:
    def test_pre_encoded_expression(self, rdl_path):
        # Visibility expressions often use comparison operators that LLMs
        # might pre-encode (`&gt;`, `&lt;`).
        set_element_visibility(
            path=str(rdl_path),
            element_name="MainTable",
            hidden_expression="=Parameters!DateFrom.Value &gt; Parameters!DateTo.Value",
        )
        assert b"&amp;gt;" not in _saved_disk_bytes(rdl_path)
        assert b"&gt;" in _saved_disk_bytes(rdl_path)


class TestRegressionTablixFilter:
    def test_pre_encoded_filter_value(self, rdl_path):
        add_tablix_filter(
            path=str(rdl_path),
            tablix_name="MainTable",
            expression="=Fields!ProductName.Value",
            operator="Equal",
            values=["A &amp; B"],
        )
        assert b"&amp;amp;" not in _saved_disk_bytes(rdl_path)


class TestParserRoundTrip:
    """Parsing the saved file must yield the original literal text."""

    def test_parsed_value_matches_intent(self, rdl_path):
        add_body_textbox(
            path=str(rdl_path),
            name="ParseCheck",
            text="A &amp; B",  # already-encoded user input
            top="0in",
            left="0in",
            width="2in",
            height="0.25in",
        )
        # Re-parse the file. The textbox value, decoded by lxml, should
        # be the literal "A & B" — what the user intended.
        doc = RDLDocument.open(rdl_path)
        nodes = doc.root.xpath(
            ".//r:Textbox[@Name='ParseCheck']//r:Value",
            namespaces={"r": RDL_NS},
        )
        assert len(nodes) == 1
        assert nodes[0].text == "A & B"

    def test_round_trip_byte_identical_to_fixture_unchanged(self, tmp_path):
        """Loading and re-saving the unmodified fixture stays byte-identical."""
        dest = tmp_path / "report.rdl"
        shutil.copy(FIXTURE, dest)
        original = dest.read_bytes()
        RDLDocument.open(dest).save()
        assert dest.read_bytes() == original
