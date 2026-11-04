"""Server-side argument validation and client-safe schemas (issue #19)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.argcheck import InvalidArguments, client_schema, validate_arguments
from pbirb_mcp.server import TRANSACTION_CONTROL_TOOLS, MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "pbi_paginated_minimal.rdl"
CHART = FIXTURES / "pbi_chart_rich.rdl"

_SERVER = MCPServer()
register_all_tools(_SERVER)


@pytest.fixture
def server() -> MCPServer:
    return _SERVER


@pytest.fixture
def rdl(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(MINIMAL, dest)
    return dest


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
    return json.loads(result["content"][0]["text"]), bool(result.get("isError"))


# ---- every tool, every parameter ----------------------------------------------

_JSON_TYPE_OF = [
    ("string", "false"),
    ("integer", 7),
    ("boolean", True),
    ("number", 1.5),
    ("array", []),
    ("object", {}),
]


def _allowed_types(schema: dict) -> set:
    alts = schema.get("oneOf") or schema.get("anyOf")
    if alts:
        out = set()
        for alt in alts:
            out |= _allowed_types(alt)
        return out
    t = schema.get("type")
    if t is None:
        return set()
    names = set(t if isinstance(t, list) else [t])
    if "number" in names:
        names.add("integer")
    return names


def _wrong_value(schema: dict):
    allowed = _allowed_types(schema)
    if not allowed:
        return None
    for json_type, value in _JSON_TYPE_OF:
        if json_type not in allowed:
            return value
    return None


def _param_cases():
    for tool in _SERVER._tools.values():
        for param, sub in tool.input_schema["properties"].items():
            if param == "transaction_id":
                continue  # routed before validation; covered separately
            value = _wrong_value(sub)
            if value is not None:
                yield pytest.param(tool.name, param, value, id=f"{tool.name}.{param}")


@pytest.mark.parametrize(("tool", "param", "value"), list(_param_cases()))
def test_wrong_type_is_rejected_for_every_parameter(server, tool, param, value):
    payload, is_error = _call(server, tool, **{param: value})
    assert is_error
    assert payload["error_type"] == "InvalidArguments"
    assert payload["param"] == param


def test_every_tool_was_covered():
    covered = {case.values[0] for case in _param_cases()}
    # Every tool with a typed parameter (besides transaction_id) is swept.
    expected = {
        t.name
        for t in _SERVER._tools.values()
        if any(p != "transaction_id" for p in t.input_schema["properties"])
    }
    assert covered == expected


# ---- the issue's cases: refused, file unchanged ---------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "param"),
    [
        ("set_textbox_style", {"textbox_name": "HeaderAmount", "can_grow": "false"}, "can_grow"),
        (
            "add_tablix_column",
            {
                "tablix_name": "MainTable",
                "column_name": "C",
                "expression": "=1",
                "position": "1",
            },
            "position",
        ),
        (
            "set_cell_span",
            {
                "tablix_name": "MainTable",
                "row_index": 1,
                "column_name": "ProductID",
                "col_span": "2",
            },
            "col_span",
        ),
        ("set_parameter_layout", {"rows": True, "columns": 1}, "rows"),
    ],
)
def test_issue_cases_are_refused_and_file_unchanged(server, rdl, tool, args, param):
    before = rdl.read_bytes()
    payload, is_error = _call(server, tool, path=str(rdl), **args)
    assert is_error and payload["error_type"] == "InvalidArguments"
    assert payload["param"] == param
    assert rdl.read_bytes() == before


def test_set_chart_legend_visible_string_refused(server, tmp_path):
    c = tmp_path / "chart.rdl"
    shutil.copy(CHART, c)
    before = c.read_bytes()
    payload, is_error = _call(
        server, "set_chart_legend", path=str(c), chart_name="SalesByProduct", visible="false"
    )
    assert is_error and payload == {
        "error_type": "InvalidArguments",
        "message": 'visible: expected boolean, got string "false"',
        "param": "visible",
        "expected": "boolean",
        "got": 'string "false"',
    }
    assert c.read_bytes() == before


def test_restore_force_string_refused(server, rdl):
    backup, _ = _call(server, "backup_report", path=str(rdl))
    _call(server, "set_textbox_value", path=str(rdl), textbox_name="HeaderAmount", value="new")
    before = rdl.read_bytes()
    payload, is_error = _call(
        server, "restore_from_backup", backup_path=backup["backup"], force="false"
    )
    assert is_error and payload["param"] == "force"
    assert rdl.read_bytes() == before


def test_missing_argument_is_structured(server):
    payload, is_error = _call(server, "describe_report")
    assert is_error
    assert payload["error_type"] == "InvalidArguments"
    assert payload["param"] == "path"
    assert "TypeError" not in json.dumps(payload)


def test_extra_argument_is_structured(server, rdl):
    payload, is_error = _call(server, "describe_report", path=str(rdl), bogus=1)
    assert is_error
    assert payload["error_type"] == "InvalidArguments"
    assert payload["param"] == "bogus"
    assert "accepted parameters" in payload["message"]


def test_null_still_accepted_for_nullable_params(server, rdl):
    payload, is_error = _call(
        server, "set_textbox_style", path=str(rdl), textbox_name="HeaderAmount", font_weight=None
    )
    assert not is_error, payload


def test_enum_violation_is_rejected(server, rdl):
    payload, is_error = _call(server, "set_page_orientation", path=str(rdl), orientation="Sideways")
    assert is_error and payload["error_type"] == "InvalidArguments"
    assert payload["param"] == "orientation"


def test_digit_string_column_handle_is_an_index(server, rdl):
    # `column` is published as a string; "0" must mean index 0.
    payload, is_error = _call(
        server, "set_column_width", path=str(rdl), tablix_name="MainTable", column="0", width="2in"
    )
    assert not is_error, payload


def test_nested_items_are_validated(server, rdl):
    payload, is_error = _call(server, "apply_edits", path=str(rdl), ops=[{"tool": 5}])
    # ops items are free-form objects; the batch reports a bad op itself.
    assert payload is not None
    payload, is_error = _call(server, "apply_edits", path=str(rdl), ops="nope")
    assert is_error and payload["param"] == "ops"


# ---- published schemas ------------------------------------------------------------


def _walk(schema):
    yield schema
    if isinstance(schema, dict):
        for v in schema.values():
            yield from _walk(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _walk(v)


def test_no_published_schema_has_type_lists_or_oneof(server):
    for tool in server._tools_list():
        for node in _walk(tool["inputSchema"]):
            if isinstance(node, dict):
                assert not isinstance(node.get("type"), list), tool["name"]
                assert "oneOf" not in node and "anyOf" not in node, tool["name"]
                assert None not in node.get("enum", []), tool["name"]


def test_every_file_tool_declares_transaction_id(server):
    for tool in server._tools_list():
        props = tool["inputSchema"]["properties"]
        if tool["name"] in TRANSACTION_CONTROL_TOOLS:
            continue
        if "path" in props or "src_path" in props:
            assert "transaction_id" in props, tool["name"]


def test_start_editing_transaction_has_no_transaction_id(server):
    tool = next(t for t in server._tools_list() if t["name"] == "start_editing_transaction")
    assert "transaction_id" not in tool["inputSchema"]["properties"]


def test_transaction_id_type_checked(server, rdl):
    payload, is_error = _call(server, "describe_report", path=str(rdl), transaction_id=7)
    assert is_error
    assert payload["error_type"] == "TransactionError"


# ---- the validator itself -----------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "value", "ok"),
    [
        ({"type": "integer"}, 1, True),
        ({"type": "integer"}, True, False),
        ({"type": "integer"}, "1", False),
        ({"type": "number"}, 1, True),
        ({"type": "number"}, 1.5, True),
        ({"type": "boolean"}, "true", False),
        ({"type": ["string", "null"]}, None, True),
        ({"type": "string"}, None, False),
        ({"type": "integer", "minimum": 1}, 0, False),
        ({"enum": ["a", "b"]}, "c", False),
        ({"type": "array", "items": {"type": "string"}}, ["a", 1], False),
        ({"type": "array", "minItems": 1}, [], False),
        ({"oneOf": [{"type": "integer"}, {"type": "string"}]}, 1, True),
        ({"oneOf": [{"type": "integer"}, {"type": "string"}]}, True, False),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": False,
            },
            {"a": "x"},
            True,
        ),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            {},
            False,
        ),
        (
            {"type": "object", "properties": {}, "additionalProperties": False},
            {"z": 1},
            False,
        ),
    ],
)
def test_validator_cases(schema, value, ok):
    wrapped = {"type": "object", "properties": {"v": schema}, "required": ["v"]}
    if ok:
        validate_arguments(wrapped, {"v": value})
    else:
        with pytest.raises(InvalidArguments):
            validate_arguments(wrapped, {"v": value})


def test_client_schema_rewrites():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": ["string", "null"], "enum": [None, "x"]},
            "b": {"oneOf": [{"type": "integer"}, {"type": "string"}], "description": "B."},
            "c": {"type": "array", "items": {"oneOf": [{"type": "string"}, {"type": "object"}]}},
        },
    }
    out = client_schema(schema)
    assert out["properties"]["a"] == {"type": "string", "enum": ["x"]}
    assert out["properties"]["b"]["type"] == "string"
    assert "integer" in out["properties"]["b"]["description"]
    assert out["properties"]["c"]["items"]["type"] == "object"
    # The original is untouched (the server validates against it).
    assert schema["properties"]["a"]["type"] == ["string", "null"]
