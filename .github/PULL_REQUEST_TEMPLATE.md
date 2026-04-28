<!--
Thanks for the PR. Filling the boxes below makes review fast — every
item maps to a rule in CONTRIBUTING.md.
-->

## Summary

<!-- One paragraph: what this changes and why. -->

## Type of change

- [ ] New tool (MINOR bump)
- [ ] New optional input on existing tool (MINOR bump)
- [ ] Bug fix (PATCH bump)
- [ ] Breaking change to an existing tool (MAJOR bump — pre-1.0 may be MINOR with migration note)
- [ ] Internal refactor / docs / tests only (no bump)

## Checklist

- [ ] Tests added — at least one that would have failed before this PR.
- [ ] Full suite green: `.venv/bin/python -m pytest tests/ -v`.
- [ ] Round-trip byte-identity test still passes (`tests/test_document.py::test_round_trip_byte_identical_to_fixture`).
- [ ] Tools take stable names, never indices.
- [ ] No raw `etree.tostring(...)` that bypasses `RDLDocument.save_as`.
- [ ] No `xml.etree.ElementTree` import (lxml only).
- [ ] If a new tool: registered in `pbirb_mcp/tools.py` with a clear `description` (LLM-facing), strict `inputSchema`, and pre-conditions / constraints called out.
- [ ] **Manual smoke in Power BI Report Builder** — opened the modified `.rdl` in Report Builder on Windows, no "this report needs to be upgraded" prompt, change visible in designer, preview renders. *(If the change isn't user-facing, mark N/A and explain.)*
- [ ] CHANGELOG entry added under `## [Unreleased]` in user-facing language.
- [ ] Commit messages follow `<area>: <summary>` style.

## Report Builder smoke

<!-- If applicable: paste a screenshot or a one-liner describing what
     you saw in Report Builder after running the new tool against
     tests/fixtures/pbi_paginated_minimal.rdl. -->

## Breaking changes

<!-- If MAJOR / breaking-pre-1.0, document the migration here. Otherwise
     write "None." -->

None.
