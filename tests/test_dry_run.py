"""Tests for dry_run_edit (Phase 7 commit 32).

Round-trip discipline: the source file's bytes must be unchanged after
``dry_run_edit`` returns, regardless of whether the ops succeeded or
failed. Diff + verify are computed against the tempfile state.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.ops.dry_run import dry_run_edit
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


class TestDryRunEdit:
    def test_no_ops_returns_empty_diff_and_clean_verify(self, rdl_path):
        before = rdl_path.read_bytes()
        result = dry_run_edit(str(rdl_path), [])
        assert result["applied"] == []
        assert result["diff"] == ""
        assert result["verify"]["valid"] is True
        # Original untouched.
        assert rdl_path.read_bytes() == before

    def test_single_op_produces_diff(self, rdl_path):
        before = rdl_path.read_bytes()
        result = dry_run_edit(
            str(rdl_path),
            [
                {
                    "tool": "set_alternating_row_color",
                    "args": {
                        "tablix_name": "MainTable",
                        "color_a": "#ffffff",
                        "color_b": "#eeeeee",
                    },
                }
            ],
        )
        assert len(result["applied"]) == 1
        assert result["applied"][0]["ok"] is True
        assert result["diff"] != ""
        # Original file's bytes are unchanged.
        assert rdl_path.read_bytes() == before

    def test_failing_op_stops_dispatch(self, rdl_path):
        before = rdl_path.read_bytes()
        result = dry_run_edit(
            str(rdl_path),
            [
                {
                    "tool": "set_alternating_row_color",
                    "args": {
                        "tablix_name": "DoesNotExist",
                        "color_a": "#ffffff",
                        "color_b": "#eeeeee",
                    },
                },
                # Second op never runs because the first failed.
                {
                    "tool": "set_alternating_row_color",
                    "args": {
                        "tablix_name": "MainTable",
                        "color_a": "#ff0000",
                        "color_b": "#000000",
                    },
                },
            ],
        )
        assert len(result["applied"]) == 1
        assert result["applied"][0]["ok"] is False
        assert "error" in result["applied"][0]
        # Original file's bytes are unchanged.
        assert rdl_path.read_bytes() == before

    def test_path_arg_is_injected(self, rdl_path):
        # Caller did not supply a `path` arg; harness injects the tempfile.
        result = dry_run_edit(
            str(rdl_path),
            [
                {
                    "tool": "set_textbox_value",
                    "args": {"textbox_name": "HeaderProductID", "value": "ID"},
                }
            ],
        )
        assert result["applied"][0]["ok"] is True

    def test_unknown_tool_recorded_as_error(self, rdl_path):
        before = rdl_path.read_bytes()
        result = dry_run_edit(
            str(rdl_path),
            [{"tool": "no_such_tool", "args": {}}],
        )
        assert result["applied"][-1]["ok"] is False
        assert rdl_path.read_bytes() == before

    def test_verify_surfaces_lint_issues(self, rdl_path):
        # Apply an op that introduces a lint warning (unused-data-source
        # via add_data_source — adding a DataSource that no DataSet uses).
        result = dry_run_edit(
            str(rdl_path),
            [
                {
                    "tool": "add_data_source",
                    "args": {
                        "name": "UnusedDS",
                        "workspace_url": "powerbi://api.powerbi.com/v1.0/myorg/Workspace",
                        "dataset_name": "Dataset",
                    },
                }
            ],
        )
        assert result["applied"][0]["ok"] is True
        rules = [i["rule"] for i in result["verify"]["issues"]]
        assert "unused-data-source" in rules

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            dry_run_edit(str(tmp_path / "nope.rdl"), [])


class TestToolRegistration:
    def test_dry_run_edit_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "dry_run_edit" in names


# ---- #84: verify semantics, apply_edits shape, bounded diff ----------------

FIXTURES = Path(__file__).parent / "fixtures"
SET_VALUE = [
    {"tool": "set_textbox_value", "args": {"textbox_name": "HeaderProductID", "value": "X"}}
]


def _call(tool, **arguments):
    srv = MCPServer()
    register_all_tools(srv)
    resp = srv.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    result = resp["result"]
    return json.loads(result["content"][0]["text"]), bool(result.get("isError"))


@pytest.fixture
def multi_ds(tmp_path: Path) -> Path:
    dest = tmp_path / "multi.rdl"
    shutil.copy(FIXTURES / "pbi_multi_datasource.rdl", dest)
    return dest


class TestVerifySemantics:
    def test_warnings_alone_keep_the_dry_run_valid(self, multi_ds):
        result = dry_run_edit(str(multi_ds), SET_VALUE)
        severities = [i["severity"] for i in result["verify"]["issues"]]
        assert severities == ["warning", "warning"]
        assert result["verify"]["valid"] is True
        # Agrees with verify_report on the same file.
        verify, _ = _call("verify_report", path=str(multi_ds))
        assert verify["valid"] is True

    def test_xsd_errors_are_issues_and_invalidate(self, rdl_path):
        # Write an XSD-invalid value straight into the temp copy's text.
        result = dry_run_edit(
            str(rdl_path),
            [
                {
                    "tool": "set_textbox_value",
                    "args": {"textbox_name": "HeaderProductID", "value": "X"},
                }
            ],
        )
        assert "errors" not in result["verify"]
        bad = rdl_path.parent / "bad.rdl"
        bad.write_text(
            rdl_path.read_text().replace("<CanGrow>true</CanGrow>", "<CanGrow>maybe</CanGrow>", 1)
        )
        verify = dry_run_edit(str(bad), SET_VALUE)["verify"]
        assert verify["valid"] is False
        xsd = [i for i in verify["issues"] if i["rule"] == "xsd"]
        assert xsd and all(i["severity"] == "error" and i["new"] is False for i in xsd)

    def test_issues_are_marked_new_against_the_source(self, multi_ds):
        result = dry_run_edit(
            str(multi_ds),
            [
                {
                    "tool": "set_textbox_value",
                    "args": {"textbox_name": "HeaderProductID", "value": "=Fields!Typo.Value"},
                }
            ],
        )
        verify = result["verify"]
        assert verify["valid"] is False
        assert verify["preexisting_count"] == 2
        assert verify["introduced_error_count"] == 1
        new = [i for i in verify["issues"] if i["new"]]
        assert [i["rule"] for i in new] == ["missing-field-reference"]

    def test_no_ops_marks_nothing_new(self, multi_ds):
        verify = dry_run_edit(str(multi_ds), [])["verify"]
        assert verify["introduced_count"] == 0
        assert verify["preexisting_count"] == 2


class TestSameShapeAsApplyEdits:
    def test_applied_and_verify_key_sets_match(self, multi_ds, tmp_path):
        dry = dry_run_edit(str(multi_ds), SET_VALUE)
        other = tmp_path / "other.rdl"
        shutil.copy(multi_ds, other)
        applied, _ = _call("apply_edits", path=str(other), ops=SET_VALUE)
        assert applied["committed"] is True
        # result_text is the deprecated alias kept for one release.
        assert set(dry["applied"][0]) - {"result_text"} == set(applied["applied"][0])
        assert set(dry["verify"]) == set(applied["verify"])
        assert dry["applied"][0]["result"] == applied["applied"][0]["result"]

    def test_result_is_an_object(self, rdl_path):
        entry = dry_run_edit(str(rdl_path), SET_VALUE)["applied"][0]
        assert isinstance(entry["result"], dict)
        assert entry["result"] == json.loads(entry["result_text"])

    def test_failed_apply_edits_has_the_same_verify_keys(self, multi_ds):
        dry = dry_run_edit(str(multi_ds), SET_VALUE)
        failed, _ = _call(
            "apply_edits",
            path=str(multi_ds),
            ops=[{"tool": "set_textbox_value", "args": {"textbox_name": "Nope", "value": "X"}}],
        )
        assert failed["committed"] is False
        assert set(failed["verify"]) == set(dry["verify"])


class TestBoundedDiff:
    ROW_COLOR = [
        {
            "tool": "set_alternating_row_color",
            "args": {"tablix_name": "MainTable", "color_a": "#ffffff", "color_b": "#eeeeee"},
        }
    ]

    @pytest.mark.parametrize("cap", [1, 10, 40, 100, 200, 400, 1000])
    def test_unified_diff_never_exceeds_the_cap(self, rdl_path, cap):
        result = dry_run_edit(str(rdl_path), self.ROW_COLOR, max_diff_bytes=cap)
        assert len(result["diff"].encode("utf-8")) <= cap
        assert result["diff_truncated"] is True
        assert "pbirb-mcp: diff truncated" in result["diff"] or cap < 30
        assert result["diff_stats"]["lines_added"] >= 1

    def test_uncapped_diff_is_whole(self, rdl_path):
        result = dry_run_edit(str(rdl_path), self.ROW_COLOR, max_diff_bytes=0)
        assert result["diff_truncated"] is False
        assert "truncated" not in result["diff"]

    def test_crlf_bom_report_one_value_edit_is_a_small_diff(self, tmp_path):
        crlf = tmp_path / "crlf.rdl"
        raw = (FIXTURES / "pbi_multi_datasource.rdl").read_bytes()
        crlf.write_bytes(b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n"))
        result = dry_run_edit(str(crlf), SET_VALUE)
        assert result["applied"][0]["ok"] is True
        assert result["diff_stats"]["lines_added"] == 1
        assert result["diff_stats"]["lines_removed"] == 1
        assert len(result["diff"]) < 1000

    def test_summary_lists_the_touched_element(self, rdl_path):
        result = dry_run_edit(str(rdl_path), SET_VALUE, diff="summary")
        assert result["diff"] == [
            "changed Textbox[HeaderProductID]/Paragraphs/Paragraph/TextRuns/TextRun/Value"
        ]
        assert result["diff_truncated"] is False

    def test_summary_is_capped_too(self, rdl_path):
        ops = [
            {"tool": "set_textbox_value", "args": {"textbox_name": tb, "value": "X"}}
            for tb in ("HeaderProductID", "HeaderProductName", "HeaderAmount")
        ]
        full = dry_run_edit(str(rdl_path), ops, diff="summary")["diff"]
        assert len(full) == 3
        capped = dry_run_edit(str(rdl_path), ops, diff="summary", max_diff_bytes=200)
        assert capped["diff_truncated"] is True
        assert len(json.dumps(capped["diff"]).encode()) <= 200
        assert capped["diff"][-1].startswith("... ")

    def test_none_leaves_the_diff_out(self, rdl_path):
        result = dry_run_edit(str(rdl_path), SET_VALUE, diff="none")
        assert "diff" not in result and "diff_truncated" not in result
        assert result["diff_stats"]["changed"] is True

    def test_summary_flag_can_still_ask_for_a_diff(self, rdl_path):
        result = dry_run_edit(str(rdl_path), SET_VALUE, summary=True, diff="summary")
        assert result["applied"] == [{"tool": "set_textbox_value", "ok": True}]
        assert len(result["diff"]) == 1

    def test_unknown_diff_mode_is_refused(self, rdl_path):
        out, is_error = _call("dry_run_edit", path=str(rdl_path), ops=SET_VALUE, diff="xml")
        assert is_error, out
        with pytest.raises(ValueError, match="diff must be one of"):
            dry_run_edit(str(rdl_path), SET_VALUE, diff="xml")
