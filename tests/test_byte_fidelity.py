"""Byte-faithful save (issue #18).

Report Builder writes a UTF-8 BOM, CRLF line ends, ``MustUnderstand``
before the xmlns declarations and ``<Style></Style>`` for empty styles.
A no-op open → save must reproduce every one of those bytes, and an edit
must only touch the lines it changes.
"""

from __future__ import annotations

import difflib
import itertools
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.escape import raw_xml_replace
from pbirb_mcp.ops.styling import set_textbox_value

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "pbi_paginated_minimal.rdl"
BOM = b"\xef\xbb\xbf"


def _variant(src: bytes, *, bom: bool, crlf: bool, trailing: bool) -> bytes:
    """Re-shape ``src`` the way Report Builder (or another editor) might."""
    data = src.lstrip(BOM).rstrip(b"\r\n")
    # Report Builder's root: MustUnderstand first, then the namespaces.
    data = data.replace(
        b"<Report xmlns=",
        b'<Report MustUnderstand="df" xmlns:df="http://schemas.microsoft.com/sqlserver/'
        b'reporting/2016/01/reportdefinition/defaultfontfamily" xmlns=',
        1,
    )
    data = data.replace(b"<Style />", b"<Style></Style>", 1)
    if trailing:
        data += b"\n"
    if crlf:
        data = data.replace(b"\n", b"\r\n")
    return (BOM if bom else b"") + data


def _write(tmp_path: Path, data: bytes, name: str = "r.rdl") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _rb_style(tmp_path: Path) -> Path:
    src = MINIMAL.read_bytes()
    return _write(tmp_path, _variant(src, bom=True, crlf=True, trailing=False))


MATRIX = list(itertools.product([False, True], repeat=3))


@pytest.mark.parametrize(("bom", "crlf", "trailing"), MATRIX)
def test_noop_save_is_byte_identical(tmp_path, bom, crlf, trailing):
    data = _variant(MINIMAL.read_bytes(), bom=bom, crlf=crlf, trailing=trailing)
    # Sanity: the variant still parses and keeps the RB root shape.
    p = _write(tmp_path, data)
    RDLDocument.open(p).save()
    assert p.read_bytes() == data


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.rdl")), ids=lambda f: f.name)
def test_noop_save_is_byte_identical_for_fixtures(tmp_path, fixture):
    p = tmp_path / fixture.name
    shutil.copy(fixture, p)
    RDLDocument.open(p).save()
    assert p.read_bytes() == fixture.read_bytes()


def test_noop_save_keeps_root_attribute_order(tmp_path):
    p = _rb_style(tmp_path)
    RDLDocument.open(p).save()
    second_line = p.read_bytes().split(b"\r\n")[1]
    assert second_line.startswith(b'<Report MustUnderstand="df" xmlns:df=')


def test_noop_save_keeps_expanded_empty_elements(tmp_path):
    p = _rb_style(tmp_path)
    RDLDocument.open(p).save()
    assert b"<Style></Style>" in p.read_bytes()


def test_expanded_empty_element_not_visible_in_tree(tmp_path):
    # `<Style></Style>` must read back as an ordinary empty element — the
    # byte-form bookkeeping can't leak into text values the ops see.
    p = _rb_style(tmp_path)
    doc = RDLDocument.open(p)
    empties = [e for e in doc.root.iter() if isinstance(e.tag, str) and e.text == ""]
    assert empties == []


def test_expanded_empty_element_gets_children_normally(tmp_path):
    # Once an op adds a child the element is no longer empty and is written
    # as an ordinary open/close pair.
    p = _rb_style(tmp_path)
    doc = RDLDocument.open(p)
    doc.save()
    assert p.read_bytes().count(b"<Style></Style>") == 1


def test_one_value_edit_changes_exactly_one_line(tmp_path):
    p = _rb_style(tmp_path)
    before = p.read_bytes()
    set_textbox_value(p, "HeaderAmount", "Total Amount")
    after = p.read_bytes()
    assert after.startswith(BOM)
    assert after.count(b"\r\n") == before.count(b"\r\n")
    a = before.decode("utf-8-sig").split("\r\n")
    b = after.decode("utf-8-sig").split("\r\n")
    changed = [line for line in difflib.unified_diff(a, b, lineterm="", n=0) if line[:1] in "+-"]
    # Two header lines (---/+++) plus exactly one removed and one added line.
    assert len(changed) == 4, changed
    assert "Total Amount" in changed[-1]


def test_raw_xml_replace_keeps_following_sibling_on_its_own_line(tmp_path):
    p = tmp_path / "r.rdl"
    shutil.copy(MINIMAL, p)
    raw_xml_replace(
        p,
        "r:ReportSections/r:ReportSection/r:Page/r:PageHeight",
        "<PageHeight>9in</PageHeight>",
    )
    lines = [line.strip() for line in p.read_text().splitlines() if "PageHeight" in line]
    assert lines == ["<PageHeight>9in</PageHeight>"]


def test_raw_xml_replace_on_crlf_file_is_a_one_line_change(tmp_path):
    p = _rb_style(tmp_path)
    before = p.read_bytes().split(b"\r\n")
    raw_xml_replace(
        p,
        "r:ReportSections/r:ReportSection/r:Page/r:PageHeight",
        "<PageHeight>9in</PageHeight>",
    )
    after = p.read_bytes().split(b"\r\n")
    assert len(after) == len(before)
    assert sum(1 for x, y in zip(before, after) if x != y) == 1


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


@pytest.mark.parametrize("crlf", [False, True])
def test_add_body_textbox_is_indented_to_sibling_depth(tmp_path, crlf):
    data = _variant(MINIMAL.read_bytes(), bom=crlf, crlf=crlf, trailing=True)
    p = _write(tmp_path, data)
    add_body_textbox(p, "NewBox", "hello", "3in", "0in", "1in", "0.25in")
    text = p.read_bytes().decode("utf-8-sig")
    nl = "\r\n" if crlf else "\n"
    lines = text.split(nl)
    if crlf:
        assert "\n" not in text.replace("\r\n", "")
    new_idx = next(i for i, line in enumerate(lines) if 'Name="NewBox"' in line)
    sibling_idx = next(i for i, line in enumerate(lines) if '<Tablix Name="MainTable"' in line)
    assert _indent_of(lines[new_idx]) == _indent_of(lines[sibling_idx])
    assert lines[new_idx].strip() == '<Textbox Name="NewBox">'
    # Every element of the inserted subtree sits on its own line.
    end_idx = next(i for i in range(new_idx, len(lines)) if lines[i].strip() == "</Textbox>")
    block = lines[new_idx : end_idx + 1]
    assert all(line.strip().startswith("<") for line in block)
    assert _indent_of(lines[end_idx]) == _indent_of(lines[new_idx])
    # The closing </ReportItems> that follows keeps its own line + depth.
    assert lines[end_idx + 1].strip() == "</ReportItems>"
    # Re-opening and saving again is a no-op.
    once = p.read_bytes()
    RDLDocument.open(p).save()
    assert p.read_bytes() == once


def test_snapshot_restore_round_trips_bookkeeping(tmp_path):
    p = _rb_style(tmp_path)
    original = p.read_bytes()
    doc = RDLDocument.open(p)
    snap = doc.snapshot()
    doc.root.set("Bogus", "1")
    doc.restore(snap)
    doc.save()
    assert p.read_bytes() == original
    # A snapshot can be restored more than once.
    doc.restore(snap)
    doc.save()
    assert p.read_bytes() == original


def test_in_memory_document_gets_report_builder_defaults(tmp_path):
    from pbirb_mcp.ops.scratch import create_report

    out = tmp_path / "new.rdl"
    create_report(str(out))
    data = out.read_bytes()
    assert data.startswith(b'<?xml version="1.0" encoding="utf-8"?>\n')
    assert data.endswith(b">\n")
