# Contributing

Thanks for taking a look. `pbirb-mcp` is a small, opinionated tool — most
contributions land cleanly when they follow the rules below.

## Status

Single-author, maintained in spare time. Bug reports get triaged within
about a week; feature requests are evaluated against the roadmap during
the next MINOR cycle. Filing an issue is welcome even if you can't
contribute the fix yourself.

## Dev setup

```bash
git clone https://github.com/mafaq229/pbirb-mcp
cd pbirb-mcp
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# Run the suite (~0.6s on 273 tests)
.venv/bin/python -m pytest tests/ -v

# Lint
.venv/bin/ruff check .
.venv/bin/ruff format --check .

# Pre-commit (catches lint + runs tests on every commit)
pip install pre-commit
pre-commit install
```

## Hard rules

These come from real RDL pain. Every PR is reviewed against them.

1. **Tests first.** Write the failing test before the implementation.
   Cover the happy path *and* at least one failure mode (missing element,
   invalid input, conflicting state).
2. **lxml only.** No `xml.etree.ElementTree`. Round-trip fidelity is a
   feature, not polish — Report Builder reads what's on disk and
   formatting drift causes silent corruption.
3. **Stable IDs, never indices.** Tools take `tablix_name` + `group_name`,
   not `column_index: 2`. Indices break across multi-step edits. The one
   exception is `<Filter>` ordinal indices, because filters are anonymous
   in RDL — the `list_tablix_filters` read-back returns those handles
   within a single read.
4. **Atomic save.** `RDLDocument.save_as` writes to `<path>.tmp` and
   renames. Don't bypass it.
5. **Round-trip byte-identity is enforced** by
   `tests/test_document.py::test_round_trip_byte_identical_to_fixture`.
   If a change drifts that test, fix the writer — not the test.
6. **One feature per commit.** Each commit lands one tool (or a tightly
   coupled set) with its own tests. Squash-merge keeps `main` history
   one-commit-per-feature so the CHANGELOG anchors against it.

## Versioning

The contract is the **tool surface** the LLM sees: tool names,
`inputSchema`, output shapes, error semantics. Internal refactors don't
bump MAJOR no matter how big the diff.

| Change | Bump |
|---|---|
| Rename or remove a tool | MAJOR |
| Remove or rename a required input field | MAJOR |
| Change a field's type or narrow an enum | MAJOR |
| Add a new tool | MINOR |
| Add an optional input field with a back-compat default | MINOR |
| Loosen a constraint (widen enum, optional-where-required) | MINOR |
| Add a new output field | MINOR |
| Bug fix that preserves the contract | PATCH |
| RDL round-trip fix that doesn't change tool behaviour | PATCH |

While the project is on `0.x`, MINOR may include a small breaking change
**if** it's documented under `### Changed` in `CHANGELOG.md` with a
migration note. After v1.0, breaking changes require MAJOR.

## RDL gotchas to know about

These have all bitten us before. Re-check every PR:

- The XML declaration uses **double quotes** (`<?xml version="1.0" encoding="utf-8"?>`), not lxml's default singles.
- Self-closing tags use `<Tag />` with a space, not `<Tag/>`.
- The `rd:` namespace prefix carries designer metadata Report Builder
  relies on — preserve it on round-trip.
- Don't put `MustUnderstand="df"` on `<Report>` unless you also declare
  `xmlns:df=...`.
- `<ChartCategoryAxes>` / `<ChartValueAxes>` hold `<ChartAxis>` children
  **directly** — there is no `<ChartCategoryAxis>` wrapper.
- `<ChartMember>` requires a `<Label>` child, even an empty one.
- DAX queries write `<CommandText>` only — no `<CommandType>` element.

## How a PR is reviewed

1. **Tests first?** PR adds at least one test that would have failed
   before the implementation.
2. **Test suite green?** `.venv/bin/python -m pytest tests/ -v` ends with
   all passing.
3. **Round-trip invariants intact?** The byte-identity round-trip test
   still passes.
4. **Stable IDs?** New tools take names, not indices.
5. **Tool registration & schema?** New tool registered in
   `pbirb_mcp/tools.py` with a clear `description` (this is *prompt
   input* for the LLM — write it for an LLM to read), strict
   `inputSchema`, and any pre-conditions called out (e.g.
   *"`Globals!PageNumber` only resolves inside `<PageHeader>` /
   `<PageFooter>`"*, *"Pass raw VB.NET; do not XML-escape"*).
6. **Manual smoke in Report Builder?** Drove the new tool against a
   copy of `tests/fixtures/pbi_paginated_minimal.rdl`, opened the
   resulting `.rdl` in Power BI Report Builder on Windows, confirmed no
   "this report needs to be upgraded" prompt, change visible in the
   designer, preview renders. **This is the actual integration test** —
   unit tests catch schema-level mistakes, but only Report Builder
   catches deserialiser nits.
7. **CHANGELOG entry?** Added under `## [Unreleased]` in the right
   section, in user-facing language ("Add `add_column_group` for matrix
   layouts" — not "Wire up `TablixColumnHierarchy`").

## Commit messages

Existing style: `<area>: <one-line summary>`. Areas mirror the op
modules (`tablix`, `dataset`, `parameters`, `header_footer`, `body`,
`styling`, `visibility`, `templates`, `embedded_images`, `page`, `chart`,
`actions`, `layout`). Multi-line bodies explain the *why* in a paragraph
under the subject.

Don't switch to full Conventional Commits — the project doesn't need
machine-parsed messages, and the hand-curated CHANGELOG beats anything
auto-generated.

## Reporting bugs

Use the **bug report** issue template. The template asks for:

- The failing RDL fragment (or a minimal reproduction).
- The exact tool call (JSON-RPC payload or Python equivalent).
- The actual vs expected XML diff, or the Report Builder error.

Reproductions live in `.rdl` files, so a fragment is the highest-signal
thing you can include.

## Security

See [SECURITY.md](SECURITY.md) for vulnerability disclosure. The threat
model is small (server reads/writes local files only, no network code,
no credentials handled), but please report through the channel listed
there rather than a public issue.
