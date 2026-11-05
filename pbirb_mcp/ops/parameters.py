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
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import (
    ElementNotFoundError,
    resolve_dataset,
    resolve_parameter,
)
from pbirb_mcp.core.mutation import put_child, remove_child_if_present, set_text_if_different
from pbirb_mcp.core.names import validate_identifier
from pbirb_mcp.core.xpath import find_child, find_children, q
from pbirb_mcp.ops import expr_walk

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


def _set_or_replace_in_order(parent: etree._Element, new_child: etree._Element) -> bool:
    """Replace/insert ``new_child`` in XSD order; ``True`` iff changed."""
    return put_child(parent, new_child, order=_PARAMETER_CHILD_ORDER)


# ---- shared validation ----------------------------------------------------


def _check_source(source: str) -> None:
    if source not in _VALID_SOURCES:
        raise ValueError(f"source must be one of {_VALID_SOURCES!r}; got {source!r}")


def _validate_query_args(
    query_dataset: Optional[str],
    query_value_field: Optional[str],
    doc: RDLDocument,
    query_label_field: Optional[str] = None,
) -> None:
    if not query_dataset or not query_value_field:
        raise ValueError("source='query' requires both query_dataset and query_value_field")
    # Raises ElementNotFoundError when the dataset doesn't exist.
    dataset = resolve_dataset(doc, query_dataset)
    # ValueField / LabelField hold bare field names that Report Builder
    # resolves against the dataset's <Fields> when it populates the
    # parameter (#93; lint: dangling-dataset-reference-field).
    fields_root = find_child(dataset, "Fields")
    fields = (
        [f.get("Name") for f in find_children(fields_root, "Field")]
        if fields_root is not None
        else []
    )
    missing = [
        f"{arg}={value!r}"
        for arg, value in (
            ("query_value_field", query_value_field),
            ("query_label_field", query_label_field),
        )
        if value is not None and value not in fields
    ]
    if missing:
        raise ValueError(
            f"{', '.join(missing)} not a field of dataset {query_dataset!r} "
            f"(fields: {', '.join(fields) or 'none declared'}); add the field "
            "(add_dataset_field / refresh_dataset_fields) or pick an existing one."
        )


# ---- parameter state (#93) -------------------------------------------------


def _flag(parameter: etree._Element, local: str) -> bool:
    node = find_child(parameter, local)
    return node is not None and (node.text or "").strip().lower() == "true"


def _static_valid_values(parameter: etree._Element) -> list[str]:
    """Literal ``<Value>`` texts of a static ``<ValidValues>`` list."""
    valid_values = find_child(parameter, "ValidValues")
    pvs = find_child(valid_values, "ParameterValues") if valid_values is not None else None
    if pvs is None:
        return []
    out: list[str] = []
    for pv in find_children(pvs, "ParameterValue"):
        value = find_child(pv, "Value")
        if value is not None and value.text is not None:
            out.append(value.text)
    return out


def parameter_state_problems(parameter: etree._Element) -> list[tuple[str, str]]:
    """Combination / type problems of one ``<ReportParameter>`` (#93).

    Returns ``[(severity, message)]``: ``"error"`` for a state Report
    Builder rejects (or that fails every run), ``"warning"`` for one that
    fails only when no value is passed. Shared by the parameter writers
    (which refuse new errors) and the ``parameter-state`` lint rule (which
    reports legacy files).
    """
    name = parameter.get("Name")
    dt_node = find_child(parameter, "DataType")
    data_type = (dt_node.text or "").strip() if dt_node is not None else "String"
    multi = _flag(parameter, "MultiValue")
    problems: list[tuple[str, str]] = []
    if multi and _flag(parameter, "Nullable"):
        problems.append(
            (
                "error",
                f"parameter {name!r} is MultiValue and Nullable; a multi-value "
                "parameter can't allow null (set allow_null=false or multi_value=false)",
            )
        )
    if multi and data_type == "Boolean":
        problems.append(
            (
                "error",
                f"parameter {name!r} is a multi-value Boolean; Boolean parameters "
                "can't be multi-value (use multi_value=false or another type)",
            )
        )
    default_value = find_child(parameter, "DefaultValue")
    values_root = find_child(default_value, "Values") if default_value is not None else None
    if values_root is not None and not multi:
        count = len(find_children(values_root, "Value"))
        if count > 1:
            problems.append(
                (
                    "error",
                    f"parameter {name!r} has {count} default values but isn't "
                    "MultiValue; give one default or set multi_value=true",
                )
            )
    if data_type in _VALID_DATA_TYPES:
        for label, values in (
            ("default value(s)", _default_value_literals(parameter)),
            ("static valid value(s)", _static_valid_values(parameter)),
        ):
            bad = [v for v in values if not _is_compatible_default(v, data_type)]
            if bad:
                problems.append(
                    (
                        "error",
                        f"parameter {name!r}: {label} {bad!r} incompatible with type {data_type!r}",
                    )
                )
    if _flag(parameter, "Hidden") and default_value is None and not _flag(parameter, "Nullable"):
        problems.append(
            (
                "warning",
                f"parameter {name!r} is hidden, not nullable and has no default; "
                "the report can't run unless a value is passed (set a default or "
                "allow_null=true)",
            )
        )
    return problems


def _check_parameter_state(parameter: etree._Element, before: list[tuple[str, str]]) -> list[str]:
    """Refuse a mutation that introduced a parameter-state error (#93).

    ``before`` is :func:`parameter_state_problems` from before the edit, so
    a problem a legacy file already had doesn't block an unrelated edit —
    it comes back as a warning instead. Returns the warnings to report.
    """
    after = parameter_state_problems(parameter)
    new_errors = [m for sev, m in after if sev == "error" and (sev, m) not in before]
    if new_errors:
        raise ValueError("; ".join(new_errors))
    return [m for sev, m in after if sev == "warning" or (sev, m) in before]


def _with_warnings(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if warnings:
        result["warnings"] = warnings
    return result


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
        etree.SubElement(pv, q("Value")).text = encode_text(value_text)
        etree.SubElement(pv, q("Label")).text = encode_text(label_text)
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
    before = parameter_state_problems(parameter)

    if source == "static" and not static_values:
        # Clear: drop the existing <ValidValues> element if present.
        changed = remove_child_if_present(parameter, "ValidValues")
        warnings = _check_parameter_state(parameter, before)
        if changed:
            doc.save()
        return _with_warnings(
            {"parameter": name, "source": source, "cleared": True, "changed": changed}, warnings
        )

    valid_values = etree.Element(q("ValidValues"))
    if source == "static":
        valid_values.append(_build_static_parameter_values(static_values))
    else:
        _validate_query_args(query_dataset, query_value_field, doc, query_label_field)
        valid_values.append(
            _build_dataset_reference(query_dataset, query_value_field, query_label_field)
        )

    changed = _set_or_replace_in_order(parameter, valid_values)
    warnings = _check_parameter_state(parameter, before)
    if changed:
        doc.save()
    return _with_warnings(
        {"parameter": name, "source": source, "cleared": False, "changed": changed}, warnings
    )


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
    before = parameter_state_problems(parameter)

    if source == "static" and not static_values:
        changed = remove_child_if_present(parameter, "DefaultValue")
        warnings = _check_parameter_state(parameter, before)
        if changed:
            doc.save()
        return _with_warnings(
            {"parameter": name, "source": source, "cleared": True, "changed": changed}, warnings
        )

    default_value = etree.Element(q("DefaultValue"))
    if source == "static":
        values_root = etree.SubElement(default_value, q("Values"))
        for value_text in static_values:
            v = etree.SubElement(values_root, q("Value"))
            v.text = encode_text(value_text)
    else:
        _validate_query_args(query_dataset, query_value_field, doc)
        # DefaultValue's DataSetReference has no LabelField — defaults are
        # values, not display strings.
        default_value.append(
            _build_dataset_reference(query_dataset, query_value_field, label_field=None)
        )

    changed = _set_or_replace_in_order(parameter, default_value)
    warnings = _check_parameter_state(parameter, before)
    if changed:
        doc.save()
    return _with_warnings(
        {"parameter": name, "source": source, "cleared": False, "changed": changed}, warnings
    )


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
    before = parameter_state_problems(parameter)

    changed: list[str] = []
    for local, value in flags:
        if value is None:
            continue
        new_node = etree.Element(q(local))
        new_node.text = "true" if value else "false"
        if _set_or_replace_in_order(parameter, new_node):
            changed.append(local)

    warnings = _check_parameter_state(parameter, before)
    if changed:
        doc.save()
    return _with_warnings({"parameter": name, "changed": changed}, warnings)


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

    if prompt == "":
        # Clear: remove the element entirely if present.
        changed = remove_child_if_present(parameter, "Prompt")
    else:
        new = etree.Element(q("Prompt"))
        new.text = encode_text(prompt)
        changed = _set_or_replace_in_order(parameter, new)

    if changed:
        doc.save()
    return {"parameter": name, "prompt": prompt if prompt != "" else None, "changed": changed}


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
    or static valid value is incompatible with the new type, or the new
    type is Boolean on a multi-value parameter (#93) — fix those first via
    :func:`set_parameter_default_values` /
    :func:`set_parameter_available_values`. A no-op (the type it already
    has) returns ``changed: false`` without checking anything.
    """
    if type not in _VALID_DATA_TYPES:
        raise ValueError(f"type {type!r} not valid; expected one of {list(_VALID_DATA_TYPES)}")

    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)
    current = find_child(parameter, "DataType")
    if current is not None and (current.text or "").strip() == type:
        return {"parameter": name, "type": type, "changed": False}
    before = parameter_state_problems(parameter)

    new = etree.Element(q("DataType"))
    new.text = type
    changed = _set_or_replace_in_order(parameter, new)
    try:
        warnings = _check_parameter_state(parameter, before)
    except ValueError as exc:
        raise ValueError(
            f"{exc}; clear or replace them first via set_parameter_default_values / "
            "set_parameter_available_values."
        ) from None
    if changed:
        doc.save()
    return _with_warnings({"parameter": name, "type": type, "changed": changed}, warnings)


def _all_parameter_names(doc: RDLDocument) -> list[str]:
    return [
        p.get("Name")
        for p in doc.root.iter(f"{{{doc.root.nsmap.get(None, '')}}}ReportParameter")
        if p.get("Name") is not None
    ]


# ---- ReportParametersLayout sync ------------------------------------------


def _grid_layout_definition(
    layout_root: etree._Element,
) -> Optional[etree._Element]:
    """Return ``<GridLayoutDefinition>`` if present (modern RDL form)."""
    return find_child(layout_root, "GridLayoutDefinition")


def _cell_definitions(grid: etree._Element) -> Optional[etree._Element]:
    return find_child(grid, "CellDefinitions")


def _cell_parameter_name(cell: etree._Element) -> Optional[str]:
    pn = find_child(cell, "ParameterName")
    return pn.text if pn is not None and pn.text else None


def _build_cell_definition(
    parameter_name: str, row_index: int, column_index: int
) -> etree._Element:
    """Build a <CellDefinition> with the canonical child order."""
    cell = etree.Element(q("CellDefinition"))
    etree.SubElement(cell, q("ColumnIndex")).text = str(column_index)
    etree.SubElement(cell, q("RowIndex")).text = str(row_index)
    etree.SubElement(cell, q("ParameterName")).text = parameter_name
    return cell


def _set_grid_dimension(grid: etree._Element, local: str, value: int) -> None:
    """Write or update <NumberOfColumns>/<NumberOfRows> as a child of grid.
    Per RDL XSD, NumberOfColumns and NumberOfRows precede CellDefinitions."""
    existing = find_child(grid, local)
    if existing is not None:
        existing.text = str(value)
        return
    new = etree.Element(q(local))
    new.text = str(value)
    cells = find_child(grid, "CellDefinitions")
    if cells is not None:
        cells.addprevious(new)
    else:
        grid.append(new)


def _int_text(node: Optional[etree._Element]) -> Optional[int]:
    if node is None or not node.text:
        return None
    try:
        return int(node.text.strip())
    except ValueError:
        return None


def _cell_slot(cell: etree._Element) -> tuple[Optional[int], Optional[int]]:
    """``(row, col)`` of a ``<CellDefinition>``; ``None`` for a missing or
    non-integer index."""
    return _int_text(find_child(cell, "RowIndex")), _int_text(find_child(cell, "ColumnIndex"))


def _layout_grid(doc: RDLDocument) -> Optional[etree._Element]:
    layout_root = find_child(doc.root, "ReportParametersLayout")
    if layout_root is None:
        return None
    return _grid_layout_definition(layout_root)


_CELL_CHILD_ORDER = ("ColumnIndex", "RowIndex", "ParameterName")


def layout_grid_problems(grid: etree._Element) -> list[dict[str, Any]]:
    """Integrity problems of a ``<GridLayoutDefinition>`` (#74), in cell order.

    Each is ``{problem, parameter, row, column, message}`` with ``problem``
    one of ``invalid-index`` (a missing, non-integer or negative
    RowIndex/ColumnIndex), ``column-out-of-range`` (ColumnIndex ≥
    NumberOfColumns), ``row-out-of-range`` (RowIndex ≥ NumberOfRows) and
    ``duplicate-slot`` (two cells in one (row, column)). Shared by
    :func:`sync_parameter_layout` and lint's ``parameter-layout-out-of-sync``.
    """
    ncols = _int_text(find_child(grid, "NumberOfColumns"))
    nrows = _int_text(find_child(grid, "NumberOfRows"))
    cells_root = _cell_definitions(grid)
    if cells_root is None:
        return []
    problems: list[dict[str, Any]] = []
    seen: dict[tuple[int, int], Optional[str]] = {}

    def add(problem: str, name, row, col, message: str) -> None:
        problems.append(
            {"problem": problem, "parameter": name, "row": row, "column": col, "message": message}
        )

    for cell in find_children(cells_root, "CellDefinition"):
        name = _cell_parameter_name(cell)
        row, col = _cell_slot(cell)
        if row is None or col is None or row < 0 or col < 0:
            add(
                "invalid-index",
                name,
                row,
                col,
                f"the cell for {name!r} has no valid RowIndex / ColumnIndex",
            )
            continue
        if ncols is not None and col >= ncols:
            add(
                "column-out-of-range",
                name,
                row,
                col,
                f"the cell for {name!r} is in column {col}, but NumberOfColumns is {ncols}",
            )
        if nrows is not None and row >= nrows:
            add(
                "row-out-of-range",
                name,
                row,
                col,
                f"the cell for {name!r} is in row {row}, but NumberOfRows is {nrows}",
            )
        if (row, col) in seen:
            add(
                "duplicate-slot",
                name,
                row,
                col,
                f"the cells for {seen[(row, col)]!r} and {name!r} share row {row}, column {col}",
            )
        else:
            seen[(row, col)] = name
    return problems


def _sync_layout(doc: RDLDocument) -> dict[str, Any]:
    """Bring the parameter-pane grid in line with ``<ReportParameters>``
    in ``doc`` (no save); see :func:`sync_parameter_layout`."""
    result: dict[str, Any] = {"added": [], "removed": [], "relocated": [], "changed": False}
    grid = _layout_grid(doc)
    if grid is None:
        return result
    cells_root = _cell_definitions(grid)
    declared = _all_parameter_names(doc)
    declared_set = set(declared)
    changed = False

    # Drop cells for undeclared parameters, and second cells for one parameter.
    present: set[str] = set()
    if cells_root is not None:
        for cell in list(find_children(cells_root, "CellDefinition")):
            pname = _cell_parameter_name(cell)
            if pname is None:
                continue
            if pname not in declared_set or pname in present:
                cells_root.remove(cell)
                result["removed"].append(pname)
                changed = True
            else:
                present.add(pname)

    cols_node = find_child(grid, "NumberOfColumns")
    ncols = _int_text(cols_node) or 0
    if ncols <= 0:
        ncols = 4  # Report Builder default

    # Keep every cell in a valid, free slot; collect the rest for relocation.
    cells = find_children(cells_root, "CellDefinition") if cells_root is not None else []
    occupied: set[tuple[int, int]] = set()
    misplaced: list[tuple[etree._Element, str]] = []
    for cell in cells:
        row, col = _cell_slot(cell)
        if row is None or col is None or row < 0 or col < 0:
            misplaced.append((cell, "invalid index"))
        elif col >= ncols:
            misplaced.append((cell, f"column {col} is outside NumberOfColumns={ncols}"))
        elif (row, col) in occupied:
            misplaced.append((cell, f"row {row}, column {col} is taken"))
        else:
            occupied.add((row, col))

    # New positions continue after the last occupied slot, row-major.
    if occupied:
        next_row = max(r for r, _c in occupied)
        next_col = max(c for r, c in occupied if r == next_row) + 1
    else:
        next_row, next_col = 0, 0

    def take_slot() -> tuple[int, int]:
        nonlocal next_row, next_col
        if next_col >= ncols:
            next_row += 1
            next_col = 0
        slot = (next_row, next_col)
        next_col += 1
        return slot

    for cell, reason in misplaced:
        old = list(_cell_slot(cell))
        row, col = take_slot()
        set_text_if_different(cell, "ColumnIndex", str(col), order=_CELL_CHILD_ORDER)
        set_text_if_different(cell, "RowIndex", str(row), order=_CELL_CHILD_ORDER)
        result["relocated"].append(
            {
                "parameter": _cell_parameter_name(cell),
                "from": old,
                "to": [row, col],
                "reason": reason,
            }
        )
        occupied.add((row, col))
        changed = True

    for pname in declared:
        if pname in present:
            continue
        if cells_root is None:
            cells_root = etree.SubElement(grid, q("CellDefinitions"))
        row, col = take_slot()
        cells_root.append(_build_cell_definition(pname, row, col))
        result["added"].append(pname)
        occupied.add((row, col))
        changed = True

    # Grow NumberOfRows to fit (never shrink here); write NumberOfColumns if absent.
    needed_rows = max((r for r, _c in occupied), default=-1) + 1
    if needed_rows > (_int_text(find_child(grid, "NumberOfRows")) or 0):
        _set_grid_dimension(grid, "NumberOfRows", needed_rows)
        changed = True
    if cols_node is None and occupied:
        _set_grid_dimension(grid, "NumberOfColumns", ncols)
        changed = True

    result["changed"] = changed
    return result


def sync_parameter_layout(path: str) -> dict[str, Any]:
    """Repair ``<ReportParametersLayout>/<GridLayoutDefinition>`` against
    ``<ReportParameters>``.

    * Drops cells whose ``<ParameterName>`` no longer exists (and a second
      cell for the same parameter).
    * Moves cells that sit outside the grid (ColumnIndex ≥
      NumberOfColumns, a missing / negative index) or share a slot with an
      earlier cell (#74) to free slots.
    * Appends cells for parameters that have none.

    Moved and new cells go to the next free ``(row, col)`` after the last
    occupied slot, wrapping at NumberOfColumns. NumberOfRows grows to fit;
    NumberOfColumns is written when absent.

    Returns ``{added, removed, relocated: [{parameter, from, to, reason}],
    grid_problems, changed}`` — ``grid_problems`` are the
    :func:`layout_grid_problems` found before the repair. A call with
    nothing to do changes nothing and doesn't touch the file. No-op for
    reports without a layout block.
    """
    doc = RDLDocument.open(path)
    grid = _layout_grid(doc)
    problems = layout_grid_problems(grid) if grid is not None else []
    result = _sync_layout(doc)
    if result["changed"]:
        doc.save()
    return {**result, "grid_problems": problems}


def _sync_parameter_layout_in_doc(doc: RDLDocument) -> tuple[list[str], list[str]]:
    """In-process variant used by add/remove helpers that save themselves.
    Returns ``(added, removed)``."""
    result = _sync_layout(doc)
    return result["added"], result["removed"]


def _reflow_layout_in_doc(doc: RDLDocument, order: list[str]) -> bool:
    """Rewrite every cell's position row-major (wrapping at NumberOfColumns)
    in ``order`` — parameters not in ``order`` follow in their current slot
    order — and set NumberOfRows to the rows used. ``True`` iff changed."""
    grid = _layout_grid(doc)
    if grid is None:
        return False
    cells_root = _cell_definitions(grid)
    if cells_root is None:
        return False
    cells = find_children(cells_root, "CellDefinition")
    if not cells:
        return False
    ncols = _int_text(find_child(grid, "NumberOfColumns")) or 0
    changed = False
    if ncols <= 0:
        ncols = 4
        _set_grid_dimension(grid, "NumberOfColumns", ncols)
        changed = True
    rank = {n: i for i, n in enumerate(order)}

    def key(item: tuple[int, etree._Element]) -> tuple:
        i, cell = item
        row, col = _cell_slot(cell)
        name = _cell_parameter_name(cell)
        slot = (row if row is not None else 1 << 30, col if col is not None else 1 << 30)
        return (rank.get(name, len(rank)), slot, i)

    for k, (_i, cell) in enumerate(sorted(enumerate(cells), key=key)):
        changed |= set_text_if_different(
            cell, "ColumnIndex", str(k % ncols), order=_CELL_CHILD_ORDER
        )
        changed |= set_text_if_different(cell, "RowIndex", str(k // ncols), order=_CELL_CHILD_ORDER)
    rows = (len(cells) + ncols - 1) // ncols
    if _int_text(find_child(grid, "NumberOfRows")) != rows:
        _set_grid_dimension(grid, "NumberOfRows", rows)
        changed = True
    return changed


def _slot_order(doc: RDLDocument) -> list[str]:
    """Parameter names of the layout cells in row-major slot order."""
    grid = _layout_grid(doc)
    cells_root = _cell_definitions(grid) if grid is not None else None
    if cells_root is None:
        return []
    cells = find_children(cells_root, "CellDefinition")

    def key(item: tuple[int, etree._Element]) -> tuple:
        i, cell = item
        row, col = _cell_slot(cell)
        return (row if row is not None else 1 << 30, col if col is not None else 1 << 30, i)

    return [
        n
        for _i, cell in sorted(enumerate(cells), key=key)
        if (n := _cell_parameter_name(cell)) is not None
    ]


def _rename_parameter_in_layout(doc: RDLDocument, old_name: str, new_name: str) -> int:
    """Rewrite every ``<CellDefinition>/<ParameterName>`` matching ``old_name``
    to ``new_name``. Returns the count of cells touched."""
    layout_root = find_child(doc.root, "ReportParametersLayout")
    if layout_root is None:
        return 0
    grid = _grid_layout_definition(layout_root)
    if grid is None:
        return 0
    cells_root = _cell_definitions(grid)
    if cells_root is None:
        return 0
    rewritten = 0
    for cell in find_children(cells_root, "CellDefinition"):
        pn = find_child(cell, "ParameterName")
        if pn is not None and pn.text == old_name:
            pn.text = new_name
            rewritten += 1
    return rewritten


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
    validate_identifier(name, "parameter")
    if type not in _VALID_DATA_TYPES:
        raise ValueError(f"type {type!r} not valid; expected one of {list(_VALID_DATA_TYPES)}")

    doc = RDLDocument.open(path)
    root = doc.root
    rdl_ns = root.nsmap.get(None) or ""
    # Check uniqueness (parameters are their own namespace).
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
        pn.text = encode_text(prompt)
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

    warnings = _check_parameter_state(new_param, [])

    layout_added, _ = _sync_parameter_layout_in_doc(doc)

    doc.save()
    return _with_warnings(
        {
            "parameter": name,
            "type": type,
            "prompt": prompt,
            "layout_synced": name in layout_added,
        },
        warnings,
    )


def _reference_locator(el: etree._Element) -> str:
    tag = etree.QName(el).localname
    ancestor = el
    while ancestor is not None:
        aname = ancestor.get("Name")
        if aname:
            return f"{etree.QName(ancestor).localname}[Name={aname!r}]/.../<{tag}>"
        ancestor = ancestor.getparent()
    return tag


def _scan_parameter_references(
    doc: RDLDocument, name: str, exclude: Optional[etree._Element] = None
) -> list[str]:
    """Locators of every element referencing parameter ``name``, in any
    form :func:`expr_walk.parameter_references` knows:
    ``Parameters!<name>`` with any member (``.Value``, ``.Label``,
    ``.Count``, ``.IsMultiValue`` …), ``Parameters("<name>")``, and
    ``Report.Parameters…`` in the ``<Code>`` block — wherever an
    expression can live (textboxes, query-parameter values, filters,
    defaults / valid values, subreport and drillthrough parameters …).
    Names inside string literals and comments don't count. References
    inside ``exclude`` (the parameter being removed) are skipped.
    """
    locators: list[str] = []
    for el, _text, _tokens, _refs in expr_walk.iter_parameter_references(doc.root, name):
        if exclude is not None and (el is exclude or exclude in el.iterancestors()):
            continue
        locators.append(_reference_locator(el))
    return locators


def remove_parameter(
    path: str,
    name: str,
    force: bool = False,
    compact_layout: bool = False,
) -> dict[str, Any]:
    """Remove a ``<ReportParameter>`` by name.

    By default refuses if the parameter is still referenced anywhere in
    the report — any reference form :func:`_scan_parameter_references`
    finds (``Parameters!<name>.Value`` / ``.Label`` / ``.Count`` /
    ``.IsMultiValue``, ``Parameters("<name>")`` …) outside the parameter's
    own declaration — and lists the offending locators in the error
    message. Pass ``force=True`` to remove anyway.

    Its parameter-pane cell is dropped, leaving a hole; with
    ``compact_layout=True`` the remaining cells are re-packed row-major in
    their current slot order and NumberOfRows shrinks to fit.
    """
    doc = RDLDocument.open(path)
    parameter = resolve_parameter(doc, name)

    if not force:
        locators = _scan_parameter_references(doc, name, exclude=parameter)
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

    _added, layout_removed = _sync_parameter_layout_in_doc(doc)
    compacted = _reflow_layout_in_doc(doc, _slot_order(doc)) if compact_layout else False

    doc.save()
    return {
        "parameter": name,
        "removed": True,
        "force": force,
        "layout_synced": name in layout_removed,
        "layout_compacted": compacted,
    }


def rename_parameter(
    path: str,
    old_name: str,
    new_name: str,
) -> dict[str, Any]:
    """Rename a ``<ReportParameter>`` and rewrite every reference.

    Rewrites every reference to ``old_name`` (case-sensitive) in every
    form :func:`_scan_parameter_references` finds —
    ``Parameters!<old_name>`` with any member (``.Value``, ``.Label``,
    ``.Count``, ``.IsMultiValue`` …), ``Parameters("<old_name>")``, and
    ``Report.Parameters…`` in the ``<Code>`` block — in every expression:
    textbox values, visibility, group / sort / filter expressions, chart
    and style properties, dataset query-parameter values (including the
    lookup datasets behind available / default values), other
    parameters' defaults, subreport / drillthrough parameters. Only the
    name token changes; names inside string literals and comments are
    left alone. Parameter-pane layout cells are renamed too.
    ``<DataSetReference>`` holds dataset and field names, not parameter
    references, so it is not touched. ``<QueryParameter Name=…>`` is the
    query-side name and is not renamed either.

    Errors if ``new_name`` already exists, or if ``new_name == old_name``.

    Atomic: collects every match first, then commits the rewrite.
    ``references_rewritten`` counts the elements rewritten.
    """
    if new_name == old_name:
        raise ValueError("new_name and old_name are identical; nothing to rename.")
    validate_identifier(new_name, "parameter")

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

    # Stage rewrites: (element, new_text) for every element with a reference.
    rewrites: list[tuple[etree._Element, str]] = [
        (el, expr_walk.rename_parameter_references(text, tokens, refs, new_name))
        for el, text, tokens, refs in expr_walk.iter_parameter_references(doc.root, old_name)
    ]

    # Commit: rename the parameter declaration, apply all text rewrites.
    parameter.set("Name", new_name)
    for el, new_text in rewrites:
        el.text = new_text

    layout_cells_rewritten = _rename_parameter_in_layout(doc, old_name, new_name)

    doc.save()
    return {
        "old_name": old_name,
        "new_name": new_name,
        "references_rewritten": len(rewrites),
        "layout_cells_rewritten": layout_cells_rewritten,
    }


# ---- reorder_parameters --------------------------------------------------


def _forward_dependency_error(order: list[str], doc: RDLDocument) -> Optional[str]:
    """The refusal message when ``order`` puts a parameter before one it
    depends on (:func:`expr_walk.parameter_dependency_edges`), else ``None``.
    Self-dependencies don't depend on order and are left to lint."""
    edges = expr_walk.parameter_dependency_edges(doc.root)
    bad = [v for v in expr_walk.forward_dependencies(order, edges) if v[0] != v[1]]
    if not bad:
        return None
    pairs = "; ".join(f"{p} -> {d} (via its {via})" for p, d, via in bad)
    return (
        "this order puts parameters before ones they depend on, which Report Builder "
        f"rejects (forward dependency): {pairs}. Move each dependency earlier."
    )


def reorder_parameters(
    path: str,
    names: list[str],
    reflow_layout: bool = False,
) -> dict[str, Any]:
    """Reorder ``<ReportParameter>`` children inside ``<ReportParameters>``
    to match the supplied ``names`` list.

    ``names`` MUST be a permutation of every existing parameter name —
    no missing entries, no duplicates, no unknown names. The strict
    permutation check prevents accidental partial reorders that would
    silently lose a parameter from the report.

    Refuses (ValueError naming each ``dependant -> dependency`` pair) an
    order that declares a parameter before one its default value,
    available values, or their lookup dataset's query parameters /
    filters reference (#74) — Report Builder evaluates parameters in
    declaration order and rejects forward dependencies.

    Layout: the parameter pane's positions come from each cell's
    RowIndex / ColumnIndex, not from declaration order, so by default the
    pane looks the same afterwards (the ``<CellDefinition>`` elements are
    reordered in lockstep, but keep their slots). ``reflow_layout=True``
    rewrites the slots row-major in the new order (wrapping at
    NumberOfColumns, NumberOfRows set to fit), so the pane shows the new
    order.

    Returns ``{order, kind: 'ReportParameters', changed, layout_reflowed}``.
    ``changed`` is False (no save) when nothing changes.
    """
    if not isinstance(names, list):
        raise ValueError("names must be a list of parameter names")

    doc = RDLDocument.open(path)
    rdl_ns = doc.root.nsmap.get(None) or ""
    params_root = doc.root.find(f"{{{rdl_ns}}}ReportParameters")
    if params_root is None:
        raise ElementNotFoundError("report has no <ReportParameters> block — nothing to reorder")

    existing = [
        p.get("Name")
        for p in find_children(params_root, "ReportParameter")
        if p.get("Name") is not None
    ]

    # Strict permutation check.
    if len(names) != len(existing):
        raise ValueError(
            f"names must be a permutation of every existing parameter; "
            f"got {len(names)} entries but {len(existing)} parameters exist."
        )
    if len(set(names)) != len(names):
        from collections import Counter

        dupes = [n for n, c in Counter(names).items() if c > 1]
        raise ValueError(
            f"names contains duplicate(s): {dupes!r}. Each parameter name must appear exactly once."
        )
    extras = [n for n in names if n not in existing]
    missing = [n for n in existing if n not in names]
    if extras or missing:
        raise ValueError(
            "names must be a permutation of every existing parameter. "
            f"Unknown names: {extras!r}. Missing names: {missing!r}."
        )
    error = _forward_dependency_error(names, doc)
    if error:
        raise ValueError(error)

    changed = False
    if names != existing:
        # Reorder the <ReportParameter> children.
        by_name = {p.get("Name"): p for p in find_children(params_root, "ReportParameter")}
        for p in list(by_name.values()):
            params_root.remove(p)
        for name in names:
            params_root.append(by_name[name])
        # Keep the <CellDefinition> elements in declaration order too.
        _reorder_layout_cells_in_doc(doc, names)
        changed = True
    reflowed = _reflow_layout_in_doc(doc, names) if reflow_layout else False

    if changed or reflowed:
        doc.save()
    return {
        "order": list(names),
        "kind": "ReportParameters",
        "changed": changed or reflowed,
        "layout_reflowed": reflowed,
    }


def _reorder_layout_cells_in_doc(doc: RDLDocument, names: list[str]) -> int:
    """Reorder ``<CellDefinition>`` entries in
    ``<ReportParametersLayout>/<GridLayoutDefinition>/<CellDefinitions>``
    to match the supplied parameter ``names`` order.

    Cells whose ``ParameterName`` is in ``names`` are reordered;
    cells with no matching parameter (orphans, normally caught by
    sync_parameter_layout) keep their relative position appended at
    the end. Returns the number of cells touched.
    """
    layout_root = find_child(doc.root, "ReportParametersLayout")
    if layout_root is None:
        return 0
    grid = _grid_layout_definition(layout_root)
    if grid is None:
        return 0
    cells_root = _cell_definitions(grid)
    if cells_root is None:
        return 0

    # Map each name → the first cell whose ParameterName matches it.
    by_name: dict[str, etree._Element] = {}
    orphans: list[etree._Element] = []
    for cell in find_children(cells_root, "CellDefinition"):
        pname = _cell_parameter_name(cell)
        if pname is not None and pname in names and pname not in by_name:
            by_name[pname] = cell
        else:
            orphans.append(cell)

    # Detach all cells, then re-append in target order.
    for cell in list(cells_root):
        cells_root.remove(cell)

    touched = 0
    for name in names:
        cell = by_name.get(name)
        if cell is not None:
            cells_root.append(cell)
            touched += 1
    for orphan in orphans:
        cells_root.append(orphan)
    return touched


# ---- set_parameter_layout (Phase 6 commit 28) ---------------------------


def _ensure_report_parameters_layout(
    doc: RDLDocument,
) -> tuple[etree._Element, etree._Element]:
    """Find or create ``<ReportParametersLayout>`` and the inner
    ``<GridLayoutDefinition>``. Returns ``(layout, grid)``.

    ``<ReportParametersLayout>`` is a top-level child of ``<Report>``.
    Per the RDL XSD it sits between ``<EmbeddedImages>`` (or earlier
    sibilings) and ``<Variables>`` / ``<ReportSections>``. We insert
    before ``<ReportSections>`` when present, since that's the most
    universal anchor.
    """
    layout = find_child(doc.root, "ReportParametersLayout")
    if layout is None:
        layout = etree.Element(q("ReportParametersLayout"))
        sections = doc.root.find(f"{{{doc.root.nsmap.get(None) or ''}}}ReportSections")
        if sections is not None:
            sections.addprevious(layout)
        else:
            doc.root.append(layout)

    grid = find_child(layout, "GridLayoutDefinition")
    if grid is None:
        grid = etree.SubElement(layout, q("GridLayoutDefinition"))

    return layout, grid


def set_parameter_layout(
    path: str,
    rows: int,
    columns: int,
    parameter_order: list[str],
) -> dict[str, Any]:
    """Author the ``<ReportParametersLayout>/<GridLayoutDefinition>``
    grid explicitly.

    Writes ``<NumberOfRows>rows</NumberOfRows>`` and
    ``<NumberOfColumns>columns</NumberOfColumns>``, then rewrites
    ``<CellDefinitions>`` so each name in ``parameter_order`` lands at
    ``(row=index // columns, col=index % columns)`` in row-major order.

    Strict permutation check on ``parameter_order``: must contain every
    existing ``<ReportParameter>`` name exactly once. No missing,
    duplicate, or unknown entries. Mirrors :func:`reorder_parameters`'s
    permutation contract.

    ``rows * columns`` must be ≥ ``len(parameter_order)``. Smaller grids
    reject up front; larger grids leave trailing cells empty.

    Auto-creates ``<ReportParametersLayout>`` and
    ``<GridLayoutDefinition>`` when absent — Phase 0 commit 4's
    auto-sync only fills cells, never creates the layout block out of
    nothing. This tool is the explicit authoring path.

    Idempotent: same grid + order → ``{changed: false}``, no save.

    Returns ``{rows, columns, order, kind: 'ReportParametersLayout',
    changed: bool}``.

    Complements :func:`reorder_parameters` (declaration order) and
    :func:`sync_parameter_layout` (gap-filling against the existing
    grid).
    """
    if not isinstance(rows, int) or rows < 1:
        raise ValueError(f"rows must be a positive integer; got {rows!r}")
    if not isinstance(columns, int) or columns < 1:
        raise ValueError(f"columns must be a positive integer; got {columns!r}")
    if not isinstance(parameter_order, list):
        raise ValueError("parameter_order must be a list of parameter names")

    doc = RDLDocument.open(path)
    declared = _all_parameter_names(doc)

    if len(parameter_order) != len(declared):
        raise ValueError(
            f"parameter_order must be a permutation of every existing "
            f"parameter; got {len(parameter_order)} entries but "
            f"{len(declared)} parameters exist."
        )
    if len(set(parameter_order)) != len(parameter_order):
        from collections import Counter

        dupes = [n for n, c in Counter(parameter_order).items() if c > 1]
        raise ValueError(f"parameter_order contains duplicate(s): {dupes!r}")
    extras = [n for n in parameter_order if n not in declared]
    missing = [n for n in declared if n not in parameter_order]
    if extras or missing:
        raise ValueError(
            "parameter_order must be a permutation of every existing "
            f"parameter. Unknown names: {extras!r}. Missing names: "
            f"{missing!r}."
        )
    if rows * columns < len(parameter_order):
        raise ValueError(
            f"grid {rows}×{columns} = {rows * columns} cells is too small "
            f"for {len(parameter_order)} parameters; increase rows or columns."
        )

    layout, grid = _ensure_report_parameters_layout(doc)

    # Read current state for idempotency check.
    cols_node = find_child(grid, "NumberOfColumns")
    rows_node = find_child(grid, "NumberOfRows")
    cells_root = find_child(grid, "CellDefinitions")
    current_cols = int(cols_node.text) if cols_node is not None and cols_node.text else None
    current_rows = int(rows_node.text) if rows_node is not None and rows_node.text else None
    current_order: list[str] = []
    if cells_root is not None:
        for cell in find_children(cells_root, "CellDefinition"):
            pname = _cell_parameter_name(cell)
            if pname is not None:
                current_order.append(pname)

    if current_cols == columns and current_rows == rows and current_order == list(parameter_order):
        return {
            "rows": rows,
            "columns": columns,
            "order": list(parameter_order),
            "kind": "ReportParametersLayout",
            "changed": False,
        }

    # Write NumberOfColumns and NumberOfRows. Per RDL XSD child order,
    # both precede CellDefinitions; _set_grid_dimension handles the
    # right placement.
    _set_grid_dimension(grid, "NumberOfColumns", columns)
    _set_grid_dimension(grid, "NumberOfRows", rows)

    # Rewrite CellDefinitions from scratch — clear and re-append in
    # row-major order. Simpler than mutating in place; Phase 0's
    # auto-sync covers the gap-filling case if/when this drifts.
    if cells_root is None:
        cells_root = etree.SubElement(grid, q("CellDefinitions"))
    else:
        for cell in list(cells_root):
            cells_root.remove(cell)
    for index, pname in enumerate(parameter_order):
        row = index // columns
        col = index % columns
        cells_root.append(_build_cell_definition(pname, row, col))

    doc.save()
    return {
        "rows": rows,
        "columns": columns,
        "order": list(parameter_order),
        "kind": "ReportParametersLayout",
        "changed": True,
    }


__all__ = [
    "add_parameter",
    "layout_grid_problems",
    "parameter_state_problems",
    "remove_parameter",
    "rename_parameter",
    "reorder_parameters",
    "set_parameter_layout",
    "sync_parameter_layout",
    "set_parameter_available_values",
    "set_parameter_default_values",
    "set_parameter_prompt",
    "set_parameter_type",
    "update_parameter_advanced",
]
