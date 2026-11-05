"""Power BI data-source connection shapes and connect-string escaping (#71).

One builder for every tool that writes a ``<DataSource>`` connection
(``add_data_source``, ``set_datasource_connection``, ``create_report``), so
the three can't drift apart again.

Two shapes:

* ``"pbidataset"`` — what Power BI Report Builder writes for a Power BI
  semantic model (every source in the real reports we've seen)::

      <ConnectionProperties>
        <DataProvider>PBIDATASET</DataProvider>
        <ConnectString>Data Source=pbiazure://api.powerbi.com/;Identity Provider="…";
          Initial Catalog=<model id>;Integrated Security=ClaimsToken</ConnectString>
      </ConnectionProperties>
      <rd:SecurityType>None</rd:SecurityType>
      <rd:DataSourceID>…</rd:DataSourceID>
      <rd:PowerBIWorkspaceName><workspace name></rd:PowerBIWorkspaceName>
      <rd:PowerBIDatasetName><model display name></rd:PowerBIDatasetName>

  ``Initial Catalog`` holds the model's internal id (``sobe_wowvirtualserver-<guid>``
  in Report Builder output), not its display name; the workspace lives
  only in the ``rd:PowerBIWorkspaceName`` label, as a bare name.
* ``"sql"`` — the legacy XMLA shape (``DataProvider=SQL`` with
  ``Data Source=powerbi://api.powerbi.com/v1.0/myorg/<workspace>;Initial
  Catalog=<model>``, ``<IntegratedSecurity>`` and ``rd:SecurityType``).

Connect strings are handled as ``key=value`` pairs (OLE DB syntax): a value
containing ``;``, ``=``, a quote, a comma or edge whitespace is written
double-quoted with ``"`` doubled, so a ``;`` in a name can't inject keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from lxml import etree

from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.mutation import remove_child_if_present, set_text_if_different
from pbirb_mcp.core.xpath import RD_NS, find_child, q, qrd

SQL = "sql"
PBIDATASET = "pbidataset"
PROVIDERS = (SQL, PBIDATASET)

DATA_PROVIDER = {SQL: "SQL", PBIDATASET: "PBIDATASET"}
PBIAZURE_DATA_SOURCE = "pbiazure://api.powerbi.com/"
XMLA_PREFIX = "powerbi://api.powerbi.com/v1.0/myorg/"

# Canonical key order for keys this module adds.
_KEY_ORDER = ("Data Source", "Identity Provider", "Initial Catalog", "Integrated Security")
_NEEDS_QUOTES = re.compile(r"[;=\"',]|^\s|\s$")
_XMLA_URL = re.compile(r"^powerbi://[^/]+/v[\d.]+/[^/]+/(.+?)/*$", re.IGNORECASE)


# ---- connect-string syntax --------------------------------------------------


def quote_value(value: str) -> str:
    """``value`` as it must appear after ``key=``: quoted (``"`` doubled)
    when it contains a separator, a quote or edge whitespace."""
    if value and _NEEDS_QUOTES.search(value):
        return '"' + value.replace('"', '""') + '"'
    return value


def parse_connect_string(text: Optional[str]) -> list[tuple[str, str]]:
    """``key=value`` pairs of an OLE DB-style connect string, in order.

    Values may be ``"…"`` / ``'…'`` quoted (a doubled quote is a literal
    quote); keys and unquoted values are trimmed; empty segments are
    dropped. A segment without ``=`` becomes ``(segment, "")``.
    """
    pairs: list[tuple[str, str]] = []
    if not text:
        return pairs
    i, n = 0, len(text)
    while i < n:
        eq = text.find("=", i)
        semi = text.find(";", i)
        if eq < 0 or (0 <= semi < eq):
            end = n if semi < 0 else semi
            seg = text[i:end].strip()
            if seg:
                pairs.append((seg, ""))
            i = end + 1
            continue
        key = text[i:eq].strip()
        j = eq + 1
        while j < n and text[j] in " \t":
            j += 1
        if j < n and text[j] in "\"'":
            quote = text[j]
            j += 1
            buf = []
            while j < n:
                if text[j] == quote:
                    if j + 1 < n and text[j + 1] == quote:
                        buf.append(quote)
                        j += 2
                        continue
                    j += 1
                    break
                buf.append(text[j])
                j += 1
            value = "".join(buf)
            semi = text.find(";", j)
            end = n if semi < 0 else semi
        else:
            end = n if semi < 0 else semi
            value = text[j:end].strip()
        if key:
            pairs.append((key, value))
        i = end + 1
    return pairs


def format_connect_string(pairs: list[tuple[str, str]]) -> str:
    """Serialise ``pairs`` (see :func:`parse_connect_string`)."""
    return ";".join(f"{k}={quote_value(v)}" for k, v in pairs)


def get_value(pairs: list[tuple[str, str]], key: str) -> Optional[str]:
    """The value of ``key`` (case-insensitive), or ``None``."""
    want = key.lower()
    for k, v in pairs:
        if k.lower() == want:
            return v
    return None


def merge_connect_string(existing: Optional[str], updates: list[tuple[str, Optional[str]]]) -> str:
    """Apply ``updates`` (``(key, value)``; ``None`` removes the key) to the
    ``existing`` connect string, keeping every other key and the existing
    keys' positions. New keys go in canonical order. Returns ``existing``
    verbatim when nothing changes, so a repeat call doesn't rewrite it.
    """
    pairs = parse_connect_string(existing)
    out = list(pairs)
    for key, value in updates:
        idx = next((i for i, (k, _v) in enumerate(out) if k.lower() == key.lower()), None)
        if value is None:
            if idx is not None:
                del out[idx]
            continue
        if idx is not None:
            out[idx] = (out[idx][0], value)
            continue
        rank = _KEY_ORDER.index(key) if key in _KEY_ORDER else len(_KEY_ORDER)
        pos = 0
        for i, (k, _v) in enumerate(out):
            canon = next((c for c in _KEY_ORDER if c.lower() == k.lower()), None)
            if canon is not None and _KEY_ORDER.index(canon) < rank:
                pos = i + 1
        out.insert(pos, (key, value))
    if existing is not None and out == pairs:
        return existing
    return format_connect_string(out)


# ---- workspace helpers --------------------------------------------------------


def xmla_data_source(workspace: str) -> str:
    """The legacy XMLA ``Data Source`` for a workspace name or URL."""
    workspace = workspace.strip()
    if workspace.lower().startswith("powerbi://"):
        return workspace
    return XMLA_PREFIX + workspace


def workspace_label(workspace: str) -> str:
    """The bare workspace name for ``rd:PowerBIWorkspaceName``: strips a
    ``powerbi://…/myorg/`` (or any ``powerbi://`` URL) prefix."""
    workspace = workspace.strip()
    if not workspace.lower().startswith("powerbi://"):
        return workspace
    m = _XMLA_URL.match(workspace)
    if m:
        return m.group(1)
    return workspace.rstrip("/").rsplit("/", 1)[-1]


def provider_of(data_provider: Optional[str]) -> Optional[str]:
    """``"sql"`` / ``"pbidataset"`` for a ``<DataProvider>`` text, else ``None``."""
    text = (data_provider or "").strip().upper()
    for key, wire in DATA_PROVIDER.items():
        if text == wire:
            return key
    return None


# ---- the shapes -----------------------------------------------------------------


@dataclass(frozen=True)
class Connection:
    """Everything a ``<DataSource>`` connection consists of in one shape."""

    provider: str
    data_provider: str
    connect_string: str
    integrated_element: bool  # <IntegratedSecurity>true</IntegratedSecurity>
    security_type: str  # rd:SecurityType
    workspace_label: Optional[str]  # rd:PowerBIWorkspaceName (pbidataset only)
    dataset_label: Optional[str]  # rd:PowerBIDatasetName (pbidataset only)
    warnings: tuple = field(default=())


def display_name_catalog_warning(dataset: str) -> str:
    return (
        f"Initial Catalog is set to the display name {dataset!r}. Report Builder writes the "
        "semantic model's internal id there (e.g. sobe_wowvirtualserver-<guid>); pass "
        "catalog_id with that id if the connection doesn't resolve."
    )


def build_connection(
    provider: str,
    workspace: str,
    dataset: str,
    catalog_id: Optional[str] = None,
    identity_provider: Optional[str] = None,
    integrated: bool = True,
    existing: Optional[str] = None,
) -> Connection:
    """The connection for ``provider`` pointing at ``workspace`` / ``dataset``.

    ``catalog_id`` is the ``Initial Catalog`` (PBIDATASET: the model's
    internal id; without it the display name is used, with a warning).
    ``identity_provider`` sets PBIDATASET's ``Identity Provider`` (refused
    for ``sql``).
    ``existing`` is the current connect string *of the same provider*: its
    other keys (an existing ``Identity Provider``, a sovereign-cloud
    ``pbiazure://`` host, …) are kept.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; valid values: {PROVIDERS!r}")
    warnings: list[str] = []
    catalog = catalog_id if catalog_id else dataset
    if provider == PBIDATASET:
        if not catalog_id:
            warnings.append(display_name_catalog_warning(dataset))
        current_source = get_value(parse_connect_string(existing), "Data Source") or ""
        source = (
            current_source
            if current_source.lower().startswith("pbiazure://")
            else PBIAZURE_DATA_SOURCE
        )
        updates: list[tuple[str, Optional[str]]] = [("Data Source", source)]
        if identity_provider:
            updates.append(("Identity Provider", identity_provider))
        updates.append(("Initial Catalog", catalog))
        updates.append(("Integrated Security", "ClaimsToken" if integrated else None))
        return Connection(
            provider=provider,
            data_provider=DATA_PROVIDER[provider],
            connect_string=merge_connect_string(existing, updates),
            integrated_element=False,
            security_type="None",
            workspace_label=workspace_label(workspace),
            dataset_label=dataset,
            warnings=tuple(warnings),
        )
    if identity_provider:
        raise ValueError("identity_provider applies to provider='pbidataset' only")
    updates = [("Data Source", xmla_data_source(workspace)), ("Initial Catalog", catalog)]
    return Connection(
        provider=provider,
        data_provider=DATA_PROVIDER[provider],
        connect_string=merge_connect_string(existing, updates),
        integrated_element=integrated,
        security_type="Integrated" if integrated else "None",
        workspace_label=None,
        dataset_label=None,
        warnings=tuple(warnings),
    )


def _set_rd(ds: etree._Element, local: str, value: Optional[str], before: tuple = ()) -> bool:
    """Write (or with ``None`` remove) ``rd:<local>`` under ``ds``. A new
    element goes before the first existing ``rd:`` sibling named in
    ``before``, else at the end."""
    node = ds.find(qrd(local))
    if value is None:
        if node is None:
            return False
        ds.remove(node)
        return True
    text = encode_text(value)
    if node is not None:
        if node.text == text:
            return False
        node.text = text
        return True
    node = etree.Element(qrd(local))
    node.text = text
    for name in before:
        anchor = ds.find(qrd(name))
        if anchor is not None:
            anchor.addprevious(node)
            return True
    ds.append(node)
    return True


def apply_connection(ds: etree._Element, conn: Connection) -> bool:
    """Write ``conn`` into the ``<DataSource>`` ``ds`` (keeping its
    ``rd:DataSourceID`` and anything else unrelated). Elements that belong
    only to the other shape are removed. Returns ``True`` iff the tree
    changed."""
    changed = False
    cp = find_child(ds, "ConnectionProperties")
    if cp is None:
        cp = etree.Element(q("ConnectionProperties"))
        ds.insert(0, cp)
        changed = True
    changed |= set_text_if_different(cp, "DataProvider", conn.data_provider)
    changed |= set_text_if_different(cp, "ConnectString", encode_text(conn.connect_string))
    if conn.integrated_element:
        changed |= set_text_if_different(cp, "IntegratedSecurity", "true")
    else:
        changed |= remove_child_if_present(cp, "IntegratedSecurity")
    changed |= _set_rd(ds, "SecurityType", conn.security_type, before=("DataSourceID",))
    changed |= _set_rd(ds, "PowerBIWorkspaceName", conn.workspace_label, before=())
    changed |= _set_rd(ds, "PowerBIDatasetName", conn.dataset_label, before=())
    return changed


def rd_text(ds: etree._Element, local: str) -> Optional[str]:
    node = ds.find(f"{{{RD_NS}}}{local}")
    return node.text if node is not None else None


__all__ = [
    "Connection",
    "DATA_PROVIDER",
    "PBIAZURE_DATA_SOURCE",
    "PBIDATASET",
    "PROVIDERS",
    "SQL",
    "XMLA_PREFIX",
    "apply_connection",
    "build_connection",
    "display_name_catalog_warning",
    "format_connect_string",
    "get_value",
    "merge_connect_string",
    "parse_connect_string",
    "provider_of",
    "quote_value",
    "rd_text",
    "workspace_label",
    "xmla_data_source",
]
