"""copy_textbox_style(source, targets) — make textboxes look like another (#112).

The source's Textbox/Style (box + border), first paragraph's Style and
paragraph properties, and first run's Style are deep-copied onto every
target paragraph / run; CanGrow / CanShrink optionally. Values, actions
and names are never touched; the report is opened and saved once.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, q
from pbirb_mcp.ops.actions import set_textbox_action
from pbirb_mcp.ops.reader import get_textbox
from pbirb_mcp.ops.styling import copy_textbox_style, set_textbox_runs, set_textbox_style
from pbirb_mcp.ops.validate import validate_report
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
TARGETS = ["HeaderProductID", "HeaderProductName"]


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    set_textbox_style(
        str(dest),
        "HeaderAmount",
        background_color="#1F3864",
        color="#FFFFFF",
        font_weight="Bold",
        text_align="Center",
        border_style="Solid",
        border_color="#1F3864",
        padding_left="4pt",
        can_shrink=True,
    )
    return dest


def _style(path: Path, name: str):
    return get_textbox(str(path), name)["style"]


def _textbox(path: Path, name: str):
    return RDLDocument.open(path).root.find(f".//{{{RDL_NS}}}Textbox[@Name='{name}']")


class TestCopy:
    def test_targets_get_the_source_style(self, rdl_path):
        out = copy_textbox_style(str(rdl_path), "HeaderAmount", TARGETS)
        assert out == {
            "source": "HeaderAmount",
            "targets": TARGETS,
            "skipped": [],
            "ambiguous": [],
            "changed": ["border", "box", "paragraph", "run", "textbox.CanShrink"],
        }
        source = _style(rdl_path, "HeaderAmount")
        for name in TARGETS:
            assert _style(rdl_path, name) == source
            assert get_textbox(str(rdl_path), name)["can_shrink"] == "true"
        assert validate_report(str(rdl_path))["valid"]

    def test_every_run_of_a_multi_run_target(self, rdl_path):
        set_textbox_runs(
            str(rdl_path),
            "HeaderProductName",
            [{"text": "Product "}, {"text": "name", "font_style": "Italic"}],
        )
        copy_textbox_style(str(rdl_path), "HeaderAmount", ["HeaderProductName"])
        runs = get_textbox(str(rdl_path), "HeaderProductName")["runs"]
        assert [r["value"] for r in runs] == ["Product ", "name"]
        assert [r["style"] for r in runs] == [{"FontWeight": "Bold", "Color": "#FFFFFF"}] * 2

    def test_every_paragraph_and_paragraph_properties(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        src_para = doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='HeaderAmount']//{q('Paragraph')}")
        indent = copy.deepcopy(find_child(src_para, "Style"))
        indent.tag = q("LeftIndent")
        for child in list(indent):
            indent.remove(child)
        indent.text = "6pt"
        src_para.append(indent)
        doc.save()
        set_textbox_runs(
            str(rdl_path), "ProductID", [{"text": "a"}, {"text": "b", "paragraph_index": 1}]
        )
        copy_textbox_style(str(rdl_path), "HeaderAmount", ["ProductID"])
        paragraphs = _textbox(rdl_path, "ProductID").iter(q("Paragraph"))
        for paragraph in paragraphs:
            assert find_child(find_child(paragraph, "Style"), "TextAlign").text == "Center"
            assert find_child(paragraph, "LeftIndent").text == "6pt"
        assert validate_report(str(rdl_path))["valid"]

    def test_values_and_actions_untouched(self, rdl_path):
        set_textbox_action(str(rdl_path), "ProductID", "Hyperlink", "https://example.test")
        before = get_textbox(str(rdl_path), "ProductID")
        action = copy.deepcopy(find_child(_textbox(rdl_path, "ProductID"), "ActionInfo"))
        copy_textbox_style(str(rdl_path), "HeaderAmount", ["ProductID"])
        after = get_textbox(str(rdl_path), "ProductID")
        assert [r["value"] for r in after["runs"]] == [r["value"] for r in before["runs"]]
        from pbirb_mcp.core.mutation import fingerprint

        assert fingerprint(find_child(_textbox(rdl_path, "ProductID"), "ActionInfo")) == (
            fingerprint(action)
        )
        assert after["name"] == "ProductID"

    def test_properties_the_source_lacks_are_removed(self, rdl_path):
        set_textbox_style(str(rdl_path), "ProductID", vertical_align="Bottom", font_style="Italic")
        copy_textbox_style(str(rdl_path), "HeaderAmount", ["ProductID"])
        assert _style(rdl_path, "ProductID") == _style(rdl_path, "HeaderAmount")

    def test_second_identical_call_is_byte_identical(self, rdl_path):
        copy_textbox_style(str(rdl_path), "HeaderAmount", TARGETS)
        before = rdl_path.read_bytes()
        again = copy_textbox_style(str(rdl_path), "HeaderAmount", TARGETS)
        assert again["changed"] == []
        assert rdl_path.read_bytes() == before


class TestScopeAndFlags:
    def test_scope_subset(self, rdl_path):
        out = copy_textbox_style(str(rdl_path), "HeaderAmount", ["ProductID"], scope=["run"])
        assert out["changed"] == ["run", "textbox.CanShrink"]
        style = _style(rdl_path, "ProductID")
        assert "box" not in style and "border" not in style and "paragraph" not in style
        assert style["run"] == {"FontWeight": "Bold", "Color": "#FFFFFF"}

    def test_border_only(self, rdl_path):
        copy_textbox_style(
            str(rdl_path),
            "HeaderAmount",
            ["ProductID"],
            scope=["border"],
            include_textbox_flags=False,
        )
        style = _style(rdl_path, "ProductID")
        assert style["border"] == {"Style": "Solid", "Color": "#1F3864"}
        assert "box" not in style
        assert get_textbox(str(rdl_path), "ProductID")["can_shrink"] is None

    def test_flags_removed_when_source_lacks_them(self, rdl_path):
        # HeaderProductID carries CanGrow=true in the fixture; HeaderAmount too,
        # so give the source none and check the target loses its flags.
        doc = RDLDocument.open(rdl_path)
        src = doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='HeaderAmount']")
        for local in ("CanGrow", "CanShrink"):
            node = find_child(src, local)
            if node is not None:
                src.remove(node)
        doc.save()
        out = copy_textbox_style(str(rdl_path), "HeaderAmount", ["HeaderProductID"])
        assert "textbox.CanGrow" in out["changed"]
        assert find_child(_textbox(rdl_path, "HeaderProductID"), "CanGrow") is None

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"scope": ["font"]}, "scope"),
            ({"scope": []}, "scope"),
            ({"include_textbox_flags": "yes"}, "include_textbox_flags"),
            ({"strict": 1}, "strict"),
        ],
    )
    def test_bad_arguments_refused(self, rdl_path, kwargs, match):
        with pytest.raises(ValueError, match=match):
            copy_textbox_style(str(rdl_path), "HeaderAmount", TARGETS, **kwargs)

    def test_empty_targets_refused(self, rdl_path):
        with pytest.raises(ValueError, match="targets"):
            copy_textbox_style(str(rdl_path), "HeaderAmount", [])


class TestResolution:
    def test_skipped_ambiguous_and_repeats(self, rdl_path):
        doc = RDLDocument.open(rdl_path)
        cell = doc.root.find(f".//{{{RDL_NS}}}Textbox[@Name='ProductName']")
        doc.root.find(f".//{{{RDL_NS}}}Body/{{{RDL_NS}}}ReportItems").append(copy.deepcopy(cell))
        doc.save()
        out = copy_textbox_style(
            str(rdl_path), "HeaderAmount", ["ProductID", "ProductID", "Nope", "ProductName"]
        )
        assert out["targets"] == ["ProductID"]
        assert out["skipped"] == ["Nope"]
        assert out["ambiguous"] == ["ProductName"]

        before = rdl_path.read_bytes()
        with pytest.raises(AmbiguousElementError, match="nothing was written"):
            copy_textbox_style(
                str(rdl_path), "HeaderAmount", ["HeaderProductID", "ProductName"], strict=True
            )
        assert rdl_path.read_bytes() == before

    def test_missing_source_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            copy_textbox_style(str(rdl_path), "Nope", TARGETS)

    def test_source_in_targets_is_a_noop_for_itself(self, rdl_path):
        out = copy_textbox_style(str(rdl_path), "HeaderAmount", ["HeaderAmount"])
        assert out["changed"] == [] and out["targets"] == ["HeaderAmount"]

    def test_opens_and_saves_once(self, rdl_path, monkeypatch):
        saves = []
        real_save = RDLDocument.save

        def counting_save(self, *a, **kw):
            saves.append(1)
            return real_save(self, *a, **kw)

        monkeypatch.setattr(RDLDocument, "save", counting_save)
        copy_textbox_style(str(rdl_path), "HeaderAmount", TARGETS + ["ProductID", "Amount"])
        assert saves == [1]


def test_through_the_server(rdl_path):
    srv = MCPServer()
    register_all_tools(srv)
    resp = srv.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "copy_textbox_style",
                "arguments": {
                    "path": str(rdl_path),
                    "source": "HeaderAmount",
                    "targets": TARGETS,
                    "scope": ["box", "run"],
                },
            },
        }
    )
    result = resp["result"]
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["changed"] == ["box", "run", "textbox.CanShrink"]
