"""Rename a report item or group and every reference to it (#111).

Report items (Textbox, Rectangle, Image, Line, Subreport, Tablix, Chart,
GaugePanel, Map, CustomReportItem) and groups share one report-wide
namespace (:mod:`pbirb_mcp.core.names`). Changing ``@Name`` alone leaves
``rd:DefaultName``, ``ReportItems!Old``, ``<ToggleItem>Old</ToggleItem>``
and ``Sum(…, "Old")`` scope strings pointing at a name that no longer
exists. :func:`rename_report_item` renames the element and rewrites every
reference :func:`pbirb_mcp.ops.expr_walk.find_inbound_references` finds
(the same helper the ``remove_*_item`` guards use, #105).

Chart sub-objects (series, chart areas, legends, titles) live in their own
per-chart namespaces and are not renamed here.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import names
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import AmbiguousElementError, ElementNotFoundError
from pbirb_mcp.core.xpath import RD_NS, find_child, find_children, q
from pbirb_mcp.ops import expr_walk

# Elements whose name doubles as a scope (aggregate scope strings, RepeatWith,
# UserSort): a data region or a group.
SCOPE_TAGS = frozenset({"Tablix", "Chart", "GaugePanel", "Map", "CustomReportItem", "Group"})

KINDS = names.SCOPE_KINDS


def _local(el: etree._Element) -> str:
    return etree.QName(el).localname


def _matches(doc: RDLDocument, name: str, kind: Optional[str]) -> list[etree._Element]:
    return [
        el
        for el in names.iter_scope_elements(doc)
        if el.get("Name") == name and (kind is None or _local(el) == kind)
    ]


def _dataset_names(doc: RDLDocument) -> set:
    block = find_child(doc.root, "DataSets")
    if block is None:
        return set()
    return {ds.get("Name") for ds in find_children(block, "DataSet") if ds.get("Name")}


def _owning_tablix(el: etree._Element) -> Optional[etree._Element]:
    for anc in el.iterancestors(q("Tablix")):
        return anc
    return None


def _list_inner(tablix: etree._Element, old_name: str) -> list[etree._Element]:
    """The tablix's own cell Rectangle(s) and groups named ``<old_name>_…``
    (what ``add_list`` generates: ``<name>_Rect`` and ``<name>_Details``);
    items of nested data regions don't count."""
    prefix = f"{old_name}_"
    out = []
    for el in tablix.iter(q("Rectangle"), q("Group")):
        if not (el.get("Name") or "").startswith(prefix) or _owning_tablix(el) is not tablix:
            continue
        if _local(el) == "Rectangle" and _local(el.getparent()) != "CellContents":
            continue
        out.append(el)
    return out


def _mirror_children(el: etree._Element) -> list[etree._Element]:
    """``rd:DefaultName`` and ``DataElementName`` directly under ``el``."""
    return [
        c
        for c in el
        if isinstance(c.tag, str)
        and (c.tag == f"{{{RD_NS}}}DefaultName" or c.tag == q("DataElementName"))
    ]


def rename_report_item(
    path: str,
    old_name: str,
    new_name: str,
    kind: Optional[str] = None,
    rename_inner: bool = False,
) -> dict[str, Any]:
    """Rename the report item or group ``old_name`` to ``new_name`` and
    rewrite every reference to it.

    ``kind`` (a report item type or ``"Group"``) restricts the lookup, for a
    report whose duplicate names make ``old_name`` ambiguous. ``new_name``
    must be a valid RDL identifier, unused by any report item or group, and —
    for a data region or group — not a dataset name (a scope string would
    then mean the dataset).

    Rewrites ``@Name``; ``rd:DefaultName`` and ``DataElementName`` on the
    element when they equal ``old_name``; and every inbound reference
    (:func:`expr_walk.find_inbound_references`): ``ReportItems!Old`` /
    ``ReportItems("Old")`` in any expression, quoted scope arguments of
    aggregates / ``RowNumber`` / ``RunningValue`` / ``Previous`` /
    ``InScope`` / ``Level``, ``<ToggleItem>``, ``<RepeatWith>``, UserSort
    ``<SortExpressionScope>`` / ``<SortTarget>``, ``<DomainScope>``. Only the
    name token changes; string literals and comments are left alone.

    ``rename_inner`` (a Tablix only — a list made by ``add_list``) also
    renames the list's ``<old>_Rect`` rectangle and ``<old>_Details`` group
    (any of the tablix's own cell rectangles / groups named ``<old>_…``) to
    ``<new>_…``.

    Returns ``{renamed, old_name, kind, references_updated: [{location,
    before, after}], changed}`` (+ ``also_renamed`` {old: new} with
    ``rename_inner``). Idempotent: when ``old_name`` is gone and ``new_name``
    exists (the rename already happened) — or the names are equal — nothing
    is written and ``changed`` is false.
    """
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}; got {kind!r}")
    label = "group" if kind == "Group" else "report item"
    names.validate_identifier(new_name, label)
    doc = RDLDocument.open(path)

    found = _matches(doc, old_name, kind)
    if not found or old_name == new_name:
        done = _matches(doc, new_name, kind)
        if len(done) == 1:
            return {
                "renamed": new_name,
                "old_name": old_name,
                "kind": _local(done[0]),
                "references_updated": [],
                "changed": False,
            }
        what = kind or "report item or group"
        raise ElementNotFoundError(f"no {what} named {old_name!r} in this report")
    if len(found) > 1:
        kinds = sorted({_local(el) for el in found})
        hint = f"; pass kind ({' / '.join(kinds)})" if len(kinds) > 1 else ""
        raise AmbiguousElementError(
            f"{len(found)} elements are named {old_name!r}{hint}; report item and group names "
            "must be unique report-wide"
        )
    target = found[0]
    target_kind = _local(target)

    mapping = {old_name: new_name}
    to_rename = [target]
    if rename_inner:
        if target_kind != "Tablix":
            raise ValueError(
                f"rename_inner applies to a Tablix (a list from add_list); "
                f"{old_name!r} is a {target_kind}"
            )
        for el in _list_inner(target, old_name):
            inner_new = new_name + el.get("Name")[len(old_name) :]
            names.validate_identifier(inner_new, label)
            mapping[el.get("Name")] = inner_new
            to_rename.append(el)

    registry = names.NameRegistry(doc, exclude=())
    registry.ensure_unique(*mapping.values())
    datasets = _dataset_names(doc)
    for el in to_rename:
        if _local(el) in SCOPE_TAGS and mapping[el.get("Name")] in datasets:
            raise ValueError(
                f"{mapping[el.get('Name')]!r} is a dataset name; a {_local(el)} with it would "
                "make scope strings ambiguous — pick a different name"
            )

    refs = expr_walk.find_inbound_references(doc.root, set(mapping))
    changes: list[tuple[etree._Element, str, str]] = []
    for el in to_rename:
        old = el.get("Name")
        for child in _mirror_children(el):
            if (child.text or "").strip() == old:
                changes.append((child, child.text, mapping[old]))
                child.text = mapping[old]
    changes.extend(expr_walk.rewrite_references(refs, mapping))
    for el in to_rename:
        el.set("Name", mapping[el.get("Name")])
    doc.save()
    # Locators taken after the rename, so they show the new names.
    updated = [
        {"location": expr_walk.locator(elem), "before": before, "after": after}
        for elem, before, after in changes
    ]

    result: dict[str, Any] = {
        "renamed": new_name,
        "old_name": old_name,
        "kind": target_kind,
        "references_updated": updated,
        "changed": True,
    }
    if rename_inner:
        result["also_renamed"] = {k: v for k, v in mapping.items() if k != old_name}
        if len(mapping) == 1:
            result["warnings"] = [
                f"rename_inner: tablix {old_name!r} has no cell rectangle or group named "
                f"{old_name + '_'!r}…; only the tablix was renamed"
            ]
    return result


__all__ = ["KINDS", "SCOPE_TAGS", "rename_report_item"]
