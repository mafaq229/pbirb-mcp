"""XSD regression fixtures for the four 2026-04-30 RB-only bugs.

The v0.3.0 live-MCP sweep cleared every static gate yet Power BI
Report Builder rejected the file on load with four schema-conformance
bugs. v0.3.1 bundles the official Microsoft RDL 2016/01 XSD (commit
``schemas: bundle RDL 2016/01 XSD …``) so that the schema-conformance
bug class is now caught at the static layer — the same gate Report
Builder runs on load.

This module asserts that each known-bad RDL fixture under
``tests/fixtures/known_bad/`` is now flagged by ``validate_report``'s
XSD layer, with the exact ``rule == "xsd"`` shape downstream tooling
expects.

**Three fixtures, not four.** The fourth v0.3.0 sweep finding —
``d999da5`` (``<ChartAxis><Visible>true</Visible>`` lowercase) — is
an RB-runtime constraint, *not* an XSD constraint: ChartAxis/Visible
is typed as ``xsd:string`` in the bundled schema, so any string is
accepted at the static layer. That bug class still requires either
the v0.7 ``load_test_report`` runner or a dedicated lint rule (out of
scope for v0.3.1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pbirb_mcp.ops.validate import validate_report

KNOWN_BAD_DIR = Path(__file__).parent / "fixtures" / "known_bad"

# Each tuple: (fixture_filename, v0.3.0_fix_commit_sha, one-line bug summary).
KNOWN_BAD_FIXTURES = [
    (
        "bare_action_under_textbox.rdl",
        "24f5375",
        "bare <Action> directly under <Textbox>; RDL wants <ActionInfo><Actions><Action>",
    ),
    (
        "actioninfo_under_chartseries.rdl",
        "408ab54",
        "<ActionInfo> under <ChartSeries>; must live on the template <ChartDataPoint>",
    ),
    (
        "chart_axis_title_wrong_element.rdl",
        "09225df",
        "ChartAxis title element is <ChartAxisTitle>, not <Title>",
    ),
]


@pytest.mark.parametrize(
    ("filename", "fix_sha", "summary"),
    KNOWN_BAD_FIXTURES,
    ids=[t[0] for t in KNOWN_BAD_FIXTURES],
)
def test_xsd_rejects_known_bad_fixture(filename, fix_sha, summary):
    """Regression: the bundled XSD must reject this fixture.

    If this test fails, either the XSD bundle drifted (schema was
    replaced with a more permissive version) or the fixture mutation
    no longer triggers the bug. Investigate before relaxing.
    """
    fixture = KNOWN_BAD_DIR / filename
    assert fixture.is_file(), f"missing regression fixture: {fixture}"

    result = validate_report(str(fixture))

    assert result["xsd_used"] is True, (
        f"XSD didn't run against {filename} — bundle is missing? "
        f"This regression test only makes sense when the XSD layer ran."
    )
    assert result["valid"] is False, (
        f"{filename} (v0.3.0 fix {fix_sha}: {summary}) was accepted "
        f"by validate_report — the XSD layer should reject it."
    )

    xsd_errors = [e for e in result["errors"] if e["rule"] == "xsd"]
    assert xsd_errors, (
        f"{filename}: validate_report flagged the file but not at the "
        f"xsd rule. Errors: {result['errors']}"
    )

    # Each error carries (severity, rule, location, message) — the
    # canonical issue shape verify_report unions.
    for err in xsd_errors:
        assert err["severity"] == "error"
        assert err["location"].startswith(str(fixture))
        assert err["message"]
