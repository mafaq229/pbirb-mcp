"""Tests for the pure-data transaction registry (v0.4 commit 7).

The registry has no MCPServer dependency and no filesystem side
effects beyond what RDLDocument.open already does. Tests here verify
the dict-mechanics, the orphan sweep, and the conflict refusal —
nothing that requires the dispatcher (that comes in commit 9) or the
public start/commit/cancel tools (commit 10).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core import transactions
from pbirb_mcp.core.document import RDLDocument

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Module-level state — reset between every test so failures
    don't leak transactions into the next case."""
    transactions._reset_for_tests()
    yield
    transactions._reset_for_tests()


@pytest.fixture
def doc(tmp_path: Path) -> RDLDocument:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return RDLDocument.open(dest)


class TestRegister:
    def test_returns_a_new_uuid_hex(self, doc):
        tx_id = transactions.register(doc)
        assert isinstance(tx_id, str)
        assert len(tx_id) == 32  # uuid4().hex
        assert all(c in "0123456789abcdef" for c in tx_id)

    def test_marks_doc_in_transaction(self, doc):
        transactions.register(doc)
        assert doc._in_transaction is True

    def test_indexes_by_resolved_abspath(self, doc):
        tx_id = transactions.register(doc)
        abspath = str(doc.path.resolve())
        assert transactions.lookup_by_path(abspath).transaction_id == tx_id

    def test_indexes_by_transaction_id(self, doc):
        tx_id = transactions.register(doc)
        tx = transactions.lookup_by_id(tx_id)
        assert tx is not None
        assert tx.doc is doc

    def test_refuses_duplicate_for_same_path(self, doc):
        transactions.register(doc)
        # Reopen the SAME path via a fresh RDLDocument — different object,
        # same canonical abspath. Registry must refuse.
        doc2 = RDLDocument.open(doc.path)
        with pytest.raises(ValueError, match="already owns"):
            transactions.register(doc2)

    def test_rejects_non_rdldocument(self, tmp_path):
        with pytest.raises(TypeError):
            transactions.register("not a doc")


class TestLookup:
    def test_unknown_id_returns_none(self):
        assert transactions.lookup_by_id("does-not-exist") is None

    def test_unknown_path_returns_none(self, tmp_path):
        assert transactions.lookup_by_path(str(tmp_path / "ghost.rdl")) is None

    def test_lookup_survives_until_cancel(self, doc):
        tx_id = transactions.register(doc)
        assert transactions.lookup_by_id(tx_id) is not None
        transactions.cancel(tx_id)
        assert transactions.lookup_by_id(tx_id) is None


class TestCancel:
    def test_returns_the_removed_transaction(self, doc):
        tx_id = transactions.register(doc)
        tx = transactions.cancel(tx_id)
        assert tx is not None
        assert tx.transaction_id == tx_id

    def test_returns_none_for_unknown_id(self):
        assert transactions.cancel("does-not-exist") is None

    def test_clears_in_transaction_flag(self, doc):
        tx_id = transactions.register(doc)
        assert doc._in_transaction is True
        transactions.cancel(tx_id)
        assert doc._in_transaction is False

    def test_removes_path_reverse_index(self, doc):
        tx_id = transactions.register(doc)
        abspath = str(doc.path.resolve())
        transactions.cancel(tx_id)
        assert transactions.lookup_by_path(abspath) is None


class TestCommit:
    def test_commit_is_alias_for_cancel_at_registry_layer(self, doc):
        # The registry doesn't save — the commit_editing_transaction
        # TOOL (v0.4 commit 10) is responsible for the actual save.
        # At the registry layer, commit and cancel both deregister.
        tx_id = transactions.register(doc)
        result = transactions.commit(tx_id)
        assert result is not None
        assert transactions.lookup_by_id(tx_id) is None
        assert doc._in_transaction is False


class TestSweepOrphans:
    def test_no_op_when_nothing_expired(self, doc):
        transactions.register(doc, now=1_000.0)
        expired = transactions.sweep_orphans(now=1_100.0)
        assert expired == []
        assert transactions.lookup_by_id is not None

    def test_expires_past_due(self, doc):
        # Register at t=0, default timeout = 600s.
        tx_id = transactions.register(doc, now=0.0)
        # Sweep at t=601 → past the expiry.
        expired = transactions.sweep_orphans(now=601.0)
        assert tx_id in expired
        assert transactions.lookup_by_id(tx_id) is None

    def test_partial_expiry(self, tmp_path):
        # Two transactions on different paths — only one expires.
        a = tmp_path / "a.rdl"
        b = tmp_path / "b.rdl"
        shutil.copy(FIXTURE, a)
        shutil.copy(FIXTURE, b)
        doc_a = RDLDocument.open(a)
        doc_b = RDLDocument.open(b)
        old_id = transactions.register(doc_a, now=0.0)
        new_id = transactions.register(doc_b, now=500.0)
        # Sweep at t=601 — old_id past expiry, new_id not yet.
        expired = transactions.sweep_orphans(now=601.0)
        assert expired == [old_id]
        assert transactions.lookup_by_id(new_id) is not None

    def test_respects_timeout_env(self, doc, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_TRANSACTION_TIMEOUT_S", "10")
        tx_id = transactions.register(doc, now=0.0)
        # Within 10s — not expired.
        assert transactions.sweep_orphans(now=9.0) == []
        # Past 10s — expired.
        assert tx_id in transactions.sweep_orphans(now=11.0)

    def test_invalid_timeout_env_falls_back_to_default(self, doc, monkeypatch):
        monkeypatch.setenv("PBIRB_MCP_TRANSACTION_TIMEOUT_S", "not-a-number")
        tx_id = transactions.register(doc, now=0.0)
        # Default 600s — not expired at t=100.
        assert tx_id not in transactions.sweep_orphans(now=100.0)


class TestActiveTransactions:
    def test_empty_initially(self):
        assert transactions.active_transactions() == []

    def test_lists_registered_ids(self, tmp_path):
        a = tmp_path / "a.rdl"
        b = tmp_path / "b.rdl"
        shutil.copy(FIXTURE, a)
        shutil.copy(FIXTURE, b)
        id_a = transactions.register(RDLDocument.open(a))
        id_b = transactions.register(RDLDocument.open(b))
        assert sorted(transactions.active_transactions()) == sorted([id_a, id_b])


class TestRDLDocumentInterception:
    """v0.4 commit 8 — RDLDocument.open and save_as consult the registry.

    Open returns the registered in-memory tree (object identity), so
    handlers calling RDLDocument.open(path) while a transaction owns
    that path reuse the live tree instead of re-parsing. save_as
    no-ops while _in_transaction is set, so intermediate doc.save()
    calls don't hit disk. Commit clears the flag, calls save_as once
    — that single write is the only on-disk mutation.
    """

    def test_open_returns_registered_doc_by_identity(self, doc):
        transactions.register(doc)
        reopened = RDLDocument.open(doc.path)
        # SAME object — not a fresh parse.
        assert reopened is doc

    def test_open_returns_fresh_parse_when_no_transaction(self, doc):
        # Sanity: with no active transaction, open() still parses fresh.
        fresh = RDLDocument.open(doc.path)
        assert fresh is not doc
        # Same tag, different identity.
        assert fresh.root.tag == doc.root.tag

    def test_open_normalises_path_via_resolve(self, doc, tmp_path):
        # Register under the canonical resolved path.
        transactions.register(doc)
        # Reopen via a non-canonical variant — same file, different
        # string. Must hit the registry.
        weird = Path(str(doc.path.parent / "." / doc.path.name))
        reopened = RDLDocument.open(weird)
        assert reopened is doc

    def test_save_noops_while_in_transaction(self, doc, tmp_path):
        # Mutate but don't save — save_as should silently no-op.
        transactions.register(doc)
        original_bytes = doc.path.read_bytes()
        # Mutate the in-memory tree.
        cmd = doc.root.find(".//{*}CommandText")
        # 'doc' was loaded from FIXTURE; we know it has a CommandText.
        if cmd is not None:
            cmd.text = "EVALUATE TOPN(42, 'Sales')"
        # Direct save calls should no-op while the flag is set.
        doc.save()
        doc.save_as(doc.path)
        # Disk file unchanged.
        assert doc.path.read_bytes() == original_bytes

    def test_save_resumes_after_commit(self, doc):
        # Register + mutate + clear the flag manually (mimics what
        # commit_editing_transaction will do in v0.4 commit 10).
        transactions.register(doc)
        cmd = doc.root.find(".//{*}CommandText")
        new_text = "EVALUATE TOPN(7, 'Sales')"
        cmd.text = new_text
        doc.save()  # no-op
        # Clear the flag (commit-style) and save.
        doc._in_transaction = False
        doc.save()
        reopened = RDLDocument.open(doc.path)
        assert reopened.root.find(".//{*}CommandText").text == new_text

    def test_save_outside_transaction_unaffected(self, doc):
        """Regression canary: with NO active transaction, doc.save()
        behaves exactly like before. This is the path every existing
        handler relies on; if it breaks, all 132 tools break."""
        original_bytes = doc.path.read_bytes()
        cmd = doc.root.find(".//{*}CommandText")
        cmd.text = "EVALUATE TOPN(3, 'Sales')"
        doc.save()
        # Disk was actually written.
        assert doc.path.read_bytes() != original_bytes
        # And the change is recoverable.
        assert (
            RDLDocument.open(doc.path).root.find(".//{*}CommandText").text
            == "EVALUATE TOPN(3, 'Sales')"
        )


# ---- v0.4 commit 10 — public start/commit/cancel tools ------------------


class TestStartEditingTransaction:
    def test_returns_canonical_shape(self, doc):
        from pbirb_mcp.ops.transactions import start_editing_transaction

        result = start_editing_transaction(str(doc.path))
        assert set(result.keys()) == {"transaction_id", "path", "expires_at"}
        assert isinstance(result["transaction_id"], str)
        assert result["path"] == str(doc.path.resolve())
        assert isinstance(result["expires_at"], float)

    def test_registers_in_the_registry(self, doc):
        from pbirb_mcp.ops.transactions import start_editing_transaction

        result = start_editing_transaction(str(doc.path))
        assert transactions.lookup_by_id(result["transaction_id"]) is not None

    def test_refuses_when_path_already_in_transaction(self, doc):
        from pbirb_mcp.ops.transactions import start_editing_transaction

        start_editing_transaction(str(doc.path))
        with pytest.raises(ValueError, match="already owns"):
            start_editing_transaction(str(doc.path))


class TestCommitEditingTransaction:
    def test_lints_then_saves_and_deregisters(self, doc):
        from pbirb_mcp.ops.transactions import (
            commit_editing_transaction,
            start_editing_transaction,
        )

        original_bytes = doc.path.read_bytes()
        tx = start_editing_transaction(str(doc.path))
        tx_id = tx["transaction_id"]

        # Mutate the in-memory tree without touching disk.
        live = transactions.lookup_by_id(tx_id).doc
        cmd = live.root.find(".//{*}CommandText")
        cmd.text = "EVALUATE TOPN(11, 'Sales')"
        # File on disk is still original — interception is keeping it stale.
        assert doc.path.read_bytes() == original_bytes

        result = commit_editing_transaction(tx_id)
        assert result["saved"] is True
        assert result["transaction_id"] == tx_id
        assert result["verify"]["valid"] is True
        # Now the change is on disk.
        assert doc.path.read_bytes() != original_bytes
        reopened = RDLDocument.open(doc.path)
        assert reopened.root.find(".//{*}CommandText").text == "EVALUATE TOPN(11, 'Sales')"
        # Registry entry is gone.
        assert transactions.lookup_by_id(tx_id) is None

    def test_unknown_transaction_id_raises(self):
        from pbirb_mcp.ops.transactions import commit_editing_transaction

        with pytest.raises(ValueError, match="unknown"):
            commit_editing_transaction("not-a-real-id")

    def test_aborts_on_lint_error_does_not_save(self, doc):
        """When lint surfaces a severity='error' issue, commit aborts:
        nothing is written to disk and the transaction stays OPEN so
        the caller can fix the problem and re-commit."""
        from lxml import etree

        from pbirb_mcp.core.xpath import q
        from pbirb_mcp.ops.transactions import (
            commit_editing_transaction,
            start_editing_transaction,
        )

        original_bytes = doc.path.read_bytes()
        tx = start_editing_transaction(str(doc.path))
        tx_id = tx["transaction_id"]

        # Inject a state that triggers lint rule
        # `missing-field-reference` (error severity): add a textbox
        # whose Value references Fields!NoSuchField.Value.
        live = transactions.lookup_by_id(tx_id).doc
        body_items = live.root.find(".//{*}ReportSection/{*}Body/{*}ReportItems")
        tb = etree.SubElement(body_items, q("Textbox"), Name="BadRef")
        paras = etree.SubElement(tb, q("Paragraphs"))
        para = etree.SubElement(paras, q("Paragraph"))
        runs = etree.SubElement(para, q("TextRuns"))
        run = etree.SubElement(runs, q("TextRun"))
        etree.SubElement(run, q("Value")).text = "=Fields!NoSuchField.Value"

        result = commit_editing_transaction(tx_id)
        assert result["saved"] is False
        assert result["verify"]["valid"] is False
        assert any(i["rule"] == "missing-field-reference" for i in result["verify"]["issues"])
        # Disk unchanged.
        assert doc.path.read_bytes() == original_bytes
        # Transaction STILL OPEN — caller can fix and re-commit.
        assert transactions.lookup_by_id(tx_id) is not None


class TestCancelEditingTransaction:
    def test_discards_without_saving(self, doc):
        from pbirb_mcp.ops.transactions import (
            cancel_editing_transaction,
            start_editing_transaction,
        )

        original_bytes = doc.path.read_bytes()
        tx = start_editing_transaction(str(doc.path))
        tx_id = tx["transaction_id"]

        live = transactions.lookup_by_id(tx_id).doc
        live.root.find(".//{*}CommandText").text = "EVALUATE WILL-BE-DISCARDED"

        result = cancel_editing_transaction(tx_id)
        assert result["discarded"] is True
        assert result["path"] == str(doc.path.resolve())
        # Disk untouched, registry empty.
        assert doc.path.read_bytes() == original_bytes
        assert transactions.lookup_by_id(tx_id) is None

    def test_unknown_transaction_id_raises(self):
        from pbirb_mcp.ops.transactions import cancel_editing_transaction

        with pytest.raises(ValueError, match="unknown"):
            cancel_editing_transaction("not-a-real-id")

    def test_save_resumes_normally_after_cancel(self, doc):
        """After cancel, the doc — if held elsewhere — must save
        normally again. The registry's cancel clears the
        _in_transaction flag defensively."""
        from pbirb_mcp.ops.transactions import (
            cancel_editing_transaction,
            start_editing_transaction,
        )

        tx = start_editing_transaction(str(doc.path))
        tx_id = tx["transaction_id"]
        cancel_editing_transaction(tx_id)
        # Re-open via the normal path (no tx active) and save — should work.
        live = RDLDocument.open(doc.path)
        live.root.find(".//{*}CommandText").text = "EVALUATE TOPN(2, 'Sales')"
        live.save()
        reopened = RDLDocument.open(doc.path)
        assert reopened.root.find(".//{*}CommandText").text == "EVALUATE TOPN(2, 'Sales')"


# ---- v0.4 commit 11 — apply_edits atomic batch --------------------------


class TestApplyEdits:
    """apply_edits opens a transaction internally, dispatches each op
    via JSON-RPC with transaction_id injected, commits at the end.
    Atomic: on any op failure or lint-error commit, the disk file is
    unchanged from its pre-call state.
    """

    def test_success_path_commits_and_writes_disk(self, doc):
        import hashlib

        from pbirb_mcp.ops.transactions import apply_edits

        before = hashlib.sha256(doc.path.read_bytes()).hexdigest()
        result = apply_edits(
            path=str(doc.path),
            ops=[
                {
                    "tool": "add_body_textbox",
                    "args": {
                        "name": "BatchedA",
                        "text": "a",
                        "top": "3in",
                        "left": "0.5in",
                        "width": "1in",
                        "height": "0.3in",
                    },
                },
                {
                    "tool": "add_body_textbox",
                    "args": {
                        "name": "BatchedB",
                        "text": "b",
                        "top": "3.5in",
                        "left": "0.5in",
                        "width": "1in",
                        "height": "0.3in",
                    },
                },
            ],
        )
        assert result["committed"] is True
        assert len(result["applied"]) == 2
        assert all(a["ok"] for a in result["applied"])
        # Disk was written (sha256 changed) and both textboxes landed.
        after = hashlib.sha256(doc.path.read_bytes()).hexdigest()
        assert before != after
        body = doc.path.read_text()
        assert "BatchedA" in body
        assert "BatchedB" in body

    def test_mid_batch_failure_rolls_back(self, doc):
        import hashlib

        from pbirb_mcp.ops.transactions import apply_edits

        before_bytes = doc.path.read_bytes()
        before_sha = hashlib.sha256(before_bytes).hexdigest()
        result = apply_edits(
            path=str(doc.path),
            ops=[
                {
                    "tool": "add_body_textbox",
                    "args": {
                        "name": "WillNotPersist",
                        "text": "x",
                        "top": "3in",
                        "left": "0.5in",
                        "width": "1in",
                        "height": "0.3in",
                    },
                },
                {
                    # Same name → handler raises ValueError "already exists".
                    "tool": "add_body_textbox",
                    "args": {
                        "name": "WillNotPersist",
                        "text": "y",
                        "top": "4in",
                        "left": "0.5in",
                        "width": "1in",
                        "height": "0.3in",
                    },
                },
                {
                    # Should never execute — batch stops at op #2.
                    "tool": "add_body_textbox",
                    "args": {
                        "name": "NeverReached",
                        "text": "z",
                        "top": "5in",
                        "left": "0.5in",
                        "width": "1in",
                        "height": "0.3in",
                    },
                },
            ],
        )
        assert result["committed"] is False
        assert len(result["applied"]) == 2
        assert result["applied"][0]["ok"] is True
        assert result["applied"][1]["ok"] is False
        # Disk byte-identical to its pre-call state.
        after_sha = hashlib.sha256(doc.path.read_bytes()).hexdigest()
        assert after_sha == before_sha
        assert "WillNotPersist" not in doc.path.read_text()
        assert "NeverReached" not in doc.path.read_text()
        # Registry is empty — the rollback path cancelled the tx.
        assert transactions.active_transactions() == []

    def test_unknown_tool_returns_failure(self, doc):
        import hashlib

        from pbirb_mcp.ops.transactions import apply_edits

        before = hashlib.sha256(doc.path.read_bytes()).hexdigest()
        result = apply_edits(
            path=str(doc.path),
            ops=[{"tool": "does_not_exist", "args": {}}],
        )
        assert result["committed"] is False
        assert len(result["applied"]) == 1
        assert result["applied"][0]["ok"] is False
        assert hashlib.sha256(doc.path.read_bytes()).hexdigest() == before

    def test_missing_path_raises_file_not_found(self, tmp_path):
        from pbirb_mcp.ops.transactions import apply_edits

        with pytest.raises(FileNotFoundError):
            apply_edits(path=str(tmp_path / "ghost.rdl"), ops=[])

    def test_non_list_ops_rejected(self, doc):
        from pbirb_mcp.ops.transactions import apply_edits

        with pytest.raises(ValueError, match="ops must be a list"):
            apply_edits(path=str(doc.path), ops="not-a-list")

    def test_empty_ops_commits_clean(self, doc):
        """Zero ops = open + immediate commit. Lint runs against the
        unmodified tree → valid. Disk MAY be touched (save_as is
        called once) — that's OK, the call asserts the user wants a
        round-trip-equivalent rewrite."""
        from pbirb_mcp.ops.transactions import apply_edits

        result = apply_edits(path=str(doc.path), ops=[])
        assert result["committed"] is True
        assert result["applied"] == []
        assert result["verify"]["valid"] is True

    def test_caller_supplied_path_in_ops_is_stripped(self, doc):
        """Each op's args["path"] is irrelevant — apply_edits uses the
        outer path. Caller-supplied path inside ops must be silently
        stripped (the dispatcher would barf otherwise — it injects
        path from the transaction)."""
        from pbirb_mcp.ops.transactions import apply_edits

        result = apply_edits(
            path=str(doc.path),
            ops=[
                {
                    "tool": "add_body_textbox",
                    # The /completely/wrong/path here would normally
                    # confuse the handler; apply_edits must strip it.
                    "args": {
                        "path": "/completely/wrong/path.rdl",
                        "name": "PathStrippedOK",
                        "text": "ok",
                        "top": "3in",
                        "left": "0.5in",
                        "width": "1in",
                        "height": "0.3in",
                    },
                }
            ],
        )
        assert result["committed"] is True
        assert "PathStrippedOK" in doc.path.read_text()
