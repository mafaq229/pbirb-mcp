"""Conditional visibility on any named ReportItem.

Group-level visibility belongs to ``set_group_visibility`` (commit 9) and
detail-row visibility to ``set_detail_row_visibility`` (commit 10) — those
edit ``<TablixMember>`` directly. This tool handles every other named
ReportItem: Tablix, Textbox, Image, Rectangle, Subreport, Chart.

Insertion is positioned just before ``<Style>`` (which Report Builder
emits as the last child of ReportItem-derived elements). When ``<Style>``
is absent the new ``<Visibility>`` is appended; when ``<Visibility>`` is
already present it is replaced in place to keep child order stable.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import (
    AmbiguousElementError,
    ElementNotFoundError,
)
from pbirb_mcp.core.xpath import XPATH_NS, find_child, q

# RDL ReportItem-derived types that legitimately accept <Visibility> as a
# child and that this tool is willing to target. Group / DataSet / Field /
# ReportParameter etc. also have a Name attribute but are out of scope.
_VISIBILITY_BEARING_TAGS = (
    "Tablix",
    "Textbox",
    "Image",
    "Rectangle",
    "Subreport",
    "Chart",
)


def _resolve_named_report_item(doc: RDLDocument, name: str) -> etree._Element:
    # XPath constraint: tag local-name must be one of the visibility-bearing
    # ReportItem types. Avoids accidentally hitting <DataSet Name="...">,
    # <ReportParameter Name="...">, <Group Name="...">.
    type_clause = " or ".join(f"local-name()='{tag}'" for tag in _VISIBILITY_BEARING_TAGS)
    matches = list(
        doc.root.xpath(
            f".//*[@Name=$n and ({type_clause})]",
            namespaces=XPATH_NS,
            n=name,
        )
    )
    if not matches:
        raise ElementNotFoundError(
            f"no ReportItem named {name!r} (looked at: {', '.join(_VISIBILITY_BEARING_TAGS)})"
        )
    if len(matches) > 1:
        raise AmbiguousElementError(f"ReportItem name {name!r} matches {len(matches)} elements")
    return matches[0]


def set_element_visibility(
    path: str,
    element_name: str,
    hidden_expression: str,
    toggle_textbox: Optional[str] = None,
) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    element = _resolve_named_report_item(doc, element_name)

    new_vis = etree.Element(q("Visibility"))
    hidden = etree.SubElement(new_vis, q("Hidden"))
    hidden.text = hidden_expression
    if toggle_textbox is not None:
        toggle = etree.SubElement(new_vis, q("ToggleItem"))
        toggle.text = toggle_textbox

    existing = find_child(element, "Visibility")
    if existing is not None:
        element.replace(existing, new_vis)
    else:
        # Place before <Style> if present, otherwise append.
        style = find_child(element, "Style")
        if style is not None:
            style.addprevious(new_vis)
        else:
            element.append(new_vis)

    doc.save()
    return {
        "element": element_name,
        "kind": etree.QName(element).localname,
        "hidden_expression": hidden_expression,
        "toggle_textbox": toggle_textbox,
    }


__all__ = ["set_element_visibility"]
