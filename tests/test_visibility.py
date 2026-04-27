"""Conditional visibility tool tests.

``set_element_visibility`` writes ``<Visibility>`` on any named ReportItem —
Tablix, Textbox, Image, Rectangle, Subreport, Chart. Group-level visibility
is a separate concern handled by ``set_group_visibility`` (commit 9), and
detail-row visibility by ``set_detail_row_visibility`` (commit 10).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.header_footer import add_header_image, add_header_textbox, set_page_header
from pbirb_mcp.ops.visibility import set_element_visibility
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _named(rdl_path: Path, name: str):
    doc = RDLDocument.open(rdl_path)
    matches = list(
        doc.root.xpath(".//*[@Name=$n]", n=name)
    )
    return matches[0] if matches else None


# ---- happy path on each element kind --------------------------------------


class TestSetElementVisibility:
    def test_on_tablix(self, rdl_path):
        set_element_visibility(
            path=str(rdl_path),
            element_name="MainTable",
            hidden_expression="=Parameters!HideTable.Value",
        )
        node = _named(rdl_path, "MainTable")
        vis = find_child(node, "Visibility")
        assert vis is not None
        assert find_child(vis, "Hidden").text == "=Parameters!HideTable.Value"

    def test_on_textbox(self, rdl_path):
        # Use a textbox that already exists in the fixture body.
        set_element_visibility(
            path=str(rdl_path),
            element_name="HeaderAmount",
            hidden_expression="false",
        )
        node = _named(rdl_path, "HeaderAmount")
        vis = find_child(node, "Visibility")
        assert vis is not None
        assert find_child(vis, "Hidden").text == "false"

    def test_on_image(self, rdl_path):
        set_page_header(path=str(rdl_path), height="0.5in")
        add_header_image(
            path=str(rdl_path),
            name="Logo",
            image_source="External",
            value="https://example.com/logo.png",
            top="0in", left="0in", width="1in", height="0.5in",
        )
        set_element_visibility(
            path=str(rdl_path),
            element_name="Logo",
            hidden_expression="=Parameters!HideLogo.Value",
        )
        node = _named(rdl_path, "Logo")
        vis = find_child(node, "Visibility")
        assert find_child(vis, "Hidden").text == "=Parameters!HideLogo.Value"

    def test_with_toggle_textbox(self, rdl_path):
        set_element_visibility(
            path=str(rdl_path),
            element_name="MainTable",
            hidden_expression="true",
            toggle_textbox="HeaderProductID",
        )
        node = _named(rdl_path, "MainTable")
        vis = find_child(node, "Visibility")
        assert find_child(vis, "ToggleItem").text == "HeaderProductID"

    def test_replaces_existing_visibility(self, rdl_path):
        set_element_visibility(
            path=str(rdl_path),
            element_name="MainTable",
            hidden_expression="=A",
        )
        set_element_visibility(
            path=str(rdl_path),
            element_name="MainTable",
            hidden_expression="=B",
        )
        node = _named(rdl_path, "MainTable")
        # Exactly one Visibility, with the new expression.
        vis_nodes = node.findall(q("Visibility"))
        assert len(vis_nodes) == 1
        assert find_child(vis_nodes[0], "Hidden").text == "=B"

    def test_visibility_inserted_before_style(self, rdl_path):
        # Per RDL emitter convention, <Style> is typically the last child;
        # Visibility should come before it.
        set_element_visibility(
            path=str(rdl_path),
            element_name="HeaderAmount",
            hidden_expression="false",
        )
        node = _named(rdl_path, "HeaderAmount")
        children_locals = [
            etree_node.tag.split("}", 1)[-1] for etree_node in list(node)
        ]
        # If both are present, Visibility must come before Style.
        if "Style" in children_locals and "Visibility" in children_locals:
            assert children_locals.index("Visibility") < children_locals.index("Style")


# ---- error paths ----------------------------------------------------------


class TestErrors:
    def test_unknown_element_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_element_visibility(
                path=str(rdl_path),
                element_name="NoSuchThing",
                hidden_expression="true",
            )

    def test_refuses_to_target_a_group(self, rdl_path):
        # Groups have their own tool (set_group_visibility); refuse here so
        # callers don't accidentally write Visibility on a <Group> element
        # instead of its <TablixMember>.
        with pytest.raises(ElementNotFoundError):
            set_element_visibility(
                path=str(rdl_path),
                element_name="Details",
                hidden_expression="true",
            )

    def test_refuses_to_target_a_dataset(self, rdl_path):
        # DataSets have a Name attribute too — make sure we only resolve
        # ReportItem-derived elements.
        with pytest.raises(ElementNotFoundError):
            set_element_visibility(
                path=str(rdl_path),
                element_name="MainDataset",
                hidden_expression="true",
            )

    def test_round_trip_safe(self, rdl_path):
        set_element_visibility(
            path=str(rdl_path),
            element_name="MainTable",
            hidden_expression="false",
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
        assert "set_element_visibility" in names

    def test_input_schema_required_fields(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        tool = next(t for t in listing if t["name"] == "set_element_visibility")
        assert set(tool["inputSchema"]["required"]) == {
            "path",
            "element_name",
            "hidden_expression",
        }
