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

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import (
    AmbiguousElementError,
    ElementNotFoundError,
    resolve_textbox,
)
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


def _insert_in_item_order(
    item: etree._Element, new_child: etree._Element
) -> None:
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
    ``drillthrough_parameters`` is ``{"name": "P", "value": "=expr"}``.

    Raises ``ValueError`` on unknown action_type, on empty
    target_expression, or on missing keys in drillthrough parameters.
    """
    if action_type not in _VALID_ACTION_TYPES:
        raise ValueError(
            f"action_type {action_type!r} not valid; expected one of "
            f"{sorted(_VALID_ACTION_TYPES)}"
        )
    if not target_expression or not str(target_expression).strip():
        raise ValueError(
            "target_expression must be a non-empty string "
            "(URL / report name / bookmark id, optionally =expression)"
        )

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
            for i, p in enumerate(drillthrough_parameters):
                if not isinstance(p, dict):
                    raise ValueError(
                        f"drillthrough_parameters[{i}] must be a dict; "
                        f"got {type(p).__name__}"
                    )
                if "name" not in p or "value" not in p:
                    raise ValueError(
                        f"drillthrough_parameters[{i}] must have 'name' "
                        "and 'value' keys"
                    )
                param = etree.SubElement(params_root, q("Parameter"), Name=p["name"])
                value_node = etree.SubElement(param, q("Value"))
                value_node.text = encode_text(p["value"])
    return action


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
    inner_action = _build_action_xml(
        action_type, target_expression, drillthrough_parameters
    )
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


_NAMED_REPORT_ITEM_LOCALS = (
    "Textbox",
    "Image",
    "Rectangle",
    "Subreport",
    "Chart",
    "Tablix",
    "Map",
    "Gauge",
    "Line",
    "List",
)


def _resolve_named_report_item(
    doc: RDLDocument, name: str
) -> etree._Element:
    """Find any named ReportItem (Textbox / Image / Rectangle / etc.)
    by Name attribute. Used by ``set_document_map_label`` which works on
    any positioned item."""
    type_clause = " or ".join(
        f"local-name()='{tag}'" for tag in _NAMED_REPORT_ITEM_LOCALS
    )
    matches = list(
        doc.root.xpath(
            f".//*[@Name=$n and ({type_clause})]",
            namespaces=XPATH_NS,
            n=name,
        )
    )
    if not matches:
        raise ElementNotFoundError(
            f"no ReportItem named {name!r} (looked at: "
            f"{', '.join(_NAMED_REPORT_ITEM_LOCALS)})"
        )
    if len(matches) > 1:
        raise AmbiguousElementError(
            f"ReportItem name {name!r} matches {len(matches)} elements"
        )
    return matches[0]


def _resolve_image(doc: RDLDocument, name: str) -> etree._Element:
    matches = list(
        doc.root.xpath(
            ".//*[local-name()='Image' and @Name=$n]",
            namespaces=XPATH_NS,
            n=name,
        )
    )
    if not matches:
        raise ElementNotFoundError(f"no Image named {name!r}")
    if len(matches) > 1:
        raise AmbiguousElementError(
            f"Image name {name!r} matches {len(matches)} elements"
        )
    return matches[0]


# ---- public tools --------------------------------------------------------


def set_textbox_action(
    path: str,
    textbox_name: str,
    action_type: str,
    target_expression: str,
    drillthrough_parameters: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    """Set ``<Textbox>/<Action>`` to one of Hyperlink / Drillthrough /
    BookmarkLink.

    ``target_expression`` is the URL (Hyperlink), drill-target report
    name (Drillthrough), or bookmark id (BookmarkLink). Pass an
    expression with a leading ``=`` for dynamic values.

    For ``Drillthrough``, ``drillthrough_parameters`` is an optional
    list of ``{"name": "<paramName>", "value": "<value-or-expr>"}``
    dicts that get wired into ``<Drillthrough>/<Parameters>``.

    Replaces any existing Action on the textbox. Returns ``{textbox,
    kind, action_type, changed: bool}``. The change check is
    structural — same action_type + target + drillthrough_parameters
    short-circuits without saving.
    """
    new_action_info = _build_action_info_xml(
        action_type, target_expression, drillthrough_parameters
    )

    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)

    existing = find_child(textbox, "ActionInfo")
    legacy_present = find_child(textbox, "Action") is not None
    if (
        existing is not None
        and not legacy_present
        and _action_info_matches(existing, new_action_info)
    ):
        return {
            "textbox": textbox_name,
            "kind": "Textbox",
            "action_type": action_type,
            "changed": False,
        }
    _drop_legacy_bare_action(textbox)
    _insert_in_item_order(textbox, new_action_info)
    doc.save()
    return {
        "textbox": textbox_name,
        "kind": "Textbox",
        "action_type": action_type,
        "changed": True,
    }


def set_image_action(
    path: str,
    image_name: str,
    action_type: str,
    target_expression: str,
    drillthrough_parameters: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    """Same shape as :func:`set_textbox_action` but on a named Image."""
    new_action_info = _build_action_info_xml(
        action_type, target_expression, drillthrough_parameters
    )
    doc = RDLDocument.open(path)
    image = _resolve_image(doc, image_name)

    existing = find_child(image, "ActionInfo")
    legacy_present = find_child(image, "Action") is not None
    if (
        existing is not None
        and not legacy_present
        and _action_info_matches(existing, new_action_info)
    ):
        return {
            "image": image_name,
            "kind": "Image",
            "action_type": action_type,
            "changed": False,
        }
    _drop_legacy_bare_action(image)
    _insert_in_item_order(image, new_action_info)
    doc.save()
    return {
        "image": image_name,
        "kind": "Image",
        "action_type": action_type,
        "changed": True,
    }


def set_textbox_tooltip(
    path: str,
    textbox_name: str,
    text_or_expression: str,
) -> dict[str, Any]:
    """Set ``<Textbox>/<ToolTip>``.

    ``text_or_expression`` is the literal hover text or an
    ``=expression``. Pass ``""`` to clear the existing ToolTip.
    Idempotent: same value → ``{changed: false}``, no save.
    """
    doc = RDLDocument.open(path)
    textbox = resolve_textbox(doc, textbox_name)

    existing = find_child(textbox, "ToolTip")
    if text_or_expression == "":
        if existing is None:
            return {
                "textbox": textbox_name,
                "kind": "Textbox",
                "changed": False,
            }
        textbox.remove(existing)
        doc.save()
        return {"textbox": textbox_name, "kind": "Textbox", "changed": True}

    encoded = encode_text(text_or_expression)
    if existing is not None and existing.text == encoded:
        return {
            "textbox": textbox_name,
            "kind": "Textbox",
            "changed": False,
        }
    new_node = etree.Element(q("ToolTip"))
    new_node.text = encoded
    _insert_in_item_order(textbox, new_node)
    doc.save()
    return {"textbox": textbox_name, "kind": "Textbox", "changed": True}


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
    element = _resolve_named_report_item(doc, element_name)
    kind = etree.QName(element).localname

    existing = find_child(element, "DocumentMapLabel")
    if label_or_expression == "":
        if existing is None:
            return {"element": element_name, "kind": kind, "changed": False}
        element.remove(existing)
        doc.save()
        return {"element": element_name, "kind": kind, "changed": True}

    encoded = encode_text(label_or_expression)
    if existing is not None and existing.text == encoded:
        return {"element": element_name, "kind": kind, "changed": False}
    new_node = etree.Element(q("DocumentMapLabel"))
    new_node.text = encoded
    _insert_in_item_order(element, new_node)
    doc.save()
    return {"element": element_name, "kind": kind, "changed": True}


# ---- helpers --------------------------------------------------------------


def _action_info_matches(
    existing: etree._Element, new_action_info: etree._Element
) -> bool:
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


def _action_matches(
    existing: etree._Element, new_action: etree._Element
) -> bool:
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
        e_params = (
            find_children(e_params_root, "Parameter")
            if e_params_root is not None
            else []
        )
        n_params = (
            find_children(n_params_root, "Parameter")
            if n_params_root is not None
            else []
        )
        if len(e_params) != len(n_params):
            return False
        for ep, np in zip(e_params, n_params):
            if ep.get("Name") != np.get("Name"):
                return False
            ev = find_child(ep, "Value")
            nv = find_child(np, "Value")
            if (ev.text if ev is not None else None) != (
                nv.text if nv is not None else None
            ):
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


def _insert_in_chart_series_order(
    series: etree._Element, new_child: etree._Element
) -> None:
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
    target_expression: str,
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


__all__ = [
    "set_chart_series_action",
    "set_document_map_label",
    "set_image_action",
    "set_textbox_action",
    "set_textbox_tooltip",
]
