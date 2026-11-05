"""A dataset carrying Report Builder's real ``rd:DesignerState`` shape, like
the "RAG Report" the #70 review used (not shipped in tests/fixtures).

Report Builder's DAX Query Designer stores its state as::

    <rd:DesignerState>
      <QueryDefinition xmlns="http://schemas.microsoft.com/AnalysisServices/QueryDefinition">
        <CommandType>DAX</CommandType>
        <QuerySpecification xsi:type="MDXQuerySpecification"> ... Select/Items ... </QuerySpecification>
        <Query>
          <Statement>EVALUATE ...</Statement>
          <ParameterDefinitions />
        </Query>
      </QueryDefinition>
    </rd:DesignerState>

``build_designer_state_report`` copies the minimal fixture and gives
``MainDataset`` that block (in place of ``rd:UseGenericDesigner``), with
``Statement`` equal to the CommandText unless ``statement`` says otherwise.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

FIXTURE = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
QD_NS = "http://schemas.microsoft.com/AnalysisServices/QueryDefinition"
COMMAND_TEXT = "EVALUATE 'Sales'"

_DESIGNER_STATE = """<rd:DesignerState>
          <QueryDefinition xmlns="{qd}">
            <CommandType>DAX</CommandType>
            <QuerySpecification xsi:type="MDXQuerySpecification" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
              <Select>
                <Items>
                  <Item>
                    <ID xsi:type="Level">
                      <DimensionName>Sales</DimensionName>
                      <HierarchyName>ProductName</HierarchyName>
                      <HierarchyUniqueName>[Sales].[ProductName]</HierarchyUniqueName>
                      <LevelName>ProductName</LevelName>
                      <UniqueName>[Sales].[ProductName].[ProductName]</UniqueName>
                    </ID>
                    <ItemCaption>ProductName</ItemCaption>
                    <UniqueName>true</UniqueName>
                  </Item>
                </Items>
              </Select>
              <AutoExecute>false</AutoExecute>
              <From>Model</From>
            </QuerySpecification>
            <Query>
              <Statement>{statement}</Statement>
              <ParameterDefinitions />
            </Query>
          </QueryDefinition>
        </rd:DesignerState>"""


def build_designer_state_report(dest: Path, statement: Optional[str] = None) -> Path:
    """Write the patched report to ``dest`` and return it."""
    shutil.copy(FIXTURE, dest)
    text = dest.read_text(encoding="utf-8")
    marker = "<rd:UseGenericDesigner>true</rd:UseGenericDesigner>"
    assert text.count(marker) == 1
    block = _DESIGNER_STATE.format(
        qd=QD_NS, statement=escape(COMMAND_TEXT if statement is None else statement)
    )
    dest.write_text(text.replace(marker, block), encoding="utf-8")
    return dest
