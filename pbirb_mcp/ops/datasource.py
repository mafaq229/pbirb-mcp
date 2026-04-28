"""Datasource-mutation tools.

Repoint a ``<DataSource>`` at a Power BI XMLA endpoint. Power BI Report
Builder uses ``DataProvider=SQL`` for Analysis Services-backed connections
(the AS provider's wire identifier in RDL — yes, it says SQL), and the
canonical connection string is::

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

from typing import Any

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import ElementNotFoundError
from pbirb_mcp.core.xpath import XPATH_NS, find_child, q

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


__all__ = ["set_datasource_connection"]
