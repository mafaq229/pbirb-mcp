"""Image-mutation tools for existing ``<Image>`` ReportItems.

The body / header / footer creators in ``body.py`` and
``header_footer.py`` build ``<Image>`` elements with a default
``<Sizing>FitProportional</Sizing>`` and a fixed ``<Source>`` /
``<Value>`` pair. This module covers the post-create edits:

- :func:`set_image_sizing` — change how the image fits its frame
  (AutoSize / Fit / FitProportional / Clip).
- :func:`set_image_source` — repoint an Image at a different source
  (Embedded / External / Database, with Database's required MIMEType)
  without delete-and-readd.
- :func:`check_image_source` — the Source / Value / MIMEType rules the
  creators (``add_body_image``, ``add_header_image``,
  ``add_footer_image``) share with ``set_image_source``.

The resolver finds the named ``<Image>`` anywhere in the report — body,
header, footer, or rectangle children — and refuses with a clear error
when the name doesn't resolve.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError
from pbirb_mcp.core.mutation import remove_child_if_present, set_node_text
from pbirb_mcp.core.xpath import XPATH_NS, find_child, find_children, q
from pbirb_mcp.ops.embedded_images import VALID_MIME_TYPES

# RDL Image Sizing enum.
_VALID_SIZING = frozenset({"AutoSize", "Fit", "FitProportional", "Clip"})


def _resolve_image(doc: RDLDocument, name: str) -> etree._Element:
    """Find an ``<Image Name="...">`` anywhere in the report. Raises
    ``ElementNotFoundError`` on miss / ``AmbiguousElementError`` on
    duplicate name (image names are report-wide unique per RDL)."""
    matches = list(
        doc.root.xpath(
            ".//*[local-name()='Image' and @Name=$n]",
            namespaces=XPATH_NS,
            n=name,
        )
    )
    if not matches:
        raise ElementNotFoundError(f"no Image named {name!r}")
    if len(matches) > 1:
        raise AmbiguousElementError(f"Image name {name!r} matches {len(matches)} elements")
    return matches[0]


# ---- set_image_sizing ----------------------------------------------------


def set_image_sizing(
    path: str,
    image_name: str,
    sizing: str,
) -> dict[str, Any]:
    """Set ``<Image>/<Sizing>``.

    ``sizing`` ∈ AutoSize / Fit / FitProportional / Clip.

    - **AutoSize** — image renders at its native size; the box grows
      to fit. Wide images can break page layouts; use sparingly.
    - **Fit** — stretches the image to fill the box, ignoring aspect
      ratio. Cheap; usually fine for solid-colour or non-photographic
      images.
    - **FitProportional** — preserves aspect ratio while filling the
      box (default for tools in this package).
    - **Clip** — image renders at native size and is clipped to the
      box bounds. Useful for headers/banners with fixed crops.

    Idempotent: setting Sizing to the existing value is a no-op
    short-circuit (no save). Returns ``{name, kind, changed: bool}``.
    """
    if sizing not in _VALID_SIZING:
        raise ValueError(f"sizing {sizing!r} not valid; expected one of {sorted(_VALID_SIZING)}")

    doc = RDLDocument.open(path)
    image = _resolve_image(doc, image_name)
    existing = find_child(image, "Sizing")
    if existing is not None and existing.text == sizing:
        return {"name": image_name, "kind": "Image", "changed": False}

    if existing is not None:
        existing.text = sizing
    else:
        # Per RDL XSD, <Sizing> sits between <Value> and the layout
        # fields (<Top>/<Left>/<Height>/<Width>). Insert before the
        # first layout child if none of <Sizing> exists yet.
        new_node = etree.Element(q("Sizing"))
        new_node.text = sizing
        anchor = None
        for follower in ("Top", "Left", "Height", "Width", "Style"):
            anchor = find_child(image, follower)
            if anchor is not None:
                anchor.addprevious(new_node)
                break
        else:
            image.append(new_node)

    doc.save()
    return {"name": image_name, "kind": "Image", "changed": True}


def _embedded_image_names(doc: RDLDocument) -> list[str]:
    """Return the names of every ``<EmbeddedImage>`` declared in the
    report's ``<EmbeddedImages>`` block. Empty list if the block is
    absent."""
    block = find_child(doc.root, "EmbeddedImages")
    if block is None:
        return []
    return [
        e.get("Name") for e in find_children(block, "EmbeddedImage") if e.get("Name") is not None
    ]


# ---- image source / MIME validation (#79) --------------------------------

IMAGE_SOURCES = ("External", "Embedded", "Database")
# The MIME types RDL supports for images (same set as embedded images).
IMAGE_MIME_TYPES = tuple(sorted(VALID_MIME_TYPES))


def check_image_source(source: str, value: str, mime_type: Optional[str]) -> Optional[str]:
    """Validate an image's ``Source`` / ``Value`` / ``MIMEType`` triple
    before anything is written, and return the MIMEType to write.

    - **External**: ``value`` is a URL (or report-server path) or an
      ``=`` expression; no ``mime_type``.
    - **Embedded**: ``value`` names an embedded image (or is an ``=``
      expression choosing one); no ``mime_type``.
    - **Database**: ``value`` is an ``=`` expression returning the image
      bytes (e.g. ``=First(Fields!Photo.Value)``) and ``mime_type`` is
      required — Report Builder refuses to publish a database image
      without one. It is one of :data:`IMAGE_MIME_TYPES` or an ``=``
      expression.

    Raises ``ValueError``; returns ``None`` for External / Embedded.
    """
    if source not in IMAGE_SOURCES:
        raise ValueError(f"image_source must be one of {list(IMAGE_SOURCES)}; got {source!r}")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"value must be a non-empty string for a {source} image")
    if source != "Database":
        if mime_type is not None:
            raise ValueError(
                f"mime_type applies only to Database images; a {source} image takes its "
                "type from the file (omit mime_type)"
            )
        return None
    if not value.startswith("="):
        raise ValueError(
            "a Database image's value must be an =expression returning the image bytes, "
            f"e.g. '=First(Fields!Photo.Value)'; got {value!r}"
        )
    if mime_type is None:
        raise ValueError(
            "mime_type is required for a Database image (Report Builder refuses to "
            f"publish one without it); one of {list(IMAGE_MIME_TYPES)}"
        )
    if not isinstance(mime_type, str) or (
        not mime_type.startswith("=") and mime_type not in IMAGE_MIME_TYPES
    ):
        raise ValueError(
            f"mime_type must be one of {list(IMAGE_MIME_TYPES)} or an =expression; "
            f"got {mime_type!r}"
        )
    return mime_type


# ---- set_image_source ----------------------------------------------------


def set_image_source(
    path: str,
    image_name: str,
    source: Optional[str] = None,
    value: Optional[str] = None,
    mime_type: Optional[str] = None,
    embedded_name: Optional[str] = None,
) -> dict[str, Any]:
    """Repoint an existing ``<Image>`` — switch its ``Source`` and
    ``Value`` (and ``MIMEType``) without delete-and-readd.

    - ``source="Embedded"``: ``value`` must name an entry of the report's
      ``<EmbeddedImages>`` (or be an ``=`` expression); refused otherwise,
      since a dangling reference renders as a broken image.
    - ``source="External"``: ``value`` is a URL / report-server path or an
      ``=`` expression.
    - ``source="Database"``: ``value`` is an ``=`` expression returning
      the image bytes and ``mime_type`` is required.

    ``<MIMEType>`` is written (after ``<Value>``) for Database and
    removed for the other two sources. ``embedded_name`` is the
    deprecated pre-#79 spelling of ``source="Embedded", value=<name>``.

    Idempotent: an identical Source/Value/MIMEType triple is a no-op
    (no save). Returns ``{name, kind, source, changed: bool}``.
    """
    if embedded_name is not None:
        if source not in (None, "Embedded") or value not in (None, embedded_name):
            raise ValueError(
                "embedded_name is the deprecated form of source='Embedded', value=<name>; "
                "pass either embedded_name or source/value, not both"
            )
        source, value = "Embedded", embedded_name
    if source is not None and source not in IMAGE_SOURCES:
        raise ValueError(
            f"source must be one of {list(IMAGE_SOURCES)}; got {source!r} (to use an "
            "embedded image pass source='Embedded', value=<its name>)"
        )
    if source is None or value is None:
        raise ValueError(
            "source and value are required: source='Embedded' with an embedded image "
            "name, 'External' with a URL, or 'Database' with an =expression and mime_type"
        )
    mime_type = check_image_source(source, value, mime_type)

    doc = RDLDocument.open(path)
    image = _resolve_image(doc, image_name)

    if source == "Embedded" and not value.startswith("="):
        available = _embedded_image_names(doc)
        if value not in available:
            raise ElementNotFoundError(
                f"embedded image {value!r} not found in <EmbeddedImages>; "
                f"available: {available}. Add the image first via "
                "add_embedded_image, or use source='External' for a URL."
            )

    changed = False
    source_node = find_child(image, "Source")
    if source_node is None:
        # Source is the first child the creators write.
        source_node = etree.Element(q("Source"))
        image.insert(0, source_node)
        changed = True
    changed |= set_node_text(source_node, source)

    value_node = find_child(image, "Value")
    if value_node is None:
        value_node = etree.Element(q("Value"))
        source_node.addnext(value_node)
        changed = True
    if value_node.text != value and value_node.text != encode_text(value):
        value_node.text = encode_text(value)
        changed = True

    mime_node = find_child(image, "MIMEType")
    if mime_type is None:
        changed |= remove_child_if_present(image, "MIMEType")
    else:
        if mime_node is None:
            mime_node = etree.Element(q("MIMEType"))
            value_node.addnext(mime_node)
            changed = True
        changed |= set_node_text(mime_node, encode_text(mime_type))

    if changed:
        doc.save()
    return {"name": image_name, "kind": "Image", "source": source, "changed": changed}


__all__ = [
    "IMAGE_MIME_TYPES",
    "IMAGE_SOURCES",
    "check_image_source",
    "set_image_sizing",
    "set_image_source",
]
