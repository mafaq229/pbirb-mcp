"""Tests for the Phase 4 image-mutation tools.

``set_image_sizing`` operates on existing ``<Image>`` ReportItems —
v0.2 had no post-create image edit tool for the Sizing field.
``set_image_source`` is added in commit 20 (this file extends).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child
from pbirb_mcp.ops.body import add_body_image
from pbirb_mcp.ops.images import set_image_sizing
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _image(rdl_path: Path, name: str) -> etree._Element:
    doc = RDLDocument.open(rdl_path)
    return doc.root.find(f".//{{{RDL_NS}}}Image[@Name='{name}']")


# ---- set_image_sizing ---------------------------------------------------


class TestSetImageSizing:
    def test_changes_sizing_value(self, rdl_path):
        # Add an image first so we have something to size.
        add_body_image(
            path=str(rdl_path),
            name="Logo",
            image_source="External",
            value="http://example.com/logo.png",
            top="0in",
            left="0in",
            width="2in",
            height="1in",
        )
        # Default is FitProportional; flip to Clip.
        result = set_image_sizing(
            path=str(rdl_path), image_name="Logo", sizing="Clip"
        )
        assert result["name"] == "Logo"
        assert result["kind"] == "Image"
        assert result["changed"] is True
        img = _image(rdl_path, "Logo")
        assert find_child(img, "Sizing").text == "Clip"

    def test_idempotent_when_unchanged(self, rdl_path):
        add_body_image(
            path=str(rdl_path),
            name="Logo",
            image_source="External",
            value="http://x/img.png",
            top="0in",
            left="0in",
            width="2in",
            height="1in",
        )
        # Default is FitProportional. Setting again is a no-op.
        before = (rdl_path).read_bytes()
        result = set_image_sizing(
            path=str(rdl_path), image_name="Logo", sizing="FitProportional"
        )
        assert result["changed"] is False
        assert (rdl_path).read_bytes() == before

    def test_invalid_sizing_rejected(self, rdl_path):
        with pytest.raises(ValueError, match="not valid"):
            set_image_sizing(
                path=str(rdl_path), image_name="Logo", sizing="StretchPlease"
            )

    def test_unknown_image_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            set_image_sizing(
                path=str(rdl_path), image_name="NoSuchImage", sizing="Fit"
            )

    def test_round_trip_safe(self, rdl_path):
        add_body_image(
            path=str(rdl_path),
            name="Logo",
            image_source="External",
            value="http://x/img.png",
            top="0in",
            left="0in",
            width="2in",
            height="1in",
        )
        set_image_sizing(path=str(rdl_path), image_name="Logo", sizing="AutoSize")
        RDLDocument.open(rdl_path).validate()


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_set_image_sizing_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "set_image_sizing" in names
