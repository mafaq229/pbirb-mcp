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

from pbirb_mcp.core import connection as conn_shape
from pbirb_mcp.core import units
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.names import validate_identifier
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, q, qrd

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
    {
        "name",
        "workspace_url",
        "dataset_name",
        "provider",
        "integrated_security",
        "catalog_id",
        "identity_provider",
    }
)
_VALID_PROVIDERS = conn_shape.PROVIDERS

# Report Builder 2016+ marks the default-font extension as must-understand
# and writes it as the report's first child (#88).
DF_NS = (
    "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition/defaultfontfamily"
)
_DEFAULT_FONT_FAMILY = "Segoe UI"
# Report Builder's root start tag: MustUnderstand before the namespaces.
_RB_ROOT_START_TAG = (
    f'<Report MustUnderstand="df" xmlns="{RDL_NS}" xmlns:rd="{RD_NS}" xmlns:df="{DF_NS}">'
).encode()
_FIELD_KEYS = frozenset({"name", "data_field", "type_name"})


def _normalise_fields(fields: Optional[list[Any]]) -> list[tuple[str, str, Optional[str]]]:
    """``fields`` entries → ``(name, data_field, type_name)``.

    An entry is a field name (``data_field`` = the name) or
    ``{name, data_field?, type_name?}``. Names are RDL identifiers and
    unique (case-insensitively, like every RDL name)."""
    out: list[tuple[str, str, Optional[str]]] = []
    seen: set[str] = set()
    for i, entry in enumerate(fields or []):
        if isinstance(entry, str):
            name, data_field, type_name = entry, entry, None
        elif isinstance(entry, dict):
            unknown = set(entry) - _FIELD_KEYS
            if unknown:
                raise ValueError(
                    f"fields[{i}]: unknown key(s) {sorted(unknown)!r}; "
                    f"valid keys: {sorted(_FIELD_KEYS)}"
                )
            name = entry.get("name")
            if not isinstance(name, str):
                raise ValueError(f"fields[{i}]: 'name' is required and must be a string")
            data_field = entry.get("data_field") or name
            type_name = entry.get("type_name") or None
            if not isinstance(data_field, str) or (
                type_name is not None and not isinstance(type_name, str)
            ):
                raise ValueError(f"fields[{i}]: 'data_field' and 'type_name' must be strings")
        else:
            raise ValueError(
                f"fields[{i}]: expected a field name or {{name, data_field, type_name}}, "
                f"got {type(entry).__name__}"
            )
        validate_identifier(name, "field")
        if name.lower() in seen:
            raise ValueError(f"fields lists {name!r} more than once")
        seen.add(name.lower())
        out.append((name, data_field, type_name))
    return out


def _emit_datasource_block(
    parent: etree._Element,
    datasource: Optional[dict[str, Any]],
    warnings: Optional[list[str]] = None,
) -> str:
    """Append a ``<DataSource>`` element to ``parent`` based on the
    optional ``datasource`` payload. Returns the ``Name`` of the
    emitted source so the DataSet can wire its ``<DataSourceName>``.

    A real connection is built by :func:`pbirb_mcp.core.connection.build_connection`
    — the same shape ``add_data_source`` writes — and its warnings are
    appended to ``warnings``.

    When ``datasource`` is None or lacks ``workspace_url`` /
    ``dataset_name``, emits a minimal placeholder (the provider's
    DataProvider, empty ConnectString).
    """
    if warnings is None:
        warnings = []
    payload = datasource or {}
    unknown = set(payload) - _DATASOURCE_KEYS
    if unknown:
        raise ValueError(
            f"unknown datasource key(s) {sorted(unknown)!r}; valid keys: {sorted(_DATASOURCE_KEYS)}"
        )

    provider = payload.get("provider", "sql")
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; valid values: {_VALID_PROVIDERS!r}")

    name = payload.get("name")
    if name is None:
        name = "DataSource1"
    validate_identifier(name, "data source")
    workspace_url = payload.get("workspace_url")
    dataset_name = payload.get("dataset_name")
    integrated_security = payload.get("integrated_security", True)

    ds_elem = etree.SubElement(parent, q("DataSource"), Name=name)

    # A real connection (workspace_url + dataset_name present) uses the
    # same builder as add_data_source / set_datasource_connection (#71);
    # otherwise a placeholder with an empty ConnectString. Either path
    # produces an XSD-valid file.
    if workspace_url and dataset_name:
        etree.SubElement(ds_elem, qrd("DataSourceID")).text = str(uuid.uuid4())
        shape = conn_shape.build_connection(
            provider,
            workspace_url,
            dataset_name,
            catalog_id=payload.get("catalog_id"),
            identity_provider=payload.get("identity_provider"),
            integrated=integrated_security,
        )
        conn_shape.apply_connection(ds_elem, shape)
        warnings.extend(shape.warnings)
        return name

    cp = etree.SubElement(ds_elem, q("ConnectionProperties"))
    etree.SubElement(cp, q("DataProvider")).text = conn_shape.DATA_PROVIDER[provider]
    etree.SubElement(cp, q("ConnectString"))
    etree.SubElement(ds_elem, qrd("DataSourceID")).text = str(uuid.uuid4())
    return name


def _build_minimal_rdl(
    page_setup: Optional[dict[str, str]],
    datasource: Optional[dict[str, Any]],
    warnings: Optional[list[str]] = None,
    fields: Optional[list[Any]] = None,
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
            # Margins may be zero; every page / body dimension must be > 0.
            margin = k.startswith("margin_")
            cfg[k] = units.validate_size(v, field=f"page_setup.{k}", allow_zero=margin)

    field_specs = _normalise_fields(fields)

    # Report Builder's root: the df (default font family) extension,
    # marked must-understand, written as the first child (#88).
    nsmap = {None: RDL_NS, "rd": RD_NS, "df": DF_NS}
    root = etree.Element(q("Report"), nsmap=nsmap)
    root.set("MustUnderstand", "df")
    etree.SubElement(root, f"{{{DF_NS}}}DefaultFontFamily").text = _DEFAULT_FONT_FAMILY

    etree.SubElement(root, q("AutoRefresh")).text = "0"

    # DataSources / DataSets — structural validator requires both
    # blocks. _emit_datasource_block produces a placeholder when
    # `datasource` is None/sparse, or a real PBI XMLA connection
    # when {workspace_url, dataset_name} are present.
    ds_root = etree.SubElement(root, q("DataSources"))
    ds_name = _emit_datasource_block(ds_root, datasource, warnings)

    datasets_root = etree.SubElement(root, q("DataSets"))
    dataset_name = "DataSet1"
    dataset_elem = etree.SubElement(datasets_root, q("DataSet"), Name=dataset_name)
    query_elem = etree.SubElement(dataset_elem, q("Query"))
    etree.SubElement(query_elem, q("DataSourceName")).text = ds_name
    etree.SubElement(query_elem, q("CommandText"))
    if field_specs:
        # Declared up front so insert_tablix_from_template & co. can bind
        # to them straight away (#88).
        fields_elem = etree.SubElement(dataset_elem, q("Fields"))
        for name, data_field, type_name in field_specs:
            field_elem = etree.SubElement(fields_elem, q("Field"), Name=name)
            etree.SubElement(field_elem, q("DataField")).text = encode_text(data_field)
            if type_name:
                etree.SubElement(field_elem, qrd("TypeName")).text = encode_text(type_name)

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

    tree = etree.ElementTree(root)
    # One element per line, two-space indent, like Report Builder (#88).
    etree.indent(tree, space="  ")
    return tree


def create_report(
    path: str,
    page_setup: Optional[dict[str, str]] = None,
    datasource: Optional[dict[str, Any]] = None,
    fields: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """Emit a minimal valid RDL from scratch at ``path``.

    Refuses if ``path`` already exists. Returns
    ``{path, validated, size_bytes}``.

    ``page_setup`` accepts any subset of these keys (others rejected):
    ``page_height``, ``page_width``, ``margin_top``, ``margin_bottom``,
    ``margin_left``, ``margin_right``, ``body_width``, ``body_height``.
    All values are RDL size strings (e.g. ``"8.5in"``, ``"297mm"``).

    ``datasource`` takes the ``add_data_source`` contract (``name``,
    ``workspace_url``, ``dataset_name``, ``provider``,
    ``integrated_security``, ``catalog_id``, ``identity_provider``) and
    writes the identical shape; without ``workspace_url`` /
    ``dataset_name`` a placeholder source is written. Builder warnings
    (e.g. a PBIDATASET catalog taken from the display name) come back as
    ``warnings``.

    ``fields`` declares ``DataSet1``'s ``<Fields>``: each entry is a field
    name (bound to the column of that name) or ``{name, data_field,
    type_name}``. Tablix / chart tools only bind to declared fields.

    The file is written the way Report Builder writes one: indented, with
    the ``df`` namespace (``MustUnderstand="df"``) and
    ``<df:DefaultFontFamily>Segoe UI</df:DefaultFontFamily>``.
    """
    dst = Path(path)
    if dst.exists():
        raise FileExistsError(
            f"{path!r} already exists; refusing to clobber. Delete it first or pick a new path."
        )

    warnings: list[str] = []
    tree = _build_minimal_rdl(page_setup, datasource, warnings, fields)

    # Write to a temporary RDLDocument so we can reuse the canonical
    # save_as flow (atomic .tmp + os.replace, exact byte shape Report
    # Builder expects). Mock up the dataclass directly.
    doc = RDLDocument(path=dst, tree=tree, encoding="utf-8")
    doc.pin_root_start_tag(_RB_ROOT_START_TAG)
    # Validate BEFORE saving — a malformed shape never hits disk.
    doc.validate()
    doc.save_as(dst)

    out: dict[str, Any] = {
        "path": str(dst),
        "validated": True,
        "size_bytes": dst.stat().st_size,
    }
    if warnings:
        out["warnings"] = warnings
    return out


__all__ = ["create_report"]
