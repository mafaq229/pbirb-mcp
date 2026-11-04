"""add_static_column vs merged (ColSpan) cells (#34, follow-up)."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core.tablix_headers import shape_problems
from pbirb_mcp.ops.tablix_static import add_static_column
from pbirb_mcp.ops.validate import validate_report
from tests._band_fixture import build_band_report

NS = {"r": "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"}


def _row(path: Path, i: int):
    tree = etree.parse(str(path))
    cells = tree.xpath(
        f"//r:Tablix[@Name='MainTable']//r:TablixRow[{i + 1}]/r:TablixCells/r:TablixCell",
        namespaces=NS,
    )
    return [
        (
            c.xpath("string(.//r:Textbox/@Name)", namespaces=NS) or None,
            int(c.xpath("string(.//r:ColSpan)", namespaces=NS) or 1),
        )
        for c in cells
    ]


def _tablix(path: Path):
    return etree.parse(str(path)).xpath("//r:Tablix[@Name='MainTable']", namespaces=NS)[0]


@pytest.fixture
def band(tmp_path: Path) -> Path:
    return build_band_report(tmp_path / "band.rdl")


def test_inside_a_band_widens_it_instead_of_hiding_the_cell(band):
    out = add_static_column(str(band), "MainTable", "New", ["", "hdr", "data"], position=1)
    assert out["widened_span_rows"] == [0]
    # Band row: IVMSBand now spans 4 (its 3 + the new column), placeholders follow.
    row0 = _row(band, 0)
    assert row0[0] == ("IVMSBand", 4)
    assert [n for n, _ in row0[1:4]] == [None, None, None]
    # The bare name goes to the first row that got a cell.
    assert out["textboxes"][0] == "New"
    assert _row(band, 1)[1][0] == "New"
    assert len(_row(band, 0)) == len(_row(band, 1)) == len(_row(band, 2))
    assert shape_problems(_tablix(band)) == []
    assert validate_report(str(band))["valid"]


def test_text_for_a_merged_row_is_refused_file_unchanged(band):
    before = band.read_bytes()
    with pytest.raises(ValueError, match="merged"):
        add_static_column(str(band), "MainTable", "New", ["band text", "h", "d"], position=1)
    assert band.read_bytes() == before


def test_at_a_span_boundary_adds_a_normal_cell(band):
    out = add_static_column(str(band), "MainTable", "New", ["b", "h", "d"], position=3)
    assert "widened_span_rows" not in out
    row0 = _row(band, 0)
    assert row0[0] == ("IVMSBand", 3)
    assert row0[3][0] == "New"
    assert validate_report(str(band))["valid"]
