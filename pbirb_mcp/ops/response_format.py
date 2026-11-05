"""``response_format: "concise" | "detailed"`` for the report readers (issue #55).

``detailed`` (the default) is each reader's full output, unchanged.
``concise`` is a projection of it: names, types, counts and positions —
enough to plan which element to touch — without the bulky parts (DAX
command text, every cell, run styles, chart axes, item inventories). The
projections live here rather than in the readers, so a reader can grow new
fields without touching this module; a concise view only carries the keys
it lists.

:func:`with_response_format` wraps a reader so ``tools.py`` registers it
with the extra argument; :data:`RESPONSE_FORMAT_PARAM` is the inputSchema
entry.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

__all__ = ["RESPONSE_FORMAT_PARAM", "with_response_format"]

RESPONSE_FORMAT_PARAM: dict = {
    "type": "string",
    "enum": ["concise", "detailed"],
    "default": "detailed",
    "description": (
        "'detailed' (default): the full read. 'concise': names, types, counts "
        "and positions only — use it to plan, then read details for what you edit."
    ),
}

_POSITION = ("top", "left", "width", "height")


def _pick(d: dict, keys: tuple[str, ...]) -> dict:
    return {k: d[k] for k in keys if k in d}


def _names(items: Any) -> list:
    if not isinstance(items, list):
        return []
    return [i.get("name") if isinstance(i, dict) else i for i in items]


# ---- per-reader projections ----------------------------------------------

# describe_report keys kept verbatim in concise mode. `tablixes` stays whole:
# its shape hints and column_details are how agents locate cells.
_REPORT_KEEP = (
    "path",
    "data_sources",
    "datasets",
    "parameters",
    "tablixes",
    "charts",
    "maps",
    "gauges",
)
_PAGE_KEEP = (
    "width",
    "height",
    "margin_top",
    "margin_bottom",
    "margin_left",
    "margin_right",
    "printable_width",
)


def _describe_report(d: dict) -> dict:
    out = _pick(d, _REPORT_KEEP)
    if isinstance(d.get("subreports"), list):
        out["subreports"] = _names(d["subreports"])
    if isinstance(d.get("page"), dict):
        out["page"] = _pick(d["page"], _PAGE_KEEP)
    counts = {
        k: len(v)
        for k, v in d.items()
        if k not in out and isinstance(v, list) and k != "subreports"
    }
    if counts:
        out["omitted_counts"] = counts
    return out


def _dataset(d: dict) -> dict:
    out = _pick(d, ("name", "data_source"))
    out["fields"] = _names(d.get("fields"))
    out["query_parameters"] = _names(d.get("query_parameters"))
    out["filter_count"] = len(d.get("filters") or [])
    if "designer_state_present" in d:
        out["designer_state_present"] = d["designer_state_present"]
    return out


def _source(values: Any) -> Any:
    return values.get("source") if isinstance(values, dict) else None


def _parameter(d: dict) -> dict:
    out = _pick(d, ("name", "data_type", "prompt", "multi_value", "hidden", "layout"))
    # Where defaults / dropdown values come from ('static' | 'query' | None);
    # the lists themselves are detail.
    if "default_values" in d:
        out["default_values_source"] = _source(d["default_values"])
    if "valid_values" in d:
        out["valid_values_source"] = _source(d["valid_values"])
    return out


def _tablix(d: dict) -> dict:
    out = _pick(d, ("name", "dataset", *_POSITION))
    out["column_count"] = len(d.get("columns") or [])
    out["row_count"] = len(d.get("rows") or [])
    out["row_groups"] = _names(d.get("row_groups"))
    out["column_groups"] = _names(d.get("column_groups"))
    out["cell_count"] = len(d.get("cells") or [])
    out["header_cell_count"] = len(d.get("header_cells") or [])
    out["corner_cell_count"] = len(d.get("corner_cells") or [])
    return out


def _textbox(d: dict) -> dict:
    out = _pick(d, ("name", "type", *_POSITION))
    out["values"] = [r.get("value") for r in d.get("runs") or [] if isinstance(r, dict)]
    return out


def _image(d: dict) -> dict:
    return _pick(d, ("name", "type", *_POSITION, "source", "value"))


def _rectangle(d: dict) -> dict:
    return _pick(d, ("name", "type", *_POSITION, "contained_items"))


def _chart(d: dict) -> dict:
    out = _pick(d, ("name", "type", *_POSITION, "dataset"))
    out["series"] = [
        _pick(s, ("name", "type")) for s in d.get("series") or [] if isinstance(s, dict)
    ]
    out["category_groups"] = _names(d.get("category_groups"))
    # Dynamic series groups only (static members have no group name).
    out["series_groups"] = [n for n in _names(d.get("series_groups")) if n]
    return out


def _data_source(d: dict) -> dict:
    return _pick(d, ("name", "data_provider", "shared_reference"))


def _each(fn: Callable[[dict], dict]) -> Callable[[list], list]:
    return lambda items: [fn(i) if isinstance(i, dict) else i for i in items]


PROJECTIONS: dict[str, Callable[[Any], Any]] = {
    "describe_report": _describe_report,
    "get_datasets": _each(_dataset),
    "get_dataset": _dataset,
    "get_parameters": _each(_parameter),
    "get_tablixes": _each(_tablix),
    "get_textbox": _textbox,
    "get_image": _image,
    "get_rectangle": _rectangle,
    "get_chart": _chart,
    "get_data_source": _data_source,
}


def with_response_format(tool_name: str, reader: Callable[..., Any]) -> Callable[..., Any]:
    """``reader`` plus a ``response_format`` keyword (default ``detailed``)."""
    project = PROJECTIONS[tool_name]

    @functools.wraps(reader)
    def handler(*args: Any, response_format: str = "detailed", **kwargs: Any) -> Any:
        if response_format not in ("concise", "detailed"):
            raise ValueError("response_format must be 'concise' or 'detailed'")
        result = reader(*args, **kwargs)
        return project(result) if response_format == "concise" else result

    return handler
