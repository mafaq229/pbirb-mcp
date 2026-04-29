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

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_dataset
from pbirb_mcp.core.xpath import RD_NS, find_child, find_children, q


def _query(dataset: etree._Element) -> etree._Element:
    query = find_child(dataset, "Query")
    if query is None:
        # An RDL DataSet without a Query is malformed — every PBI Report
        # Builder export has one. Raise a clear error rather than auto-creating
        # one whose child order we'd have to guess.
        raise ValueError(f"DataSet {dataset.get('Name')!r} has no <Query> element")
    return query


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


def update_dataset_query(path: str, dataset_name: str, dax_body: str) -> dict[str, Any]:
    if not dax_body or not dax_body.strip():
        raise ValueError("dax_body must be a non-empty DAX expression")

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

    doc.save()
    return {
        "dataset": dataset_name,
        "command_text": dax_body,
        "designer_state_synced": designer_state_synced,
    }


# ---- query parameter management -------------------------------------------


def add_query_parameter(
    path: str,
    dataset_name: str,
    name: str,
    value_expression: str,
) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    query = _query(dataset)
    qp_root = _query_parameters_root(query, create=True)

    if _find_query_parameter(qp_root, name) is not None:
        raise ValueError(f"QueryParameter {name!r} already exists in dataset {dataset_name!r}")

    qp = etree.SubElement(qp_root, q("QueryParameter"), Name=name)
    value = etree.SubElement(qp, q("Value"))
    value.text = encode_text(value_expression)

    doc.save()
    return {"dataset": dataset_name, "name": name, "value": value_expression}


def update_query_parameter(
    path: str,
    dataset_name: str,
    name: str,
    value_expression: str,
) -> dict[str, Any]:
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    query = _query(dataset)
    qp_root = _query_parameters_root(query, create=False)
    if qp_root is None:
        raise ElementNotFoundError(f"QueryParameter {name!r} not found in dataset {dataset_name!r}")
    qp = _find_query_parameter(qp_root, name)
    if qp is None:
        raise ElementNotFoundError(f"QueryParameter {name!r} not found in dataset {dataset_name!r}")

    value = find_child(qp, "Value")
    if value is None:
        value = etree.SubElement(qp, q("Value"))
    value.text = encode_text(value_expression)

    doc.save()
    return {"dataset": dataset_name, "name": name, "value": value_expression}


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


__all__ = [
    "add_calculated_field",
    "add_dataset_filter",
    "add_query_parameter",
    "get_dataset",
    "list_dataset_filters",
    "remove_calculated_field",
    "remove_dataset_filter",
    "remove_query_parameter",
    "update_dataset_query",
    "update_query_parameter",
]
