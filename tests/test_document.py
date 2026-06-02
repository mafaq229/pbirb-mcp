"""Tests for the RDLDocument core: open, save, validate, round-trip fidelity."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import (
    RD_NS,
    RDL_NS,
    RDLDocument,
    RDLValidationError,
)

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def tmp_rdl(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


class TestOpen:
    def test_open_returns_document(self, tmp_rdl):
        doc = RDLDocument.open(tmp_rdl)
        assert doc.path == tmp_rdl
        assert doc.root.tag == f"{{{RDL_NS}}}Report"

    def test_open_exposes_namespace_map(self, tmp_rdl):
        doc = RDLDocument.open(tmp_rdl)
        assert doc.nsmap[None] == RDL_NS
        assert doc.nsmap["rd"] == RD_NS

    def test_open_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            RDLDocument.open(tmp_path / "missing.rdl")

    def test_open_malformed_xml_raises(self, tmp_path):
        bad = tmp_path / "bad.rdl"
        bad.write_text("<not valid xml")
        with pytest.raises(etree.XMLSyntaxError):
            RDLDocument.open(bad)


class TestSave:
    def test_save_writes_file(self, tmp_rdl):
        doc = RDLDocument.open(tmp_rdl)
        out = tmp_rdl.parent / "out.rdl"
        doc.save_as(out)
        assert out.exists()
        # Reopen and confirm root element survives.
        reopened = RDLDocument.open(out)
        assert reopened.root.tag == f"{{{RDL_NS}}}Report"

    def test_save_in_place(self, tmp_rdl):
        doc = RDLDocument.open(tmp_rdl)
        doc.save()
        assert tmp_rdl.exists()

    def test_save_is_atomic_no_partial_file_on_failure(self, tmp_rdl, monkeypatch):
        """If serialization fails mid-write, the original file must remain intact."""
        doc = RDLDocument.open(tmp_rdl)
        original = tmp_rdl.read_bytes()

        def boom(*a, **kw):
            raise RuntimeError("simulated write failure")

        # Patch the tostring used by document.save_as (imported as `etree`).
        monkeypatch.setattr("pbirb_mcp.core.document.etree.tostring", boom)
        with pytest.raises(RuntimeError):
            doc.save()
        # Original file untouched.
        assert tmp_rdl.read_bytes() == original
        # No temp file left lying around.
        assert not (tmp_rdl.parent / (tmp_rdl.name + ".tmp")).exists()


class TestRoundTrip:
    def test_no_op_open_save_preserves_structure(self, tmp_rdl):
        """Open → save → reopen produces a structurally equivalent tree."""
        doc1 = RDLDocument.open(tmp_rdl)
        out = tmp_rdl.parent / "rt.rdl"
        doc1.save_as(out)
        doc2 = RDLDocument.open(out)

        # Canonicalized XML must match exactly: same elements, same attributes,
        # same order, same text. This is the strict structural-fidelity test.
        c14n_1 = etree.tostring(doc1.tree, method="c14n")
        c14n_2 = etree.tostring(doc2.tree, method="c14n")
        assert c14n_1 == c14n_2

    def test_no_op_preserves_namespace_prefixes(self, tmp_rdl):
        """The 'rd:' prefix must survive a round-trip — Report Builder relies on it."""
        doc = RDLDocument.open(tmp_rdl)
        out = tmp_rdl.parent / "rt.rdl"
        doc.save_as(out)
        text = out.read_text(encoding="utf-8")
        assert 'xmlns:rd="' in text
        assert "rd:TypeName" in text
        assert "rd:DataSourceID" in text

    def test_round_trip_byte_identical_to_fixture(self, tmp_rdl):
        """The bundled fixture is hand-written to match Report Builder's style.
        A no-op round-trip should reproduce it byte-for-byte."""
        doc = RDLDocument.open(tmp_rdl)
        out = tmp_rdl.parent / "rt.rdl"
        doc.save_as(out)
        assert out.read_bytes() == FIXTURE.read_bytes()

    def test_xml_declaration_uses_double_quotes(self, tmp_rdl):
        """Report Builder writes <?xml version="1.0" ...?> — keep that style."""
        doc = RDLDocument.open(tmp_rdl)
        out = tmp_rdl.parent / "rt.rdl"
        doc.save_as(out)
        head = out.read_bytes()[:60]
        assert b'<?xml version="1.0"' in head


class TestValidate:
    def test_validate_passes_on_minimal_fixture(self, tmp_rdl):
        doc = RDLDocument.open(tmp_rdl)
        # Structural validate: well-formed, root is Report in RDL ns, has DataSets+ReportSections.
        doc.validate()  # no raise

    def test_validate_rejects_wrong_root_namespace(self, tmp_path):
        bad = tmp_path / "bad.rdl"
        bad.write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<Report xmlns="http://example.com/wrong"></Report>'
        )
        doc = RDLDocument.open(bad)
        with pytest.raises(RDLValidationError):
            doc.validate()

    def test_validate_rejects_missing_required_sections(self, tmp_path):
        bad = tmp_path / "bad.rdl"
        bad.write_text(f'<?xml version="1.0" encoding="utf-8"?><Report xmlns="{RDL_NS}"></Report>')
        doc = RDLDocument.open(bad)
        with pytest.raises(RDLValidationError) as excinfo:
            doc.validate()
        assert "ReportSections" in str(excinfo.value)


class TestEditAndRoundTrip:
    def test_edit_writes_change_and_reopens(self, tmp_rdl):
        doc = RDLDocument.open(tmp_rdl)
        # Edit the DAX command text in-place via lxml.
        cmd = doc.root.find(f".//{{{RDL_NS}}}CommandText")
        assert cmd is not None
        cmd.text = "EVALUATE TOPN(10, 'Sales')"
        doc.save()
        # Reopen and confirm.
        doc2 = RDLDocument.open(tmp_rdl)
        cmd2 = doc2.root.find(f".//{{{RDL_NS}}}CommandText")
        assert cmd2.text == "EVALUATE TOPN(10, 'Sales')"


class TestBatch:
    """v0.4 commit 6 — RDLDocument.batch context manager.

    Building block for create_report (commit 13) and the transaction
    commit path (commit 10). Independent of the transaction registry
    that lands in commit 7 — this is just the "open once / save once
    on clean exit / no save on exception" wrapper every multi-mutation
    caller wants.
    """

    def test_two_mutations_one_save(self, tmp_rdl, monkeypatch):
        """Multiple mutations inside one batch produce exactly one
        on-disk write. We count save_as invocations to make the
        single-save guarantee unambiguous (mtime checks are racy on
        same-second writes)."""
        call_count = {"n": 0}
        original_save_as = RDLDocument.save_as

        def counting_save_as(self, path):
            call_count["n"] += 1
            return original_save_as(self, path)

        monkeypatch.setattr(RDLDocument, "save_as", counting_save_as)

        with RDLDocument.batch(tmp_rdl) as doc:
            # Two distinct in-memory mutations.
            cmd = doc.root.find(f".//{{{RDL_NS}}}CommandText")
            cmd.text = "EVALUATE TOPN(5, 'Sales')"
            for ds in doc.root.iter(f"{{{RDL_NS}}}DataSet"):
                ds.set("Name", ds.get("Name") + "_BATCHED")
                break

        assert call_count["n"] == 1, f"expected exactly one save_as call, got {call_count['n']}"

        # Both mutations landed on disk.
        reopened = RDLDocument.open(tmp_rdl)
        assert reopened.root.find(f".//{{{RDL_NS}}}CommandText").text == "EVALUATE TOPN(5, 'Sales')"
        assert any(
            ds.get("Name", "").endswith("_BATCHED")
            for ds in reopened.root.iter(f"{{{RDL_NS}}}DataSet")
        )

    def test_exception_inside_batch_leaves_file_untouched(self, tmp_rdl):
        """The whole point of the batch wrapper: a half-applied edit
        on failure must NEVER reach disk. The original bytes survive
        verbatim — same sha256 as before the with-block."""
        import hashlib

        before = tmp_rdl.read_bytes()
        before_sha = hashlib.sha256(before).hexdigest()
        before_mtime = tmp_rdl.stat().st_mtime_ns

        class _Boom(RuntimeError):
            pass

        with pytest.raises(_Boom), RDLDocument.batch(tmp_rdl) as doc:
            cmd = doc.root.find(f".//{{{RDL_NS}}}CommandText")
            cmd.text = "EVALUATE TOPN(99, 'Sales')"
            raise _Boom("partway through")

        after = tmp_rdl.read_bytes()
        assert hashlib.sha256(after).hexdigest() == before_sha
        assert tmp_rdl.stat().st_mtime_ns == before_mtime

    def test_yields_a_real_rdldocument(self, tmp_rdl):
        """The yielded object is the same RDLDocument shape every
        handler already knows how to work with."""
        with RDLDocument.batch(tmp_rdl) as doc:
            assert isinstance(doc, RDLDocument)
            assert doc.root.tag.endswith("}Report")
            # Trivially valid — no edits.

    def test_no_op_batch_still_saves_once(self, tmp_rdl, monkeypatch):
        """Even an empty batch saves once on exit. The cost (re-serialize
        + atomic rename) is acceptable: an empty batch is the caller's
        intent to validate the round-trip, and skipping the save would
        be a surprising silent no-op."""
        call_count = {"n": 0}
        original_save_as = RDLDocument.save_as

        def counting_save_as(self, path):
            call_count["n"] += 1
            return original_save_as(self, path)

        monkeypatch.setattr(RDLDocument, "save_as", counting_save_as)
        with RDLDocument.batch(tmp_rdl):
            pass
        assert call_count["n"] == 1
