"""Report-parameter advanced authoring.

Edits ``<ReportParameter>`` blocks. Two concerns covered here:

1. **Available values** — what the user can pick. Either a static list of
   ``<ParameterValue><Value>/<Label></ParameterValue>`` entries, or a
   ``<DataSetReference>`` pointing at a lookup dataset.
2. **Default values** — what the parameter starts as. Same two sources;
   ``DataSetReference`` for defaults uses ``ValueField`` only (no
   ``LabelField`` — defaults are values, not display strings).

RDL XSD child order inside ``<ReportParameter>``:
  DataType, Nullable, DefaultValue, AllowBlank, Prompt, rd:PromptLocID,
  ValidValues, MultiValue, UsedInQuery, Hidden, ...

Inserts respect that order via :func:`_set_or_replace_in_order`.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.ids import (
    ElementNotFoundError,
    resolve_dataset,
    resolve_parameter,
)
from pbirb_mcp.core.xpath import find_child, q


_VALID_SOURCES = ("static", "query")


_PARAMETER_CHILD_ORDER = (
    "DataType",
    "Nullable",
    "DefaultValue",
    "AllowBlank",
    "Prompt",
    "ValidValues",
    "MultiValue",
    "UsedInQuery",
    "Hidden",
    "DataElementName",
    "DataElementOutput",
    "DynamicDefaultValue",
    "DynamicValidValues",
)


def _set_or_replace_in_order(
    parent: etree._Element, new_child: etree._Element
) -> None:
    new_local = etree.QName(new_child).localname
    existing = find_child(parent, new_local)
    if existing is not None:
        parent.replace(existing, new_child)
        return
    if new_local in _PARAMETER_CHILD_ORDER:
        new_idx = _PARAMETER_CHILD_ORDER.index(new_local)
        for i, child in enumerate(list(parent)):
            local = etree.QName(child).localname
            if (
                local in _PARAMETER_CHILD_ORDER
                and _PARAMETER_CHILD_ORDER.index(local) > new_idx
            ):
                parent.insert(i, new_child)
                return
    parent.append(new_child)


# ---- shared validation ----------------------------------------------------


def _check_source(source: str) -> None:
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"source must be one of {_VALID_SOURCES!r}; got {source!r}"
        )


def _validate_query_args(
    query_dataset: Optional[str],
    query_value_field: Optional[str],
    doc: RDLDocument,
) -> None:
    if not query_dataset or not query_value_field:
        raise ValueError(
            "source='query' requires both query_dataset and query_value_field"
        )
    # Raises ElementNotFoundError when the dataset doesn't exist.
    resolve_dataset(doc, query_dataset)


# ---- builders -------------------------------------------------------------


def _build_static_parameter_values(
    static_values: list[Union[str, dict[str, str]]],
) -> etree._Element:
    """Return a ``<ParameterValues>`` block. Each entry is either a string
    (used for both Value and Label) or a dict with ``value`` and optional
    ``label`` keys."""
    pvs_root = etree.Element(q("ParameterValues"))
    for entry in static_values:
        if isinstance(entry, str):
            value_text = entry
            label_text = entry
        elif isinstance(entry, dict):
            if "value" not in entry:
                raise ValueError(
                    "dict static_values entries must include a 'value' key"
                )
            value_text = entry["value"]
            label_text = entry.get("label", entry["value"])
        else:
            raise ValueError(
                f"static_values entries must be str or dict; got {type(entry).__name__}"
            )
        pv = etree.SubElement(pvs_root, q("ParameterValue"))
        etree.SubElement(pv, q("Value")).text = value_text
        etree.SubElement(pv, q("Label")).text = label_text
    return pvs_root


def _build_dataset_reference(
    dataset_name: str,
    value_field: str,
    label_field: Optional[str],
) -> etree._Element:
    ref = etree.Element(q("DataSetReference"))
    etree.SubElement(ref, q("DataSetName")).text = dataset_name
    etree.SubElement(ref, q("ValueField")).text = value_field
    if label_field is not None:
        etree.SubElement(ref, q("LabelField")).text = label_field
    return ref


# ---- set_parameter_available_values --------------------------------------


def set_parameter_available_values(
    path: str,
    name: str,
    source: str,
    static_values: Optional[list[Union[str, dict[str, str]]]] = None,
    query_dataset: Optional[str] = None,
    query_value_field: Optional[str] = None,
    query_label_field: Optional[str] = None,
) -> dict[str, Any]:
    _check_source(source)

    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)

    valid_values = etree.Element(q("ValidValues"))
    if source == "static":
        if not static_values:
            raise ValueError("source='static' requires static_values")
        valid_values.append(_build_static_parameter_values(static_values))
    else:
        _validate_query_args(query_dataset, query_value_field, doc)
        valid_values.append(
            _build_dataset_reference(
                query_dataset, query_value_field, query_label_field
            )
        )

    _set_or_replace_in_order(parameter, valid_values)

    doc.save()
    return {"parameter": name, "source": source}


# ---- set_parameter_default_values ----------------------------------------


def set_parameter_default_values(
    path: str,
    name: str,
    source: str,
    static_values: Optional[list[str]] = None,
    query_dataset: Optional[str] = None,
    query_value_field: Optional[str] = None,
) -> dict[str, Any]:
    _check_source(source)

    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)

    default_value = etree.Element(q("DefaultValue"))
    if source == "static":
        if not static_values:
            raise ValueError("source='static' requires static_values")
        values_root = etree.SubElement(default_value, q("Values"))
        for value_text in static_values:
            v = etree.SubElement(values_root, q("Value"))
            v.text = value_text
    else:
        _validate_query_args(query_dataset, query_value_field, doc)
        # DefaultValue's DataSetReference has no LabelField — defaults are
        # values, not display strings.
        default_value.append(
            _build_dataset_reference(
                query_dataset, query_value_field, label_field=None
            )
        )

    _set_or_replace_in_order(parameter, default_value)

    doc.save()
    return {"parameter": name, "source": source}


# ---- update_parameter_advanced -------------------------------------------


def update_parameter_advanced(
    path: str,
    name: str,
    multi_value: Optional[bool] = None,
    hidden: Optional[bool] = None,
    allow_null: Optional[bool] = None,
    allow_blank: Optional[bool] = None,
) -> dict[str, Any]:
    """Toggle the four boolean flags on a report parameter.

    Each flag is independently optional; only fields the caller passes
    are written. Cascading parameter dependencies (the original plan's
    ``depends_on``) are NOT a separate flag in RDL — they're inferred
    from ``=Parameters!X.Value`` references in a lookup dataset's
    ``<QueryParameters>``. Wire one parameter to depend on another by
    combining ``set_parameter_available_values(source='query', ...)``
    with ``add_query_parameter(...)`` on that lookup dataset.
    """
    flags: list[tuple[str, Optional[bool]]] = [
        ("MultiValue", multi_value),
        ("Hidden", hidden),
        ("Nullable", allow_null),
        ("AllowBlank", allow_blank),
    ]
    if all(v is None for _, v in flags):
        return {"parameter": name, "changed": []}

    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)

    changed: list[str] = []
    for local, value in flags:
        if value is None:
            continue
        new_node = etree.Element(q(local))
        new_node.text = "true" if value else "false"
        _set_or_replace_in_order(parameter, new_node)
        changed.append(local)

    doc.save()
    return {"parameter": name, "changed": changed}


__all__ = [
    "set_parameter_available_values",
    "set_parameter_default_values",
    "update_parameter_advanced",
]
