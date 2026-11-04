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
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import find_child, q
from pbirb_mcp.ops.body import add_body_textbox
from pbirb_mcp.ops.header_footer import add_header_image, add_header_textbox, set_page_header
from pbirb_mcp.ops.layout import add_line
from pbirb_mcp.ops.lint import lint_report
from pbirb_mcp.ops.validate import validate_report
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
    matches = list(doc.root.xpath(".//*[@Name=$n]", n=name))
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
            top="0in",
            left="0in",
            width="1in",
            height="0.5in",
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
        # The toggle must sit outside the tablix it hides (#42: a cell of
        # MainTable such as HeaderProductID used to be accepted).
        add_body_textbox(str(rdl_path), "Toggle", "+", "0.1in", "0.5in", "0.3in", "0.25in")
        set_element_visibility(
            path=str(rdl_path),
            element_name="MainTable",
            hidden_expression="true",
            toggle_textbox="Toggle",
        )
        node = _named(rdl_path, "MainTable")
        vis = find_child(node, "Visibility")
        assert find_child(vis, "ToggleItem").text == "Toggle"

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
        children_locals = [etree_node.tag.split("}", 1)[-1] for etree_node in list(node)]
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


# ---- #42: validation, keep-toggle, clear, Line, lint ----------------------


@pytest.fixture
def toggles_path(rdl_path: Path) -> Path:
    add_body_textbox(str(rdl_path), "Toggle", "+", "0.1in", "0.5in", "0.3in", "0.25in")
    add_header_textbox(str(rdl_path), "HdrToggle", "+", "0in", "0in", "0.3in", "0.25in")
    return rdl_path


def _visibility(path: Path, name: str):
    # Report items only — a dataset Field can share a textbox's name.
    doc = RDLDocument.open(path)
    item = doc.root.xpath(
        ".//*[@Name=$n and local-name()!='Field' and local-name()!='DataSet']", n=name
    )[0]
    return find_child(item, "Visibility")


class TestValidation:
    @pytest.mark.parametrize(
        "target, hidden, toggle, match",
        [
            ("MainTable", "true", "NoSuchTextbox", "does not name any textbox"),
            ("Toggle", "true", "Toggle", "the item itself"),
            ("MainTable", "true", "HdrToggle", "same region"),
            ("MainTable", "true", "ProductID", "inside Tablix 'MainTable'"),
            ("MainTable", "true", "MainDataset", "names a DataSet, not a Textbox"),
            ("Toggle", "", None, "neither a Boolean literal"),
            ("Toggle", "Yes", None, "neither a Boolean literal"),
            ("Toggle", "=", None, "empty expression body"),
            ("Toggle", '="abc', None, "unterminated string"),
            ("Toggle", "=IIf(A, B", None, "unbalanced parentheses"),
        ],
    )
    def test_rejected_and_file_untouched(self, toggles_path, target, hidden, toggle, match):
        before = toggles_path.read_bytes()
        kwargs = {} if toggle is None else {"toggle_textbox": toggle}
        with pytest.raises(ValueError, match=match):
            set_element_visibility(str(toggles_path), target, hidden, **kwargs)
        assert toggles_path.read_bytes() == before

    def test_toggle_naming_a_non_textbox_rejected(self, toggles_path):
        add_line(str(toggles_path), "Rule", "1.1in", "0.5in", "4in", "0in")
        with pytest.raises(ValueError, match="names a Line, not a Textbox"):
            set_element_visibility(str(toggles_path), "MainTable", "true", "Rule")

    def test_toggle_cell_outside_target_tablix_rejected(self, toggles_path):
        # A cell of MainTable cannot toggle a body textbox outside it.
        with pytest.raises(ValueError, match="not inside that tablix"):
            set_element_visibility(str(toggles_path), "Toggle", "true", "HeaderAmount")

    def test_cell_toggling_a_cell_of_the_same_tablix_accepted(self, toggles_path):
        set_element_visibility(str(toggles_path), "Amount", "true", "HeaderAmount")
        assert find_child(_visibility(toggles_path, "Amount"), "ToggleItem").text == "HeaderAmount"
        assert lint_report(str(toggles_path))["issues"] == []

    def test_boolean_literal_case_insensitive(self, toggles_path):
        result = set_element_visibility(str(toggles_path), "Toggle", " TRUE ")
        assert result["hidden_expression"] == "true"
        assert find_child(_visibility(toggles_path, "Toggle"), "Hidden").text == "true"

    def test_expression_accepted(self, toggles_path):
        set_element_visibility(
            str(toggles_path), "MainTable", "=Parameters!DateFrom.Value Is Nothing"
        )
        hidden = find_child(_visibility(toggles_path, "MainTable"), "Hidden")
        assert hidden.text == "=Parameters!DateFrom.Value Is Nothing"


class TestToggleAndClear:
    def test_omitting_toggle_keeps_existing(self, toggles_path):
        set_element_visibility(str(toggles_path), "MainTable", "true", "Toggle")
        result = set_element_visibility(
            str(toggles_path), "MainTable", "=Parameters!DateFrom.Value Is Nothing"
        )
        assert result["toggle_textbox"] == "Toggle"
        vis = _visibility(toggles_path, "MainTable")
        assert find_child(vis, "ToggleItem").text == "Toggle"
        assert find_child(vis, "Hidden").text == "=Parameters!DateFrom.Value Is Nothing"

    @pytest.mark.parametrize("remove", ["", None])
    def test_empty_or_null_toggle_removes_it(self, toggles_path, remove):
        set_element_visibility(str(toggles_path), "MainTable", "true", "Toggle")
        result = set_element_visibility(str(toggles_path), "MainTable", "true", remove)
        assert result["changed"] is True
        assert result["toggle_textbox"] is None
        assert find_child(_visibility(toggles_path, "MainTable"), "ToggleItem") is None

    def test_clear_removes_visibility(self, toggles_path):
        set_element_visibility(str(toggles_path), "MainTable", "true", "Toggle")
        result = set_element_visibility(str(toggles_path), "MainTable", clear=True)
        assert result["changed"] is True
        assert _visibility(toggles_path, "MainTable") is None
        assert validate_report(str(toggles_path))["valid"]

    def test_clear_without_visibility_is_a_no_op(self, toggles_path):
        before = toggles_path.read_bytes()
        result = set_element_visibility(str(toggles_path), "MainTable", clear=True)
        assert result["changed"] is False
        assert toggles_path.read_bytes() == before

    def test_clear_with_hidden_rejected(self, toggles_path):
        with pytest.raises(ValueError, match="clear=true"):
            set_element_visibility(str(toggles_path), "MainTable", "true", clear=True)

    def test_hidden_required_without_clear(self, toggles_path):
        with pytest.raises(ValueError, match="hidden_expression is required"):
            set_element_visibility(str(toggles_path), "MainTable")

    def test_identical_call_is_unchanged_and_not_saved(self, toggles_path):
        set_element_visibility(str(toggles_path), "MainTable", "true", "Toggle")
        before = toggles_path.read_bytes()
        result = set_element_visibility(str(toggles_path), "MainTable", "True", "Toggle")
        assert result["changed"] is False
        assert toggles_path.read_bytes() == before

    def test_line_is_a_valid_target(self, toggles_path):
        add_line(str(toggles_path), "Rule", "1.1in", "0.5in", "4in", "0in")
        result = set_element_visibility(str(toggles_path), "Rule", "true")
        assert result["kind"] == "Line"
        assert result["changed"] is True
        assert find_child(_visibility(toggles_path, "Rule"), "Hidden").text == "true"
        assert validate_report(str(toggles_path))["valid"]


class TestDanglingToggleLint:
    def _hand_write_toggle(self, path: Path, toggle: str) -> None:
        doc = RDLDocument.open(path)
        tablix = doc.root.find(f".//{q('Tablix')}[@Name='MainTable']")
        vis = tablix.makeelement(q("Visibility"), {})
        hidden = vis.makeelement(q("Hidden"), {})
        hidden.text = "true"
        vis.append(hidden)
        item = vis.makeelement(q("ToggleItem"), {})
        item.text = toggle
        vis.append(item)
        tablix.append(vis)
        doc.save()

    @pytest.mark.parametrize("toggle", ["NoSuchTextbox", "ProductID", "HdrToggle"])
    def test_lint_flags_hand_written_dangling_toggle(self, toggles_path, toggle):
        self._hand_write_toggle(toggles_path, toggle)
        issues = [
            i
            for i in lint_report(str(toggles_path))["issues"]
            if i["rule"] == "dangling-toggle-item"
        ]
        assert len(issues) == 1
        assert issues[0]["severity"] == "error"
        assert toggle in issues[0]["message"]

    def test_lint_accepts_valid_toggle(self, toggles_path):
        set_element_visibility(str(toggles_path), "MainTable", "true", "Toggle")
        assert lint_report(str(toggles_path))["issues"] == []


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_tool_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "set_element_visibility" in names

    def test_input_schema_required_fields(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        tool = next(t for t in listing if t["name"] == "set_element_visibility")
        # hidden_expression is required unless clear=true (#42), which the
        # handler enforces.
        assert set(tool["inputSchema"]["required"]) == {"path", "element_name"}
        assert {"hidden_expression", "toggle_textbox", "clear"} <= set(
            tool["inputSchema"]["properties"]
        )
