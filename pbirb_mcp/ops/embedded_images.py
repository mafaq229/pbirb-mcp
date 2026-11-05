"""Embedded-image authoring.

``<EmbeddedImages>`` lives at the top level of ``<Report>`` and holds
zero or more ``<EmbeddedImage Name="...">`` entries with ``<MIMEType>``
and ``<ImageData>`` (base64-encoded bytes). Once an image is embedded,
``<Image Source="Embedded"><Value>...</Value></Image>`` references it
by ``Name`` from anywhere in the report.

Insertion respects the RDL XSD top-level child order: EmbeddedImages
sits between ReportParameters and ReportSections in our fixture's
arrangement; the helper finds the right anchor.

MIME types are constrained to the RDL-supported set
(``image/bmp``, ``image/gif``, ``image/jpeg``, ``image/png``,
``image/x-png``). Unknown MIME types are rejected up front rather than
letting Report Builder load a file it can't render.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.names import validate_identifier
from pbirb_mcp.core.xpath import find_child, find_children, q
from pbirb_mcp.ops import expr_walk, vbexpr

# Per the RDL spec, embedded images only support these MIME types.
_VALID_MIME_TYPES = frozenset(
    {
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/x-png",
    }
)
# Public name: Database images (``Image/MIMEType``) use the same set (#79).
VALID_MIME_TYPES = _VALID_MIME_TYPES


# Magic-byte prefixes for the supported formats. We sniff the file to refuse
# obvious mime/format mismatches early — without this, callers can embed PNG
# bytes as image/jpeg and only see the breakage when Report Builder fails
# to render at preview time.
_FORMAT_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/bmp": (b"BM",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/x-png": (b"\x89PNG\r\n\x1a\n",),
}


def _sniff_mime(prefix: bytes) -> Optional[str]:
    """Return the canonical MIME type for the given file prefix, or None
    if the bytes don't match any supported format."""
    for mime, signatures in _FORMAT_MAGIC.items():
        for sig in signatures:
            if prefix.startswith(sig):
                # x-png is an alias for png; collapse for comparison purposes.
                return "image/png" if mime == "image/x-png" else mime
    return None


# Top-level <Report> child order we care about for placement. The fixture
# follows: AutoRefresh, DataSources, DataSets, ReportParameters,
# ReportSections, then rd:* metadata. EmbeddedImages slots between
# ReportParameters and ReportSections.
_REPORT_CHILD_ORDER_AFTER_EMBEDDED = (
    "ReportSections",
    "Variables",
    "Page",
    "Language",
)
_REPORT_CHILD_ORDER_BEFORE_EMBEDDED = (
    "ReportParameters",
    "DataSets",
    "DataSources",
    "AutoRefresh",
)


def _ensure_embedded_block(report: etree._Element) -> etree._Element:
    block = find_child(report, "EmbeddedImages")
    if block is not None:
        return block
    block = etree.Element(q("EmbeddedImages"))
    # Prefer to insert before ReportSections (or one of its peers).
    for local in _REPORT_CHILD_ORDER_AFTER_EMBEDDED:
        anchor = find_child(report, local)
        if anchor is not None:
            anchor.addprevious(block)
            return block
    # Otherwise, after ReportParameters / DataSets / etc.
    for local in _REPORT_CHILD_ORDER_BEFORE_EMBEDDED:
        anchor = find_child(report, local)
        if anchor is not None:
            anchor.addnext(block)
            return block
    # Last resort: append. Shouldn't happen for any well-formed PBI report.
    report.append(block)
    return block


def _find_embedded(block: etree._Element, name: str) -> etree._Element | None:
    for entry in find_children(block, "EmbeddedImage"):
        if entry.get("Name") == name:
            return entry
    return None


# ---- add_embedded_image ---------------------------------------------------


def add_embedded_image(
    path: str,
    name: str,
    mime_type: str,
    image_path: str,
) -> dict[str, Any]:
    # Report Builder names embedded images with the same identifier rule
    # as every other RDL name (its import dialog strips "logo 2.png" to
    # "logo2"); the XSD only says normalizedString.
    validate_identifier(name, "embedded image")
    if mime_type not in _VALID_MIME_TYPES:
        raise ValueError(f"mime_type must be one of {sorted(_VALID_MIME_TYPES)}; got {mime_type!r}")

    src = Path(image_path)
    if not src.is_file():
        raise FileNotFoundError(image_path)

    image_bytes = src.read_bytes()

    # Sniff the magic bytes and refuse mime/format mismatches early. Report
    # Builder otherwise embeds the bad bytes happily and only fails at
    # preview time, far from the call that introduced the bug.
    detected = _sniff_mime(image_bytes[:8])
    canonical_claim = "image/png" if mime_type == "image/x-png" else mime_type
    if detected is None:
        raise ValueError(
            f"file {image_path!r} does not look like a supported image "
            f"(magic bytes: {image_bytes[:8]!r})."
        )
    if detected != canonical_claim:
        raise ValueError(
            f"mime_type {mime_type!r} does not match the file content "
            f"(detected {detected!r} from magic bytes). Pass the matching "
            "mime_type or supply the right file."
        )

    encoded = base64.b64encode(image_bytes).decode("ascii")

    doc = RDLDocument.open(path)
    block = _ensure_embedded_block(doc.root)
    if _find_embedded(block, name) is not None:
        raise ValueError(f"embedded image named {name!r} already exists")

    entry = etree.SubElement(block, q("EmbeddedImage"), Name=name)
    etree.SubElement(entry, q("MIMEType")).text = mime_type
    etree.SubElement(entry, q("ImageData")).text = encoded

    doc.save()
    return {
        "name": name,
        "mime_type": mime_type,
        "bytes": src.stat().st_size,
    }


# ---- list_embedded_images -------------------------------------------------


def list_embedded_images(path: str) -> list[dict[str, str]]:
    doc = RDLDocument.open(path)
    block = find_child(doc.root, "EmbeddedImages")
    if block is None:
        return []
    return [
        {
            "name": e.get("Name"),
            "mime_type": (find_child(e, "MIMEType").text or "")
            if find_child(e, "MIMEType") is not None
            else "",
        }
        for e in find_children(block, "EmbeddedImage")
    ]


# ---- remove_embedded_image ------------------------------------------------


class EmbeddedImageRef(NamedTuple):
    """One reference to an embedded image (see :func:`iter_embedded_image_refs`)."""

    element: etree._Element  # the <Image> or <BackgroundImage>
    kind: str  # "Image" | "BackgroundImage"
    ref: str  # the embedded image name referenced
    is_expression: bool  # True: a string literal inside an =expression Value


def iter_embedded_image_refs(root: etree._Element) -> Iterator[EmbeddedImageRef]:
    """Yield every reference to an embedded image in the report (#81).

    Covers each ``<Image>`` report item and each ``Style/<BackgroundImage>``
    (Body, Rectangle, Textbox, Tablix, Chart, ... — the two share the
    ``Source`` / ``Value`` shape) whose ``Source`` is ``Embedded``:

    - a literal ``Value`` yields one ref with ``is_expression=False``;
    - an ``=`` expression ``Value`` (``=IIf(..., "LogoA", "LogoB")``)
      yields one ref per distinct double-quoted string literal in it, with
      ``is_expression=True`` — the expression *may* pick that image at
      run time. Literals are found with the VB tokenizer, so text in
      comments never counts.
    """
    for elem in root.iter(q("Image"), q("BackgroundImage")):
        source = find_child(elem, "Source")
        if source is None or (source.text or "").strip() != "Embedded":
            continue
        value = find_child(elem, "Value")
        text = (value.text or "").strip() if value is not None else ""
        if not text:
            continue
        kind = etree.QName(elem).localname
        if not text.startswith("="):
            yield EmbeddedImageRef(elem, kind, text, False)
            continue
        seen: set[str] = set()
        for token in expr_walk.expression_tokens(text) or []:
            if token.kind != vbexpr.STRING:
                continue
            literal = vbexpr.string_value(token)
            if literal and literal not in seen:
                seen.add(literal)
                yield EmbeddedImageRef(elem, kind, literal, True)


def ref_locator(ref: EmbeddedImageRef) -> str:
    """Human-readable location of a reference: ``Image[Name='Logo']``, or
    the owner of a background image — ``Body/Style/BackgroundImage``,
    ``Textbox[Name='Amount']/Style/BackgroundImage``."""
    if ref.kind == "Image":
        return f"Image[Name={ref.element.get('Name')!r}]" if ref.element.get("Name") else "Image"
    style = ref.element.getparent()
    owner = style.getparent() if style is not None else None
    if owner is None:
        return "BackgroundImage"
    owner_local = etree.QName(owner).localname
    label = f"{owner_local}[Name={owner.get('Name')!r}]" if owner.get("Name") else owner_local
    return f"{label}/Style/BackgroundImage"


def _scan_embedded_image_references(doc: RDLDocument, name: str) -> list[str]:
    """Locators of every reference to embedded image ``name``: literal
    ones as-is, expression ones marked "possible reference". Mirrors
    ``parameters._scan_parameter_references`` so callers get the same
    experience: the message lists where the references live, and they
    can fix them or pass ``force=True``."""
    locators: list[str] = []
    for ref in iter_embedded_image_refs(doc.root):
        if ref.ref != name:
            continue
        where = ref_locator(ref)
        if ref.is_expression:
            where += " (possible reference: string literal in its =expression Value)"
        locators.append(where)
    return locators


def remove_embedded_image(
    path: str,
    name: str,
    force: bool = False,
) -> dict[str, Any]:
    """Remove a named embedded image.

    By default refuses while anything still references it — an
    ``<Image>`` or a ``Style/BackgroundImage`` (Body, Rectangle, Textbox,
    Tablix, Chart, ...) with ``Source=Embedded`` whose ``Value`` is the
    name, or whose ``=`` expression ``Value`` contains it as a string
    literal (reported as a "possible reference"); the report would render
    with a missing image there. The file is untouched. Pass ``force=True``
    to remove anyway and accept the dangling references; this matches
    the safety story in ``remove_parameter``.

    Drops the empty ``<EmbeddedImages/>`` block when removing the last entry.
    """
    doc = RDLDocument.open(path)
    block = find_child(doc.root, "EmbeddedImages")
    target = _find_embedded(block, name) if block is not None else None
    if target is None:
        raise ElementNotFoundError(f"embedded image named {name!r} not found")

    if not force:
        locators = _scan_embedded_image_references(doc, name)
        if locators:
            raise ValueError(
                f"embedded image {name!r} is still referenced from "
                f"{len(locators)} location(s): {locators[:5]}"
                + (" (more elided)" if len(locators) > 5 else "")
                + ". Pass force=True to remove anyway."
            )

    block.remove(target)
    if len(find_children(block, "EmbeddedImage")) == 0:
        block.getparent().remove(block)
    doc.save()
    return {"removed": name, "force": force}


# ---- get_embedded_image_data (Phase 6 commit 29) ------------------------


def get_embedded_image_data(path: str, name: str) -> dict[str, Any]:
    """Read an embedded image's base64 ``<ImageData>`` for porting it to
    another report without re-reading from disk.

    Returns ``{name, mime_type, base64, byte_size}`` — ``base64`` is the
    raw text of ``<EmbeddedImages>/<EmbeddedImage Name=name>/
    <ImageData>`` (a base64-encoded string); ``byte_size`` is the
    decoded byte length so callers can sanity-check before passing the
    payload around.

    The companion :func:`add_embedded_image` reads bytes from a path
    and base64-encodes them. The intended round-trip is:
    ``add_embedded_image(src) → … → get_embedded_image_data(name)`` then
    feed the returned base64 + mime_type to a manual
    ``<EmbeddedImage>`` write in another report (Phase 6 doesn't yet
    ship a paired ``add_embedded_image_from_data`` writer; the v0.1
    ``add_embedded_image`` requires a file path).

    Refuses with ``ElementNotFoundError`` if the named entry isn't in
    ``<EmbeddedImages>``.
    """
    doc = RDLDocument.open(path)
    block = find_child(doc.root, "EmbeddedImages")
    target = _find_embedded(block, name) if block is not None else None
    if target is None:
        raise ElementNotFoundError(f"embedded image named {name!r} not found")

    mime_node = find_child(target, "MIMEType")
    data_node = find_child(target, "ImageData")
    mime_type = mime_node.text if mime_node is not None else None
    b64 = data_node.text if data_node is not None else ""
    if b64 is None:
        b64 = ""
    # Compute decoded byte size — base64 strings have whitespace in some
    # exports; ignore whitespace via the validate flag's relaxed mode.
    try:
        byte_size = len(base64.b64decode(b64, validate=False))
    except Exception:
        byte_size = 0
    return {
        "name": name,
        "mime_type": mime_type,
        "base64": b64,
        "byte_size": byte_size,
    }


__all__ = [
    "VALID_MIME_TYPES",
    "EmbeddedImageRef",
    "iter_embedded_image_refs",
    "ref_locator",
    "add_embedded_image",
    "get_embedded_image_data",
    "list_embedded_images",
    "remove_embedded_image",
]
