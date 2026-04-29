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


__all__ = [
    "add_query_parameter",
    "remove_query_parameter",
    "update_dataset_query",
    "update_query_parameter",
]
