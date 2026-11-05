"""Datasource-mutation tools.

Covers the full DataSource CRUD surface:

- ``set_datasource_connection``: repoint an existing ``<DataSource>`` at
  another Power BI workspace / semantic model, in the source's own
  provider shape (or convert it with ``provider=``).
- ``add_data_source``: create a new ``<DataSource>``.
- ``remove_data_source``: drop a ``<DataSource>``; refuses if any
  ``<DataSet>/<Query>/<DataSourceName>`` references it.
- ``rename_data_source``: rewrites both ``<DataSourceName>`` (in every
  DataSet's Query) and ``<DataSourceReference>`` (in any shared-source
  links) atomically.

The two connection shapes (``pbidataset`` — what Power BI Report Builder
writes — and the legacy ``sql`` XMLA shape) and connect-string escaping
live in :mod:`pbirb_mcp.core.connection`, shared with ``create_report``.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import connection as conn_shape
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.names import validate_identifier
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, XPATH_NS, find_child, find_children, q, qrd


def _resolve_data_source(doc: RDLDocument, name: str) -> etree._Element:
    matches = list(
        doc.root.xpath(
            ".//r:DataSources/r:DataSource[@Name=$n]",
            namespaces=XPATH_NS,
            n=name,
        )
    )
    if not matches:
        raise ElementNotFoundError(f"DataSource {name!r} not found")
    if len(matches) > 1:
        raise ElementNotFoundError(f"DataSource {name!r} is ambiguous: {len(matches)} matches")
    return matches[0]


def _check_provider(provider: Optional[str]) -> None:
    if provider is not None and provider not in conn_shape.PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; valid values: {conn_shape.PROVIDERS!r}")


def _connection_texts(ds: etree._Element) -> tuple[str, Optional[str]]:
    """``(DataProvider text, ConnectString text)`` of an existing source."""
    cp = find_child(ds, "ConnectionProperties")
    dp = find_child(cp, "DataProvider") if cp is not None else None
    cs = find_child(cp, "ConnectString") if cp is not None else None
    wire = (dp.text or "").strip() if dp is not None else ""
    return wire, (cs.text if cs is not None else None)


def set_datasource_connection(
    path: str,
    name: str,
    workspace_url: str,
    dataset_name: str,
    integrated_security: bool = True,
    provider: Optional[str] = None,
    catalog_id: Optional[str] = None,
    identity_provider: Optional[str] = None,
) -> dict[str, Any]:
    """Repoint ``<DataSource Name=name>`` at ``workspace_url`` / ``dataset_name``.

    ``provider=None`` keeps the source's provider and rewrites it in that
    provider's shape (see :mod:`pbirb_mcp.core.connection`): a
    ``PBIDATASET`` source stays ``PBIDATASET`` with its ``pbiazure://``
    string, its other connect-string keys (``Identity Provider`` …) and
    updated ``rd:PowerBIWorkspaceName`` / ``rd:PowerBIDatasetName``
    labels; a ``SQL`` source keeps the ``powerbi://`` XMLA shape. A source
    with no ``<DataProvider>`` yet gets the ``sql`` shape; any other
    provider is refused unless ``provider`` says what to convert it to.

    PBIDATASET ``Initial Catalog`` is the model's internal id:
    ``catalog_id`` sets it. Without it the current id is kept when the
    workspace and model labels are unchanged; otherwise the display name
    is written and a warning returned.

    Returns ``{name, data_provider, connect_string, integrated_security,
    changed, before, after, warnings}``.
    """
    _check_provider(provider)
    doc = RDLDocument.open(path)
    data_source = _resolve_data_source(doc, name)
    before = _data_source_to_dict(data_source)

    wire, existing_cs = _connection_texts(data_source)
    current = conn_shape.provider_of(wire)
    if provider is None:
        if current is None and wire:
            raise ValueError(
                f"DataSource {name!r} uses DataProvider {wire!r}, which this tool doesn't "
                "write; pass provider='pbidataset' or provider='sql' to convert it."
            )
        target = current or conn_shape.SQL
    else:
        target = provider
    if current != target:
        existing_cs = None  # the other provider's keys don't carry over

    catalog = catalog_id
    if target == conn_shape.PBIDATASET and not catalog and current == target:
        current_catalog = conn_shape.get_value(
            conn_shape.parse_connect_string(existing_cs), "Initial Catalog"
        )
        same_model = conn_shape.rd_text(data_source, "PowerBIDatasetName") == dataset_name
        same_workspace = conn_shape.rd_text(
            data_source, "PowerBIWorkspaceName"
        ) == conn_shape.workspace_label(workspace_url)
        if current_catalog and same_model and same_workspace:
            catalog = current_catalog  # same model: keep its id

    shape = conn_shape.build_connection(
        target,
        workspace_url,
        dataset_name,
        catalog_id=catalog,
        identity_provider=identity_provider,
        integrated=integrated_security,
        existing=existing_cs,
    )
    changed = conn_shape.apply_connection(data_source, shape)
    if changed:
        doc.save()
    return {
        "name": name,
        "data_provider": shape.data_provider,
        "connect_string": shape.connect_string,
        "integrated_security": integrated_security,
        "changed": changed,
        "before": before,
        "after": _data_source_to_dict(data_source),
        "warnings": list(shape.warnings),
    }


# ---- list_data_sources / get_data_source --------------------------------


def _data_source_to_dict(ds: etree._Element) -> dict[str, Any]:
    """Read-back shape for a ``<DataSource>``: name, data_provider,
    connect_string, integrated_security, plus the rd: metadata
    (security_type, data_source_id, powerbi_workspace_name,
    powerbi_dataset_name) and any DataSourceReference (shared source) link
    that supersedes ConnectionProperties.
    """
    cp = find_child(ds, "ConnectionProperties")
    data_provider: Optional[str] = None
    connect_string: Optional[str] = None
    integrated_security: Optional[bool] = None
    if cp is not None:
        data_provider_node = find_child(cp, "DataProvider")
        connect_string_node = find_child(cp, "ConnectString")
        is_node = find_child(cp, "IntegratedSecurity")
        if data_provider_node is not None:
            data_provider = data_provider_node.text
        if connect_string_node is not None:
            connect_string = connect_string_node.text
        if is_node is not None:
            integrated_security = (is_node.text or "").lower() == "true"

    ref_node = find_child(ds, "DataSourceReference")
    shared_reference = ref_node.text if ref_node is not None else None

    def rd(local: str) -> Optional[str]:
        node = ds.find(f"{{{RD_NS}}}{local}")
        return node.text if node is not None else None

    return {
        "name": ds.get("Name"),
        "data_provider": data_provider,
        "connect_string": connect_string,
        "integrated_security": integrated_security,
        "shared_reference": shared_reference,
        "security_type": rd("SecurityType"),
        "data_source_id": rd("DataSourceID"),
        "powerbi_workspace_name": rd("PowerBIWorkspaceName"),
        "powerbi_dataset_name": rd("PowerBIDatasetName"),
    }


def list_data_sources(path: str) -> list[dict[str, Any]]:
    """Return a rich list of every ``<DataSource>`` in the report.

    ``describe_report.data_sources`` returns names only; this tool
    returns the full shape (data_provider, connect_string,
    integrated_security, shared_reference, security_type,
    data_source_id, Power BI workspace / model labels) so an LLM can plan
    repointing / repairing without a second round-trip.
    """
    doc = RDLDocument.open(path)
    return [_data_source_to_dict(ds) for ds in doc.root.iter(f"{{{RDL_NS}}}DataSource")]


def get_data_source(path: str, name: str) -> dict[str, Any]:
    """Single-DataSource read-back (parity with ``get_textbox`` /
    ``get_image`` / ``get_rectangle`` / ``get_chart``)."""
    doc = RDLDocument.open(path)
    ds = _resolve_data_source(doc, name)
    return _data_source_to_dict(ds)


# ---- add_data_source ----------------------------------------------------


def add_data_source(
    path: str,
    name: str,
    workspace_url: str,
    dataset_name: str,
    integrated_security: bool = True,
    provider: str = "sql",
    catalog_id: Optional[str] = None,
    identity_provider: Optional[str] = None,
) -> dict[str, Any]:
    """Create a new ``<DataSource>`` for a Power BI semantic model.

    ``provider`` controls the shape (:mod:`pbirb_mcp.core.connection`):

    * ``"sql"`` (default, back-compat): ``<DataProvider>SQL</DataProvider>``
      + ``Data Source=powerbi://…/myorg/<workspace>;Initial Catalog=<dataset>``
      + ``<IntegratedSecurity>`` + ``<rd:SecurityType>Integrated</rd:SecurityType>``.
    * ``"pbidataset"`` — what Power BI Report Builder writes:
      ``<DataProvider>PBIDATASET</DataProvider>`` + ``Data Source=
      pbiazure://api.powerbi.com/;[Identity Provider=…;]Initial
      Catalog=<catalog>;Integrated Security=ClaimsToken``,
      ``<rd:SecurityType>None</rd:SecurityType>`` and the
      ``rd:PowerBIWorkspaceName`` (bare workspace name — a ``powerbi://``
      URL prefix is stripped) / ``rd:PowerBIDatasetName`` labels.
      ``catalog_id`` is the model's internal id for ``Initial Catalog``;
      without it the display name is used and a warning returned.

    Connect-string values are quoted when they contain ``;``, ``=`` or a
    quote, so a name can't inject keys. Both shapes are recognised by
    ``_is_pbidataset_dataset`` (the ``@``-prefix defence). Generates a
    fresh ``<rd:DataSourceID>`` GUID. Refuses with ValueError if a
    DataSource of the same name already exists (DataSource names are
    report-wide unique) or if ``provider`` is unknown.
    """
    validate_identifier(name, "data source")
    _check_provider(provider)
    shape = conn_shape.build_connection(
        provider,
        workspace_url,
        dataset_name,
        catalog_id=catalog_id,
        identity_provider=identity_provider,
        integrated=integrated_security,
    )

    doc = RDLDocument.open(path)
    root = doc.root

    # Find or create the <DataSources> container.
    ds_root = find_child(root, "DataSources")
    if ds_root is None:
        ds_root = etree.Element(q("DataSources"))
        # Per RDL XSD, DataSources is the FIRST child of Report (after
        # AutoRefresh if present). Insert before DataSets / ReportParameters
        # / ReportSections / etc.
        for follower_local in (
            "DataSets",
            "ReportParameters",
            "ReportSections",
            "EmbeddedImages",
        ):
            anchor = find_child(root, follower_local)
            if anchor is not None:
                anchor.addprevious(ds_root)
                break
        else:
            root.append(ds_root)

    existing = [d.get("Name") for d in find_children(ds_root, "DataSource")]
    if name in existing:
        raise ValueError(f"DataSource named {name!r} already exists")

    new_ds = etree.SubElement(ds_root, q("DataSource"), Name=name)
    etree.SubElement(new_ds, qrd("DataSourceID")).text = str(uuid.uuid4())
    conn_shape.apply_connection(new_ds, shape)

    doc.save()
    return _data_source_to_dict(new_ds) | {"kind": "DataSource", "warnings": list(shape.warnings)}


# ---- remove_data_source -------------------------------------------------


def _scan_data_source_references(doc: RDLDocument, name: str) -> list[str]:
    """Walk every ``<DataSet>/<Query>/<DataSourceName>`` AND every
    ``<DataSource>/<DataSourceReference>`` looking for ``name``. Returns
    a list of human-readable locator strings — same pattern as
    ``parameters._scan_parameter_references`` for consistent UX.
    """
    locators: list[str] = []
    # DataSet / Query / DataSourceName references.
    for dataset in doc.root.iter(f"{{{RDL_NS}}}DataSet"):
        query = find_child(dataset, "Query")
        if query is None:
            continue
        ref = find_child(query, "DataSourceName")
        if ref is not None and ref.text == name:
            locators.append(f"DataSet[Name={dataset.get('Name')!r}]/Query/DataSourceName")
    # Shared DataSource reference: <DataSource>/<DataSourceReference>.
    for ds in doc.root.iter(f"{{{RDL_NS}}}DataSource"):
        ref = find_child(ds, "DataSourceReference")
        if ref is not None and ref.text == name:
            locators.append(f"DataSource[Name={ds.get('Name')!r}]/DataSourceReference")
    return locators


def remove_data_source(
    path: str,
    name: str,
    force: bool = False,
) -> dict[str, Any]:
    """Remove a named ``<DataSource>``.

    Refuses by default if any ``<DataSet>/<Query>/<DataSourceName>`` or
    ``<DataSource>/<DataSourceReference>`` still references it; the
    error message lists the offending locators (mirrors the
    ``remove_parameter`` and ``remove_embedded_image`` safety pattern).
    Pass ``force=True`` to remove anyway and accept the dangling refs.
    """
    doc = RDLDocument.open(path)
    target = _resolve_data_source(doc, name)

    if not force:
        locators = _scan_data_source_references(doc, name)
        if locators:
            raise ValueError(
                f"DataSource {name!r} is still referenced from "
                f"{len(locators)} location(s): {locators[:5]}"
                + (" (more elided)" if len(locators) > 5 else "")
                + ". Pass force=True to remove anyway."
            )

    parent = target.getparent()
    parent.remove(target)
    # Tidy up an empty <DataSources> block.
    if len(list(parent)) == 0:
        gp = parent.getparent()
        if gp is not None:
            gp.remove(parent)
    doc.save()
    return {"removed": name, "force": force}


# ---- rename_data_source -------------------------------------------------


def rename_data_source(
    path: str,
    old_name: str,
    new_name: str,
) -> dict[str, Any]:
    """Rename a ``<DataSource>`` and rewrite every reference to it.

    Rewrites:

    1. The ``<DataSource Name=...>`` declaration.
    2. Every ``<DataSet>/<Query>/<DataSourceName>`` matching ``old_name``.
    3. Every ``<DataSource>/<DataSourceReference>`` (shared-source link)
       matching ``old_name``.

    Refuses if ``new_name`` already exists or equals ``old_name``.
    Atomic: collects all matches first, commits the rewrite only when
    every site is staged.
    """
    if new_name == old_name:
        raise ValueError("new_name and old_name are identical; nothing to rename.")
    validate_identifier(new_name, "data source")

    doc = RDLDocument.open(path)
    root = doc.root

    existing = [d.get("Name") for d in root.iter(f"{{{RDL_NS}}}DataSource")]
    if new_name in existing:
        raise ValueError(f"DataSource named {new_name!r} already exists; cannot rename onto it.")

    target = _resolve_data_source(doc, old_name)

    # Stage rewrites: collect (element, new_text) tuples so the commit
    # is atomic on text rewrites.
    rewrites: list[tuple[etree._Element, str]] = []

    # 2. <DataSet>/<Query>/<DataSourceName>
    for dataset in root.iter(f"{{{RDL_NS}}}DataSet"):
        query = find_child(dataset, "Query")
        if query is None:
            continue
        ref = find_child(query, "DataSourceName")
        if ref is not None and ref.text == old_name:
            rewrites.append((ref, new_name))

    # 3. <DataSource>/<DataSourceReference>
    for ds in root.iter(f"{{{RDL_NS}}}DataSource"):
        ref = find_child(ds, "DataSourceReference")
        if ref is not None and ref.text == old_name:
            rewrites.append((ref, new_name))

    # Commit: rename declaration + apply staged text rewrites.
    target.set("Name", new_name)
    for el, new_text in rewrites:
        el.text = new_text

    doc.save()
    return {
        "old_name": old_name,
        "new_name": new_name,
        "references_rewritten": len(rewrites),
    }


__all__ = [
    "add_data_source",
    "get_data_source",
    "list_data_sources",
    "remove_data_source",
    "rename_data_source",
    "set_datasource_connection",
]
