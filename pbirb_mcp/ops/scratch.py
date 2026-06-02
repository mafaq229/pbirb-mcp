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

_DATASOURCE_KEYS = frozenset(
    {"name", "workspace_url", "dataset_name", "provider", "integrated_security"}
)
_VALID_PROVIDERS = ("sql", "pbidataset")


def _emit_datasource_block(parent: etree._Element, datasource: Optional[dict[str, Any]]) -> str:
    """Append a ``<DataSource>`` element to ``parent`` based on the
    optional ``datasource`` payload. Returns the ``Name`` of the
    emitted source so the DataSet can wire its ``<DataSourceName>``.

    Mirrors the v0.4 commit 1 ``add_data_source`` shape for both
    provider variants so a scratch-built source round-trips through
    ``_is_pbidataset_dataset``, ``list_data_sources``, and the rest of
    the existing tools without surprise.

    When ``datasource`` is None or has only a ``name`` field, emits a
    minimal placeholder (DataProvider=SQL, empty ConnectString) — the
    commit-13 behaviour.
    """
    payload = datasource or {}
    unknown = set(payload) - _DATASOURCE_KEYS
    if unknown:
        raise ValueError(
            f"unknown datasource key(s) {sorted(unknown)!r}; valid keys: {sorted(_DATASOURCE_KEYS)}"
        )

    provider = payload.get("provider", "sql")
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; valid values: {_VALID_PROVIDERS!r}")

    name = payload.get("name") or "DataSource1"
    workspace_url = payload.get("workspace_url")
    dataset_name = payload.get("dataset_name")
    integrated_security = payload.get("integrated_security", True)

    ds_elem = etree.SubElement(parent, q("DataSource"), Name=name)
    cp = etree.SubElement(ds_elem, q("ConnectionProperties"))

    # Decide whether to emit a real connection (when workspace_url +
    # dataset_name are present) or the placeholder shape. Either path
    # produces an XSD-valid file.
    has_real_connection = workspace_url and dataset_name

    if provider == "pbidataset":
        etree.SubElement(cp, q("DataProvider")).text = "PBIDATASET"
        if has_real_connection:
            cs = "Data Source=pbiazure://api.powerbi.com/;Initial Catalog=" + dataset_name
            if integrated_security:
                cs += ";Integrated Security=ClaimsToken"
            etree.SubElement(cp, q("ConnectString")).text = cs
        else:
            etree.SubElement(cp, q("ConnectString"))
        etree.SubElement(ds_elem, qrd("DataSourceID")).text = str(uuid.uuid4())
        if has_real_connection:
            etree.SubElement(ds_elem, qrd("PowerBIWorkspaceName")).text = workspace_url
            etree.SubElement(ds_elem, qrd("PowerBIDatasetName")).text = dataset_name
    else:  # "sql"
        etree.SubElement(cp, q("DataProvider")).text = "SQL"
        if has_real_connection:
            etree.SubElement(cp, q("ConnectString")).text = (
                f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{workspace_url}"
                f";Initial Catalog={dataset_name}"
            )
            if integrated_security:
                etree.SubElement(cp, q("IntegratedSecurity")).text = "true"
            etree.SubElement(ds_elem, qrd("SecurityType")).text = (
                "Integrated" if integrated_security else "None"
            )
        else:
            etree.SubElement(cp, q("ConnectString"))
        etree.SubElement(ds_elem, qrd("DataSourceID")).text = str(uuid.uuid4())

    return name


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

    # DataSources / DataSets — structural validator requires both
    # blocks. _emit_datasource_block produces a placeholder when
    # `datasource` is None/sparse, or a real PBI XMLA connection
    # when {workspace_url, dataset_name} are present.
    ds_root = etree.SubElement(root, q("DataSources"))
    ds_name = _emit_datasource_block(ds_root, datasource)

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
