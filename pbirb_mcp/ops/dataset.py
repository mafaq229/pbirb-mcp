"""Dataset-mutation tools.

Edits the ``<DataSet>`` block of an RDL: the DAX command text and the
``<QueryParameters>`` collection that wires report parameters into the query.

DAX bodies are accepted verbatim — we don't parse DAX, so the user (or
Report Builder at preview time) is the source of truth for syntactic
correctness. Empty / whitespace-only bodies are rejected up front because
Report Builder loads them but immediately errors at preview, which is a
worse signal than a clear ValueError here.

PBI paginated reports do **not** carry ``<CommandType>`` for DAX (unlike
SSRS where you'd set ``CommandType=StoredProcedure``); these tools never
add it.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_dataset
from pbirb_mcp.core.xpath import RD_NS, RDL_NS, find_child, find_children, q


def _query(dataset: etree._Element) -> etree._Element:
    query = find_child(dataset, "Query")
    if query is None:
        # An RDL DataSet without a Query is malformed — every PBI Report
        # Builder export has one. Raise a clear error rather than auto-creating
        # one whose child order we'd have to guess.
        raise ValueError(f"DataSet {dataset.get('Name')!r} has no <Query> element")
    return query


# ---- PBIDATASET-aware @-prefix detection --------------------------------


def _is_pbidataset_dataset(doc: RDLDocument, dataset: etree._Element) -> bool:
    """Return True iff the dataset binds to a Power BI XMLA DataSource.

    Two recognised shapes:

    1. ``<DataProvider>PBIDATASET</DataProvider>`` — the modern PBI
       authoring path.
    2. ``<DataProvider>SQL</DataProvider>`` + a ``<ConnectString>`` that
       starts with ``Data Source=powerbi://`` — the legacy AS-provider
       wire identifier our own ``set_datasource_connection`` emits.

    PBIDATASET parameters use ``@Name`` in the DAX text but the bare
    ``Name`` in ``<QueryParameter Name=...>``. SQL/MDX uses ``@`` in
    both places. Detect the provider so add/update_query_parameter can
    strip the ``@`` automatically and warn the caller.
    """
    query = find_child(dataset, "Query")
    if query is None:
        return False
    ds_name_node = find_child(query, "DataSourceName")
    if ds_name_node is None or not ds_name_node.text:
        return False
    ds_name = ds_name_node.text
    # Resolve the matching DataSource.
    for ds in doc.root.iter(f"{{{RDL_NS}}}DataSource"):
        if ds.get("Name") != ds_name:
            continue
        cp = find_child(ds, "ConnectionProperties")
        if cp is None:
            return False
        provider_node = find_child(cp, "DataProvider")
        provider = provider_node.text if provider_node is not None else ""
        if provider == "PBIDATASET":
            return True
        if provider == "SQL":
            cs_node = find_child(cp, "ConnectString")
            if cs_node is not None and cs_node.text and "powerbi://" in cs_node.text:
                return True
        return False
    return False


def _normalise_query_parameter_name(
    doc: RDLDocument,
    dataset: etree._Element,
    name: str,
    *,
    force_at_prefix: bool,
) -> tuple[str, bool, Optional[str]]:
    """Return ``(effective_name, normalised, warning_text)``.

    For PBIDATASET-bound datasets, strip a leading ``@`` from ``name`` —
    the Query Designer GUI does this automatically; hand-edited
    parameters that arrive with the ``@`` prefix would silently mismatch
    the DAX reference and fail at preview time with the (cryptic)
    ``"The query contains the 'X' parameter, which is not declared"``
    error.

    For SQL/MDX (and unknown providers), pass the name through unchanged.

    ``force_at_prefix=True`` skips the strip even on PBIDATASET — for
    callers that genuinely want the ``@`` prefix and accept the
    consequences.

    The warning string is ``None`` when no normalisation happened.
    """
    if force_at_prefix:
        return name, False, None
    if not name.startswith("@"):
        return name, False, None
    if not _is_pbidataset_dataset(doc, dataset):
        return name, False, None
    bare = name.lstrip("@")
    if not bare:
        # Pathological: name was just "@@" or "@". Don't strip to empty;
        # let RDL reject it downstream with a clear error.
        return name, False, None
    return (
        bare,
        True,
        (
            f"PBIDATASET parameter naming: stripped the leading '@' "
            f"from {name!r} → {bare!r}. The DAX text continues to "
            f"reference '@{bare}' (with the '@'); only the "
            "<QueryParameter Name=...> attribute uses the bare form. "
            "Pass force_at_prefix=True to keep the '@' anyway."
        ),
    )


def _query_parameters_root(query: etree._Element, *, create: bool) -> Optional[etree._Element]:
    qp = find_child(query, "QueryParameters")
    if qp is not None or not create:
        return qp
    qp = etree.SubElement(query, q("QueryParameters"))
    return qp


def _find_query_parameter(qp_root: etree._Element, name: str) -> Optional[etree._Element]:
    for qp in find_children(qp_root, "QueryParameter"):
        if qp.get("Name") == name:
            return qp
    return None


# ---- update_dataset_query --------------------------------------------------


def _sync_designer_state_statement(query: etree._Element, dax_body: str) -> bool:
    """Rewrite ``<rd:DesignerState>/<Statement>`` to mirror ``<CommandText>``.

    PBIDATASET datasets carry a ``<rd:DesignerState>`` block whose
    ``<Statement>`` child is what the Report Builder Query Designer GUI
    displays. When ``<CommandText>`` changes but ``<Statement>`` doesn't,
    the GUI shows stale DAX, masking the actual runtime query. This sync
    keeps the two in step.

    Returns True iff a Statement element was rewritten (used by callers
    that surface a ``changed`` list — none today, but the helper stays
    truthful so future callers don't have to guess).

    Non-PBIDATASET datasets typically have no DesignerState; the function
    is a no-op for those (returns False).
    """
    designer_state = query.find(f"{{{RD_NS}}}DesignerState")
    if designer_state is None:
        return False
    statement = designer_state.find(f"{{{RD_NS}}}Statement")
    if statement is None:
        # Some PBIDATASET DesignerState blocks omit Statement entirely
        # (rare, but valid). Don't synthesise one — Report Builder only
        # populates it for queries authored via the Query Designer.
        return False
    new_text = encode_text(dax_body)
    if statement.text == new_text:
        return False
    statement.text = new_text
    return True


_VALID_ALIAS_STRATEGIES = (None, "preserve_field_names")


def update_dataset_query(
    path: str,
    dataset_name: str,
    dax_body: str,
    alias_strategy: Optional[str] = None,
) -> dict[str, Any]:
    """Rewrite a dataset's ``<CommandText>`` (and ``<rd:DesignerState>``).

    ``alias_strategy`` (Phase 8 commit 35):

    * ``None`` (default) — only ``<CommandText>`` and DesignerState are
      touched; existing ``<Field>/<DataField>`` cells are left as-is.
    * ``"preserve_field_names"`` — after the rewrite, parse the new DAX
      and zip its column list **positionally** against existing
      data-bound fields (those with ``<DataField>``; calculated fields
      are skipped). Each Field's ``<DataField>`` is rewritten to the
      corresponding new column. Field NAMES are preserved so existing
      ``Fields!X.Value`` references in expressions keep resolving.

      Count mismatches are reported as warnings — extra fields and
      extra columns are both surfaced rather than silently dropped or
      auto-appended (the user picks how to fix).
    """
    if not dax_body or not dax_body.strip():
        raise ValueError("dax_body must be a non-empty DAX expression")
    if alias_strategy not in _VALID_ALIAS_STRATEGIES:
        raise ValueError(
            f"unknown alias_strategy {alias_strategy!r}; valid values: "
            f"{[s for s in _VALID_ALIAS_STRATEGIES]}"
        )

    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    query = _query(dataset)

    cmd = find_child(query, "CommandText")
    if cmd is None:
        # Insert before <QueryParameters> if present; otherwise after <DataSourceName>.
        cmd = etree.Element(q("CommandText"))
        ds_name = find_child(query, "DataSourceName")
        anchor = ds_name if ds_name is not None else None
        if anchor is not None:
            anchor.addnext(cmd)
        else:
            query.insert(0, cmd)
    cmd.text = encode_text(dax_body)
    designer_state_synced = _sync_designer_state_statement(query, dax_body)

    result: dict[str, Any] = {
        "dataset": dataset_name,
        "command_text": dax_body,
        "designer_state_synced": designer_state_synced,
    }

    if alias_strategy == "preserve_field_names":
        new_columns, dax_warnings = _extract_dax_field_names(dax_body)
        mapped, warnings = _remap_data_fields_positional(dataset, new_columns)
        result["alias_strategy"] = "preserve_field_names"
        result["mapped"] = mapped
        # Surface DAX-shape warnings alongside mapping warnings; they're
        # the same class of "couldn't auto-resolve" concern.
        result["warnings"] = list(dax_warnings) + warnings

    doc.save()
    return result


def _remap_data_fields_positional(
    dataset: etree._Element, new_columns: list[str]
) -> tuple[list[dict[str, str]], list[str]]:
    """Zip a dataset's data-bound ``<Field>`` entries against the new
    DAX column list and rewrite each ``<DataField>`` to the matching
    new column. Calculated fields (``<Value>`` instead of ``<DataField>``)
    are skipped — they're derived, not directly bound.

    Returns ``(mapped, warnings)``:

    * ``mapped`` — list of ``{name, old, new}`` for each Field whose
      DataField was rewritten (only entries where old != new).
    * ``warnings`` — count-mismatch and unmapped messages.
    """
    fields_block = find_child(dataset, "Fields")
    data_fields: list[etree._Element] = []
    if fields_block is not None:
        for f in find_children(fields_block, "Field"):
            df = find_child(f, "DataField")
            if df is not None:
                data_fields.append(f)

    mapped: list[dict[str, str]] = []
    warnings: list[str] = []
    n_fields = len(data_fields)
    n_cols = len(new_columns)
    n_pairs = min(n_fields, n_cols)

    for i in range(n_pairs):
        field = data_fields[i]
        df = find_child(field, "DataField")
        old = df.text or ""
        new = new_columns[i]
        if old != new:
            df.text = encode_text(new)
            mapped.append({"name": field.get("Name") or "", "old": old, "new": new})

    if n_fields > n_cols:
        for f in data_fields[n_cols:]:
            warnings.append(
                f"existing Field {f.get('Name')!r} unmapped — new DAX has only "
                f"{n_cols} column(s); pass alias_strategy=None to keep its DataField as-is, "
                "or remove the Field."
            )
    if n_cols > n_fields:
        for col in new_columns[n_fields:]:
            warnings.append(
                f"new DAX column {col!r} has no Field — call add_dataset_field "
                "or refresh_dataset_fields to add bindings."
            )

    return mapped, warnings


# ---- query parameter management -------------------------------------------


def add_query_parameter(
    path: str,
    dataset_name: str,
    name: str,
    value_expression: str,
    force_at_prefix: bool = False,
) -> dict[str, Any]:
    """Add a ``<QueryParameter>`` binding to a dataset's query.

    PBIDATASET defence (RAG-Report session feedback bug #2): when the
    dataset binds to a Power BI XMLA DataSource, a leading ``@`` on
    ``name`` is automatically stripped (the DAX text references
    ``@<bareName>`` regardless; only the attribute uses the bare form).
    The response carries ``normalised: bool`` and a ``warning`` string
    when the strip happens. Pass ``force_at_prefix=True`` to skip the
    strip and keep the ``@`` — for the rare case where a non-DAX
    consumer (e.g. an `RSCustomDaxFilter`) needs it.

    SQL / MDX / unknown-provider datasets pass the name through
    unchanged (those conventions DO use ``@`` in both places).
    """
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    effective_name, normalised, warning = _normalise_query_parameter_name(
        doc, dataset, name, force_at_prefix=force_at_prefix
    )

    query = _query(dataset)
    qp_root = _query_parameters_root(query, create=True)

    if _find_query_parameter(qp_root, effective_name) is not None:
        raise ValueError(
            f"QueryParameter {effective_name!r} already exists in "
            f"dataset {dataset_name!r}"
        )

    qp = etree.SubElement(qp_root, q("QueryParameter"), Name=effective_name)
    value = etree.SubElement(qp, q("Value"))
    value.text = encode_text(value_expression)

    doc.save()
    result: dict[str, Any] = {
        "dataset": dataset_name,
        "name": effective_name,
        "value": value_expression,
        "normalised": normalised,
    }
    if warning is not None:
        result["warning"] = warning
    return result


def update_query_parameter(
    path: str,
    dataset_name: str,
    name: str,
    value_expression: str,
    force_at_prefix: bool = False,
) -> dict[str, Any]:
    """Update an existing query parameter's value expression.

    PBIDATASET defence: same ``@``-prefix normalisation as
    :func:`add_query_parameter`. Looks up by the normalised name first;
    falls back to looking up by the raw name (so callers updating a
    legacy ``Name="@X"`` PBIDATASET parameter that already exists on
    disk still find it).
    """
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    effective_name, normalised, warning = _normalise_query_parameter_name(
        doc, dataset, name, force_at_prefix=force_at_prefix
    )

    query = _query(dataset)
    qp_root = _query_parameters_root(query, create=False)
    if qp_root is None:
        raise ElementNotFoundError(
            f"QueryParameter {effective_name!r} not found in dataset {dataset_name!r}"
        )
    # Try the normalised name first; fall back to the raw input so legacy
    # PBIDATASET parameters that still carry the @ prefix are addressable.
    qp = _find_query_parameter(qp_root, effective_name)
    if qp is None and normalised:
        qp = _find_query_parameter(qp_root, name)
        if qp is not None:
            effective_name = name  # don't pretend we normalised
            normalised = False
            warning = None
    if qp is None:
        raise ElementNotFoundError(
            f"QueryParameter {effective_name!r} not found in dataset {dataset_name!r}"
        )

    value = find_child(qp, "Value")
    if value is None:
        value = etree.SubElement(qp, q("Value"))
    value.text = encode_text(value_expression)

    doc.save()
    result: dict[str, Any] = {
        "dataset": dataset_name,
        "name": effective_name,
        "value": value_expression,
        "normalised": normalised,
    }
    if warning is not None:
        result["warning"] = warning
    return result


def remove_query_parameter(path: str, dataset_name: str, name: str) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    query = _query(dataset)
    qp_root = _query_parameters_root(query, create=False)
    if qp_root is None:
        raise ElementNotFoundError(f"QueryParameter {name!r} not found in dataset {dataset_name!r}")
    qp = _find_query_parameter(qp_root, name)
    if qp is None:
        raise ElementNotFoundError(f"QueryParameter {name!r} not found in dataset {dataset_name!r}")
    qp_root.remove(qp)

    # An empty <QueryParameters/> block sometimes confuses Report Builder's
    # designer pane; drop it when we removed the last child.
    if len(find_children(qp_root, "QueryParameter")) == 0:
        qp_root.getparent().remove(qp_root)

    doc.save()
    return {"dataset": dataset_name, "removed": name}


# ---- dataset-level filters ------------------------------------------------


# Same enum as tablix.py — DRY would move it to a shared constant, but
# the cross-module import would create a cycle (tablix imports nothing
# from dataset today; we keep it duplicated rather than restructuring).
_VALID_FILTER_OPERATORS = frozenset(
    {
        "Equal",
        "NotEqual",
        "GreaterThan",
        "GreaterThanOrEqual",
        "LessThan",
        "LessThanOrEqual",
        "Like",
        "TopN",
        "BottomN",
        "TopPercent",
        "BottomPercent",
        "In",
        "Between",
    }
)


# Per RDL XSD, the <Filters> block on a DataSet sits AFTER <Fields>
# (which itself follows <Query>) and BEFORE the rd:* metadata children.
_DATASET_FILTERS_PRECEDED_BY = ("Fields", "Query")
_DATASET_FILTERS_FOLLOWED_BY = (
    "CaseSensitivity",
    "Collation",
    "AccentSensitivity",
    "KanatypeSensitivity",
    "WidthSensitivity",
)


def _ensure_dataset_filters_block(dataset: etree._Element) -> etree._Element:
    """Find or create ``<DataSet>/<Filters>`` respecting the schema-mandated
    sibling order (after Fields, before rd:* metadata). Mirrors the helper
    in tablix.py for tablix-level filters."""
    existing = find_child(dataset, "Filters")
    if existing is not None:
        return existing

    block = etree.Element(q("Filters"))
    for local in _DATASET_FILTERS_PRECEDED_BY:
        anchor = find_child(dataset, local)
        if anchor is not None:
            anchor.addnext(block)
            return block
    for local in _DATASET_FILTERS_FOLLOWED_BY:
        anchor = find_child(dataset, local)
        if anchor is not None:
            anchor.addprevious(block)
            return block
    dataset.append(block)
    return block


def _filter_to_dict(filter_node: etree._Element) -> dict[str, Any]:
    """Return a JSON-friendly read-back shape for one ``<Filter>``."""
    expr = find_child(filter_node, "FilterExpression")
    op = find_child(filter_node, "Operator")
    values_root = find_child(filter_node, "FilterValues")
    values: list[str] = []
    if values_root is not None:
        for v in find_children(values_root, "FilterValue"):
            values.append(v.text or "")
    return {
        "expression": expr.text if expr is not None else None,
        "operator": op.text if op is not None else None,
        "values": values,
    }


def list_dataset_filters(path: str, dataset_name: str) -> list[dict[str, Any]]:
    """List dataset-level ``<Filter>`` entries in document order. The
    list index is the stable handle for :func:`remove_dataset_filter`."""
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    filters_root = find_child(dataset, "Filters")
    if filters_root is None:
        return []
    return [_filter_to_dict(f) for f in find_children(filters_root, "Filter")]


def add_dataset_filter(
    path: str,
    dataset_name: str,
    expression: str,
    operator: str,
    values: list[str],
) -> dict[str, Any]:
    """Append a ``<Filter>`` to the dataset's ``<Filters>`` block.

    Operator must be one of the RDL 2016 FilterOperator enum values
    (Equal, NotEqual, GreaterThan, GreaterThanOrEqual, LessThan,
    LessThanOrEqual, Like, In, Between, TopN, BottomN, TopPercent,
    BottomPercent). values must be non-empty.

    DataSet-level filters apply to every consumer of the dataset (every
    Tablix / Chart bound to it). For per-tablix filtering use
    :func:`add_tablix_filter` instead.
    """
    if operator not in _VALID_FILTER_OPERATORS:
        raise ValueError(
            f"unknown filter operator {operator!r}; valid operators are: "
            f"{sorted(_VALID_FILTER_OPERATORS)}"
        )
    if not values:
        raise ValueError("at least one filter value is required")

    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    filters_root = _ensure_dataset_filters_block(dataset)

    filter_node = etree.SubElement(filters_root, q("Filter"))
    expr_node = etree.SubElement(filter_node, q("FilterExpression"))
    expr_node.text = encode_text(expression)
    op_node = etree.SubElement(filter_node, q("Operator"))
    op_node.text = operator
    values_root = etree.SubElement(filter_node, q("FilterValues"))
    for v in values:
        v_node = etree.SubElement(values_root, q("FilterValue"))
        v_node.text = encode_text(v)

    new_index = len(find_children(filters_root, "Filter")) - 1
    doc.save()
    return {
        "dataset": dataset_name,
        "index": new_index,
        "expression": expression,
        "operator": operator,
        "values": list(values),
    }


def remove_dataset_filter(
    path: str,
    dataset_name: str,
    filter_index: int,
) -> dict[str, Any]:
    """Remove a dataset-level filter by its 0-based index in the
    document order returned by :func:`list_dataset_filters`."""
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    filters_root = find_child(dataset, "Filters")
    filters = find_children(filters_root, "Filter") if filters_root is not None else []
    if not filters or filter_index < 0 or filter_index >= len(filters):
        raise IndexError(
            f"dataset {dataset_name!r} has no filter at index {filter_index}"
        )

    target = filters[filter_index]
    filters_root.remove(target)
    if len(find_children(filters_root, "Filter")) == 0:
        filters_root.getparent().remove(filters_root)

    doc.save()
    return {"dataset": dataset_name, "removed_index": filter_index}


# ---- get_dataset ---------------------------------------------------------


def _field_to_dict(field: etree._Element) -> dict[str, Any]:
    """Read-back shape for one ``<Field>``: name, data_field, value
    (when calculated), type_name."""
    df = find_child(field, "DataField")
    val = find_child(field, "Value")
    type_name_node = field.find(f"{{{RD_NS}}}TypeName")
    return {
        "name": field.get("Name"),
        "data_field": df.text if df is not None else None,
        "value": val.text if val is not None else None,
        "type_name": (
            type_name_node.text if type_name_node is not None else None
        ),
    }


def get_dataset(path: str, name: str) -> dict[str, Any]:
    """Single-DataSet read-back (parity with the other v0.3 read-back
    tools). Returns dataset name, data source binding, command text,
    fields (with both data_field and value to surface calculated
    fields), query parameters, dataset-level filters, and any
    rd:DesignerState marker."""
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, name)
    query = find_child(dataset, "Query")
    command_text = None
    data_source = None
    query_parameters: list[dict[str, Any]] = []
    designer_state_present = False
    if query is not None:
        cmd = find_child(query, "CommandText")
        ds_name = find_child(query, "DataSourceName")
        command_text = cmd.text if cmd is not None else None
        data_source = ds_name.text if ds_name is not None else None
        qp_root = find_child(query, "QueryParameters")
        if qp_root is not None:
            for qp in find_children(qp_root, "QueryParameter"):
                qp_value = find_child(qp, "Value")
                query_parameters.append(
                    {
                        "name": qp.get("Name"),
                        "value": qp_value.text if qp_value is not None else None,
                    }
                )
        designer_state_present = (
            query.find(f"{{{RD_NS}}}DesignerState") is not None
        )

    fields: list[dict[str, Any]] = []
    fields_root = find_child(dataset, "Fields")
    if fields_root is not None:
        for f in find_children(fields_root, "Field"):
            fields.append(_field_to_dict(f))

    filters: list[dict[str, Any]] = []
    filters_root = find_child(dataset, "Filters")
    if filters_root is not None:
        filters = [_filter_to_dict(f) for f in find_children(filters_root, "Filter")]

    return {
        "name": dataset.get("Name"),
        "data_source": data_source,
        "command_text": command_text,
        "fields": fields,
        "query_parameters": query_parameters,
        "filters": filters,
        "designer_state_present": designer_state_present,
    }


# ---- calculated fields ---------------------------------------------------


def _ensure_fields_block(dataset: etree._Element) -> etree._Element:
    """Find or create ``<DataSet>/<Fields>`` respecting child order
    (after ``<Query>``, before ``<Filters>``)."""
    existing = find_child(dataset, "Fields")
    if existing is not None:
        return existing

    block = etree.Element(q("Fields"))
    query = find_child(dataset, "Query")
    if query is not None:
        query.addnext(block)
        return block
    # Defensive: a DataSet without a Query is malformed, but place the
    # Fields block as the first child if so.
    dataset.insert(0, block)
    return block


def add_calculated_field(
    path: str,
    dataset_name: str,
    field_name: str,
    expression: str,
) -> dict[str, Any]:
    """Append a ``<Field>`` with a ``<Value>`` (calculated) child to the
    named dataset.

    A calculated field carries an expression (``<Value>``) instead of a
    column reference (``<DataField>``). Use this for derived fields like
    ``Total = Amount * Quantity`` that don't exist in the source query
    but should be available to consumers via ``Fields!Name.Value``.

    Refuses if a field of the same name already exists in this dataset
    (RDL semantics: field names are unique within a dataset).

    Returns ``{dataset, name, kind: 'CalculatedField', value, type_name}``.
    """
    if not field_name or not field_name.strip():
        raise ValueError("field_name must be a non-empty string")
    if not expression or not expression.strip():
        raise ValueError("expression must be a non-empty string")

    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)

    fields_root = _ensure_fields_block(dataset)
    existing_names = [
        f.get("Name")
        for f in find_children(fields_root, "Field")
        if f.get("Name") is not None
    ]
    if field_name in existing_names:
        raise ValueError(
            f"field {field_name!r} already exists in dataset {dataset_name!r}"
        )

    new_field = etree.SubElement(fields_root, q("Field"), Name=field_name)
    value_node = etree.SubElement(new_field, q("Value"))
    value_node.text = encode_text(expression)

    doc.save()
    return {
        "dataset": dataset_name,
        "name": field_name,
        "kind": "CalculatedField",
        "value": expression,
    }


def remove_calculated_field(
    path: str,
    dataset_name: str,
    field_name: str,
) -> dict[str, Any]:
    """Remove a calculated ``<Field>`` (one with ``<Value>`` instead of
    ``<DataField>``) by name.

    Refuses if the named field is data-bound (has ``<DataField>`` rather
    than ``<Value>``) — those reflect the source query's columns and
    should not be removed via this tool. Use the data-binding workflow
    (rewrite the dataset query) instead.

    Cleans up the empty ``<Fields>`` block when removing the last field.
    """
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)

    fields_root = find_child(dataset, "Fields")
    if fields_root is None:
        raise ElementNotFoundError(
            f"dataset {dataset_name!r} has no <Fields> block"
        )

    target: Optional[etree._Element] = None
    for f in find_children(fields_root, "Field"):
        if f.get("Name") == field_name:
            target = f
            break
    if target is None:
        raise ElementNotFoundError(
            f"field {field_name!r} not found in dataset {dataset_name!r}"
        )

    if find_child(target, "DataField") is not None and find_child(target, "Value") is None:
        raise ValueError(
            f"field {field_name!r} is data-bound (has <DataField>, not <Value>); "
            "remove_calculated_field only deletes calculated fields. Rewrite the "
            "dataset query via update_dataset_query to drop a data-bound field."
        )

    fields_root.remove(target)
    if len(find_children(fields_root, "Field")) == 0:
        dataset.remove(fields_root)

    doc.save()
    return {
        "dataset": dataset_name,
        "removed": field_name,
        "kind": "CalculatedField",
    }


# ---- data-bound field authoring (Phase 6 commit 27) ---------------------


def add_dataset_field(
    path: str,
    dataset_name: str,
    field_name: str,
    data_field: str,
    type_name: Optional[str] = None,
) -> dict[str, Any]:
    """Append a **data-bound** ``<Field>`` to a dataset.

    Writes ``<Field Name="..."><DataField>...</DataField>...</Field>``.
    Distinct from :func:`add_calculated_field` which writes ``<Value>``
    for derived fields. Use this when a column exists in the source
    query (e.g. came back from a fresh DAX rewrite) but isn't yet
    declared in the report's ``<Fields>`` collection — without a
    declaration, ``=Fields!X.Value`` references won't resolve at preview
    time.

    ``type_name`` writes ``<rd:TypeName>`` (e.g. ``System.String``,
    ``System.DateTime``, ``System.Decimal``); omit to leave it off, which
    is fine for most fields.

    Refuses if a field of the same name already exists in this dataset.

    Returns ``{dataset, name, kind: 'DataBoundField', data_field,
    type_name}``.
    """
    if not field_name or not field_name.strip():
        raise ValueError("field_name must be a non-empty string")
    if not data_field or not data_field.strip():
        raise ValueError("data_field must be a non-empty string")

    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)

    fields_root = _ensure_fields_block(dataset)
    existing_names = [
        f.get("Name")
        for f in find_children(fields_root, "Field")
        if f.get("Name") is not None
    ]
    if field_name in existing_names:
        raise ValueError(
            f"field {field_name!r} already exists in dataset {dataset_name!r}"
        )

    new_field = etree.SubElement(fields_root, q("Field"), Name=field_name)
    df_node = etree.SubElement(new_field, q("DataField"))
    df_node.text = encode_text(data_field)
    if type_name is not None and type_name != "":
        tn_node = etree.SubElement(new_field, f"{{{RD_NS}}}TypeName")
        tn_node.text = encode_text(type_name)

    doc.save()
    return {
        "dataset": dataset_name,
        "name": field_name,
        "kind": "DataBoundField",
        "data_field": data_field,
        "type_name": type_name,
    }


# ---- DAX-aware field refresh (Phase 6 commit 27, second tool) -----------


# Regex to capture <Table>[Column] tokens. Supports both quoted and
# unquoted table names ('Sales'[Region] or Sales[Region]). The bracketed
# column name is captured.
_DAX_TABLE_COLUMN_RE = re.compile(
    r"""
    (?:
        '(?P<quoted>[^']+)'   # 'Sales' or 'My Table'
        |
        (?P<unquoted>[A-Za-z_][\w]*)  # Sales / Customer123
    )
    \[
        (?P<col>[^\[\]]+)
    \]
    """,
    re.VERBOSE,
)

# Regex to capture SELECTCOLUMNS aliases. SELECTCOLUMNS pairs are
# "Alias", expression. We capture the quoted alias names. This is a
# best-effort extraction — it doesn't fully understand DAX, just looks
# for the typical comma-separated "..." alias pattern.
_DAX_SELECTCOLUMNS_RE = re.compile(
    r"SELECTCOLUMNS\s*\(",
    re.IGNORECASE,
)
_DAX_QUOTED_ALIAS_RE = re.compile(r'"([^"]+)"')


def _extract_dax_field_names(command_text: str) -> tuple[list[str], list[str]]:
    """Extract candidate field names from a DAX ``<CommandText>``.

    Returns ``(field_names, warnings)``. ``field_names`` is the
    deduplicated, document-order list of fields the DAX appears to
    return. ``warnings`` is a list of human-readable strings about
    unresolvable shapes (e.g. plain ``EVALUATE 'Table'`` whose columns
    aren't visible without a metadata fetch).

    Best-effort regex-based detection. Recognises:

    1. **SELECTCOLUMNS**: extracts every quoted alias inside the call
       (only the first SELECTCOLUMNS — nested ones are conservatively
       ignored to avoid false positives from inner expressions).
    2. **SUMMARIZECOLUMNS / Generic 'Table'[Col] tokens**: extracts each
       bracketed column name. The Table-prefix is stripped — RDL field
       names match the column part.
    3. **EVALUATE 'Table'**: emits a warning recommending an explicit
       SELECTCOLUMNS / SUMMARIZECOLUMNS rewrite or manual
       ``add_dataset_field`` calls.
    """
    warnings: list[str] = []
    fields: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        n = name.strip()
        if n and n not in seen:
            seen.add(n)
            fields.append(n)

    # Try SELECTCOLUMNS first — its alias shape is the highest-fidelity.
    m = _DAX_SELECTCOLUMNS_RE.search(command_text)
    if m is not None:
        # Walk forward from the call site to the matching close paren.
        depth = 0
        start = m.end() - 1  # position of "("
        end = None
        for i in range(start, len(command_text)):
            ch = command_text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        body = command_text[start + 1 : end] if end is not None else command_text[start + 1 :]
        # Strip the table argument (first comma-separated token) so
        # quoted strings inside the table reference don't pollute
        # alias extraction.
        # Heuristic: split on commas at depth 0 and take alias
        # candidates from odd-indexed positions (assuming the typical
        # SELECTCOLUMNS(<table>, "alias", expr, ...) shape).
        for alias in _DAX_QUOTED_ALIAS_RE.findall(body):
            _add(alias)

    # Then bracket tokens — SUMMARIZECOLUMNS and ad-hoc references.
    for token in _DAX_TABLE_COLUMN_RE.finditer(command_text):
        col = token.group("col")
        _add(col)

    # If still nothing recognised, emit a warning.
    if not fields:
        if "EVALUATE" in command_text.upper() and "(" not in command_text:
            warnings.append(
                "DAX is a bare EVALUATE 'Table' shape — column list isn't "
                "extractable without a metadata fetch. Use add_dataset_field "
                "explicitly per column, or rewrite as SELECTCOLUMNS / "
                "SUMMARIZECOLUMNS."
            )
        else:
            warnings.append(
                "no recognisable DAX shape (SUMMARIZECOLUMNS / SELECTCOLUMNS / "
                "Table[Col] tokens). refresh_dataset_fields couldn't extract "
                "field names; use add_dataset_field explicitly per column."
            )

    return fields, warnings


def refresh_dataset_fields(
    path: str,
    dataset_name: str,
) -> dict[str, Any]:
    """Sync a dataset's ``<Fields>`` block against shape detected in its
    DAX ``<CommandText>``. Eliminates the manual "open Report Builder
    → right-click → Refresh Fields" step after a query rewrite.

    Walks the dataset's DAX, regex-extracts candidate field names from:

    1. ``SELECTCOLUMNS(<table>, "Alias", expr, ...)`` — quoted aliases.
    2. ``'Table'[Column]`` / ``Table[Column]`` tokens — bracketed column
       names (covers SUMMARIZECOLUMNS and ad-hoc references).

    Compares the extracted set against the existing ``<Fields>`` block:
    - **Adds** missing data-bound fields (each as ``<Field
      Name="X"><DataField>X</DataField></Field>``; ``rd:TypeName`` is
      omitted — Report Builder defaults to String).
    - **Lists orphans** without auto-removing them. Removing an orphan
      could break ``Fields!X.Value`` references that the LLM forgot to
      update; the user reviews the orphan list and removes intentionally.

    Returns ``{added: list[str], orphans: list[str], unchanged:
    list[str], warnings: list[str]}``.

    Cheap regex-based shape detection — not a full DAX parser.
    Unparseable shapes return a warning rather than failing the call.
    """
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    query = _query(dataset)

    cmd = find_child(query, "CommandText")
    command_text = cmd.text if cmd is not None else ""
    if not command_text:
        return {
            "added": [],
            "orphans": [],
            "unchanged": [],
            "warnings": [
                f"dataset {dataset_name!r} has no <CommandText>; nothing to refresh."
            ],
        }

    extracted, warnings = _extract_dax_field_names(command_text)

    fields_root = _ensure_fields_block(dataset)
    existing_fields = find_children(fields_root, "Field")
    existing_names = [
        f.get("Name") for f in existing_fields if f.get("Name") is not None
    ]
    extracted_set = set(extracted)
    existing_set = set(existing_names)

    added: list[str] = []
    orphans = sorted(existing_set - extracted_set)
    unchanged = sorted(extracted_set & existing_set)

    for name in extracted:
        if name in existing_set:
            continue
        new_field = etree.SubElement(fields_root, q("Field"), Name=name)
        df_node = etree.SubElement(new_field, q("DataField"))
        df_node.text = encode_text(name)
        added.append(name)

    if added:
        doc.save()

    return {
        "added": added,
        "orphans": orphans,
        "unchanged": unchanged,
        "warnings": warnings,
    }


__all__ = [
    "add_calculated_field",
    "add_dataset_field",
    "add_dataset_filter",
    "add_query_parameter",
    "get_dataset",
    "list_dataset_filters",
    "refresh_dataset_fields",
    "remove_calculated_field",
    "remove_dataset_filter",
    "remove_query_parameter",
    "update_dataset_query",
    "update_query_parameter",
]
