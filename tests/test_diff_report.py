"""diff_report: a semantic, identity-matched diff of two reports (#87)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.toolsets import toolset_of
from pbirb_mcp.ops.diff import diff_report
from pbirb_mcp.ops.dry_run import dry_run_edit
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "pbi_paginated_minimal.rdl"
EMPTY = {k: [] for k in ("added", "removed", "changed", "moved", "reordered")}


@pytest.fixture(scope="module")
def server() -> MCPServer:
    s = MCPServer()
    register_all_tools(s)
    return s


def _call(server, tool, **arguments):
    resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    result = resp["result"]
    payload = json.loads(result["content"][0]["text"])
    assert not result.get("isError"), payload
    return payload


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    """(baseline, report): two copies of the minimal fixture."""
    base = tmp_path / "before.rdl"
    report = tmp_path / "after.rdl"
    shutil.copy(MINIMAL, base)
    shutil.copy(MINIMAL, report)
    return base, report


def _lists(out: dict) -> dict:
    return {k: out[k] for k in EMPTY}


class TestNoise:
    def test_identical_copy(self, pair):
        base, report = pair
        out = diff_report(str(report), str(base))
        assert out["identical"] is True
        assert _lists(out) == EMPTY
        assert out["counts"] == {k: 0 for k in EMPTY}
        assert out["truncated"] is False

    def test_bom_crlf_and_attribute_order_are_not_changes(self, tmp_path):
        text = MINIMAL.read_text()
        attrs = (
            'Name="PowerBIDataset" rd:A="1" rd:B="2"',
            'rd:B="2" Name="PowerBIDataset" rd:A="1"',
        )
        base = tmp_path / "base.rdl"
        base.write_text(text.replace('Name="PowerBIDataset"', attrs[0], 1))
        other = tmp_path / "other.rdl"
        variant = text.replace('Name="PowerBIDataset"', attrs[1], 1)
        # Report Builder form: BOM + CRLF, plus different indentation.
        variant = variant.replace("\n  <", "\n    <")
        other.write_bytes(b"\xef\xbb\xbf" + variant.encode("utf-8").replace(b"\n", b"\r\n"))
        assert other.read_bytes() != base.read_bytes()
        out = diff_report(str(other), str(base))
        assert out["identical"] is True, _lists(out)

    def test_comments_are_ignored(self, pair):
        base, report = pair
        report.write_text(report.read_text().replace("<AutoRefresh>", "<!-- hi --><AutoRefresh>"))
        assert diff_report(str(report), str(base))["identical"] is True

    def test_entity_normalised_text(self, pair):
        base, report = pair
        base.write_text(base.read_text().replace(">Product ID<", ">A &amp; B<"))
        # Double-encoded on disk: the text is literally "A &amp; B".
        report.write_text(report.read_text().replace(">Product ID<", ">A &amp;amp; B<"))
        assert diff_report(str(report), str(base))["identical"] is True

    def test_regenerated_ids_ignored_by_default(self, server, pair, tmp_path):
        base, _ = pair
        dup = tmp_path / "dup.rdl"
        _call(server, "duplicate_report", src_path=str(base), dst_path=str(dup))
        assert diff_report(str(dup), str(base))["identical"] is True
        out = diff_report(str(dup), str(base), ignore_ids=False)
        props = {(c["kind"], c["property"]) for c in out["changed"]}
        assert ("Report", "rd:ReportID") in props
        assert ("DataSource", "rd:DataSourceID") in props


class TestChanges:
    def test_bold_is_exactly_one_changed_entry(self, server, pair):
        base, report = pair
        _call(
            server, "set_textbox_style", path=str(report), textbox_name="Amount", font_weight="Bold"
        )
        out = diff_report(str(report), str(base))
        assert out["changed"] == [
            {
                "kind": "Textbox",
                "name": "Amount",
                "property": "Paragraphs/Paragraph/TextRuns/TextRun/Style/FontWeight",
                "from": None,
                "to": "Bold",
            }
        ]
        assert out["counts"] == {"added": 0, "removed": 0, "changed": 1, "moved": 0, "reordered": 0}

    def test_value_change(self, server, pair):
        base, report = pair
        _call(
            server,
            "set_textbox_value",
            path=str(report),
            textbox_name="HeaderAmount",
            value="Total",
        )
        (entry,) = diff_report(str(report), str(base))["changed"]
        assert (entry["name"], entry["from"], entry["to"]) == ("HeaderAmount", "Amount", "Total")

    def test_add_tablix_column_adds_the_column_and_its_cells(self, server, pair):
        base, report = pair
        _call(
            server,
            "add_tablix_column",
            path=str(report),
            tablix_name="MainTable",
            column_name="Extra",
            header_text="Extra",
            expression="=Fields!Amount.Value",
        )
        out = diff_report(str(report), str(base))
        added = {(a["kind"], a["name"], a.get("path")) for a in out["added"]}
        assert ("TablixColumn", None, "TablixBody/TablixColumns/TablixColumn[4]") in added
        cells = [a for a in out["added"] if a["kind"] == "TablixCell"]
        assert len(cells) == 2 and all(c["parent"] == "MainTable" for c in cells)
        assert any(a["kind"] == "TablixMember" for a in out["added"])
        textboxes = {a["name"] for a in out["added"] if a["kind"] == "Textbox"}
        assert len(textboxes) == 2
        # Nothing else in the tablix "changed" because of the shift.
        # #89: adding a column also grows Tablix/Width to the column sum.
        assert [(c["kind"], c["name"], c["property"]) for c in out["changed"]] == [
            ("Tablix", "MainTable", "Width")
        ]
        assert out["removed"] == []

    def test_removed_item(self, server, pair):
        base, report = pair
        _call(
            server,
            "add_body_textbox",
            path=str(base),
            name="Note",
            text="Note",
            top="3in",
            left="0in",
            width="2in",
            height="0.25in",
        )
        out = diff_report(str(report), str(base))
        assert out["removed"] == [{"kind": "Textbox", "name": "Note", "parent": "Body"}]
        out = diff_report(str(base), str(report))
        assert out["added"] == [{"kind": "Textbox", "name": "Note", "parent": "Body"}]

    def test_moved_item(self, server, pair):
        base, report = pair
        for path in (base, report):
            _call(server, "set_page_header", path=str(path), height="1in")
            _call(
                server,
                "add_body_textbox",
                path=str(path),
                name="Note",
                text="Note",
                top="3in",
                left="0in",
                width="2in",
                height="0.25in",
            )
        _call(server, "move_report_item", path=str(report), name="Note", to="header")
        out = diff_report(str(report), str(base))
        assert out["moved"] == [
            {"kind": "Textbox", "name": "Note", "from": "Body", "to": "PageHeader"}
        ]

    def test_reordered_parameters(self, server, pair):
        base, report = pair
        _call(server, "reorder_parameters", path=str(report), names=["DateTo", "DateFrom"])
        out = diff_report(str(report), str(base))
        assert out["reordered"] == [
            {
                "kind": "Report",
                "name": None,
                "path": "ReportParameters",
                "from": ["DateFrom", "DateTo"],
                "to": ["DateTo", "DateFrom"],
            }
        ]
        assert out["changed"] == []

    def test_page_property(self, server, pair):
        base, report = pair
        _call(server, "set_page_setup", path=str(report), page_height="12in")
        assert diff_report(str(report), str(base))["changed"] == [
            {"kind": "Page", "name": None, "property": "PageHeight", "from": "11in", "to": "12in"}
        ]

    def test_field_scoped_to_its_dataset(self, server, pair):
        base, report = pair
        _call(
            server,
            "add_calculated_field",
            path=str(report),
            dataset_name="MainDataset",
            field_name="Double",
            expression="=Fields!Amount.Value * 2",
        )
        out = diff_report(str(report), str(base))
        assert {"kind": "Field", "name": "Double", "parent": "MainDataset"} in out["added"]


class TestScopeAndBounds:
    def _two_edits(self, server, report):
        _call(
            server, "set_textbox_style", path=str(report), textbox_name="Amount", font_weight="Bold"
        )
        _call(server, "set_page_setup", path=str(report), page_height="12in")

    def test_scope_to_a_named_item(self, server, pair):
        base, report = pair
        self._two_edits(server, report)
        out = diff_report(str(report), str(base), scope="Amount")
        assert [c["name"] for c in out["changed"]] == ["Amount"]
        # The tablix holding the textbox includes it too.
        assert len(diff_report(str(report), str(base), scope="MainTable")["changed"]) == 1

    def test_scope_to_an_anchor(self, server, pair):
        base, report = pair
        self._two_edits(server, report)
        out = diff_report(str(report), str(base), scope="Page")
        assert [c["kind"] for c in out["changed"]] == ["Page"]

    def test_unknown_scope(self, pair):
        base, report = pair
        with pytest.raises(ElementNotFoundError):
            diff_report(str(report), str(base), scope="Nope")

    def test_max_entries(self, server, pair):
        base, report = pair
        self._two_edits(server, report)
        out = diff_report(str(report), str(base), max_entries=1)
        assert out["truncated"] is True
        assert len(out["changed"]) == 1
        assert out["counts"]["changed"] == 2
        assert out["identical"] is False
        assert diff_report(str(report), str(base), max_entries=0)["truncated"] is False

    def test_negative_max_entries(self, pair):
        base, report = pair
        with pytest.raises(ValueError, match="max_entries"):
            diff_report(str(report), str(base), max_entries=-1)


class TestTool:
    def test_registered_read_only_in_core(self, server, pair):
        assert toolset_of("diff_report") == "core"
        annotations = server._tools["diff_report"].annotations
        assert annotations["readOnlyHint"] is True
        base, report = pair
        before = (report.read_bytes(), base.read_bytes())
        out = _call(server, "diff_report", path=str(report), baseline_path=str(base))
        assert out["identical"] is True
        assert (report.read_bytes(), base.read_bytes()) == before

    def test_inside_a_transaction_sees_uncommitted_edits(self, server, pair):
        base, report = pair
        tx = _call(server, "start_editing_transaction", path=str(report))["transaction_id"]
        try:
            _call(
                server,
                "set_textbox_value",
                path=str(report),
                textbox_name="HeaderAmount",
                value="Total",
                transaction_id=tx,
            )
            out = _call(
                server,
                "diff_report",
                path=str(report),
                baseline_path=str(base),
                transaction_id=tx,
            )
            assert out["counts"]["changed"] == 1
            assert diff_report(str(report), str(base))["identical"] is True  # disk untouched
        finally:
            _call(server, "cancel_editing_transaction", transaction_id=tx)


class TestDryRunSemantic:
    def test_dry_run_semantic_reuses_diff_report(self, pair):
        base, _ = pair
        ops = [
            {
                "tool": "set_textbox_style",
                "args": {"textbox_name": "Amount", "font_weight": "Bold"},
            }
        ]
        result = dry_run_edit(str(base), ops, diff="semantic")
        diff = result["diff"]
        assert diff["counts"]["changed"] == 1
        assert diff["changed"][0]["property"].endswith("Style/FontWeight")
        assert result["diff_truncated"] is False

    def test_dry_run_semantic_is_capped(self, pair):
        base, _ = pair
        ops = [
            {"tool": "set_textbox_value", "args": {"textbox_name": tb, "value": "X"}}
            for tb in ("HeaderProductID", "HeaderProductName", "HeaderAmount")
        ]
        result = dry_run_edit(str(base), ops, diff="semantic", max_diff_bytes=300)
        assert result["diff_truncated"] is True
        assert result["diff"]["counts"]["changed"] == 3
        assert len(result["diff"]["changed"]) < 3
        assert len(json.dumps(result["diff"]).encode()) <= 300

    def test_no_ops_is_empty(self, pair):
        base, _ = pair
        result = dry_run_edit(str(base), [], diff="semantic")
        assert result["diff"]["counts"] == {k: 0 for k in EMPTY}
