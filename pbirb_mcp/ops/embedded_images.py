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
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import find_child, find_children, q

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


def _scan_embedded_image_references(doc: RDLDocument, name: str) -> list[str]:
    """Walk every ``<Image>`` looking for ``Source=Embedded`` + ``Value=<name>``.

    Returns a list of human-readable locator strings (the Image's Name and,
    when nameless, an ancestor anchor). Mirrors the pattern used by
    ``parameters._scan_parameter_references`` so callers get the same
    experience: the message lists where the offending references live, and
    they can decide to fix them or pass ``force=True``.
    """
    locators: list[str] = []
    for img in doc.root.iter(q("Image")):
        source = find_child(img, "Source")
        if source is None or source.text != "Embedded":
            continue
        value = find_child(img, "Value")
        if value is None or value.text != name:
            continue
        img_name = img.get("Name") or "<unnamed>"
        locators.append(f"Image[Name={img_name!r}]")
    return locators


def remove_embedded_image(
    path: str,
    name: str,
    force: bool = False,
) -> dict[str, Any]:
    """Remove a named embedded image.

    By default refuses if any ``<Image Source="Embedded"><Value>=<name>``
    still references it — the report would render with a broken image at
    every reference site. Pass ``force=True`` to remove anyway and accept
    the dangling references; this matches the safety story in
    ``remove_parameter``.

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


__all__ = [
    "add_embedded_image",
    "list_embedded_images",
    "remove_embedded_image",
]
