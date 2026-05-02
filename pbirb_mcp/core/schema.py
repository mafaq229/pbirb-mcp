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
"""

from __future__ import annotations

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


def validate_against_xsd(tree: etree._ElementTree) -> None:
    schema = _load_xsd()
    if schema is None:
        return  # opt-in; silently skip
    if not schema.validate(tree):
        first = schema.error_log[0]
        raise RDLValidationError(f"RDL XSD validation failed at line {first.line}: {first.message}")
