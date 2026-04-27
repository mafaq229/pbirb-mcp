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
from typing import Any

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
        raise ValueError(
            f"mime_type must be one of {sorted(_VALID_MIME_TYPES)}; "
            f"got {mime_type!r}"
        )

    src = Path(image_path)
    if not src.is_file():
        raise FileNotFoundError(image_path)

    encoded = base64.b64encode(src.read_bytes()).decode("ascii")

    doc = RDLDocument.open(path)
    block = _ensure_embedded_block(doc.root)
    if _find_embedded(block, name) is not None:
        raise ValueError(
            f"embedded image named {name!r} already exists"
        )

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


def remove_embedded_image(path: str, name: str) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    block = find_child(doc.root, "EmbeddedImages")
    target = _find_embedded(block, name) if block is not None else None
    if target is None:
        raise ElementNotFoundError(
            f"embedded image named {name!r} not found"
        )
    block.remove(target)
    if len(find_children(block, "EmbeddedImage")) == 0:
        block.getparent().remove(block)
    doc.save()
    return {"removed": name}


__all__ = [
    "add_embedded_image",
    "list_embedded_images",
    "remove_embedded_image",
]
