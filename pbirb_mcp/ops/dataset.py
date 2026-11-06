"""Dataset-mutation tools.

Edits the ``<DataSet>`` block of an RDL: the DAX command text and the
``<QueryParameters>`` collection that wires report parameters into the query.

DAX bodies are stored verbatim — we don't validate DAX syntax, so the user
(or Report Builder at preview time) is the source of truth for syntactic
correctness. The query's *result columns* are read with the tokenizer in
:mod:`pbirb_mcp.ops.dax` (``refresh_dataset_fields`` and
``alias_strategy='preserve_field_names'``). Empty / whitespace-only bodies
are rejected up front because
Report Builder loads them but immediately errors at preview, which is a
worse signal than a clear ValueError here.

PBI paginated reports do **not** carry ``<CommandType>`` for DAX (unlike
SSRS where you'd set ``CommandType=StoredProcedure``); these tools never
add it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import ElementNotFoundError, resolve_dataset
from pbirb_mcp.core.mutation import (
    ChangeTracker,
    remove_child_if_present,
    set_node_text,
    set_text_if_different,
)
from pbirb_mcp.core.names import (
    MAX_IDENTIFIER_LENGTH,
    is_identifier,
    report_scope_names,
    validate_identifier,
)
from pbirb_mcp.core.xpath import (
    RD_NS,
    RDL_NS,
    find_child,
    find_children,
    find_designer_statement,
    q,
    qrd,
)
from pbirb_mcp.ops import expr_walk
from pbirb_mcp.ops.dax import data_field_matches, normalize_data_field, result_columns
from pbirb_mcp.ops.dax import tokenize as dax_tokenize
from pbirb_mcp.ops.filter_types import (
    encode_filter_texts,
    filter_to_dict,
    type_mismatch_warnings,
    validate_filter,
    wrap_with_format,
    write_filter,
)


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
        return _source_family(ds) == "Power BI"
    return False


def _source_family(source: etree._Element) -> Optional[str]:
    """``"Power BI"`` for a PBIDATASET source or the SQL + ``powerbi://``
    XMLA shape (DAX; bare ``<QueryParameter Name>``), ``"SQL/other"`` for
    any other provider (``@`` in both places), ``None`` when the source
    has no ``<ConnectionProperties>`` (a shared reference)."""
    cp = find_child(source, "ConnectionProperties")
    if cp is None:
        return None
    provider_node = find_child(cp, "DataProvider")
    provider = provider_node.text if provider_node is not None else ""
    if provider == "PBIDATASET":
        return "Power BI"
    if provider == "SQL":
        cs_node = find_child(cp, "ConnectString")
        if cs_node is not None and cs_node.text and "powerbi://" in cs_node.text:
            return "Power BI"
    return "SQL/other"


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
    """Rewrite the Query Designer's Statement to mirror ``<CommandText>``.

    Report Builder stores what its Query Designer shows in
    ``<rd:DesignerState>/<QueryDefinition>/<Query>/<Statement>`` (the
    ``QueryDefinition`` namespace — issue #70; located by
    :func:`pbirb_mcp.core.xpath.find_designer_statement`, which also
    accepts a legacy ``<rd:Statement>`` child). When ``<CommandText>``
    changes but the Statement doesn't, the designer shows stale DAX — and
    clicking OK in the graphical designer can regenerate the old query
    over the new one.

    Returns True iff a Statement element was rewritten. A missing
    DesignerState, or one without a Statement, is left alone (False):
    nothing is synthesised.
    """
    statement = find_designer_statement(query)
    if statement is None:
        return False
    # Verbatim, like <CommandText> (#75): DAX legitimately contains
    # entity-shaped text such as SUBSTITUTE(x, "&amp;", "&").
    if statement.text == dax_body:
        return False
    statement.text = dax_body
    return True


def _drop_designer_state(query: etree._Element) -> bool:
    """Remove ``<rd:DesignerState>``; True iff one existed."""
    designer = query.find(qrd("DesignerState"))
    if designer is None:
        return False
    query.remove(designer)
    return True


_VALID_DESIGNER_STATE_MODES = ("sync", "drop")


_VALID_ALIAS_STRATEGIES = (None, "preserve_field_names")


def update_dataset_query(
    path: str,
    dataset_name: str,
    dax_body: str,
    alias_strategy: Optional[str] = None,
    dry_run: bool = False,
    designer_state: str = "sync",
) -> dict[str, Any]:
    """Rewrite a dataset's ``<CommandText>`` (and ``<rd:DesignerState>``).

    ``designer_state`` (issue #70) — what to do with Report Builder's
    Query Designer state:

    * ``"sync"`` (default) — rewrite its ``Statement`` to the new DAX
      (``designer_state_synced`` says whether it changed). Only the text is
      updated: the ``QueryDefinition``'s Select/Items graph cannot describe
      arbitrary DAX and is left as it was.
    * ``"drop"`` — remove ``<rd:DesignerState>`` altogether
      (``designer_state_dropped``). Report Builder then opens the query in
      its text editor instead of the graphical designer, so the stale item
      graph can never regenerate the old query. Prefer it for hand-written
      DAX the graphical designer couldn't have produced.

    ``alias_strategy``:

    * ``None`` (default) — only ``<CommandText>`` and DesignerState are
      touched; existing ``<Field>/<DataField>`` cells are left as-is.
    * ``"preserve_field_names"`` — keep every ``<Field Name>`` (so
      ``Fields!X.Value`` references keep resolving) and re-aim
      ``<DataField>`` cells at the new query's result columns
      (:func:`pbirb_mcp.ops.dax.result_columns`; targets are
      ``Table[Col]`` or ``[Alias]``) in three passes — see
      :func:`_plan_preserve_field_names`:

      1. **Identity** — a Field whose DataField is still returned by the
         new query is left alone (re-submitting a report's own DAX, in
         any column order, changes nothing).
      2. **Position** — a Field bound to an old-query column that
         disappeared is re-aimed only when exactly one new column took
         that column's slot in the *old DAX's* column order.
      3. **Leftovers** — every Field / new column still unmatched is
         reported in ``warnings`` and left untouched.

      When the old query's columns can't be determined (e.g. the bare
      ``EVALUATE 'Table'``) pass 2 only handles the forced case of one
      unmatched Field and one unbound new column (with a warning).

      Calculated fields (``<Value>``) are never touched.

    ``dry_run=True`` computes the result (including ``mapped``) without
    writing the file.
    """
    if not dax_body or not dax_body.strip():
        raise ValueError("dax_body must be a non-empty DAX expression")
    if alias_strategy not in _VALID_ALIAS_STRATEGIES:
        raise ValueError(
            f"unknown alias_strategy {alias_strategy!r}; valid values: "
            f"{[s for s in _VALID_ALIAS_STRATEGIES]}"
        )
    if designer_state not in _VALID_DESIGNER_STATE_MODES:
        raise ValueError(
            f"unknown designer_state {designer_state!r}; valid values: "
            f"{list(_VALID_DESIGNER_STATE_MODES)}"
        )

    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    query = _query(dataset)

    tracker = ChangeTracker(dataset)
    cmd = find_child(query, "CommandText")
    old_command_text = cmd.text if cmd is not None else None
    if cmd is None:
        # Insert before <QueryParameters> if present; otherwise after <DataSourceName>.
        cmd = etree.Element(q("CommandText"))
        ds_name = find_child(query, "DataSourceName")
        anchor = ds_name if ds_name is not None else None
        if anchor is not None:
            anchor.addnext(cmd)
        else:
            query.insert(0, cmd)
    # Verbatim (#75): no entity decoding. DAX string literals such as
    # SUBSTITUTE(col, "&amp;", "&") are meaningful; lxml escapes once.
    cmd.text = dax_body
    result: dict[str, Any] = {"dataset": dataset_name, "command_text": dax_body}
    if designer_state == "drop":
        result["designer_state_synced"] = False
        result["designer_state_dropped"] = _drop_designer_state(query)
    else:
        result["designer_state_synced"] = _sync_designer_state_statement(query, dax_body)

    if alias_strategy == "preserve_field_names":
        plan, warnings = _plan_preserve_field_names(dataset, old_command_text, dax_body)
        mapped: list[dict[str, str]] = []
        for field, df_node, new in plan:
            old = df_node.text or ""
            df_node.text = new  # derived from the verbatim DAX (#75)
            mapped.append({"name": field.get("Name") or "", "old": old, "new": new})
        result["alias_strategy"] = "preserve_field_names"
        result["mapped"] = mapped
        result["warnings"] = warnings

    result["changed"] = tracker.check()
    if dry_run:
        result["dry_run"] = True
    elif result["changed"]:
        doc.save()
    return result


_START = object()  # slot anchor: "before the first surviving column"


def _plan_preserve_field_names(
    dataset: etree._Element, old_dax: Optional[str], new_dax: str
) -> tuple[list[tuple[etree._Element, etree._Element, str]], list[str]]:
    """Work out the ``<DataField>`` rewrites for
    ``alias_strategy='preserve_field_names'``.

    Returns ``(plan, warnings)`` where ``plan`` lists ``(field,
    data_field_element, new_data_field)`` for every Field to re-aim.

    1. **Identity.** A data-bound Field whose DataField matches a new
       result column (:func:`pbirb_mcp.ops.dax.data_field_matches`)
       stays as it is and claims that column.
    2. **Position.** Remaining Fields are located in the *old* query's
       result columns (``result_columns(old_dax)``), never in the
       ``<Fields>`` order (Report Builder doesn't keep Fields in DAX
       order). Each old column that disappeared, and each new column
       that wasn't in the old query, belongs to the slot after the
       nearest preceding column present in both queries (or the start).
       Within one slot, the vanished old columns and the new columns are
       paired in order only when their counts are equal — i.e. each
       vanished column was replaced by exactly one new column. Slots
       whose counts differ are left alone.
    3. **Leftovers.** Fields and new columns still unmatched are
       reported as warnings; nothing else is rewritten.

    The position pass is skipped (with a warning) when the new query's
    columns can't be fully determined, and when the old query's can't
    — except for the forced case of exactly one unmatched Field and
    exactly one unclaimed new column, which is re-aimed with a warning
    asking the caller to check it. Anything else would be a guess, i.e.
    exactly the silent rebinding this function exists to avoid.
    """
    warnings: list[str] = []
    new_cols, new_warnings = result_columns(new_dax)
    warnings.extend(new_warnings)

    bound = [(f, find_child(f, "DataField"), df_text) for f, df_text in _data_bound_fields(dataset)]

    # Pass 1 — identity.
    claimed: set[str] = set()
    remaining: list[tuple[etree._Element, etree._Element, str]] = []
    for entry in bound:
        hit = next((c for c in new_cols if data_field_matches(entry[2], c)), None)
        if hit is None:
            remaining.append(entry)
        else:
            claimed.add(normalize_data_field(hit.data_field))

    unclaimed: list = []
    seen: set[str] = set()
    for c in new_cols:
        key = normalize_data_field(c.data_field)
        if key not in claimed and key not in seen:
            seen.add(key)
            unclaimed.append(c)

    plan: list[tuple[etree._Element, etree._Element, str]] = []
    if remaining and unclaimed:
        if new_warnings:
            warnings.append(
                "positional remap skipped: the new DAX's result columns could not be "
                "fully determined (see the warnings above)."
            )
        else:
            old_cols, old_warnings = result_columns(old_dax or "")
            if (old_warnings or not old_cols) and len(remaining) == len(unclaimed) == 1:
                # No old column order to go by, but the rebinding is
                # forced: one Field lost its column, one column has no
                # Field.
                f, df_node, df_text = remaining[0]
                plan = [(f, df_node, unclaimed[0].data_field)]
                warnings.append(
                    f"Field {f.get('Name')!r} re-aimed from {df_text!r} to "
                    f"{unclaimed[0].data_field!r} as the only unmatched Field / column "
                    "pair (the previous DAX's column order could not be determined); "
                    "check it is the intended column."
                )
            elif old_warnings or not old_cols:
                warnings.append(
                    "positional remap skipped: the dataset's previous DAX result columns "
                    "could not be determined, so there is no reliable column order to "
                    "remap by. Set the remaining DataFields explicitly (add_dataset_field "
                    "/ remove_dataset_field) or run refresh_dataset_fields."
                )
            else:
                plan = _positional_plan(remaining, unclaimed, old_cols, new_cols, warnings)

    planned = {id(entry[0]) for entry in plan}
    planned_targets = {normalize_data_field(new) for _f, _d, new in plan}
    for f, _df, df_text in remaining:
        if id(f) in planned:
            continue
        warnings.append(
            f"existing Field {f.get('Name')!r} (DataField {df_text!r}) unmapped — the new "
            "DAX doesn't return that column and no single replacement could be "
            "identified; its DataField is left as-is. Fix it with add_dataset_field / "
            "remove_dataset_field, or pass alias_strategy=None."
        )
    for c in unclaimed:
        if normalize_data_field(c.data_field) in planned_targets:
            continue
        warnings.append(
            f"new DAX column {c.data_field!r} has no Field — call add_dataset_field "
            "or refresh_dataset_fields to add a binding."
        )
    return plan, warnings


def _positional_plan(
    remaining: list[tuple[etree._Element, etree._Element, str]],
    unclaimed: list,
    old_cols: list,
    new_cols: list,
    warnings: list[str],
) -> list[tuple[etree._Element, etree._Element, str]]:
    """Pass 2 of :func:`_plan_preserve_field_names`."""
    old_keys = [normalize_data_field(c.data_field) for c in old_cols]
    new_keys = {normalize_data_field(c.data_field) for c in new_cols}
    old_key_set = set(old_keys)
    shown = {k: c.data_field for k, c in zip(old_keys, old_cols)}

    # Vanished old columns, grouped by the surviving column before them.
    vanished: dict[object, list[int]] = {}
    anchor: object = _START
    for i, key in enumerate(old_keys):
        if key in new_keys:
            anchor = key
        elif i == old_keys.index(key):
            vanished.setdefault(anchor, []).append(i)

    # New columns (not in the old query), grouped the same way.
    unclaimed_keys = {normalize_data_field(c.data_field) for c in unclaimed}
    arrived: dict[object, list] = {}
    anchor = _START
    seen: set[str] = set()
    for c in new_cols:
        key = normalize_data_field(c.data_field)
        if key in old_key_set:
            anchor = key
        elif key in unclaimed_keys and key not in seen:
            seen.add(key)
            arrived.setdefault(anchor, []).append(c)

    replacement: dict[int, object] = {}
    for slot, old_idx in vanished.items():
        new_in_slot = arrived.get(slot, [])
        if len(old_idx) == len(new_in_slot):
            replacement.update(zip(old_idx, new_in_slot))
        elif new_in_slot:
            where = "at the start" if slot is _START else f"after {shown[slot]!r}"
            warnings.append(
                f"positional remap ambiguous {where}: {len(old_idx)} old column(s) "
                f"({', '.join(old_cols[i].data_field for i in old_idx)}) vanished but "
                f"{len(new_in_slot)} new column(s) "
                f"({', '.join(c.data_field for c in new_in_slot)}) took their place; "
                "those Fields are left as-is."
            )

    plan: list[tuple[etree._Element, etree._Element, str]] = []
    for f, df_node, df_text in remaining:
        idx = next((i for i, c in enumerate(old_cols) if data_field_matches(df_text, c)), None)
        if idx is None or idx not in replacement:
            continue
        plan.append((f, df_node, replacement[idx].data_field))
    return plan


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
            f"QueryParameter {effective_name!r} already exists in dataset {dataset_name!r}"
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
        "warnings": _binding_warnings(doc, dataset, effective_name),
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

    changed = set_text_if_different(qp, "Value", encode_text(value_expression))

    if changed:
        doc.save()
    result: dict[str, Any] = {
        "dataset": dataset_name,
        "name": effective_name,
        "value": value_expression,
        "normalised": normalised,
        "changed": changed,
        "warnings": _binding_warnings(doc, dataset, effective_name),
    }
    if warning is not None:
        result["warning"] = warning
    return result


def remove_query_parameter(path: str, dataset_name: str, name: str) -> dict[str, Any]:
    """Remove a ``<QueryParameter>`` binding.

    Same lookup as :func:`update_query_parameter` (#95): on a PBIDATASET
    dataset a leading ``@`` is stripped first (``"@X"`` finds the
    ``Name="X"`` that :func:`add_query_parameter` wrote), falling back to
    the raw name for a legacy ``Name="@X"``. ``warnings`` reports an
    ``@name`` the DAX still uses now that its binding is gone.
    """
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    effective_name, normalised, _warning = _normalise_query_parameter_name(
        doc, dataset, name, force_at_prefix=False
    )
    query = _query(dataset)
    qp_root = _query_parameters_root(query, create=False)
    qp = _find_query_parameter(qp_root, effective_name) if qp_root is not None else None
    if qp is None and normalised and qp_root is not None:
        qp = _find_query_parameter(qp_root, name)
    if qp is None:
        raise ElementNotFoundError(f"QueryParameter {name!r} not found in dataset {dataset_name!r}")
    removed = qp.get("Name")
    qp_root.remove(qp)

    # An empty <QueryParameters/> block sometimes confuses Report Builder's
    # designer pane; drop it when we removed the last child.
    if len(find_children(qp_root, "QueryParameter")) == 0:
        qp_root.getparent().remove(qp_root)

    doc.save()
    return {
        "dataset": dataset_name,
        "removed": removed,
        "warnings": _binding_warnings(doc, dataset, removed),
    }


def _binding_warnings(doc: RDLDocument, dataset: etree._Element, name: str) -> list[str]:
    """Lint findings about one query-parameter binding (#95): an ``@name``
    the DAX uses without a binding, a binding the DAX doesn't use, a
    multi-value parameter passed raw into a scalar comparison, an
    undeclared report parameter in the value."""
    # Imported here: the lint package imports this module.
    from pbirb_mcp.ops.lint_rules.query_parameters import binding_warnings

    return binding_warnings(doc, dataset, name)


# ---- dataset-level filters ------------------------------------------------


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


def list_dataset_filters(path: str, dataset_name: str) -> list[dict[str, Any]]:
    """List dataset-level ``<Filter>`` entries in document order. The
    list index is the stable handle for :func:`remove_dataset_filter`."""
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    filters_root = find_child(dataset, "Filters")
    if filters_root is None:
        return []
    return [filter_to_dict(f) for f in find_children(filters_root, "Filter")]


def add_dataset_filter(
    path: str,
    dataset_name: str,
    expression: str,
    operator: str,
    values: list[str],
    field_format: Optional[str] = None,
    value_data_type: Optional[str] = None,
) -> dict[str, Any]:
    """Append a ``<Filter>`` to the dataset's ``<Filters>`` block.

    Operator must be one of the RDL 2016 FilterOperator enum values
    (Equal, NotEqual, GreaterThan, GreaterThanOrEqual, LessThan,
    LessThanOrEqual, Like, In, Between, TopN, BottomN, TopPercent,
    BottomPercent). Arity is checked (#94,
    :func:`pbirb_mcp.ops.filter_types.validate_filter`): Between takes 2
    values, In one or more, every other operator exactly 1; TopN-style
    operators take a number or an expression.

    ``value_data_type`` (Boolean / DateTime / Integer / Float / String)
    writes ``<FilterValue DataType="...">`` so a literal compares with a
    typed field; without it a numeric / date field compared with bare
    literals warns.

    DataSet-level filters apply to every consumer of the dataset (every
    Tablix / Chart bound to it). For per-tablix filtering use
    :func:`add_tablix_filter` instead.

    ``field_format`` (Phase 8 commit 36): when provided, the filter
    expression is wrapped as ``=Format(<body>, "<field_format>")`` to
    coerce a typed field into a string for comparison against a String
    parameter — closes the date-vs-string mismatch class.

    Type-aware warnings cross-check ``Fields!X.Value`` references in
    the expression against ``Parameters!Y.Value`` references in the
    values; mismatches surface as ``warnings`` in the response.
    """
    validate_filter(operator, values, value_data_type)

    effective_expression = (
        wrap_with_format(expression, field_format) if field_format else expression
    )

    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    warnings = type_mismatch_warnings(
        doc.root,
        dataset,
        effective_expression,
        values,
        operator=operator,
        value_data_type=value_data_type,
    )
    filters_root = _ensure_dataset_filters_block(dataset)

    written_expression, written_values, entity_warnings = encode_filter_texts(
        effective_expression, values
    )
    warnings.extend(entity_warnings)
    write_filter(filters_root, written_expression, operator, written_values, value_data_type)

    new_index = len(find_children(filters_root, "Filter")) - 1
    doc.save()
    return {
        "dataset": dataset_name,
        "index": new_index,
        "expression": written_expression,
        "operator": operator,
        "values": written_values,
        "data_type": value_data_type,
        "formatted": field_format is not None,
        "warnings": warnings,
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
        raise IndexError(f"dataset {dataset_name!r} has no filter at index {filter_index}")

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
        "type_name": (type_name_node.text if type_name_node is not None else None),
    }


def get_dataset(path: str, name: str) -> dict[str, Any]:
    """Single-DataSet read-back (parity with the other v0.3 read-back
    tools). Returns dataset name, data source binding, command text,
    fields (with both data_field and value to surface calculated
    fields), query parameters, dataset-level filters, and any
    rd:DesignerState marker."""
    doc = RDLDocument.open(path)
    return dataset_to_dict(resolve_dataset(doc, name))


def dataset_to_dict(dataset: etree._Element) -> dict[str, Any]:
    """Read-back shape for one ``<DataSet>`` — shared by ``get_dataset`` and
    every ``reader.get_datasets`` entry so the two can't drift (#97)."""
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
        designer_state_present = query.find(f"{{{RD_NS}}}DesignerState") is not None

    fields: list[dict[str, Any]] = []
    fields_root = find_child(dataset, "Fields")
    if fields_root is not None:
        for f in find_children(fields_root, "Field"):
            fields.append(_field_to_dict(f))

    filters: list[dict[str, Any]] = []
    filters_root = find_child(dataset, "Filters")
    if filters_root is not None:
        filters = [filter_to_dict(f) for f in find_children(filters_root, "Filter")]

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
    validate_identifier(field_name, "field")
    if not expression or not expression.strip():
        raise ValueError("expression must be a non-empty string")

    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)

    fields_root = _ensure_fields_block(dataset)
    existing_names = [
        f.get("Name") for f in find_children(fields_root, "Field") if f.get("Name") is not None
    ]
    if field_name in existing_names:
        raise ValueError(f"field {field_name!r} already exists in dataset {dataset_name!r}")

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
    force: bool = False,
) -> dict[str, Any]:
    """Remove a calculated ``<Field>`` (one with ``<Value>`` instead of
    ``<DataField>``) by name.

    Refuses if the named field is data-bound (has ``<DataField>`` rather
    than ``<Value>``) — those reflect the source query's columns and
    should not be removed via this tool. Use the data-binding workflow
    (rewrite the dataset query) instead.

    Refuses while the field is still referenced (#72) — same scope-aware
    check as :func:`remove_dataset_field`. Pass ``force=True`` to remove
    anyway.

    Cleans up the empty ``<Fields>`` block when removing the last field.
    """
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)

    fields_root = find_child(dataset, "Fields")
    if fields_root is None:
        raise ElementNotFoundError(f"dataset {dataset_name!r} has no <Fields> block")

    target: Optional[etree._Element] = None
    for f in find_children(fields_root, "Field"):
        if f.get("Name") == field_name:
            target = f
            break
    if target is None:
        raise ElementNotFoundError(f"field {field_name!r} not found in dataset {dataset_name!r}")

    if find_child(target, "DataField") is not None and find_child(target, "Value") is None:
        raise ValueError(
            f"field {field_name!r} is data-bound (has <DataField>, not <Value>); "
            "remove_calculated_field only deletes calculated fields. Rewrite the "
            "dataset query via update_dataset_query to drop a data-bound field."
        )

    if not force:
        _refuse_if_referenced(doc, dataset_name, field_name)

    fields_root.remove(target)
    if len(find_children(fields_root, "Field")) == 0:
        dataset.remove(fields_root)

    doc.save()
    return {
        "dataset": dataset_name,
        "removed": field_name,
        "kind": "CalculatedField",
    }


def _refuse_if_referenced(doc: RDLDocument, dataset_name: str, field_name: str) -> None:
    """Raise ``ValueError`` when field ``field_name`` of ``dataset_name`` still
    has consumers (:func:`pbirb_mcp.ops.expr_walk.find_field_references`:
    scope-aware ``Fields!X`` / ``Fields("X")`` references and parameter
    ``DataSetReference`` ValueField/LabelField). Lists up to 5 locators."""
    refs = expr_walk.find_field_references(doc.root, dataset_name, field_name)
    if not refs:
        return
    locators: list[str] = []
    for r in refs:
        if r.locator not in locators:
            locators.append(r.locator)
    shown = locators[:5]
    elided = f" (+{len(locators) - 5} more)" if len(locators) > 5 else ""
    raise ValueError(
        f"field {field_name!r} of dataset {dataset_name!r} is still referenced from "
        f"{len(locators)} location(s): {shown}{elided}. Pass force=True to remove anyway."
    )


def remove_dataset_field(
    path: str,
    dataset_name: str,
    field_name: str,
    force: bool = False,
) -> dict[str, Any]:
    """Remove a **data-bound** ``<Field>`` (one with ``<DataField>``) by name.

    Symmetric counterpart to :func:`remove_calculated_field` — that one
    deletes ``<Value>``-bearing calculated fields and refuses on
    data-bound; this one deletes ``<DataField>``-bearing data-bound
    fields and refuses on calculated. Together they close the cookbook
    flow ``refresh_dataset_fields`` (lists orphans) → review →
    ``remove_dataset_field`` (drops them) without falling back to
    ``raw_xml_replace``.

    Refusal paths:

    1. **Calculated field**: target has ``<Value>`` and not
       ``<DataField>`` → ``ValueError`` pointing at
       :func:`remove_calculated_field`.
    2. **Still referenced** (#72): an expression evaluated against this
       dataset uses ``Fields!<field_name>.<member>`` or
       ``Fields("<field_name>")``, or a parameter's ValidValues /
       DefaultValue ``DataSetReference`` names it as ``ValueField`` /
       ``LabelField`` → ``ValueError`` carrying up to the first 5 locators
       (see :func:`pbirb_mcp.ops.expr_walk.find_field_references`). A
       same-named field of *another* dataset doesn't count. Pass
       ``force=True`` to remove anyway.

    Cleans up the empty ``<Fields>`` block when removing the last field.

    Returns ``{dataset, removed, kind: 'DataBoundField'}``.
    """
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)

    fields_root = find_child(dataset, "Fields")
    if fields_root is None:
        raise ElementNotFoundError(f"dataset {dataset_name!r} has no <Fields> block")

    target: Optional[etree._Element] = None
    for f in find_children(fields_root, "Field"):
        if f.get("Name") == field_name:
            target = f
            break
    if target is None:
        raise ElementNotFoundError(f"field {field_name!r} not found in dataset {dataset_name!r}")

    if find_child(target, "Value") is not None and find_child(target, "DataField") is None:
        raise ValueError(
            f"field {field_name!r} is calculated (has <Value>, not <DataField>); "
            "remove_dataset_field only deletes data-bound fields. Use "
            "remove_calculated_field instead."
        )

    if not force:
        _refuse_if_referenced(doc, dataset_name, field_name)

    fields_root.remove(target)
    if len(find_children(fields_root, "Field")) == 0:
        dataset.remove(fields_root)

    doc.save()
    return {
        "dataset": dataset_name,
        "removed": field_name,
        "kind": "DataBoundField",
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
    validate_identifier(field_name, "field")
    if not data_field or not data_field.strip():
        raise ValueError("data_field must be a non-empty string")

    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)

    fields_root = _ensure_fields_block(dataset)
    existing_names = [
        f.get("Name") for f in find_children(fields_root, "Field") if f.get("Name") is not None
    ]
    if field_name in existing_names:
        raise ValueError(f"field {field_name!r} already exists in dataset {dataset_name!r}")

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


def _extract_dax_field_names(command_text: str) -> tuple[list[str], list[str]]:
    """Extract result-set field names from a DAX ``<CommandText>``.

    Thin wrapper over :func:`pbirb_mcp.ops.dax.result_columns`. Returns
    ``(field_names, warnings)``: ``field_names`` is the result-set
    column names in query order (aliases for ``"name", expr`` pairs,
    the bare column part for ``'Table'[Col]`` references). Names are
    NOT deduplicated — two tables can each contribute a ``Name``
    column — so callers that create Fields from this list must skip
    repeats themselves. ``warnings`` is non-empty when part of the
    query couldn't be seen into (bare ``EVALUATE 'Table'``, unknown
    table functions, …); the name list may then be incomplete.
    """
    columns, warnings = result_columns(command_text)
    return [c.name for c in columns], warnings


def _extract_dax_qualified_data_fields(
    command_text: str,
) -> tuple[list[str], list[str]]:
    """Extract ``<DataField>`` targets from a DAX ``<CommandText>``.

    One entry per result-set column, in query order, exactly as Report
    Builder writes ``<DataField>``: ``Table[Col]`` (unquoted, ``''``
    un-escaped) for a column reference, ``[alias]`` for a ``"name",
    expr`` pair (#38 — this used to be ``None``).

    Returns ``(targets, warnings)``; parser warnings are passed through,
    plus one extra warning when no target could be recovered.
    """
    columns, warnings = result_columns(command_text)
    targets = [c.data_field for c in columns]
    if not targets:
        warnings = list(warnings) + [
            "DAX shape has no recoverable qualified Table[Col] / [Alias] DataField "
            "targets. DataFields left untouched; use add_dataset_field explicitly per column."
        ]
    return targets, warnings


_NON_WORD_CHAR_RE = re.compile(r"\W")


def rb_field_name(column_name: str, taken: Iterable[str] = ()) -> str:
    """Report Builder-style ``<Field Name>`` for a result column.

    Mirrors Report Builder's Refresh Fields: every character that is not
    a letter, digit or underscore becomes ``_`` (``Amount (USD)`` →
    ``Amount__USD_``, ``Region (EU)`` → ``Region__EU_``), and a name
    already in ``taken`` gets a numeric suffix (``Name``, ``Name1``,
    ``Name2`` …). A name that would not start with a letter (``2024``,
    ``_D``) is prefixed with ``Field`` so it stays a valid RDL identifier
    (:func:`pbirb_mcp.core.names.validate_identifier`).
    """
    stem = _NON_WORD_CHAR_RE.sub("_", column_name or "")
    if not stem:
        stem = "Field"
    elif not is_identifier(stem[:1]):
        stem = f"Field{stem}"
    stem = stem[:MAX_IDENTIFIER_LENGTH]
    taken_set = set(taken)
    if stem not in taken_set:
        return stem
    i = 1
    while True:
        suffix = str(i)
        candidate = stem[: MAX_IDENTIFIER_LENGTH - len(suffix)] + suffix
        if candidate not in taken_set:
            return candidate
        i += 1


def _data_bound_fields(dataset: etree._Element) -> list[tuple[etree._Element, str]]:
    """``(field, DataField text)`` for every data-bound ``<Field>`` (one
    with a ``<DataField>``) in document order. Calculated fields are
    skipped."""
    out: list[tuple[etree._Element, str]] = []
    fields_root = find_child(dataset, "Fields")
    if fields_root is None:
        return out
    for f in find_children(fields_root, "Field"):
        df = find_child(f, "DataField")
        if df is not None:
            out.append((f, df.text or ""))
    return out


def refresh_dataset_fields(
    path: str,
    dataset_name: str,
) -> dict[str, Any]:
    """Sync a dataset's ``<Fields>`` block against the result columns of
    its DAX ``<CommandText>``. Eliminates the manual "open Report Builder
    → right-click → Refresh Fields" step after a query rewrite.

    Parses the dataset's DAX with :func:`pbirb_mcp.ops.dax.result_columns`
    to find the query's result-set columns: SUMMARIZECOLUMNS group-by
    columns and ``"name", expr`` pairs, SELECTCOLUMNS / ADDCOLUMNS /
    GROUPBY / SUMMARIZE aliases, through CALCULATETABLE / FILTER / TOPN
    wrappers and ``VAR ... RETURN``. Columns referenced only in filters,
    ORDER BY, measure definitions or comments are not result columns.

    Existing Fields are matched to columns by their **DataField**
    (:func:`pbirb_mcp.ops.dax.data_field_matches` — ``Table[Col]`` /
    ``[Alias]``, quotes and case ignored), never by Field Name, so
    Report Builder's sanitised names (``Amount__USD_`` bound to
    ``Sales[Amount (USD)]``) are recognised as in sync.

    - **Adds** a data-bound Field for every column no Field is bound to,
      written the way Report Builder's Refresh Fields writes it:
      ``Name`` = :func:`rb_field_name` (``Region (EU)`` →
      ``Region__EU_``; a duplicate ``Name`` becomes ``Name1``, …) and
      ``<DataField>`` = the column's qualified binding (``Sales[Region
      (EU)]``, or ``[Total]`` for an alias). ``rd:TypeName`` is omitted.
    - **Lists orphans** — data-bound Fields bound to no result column —
      without removing them (removing could break ``Fields!X.Value``
      references); the caller reviews and removes intentionally.
    - **Refuses to add** anything when the parser returned warnings
      (bare ``EVALUATE 'Table'``, unknown table functions, …): the column
      list may then be incomplete, so ``added`` and ``orphans`` are empty
      and the warnings say why.

    Returns ``{added: [{name, data_field}], orphans: [{name,
    data_field}], unchanged: [field names], warnings: [str], changed:
    bool}``. The file is only written (``changed``) when something is
    added.
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
            "warnings": [f"dataset {dataset_name!r} has no <CommandText>; nothing to refresh."],
            "changed": False,
        }

    columns, parser_warnings = result_columns(command_text)
    warnings = list(parser_warnings)

    bound = _data_bound_fields(dataset)
    unchanged: list[str] = []
    orphans: list[dict[str, str]] = []
    for f, df_text in bound:
        if any(data_field_matches(df_text, c) for c in columns):
            unchanged.append(f.get("Name") or "")
        else:
            orphans.append({"name": f.get("Name") or "", "data_field": df_text})

    if parser_warnings:
        warnings.append(
            "no Fields added and no orphans reported: the DAX result columns could "
            "not be fully determined (see the warnings above)."
        )
        return {
            "added": [],
            "orphans": [],
            "unchanged": unchanged,
            "warnings": warnings,
            "changed": False,
        }

    fields_root = find_child(dataset, "Fields")
    taken = (
        {f.get("Name") for f in find_children(fields_root, "Field") if f.get("Name")}
        if fields_root is not None
        else set()
    )
    added: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for col in columns:
        key = normalize_data_field(col.data_field)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if any(data_field_matches(df_text, col) for _f, df_text in bound):
            continue
        name = rb_field_name(col.name, taken)
        taken.add(name)
        if fields_root is None:
            fields_root = _ensure_fields_block(dataset)
        new_field = etree.SubElement(fields_root, q("Field"), Name=name)
        df_node = etree.SubElement(new_field, q("DataField"))
        df_node.text = col.data_field  # derived from the verbatim DAX (#75)
        added.append({"name": name, "data_field": col.data_field})
        if name != col.name:
            warnings.append(
                f"result column {col.name!r} was added as Field {name!r} "
                f"(DataField {col.data_field!r})."
            )

    if added:
        doc.save()

    return {
        "added": added,
        "orphans": orphans,
        "unchanged": unchanged,
        "warnings": warnings,
        "changed": bool(added),
    }


# ---- dataset authoring (#98) ---------------------------------------------


def _data_sources(doc: RDLDocument) -> dict[str, etree._Element]:
    out: dict[str, etree._Element] = {}
    block = find_child(doc.root, "DataSources")
    for ds in find_children(block, "DataSource") if block is not None else []:
        if ds.get("Name"):
            out.setdefault(ds.get("Name"), ds)
    return out


def _require_data_source(doc: RDLDocument, name: str) -> etree._Element:
    sources = _data_sources(doc)
    if name not in sources:
        raise ElementNotFoundError(
            f"DataSource {name!r} not found; data sources: {sorted(sources) or 'none'}"
        )
    return sources[name]


def _dataset_names(doc: RDLDocument) -> list[str]:
    block = find_child(doc.root, "DataSets")
    return [d.get("Name") for d in find_children(block, "DataSet")] if block is not None else []


def _check_new_dataset_name(doc: RDLDocument, name: str) -> None:
    validate_identifier(name, "dataset")
    if name in _dataset_names(doc):
        raise ValueError(f"DataSet {name!r} already exists")
    if name in report_scope_names(doc):
        # A scope string ("Sum(..., 'X')") could no longer tell them apart.
        raise ValueError(
            f"{name!r} is already the name of a report item or group; pick another dataset name"
        )


def _locators(elements: Iterable[etree._Element]) -> list[str]:
    out: list[str] = []
    for el in elements:
        loc = expr_walk.locator(el)
        if loc not in out:
            out.append(loc)
    return out


def _in_dataset(elem: etree._Element, dataset: etree._Element) -> bool:
    cur: Optional[etree._Element] = elem
    while cur is not None:
        if cur is dataset:
            return True
        cur = cur.getparent()
    return False


def _dataset_name_nodes(doc: RDLDocument, name: str) -> list[etree._Element]:
    """Every ``<DataSetName>`` naming ``name``: data regions (Tablix,
    Chart, GaugePanel, MapDataRegion, CustomReportItem) and parameters'
    ``DataSetReference``."""
    return [el for el in doc.root.iter(q("DataSetName")) if (el.text or "").strip() == name]


def set_dataset_data_source(path: str, dataset_name: str, data_source_name: str) -> dict[str, Any]:
    """Point a dataset's ``<Query>/<DataSourceName>`` at another data source.

    The source must exist. ``warnings`` flags a switch between a Power BI
    source (DAX; bare ``<QueryParameter Name>``) and a SQL / other one
    (``@`` in both places): the command text and the query-parameter
    names probably need rewriting too. Same source again → ``changed:
    false``, no write.
    """
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    query = _query(dataset)
    new_source = _require_data_source(doc, data_source_name)
    node = find_child(query, "DataSourceName")
    previous = (node.text or "").strip() if node is not None else None
    changed = set_text_if_different(query, "DataSourceName", data_source_name, order=_QUERY_ORDER)
    warnings: list[str] = []
    old_source = _data_sources(doc).get(previous or "")
    if changed and old_source is not None:
        old_family, new_family = _source_family(old_source), _source_family(new_source)
        if old_family and new_family and old_family != new_family:
            warnings.append(
                f"provider family changes from {old_family} ({previous!r}) to {new_family} "
                f"({data_source_name!r}): review the CommandText and the QueryParameter names "
                "(Power BI: '@Name' in the DAX, bare 'Name' in the binding; SQL: '@Name' in both)."
            )
    if changed:
        doc.save()
    return {
        "dataset": dataset_name,
        "data_source": data_source_name,
        "previous": previous,
        "changed": changed,
        "warnings": warnings,
    }


_QUERY_ORDER = ("DataSourceName", "CommandType", "CommandText", "QueryParameters", "Timeout")
# DataSet children in Report Builder's order (the XSD allows any order).
_DATASET_ORDER = (
    "Query",
    "Fields",
    "Filters",
    "CaseSensitivity",
    "Collation",
    "AccentSensitivity",
    "KanatypeSensitivity",
    "WidthSensitivity",
)


def _column_name(data_field: str) -> str:
    """The column part of a DataField (``'Sales'[Amount (USD)]`` →
    ``Amount (USD)``, ``[Total]`` → ``Total``) for :func:`rb_field_name`."""
    brackets = [t for t in dax_tokenize(data_field) if t.kind == "bracket"]
    return brackets[-1].value if brackets else data_field.strip()


def add_dataset(
    path: str,
    name: str,
    data_source_name: str,
    command_text: str,
    fields: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Create ``<DataSet Name><Query><DataSourceName/><CommandText/></Query>``.

    ``name`` must be a valid identifier, not an existing dataset and not
    a report item / group name (scope strings would be ambiguous); the
    data source must exist; ``command_text`` (DAX for a Power BI source)
    is stored verbatim. ``fields`` optionally seeds data-bound Fields from
    DataField bindings (``Sales[Amount]``, ``[Total]``), named the way
    Report Builder's Refresh Fields does (:func:`rb_field_name`);
    :func:`refresh_dataset_fields` seeds them from the DAX instead.
    ``warnings`` lists ``@Name`` parameters the DAX uses with no binding
    yet (add them with :func:`add_query_parameter`).
    """
    if not isinstance(command_text, str) or not command_text.strip():
        raise ValueError("command_text must be a non-empty query")
    doc = RDLDocument.open(path)
    _check_new_dataset_name(doc, name)
    _require_data_source(doc, data_source_name)

    seeded: list[tuple[str, str]] = []
    taken: set[str] = set()
    keys: set[str] = set()
    for data_field in fields or []:
        if not isinstance(data_field, str) or not data_field.strip():
            raise ValueError("fields entries must be non-empty DataField strings")
        key = normalize_data_field(data_field)
        if key in keys:
            raise ValueError(f"DataField {data_field!r} is listed twice")
        keys.add(key)
        field_name = rb_field_name(_column_name(data_field), taken)
        taken.add(field_name)
        seeded.append((field_name, data_field.strip()))

    block = find_child(doc.root, "DataSets")
    if block is None:
        block = etree.Element(q("DataSets"))
        anchor = find_child(doc.root, "DataSources")
        if anchor is not None:
            anchor.addnext(block)
        else:
            doc.root.insert(0, block)
    dataset = etree.SubElement(block, q("DataSet"), Name=name)
    query = etree.SubElement(dataset, q("Query"))
    etree.SubElement(query, q("DataSourceName")).text = data_source_name
    etree.SubElement(query, q("CommandText")).text = command_text  # verbatim (#75)
    if seeded:
        fields_root = etree.SubElement(dataset, q("Fields"))
        for field_name, data_field in seeded:
            field = etree.SubElement(fields_root, q("Field"), Name=field_name)
            etree.SubElement(field, q("DataField")).text = data_field

    warnings = _dataset_lint_warnings(doc, dataset)
    doc.save()
    return {
        "dataset": name,
        "kind": "DataSet",
        "data_source": data_source_name,
        "fields": [{"name": n, "data_field": df} for n, df in seeded],
        "changed": True,
        "warnings": warnings,
    }


def _dataset_lint_warnings(doc: RDLDocument, dataset: etree._Element) -> list[str]:
    # Imported here: the lint package imports this module.
    from pbirb_mcp.ops.lint_rules.query_parameters import dataset_warnings

    return dataset_warnings(doc, dataset)


def _dataset_consumers(doc: RDLDocument, name: str) -> list[etree._Element]:
    """Elements outside the dataset that use it: ``<DataSetName>`` nodes
    and expressions naming it as an aggregate / lookup scope."""
    dataset = resolve_dataset(doc, name)
    out = list(_dataset_name_nodes(doc, name))
    out += [
        elem
        for elem, _t, _s, _c in expr_walk.dataset_scope_references(doc.root, name)
        if not _in_dataset(elem, dataset)
    ]
    order = {el: i for i, el in enumerate(doc.root.iter())}
    return sorted(out, key=lambda el: order.get(el, 0))


def remove_dataset(path: str, name: str, force: bool = False) -> dict[str, Any]:
    """Remove a ``<DataSet>``.

    Refuses while anything uses it — a data region's or a parameter
    ``DataSetReference``'s ``<DataSetName>``, or an aggregate / lookup
    scope string (``Sum(…, "DS")``, ``Lookup(…, "DS")``) — listing up to
    5 locators; ``force=True`` removes anyway. The empty ``<DataSets>``
    block is dropped with the last dataset.
    """
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, name)
    consumers = _dataset_consumers(doc, name)
    if consumers and not force:
        locators = _locators(consumers)
        elided = f" (+{len(locators) - 5} more)" if len(locators) > 5 else ""
        raise ValueError(
            f"DataSet {name!r} is still used from {len(locators)} location(s): "
            f"{locators[:5]}{elided}. Pass force=True to remove anyway."
        )
    block = dataset.getparent()
    block.remove(dataset)
    if len(find_children(block, "DataSet")) == 0:
        block.getparent().remove(block)
    doc.save()
    return {"removed": name, "force": force, "references": len(consumers), "changed": True}


def _rewrite_tokens(text: str, tokens: list, replacement: str) -> str:
    pieces: list[str] = []
    pos = 0
    for tok in sorted(tokens, key=lambda t: t.start):
        pieces.append(text[pos : tok.start])
        pieces.append(replacement)
        pos = tok.end
    pieces.append(text[pos:])
    return "".join(pieces)


def rename_dataset(path: str, old_name: str, new_name: str) -> dict[str, Any]:
    """Rename a ``<DataSet>`` and rewrite every reference to it: each
    ``<DataSetName>`` (data regions, parameter ``DataSetReference``) and
    each aggregate / lookup scope string (``Sum(…, "Old")``,
    ``Lookup(…, "Old")``, ``CountRows("Old")``). Names in other string
    literals and comments are left alone. ``new_name`` is checked like
    :func:`add_dataset`'s ``name``. Atomic.
    """
    if new_name == old_name:
        raise ValueError("new_name and old_name are identical; nothing to rename.")
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, old_name)
    _check_new_dataset_name(doc, new_name)

    rewrites: list[tuple[etree._Element, str]] = [
        (el, new_name) for el in _dataset_name_nodes(doc, old_name)
    ]
    quoted = '"' + new_name.replace('"', '""') + '"'
    for elem, text, _sig, calls in expr_walk.dataset_scope_references(doc.root, old_name):
        rewrites.append((elem, _rewrite_tokens(text, [c.scope_token for c in calls], quoted)))

    dataset.set("Name", new_name)
    for el, text in rewrites:
        el.text = text
    doc.save()
    return {
        "old_name": old_name,
        "new_name": new_name,
        "references_rewritten": len(rewrites),
        "changed": True,
    }


def _find_field(dataset: etree._Element, field_name: str) -> etree._Element:
    fields_root = find_child(dataset, "Fields")
    for f in find_children(fields_root, "Field") if fields_root is not None else []:
        if f.get("Name") == field_name:
            return f
    raise ElementNotFoundError(f"field {field_name!r} not found in dataset {dataset.get('Name')!r}")


def rename_dataset_field(
    path: str, dataset_name: str, old_name: str, new_name: str
) -> dict[str, Any]:
    """Rename a ``<Field>`` and rewrite its consumers: ``Fields!Old.<member>``
    and ``Fields("Old")`` in expressions evaluated against this dataset
    (the scope-aware resolution of
    :func:`pbirb_mcp.ops.expr_walk.find_field_references`), and parameter
    ``DataSetReference`` ValueField / LabelField. The ``<DataField>``
    binding to the query column is unchanged.

    A reference whose dataset can't be resolved statically is rewritten
    only when no other dataset has a field ``old_name``; otherwise it is
    left alone and listed in ``warnings``. Atomic.
    """
    if new_name == old_name:
        raise ValueError("new_name and old_name are identical; nothing to rename.")
    validate_identifier(new_name, "field")
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    field = _find_field(dataset, old_name)
    siblings = {f.get("Name") for f in field.getparent() if isinstance(f.tag, str)}
    if new_name in siblings:
        raise ValueError(f"field {new_name!r} already exists in dataset {dataset_name!r}")
    others = expr_walk.ScopeIndex(doc.root).datasets
    elsewhere = any(old_name in flds for ds, flds in others.items() if ds != dataset_name)

    rewrites: list[tuple[etree._Element, str]] = []
    warnings: list[str] = []
    for elem, text, sig, refs in expr_walk.iter_field_expression_references(
        doc.root, dataset_name, old_name
    ):
        chosen = [ref for ref, resolved, _ds in refs if resolved or not elsewhere]
        if len(chosen) < len(refs):
            warnings.append(
                f"{expr_walk.locator(elem)}: left a reference to {old_name!r} alone — its "
                f"dataset can't be resolved and another dataset has a field {old_name!r}"
            )
        if chosen:
            rewrites.append(
                (elem, expr_walk.rename_parameter_references(text, sig, chosen, new_name))
            )
    for slot in expr_walk.field_dataset_reference_slots(doc.root, dataset_name, old_name):
        rewrites.append((slot, new_name))

    field.set("Name", new_name)
    for el, text in rewrites:
        el.text = text
    doc.save()
    return {
        "dataset": dataset_name,
        "old_name": old_name,
        "new_name": new_name,
        "references_rewritten": len(rewrites),
        "changed": True,
        "warnings": warnings,
    }


# .NET types Report Builder writes in <rd:TypeName>.
FIELD_TYPE_NAMES: tuple[str, ...] = (
    "System.String",
    "System.Boolean",
    "System.Char",
    "System.Byte",
    "System.SByte",
    "System.Int16",
    "System.Int32",
    "System.Int64",
    "System.UInt16",
    "System.UInt32",
    "System.UInt64",
    "System.Single",
    "System.Double",
    "System.Decimal",
    "System.DateTime",
    "System.DateTimeOffset",
    "System.TimeSpan",
    "System.Guid",
    "System.Object",
    "System.Byte[]",
)
_FIELD_TYPES_BY_KEY = {t.casefold(): t for t in FIELD_TYPE_NAMES} | {
    t[len("System.") :].casefold(): t for t in FIELD_TYPE_NAMES
}


def set_field_type(
    path: str, dataset_name: str, field_name: str, type_name: Optional[str]
) -> dict[str, Any]:
    """Set a field's ``<rd:TypeName>``. ``type_name`` is a .NET type
    (``System.Int64``, or ``Int64`` / any case — written in the canonical
    ``System.`` form; see :data:`FIELD_TYPE_NAMES`); ``None`` removes it.
    The type drives the type checks of the filter tools. No-op when
    already set (``changed: false``)."""
    canonical: Optional[str] = None
    if type_name is not None:
        canonical = _FIELD_TYPES_BY_KEY.get(str(type_name).strip().casefold())
        if canonical is None:
            raise ValueError(
                f"unknown .NET type {type_name!r}; valid: {', '.join(FIELD_TYPE_NAMES)}"
            )
    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    field = _find_field(dataset, field_name)
    node = field.find(qrd("TypeName"))
    if canonical is None:
        changed = node is not None
        if node is not None:
            field.remove(node)
    elif node is None:
        etree.SubElement(field, qrd("TypeName")).text = canonical
        changed = True
    else:
        changed = set_node_text(node, canonical)
    if changed:
        doc.save()
    return {
        "dataset": dataset_name,
        "field": field_name,
        "type_name": canonical,
        "changed": changed,
    }


_SENSITIVITY_VALUES = ("True", "False", "Auto")


def set_dataset_options(
    path: str,
    dataset_name: str,
    timeout: Optional[int] = None,
    case_sensitivity: Optional[str] = None,
    collation: Optional[str] = None,
    accent_sensitivity: Optional[str] = None,
    kanatype_sensitivity: Optional[str] = None,
    width_sensitivity: Optional[str] = None,
) -> dict[str, Any]:
    """Set dataset properties: the query ``<Timeout>`` (seconds, 0 = none)
    and ``<CaseSensitivity>`` / ``<Collation>`` / ``<AccentSensitivity>`` /
    ``<KanatypeSensitivity>`` / ``<WidthSensitivity>`` (True / False /
    Auto; collation a name such as ``Latin1_General``, ``""`` removes it).
    ``None`` leaves an option alone; at least one is required. Returns the
    options now set; ``changed: false`` (no write) when nothing differs.
    """
    sens = {
        "CaseSensitivity": case_sensitivity,
        "AccentSensitivity": accent_sensitivity,
        "KanatypeSensitivity": kanatype_sensitivity,
        "WidthSensitivity": width_sensitivity,
    }
    if timeout is None and collation is None and all(v is None for v in sens.values()):
        raise ValueError("pass at least one option to set")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)):
        raise ValueError("timeout must be a whole number of seconds")
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be >= 0 (0 = no timeout)")
    for local, value in sens.items():
        if value is not None and value not in _SENSITIVITY_VALUES:
            raise ValueError(f"{local} must be one of {list(_SENSITIVITY_VALUES)}; got {value!r}")

    doc = RDLDocument.open(path)
    dataset = resolve_dataset(doc, dataset_name)
    query = _query(dataset)
    changed = False
    if timeout is not None:
        changed |= set_text_if_different(query, "Timeout", str(timeout), order=_QUERY_ORDER)
    if collation is not None:
        if collation.strip():
            changed |= set_text_if_different(
                dataset, "Collation", collation.strip(), order=_DATASET_ORDER
            )
        else:
            changed |= remove_child_if_present(dataset, "Collation")
    for local, value in sens.items():
        if value is not None:
            changed |= set_text_if_different(dataset, local, value, order=_DATASET_ORDER)
    if changed:
        doc.save()

    def text(parent: etree._Element, local: str) -> Optional[str]:
        node = find_child(parent, local)
        return node.text if node is not None else None

    timeout_text = text(query, "Timeout")
    return {
        "dataset": dataset_name,
        "options": {
            "timeout": int(timeout_text) if timeout_text and timeout_text.isdigit() else None,
            "case_sensitivity": text(dataset, "CaseSensitivity"),
            "collation": text(dataset, "Collation"),
            "accent_sensitivity": text(dataset, "AccentSensitivity"),
            "kanatype_sensitivity": text(dataset, "KanatypeSensitivity"),
            "width_sensitivity": text(dataset, "WidthSensitivity"),
        },
        "changed": changed,
    }


__all__ = [
    "FIELD_TYPE_NAMES",
    "add_calculated_field",
    "add_dataset",
    "add_dataset_field",
    "add_dataset_filter",
    "add_query_parameter",
    "get_dataset",
    "list_dataset_filters",
    "rb_field_name",
    "refresh_dataset_fields",
    "remove_calculated_field",
    "remove_dataset",
    "remove_dataset_filter",
    "remove_query_parameter",
    "rename_dataset",
    "rename_dataset_field",
    "set_dataset_data_source",
    "set_dataset_options",
    "set_field_type",
    "update_dataset_query",
    "update_query_parameter",
]
