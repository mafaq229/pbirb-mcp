"""RDL ``ChartSeries/Type`` + ``ChartSeries/Subtype`` enumerations.

The bundled XSD types both elements as ``xsd:string``, so it accepts any
text; Report Builder and the report server do not. The values here are the
RDL 2016 ones (the ``ChartSeriesType`` / ``ChartSeriesSubtype`` enums of
the Reporting Services object model, which [MS-RDL] serialises by name):

* ``Type`` ∈ Column, Bar, Line, Area, Shape, Scatter, Range, Polar.
* ``Subtype`` depends on ``Type`` — see :data:`SUBTYPES_BY_TYPE`. Pie,
  doughnut, funnel, pyramid, treemap and sunburst charts are all
  ``Type=Shape``; bubble is ``Scatter/Bubble``; stock / candlestick / box
  plot / error bar are ``Range`` subtypes; radar is ``Polar/Radar``; a
  stepped line is ``Line/Stepped``.

The designer-only ``CalloutPie`` subtype is deliberately absent: it exists
in Report Builder's designer object model but not in the rendering enum
that reads RDL.

Friendly shorthands (``Pie`` → ``Shape/Pie``, ``Bubble`` →
``Scatter/Bubble``, ``100`` → ``PercentStacked`` …) are accepted by
:func:`normalize_series_type`, which always returns the canonical pair, so
no non-RDL string is ever written. :func:`series_type_problem` checks a
pair as it stands in a file (no shorthands) for lint.
"""

from __future__ import annotations

from typing import Optional

SERIES_TYPES: tuple[str, ...] = (
    "Column",
    "Bar",
    "Line",
    "Area",
    "Shape",
    "Scatter",
    "Range",
    "Polar",
)

# First entry of each tuple is that type's default subtype.
SUBTYPES_BY_TYPE: dict[str, tuple[str, ...]] = {
    "Column": ("Plain", "Stacked", "PercentStacked"),
    "Bar": ("Plain", "Stacked", "PercentStacked"),
    "Line": ("Plain", "Smooth", "Stepped"),
    "Area": ("Plain", "Smooth", "Stacked", "PercentStacked"),
    "Shape": (
        "Pie",
        "ExplodedPie",
        "Doughnut",
        "ExplodedDoughnut",
        "Funnel",
        "Pyramid",
        "TreeMap",
        "Sunburst",
    ),
    "Scatter": ("Plain", "Bubble"),
    "Range": (
        "Plain",
        "Smooth",
        "Column",
        "Bar",
        "Stock",
        "Candlestick",
        "BoxPlot",
        "ErrorBar",
    ),
    "Polar": ("Plain", "Radar"),
}

SERIES_SUBTYPES: frozenset[str] = frozenset(s for subs in SUBTYPES_BY_TYPE.values() for s in subs)

DEFAULT_SUBTYPE: dict[str, str] = {t: subs[0] for t, subs in SUBTYPES_BY_TYPE.items()}

# RDL default when <Type> / <Subtype> is absent.
RDL_DEFAULT_TYPE = "Column"
RDL_DEFAULT_SUBTYPE = "Plain"

# series_type shorthand → canonical (Type, Subtype).
TYPE_ALIASES: dict[str, tuple[str, str]] = {
    "Pie": ("Shape", "Pie"),
    "ExplodedPie": ("Shape", "ExplodedPie"),
    "Doughnut": ("Shape", "Doughnut"),
    "ExplodedDoughnut": ("Shape", "ExplodedDoughnut"),
    "Funnel": ("Shape", "Funnel"),
    "Pyramid": ("Shape", "Pyramid"),
    "TreeMap": ("Shape", "TreeMap"),
    "Sunburst": ("Shape", "Sunburst"),
    "Bubble": ("Scatter", "Bubble"),
    "Radar": ("Polar", "Radar"),
    "Stock": ("Range", "Stock"),
    "Candlestick": ("Range", "Candlestick"),
    "BoxPlot": ("Range", "BoxPlot"),
    "ErrorBar": ("Range", "ErrorBar"),
    "SmoothLine": ("Line", "Smooth"),
    "StepLine": ("Line", "Stepped"),
}

# series_subtype shorthand → canonical Subtype.
SUBTYPE_ALIASES: dict[str, str] = {
    "SmoothLine": "Smooth",
    "Spline": "Smooth",
    "100": "PercentStacked",
}

# ``Exploded`` qualifies the Pie / Doughnut shorthands (the pre-v0.5 tool
# surface used ``("Doughnut", "Exploded")``).
_EXPLODABLE = {"Pie": "ExplodedPie", "Doughnut": "ExplodedDoughnut"}


def _types_with_subtype(subtype: str) -> list[str]:
    return [t for t, subs in SUBTYPES_BY_TYPE.items() if subtype in subs]


def describe() -> str:
    """One-line ``Type: subtypes`` summary, for tool descriptions/errors."""
    return "; ".join(f"{t}: {'/'.join(subs)}" for t, subs in SUBTYPES_BY_TYPE.items())


def normalize_series_type(
    series_type: str, series_subtype: Optional[str] = None
) -> tuple[str, Optional[str]]:
    """Validate a requested ``(Type, Subtype)`` and return its canonical
    RDL form.

    ``series_type`` may be a real Type or a shorthand from
    :data:`TYPE_ALIASES` (which implies a subtype). ``series_subtype`` may
    be a real Subtype, a shorthand from :data:`SUBTYPE_ALIASES`, or None
    ("not specified" — the caller decides what that means; a type
    shorthand fills in its implied subtype). Raises ``ValueError`` naming
    the valid values on anything that is not RDL or not compatible.
    """
    implied: Optional[str] = None
    if series_type in SUBTYPES_BY_TYPE:
        canonical_type = series_type
    elif series_type in TYPE_ALIASES:
        canonical_type, implied = TYPE_ALIASES[series_type]
    else:
        raise ValueError(
            f"series_type {series_type!r} is not an RDL ChartSeries Type; expected one "
            f"of {list(SERIES_TYPES)} (shorthands: {sorted(TYPE_ALIASES)}). "
            f"Subtypes per type — {describe()}"
        )

    subtype = series_subtype
    if subtype == "Exploded" and series_type in _EXPLODABLE:
        subtype = implied = _EXPLODABLE[series_type]
    elif subtype is not None:
        subtype = SUBTYPE_ALIASES.get(subtype, subtype)

    if implied is not None:
        if subtype is None:
            subtype = implied
        elif subtype != implied:
            raise ValueError(
                f"series_type {series_type!r} is shorthand for "
                f"{canonical_type}/{implied}, which conflicts with series_subtype "
                f"{series_subtype!r}; pass series_type={canonical_type!r} with one of "
                f"{list(SUBTYPES_BY_TYPE[canonical_type])}"
            )

    if subtype is not None and subtype not in SUBTYPES_BY_TYPE[canonical_type]:
        owners = _types_with_subtype(subtype)
        hint = f" ({subtype!r} is a subtype of {owners})" if owners else ""
        raise ValueError(
            f"series_subtype {series_subtype!r} is not valid for Type "
            f"{canonical_type!r}; expected one of "
            f"{list(SUBTYPES_BY_TYPE[canonical_type])}{hint}"
        )
    return canonical_type, subtype


def series_type_problem(type_text: Optional[str], subtype_text: Optional[str]) -> Optional[str]:
    """Why the ``<Type>`` / ``<Subtype>`` texts of a ChartSeries are not a
    valid RDL pair, or None when they are. Absent elements (None) take the
    RDL defaults (Column; any subtype allowed when absent). Shorthands are
    NOT accepted here — this checks what a file actually holds."""
    t = RDL_DEFAULT_TYPE if type_text is None else type_text.strip()
    if t not in SUBTYPES_BY_TYPE:
        hint = ""
        if t in TYPE_ALIASES:
            alias_type, alias_subtype = TYPE_ALIASES[t]
            hint = f" (use Type={alias_type}, Subtype={alias_subtype})"
        return (
            f"Type {t!r} is not an RDL ChartSeries Type{hint}; expected one of {list(SERIES_TYPES)}"
        )
    if subtype_text is None:
        return None
    st = subtype_text.strip()
    if st not in SUBTYPES_BY_TYPE[t]:
        return (
            f"Subtype {st!r} is not valid for Type {t!r}; expected one of "
            f"{list(SUBTYPES_BY_TYPE[t])}"
        )
    return None


__all__ = [
    "DEFAULT_SUBTYPE",
    "RDL_DEFAULT_SUBTYPE",
    "RDL_DEFAULT_TYPE",
    "SERIES_SUBTYPES",
    "SERIES_TYPES",
    "SUBTYPES_BY_TYPE",
    "SUBTYPE_ALIASES",
    "TYPE_ALIASES",
    "describe",
    "normalize_series_type",
    "series_type_problem",
]
