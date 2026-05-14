"""Scratch-creation tools (v0.4 commits 13 + 14).

``create_report(path, page_setup=None, datasource=None)`` emits a
minimal valid RDL from scratch. Removes the "you must already have
an RDL" precondition every other tool implicitly imposed — combined
with v0.3.0's chart/tablix/datasource CRUD, an LLM can author a
report end-to-end without a hand-prepared template.

Design:

* Refuses if ``path`` already exists (no clobbering — callers who
  want to overwrite must delete the file themselves first).
* Default ``page_setup`` is US Letter portrait, 1in margins. Mirrors
  ``tests/fixtures/pbi_paginated_minimal.rdl``.
* Default ``datasource=None`` emits a placeholder ``DataSource1`` +
  ``DataSet1`` with an empty ``ConnectString`` and empty
  ``CommandText`` so the file is structurally valid out of the box.
  Commit 14 wires real datasource bytes via the ``provider``
  contract from ``add_data_source``.
* The body is empty — no tablix, no items. Caller adds content via
  the existing body / tablix / chart tools.
* Validates against the bundled XSD (when shipped) and the
  structural validator BEFORE saving. A malformed shape never
  hits disk.
* Uses :meth:`RDLDocument.batch` for the open / mutate / save
  lifecycle. One atomic ``.tmp`` + ``os.replace``.

Returns ``{path, validated, size_bytes}``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.xpath import RDL_NS, q, qrd

_DEFAULT_PAGE_SETUP: dict[str, str] = {
    "page_height": "11in",
    "page_width": "8.5in",
    "margin_top": "1in",
    "margin_bottom": "1in",
    "margin_left": "1in",
    "margin_right": "1in",
    "body_width": "5in",
    "body_height": "2in",
}


def _build_minimal_rdl(
    page_setup: Optional[dict[str, str]],
    datasource: Optional[dict[str, Any]],
) -> etree._ElementTree:
    """Construct the in-memory minimal RDL tree.

    Separated from :func:`create_report` so commit 14 can extend the
    datasource branch without re-walking the top-level shape.
    """
    cfg = dict(_DEFAULT_PAGE_SETUP)
    if page_setup:
        for k, v in page_setup.items():
            if k not in cfg:
                raise ValueError(f"unknown page_setup key {k!r}; valid keys: {sorted(cfg)}")
            cfg[k] = v

    nsmap = {None: RDL_NS, "rd": "http://schemas.microsoft.com/SQLServer/reporting/reportdesigner"}
    root = etree.Element(q("Report"), nsmap=nsmap)

    etree.SubElement(root, q("AutoRefresh")).text = "0"

    # DataSources / DataSets — placeholders so the structural validator
    # passes. Commit 14 swaps in real connection bytes via add_data_source
    # when a `datasource` payload is provided.
    ds_root = etree.SubElement(root, q("DataSources"))
    ds_name = (datasource or {}).get("name") or "DataSource1"
    ds_elem = etree.SubElement(ds_root, q("DataSource"), Name=ds_name)
    cp = etree.SubElement(ds_elem, q("ConnectionProperties"))
    etree.SubElement(cp, q("DataProvider")).text = "SQL"
    # Empty placeholder — leave .text unset (None) so the element
    # serialises as <ConnectString /> consistently across the initial
    # create and any subsequent open+save round-trip.
    etree.SubElement(cp, q("ConnectString"))
    etree.SubElement(ds_elem, qrd("DataSourceID")).text = str(uuid.uuid4())

    datasets_root = etree.SubElement(root, q("DataSets"))
    dataset_name = "DataSet1"
    dataset_elem = etree.SubElement(datasets_root, q("DataSet"), Name=dataset_name)
    query_elem = etree.SubElement(dataset_elem, q("Query"))
    etree.SubElement(query_elem, q("DataSourceName")).text = ds_name
    etree.SubElement(query_elem, q("CommandText"))

    # ReportSections — one section with an empty body. Note: an empty
    # <ReportItems/> is XSD-INVALID (the element requires at least one
    # child). Body itself doesn't require ReportItems, so we omit the
    # element entirely when there are no items; later body-additions
    # (add_body_textbox / add_body_image / etc.) create it on demand.
    sections = etree.SubElement(root, q("ReportSections"))
    section = etree.SubElement(sections, q("ReportSection"))
    body = etree.SubElement(section, q("Body"))
    etree.SubElement(body, q("Height")).text = cfg["body_height"]
    etree.SubElement(body, q("Style"))
    etree.SubElement(section, q("Width")).text = cfg["body_width"]
    page = etree.SubElement(section, q("Page"))
    etree.SubElement(page, q("PageHeight")).text = cfg["page_height"]
    etree.SubElement(page, q("PageWidth")).text = cfg["page_width"]
    etree.SubElement(page, q("LeftMargin")).text = cfg["margin_left"]
    etree.SubElement(page, q("RightMargin")).text = cfg["margin_right"]
    etree.SubElement(page, q("TopMargin")).text = cfg["margin_top"]
    etree.SubElement(page, q("BottomMargin")).text = cfg["margin_bottom"]
    etree.SubElement(page, q("Style"))

    etree.SubElement(root, qrd("ReportUnitType")).text = "Inch"
    etree.SubElement(root, qrd("ReportID")).text = str(uuid.uuid4())

    return etree.ElementTree(root)


def create_report(
    path: str,
    page_setup: Optional[dict[str, str]] = None,
    datasource: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Emit a minimal valid RDL from scratch at ``path``.

    Refuses if ``path`` already exists. Returns
    ``{path, validated, size_bytes}``.

    ``page_setup`` accepts any subset of these keys (others rejected):
    ``page_height``, ``page_width``, ``margin_top``, ``margin_bottom``,
    ``margin_left``, ``margin_right``, ``body_width``, ``body_height``.
    All values are RDL size strings (e.g. ``"8.5in"``, ``"297mm"``).

    ``datasource`` is a forward-compat hook — commit 13 only honours
    the optional ``name`` key for the placeholder DataSource's
    ``Name`` attribute. Commit 14 wires the full ``add_data_source``
    contract (``name``, ``workspace_url``, ``dataset_name``,
    ``provider``, ``integrated_security``).
    """
    dst = Path(path)
    if dst.exists():
        raise FileExistsError(
            f"{path!r} already exists; refusing to clobber. Delete it first or pick a new path."
        )

    tree = _build_minimal_rdl(page_setup, datasource)

    # Write to a temporary RDLDocument so we can reuse the canonical
    # save_as flow (atomic .tmp + os.replace, exact byte shape Report
    # Builder expects). Mock up the dataclass directly.
    doc = RDLDocument(path=dst, tree=tree, encoding="utf-8")
    # Validate BEFORE saving — a malformed shape never hits disk.
    doc.validate()
    doc.save_as(dst)

    return {
        "path": str(dst),
        "validated": True,
        "size_bytes": dst.stat().st_size,
    }


__all__ = ["create_report"]
