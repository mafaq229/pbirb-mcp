"""Tests for duplicate_report (Phase 6 commit 29).

Atomic clone with optional GUID regeneration. Driven by Overspeed-
Violations session feedback wishlist — duplicate an .rdl to a new path
without copy + manual GUID rewrite.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RD_NS
from pbirb_mcp.ops.clone import duplicate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
FIXTURE_MULTI_DS = Path(__file__).parent / "fixtures" / "pbi_multi_datasource.rdl"


@pytest.fixture
def src_path(tmp_path: Path) -> Path:
    dest = tmp_path / "src.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def src_multi(tmp_path: Path) -> Path:
    dest = tmp_path / "src.rdl"
    shutil.copy(FIXTURE_MULTI_DS, dest)
    return dest


def _data_source_ids(path: Path) -> list[str]:
    doc = RDLDocument.open(path)
    return [e.text for e in doc.root.iter(f"{{{RD_NS}}}DataSourceID") if e.text]


def _all_rd_guids(path: Path) -> list[str]:
    """Return every <rd:DataSourceID> and <rd:ReportID> GUID."""
    doc = RDLDocument.open(path)
    out: list[str] = []
    for e in doc.root.iter(f"{{{RD_NS}}}DataSourceID"):
        if e.text:
            out.append(e.text)
    for e in doc.root.iter(f"{{{RD_NS}}}ReportID"):
        if e.text:
            out.append(e.text)
    return out


# ---- happy path ---------------------------------------------------------


class TestDuplicateReport:
    def test_creates_destination_file(self, src_path, tmp_path):
        dst = tmp_path / "dst.rdl"
        result = duplicate_report(src_path=str(src_path), dst_path=str(dst))
        assert dst.exists()
        assert result["dst"] == str(dst)
        # Document parses cleanly.
        RDLDocument.open(dst).validate()

    def test_regenerate_ids_default_true(self, src_path, tmp_path):
        # Fixture has one <rd:DataSourceID> AND one <rd:ReportID>; the
        # tool regenerates both.
        src_guids = _all_rd_guids(src_path)
        assert len(src_guids) == 2
        dst = tmp_path / "dst.rdl"
        result = duplicate_report(src_path=str(src_path), dst_path=str(dst))
        assert sorted(result["regenerated_ids"]) == sorted(src_guids)
        # Destination has different IDs.
        dst_guids = _all_rd_guids(dst)
        assert len(dst_guids) == 2
        assert set(dst_guids).isdisjoint(set(src_guids))
        # New IDs are valid UUIDs.
        for g in dst_guids:
            uuid.UUID(g)

    def test_regenerate_ids_handles_multiple_data_sources(self, src_multi, tmp_path):
        # pbi_multi_datasource fixture has 3 DataSources (each with an
        # rd:DataSourceID) plus one rd:ReportID. All four are
        # regenerated.
        src_ds_ids = _data_source_ids(src_multi)
        assert len(src_ds_ids) == 3
        src_all = _all_rd_guids(src_multi)
        dst = tmp_path / "dst.rdl"
        result = duplicate_report(src_path=str(src_multi), dst_path=str(dst))
        assert sorted(result["regenerated_ids"]) == sorted(src_all)
        dst_ds_ids = _data_source_ids(dst)
        assert len(dst_ds_ids) == 3
        # All three DataSourceIDs regenerated.
        assert set(dst_ds_ids).isdisjoint(set(src_ds_ids))

    def test_regenerate_ids_false_byte_for_byte_copy(self, src_path, tmp_path):
        dst = tmp_path / "dst.rdl"
        result = duplicate_report(
            src_path=str(src_path),
            dst_path=str(dst),
            regenerate_ids=False,
        )
        assert result["regenerated_ids"] == []
        # Byte-for-byte equal.
        assert dst.read_bytes() == src_path.read_bytes()

    def test_round_trip_safe(self, src_path, tmp_path):
        dst = tmp_path / "dst.rdl"
        duplicate_report(src_path=str(src_path), dst_path=str(dst))
        # Re-open and structurally validate.
        RDLDocument.open(dst).validate()


# ---- safety guards -------------------------------------------------------


class TestDuplicateReportRefusals:
    def test_refuses_when_dst_exists(self, src_path, tmp_path):
        dst = tmp_path / "dst.rdl"
        dst.write_text("preexisting")
        with pytest.raises(FileExistsError, match="already exists"):
            duplicate_report(src_path=str(src_path), dst_path=str(dst))
        # The pre-existing file is untouched.
        assert dst.read_text() == "preexisting"

    def test_refuses_unknown_src(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="not a regular file"):
            duplicate_report(
                src_path=str(tmp_path / "nope.rdl"),
                dst_path=str(tmp_path / "dst.rdl"),
            )

    def test_refuses_directory_src(self, tmp_path):
        # Pass a directory path as src.
        with pytest.raises(FileNotFoundError, match="not a regular file"):
            duplicate_report(
                src_path=str(tmp_path),
                dst_path=str(tmp_path / "dst.rdl"),
            )


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_duplicate_report_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "duplicate_report" in names
