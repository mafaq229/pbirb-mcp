"""Datasource-mutation tools.

Covers the full DataSource CRUD surface:

- ``set_datasource_connection``: repoint an existing ``<DataSource>`` at
  a Power BI XMLA endpoint. (v0.1.0)
- ``add_data_source``: create a new ``<DataSource>``. (v0.3.0)
- ``remove_data_source``: drop a ``<DataSource>``; refuses if any
  ``<DataSet>/<Query>/<DataSourceName>`` references it. (v0.3.0)
- ``rename_data_source``: rewrites both ``<DataSourceName>`` (in every
  DataSet's Query) and ``<DataSourceReference>`` (in any shared-source
  links) atomically. (v0.3.0)

Power BI Report Builder uses ``DataProvider=SQL`` for Analysis
Services-backed connections (the AS provider's wire identifier in RDL —
yes, it says SQL), and the canonical connection string is::

    Data Source=powerbi://api.powerbi.com/v1.0/myorg/<workspace>;Initial Catalog=<dataset>

Callers may pass either a bare workspace name (``MyWorkspace``) or a full
XMLA URL (``powerbi://api.powerbi.com/v1.0/myorg/MyWorkspace``). The tool
detects the latter and avoids double-prefixing.

``IntegratedSecurity`` defaults to true (the only auth mode currently
supported by PBI XMLA from Report Builder); passing ``False`` omits the
element entirely, matching Report Builder's convention for non-integrated
connections.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, XPATH_NS, find_child, find_children, q, qrd

_XMLA_PREFIX = "powerbi://api.powerbi.com/v1.0/myorg/"


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


def _xmla_data_source(workspace_url: str) -> str:
    workspace_url = workspace_url.strip()
    if workspace_url.startswith("powerbi://"):
        return workspace_url
    return _XMLA_PREFIX + workspace_url


def _set_or_create_text(parent: etree._Element, local: str, value: str) -> None:
    node = find_child(parent, local)
    if node is None:
        node = etree.SubElement(parent, q(local))
    node.text = value


def _remove_if_present(parent: etree._Element, local: str) -> None:
    node = find_child(parent, local)
    if node is not None:
        parent.remove(node)


def set_datasource_connection(
    path: str,
    name: str,
    workspace_url: str,
    dataset_name: str,
    integrated_security: bool = True,
) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    data_source = _resolve_data_source(doc, name)
    cp = find_child(data_source, "ConnectionProperties")
    if cp is None:
        # Defensive: every PBI Report Builder export has one, but if we're
        # repointing a freshly-stubbed DataSource, create the block.
        cp = etree.SubElement(data_source, q("ConnectionProperties"))

    connect_string = (
        f"Data Source={_xmla_data_source(workspace_url)};Initial Catalog={dataset_name}"
    )
    _set_or_create_text(cp, "DataProvider", "SQL")
    _set_or_create_text(cp, "ConnectString", connect_string)
    if integrated_security:
        _set_or_create_text(cp, "IntegratedSecurity", "true")
    else:
        _remove_if_present(cp, "IntegratedSecurity")

    doc.save()
    return {
        "name": name,
        "data_provider": "SQL",
        "connect_string": connect_string,
        "integrated_security": integrated_security,
    }


# ---- list_data_sources / get_data_source --------------------------------


def _data_source_to_dict(ds: etree._Element) -> dict[str, Any]:
    """Read-back shape for a ``<DataSource>``: name, data_provider,
    connect_string, integrated_security, plus the rd: metadata
    (security_type, data_source_id) and any DataSourceReference (shared
    source) link that supersedes ConnectionProperties.
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

    security_type_node = ds.find(f"{{{RD_NS}}}SecurityType")
    data_source_id_node = ds.find(f"{{{RD_NS}}}DataSourceID")

    return {
        "name": ds.get("Name"),
        "data_provider": data_provider,
        "connect_string": connect_string,
        "integrated_security": integrated_security,
        "shared_reference": shared_reference,
        "security_type": (
            security_type_node.text if security_type_node is not None else None
        ),
        "data_source_id": (
            data_source_id_node.text if data_source_id_node is not None else None
        ),
    }


def list_data_sources(path: str) -> list[dict[str, Any]]:
    """Return a rich list of every ``<DataSource>`` in the report.

    ``describe_report.data_sources`` returns names only; this tool
    returns the full shape (data_provider, connect_string,
    integrated_security, shared_reference, security_type,
    data_source_id) so an LLM can plan repointing / repairing without
    a second round-trip.
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
) -> dict[str, Any]:
    """Create a new ``<DataSource>`` for a Power BI XMLA endpoint.

    Same connection-string convention as ``set_datasource_connection``.
    Generates a fresh ``<rd:DataSourceID>`` GUID and emits
    ``<rd:SecurityType>Integrated</rd:SecurityType>`` to match Report
    Builder's emitted shape.

    Refuses with ValueError if a DataSource of the same name already
    exists (per RDL semantics: DataSource names are report-wide unique).
    """
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
    cp = etree.SubElement(new_ds, q("ConnectionProperties"))
    etree.SubElement(cp, q("DataProvider")).text = "SQL"
    etree.SubElement(cp, q("ConnectString")).text = encode_text(
        f"Data Source={_xmla_data_source(workspace_url)};Initial Catalog={dataset_name}"
    )
    if integrated_security:
        etree.SubElement(cp, q("IntegratedSecurity")).text = "true"
    etree.SubElement(new_ds, qrd("SecurityType")).text = (
        "Integrated" if integrated_security else "None"
    )
    etree.SubElement(new_ds, qrd("DataSourceID")).text = str(uuid.uuid4())

    doc.save()
    return _data_source_to_dict(new_ds) | {"kind": "DataSource"}


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
            locators.append(
                f"DataSet[Name={dataset.get('Name')!r}]/Query/DataSourceName"
            )
    # Shared DataSource reference: <DataSource>/<DataSourceReference>.
    for ds in doc.root.iter(f"{{{RDL_NS}}}DataSource"):
        ref = find_child(ds, "DataSourceReference")
        if ref is not None and ref.text == name:
            locators.append(
                f"DataSource[Name={ds.get('Name')!r}]/DataSourceReference"
            )
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

    doc = RDLDocument.open(path)
    root = doc.root

    existing = [d.get("Name") for d in root.iter(f"{{{RDL_NS}}}DataSource")]
    if new_name in existing:
        raise ValueError(
            f"DataSource named {new_name!r} already exists; cannot rename onto it."
        )

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
