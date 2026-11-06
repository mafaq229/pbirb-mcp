"""Report-parameter state rules (#93).

* ``parameter-state`` — the combination / type checks the parameter
  writers run at write time (:func:`pbirb_mcp.ops.parameters.parameter_state_problems`),
  so files written by hand or by older versions are reported too:

  - (error) MultiValue + Nullable; multi-value Boolean; more than one
    static default on a single-value parameter; a literal default or a
    static valid value incompatible with the DataType;
  - (warning) a hidden parameter with neither a default nor Nullable.

  A query-sourced ValueField / LabelField the dataset lacks is
  ``dangling-dataset-reference-field``'s job.
"""

from __future__ import annotations

from pbirb_mcp.core.xpath import q
from pbirb_mcp.ops.lint import Issue, _named_locator
from pbirb_mcp.ops.parameters import parameter_state_problems

_SUGGESTIONS = {
    "error": "fix it with update_parameter_advanced / set_parameter_default_values / "
    "set_parameter_available_values / set_parameter_type",
    "warning": "set a default (set_parameter_default_values) or allow_null=true",
}


def _rule_parameter_state(doc) -> list[Issue]:
    issues: list[Issue] = []
    for parameter in doc.root.iter(q("ReportParameter")):
        for severity, message in parameter_state_problems(parameter):
            issues.append(
                {
                    "severity": severity,
                    "rule": "parameter-state",
                    "location": _named_locator(parameter),
                    "message": message,
                    "suggestion": _SUGGESTIONS[severity],
                }
            )
    return issues


RULES = {"parameter-state": _rule_parameter_state}
