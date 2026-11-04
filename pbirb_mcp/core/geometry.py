"""Report-item and page geometry shared by the readers, lint and layout tools.

RDL positions every report item by ``<Top>`` / ``<Left>`` / ``<Width>`` /
``<Height>`` relative to its container (``<Body>``, ``<PageHeader>``,
``<PageFooter>`` or a ``<Rectangle>``). This module names the report-item
element tags once and holds the RDL defaults Report Builder applies when a
``<Page>`` size element is absent.
"""

from __future__ import annotations

# Every RDL 2016 ReportItem element name. A "List" or "Matrix" in the
# Report Builder UI is a <Tablix>; a gauge is a <GaugePanel>.
REPORT_ITEM_TAGS = (
    "Tablix",
    "Textbox",
    "Image",
    "Rectangle",
    "Subreport",
    "Chart",
    "Map",
    "GaugePanel",
    "Line",
    "CustomReportItem",
)

# RDL defaults for <Page> size elements that are absent (what Report
# Builder renders): US Letter portrait, zero margins.
PAGE_DEFAULTS = {
    "PageHeight": "11in",
    "PageWidth": "8.5in",
    "TopMargin": "0in",
    "BottomMargin": "0in",
    "LeftMargin": "0in",
    "RightMargin": "0in",
}


__all__ = ["PAGE_DEFAULTS", "REPORT_ITEM_TAGS"]
