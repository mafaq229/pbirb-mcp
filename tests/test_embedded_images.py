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
from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import find_child, find_children, q
from pbirb_mcp.ops.embedded_images import (
    add_embedded_image,
    get_embedded_image_data,
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
    "0000000b49444154789c6360000200000500017a5eab3f0000000049454e44ae"
    "426082"
)
# SOI, JFIF APP0, a 3x2 baseline SOF0, EOI: structurally a complete JPEG.
_TINY_JPEG = (
    b"\xff\xd8"
    b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xc0\x00\x0b\x08\x00\x02\x00\x03\x01\x01\x11\x00"
    b"\xff\xd9"
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
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        add_embedded_image(
            path=str(rdl_path),
            name="Banner",
            mime_type="image/png",
            image_path=str(png_path),
        )
        names = [e.get("Name") for e in find_children(_embedded_block(rdl_path), "EmbeddedImage")]
        assert names == ["Logo", "Banner"]

    def test_duplicate_name_rejected(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        with pytest.raises(ValueError):
            add_embedded_image(
                path=str(rdl_path),
                name="Logo",
                mime_type="image/png",
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

    def test_mime_mismatch_rejected(self, rdl_path, png_path):
        """PNG bytes claimed as image/jpeg fail the magic-byte sniff. Without
        this check Report Builder would only fail at preview time."""
        with pytest.raises(ValueError) as exc:
            add_embedded_image(
                path=str(rdl_path),
                name="Logo",
                mime_type="image/jpeg",
                image_path=str(png_path),
            )
        # Error message names both the claim and the detected format so the
        # caller can self-correct.
        msg = str(exc.value)
        assert "image/jpeg" in msg
        assert "image/png" in msg

    def test_unrecognised_bytes_rejected(self, rdl_path, tmp_path):
        """A file that isn't a recognised image format at all is rejected."""
        bogus = tmp_path / "not_an_image.png"
        bogus.write_bytes(b"this is plain text, not an image")
        with pytest.raises(ValueError) as exc:
            add_embedded_image(
                path=str(rdl_path),
                name="Logo",
                mime_type="image/png",
                image_path=str(bogus),
            )
        assert "magic bytes" in str(exc.value)

    def test_x_png_alias_accepts_png_bytes(self, rdl_path, png_path):
        """image/x-png is an RDL alias for image/png — PNG bytes should pass."""
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/x-png",
            image_path=str(png_path),
        )
        entry = find_children(_embedded_block(rdl_path), "EmbeddedImage")[0]
        assert find_child(entry, "MIMEType").text == "image/x-png"

    def test_round_trip_safe(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
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
        jpg_path.write_bytes(_TINY_JPEG)
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        add_embedded_image(
            path=str(rdl_path),
            name="Cover",
            mime_type="image/jpeg",
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
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        add_embedded_image(
            path=str(rdl_path),
            name="Banner",
            mime_type="image/png",
            image_path=str(png_path),
        )
        remove_embedded_image(path=str(rdl_path), name="Logo")
        names = [e.get("Name") for e in find_children(_embedded_block(rdl_path), "EmbeddedImage")]
        assert names == ["Banner"]

    def test_removes_block_when_last_image_removed(self, rdl_path, png_path):
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        remove_embedded_image(path=str(rdl_path), name="Logo")
        assert _embedded_block(rdl_path) is None

    def test_unknown_name_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError):
            remove_embedded_image(path=str(rdl_path), name="Ghost")

    def test_refuses_when_referenced_by_body_image(self, rdl_path, png_path):
        """An Image element with Source=Embedded + Value=<name> blocks
        removal; the error names the offending Image so the caller knows
        what to fix."""
        from pbirb_mcp.ops.body import add_body_image

        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        add_body_image(
            path=str(rdl_path),
            name="HeaderLogo",
            image_source="Embedded",
            value="Logo",
            top="0in",
            left="0in",
            width="1in",
            height="0.5in",
        )

        with pytest.raises(ValueError) as exc:
            remove_embedded_image(path=str(rdl_path), name="Logo")
        msg = str(exc.value)
        assert "HeaderLogo" in msg
        assert "force=True" in msg

        # The image is still there.
        assert _embedded_block(rdl_path) is not None
        assert any(
            e.get("Name") == "Logo"
            for e in find_children(_embedded_block(rdl_path), "EmbeddedImage")
        )

    def test_force_bypasses_reference_check(self, rdl_path, png_path):
        """force=True deletes the image even when references exist; the
        result echoes the force flag so the caller knows what happened."""
        from pbirb_mcp.ops.body import add_body_image

        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png_path),
        )
        add_body_image(
            path=str(rdl_path),
            name="HeaderLogo",
            image_source="Embedded",
            value="Logo",
            top="0in",
            left="0in",
            width="1in",
            height="0.5in",
        )

        out = remove_embedded_image(path=str(rdl_path), name="Logo", force=True)
        assert out["force"] is True
        assert out["removed"] == "Logo"
        assert _embedded_block(rdl_path) is None


# ---- #81: BackgroundImage and expression references ---------------------


_BODY_STYLE = "<Height>2in</Height>\n        <Style />"
_LOGO_EXPR = '=IIf(Parameters!DateFrom.Value Is Nothing, "LogoA", "LogoA")'


def _issue_81_report(rdl_path: Path, png_path: Path) -> None:
    """The #81 repro: Watermark used only as the Body's background image,
    LogoA only through a string literal in an Image's =expression."""
    from pbirb_mcp.ops.body import add_body_image

    for name in ("Watermark", "LogoA"):
        add_embedded_image(str(rdl_path), name, "image/png", str(png_path))
    text = rdl_path.read_text(encoding="utf-8")
    assert _BODY_STYLE in text
    rdl_path.write_text(
        text.replace(
            _BODY_STYLE,
            "<Height>2in</Height>\n        <Style><BackgroundImage><Source>Embedded</Source>"
            "<Value>Watermark</Value></BackgroundImage></Style>",
        ),
        encoding="utf-8",
    )
    add_body_image(str(rdl_path), "Logo", "Embedded", _LOGO_EXPR, "1.2in", "0.5in", "1in", "0.5in")


class TestRemoveEmbeddedImageReferences:
    def test_body_background_image_blocks_removal(self, rdl_path, png_path):
        _issue_81_report(rdl_path, png_path)
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=r"Body/Style/BackgroundImage") as exc:
            remove_embedded_image(str(rdl_path), "Watermark")
        assert "force=True" in str(exc.value)
        assert rdl_path.read_bytes() == before

    def test_expression_literal_blocks_removal_as_possible_reference(self, rdl_path, png_path):
        _issue_81_report(rdl_path, png_path)
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=r"Image\[Name='Logo'\] \(possible reference") as exc:
            remove_embedded_image(str(rdl_path), "LogoA")
        assert "1 location(s)" in str(exc.value)  # duplicate literal counted once
        assert rdl_path.read_bytes() == before

    def test_force_overrides_both_and_lint_reports_them(self, rdl_path, png_path):
        from pbirb_mcp.ops.lint import lint_report

        _issue_81_report(rdl_path, png_path)
        assert remove_embedded_image(str(rdl_path), "Watermark", force=True)["force"] is True
        assert remove_embedded_image(str(rdl_path), "LogoA", force=True)["removed"] == "LogoA"
        issues = lint_report(str(rdl_path), rules=["dangling-embedded-image"])["issues"]
        assert sorted((i["severity"], i["location"]) for i in issues) == [
            ("error", "Body/Style/BackgroundImage"),
            ("warning", "Image[Name='Logo']"),
        ]

    def test_background_image_on_a_textbox_is_found(self, rdl_path, png_path):
        add_embedded_image(str(rdl_path), "Wm", "image/png", str(png_path))
        doc = RDLDocument.open(rdl_path)
        tb = doc.root.find(f".//{q('Textbox')}[@Name='ProductID']")
        style = find_child(tb, "Style")
        if style is None:
            style = etree.SubElement(tb, q("Style"))
        bg = etree.SubElement(style, q("BackgroundImage"))
        etree.SubElement(bg, q("Source")).text = "Embedded"
        etree.SubElement(bg, q("Value")).text = "Wm"
        doc.save()
        with pytest.raises(ValueError, match=r"Textbox\[Name='ProductID'\]/Style/BackgroundImage"):
            remove_embedded_image(str(rdl_path), "Wm")

    def test_unrelated_expression_literals_do_not_block(self, rdl_path, png_path):
        from pbirb_mcp.ops.body import add_body_image

        add_embedded_image(str(rdl_path), "LogoA", "image/png", str(png_path))
        add_embedded_image(str(rdl_path), "LogoB", "image/png", str(png_path))
        add_body_image(
            str(rdl_path), "Pick", "Embedded", '=IIf(True, "LogoB", "LogoB")',
            "1.2in", "0.5in", "1in", "0.5in",
        )  # fmt: skip
        assert remove_embedded_image(str(rdl_path), "LogoA")["removed"] == "LogoA"

    def test_iter_embedded_image_refs(self, rdl_path, png_path):
        from pbirb_mcp.ops.embedded_images import iter_embedded_image_refs

        _issue_81_report(rdl_path, png_path)
        refs = iter_embedded_image_refs(RDLDocument.open(rdl_path).root)
        assert sorted((r.kind, r.ref, r.is_expression) for r in refs) == [
            ("BackgroundImage", "Watermark", False),
            ("Image", "LogoA", True),
        ]


# ---- v0.3 Phase 6: get_embedded_image_data ------------------------------


class TestGetEmbeddedImageData:
    def test_returns_base64_payload(self, rdl_path, tmp_path):
        png = tmp_path / "logo.png"
        png.write_bytes(_TINY_PNG)
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png),
        )
        result = get_embedded_image_data(path=str(rdl_path), name="Logo")
        assert result["name"] == "Logo"
        assert result["mime_type"] == "image/png"
        # base64 is a non-empty string.
        assert isinstance(result["base64"], str)
        assert result["base64"]
        # byte_size matches the source PNG length.
        assert result["byte_size"] == len(_TINY_PNG)

    def test_round_trip_via_decode(self, rdl_path, tmp_path):
        import base64

        png = tmp_path / "logo.png"
        png.write_bytes(_TINY_PNG)
        add_embedded_image(
            path=str(rdl_path),
            name="Logo",
            mime_type="image/png",
            image_path=str(png),
        )
        result = get_embedded_image_data(path=str(rdl_path), name="Logo")
        # Decoding the returned base64 yields the original bytes.
        assert base64.b64decode(result["base64"]) == _TINY_PNG

    def test_unknown_image_raises(self, rdl_path):
        with pytest.raises(ElementNotFoundError, match="not found"):
            get_embedded_image_data(path=str(rdl_path), name="NoSuch")

    def test_no_embedded_block_raises(self, rdl_path):
        # Fixture has no <EmbeddedImages> block at all.
        with pytest.raises(ElementNotFoundError, match="not found"):
            get_embedded_image_data(path=str(rdl_path), name="Anything")


# ---- #110: structure checks, size warning, names ------------------------


def _png(width: int = 1, height: int = 1, payload: bytes = b"") -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))
    out = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    )
    out += chunk(b"IDAT", zlib.compress(raw))
    if payload:  # an ancillary chunk to make the file big
        out += chunk(b"zTXt", payload)
    return out + chunk(b"IEND", b"")


def _gif(width: int = 2, height: int = 3) -> bytes:
    import struct

    return (
        b"GIF89a"
        + struct.pack("<HHBBB", width, height, 0, 0, 0)
        + b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00"
        + b";"
    )


def _bmp(width: int = 1, height: int = 1) -> bytes:
    import struct

    pixels = b"\x00\x00\xff\x00" * width * abs(height)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    size = 14 + len(dib) + len(pixels)
    return b"BM" + struct.pack("<IHHI", size, 0, 0, 14 + len(dib)) + dib + pixels


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


class TestImageStructure:
    def test_issue_repro_all_refused_file_unchanged(self, rdl_path, tmp_path):
        import os

        trunc = _write(tmp_path, "trunc.png", b"\x89PNG\r\n\x1a\n")
        big = _write(tmp_path, "big.png", b"\x89PNG\r\n\x1a\n" + os.urandom(200_000))
        good = _write(tmp_path, "good.png", _TINY_PNG)
        before = rdl_path.read_bytes()
        cases = [
            ("Trunc", trunc, "IHDR"),
            ("Big", big, "IHDR"),
            ("", good, "empty"),
            ("Logo With Space", good, "invalid"),
            ('Logo&<>"', good, "invalid"),
        ]
        for name, src, match in cases:
            with pytest.raises(ValueError, match=match):
                add_embedded_image(str(rdl_path), name, "image/png", str(src))
        assert rdl_path.read_bytes() == before

    def test_real_png_embeds_round_trips_and_reports_size(self, rdl_path, tmp_path):
        data = _png(3, 2)
        src = _write(tmp_path, "real.png", data)
        result = add_embedded_image(str(rdl_path), "Real", "image/png", str(src))
        assert result == {
            "name": "Real",
            "mime_type": "image/png",
            "bytes": len(data),
            "width": 3,
            "height": 2,
        }
        assert base64.b64decode(get_embedded_image_data(str(rdl_path), "Real")["base64"]) == data
        doc = RDLDocument.open(rdl_path)
        doc.validate()

    def test_eval_fixture_logo_embeds(self, rdl_path):
        logo = Path(__file__).parent.parent / "evals" / "tool_selection" / "fixtures" / "logo.png"
        result = add_embedded_image(str(rdl_path), "Logo", "image/png", str(logo))
        assert result["width"] and result["height"]
        assert base64.b64decode(get_embedded_image_data(str(rdl_path), "Logo")["base64"]) == (
            logo.read_bytes()
        )

    @pytest.mark.parametrize(
        "mime, data, size",
        [
            ("image/gif", _gif(2, 3), (2, 3)),
            ("image/bmp", _bmp(4, -5), (4, 5)),
            ("image/jpeg", _TINY_JPEG, (3, 2)),
            ("image/x-png", _png(1, 1), (1, 1)),
        ],
    )
    def test_other_formats_embed(self, rdl_path, tmp_path, mime, data, size):
        src = _write(tmp_path, "img.bin", data)
        result = add_embedded_image(str(rdl_path), "Img", mime, str(src))
        assert (result["width"], result["height"]) == size
        assert "warnings" not in result

    @pytest.mark.parametrize(
        "mime, data, match",
        [
            ("image/png", _png()[:-12], "IEND"),
            ("image/png", _png()[:30], "IHDR"),
            ("image/png", _png(0, 0), "0x0"),
            ("image/jpeg", _TINY_JPEG[:-2], "FFD9"),
            ("image/jpeg", b"\xff\xd8\xff", "FFD9"),
            ("image/gif", _gif()[:-1], "trailer"),
            ("image/gif", b"GIF89a\x01\x00", "screen descriptor"),
            ("image/bmp", _bmp(4, 4)[:-8], "truncated"),
            ("image/bmp", b"BM" + b"\x00" * 10, "truncated"),
        ],
    )
    def test_broken_images_refused(self, rdl_path, tmp_path, mime, data, match):
        src = _write(tmp_path, "broken.bin", data)
        before = rdl_path.read_bytes()
        with pytest.raises(ValueError, match=match):
            add_embedded_image(str(rdl_path), "Broken", mime, str(src))
        assert rdl_path.read_bytes() == before

    def test_large_image_warns(self, rdl_path, tmp_path):
        import os

        data = _png(1, 1, payload=b"k\x00\x00" + os.urandom(1_100_000))
        src = _write(tmp_path, "large.png", data)
        result = add_embedded_image(str(rdl_path), "Large", "image/png", str(src))
        assert len(result["warnings"]) == 1 and "base64" in result["warnings"][0]
        assert list_embedded_images(str(rdl_path))[0]["name"] == "Large"

    def test_warning_threshold_is_configurable(self, rdl_path, tmp_path):
        src = _write(tmp_path, "small.png", _TINY_PNG)
        result = add_embedded_image(str(rdl_path), "Small", "image/png", str(src), 10)
        assert "warnings" in result
        result = add_embedded_image(
            str(rdl_path), "Small2", "image/png", str(src), warn_above_bytes=10_000
        )
        assert "warnings" not in result


# ---- registration ---------------------------------------------------------


class TestToolRegistration:
    def test_three_tools_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert {
            "add_embedded_image",
            "list_embedded_images",
            "remove_embedded_image",
        } <= names

    def test_get_embedded_image_data_registered(self):
        srv = MCPServer()
        register_all_tools(srv)
        listing = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"][
            "tools"
        ]
        names = {t["name"] for t in listing}
        assert "get_embedded_image_data" in names
