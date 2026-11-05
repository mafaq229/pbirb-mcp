"""Dataset-field reference rules (#72).

* ``dangling-dataset-reference-field`` (error) — a parameter's
  ValidValues / DefaultValue ``<DataSetReference>`` whose ``<ValueField>``
  or ``<LabelField>`` names a field its ``<DataSetName>`` dataset doesn't
  declare. These hold *bare* field names, not expressions, so
  ``missing-field-reference`` never sees them; Report Builder fails when
  it populates the parameter. A ``DataSetName`` that names no dataset is
  ``dangling-dataset-reference``'s job — this rule stays silent on it.
"""

from __future__ import annotations

from pbirb_mcp.core.xpath import find_child, q
from pbirb_mcp.ops import expr_walk
from pbirb_mcp.ops.lint import Issue, _ancestor_chain


def _rule_dangling_dataset_reference_field(doc) -> list[Issue]:
    datasets = expr_walk.ScopeIndex(doc.root).datasets
    issues: list[Issue] = []
    for ref in doc.root.iter(q("DataSetReference")):
        ds_node = find_child(ref, "DataSetName")
        ds_name = (ds_node.text or "").strip() if ds_node is not None else ""
        if ds_name not in datasets:
            continue
        fields = datasets[ds_name]
        for slot in ("ValueField", "LabelField"):
            node = find_child(ref, slot)
            name = (node.text or "").strip() if node is not None else ""
            if not name or name in fields:
                continue
            issues.append(
                {
                    "severity": "error",
                    "rule": "dangling-dataset-reference-field",
                    "location": _ancestor_chain(node),
                    "message": (
                        f"DataSetReference {slot} {name!r} is not a field of dataset {ds_name!r}"
                    ),
                    "suggestion": (
                        f"point {slot} at a field of {ds_name!r} "
                        f"({', '.join(sorted(fields)) or 'none declared'}) or add the field"
                    ),
                }
            )
    return issues


RULES = {"dangling-dataset-reference-field": _rule_dangling_dataset_reference_field}
