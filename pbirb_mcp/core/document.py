"""RDLDocument — open / save / validate with round-trip fidelity.

Power BI Report Builder is sensitive to formatting drift: re-serialised
documents that don't preserve namespace prefixes, attribute order, and
declaration style can fail to open or silently lose metadata. This module
parses with lxml configured to preserve the original tree exactly, and
serialises with the same encoding and declaration style as the source.

Key behaviours:

* **Atomic save** — write to ``<path>.tmp`` then rename. A failure mid-write
  never leaves a half-written report or scrubs the original.
* **Structural validation** — runs on demand via :meth:`RDLDocument.validate`.
  XSD validation is opt-in: drop the Microsoft RDL 2016 XSD into
  ``pbirb_mcp/schemas/reportdefinition.xsd`` and it picks up automatically.
* **Round-trip canonical equality** — for a no-op edit, the c14n form before
  and after save is identical. Tests enforce this on the bundled fixture.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Union

# Report Builder writes self-closing tags as `<Tag />` with a space before the
# slash; lxml emits `<Tag/>`. Attribute values are always quoted in XML, so a
# `/>` byte sequence only ever appears as a self-closing tag terminator —
# making this substitution safe without a real XML parse.
_SELF_CLOSING_NORMALISE = re.compile(rb"(?<=[^ /])/>")

from lxml import etree

from pbirb_mcp.core.schema import (
    RDLValidationError,
    validate_against_xsd,
    validate_structure,
)
from pbirb_mcp.core.xpath import RDL_NS, RD_NS

PathLike = Union[str, os.PathLike]


@dataclass
class RDLDocument:
    """A loaded RDL document with namespace-aware tree access."""

    path: Path
    tree: etree._ElementTree
    encoding: str

    # ---- construction --------------------------------------------------------

    @classmethod
    def open(cls, path: PathLike) -> "RDLDocument":
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        # Preserve everything: comments, CDATA, whitespace.
        parser = etree.XMLParser(
            remove_blank_text=False,
            remove_comments=False,
            strip_cdata=False,
            resolve_entities=False,
        )
        tree = etree.parse(str(path), parser)
        encoding = tree.docinfo.encoding or "utf-8"
        return cls(path=path, tree=tree, encoding=encoding)

    # ---- accessors -----------------------------------------------------------

    @property
    def root(self) -> etree._Element:
        return self.tree.getroot()

    @property
    def nsmap(self) -> dict:
        # Always expose at least the canonical RDL + rd prefixes, even if a
        # source file omits one.
        merged = {None: RDL_NS, "rd": RD_NS}
        merged.update({k: v for k, v in self.root.nsmap.items() if v})
        return merged

    # ---- mutation lifecycle --------------------------------------------------

    def validate(self, *, with_xsd: bool = True) -> None:
        validate_structure(self.tree)
        if with_xsd:
            validate_against_xsd(self.tree)

    def save(self) -> None:
        self.save_as(self.path)

    def save_as(self, path: PathLike) -> None:
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        try:
            # Serialize body without the XML declaration so we can write the
            # declaration ourselves with double quotes — Report Builder always
            # uses <?xml version="1.0" encoding="utf-8"?> and tooling that
            # diffs by string sees single quotes (lxml's default) as drift.
            body = etree.tostring(
                self.tree, xml_declaration=False, encoding=self.encoding
            )
            body = _SELF_CLOSING_NORMALISE.sub(b" />", body)
            declaration = (
                f'<?xml version="1.0" encoding="{self.encoding}"?>\n'.encode(self.encoding)
            )
            with open(tmp, "wb") as fh:
                fh.write(declaration + body)
                if not body.endswith(b"\n"):
                    fh.write(b"\n")
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise
        os.replace(tmp, path)
        self.path = path


__all__ = [
    "RDLDocument",
    "RDLValidationError",
    "RDL_NS",
    "RD_NS",
]
