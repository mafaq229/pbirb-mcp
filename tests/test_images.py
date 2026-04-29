"""Tests for the Phase 4 image-mutation tools.

``set_image_sizing`` and ``set_image_source`` operate on existing
``<Image>`` ReportItems. v0.2 had no post-create image edit tools —
the Sizing field was invisible to the API and repointing required
delete-and-readd. RAG-Report session feedback bug #15 cited the
missing repoint as a particularly common workaround.
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
from pbirb_mcp.ops.embedded_images import add_embedded_image
from pbirb_mcp.ops.images import set_image_sizing, set_image_source
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


def _png_file(tmp_path: Path, name: str = "logo.png") -> Path:
    """Write a tiny valid PNG so add_embedded_image accepts it."""
    p = tmp_path / name
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    p.write_bytes(png)
    return p


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


# ---- set_image_source ---------------------------------------------------


class TestSetImageSource:
    def test_repoints_to_existing_embedded_image(self, rdl_path, tmp_path):
        png_a = _png_file(tmp_path, "a.png")
        png_b = _png_file(tmp_path, "b.png")
        add_embedded_image(
            path=str(rdl_path),
            name="LogoA",
            mime_type="image/png",
            image_path=str(png_a),
        )
        add_embedded_image(
            path=str(rdl_path),
            name="LogoB",
            mime_type="image/png",
            image_path=str(png_b),
        )
        add_body_image(
            path=str(rdl_path),
            name="HeaderLogo",
            image_source="Embedded",
            value="LogoA",
            top="0in",
            left="0in",
            width="2in",
            height="1in",
        )
        result = set_image_source(
            path=str(rdl_path),
            image_name="HeaderLogo",
            embedded_name="LogoB",
        )
        assert result["changed"] is True
        img = _image(rdl_path, "HeaderLogo")
        assert find_child(img, "Source").text == "Embedded"
        assert find_child(img, "Value").text == "LogoB"

    def test_idempotent_when_already_pointing_there(self, rdl_path, tmp_path):
        png = _png_file(tmp_path)
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png),
        )
        add_body_image(
            path=str(rdl_path),
            name="HeaderLogo",
            image_source="Embedded",
            value="Logo",
            top="0in",
            left="0in",
            width="2in",
            height="1in",
        )
        before = (rdl_path).read_bytes()
        result = set_image_source(
            path=str(rdl_path), image_name="HeaderLogo", embedded_name="Logo"
        )
        assert result["changed"] is False
        assert (rdl_path).read_bytes() == before

    def test_refuses_unknown_embedded_name(self, rdl_path, tmp_path):
        png = _png_file(tmp_path)
        add_embedded_image(
            path=str(rdl_path),
            name="Real",
            mime_type="image/png",
            image_path=str(png),
        )
        add_body_image(
            path=str(rdl_path),
            name="HeaderLogo",
            image_source="Embedded",
            value="Real",
            top="0in",
            left="0in",
            width="2in",
            height="1in",
        )
        with pytest.raises(
            ElementNotFoundError, match="not found in <EmbeddedImages>"
        ):
            set_image_source(
                path=str(rdl_path),
                image_name="HeaderLogo",
                embedded_name="Ghost",
            )

    def test_switches_external_to_embedded(self, rdl_path, tmp_path):
        # Image starts as External; repointing must rewrite Source as
        # well as Value.
        png = _png_file(tmp_path)
        add_embedded_image(
            path=str(rdl_path),
            name="Embedded",
            mime_type="image/png",
            image_path=str(png),
        )
        add_body_image(
            path=str(rdl_path),
            name="ExternalLogo",
            image_source="External",
            value="http://x/img.png",
            top="0in",
            left="0in",
            width="2in",
            height="1in",
        )
        set_image_source(
            path=str(rdl_path),
            image_name="ExternalLogo",
            embedded_name="Embedded",
        )
        img = _image(rdl_path, "ExternalLogo")
        assert find_child(img, "Source").text == "Embedded"
        assert find_child(img, "Value").text == "Embedded"

    def test_unknown_image_rejected(self, rdl_path):
        with pytest.raises(ElementNotFoundError, match="no Image"):
            set_image_source(
                path=str(rdl_path),
                image_name="NoSuch",
                embedded_name="Whatever",
            )

    def test_round_trip_safe(self, rdl_path, tmp_path):
        png = _png_file(tmp_path)
        add_embedded_image(
            path=str(rdl_path),
            name="X",
            mime_type="image/png",
            image_path=str(png),
        )
        add_body_image(
            path=str(rdl_path),
            name="Img",
            image_source="External",
            value="http://x",
            top="0in",
            left="0in",
            width="1in",
            height="1in",
        )
        set_image_source(path=str(rdl_path), image_name="Img", embedded_name="X")
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

    def test_set_image_source_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert "set_image_source" in names
