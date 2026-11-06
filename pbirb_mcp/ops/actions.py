"""Action / tooltip / document-map authoring tools (Phase 5 of v0.3).

RDL ReportItems (Textbox / Image / Rectangle / Chart / ChartSeries)
support three sibling-of-Style mechanisms for interactivity:

- ``<ActionInfo>``: container for one or more ``<Action>`` entries
  (Hyperlink / Drillthrough / BookmarkLink). Wire shape is
  ``<ActionInfo><Actions><Action>…</Action></Actions></ActionInfo>``;
  Report Builder rejects a bare ``<Action>`` directly under a
  ReportItem with ``has invalid child element 'Action'``.
- ``<ToolTip>``: a string or expression rendered when the user hovers.
- ``<DocumentMapLabel>``: a string / expression that appears in the
  rendered report's "document map" (the navigable table-of-contents
  pane).
- ``<Bookmark>``: the anchor a ``BookmarkLink`` action jumps to.

The generic setters (:func:`set_item_action`, :func:`set_item_tooltip`,
:func:`set_bookmark`, :func:`set_document_map_label`) address any named
item; the textbox / image variants are thin wrappers (#109).

This module respects the RDL 2016 trailing-child order via
:func:`_insert_in_item_order` so inserts don't drift round-trip
byte-identity. Earlier v0.3.0 setters emitted a bare ``<Action>`` —
fixed in v0.3.1; the setters here also migrate any legacy bare
``<Action>`` they find on the same ReportItem during write so
upgraded reports stop tripping RB's deserialization.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core import chart_shape
from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.geometry import REPORT_ITEM_TAGS
from pbirb_mcp.core.ids import (
    AmbiguousElementError,
    ElementNotFoundError,
    resolve_report_item,
    resolve_textbox,
)
from pbirb_mcp.core.names import is_identifier
from pbirb_mcp.core.tablix_shape import detach
from pbirb_mcp.core.xpath import XPATH_NS, find_child, find_children, q

# RDL Action sub-element types.
_VALID_ACTION_TYPES = frozenset({"Hyperlink", "Drillthrough", "BookmarkLink"})


# Reused order for Textbox / Image / Rectangle / Subreport — the v0.3
# styling commit defined this; we duplicate the relevant subset rather
# than import from styling.py to avoid a circular dep (styling already
# imports from reader for find_textboxes_by_style helpers).
_REPORT_ITEM_TRAILING_CHILD_ORDER = (
    "ToolTip",
    "DocumentMapLabel",
    "Bookmark",
    "RepeatWith",
    "CustomProperties",
    "ActionInfo",
    "Style",
)


def _insert_in_item_order(item: etree._Element, new_child: etree._Element) -> None:
    """Insert ``new_child`` into a ReportItem (Textbox/Image/Rectangle/...)
    respecting the schema-required trailing-child order. Replaces any
    existing child of the same local name."""
    new_local = etree.QName(new_child).localname
    existing = find_child(item, new_local)
    if existing is not None:
        item.replace(existing, new_child)
        return
    if new_local in _REPORT_ITEM_TRAILING_CHILD_ORDER:
        new_idx = _REPORT_ITEM_TRAILING_CHILD_ORDER.index(new_local)
        for i, child in enumerate(list(item)):
            local = etree.QName(child).localname
            if (
                local in _REPORT_ITEM_TRAILING_CHILD_ORDER
                and _REPORT_ITEM_TRAILING_CHILD_ORDER.index(local) > new_idx
            ):
                item.insert(i, new_child)
                return
    item.append(new_child)


# ---- shared <Action> XML builder -----------------------------------------


def _build_action_xml(
    action_type: str,
    target_expression: str,
    drillthrough_parameters: Optional[list[dict[str, str]]] = None,
) -> etree._Element:
    """Construct an ``<Action>`` element.

    ``action_type`` ∈ Hyperlink / Drillthrough / BookmarkLink.

    For ``Hyperlink``: ``<Action><Hyperlink>...</Hyperlink></Action>``.
    For ``BookmarkLink``: ``<Action><BookmarkLink>...</BookmarkLink></Action>``.
    For ``Drillthrough``: ``<Action><Drillthrough><ReportName>...</ReportName>
    <Parameters>...</Parameters></Drillthrough></Action>``. Each entry in
    ``drillthrough_parameters`` is ``{"name": "P", "value": "=expr"}``,
    optionally with ``"omit"`` (a bool or an ``=expression``) written as
    ``<Omit>``.

    Raises ``ValueError`` on unknown action_type, on empty
    target_expression, and on bad drillthrough parameters: missing keys,
    an empty / non-identifier or duplicate name, or parameters passed with
    a non-Drillthrough action (they would be dropped) (#109).
    """
    if action_type not in _VALID_ACTION_TYPES:
        raise ValueError(
            f"action_type {action_type!r} not valid; expected one of {sorted(_VALID_ACTION_TYPES)}"
        )
    if not isinstance(target_expression, str) or not target_expression.strip():
        raise ValueError(
            "target_expression must be a non-empty string "
            "(URL / report name / bookmark id, optionally =expression); "
            "to remove an action pass clear=true"
        )
    if drillthrough_parameters and action_type != "Drillthrough":
        raise ValueError(
            f"drillthrough_parameters only apply to action_type='Drillthrough', "
            f"not {action_type!r}; drop them or switch the action type"
        )
    _check_drillthrough_parameters(drillthrough_parameters or [])

    action = etree.Element(q("Action"))
    if action_type == "Hyperlink":
        node = etree.SubElement(action, q("Hyperlink"))
        node.text = encode_text(target_expression)
    elif action_type == "BookmarkLink":
        node = etree.SubElement(action, q("BookmarkLink"))
        node.text = encode_text(target_expression)
    else:  # Drillthrough
        drill = etree.SubElement(action, q("Drillthrough"))
        report_name = etree.SubElement(drill, q("ReportName"))
        report_name.text = encode_text(target_expression)
        if drillthrough_parameters:
            params_root = etree.SubElement(drill, q("Parameters"))
            for p in drillthrough_parameters:
                param = etree.SubElement(params_root, q("Parameter"), Name=p["name"])
                value_node = etree.SubElement(param, q("Value"))
                value_node.text = encode_text(p["value"])
                if p.get("omit") is not None:
                    omit = p["omit"]
                    etree.SubElement(param, q("Omit")).text = (
                        ("true" if omit else "false") if isinstance(omit, bool) else omit
                    )
    return action


def _check_drillthrough_parameters(params: Any) -> None:
    """Refuse a malformed ``drillthrough_parameters`` list: each entry a
    dict with ``name`` (a parameter identifier, unique in the list) and a
    string ``value``, and ``omit`` a bool or an ``=expression``."""
    if not isinstance(params, list):
        raise ValueError(f"drillthrough_parameters must be a list; got {type(params).__name__}")
    seen: set[str] = set()
    for i, p in enumerate(params):
        if not isinstance(p, dict):
            raise ValueError(f"drillthrough_parameters[{i}] must be a dict; got {type(p).__name__}")
        if "name" not in p or "value" not in p:
            raise ValueError(f"drillthrough_parameters[{i}] must have 'name' and 'value' keys")
        unknown = sorted(set(p) - {"name", "value", "omit"})
        if unknown:
            raise ValueError(
                f"drillthrough_parameters[{i}] has unknown keys {unknown}; valid: name, value, omit"
            )
        name = p["name"]
        if not is_identifier(name):
            raise ValueError(
                f"drillthrough_parameters[{i}].name {name!r} is not a parameter name "
                "(non-empty; a letter, then letters, digits or _)"
            )
        if name in seen:
            raise ValueError(f"drillthrough_parameters has {name!r} twice")
        seen.add(name)
        if not isinstance(p["value"], str):
            raise ValueError(
                f"drillthrough_parameters[{i}].value must be a string (literal or =expression)"
            )
        omit = p.get("omit")
        omit_ok = (
            omit is None
            or isinstance(omit, bool)
            or (
                isinstance(omit, str)
                and (omit.strip().startswith("=") or omit.lower() in ("true", "false"))
            )
        )
        if not omit_ok:
            raise ValueError(
                f"drillthrough_parameters[{i}].omit must be true / false or an "
                f"=expression; got {omit!r}"
            )


def _build_action_info_xml(
    action_type: str,
    target_expression: str,
    drillthrough_parameters: Optional[list[dict[str, str]]] = None,
) -> etree._Element:
    """Wrap :func:`_build_action_xml` in the RDL 2016 wire shape:
    ``<ActionInfo><Actions><Action>…</Action></Actions></ActionInfo>``.

    Report Builder rejects a bare ``<Action>`` directly under a
    ReportItem; only the wrapped form passes deserialization.
    """
    inner_action = _build_action_xml(action_type, target_expression, drillthrough_parameters)
    action_info = etree.Element(q("ActionInfo"))
    actions_root = etree.SubElement(action_info, q("Actions"))
    actions_root.append(inner_action)
    return action_info


def _drop_legacy_bare_action(item: etree._Element) -> bool:
    """Remove any direct ``<Action>`` child from a ReportItem (or
    ChartSeries). Pre-v0.3.1 setters emitted ``<Action>`` directly
    instead of ``<ActionInfo>/<Actions>/<Action>``; on subsequent
    writes the new setter migrates the file by dropping the bare
    Action and inserting a fresh ActionInfo. Returns True iff a
    legacy node was found and removed."""
    legacy = find_child(item, "Action")
    if legacy is None:
        return False
    item.remove(legacy)
    return True


# ---- ReportItem resolver --------------------------------------------------


# Report items set_item_action writes an <ActionInfo> on (#109). The XSD
# allows it on every ReportItem; these are the ones whose click action is
# the item itself (a chart's lives on its series' data points —
# set_chart_series_action; a tablix's on its cells' textboxes).
ACTION_ITEM_KINDS = ("Textbox", "Image", "Rectangle", "Line")


def _resolve_item(
    doc: RDLDocument, name: str, kinds: Optional[tuple[str, ...]] = None
) -> etree._Element:
    """The report item named ``name`` anywhere in the report (body, page
    header / footer, rectangles, tablix cells); with ``kinds``, refuse an
    item of another type."""
    try:
        item, _container, _region = resolve_report_item(doc, name)
    except ElementNotFoundError:
        raise ElementNotFoundError(
            f"no ReportItem named {name!r} (looked at: {', '.join(REPORT_ITEM_TAGS)})"
        ) from None
    kind = etree.QName(item).localname
    if kinds is not None and kind not in kinds:
        raise ValueError(f"{kind} {name!r} can't carry an action here; expected one of {kinds}")
    return item


def _resolve_kind(doc: RDLDocument, local: str, name: str) -> etree._Element:
    """The ``<local>`` element named ``name`` (any depth)."""
    matches = list(
        doc.root.xpath(f".//*[local-name()='{local}' and @Name=$n]", namespaces=XPATH_NS, n=name)
    )
    if not matches:
        raise ElementNotFoundError(f"no {local} named {name!r}")
    if len(matches) > 1:
        raise AmbiguousElementError(f"{local} name {name!r} matches {len(matches)} elements")
    return matches[0]


# ---- bookmarks ------------------------------------------------------------


def _is_expression(text: Optional[str]) -> bool:
    return bool(text) and text.lstrip().startswith("=")


def _check_bookmark_target(doc: RDLDocument, target: str) -> None:
    """Refuse a literal BookmarkLink target no ``<Bookmark>`` carries. An
    =expression target, or any =expression bookmark in the report (it may
    produce the target at run time), passes — the ``dangling-bookmark-link``
    lint rule's logic."""
    if _is_expression(target):
        return
    literal: set[str] = set()
    for bm in doc.root.iter(q("Bookmark")):
        if _is_expression(bm.text):
            return
        if bm.text and bm.text.strip():
            literal.add(bm.text.strip())
    if target.strip() not in literal:
        raise ValueError(
            f"BookmarkLink target {target.strip()!r} matches no <Bookmark> in the report; "
            f"add it first with set_bookmark(name=<item>, bookmark_or_expression="
            f"{target.strip()!r}), or pass an =expression"
        )


# ---- action writer --------------------------------------------------------


def _write_action(
    doc: RDLDocument,
    item: etree._Element,
    action_type: Optional[str],
    target_expression: Optional[str],
    drillthrough_parameters: Optional[list[dict[str, Any]]],
    clear: bool,
) -> bool:
    """Set (or, with ``clear``, remove) ``item``'s ``<ActionInfo>``.
    Validates everything before touching the tree; saves only on a change.
    Returns ``changed``."""
    if clear:
        if action_type is not None or target_expression is not None or drillthrough_parameters:
            raise ValueError(
                "clear=true removes the action; pass no action_type / target_expression / "
                "drillthrough_parameters with it"
            )
        changed = False
        for local in ("ActionInfo", "Action"):
            node = find_child(item, local)
            if node is not None:
                detach(node)
                changed = True
        if changed:
            doc.save()
        return changed

    if action_type is None or target_expression is None:
        raise ValueError("action_type and target_expression are required (or pass clear=true)")
    new_action_info = _build_action_info_xml(
        action_type, target_expression, drillthrough_parameters
    )
    if action_type == "BookmarkLink":
        _check_bookmark_target(doc, target_expression)

    existing = find_child(item, "ActionInfo")
    legacy_present = find_child(item, "Action") is not None
    if (
        existing is not None
        and not legacy_present
        and _action_info_matches(existing, new_action_info)
    ):
        return False
    _drop_legacy_bare_action(item)
    _insert_in_item_order(item, new_action_info)
    doc.save()
    return True


def _write_text_child(item: etree._Element, local: str, value: str) -> bool:
    """Set ``item``'s ``<local>`` to ``value``, or remove it for ``""``.
    Returns True iff the tree changed (the caller saves)."""
    existing = find_child(item, local)
    if value == "":
        if existing is None:
            return False
        detach(existing)
        return True
    encoded = encode_text(value)
    if existing is not None and existing.text == encoded:
        return False
    new_node = etree.Element(q(local))
    new_node.text = encoded
    _insert_in_item_order(item, new_node)
    return True


# ---- public tools --------------------------------------------------------


def set_item_action(
    path: str,
    name: str,
    action_type: Optional[str] = None,
    target_expression: Optional[str] = None,
    drillthrough_parameters: Optional[list[dict[str, Any]]] = None,
    clear: bool = False,
) -> dict[str, Any]:
    """Set the click action of a named Textbox / Image / Rectangle / Line
    (anywhere in the report): ``Hyperlink`` (URL), ``Drillthrough``
    (report name, with optional ``drillthrough_parameters`` —
    ``[{name, value, omit?}]``) or ``BookmarkLink`` (a ``<Bookmark>`` of
    this report; see :func:`set_bookmark`). ``clear=True`` removes it.

    Refuses (file untouched) bad drillthrough parameters, parameters with a
    non-Drillthrough action and a literal BookmarkLink target no item
    carries. Idempotent. Returns ``{name, kind, action_type, changed}``.
    """
    doc = RDLDocument.open(path)
    item = _resolve_item(doc, name, ACTION_ITEM_KINDS)
    changed = _write_action(
        doc, item, action_type, target_expression, drillthrough_parameters, clear
    )
    return {
        "name": name,
        "kind": etree.QName(item).localname,
        "action_type": None if clear else action_type,
        "changed": changed,
    }


def set_textbox_action(
    path: str,
    textbox_name: str,
    action_type: Optional[str] = None,
    target_expression: Optional[str] = None,
    drillthrough_parameters: Optional[list[dict[str, Any]]] = None,
    clear: bool = False,
) -> dict[str, Any]:
    """:func:`set_item_action` on a named Textbox (tablix cells included).

    ``target_expression`` is the URL (Hyperlink), drill-target report
    name (Drillthrough), or bookmark id (BookmarkLink); a leading ``=``
    makes it an expression. Replaces any existing action; ``clear=True``
    removes it. Returns ``{textbox, kind, action_type, changed}``.
    """
    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)
    changed = _write_action(
        doc, textbox, action_type, target_expression, drillthrough_parameters, clear
    )
    return {
        "textbox": textbox_name,
        "kind": "Textbox",
        "action_type": None if clear else action_type,
        "changed": changed,
    }


def set_image_action(
    path: str,
    image_name: str,
    action_type: Optional[str] = None,
    target_expression: Optional[str] = None,
    drillthrough_parameters: Optional[list[dict[str, Any]]] = None,
    clear: bool = False,
) -> dict[str, Any]:
    """Same shape as :func:`set_textbox_action` but on a named Image."""
    doc = RDLDocument.open(path)
    image = _resolve_kind(doc, "Image", image_name)
    changed = _write_action(
        doc, image, action_type, target_expression, drillthrough_parameters, clear
    )
    return {
        "image": image_name,
        "kind": "Image",
        "action_type": None if clear else action_type,
        "changed": changed,
    }


def set_item_tooltip(path: str, name: str, text_or_expression: str) -> dict[str, Any]:
    """Set ``<ToolTip>`` on any named report item (Textbox, Image,
    Rectangle, Line, Chart, Tablix, Subreport, Map, GaugePanel, ...).
    Literal text or ``=expression``; ``""`` clears. Idempotent. Returns
    ``{name, kind, changed}``."""
    doc = RDLDocument.open(path)
    item = _resolve_item(doc, name)
    changed = _write_text_child(item, "ToolTip", text_or_expression)
    if changed:
        doc.save()
    return {"name": name, "kind": etree.QName(item).localname, "changed": changed}


def set_textbox_tooltip(
    path: str,
    textbox_name: str,
    text_or_expression: str,
) -> dict[str, Any]:
    """Set ``<Textbox>/<ToolTip>`` (:func:`set_item_tooltip` for a
    textbox, tablix cells included). ``""`` clears. Idempotent: same value
    → ``{changed: false}``, no save."""
    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)
    changed = _write_text_child(textbox, "ToolTip", text_or_expression)
    if changed:
        doc.save()
    return {"textbox": textbox_name, "kind": "Textbox", "changed": changed}


def set_bookmark(path: str, name: str, bookmark_or_expression: str) -> dict[str, Any]:
    """Set ``<Bookmark>`` on any named report item — the anchor a
    ``BookmarkLink`` action jumps to. Literal id or ``=expression``;
    ``""`` clears. Idempotent. Returns ``{name, kind, changed}``, plus
    ``warnings`` when another item already carries the same literal
    bookmark (a link jumps to the first one)."""
    if not isinstance(bookmark_or_expression, str):
        raise ValueError("bookmark_or_expression must be a string ('' clears)")
    doc = RDLDocument.open(path)
    item = _resolve_item(doc, name)
    changed = _write_text_child(item, "Bookmark", bookmark_or_expression)
    result: dict[str, Any] = {
        "name": name,
        "kind": etree.QName(item).localname,
        "changed": changed,
    }
    wanted = bookmark_or_expression.strip()
    if wanted and not _is_expression(wanted):
        others = sorted(
            bm.getparent().get("Name") or "?"
            for bm in doc.root.iter(q("Bookmark"))
            if bm.getparent() is not item and (bm.text or "").strip() == wanted
        )
        if others:
            result["warnings"] = [
                f"bookmark {wanted!r} is also on {', '.join(others)}; a BookmarkLink "
                "jumps to the first one"
            ]
    if changed:
        doc.save()
    return result


def set_document_map_label(
    path: str,
    element_name: str,
    label_or_expression: str,
) -> dict[str, Any]:
    """Set ``<DocumentMapLabel>`` on any named ReportItem (Textbox /
    Image / Rectangle / Chart / Tablix / etc.).

    The DocumentMapLabel surfaces in the rendered report's navigable
    table-of-contents pane. Use literal text for static labels or
    ``=expression`` for dynamic ones. Pass ``""`` to clear.

    Idempotent: same value → ``{changed: false}``, no save.
    """
    doc = RDLDocument.open(path)
    element = _resolve_item(doc, element_name)
    kind = etree.QName(element).localname
    changed = _write_text_child(element, "DocumentMapLabel", label_or_expression)
    if changed:
        doc.save()
    return {"element": element_name, "kind": kind, "changed": changed}


# ---- helpers --------------------------------------------------------------


def _action_info_matches(existing: etree._Element, new_action_info: etree._Element) -> bool:
    """Structural equality check on two ``<ActionInfo>`` subtrees.

    Walks ``ActionInfo/Actions/Action`` on each side and delegates to
    :func:`_action_matches` for the inner-Action comparison. Returns
    False if either side has zero or multiple Action children — we
    only emit single-Action ActionInfo blocks today.
    """
    e_actions = find_child(existing, "Actions")
    n_actions = find_child(new_action_info, "Actions")
    if e_actions is None or n_actions is None:
        return False
    e_list = find_children(e_actions, "Action")
    n_list = find_children(n_actions, "Action")
    if len(e_list) != 1 or len(n_list) != 1:
        return False
    return _action_matches(e_list[0], n_list[0])


def _action_matches(existing: etree._Element, new_action: etree._Element) -> bool:
    """Structural equality check on two ``<Action>`` subtrees that
    avoids namespace-prefix drift in serialised bytes.

    Compares the single child's local name, its inner text (for
    Hyperlink / BookmarkLink) or its ReportName + Parameters list (for
    Drillthrough)."""
    existing_children = list(existing)
    new_children = list(new_action)
    if len(existing_children) != 1 or len(new_children) != 1:
        return False
    e_inner = existing_children[0]
    n_inner = new_children[0]
    if etree.QName(e_inner).localname != etree.QName(n_inner).localname:
        return False
    inner_local = etree.QName(e_inner).localname
    if inner_local in ("Hyperlink", "BookmarkLink"):
        return e_inner.text == n_inner.text
    if inner_local == "Drillthrough":
        e_report = find_child(e_inner, "ReportName")
        n_report = find_child(n_inner, "ReportName")
        if (e_report.text if e_report is not None else None) != (
            n_report.text if n_report is not None else None
        ):
            return False
        e_params_root = find_child(e_inner, "Parameters")
        n_params_root = find_child(n_inner, "Parameters")
        e_params = find_children(e_params_root, "Parameter") if e_params_root is not None else []
        n_params = find_children(n_params_root, "Parameter") if n_params_root is not None else []
        if len(e_params) != len(n_params):
            return False
        for ep, np in zip(e_params, n_params):
            if ep.get("Name") != np.get("Name"):
                return False
            for local in ("Value", "Omit"):
                ev = find_child(ep, local)
                nv = find_child(np, local)
                if (ev.text if ev is not None else None) != (nv.text if nv is not None else None):
                    return False
        return True
    return False


# ---- set_chart_series_action ---------------------------------------------


# Per RDL 2016 XSD, ChartSeries child order (subset relevant to our
# writers): Hidden, ChartSmartLabel, ChartDataPoints, Type, Subtype,
# EmptyPoints, Style, ChartItemInLegend, ChartDataLabel, ChartMarker,
# ChartEmptyPoints, LegendName, ...
#
# **ActionInfo / Action are NOT children of ChartSeries** in RDL 2016 —
# RB rejects both with "has invalid child element 'ActionInfo'". The
# action lives one level deeper, on the series's template
# <ChartDataPoint> inside <ChartDataPoints>.
_CHART_SERIES_CHILD_ORDER = (
    "Hidden",
    "ChartSmartLabel",
    "ChartDataPoints",
    "Type",
    "Subtype",
    "EmptyPoints",
    "Style",
    "ChartItemInLegend",
    "ChartDataLabel",
    "ChartMarker",
    "ChartEmptyPoints",
    "LegendName",
    "LegendText",
    "HideInLegend",
    "ValueAxisName",
    "CategoryAxisName",
    "ChartAreaName",
)


# Per RDL 2016 XSD, ChartDataPoint child order:
#   ChartDataPointValues, Style, ChartMarker, ChartDataLabel,
#   ActionInfo, CustomProperties, ChartItemInLegend,
#   DataElementName, DataElementOutput
# ActionInfo sits after ChartDataLabel and before CustomProperties.
_CHART_DATA_POINT_CHILD_ORDER = (
    "ChartDataPointValues",
    "Style",
    "ChartMarker",
    "ChartDataLabel",
    "ActionInfo",
    "CustomProperties",
    "ChartItemInLegend",
    "DataElementName",
    "DataElementOutput",
)


def _insert_in_chart_data_point_order(
    data_point: etree._Element, new_child: etree._Element
) -> None:
    """Insert ``new_child`` into a ChartDataPoint respecting the
    schema-required order. Replaces any existing element of the same
    local name."""
    new_local = etree.QName(new_child).localname
    existing = find_child(data_point, new_local)
    if existing is not None:
        data_point.replace(existing, new_child)
        return
    if new_local in _CHART_DATA_POINT_CHILD_ORDER:
        new_idx = _CHART_DATA_POINT_CHILD_ORDER.index(new_local)
        for i, child in enumerate(list(data_point)):
            local = etree.QName(child).localname
            if (
                local in _CHART_DATA_POINT_CHILD_ORDER
                and _CHART_DATA_POINT_CHILD_ORDER.index(local) > new_idx
            ):
                data_point.insert(i, new_child)
                return
    data_point.append(new_child)


def _series_template_data_point(
    series: etree._Element,
) -> etree._Element:
    """Return the series's first ``<ChartDataPoint>`` (the template
    that gets rendered per data row). Raises if the structure is
    missing — every real chart series has one.
    """
    cdps = find_child(series, "ChartDataPoints")
    if cdps is None:
        raise ValueError(
            f"ChartSeries {series.get('Name')!r} has no <ChartDataPoints> "
            "block; can't host an action without a data-point template."
        )
    cdp = find_child(cdps, "ChartDataPoint")
    if cdp is None:
        raise ValueError(
            f"ChartSeries {series.get('Name')!r} has empty "
            "<ChartDataPoints>; expected one template <ChartDataPoint>."
        )
    return cdp


def _insert_in_chart_series_order(series: etree._Element, new_child: etree._Element) -> None:
    """Insert ``new_child`` into a ChartSeries respecting the
    schema-required order. Replaces any existing element of the same
    local name."""
    new_local = etree.QName(new_child).localname
    existing = find_child(series, new_local)
    if existing is not None:
        series.replace(existing, new_child)
        return
    if new_local in _CHART_SERIES_CHILD_ORDER:
        new_idx = _CHART_SERIES_CHILD_ORDER.index(new_local)
        for i, child in enumerate(list(series)):
            local = etree.QName(child).localname
            if (
                local in _CHART_SERIES_CHILD_ORDER
                and _CHART_SERIES_CHILD_ORDER.index(local) > new_idx
            ):
                series.insert(i, new_child)
                return
    series.append(new_child)


def set_chart_series_action(
    path: str,
    chart_name: str,
    series_name: str,
    action_type: str,
    target_expression: Optional[str] = None,
    drillthrough_parameters: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    """Set the click-through action for a chart series's data points.

    Per RDL 2016 schema, ``<ActionInfo>`` is **not** a child of
    ``<ChartSeries>``; it lives on the series's template
    ``<ChartDataPoint>`` (the first ChartDataPoint inside
    ``<ChartDataPoints>``). RB rejects ActionInfo at the series level
    with ``has invalid child element 'ActionInfo'``.

    This setter:

    * resolves the series via ``(chart_name, series_name)`` (same
      handle :func:`pbirb_mcp.ops.chart.set_chart_series_type` uses),
    * walks into the series's template ChartDataPoint,
    * writes ``<ActionInfo>/<Actions>/<Action>`` there per the RDL 2016
      ChartDataPoint XSD ordering (after ChartDataLabel, before
      CustomProperties),
    * migrates any legacy bare ``<Action>`` or wrong-host
      ``<ActionInfo>`` sitting at the ChartSeries level (pre-v0.3.1
      shape) by removing them — re-running the setter on an upgraded
      report self-heals it.

    ``drillthrough_parameters`` are only valid with ``Drillthrough`` (a
    Hyperlink or BookmarkLink has no parameters; they used to be dropped
    silently). ``action_type="None"`` removes the series' action — the
    ``ActionInfo`` of its data points and any legacy series-level one —
    and takes no target or parameters.

    Returns ``{chart, series, kind: 'ChartSeries', action_type,
    changed: bool}``.
    """
    # Import lazily to avoid circular imports (chart.py is independent
    # of actions.py today; that stays true).
    from pbirb_mcp.ops.chart import (
        _find_series,
        _resolve_chart,
        _series_collection,
    )

    if action_type == "None":
        if target_expression not in (None, "") or drillthrough_parameters:
            raise ValueError(
                "action_type='None' removes the action; target_expression and "
                "drillthrough_parameters must be omitted"
            )
        return _clear_chart_series_action(path, chart_name, series_name)
    if drillthrough_parameters and action_type != "Drillthrough":
        raise ValueError(
            f"drillthrough_parameters are only valid with action_type='Drillthrough'; "
            f"a {action_type} action has no parameters"
        )
    new_action_info = _build_action_info_xml(
        action_type, target_expression, drillthrough_parameters
    )

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    sc = _series_collection(chart)
    series = _find_series(sc, series_name)
    data_point = _series_template_data_point(series)

    # Migrate any pre-v0.3.1 wrong-host shapes on the SERIES level
    # before writing the canonical ActionInfo on the data point.
    legacy_series_action = find_child(series, "Action")
    legacy_series_info = find_child(series, "ActionInfo")
    legacy_present = legacy_series_action is not None or legacy_series_info is not None

    existing = find_child(data_point, "ActionInfo")
    if (
        existing is not None
        and not legacy_present
        and _action_info_matches(existing, new_action_info)
    ):
        return {
            "chart": chart_name,
            "series": series_name,
            "kind": "ChartSeries",
            "action_type": action_type,
            "changed": False,
        }

    if legacy_series_action is not None:
        series.remove(legacy_series_action)
    if legacy_series_info is not None:
        series.remove(legacy_series_info)
    _insert_in_chart_data_point_order(data_point, new_action_info)
    doc.save()
    return {
        "chart": chart_name,
        "series": series_name,
        "kind": "ChartSeries",
        "action_type": action_type,
        "changed": True,
    }


def _clear_chart_series_action(path: str, chart_name: str, series_name: str) -> dict[str, Any]:
    """``set_chart_series_action(action_type='None')``: drop every
    ``ActionInfo`` of the series' data points plus legacy series-level
    ``Action`` / ``ActionInfo``. No save when there was none."""
    from pbirb_mcp.ops.chart import _find_series, _resolve_chart, _series_collection

    doc = RDLDocument.open(path)
    chart = _resolve_chart(doc, chart_name)
    series = _find_series(_series_collection(chart), series_name)
    removed = False
    for host in [series, *chart_shape.data_points(series)]:
        for local in ("ActionInfo", "Action"):
            for node in find_children(host, local):
                host.remove(node)
                removed = True
    if removed:
        doc.save()
    return {
        "chart": chart_name,
        "series": series_name,
        "kind": "ChartSeries",
        "action_type": "None",
        "changed": removed,
    }


__all__ = [
    "ACTION_ITEM_KINDS",
    "set_bookmark",
    "set_chart_series_action",
    "set_document_map_label",
    "set_image_action",
    "set_item_action",
    "set_item_tooltip",
    "set_textbox_action",
    "set_textbox_tooltip",
]
