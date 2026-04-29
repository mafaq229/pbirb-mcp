"""Image-mutation tools for existing ``<Image>`` ReportItems.

The body / header / footer creators in ``body.py`` and
``header_footer.py`` build ``<Image>`` elements with a default
``<Sizing>FitProportional</Sizing>`` and a fixed ``<Source>`` /
``<Value>`` pair. This module covers the post-create edits:

- :func:`set_image_sizing` — change how the image fits its frame
  (AutoSize / Fit / FitProportional / Clip).

Subsequent v0.3 commits add :func:`set_image_source` (repoint embedded
reference) and other Image edits.

The resolver finds the named ``<Image>`` anywhere in the report — body,
header, footer, or rectangle children — and refuses with a clear error
when the name doesn't resolve.
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError
from pbirb_mcp.core.xpath import XPATH_NS, find_child, q

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
        raise AmbiguousElementError(
            f"Image name {name!r} matches {len(matches)} elements"
        )
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
        raise ValueError(
            f"sizing {sizing!r} not valid; expected one of {sorted(_VALID_SIZING)}"
        )

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


__all__ = ["set_image_sizing"]
