"""Tests for update_parameter_advanced.

Covers the four boolean flags on a <ReportParameter>: multi_value,
hidden, allow_null (writes <Nullable>), and allow_blank.

Cascading parameters (the plan's ``depends_on``) are NOT a separate
flag in RDL — they're inferred from =Parameters!X.Value references in a
lookup dataset's QueryParameters. To wire one parameter to depend on
another, combine set_parameter_available_values(source='query') with
add_query_parameter(...) on that lookup dataset.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.parameters import update_parameter_advanced
from pbirb_mcp.ops.reader import get_parameters
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _flags_from_reader(rdl_path: Path, name: str) -> dict:
    return {
        p["name"]: p
        for p in get_parameters(path=str(rdl_path))
    }[name]


# ---- happy path ----------------------------------------------------------


class TestUpdateAdvanced:
    def test_multi_value_true(self, rdl_path):
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            multi_value=True,
        )
        assert _flags_from_reader(rdl_path, "DateFrom")["multi_value"] is True

    def test_hidden_true(self, rdl_path):
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            hidden=True,
        )
        assert _flags_from_reader(rdl_path, "DateFrom")["hidden"] is True

    def test_allow_null_writes_nullable(self, rdl_path):
        # The RDL element is <Nullable>; the user-facing flag we expose is
        # allow_null because that's what Report Builder's UI calls it.
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            allow_null=True,
        )
        doc = RDLDocument.open(rdl_path)
        p = doc.root.find(f".//{{{RDL_NS}}}ReportParameter[@Name='DateFrom']")
        assert find_child(p, "Nullable").text == "true"

    def test_allow_blank_true(self, rdl_path):
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            allow_blank=True,
        )
        assert _flags_from_reader(rdl_path, "DateFrom")["allow_blank"] is True

    def test_all_flags_at_once(self, rdl_path):
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            multi_value=True,
            hidden=False,
            allow_null=True,
            allow_blank=True,
        )
        flags = _flags_from_reader(rdl_path, "DateFrom")
        assert flags["multi_value"] is True
        assert flags["hidden"] is False
        assert flags["nullable"] is True
        assert flags["allow_blank"] is True

    def test_false_values_persist_explicitly(self, rdl_path):
        # Writing False should explicitly write "false", not omit the element.
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            hidden=False,
        )
        doc = RDLDocument.open(rdl_path)
        p = doc.root.find(f".//{{{RDL_NS}}}ReportParameter[@Name='DateFrom']")
        hidden = find_child(p, "Hidden")
        assert hidden is not None and hidden.text == "false"


# ---- partial / no-op ------------------------------------------------------


class TestPartial:
    def test_partial_update_leaves_unspecified_flags_alone(self, rdl_path):
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            multi_value=True,
        )
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            hidden=True,
        )
        flags = _flags_from_reader(rdl_path, "DateFrom")
        # Both flags ended up set; neither was reset by the second call.
        assert flags["multi_value"] is True
        assert flags["hidden"] is True

    def test_no_args_is_no_op(self, rdl_path):
        before = rdl_path.read_bytes()
        update_parameter_advanced(path=str(rdl_path), name="DateFrom")
        assert rdl_path.read_bytes() == before

    def test_repeat_overwrites_in_place(self, rdl_path):
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            multi_value=True,
        )
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            multi_value=False,
        )
        doc = RDLDocument.open(rdl_path)
        p = doc.root.find(f".//{{{RDL_NS}}}ReportParameter[@Name='DateFrom']")
        # Exactly one MultiValue element with the latest value.
        mvs = p.findall(q("MultiValue"))
        assert len(mvs) == 1 and mvs[0].text == "false"


# ---- error paths ----------------------------------------------------------


class TestErrors:
    def test_unknown_parameter_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            update_parameter_advanced(
                path=str(rdl_path),
                name="Ghost",
                multi_value=True,
            )

    def test_round_trip_safe(self, rdl_path):
        update_parameter_advanced(
            path=str(rdl_path),
            name="DateFrom",
            multi_value=True,
            allow_null=True,
            allow_blank=True,
            hidden=False,
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "update_parameter_advanced" in names
