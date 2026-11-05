"""Validation for RDL documents.

Two layers:

1. **Structural** — always runs. Confirms the root element is ``Report`` in the
   RDL 2016 namespace and that the load-bearing top-level sections (DataSources,
   DataSets, ReportSections) are present. Cheap and dependency-free.

2. **XSD** — bundled by default since v0.3.1. The Microsoft RDL 2016/01 XSD
   ships under ``pbirb_mcp/schemas/reportdefinition.xsd`` (see
   ``pbirb_mcp/schemas/NOTICE.md`` for the redistribution permission granted by
   the MS-RDL Open Specifications IP Rights Notice). :func:`validate_against_xsd`
   runs lxml's schema validator against it. A source-build that omits
   package-data will cause :func:`xsd_available` to return ``False``;
   :mod:`pbirb_mcp.ops.validate` surfaces that as a loud
   ``rule="xsd-not-bundled"`` warning rather than silently skipping.

   Foreign ``xsi:type`` attributes are ignored (#76): Report Builder's
   ``rd:DesignerState`` holds a ``QueryDefinition`` whose
   ``QuerySpecification xsi:type="MDXQuerySpecification"`` names a type from
   a schema that isn't bundled. The RDL wildcard is ``processContents="lax"``,
   but lxml still resolves ``xsi:type`` and fails an untouched report. The
   XSD runs on :func:`xsd_view` — a copy without those attributes (line
   numbers kept) — so the report itself is never changed.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Optional

from lxml import etree

from pbirb_mcp.core.xpath import RDL_NS, find_child


class RDLValidationError(ValueError):
    """Raised when a document is not a valid RDL report."""


REQUIRED_TOP_LEVEL_SECTIONS = ("DataSources", "DataSets", "ReportSections")


def validate_structure(tree: etree._ElementTree) -> None:
    root = tree.getroot()
    expected_root = f"{{{RDL_NS}}}Report"
    if root.tag != expected_root:
        raise RDLValidationError(
            f"root element must be Report in namespace {RDL_NS!r}; got {root.tag!r}"
        )
    missing = [name for name in REQUIRED_TOP_LEVEL_SECTIONS if find_child(root, name) is None]
    if missing:
        raise RDLValidationError(
            "RDL document is missing required top-level sections: " + ", ".join(missing)
        )


def _bundled_xsd_path() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / "reportdefinition.xsd"


def xsd_available() -> bool:
    return _bundled_xsd_path().is_file()


_xsd_cache: Optional[etree.XMLSchema] = None


def _load_xsd() -> Optional[etree.XMLSchema]:
    global _xsd_cache
    if _xsd_cache is not None:
        return _xsd_cache
    path = _bundled_xsd_path()
    if not path.is_file():
        return None
    _xsd_cache = etree.XMLSchema(etree.parse(str(path)))
    return _xsd_cache


XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_XSI_TYPE = f"{{{XSI_NS}}}type"
# Elements outside the RDL namespace that carry xsi:type.
_FOREIGN_XSI_TYPED = etree.XPath(
    "//*[@xsi:type and namespace-uri() != $rdl]", namespaces={"xsi": XSI_NS}
)


def xsd_view(tree: etree._ElementTree) -> etree._ElementTree:
    """The tree to run the RDL XSD against (#76).

    ``tree`` itself when no element outside the RDL namespace carries
    ``xsi:type``; otherwise a deep copy (source line numbers kept, so XSD
    errors still point into the file) with those attributes removed. Such
    elements sit under the XSD's lax ``##other`` wildcards, where the
    types they name (``MDXQuerySpecification`` in Report Builder's
    ``rd:DesignerState``) aren't known, and Report Builder ignores
    DesignerState when rendering. RDL elements are validated unchanged.
    """
    if not _FOREIGN_XSI_TYPED(tree, rdl=RDL_NS):
        return tree
    view = copy.deepcopy(tree)
    for elem in _FOREIGN_XSI_TYPED(view, rdl=RDL_NS):
        del elem.attrib[_XSI_TYPE]
    return view


def validate_against_xsd(tree: etree._ElementTree) -> None:
    schema = _load_xsd()
    if schema is None:
        return  # opt-in; silently skip
    if not schema.validate(xsd_view(tree)):
        first = schema.error_log[0]
        raise RDLValidationError(f"RDL XSD validation failed at line {first.line}: {first.message}")
