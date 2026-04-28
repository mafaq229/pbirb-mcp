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
    resolve_dataset,
    resolve_parameter,
)
from pbirb_mcp.core.xpath import find_child, find_children, q

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


def _set_or_replace_in_order(parent: etree._Element, new_child: etree._Element) -> None:
    new_local = etree.QName(new_child).localname
    existing = find_child(parent, new_local)
    if existing is not None:
        parent.replace(existing, new_child)
        return
    if new_local in _PARAMETER_CHILD_ORDER:
        new_idx = _PARAMETER_CHILD_ORDER.index(new_local)
        for i, child in enumerate(list(parent)):
            local = etree.QName(child).localname
            if local in _PARAMETER_CHILD_ORDER and _PARAMETER_CHILD_ORDER.index(local) > new_idx:
                parent.insert(i, new_child)
                return
    parent.append(new_child)


# ---- shared validation ----------------------------------------------------


def _check_source(source: str) -> None:
    if source not in _VALID_SOURCES:
        raise ValueError(f"source must be one of {_VALID_SOURCES!r}; got {source!r}")


def _validate_query_args(
    query_dataset: Optional[str],
    query_value_field: Optional[str],
    doc: RDLDocument,
) -> None:
    if not query_dataset or not query_value_field:
        raise ValueError("source='query' requires both query_dataset and query_value_field")
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
                raise ValueError("dict static_values entries must include a 'value' key")
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
    """Set or clear ``<ValidValues>`` on a report parameter.

    Modes:
    - ``source='static'`` with a non-empty ``static_values`` writes the
      value/label list.
    - ``source='static'`` with ``static_values=[]`` (or ``None``) **clears**
      the ``<ValidValues>`` element entirely. Mirrors the
      ``set_parameter_prompt('')`` clear convention used elsewhere in the
      parameter-CRUD surface.
    - ``source='query'`` writes a ``<DataSetReference>``.
    """
    _check_source(source)

    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)

    if source == "static" and not static_values:
        # Clear: drop the existing <ValidValues> element if present.
        existing = find_child(parameter, "ValidValues")
        if existing is not None:
            parameter.remove(existing)
            doc.save()
        return {"parameter": name, "source": source, "cleared": True}

    valid_values = etree.Element(q("ValidValues"))
    if source == "static":
        valid_values.append(_build_static_parameter_values(static_values))
    else:
        _validate_query_args(query_dataset, query_value_field, doc)
        valid_values.append(
            _build_dataset_reference(query_dataset, query_value_field, query_label_field)
        )

    _set_or_replace_in_order(parameter, valid_values)

    doc.save()
    return {"parameter": name, "source": source, "cleared": False}


# ---- set_parameter_default_values ----------------------------------------


def set_parameter_default_values(
    path: str,
    name: str,
    source: str,
    static_values: Optional[list[str]] = None,
    query_dataset: Optional[str] = None,
    query_value_field: Optional[str] = None,
) -> dict[str, Any]:
    """Set or clear ``<DefaultValue>`` on a report parameter.

    Modes:
    - ``source='static'`` with a non-empty ``static_values`` writes the
      ``<Values>`` list.
    - ``source='static'`` with ``static_values=[]`` (or ``None``) **clears**
      the ``<DefaultValue>`` element entirely. Mirrors the
      ``set_parameter_prompt('')`` clear convention used elsewhere in the
      parameter-CRUD surface.
    - ``source='query'`` writes a ``<DataSetReference>`` with
      ``ValueField`` only (defaults are values, not display strings).
    """
    _check_source(source)

    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)

    if source == "static" and not static_values:
        existing = find_child(parameter, "DefaultValue")
        if existing is not None:
            parameter.remove(existing)
            doc.save()
        return {"parameter": name, "source": source, "cleared": True}

    default_value = etree.Element(q("DefaultValue"))
    if source == "static":
        values_root = etree.SubElement(default_value, q("Values"))
        for value_text in static_values:
            v = etree.SubElement(values_root, q("Value"))
            v.text = value_text
    else:
        _validate_query_args(query_dataset, query_value_field, doc)
        # DefaultValue's DataSetReference has no LabelField — defaults are
        # values, not display strings.
        default_value.append(
            _build_dataset_reference(query_dataset, query_value_field, label_field=None)
        )

    _set_or_replace_in_order(parameter, default_value)

    doc.save()
    return {"parameter": name, "source": source, "cleared": False}


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


# ---- v0.2: full parameter CRUD --------------------------------------------


_VALID_DATA_TYPES = ("Boolean", "DateTime", "Integer", "Float", "String")


def set_parameter_prompt(
    path: str,
    name: str,
    prompt: str,
) -> dict[str, Any]:
    """Write the ``<Prompt>`` text on a ``<ReportParameter>``.

    Empty string ``""`` clears the ``<Prompt>`` element entirely. Pass a
    single space ``" "`` for blank-but-present.
    """
    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)

    existing = find_child(parameter, "Prompt")
    if prompt == "":
        # Clear: remove the element entirely if present.
        if existing is not None:
            parameter.remove(existing)
    else:
        new = etree.Element(q("Prompt"))
        new.text = prompt
        _set_or_replace_in_order(parameter, new)

    doc.save()
    return {"parameter": name, "prompt": prompt if prompt != "" else None}


def _default_value_literals(parameter: etree._Element) -> list[str]:
    """Return literal <Value> entries from <DefaultValue>/<Values>; empty list
    if defaults come from a query or are absent."""
    default_value = find_child(parameter, "DefaultValue")
    if default_value is None:
        return []
    values_root = find_child(default_value, "Values")
    if values_root is None:
        return []
    out: list[str] = []
    for v in find_children(values_root, "Value"):
        if v.text is not None:
            out.append(v.text)
    return out


def _is_compatible_default(value: str, data_type: str) -> bool:
    """True if ``value`` is plausibly a literal of ``data_type``. Best-effort
    structural check — not a full parser. Expression strings (starting with
    ``=``) always pass; the user will see Report Builder's error at preview."""
    if value.startswith("="):
        return True
    if data_type == "Boolean":
        return value.lower() in {"true", "false"}
    if data_type == "Integer":
        try:
            int(value)
        except ValueError:
            return False
        return True
    if data_type == "Float":
        try:
            float(value)
        except ValueError:
            return False
        return True
    if data_type == "DateTime":
        # Accept anything matching YYYY-MM-DD prefix; full ISO not required.
        return len(value) >= 10 and value[4] == "-" and value[7] == "-"
    # String: anything is fine.
    return True


def set_parameter_type(
    path: str,
    name: str,
    type: str,  # noqa: A002 - tool-facing param name; "type" is intentional
) -> dict[str, Any]:
    """Set ``<DataType>`` on a ``<ReportParameter>``.

    ``type`` ∈ {Boolean, DateTime, Integer, Float, String}. Rejects the
    change with :class:`ValueError` if any existing literal default value
    is incompatible with the new type — fix the defaults first or call
    :func:`set_parameter_default_values`.
    """
    if type not in _VALID_DATA_TYPES:
        raise ValueError(f"type {type!r} not valid; expected one of {list(_VALID_DATA_TYPES)}")

    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)

    incompatible = [
        v for v in _default_value_literals(parameter) if not _is_compatible_default(v, type)
    ]
    if incompatible:
        raise ValueError(
            f"existing default value(s) {incompatible!r} incompatible with type "
            f"{type!r}; clear or replace them first via set_parameter_default_values."
        )

    new = etree.Element(q("DataType"))
    new.text = type
    _set_or_replace_in_order(parameter, new)
    doc.save()
    return {"parameter": name, "type": type}


def _all_parameter_names(doc: RDLDocument) -> list[str]:
    return [
        p.get("Name")
        for p in doc.root.iter(f"{{{doc.root.nsmap.get(None, '')}}}ReportParameter")
        if p.get("Name") is not None
    ]


def add_parameter(
    path: str,
    name: str,
    type: str,  # noqa: A002
    prompt: Optional[str] = None,
    allow_null: Optional[bool] = None,
    allow_blank: Optional[bool] = None,
    multi_value: Optional[bool] = None,
    hidden: Optional[bool] = None,
) -> dict[str, Any]:
    """Create a new ``<ReportParameter>`` with a minimal valid declaration.

    Appends to ``<ReportParameters>`` (creating the container if absent).
    Pair with :func:`set_parameter_available_values` /
    :func:`set_parameter_default_values` afterwards for value lists.
    """
    if type not in _VALID_DATA_TYPES:
        raise ValueError(f"type {type!r} not valid; expected one of {list(_VALID_DATA_TYPES)}")

    doc = RDLDocument.open(path)
    root = doc.root
    rdl_ns = root.nsmap.get(None) or ""
    # Check uniqueness.
    existing = [
        p.get("Name")
        for p in root.iter(f"{{{rdl_ns}}}ReportParameter")
        if p.get("Name") is not None
    ]
    if name in existing:
        raise ValueError(f"parameter {name!r} already exists")

    # Find or create <ReportParameters> at the right place. Per RDL XSD it's
    # a child of <Report>. Position after <DataSets> if present, else
    # before <ReportSections>.
    params_root = root.find(f"{{{rdl_ns}}}ReportParameters")
    if params_root is None:
        params_root = etree.Element(q("ReportParameters"))
        # Insert at a sensible spot: before <ReportSections> if it exists.
        sections = root.find(f"{{{rdl_ns}}}ReportSections")
        if sections is not None:
            sections.addprevious(params_root)
        else:
            root.append(params_root)

    new_param = etree.SubElement(params_root, q("ReportParameter"), Name=name)
    dt = etree.SubElement(new_param, q("DataType"))
    dt.text = type
    if prompt is not None and prompt != "":
        pn = etree.SubElement(new_param, q("Prompt"))
        pn.text = prompt
    # Boolean flags — only emit if the user supplied an explicit value.
    flag_pairs = (
        ("Nullable", allow_null),
        ("AllowBlank", allow_blank),
        ("MultiValue", multi_value),
        ("Hidden", hidden),
    )
    for local, value in flag_pairs:
        if value is None:
            continue
        node = etree.Element(q(local))
        node.text = "true" if value else "false"
        _set_or_replace_in_order(new_param, node)

    doc.save()
    return {
        "parameter": name,
        "type": type,
        "prompt": prompt,
    }


def _scan_parameter_references(doc: RDLDocument, name: str) -> list[str]:
    """Walk every text-bearing element looking for `Parameters!<name>.Value`.

    Returns a list of human-readable locator strings describing where the
    references live. Used by ``remove_parameter`` to refuse a destructive
    delete and by ``rename_parameter`` to drive the rewrite.
    """
    needle_value = f"Parameters!{name}.Value"
    needle_label = f"Parameters!{name}.Label"
    locators: list[str] = []
    for el in doc.root.iter():
        if not isinstance(el.tag, str):
            continue
        # Check the element's text and tail for matches.
        text = el.text
        if text and (needle_value in text or needle_label in text):
            tag = etree.QName(el).localname
            # Walk up to find a stable ancestor identifier.
            ancestor = el
            label = tag
            while ancestor is not None:
                aname = ancestor.get("Name")
                if aname:
                    label = f"{etree.QName(ancestor).localname}[Name={aname!r}]/.../<{tag}>"
                    break
                ancestor = ancestor.getparent()
            locators.append(label)
    return locators


def remove_parameter(
    path: str,
    name: str,
    force: bool = False,
) -> dict[str, Any]:
    """Remove a ``<ReportParameter>`` by name.

    By default refuses if the parameter is still referenced anywhere in
    the report (any element text containing ``Parameters!<name>.Value``
    or ``.Label``) — returns the offending locators in the error message.
    Pass ``force=True`` to remove anyway.
    """
    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)

    if not force:
        locators = _scan_parameter_references(doc, name)
        if locators:
            raise ValueError(
                f"parameter {name!r} is still referenced from "
                f"{len(locators)} location(s): {locators[:5]}"
                + (" (more elided)" if len(locators) > 5 else "")
                + ". Pass force=True to remove anyway."
            )

    parent = parameter.getparent()
    parent.remove(parameter)
    # Tidy up an empty <ReportParameters>.
    if len(list(parent)) == 0:
        gp = parent.getparent()
        if gp is not None:
            gp.remove(parent)

    doc.save()
    return {"parameter": name, "removed": True, "force": force}


def rename_parameter(
    path: str,
    old_name: str,
    new_name: str,
) -> dict[str, Any]:
    """Rename a ``<ReportParameter>`` and rewrite every reference.

    Rewrites every textual occurrence of ``Parameters!<old_name>.Value``
    and ``Parameters!<old_name>.Label`` (case-sensitive) across:

    * Textbox values, visibility expressions, group / sort / filter
      expressions
    * Dataset query parameters' ``<Value>`` text
    * ``<DataSetReference>`` available/default-values lookups (covered
      because they live in textual expression form)

    Errors if ``new_name`` already exists, or if ``new_name == old_name``.

    Atomic: collects every match first, then commits the rewrite. If any
    match fails to rewrite (e.g. malformed mid-rewrite), no changes are
    saved.
    """
    if new_name == old_name:
        raise ValueError("new_name and old_name are identical; nothing to rename.")

    doc = RDLDocument.open(path)
    rdl_ns = doc.root.nsmap.get(None) or ""

    # Reject collision.
    existing = [
        p.get("Name")
        for p in doc.root.iter(f"{{{rdl_ns}}}ReportParameter")
        if p.get("Name") is not None
    ]
    if new_name in existing:
        raise ValueError(f"parameter {new_name!r} already exists; cannot rename onto it.")

    # Locate the original (raises if absent).
    parameter = resolve_parameter(doc, old_name)

    # Stage rewrites: walk every element, build a list of (element, attr_or_text, new_value).
    needles = (
        f"Parameters!{old_name}.Value",
        f"Parameters!{old_name}.Label",
    )
    replacements = (
        f"Parameters!{new_name}.Value",
        f"Parameters!{new_name}.Label",
    )

    rewrites: list[tuple[etree._Element, str]] = []  # (element, new_text)
    for el in doc.root.iter():
        if not isinstance(el.tag, str):
            continue
        text = el.text
        if not text:
            continue
        new_text = text
        for old, new in zip(needles, replacements):
            if old in new_text:
                new_text = new_text.replace(old, new)
        if new_text != text:
            rewrites.append((el, new_text))

    # Commit: rename the parameter declaration, apply all text rewrites.
    parameter.set("Name", new_name)
    for el, new_text in rewrites:
        el.text = new_text

    doc.save()
    return {
        "old_name": old_name,
        "new_name": new_name,
        "references_rewritten": len(rewrites),
    }


__all__ = [
    "add_parameter",
    "remove_parameter",
    "rename_parameter",
    "set_parameter_available_values",
    "set_parameter_default_values",
    "set_parameter_prompt",
    "set_parameter_type",
    "update_parameter_advanced",
]
