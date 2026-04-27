"""Embedded-image tool tests.

``add_embedded_image`` reads a real file off disk, base64-encodes its
bytes, and writes a ``<EmbeddedImage>`` entry under
``<EmbeddedImages>``. ``remove_embedded_image`` undoes that and tidies
the empty parent block. ``list_embedded_images`` is a read-only inventory
that returns names + MIME types.
"""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RDL_NS, find_child, find_children, q
from pbirb_mcp.ops.embedded_images import (
    add_embedded_image,
    list_embedded_images,
    remove_embedded_image,
)
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"

# A 1x1 transparent PNG — small enough to inline, real enough that a
# base64-decoded round-trip equals the bytes we wrote.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63faffff3f000005fe02fea3796300000000049454e4ae"
    "426082"
)


@pytest.fixture
def rdl_path(tmp_path: Path) -> Path:
    dest = tmp_path / "report.rdl"
    shutil.copy(FIXTURE, dest)
    return dest


@pytest.fixture
def png_path(tmp_path: Path) -> Path:
    p = tmp_path / "logo.png"
    p.write_bytes(_TINY_PNG)
    return p


def _embedded_block(rdl_path: Path):
    doc = RDLDocument.open(rdl_path)
    return find_child(doc.root, "EmbeddedImages")


# ---- add_embedded_image ---------------------------------------------------


class TestAddEmbeddedImage:
    def test_creates_block_when_absent(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        block = _embedded_block(rdl_path)
        assert block is not None
        entries = find_children(block, "EmbeddedImage")
        assert len(entries) == 1
        assert entries[0].get("Name") == "Logo"

    def test_writes_mime_type(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        entry = find_children(_embedded_block(rdl_path), "EmbeddedImage")[0]
        assert find_child(entry, "MIMEType").text == "image/png"

    def test_image_data_is_base64_of_file_bytes(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        entry = find_children(_embedded_block(rdl_path), "EmbeddedImage")[0]
        encoded = find_child(entry, "ImageData").text
        assert encoded is not None
        decoded = base64.b64decode(encoded)
        assert decoded == _TINY_PNG

    def test_appends_subsequent_images_to_block(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path), name="Logo", mime_type="image/png",
            image_path=str(png_path),
        )
        add_embedded_image(
            path=str(rdl_path), name="Banner", mime_type="image/png",
            image_path=str(png_path),
        )
        names = [
            e.get("Name")
            for e in find_children(_embedded_block(rdl_path), "EmbeddedImage")
        ]
        assert names == ["Logo", "Banner"]

    def test_duplicate_name_rejected(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path), name="Logo", mime_type="image/png",
            image_path=str(png_path),
        )
        with pytest.raises(ValueError):
            add_embedded_image(
                path=str(rdl_path), name="Logo", mime_type="image/png",
                image_path=str(png_path),
            )

    def test_unknown_mime_type_rejected(self, rdl_path, png_path):
        with pytest.raises(ValueError):
            add_embedded_image(
                path=str(rdl_path),
                name="Logo",
                mime_type="image/svg+xml",
                image_path=str(png_path),
            )

    def test_missing_image_file_raises(self, rdl_path, tmp_path):
        with pytest.raises(FileNotFoundError):
            add_embedded_image(
                path=str(rdl_path),
                name="Logo",
                mime_type="image/png",
                image_path=str(tmp_path / "nope.png"),
            )

    def test_round_trip_safe(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path), name="Logo", mime_type="image/png",
            image_path=str(png_path),
        )
        doc = RDLDocument.open(rdl_path)
        doc.validate()


# ---- list_embedded_images -------------------------------------------------


class TestListEmbeddedImages:
    def test_empty_when_block_absent(self, rdl_path):
        assert list_embedded_images(path=str(rdl_path)) == []

    def test_returns_name_and_mime_per_image(self, rdl_path, png_path, tmp_path):
        # A second tiny image — same bytes, different name + MIME.
        jpg_path = tmp_path / "logo.jpg"
        jpg_path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")
        add_embedded_image(
            path=str(rdl_path), name="Logo", mime_type="image/png",
            image_path=str(png_path),
        )
        add_embedded_image(
            path=str(rdl_path), name="Cover", mime_type="image/jpeg",
            image_path=str(jpg_path),
        )
        listing = list_embedded_images(path=str(rdl_path))
        assert listing == [
            {"name": "Logo", "mime_type": "image/png"},
            {"name": "Cover", "mime_type": "image/jpeg"},
        ]


# ---- remove_embedded_image ------------------------------------------------


class TestRemoveEmbeddedImage:
    def test_removes_named_image(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path), name="Logo", mime_type="image/png",
            image_path=str(png_path),
        )
        add_embedded_image(
            path=str(rdl_path), name="Banner", mime_type="image/png",
            image_path=str(png_path),
        )
        remove_embedded_image(path=str(rdl_path), name="Logo")
        names = [
            e.get("Name")
            for e in find_children(_embedded_block(rdl_path), "EmbeddedImage")
        ]
        assert names == ["Banner"]

    def test_removes_block_when_last_image_removed(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path), name="Logo", mime_type="image/png",
            image_path=str(png_path),
        )
        remove_embedded_image(path=str(rdl_path), name="Logo")
        assert _embedded_block(rdl_path) is None

    def test_unknown_name_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_embedded_image(path=str(rdl_path), name="Ghost")


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_three_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {t["name"] for t in listing}
        assert {
            "add_embedded_image",
            "list_embedded_images",
            "remove_embedded_image",
        } <= names
