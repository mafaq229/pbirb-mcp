"""ToggleItem / Hidden validation in the tablix visibility tools (#91, in M2).

set_group_visibility, set_column_group_visibility and
set_detail_row_visibility used to write any toggle string and any Hidden
text, leaving dangling or self-scoped toggles that Report Builder rejects.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import resolve_tablix
from pbirb_mcp.core.tablix_shape import detail_member
from pbirb_mcp.core.xpath import find_child, q
from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.tablix import add_row_group, set_detail_row_visibility, set_group_visibility
from pbirb_mcp.ops.tablix_columns import add_column_group, set_column_group_visibility
from pbirb_mcp.ops.validate import verify_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    add_body_textbox(str(dest), "Toggle", "+", "0in", "6in", "0.3in", "0.25in")
    return dest


def _detail_vis(path: Path):
    doc = RDLDocument.open(path)
    return find_child(detail_member(resolve_tablix(doc, "MainTable")), "Visibility")


def _group_member(path: Path, name: str):
    doc = RDLDocument.open(path)
    tablix = resolve_tablix(doc, "MainTable")
    return next(
        m
        for m in tablix.iter(q("TablixMember"))
        if find_child(m, "Group") is not None and find_child(m, "Group").get("Name") == name
    )


# ---- the issue's repro -------------------------------------------------------


def test_unknown_toggle_textbox_raises_and_file_unchanged(rdl):
    add_row_group(str(rdl), "MainTable", "G", "=Fields!ProductName.Value")
    before = rdl.read_bytes()
    with pytest.raises(ValueError, match="does not name any textbox"):
        set_group_visibility(str(rdl), "MainTable", "G", "=False", toggle_textbox="NoSuchBox")
    assert rdl.read_bytes() == before


def test_toggle_in_the_hidden_detail_rows_raises(rdl):
    before = rdl.read_bytes()
    # ProductID is a detail-row cell: it would hide itself.
    with pytest.raises(ValueError, match="hides"):
        set_detail_row_visibility(str(rdl), "MainTable", "=True", toggle_textbox="ProductID")
    assert rdl.read_bytes() == before


def test_toggle_in_group_header_cell_of_same_group_raises(rdl):
    out = add_row_group(str(rdl), "MainTable", "G", "=Fields!ProductName.Value")
    header = out["header_textboxes"][0]
    with pytest.raises(ValueError, match="hides|header"):
        set_group_visibility(str(rdl), "MainTable", "G", "=False", toggle_textbox=header)


def test_parent_group_header_can_toggle_detail_rows(rdl):
    out = add_row_group(str(rdl), "MainTable", "G", "=Fields!ProductName.Value")
    header = out["header_textboxes"][0]
    result = set_detail_row_visibility(str(rdl), "MainTable", "=True", toggle_textbox=header)
    assert result["toggle_textbox"] == header
    assert verify_report(str(rdl))["valid"] is True
    assert not [i for i in lint_report(str(rdl))["issues"] if i["rule"] == "dangling-toggle-item"]


def test_non_textbox_toggle_raises(rdl):
    with pytest.raises(ValueError, match="not a Textbox"):
        set_detail_row_visibility(str(rdl), "MainTable", "=True", toggle_textbox="MainTable")


def test_column_group_toggle_in_hidden_column_raises(rdl):
    out = add_column_group(str(rdl), "MainTable", "CG", "=Fields!ProductID.Value")
    before = rdl.read_bytes()
    # The group's own caption textbox is hidden along with the group.
    target = (out.get("header_textboxes") or ["ProductID"])[0]
    with pytest.raises(ValueError):
        set_column_group_visibility(str(rdl), "MainTable", "CG", "=True", toggle_textbox=target)
    assert rdl.read_bytes() == before


def test_column_group_toggle_from_body_textbox_ok(rdl):
    add_column_group(str(rdl), "MainTable", "CG", "=Fields!ProductID.Value")
    out = set_column_group_visibility(str(rdl), "MainTable", "CG", "=True", toggle_textbox="Toggle")
    assert out["toggle_textbox"] == "Toggle"
    assert out["changed"] is True


# ---- Hidden ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["yes", "", "=", '="open', "=IIF(Fields!X.Value", "0"])
def test_bad_hidden_value_raises_and_file_unchanged(rdl, bad):
    before = rdl.read_bytes()
    with pytest.raises(ValueError):
        set_detail_row_visibility(str(rdl), "MainTable", bad)
    assert rdl.read_bytes() == before


def test_boolean_literal_normalised(rdl):
    out = set_detail_row_visibility(str(rdl), "MainTable", "TRUE")
    assert out["expression"] == "true"
    assert find_child(_detail_vis(rdl), "Hidden").text == "true"


# ---- toggle_textbox contract ------------------------------------------------------


def test_omitted_toggle_keeps_existing(rdl):
    set_detail_row_visibility(str(rdl), "MainTable", "=True", toggle_textbox="Toggle")
    out = set_detail_row_visibility(str(rdl), "MainTable", "=False")
    assert out["toggle_textbox"] == "Toggle"
    vis = _detail_vis(rdl)
    assert find_child(vis, "ToggleItem").text == "Toggle"
    assert find_child(vis, "Hidden").text == "=False"


@pytest.mark.parametrize("remove", [None, ""])
def test_null_or_empty_toggle_removes(rdl, remove):
    set_detail_row_visibility(str(rdl), "MainTable", "=True", toggle_textbox="Toggle")
    out = set_detail_row_visibility(str(rdl), "MainTable", "=True", toggle_textbox=remove)
    assert out["toggle_textbox"] is None
    assert find_child(_detail_vis(rdl), "ToggleItem") is None


def test_no_op_does_not_rewrite_file(rdl):
    set_detail_row_visibility(str(rdl), "MainTable", "=True")
    before = rdl.read_bytes()
    out = set_detail_row_visibility(str(rdl), "MainTable", "=True")
    assert out["changed"] is False
    assert rdl.read_bytes() == before


def test_visibility_edited_in_place_keeps_member_children(rdl):
    add_row_group(str(rdl), "MainTable", "G", "=Fields!ProductName.Value")
    set_group_visibility(str(rdl), "MainTable", "G", "=True", toggle_textbox="Toggle")
    set_group_visibility(str(rdl), "MainTable", "G", "=False")
    member = _group_member(rdl, "G")
    vis = find_child(member, "Visibility")
    assert find_child(vis, "Hidden").text == "=False"
    assert find_child(vis, "ToggleItem").text == "Toggle"
    assert len(member.findall(q("Visibility"))) == 1


def test_server_null_toggle_removes(rdl):
    import json

    server = MCPServer()
    register_all_tools(server)

    def call(**args):
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "set_detail_row_visibility", "arguments": args},
            }
        )
        return json.loads(resp["result"]["content"][0]["text"])

    call(path=str(rdl), tablix_name="MainTable", expression="=True", toggle_textbox="Toggle")
    out = call(path=str(rdl), tablix_name="MainTable", expression="=True", toggle_textbox=None)
    assert out["toggle_textbox"] is None


# ---- remove_tablix_column and toggle targets (#91) -------------------------------


def _repro(rdl: Path) -> str:
    """The issue's repro: the detail rows are toggled by a group header cell
    that lives in the ProductID column."""
    out = add_row_group(str(rdl), "MainTable", "G", "=Fields!ProductName.Value")
    header = out["header_textboxes"][0]
    set_detail_row_visibility(str(rdl), "MainTable", "=True", toggle_textbox=header)
    return header


def test_remove_column_holding_toggle_target_refused_file_unchanged(rdl):
    from pbirb_mcp.ops.tablix_columns import remove_tablix_column

    header = _repro(rdl)
    before = rdl.read_bytes()
    with pytest.raises(ValueError, match="ToggleItem"):
        remove_tablix_column(str(rdl), "MainTable", "ProductID")
    assert rdl.read_bytes() == before
    assert f'Name="{header}"' in rdl.read_text()


def test_remove_column_force_leaves_no_dangling_toggle(rdl):
    from pbirb_mcp.ops.tablix_columns import remove_tablix_column

    header = _repro(rdl)
    out = remove_tablix_column(str(rdl), "MainTable", "ProductID", force=True)
    assert out["removed_toggle_targets"] == [header]
    text = rdl.read_text()
    assert f"<ToggleItem>{header}</ToggleItem>" not in text
    assert not [i for i in lint_report(str(rdl))["issues"] if i["rule"] == "dangling-toggle-item"]
    assert verify_report(str(rdl))["valid"] is True


def test_remove_column_without_toggle_targets_unaffected(rdl):
    from pbirb_mcp.ops.tablix_columns import remove_tablix_column

    _repro(rdl)
    out = remove_tablix_column(str(rdl), "MainTable", "Amount")
    assert "removed_toggle_targets" not in out
