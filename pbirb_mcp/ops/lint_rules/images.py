"""Image rules (#79).

* ``database-image-missing-mimetype`` (error) — an ``Image`` or
  ``Style/BackgroundImage`` with ``Source=Database`` and no (or an empty)
  ``<MIMEType>``. The XSD makes MIMEType optional, but Report Builder
  refuses to publish a database image without one: the bytes in the
  field carry no type of their own.

Dangling ``Source=Embedded`` references are ``dangling-embedded-image``
(in :mod:`pbirb_mcp.ops.lint`).
"""

from __future__ import annotations

from lxml import etree

from pbirb_mcp.core.xpath import find_child, q
from pbirb_mcp.ops.lint import Issue, _ancestor_chain

RULE = "database-image-missing-mimetype"


def _rule_database_image_missing_mimetype(doc) -> list[Issue]:
    issues: list[Issue] = []
    for img in doc.root.iter(q("Image"), q("BackgroundImage")):
        source = find_child(img, "Source")
        if source is None or (source.text or "").strip() != "Database":
            continue
        mime = find_child(img, "MIMEType")
        if mime is not None and (mime.text or "").strip():
            continue
        if etree.QName(img).localname == "BackgroundImage":
            location, what = _ancestor_chain(img), "BackgroundImage"
            fix = "add a <MIMEType> (e.g. image/png) after its <Value>"
        else:
            location = f"Image[Name={img.get('Name')!r}]" if img.get("Name") else "Image"
            what = "Image"
            fix = "set_image_source(source='Database', value=..., mime_type='image/png')"
        issues.append(
            {
                "severity": "error",
                "rule": RULE,
                "location": location,
                "message": (
                    f"{what} Source=Database has no MIMEType; Report Builder refuses to "
                    "publish a database image without one"
                ),
                "suggestion": fix,
            }
        )
    return issues


RULES = {RULE: _rule_database_image_missing_mimetype}
