# Tool-selection eval: context for the next agent

Read this before running or changing the eval. It records what the 2026-11-03 baseline found, how the runs
were done, and what to watch for when you re-run it after fixes.

The fix plan is the tracking issue **#113**: 95 issues in six milestones, M1–M6, from a full
empirical review of v0.4.1 @ `e1dbcce`. Issues #59 (EV-1) and #60 (EV-2) in that plan are about this eval itself.

## What it measures

Can a model, given a plain-English edit request and a report path, pick the right pbirb tools, call them
correctly, and leave the report in the requested state? The harness drives `MCPServer.handle_request`
in-process, the same `tools/call` path an MCP client uses. The grader then reads the edited `.rdl`
directly, and a structural gate fails any edit that left the file broken (see "Structural gate" below).
There are 65 cases in 9 categories (data, parameters, tablix, style, layout, chart, interactivity,
question, decline), on synthetic fixtures in `fixtures/`. `CASES.md` lists them, and is generated from
`cases.py` by `harness.py cases`.

Fixtures (`fixtures/build_fixtures.py`):

| File | What it is |
|---|---|
| `fleet_utilization.rdl` | the original fixture: DAX dataset, two unused data sources, String-typed date parameters, title, flat detail table, one-series column chart |
| `fleet_rb.rdl` | the same report after a Report Builder session (added for #60): styled header row, detail rows inside a `Site` group with no group-header row, a static grand-total row (`KeepWithGroup=Before`), a two-series chart, and the `Site` parameter used in the title, a table filter, a query parameter and the prompt layout |
| `bare_action_under_textbox.rdl` | copy of `tests/fixtures/known_bad/`; XSD-invalid on purpose (q03) |

`build_fixtures.py rb` rebuilds only `fleet_rb.rdl`, from the committed `fleet_utilization.rdl`.
`build_fixtures.py` (or `all`) also rebuilds `fleet_utilization.rdl`, which mints new DataSourceID /
ReportID GUIDs and so changes every case's starting file; avoid that unless the fixture really has to
change.

There are three arms, i.e. three ways of offering the tools to the model:

| Arm | What the model sees |
|---|---|
| `all` | every tool definition up front (~100 KB, ~25k tokens), the way MCP clients do it today |
| `search` | everything deferred behind Anthropic's BM25 tool search, except `describe_report` |
| `subset` | only the case's category tools plus common readers; a ceiling on how easy tool choice can get |

## Baseline: 2026-11-03

Run with `claude-haiku-5-5` at effort medium, 55 cases × 2 reps per arm. The self-test was green and there
were 0 harness errors. Cost was about $0.77 in total at the harness's price table (`PRICES` in `harness.py`).
The provider console is the source of truth for real spend.

| Variant | Arm | Success | Avg tool calls | Cache-read tokens | Cache-write tokens | Output tokens |
|---|---|---|---|---|---|---|
| `baseline` | all | 97.3% ±3.0 | 4.5 | 16.7M | 0.59M | 166k |
| `v1` | search | 87.3% ±6.2 | 3.5 (+1.8 searches) | 1.75M (+0.61M uncached) | 0.67M | 140k |
| `v2` | subset | 97.3% ±3.0 | 4.9 | 3.9M | 0.46M | 146k |

Results are in `runs/<variant>/results.jsonl`, with per-case transcripts in `runs/<variant>/traces/`.
All three are committed (110 rows each; re-checked on 2026-11-05: 97.3 / 87.3 / 97.3%), so regressions
are measurable. They predate the structural gate and the 10 new cases: their rows have no `corrupted`
field (summaries print `corrupted n/a`), and they cover only the original 55 case ids.

### Why runs failed

All failures map to real tool gaps. None of them come from the harness.

| Pattern | Cases | Root cause | Issue |
|---|---|---|---|
| Model can't find which cell textbox is "the Asset Name column", so it gives up | t04, s09, i01, i02 (search arm) | Tools address cells by textbox name; `describe_report` doesn't list cell names, header text or bound fields | #59 |
| "Sort highest first" can't be done | t05 (search and subset arms) | `set_group_sort` has no direction and drops an existing `Descending` | #66 |
| Stops after `describe_report`: "no tool to remove a data source" | d01, d03, d06, i02, q03 (search arm) | BM25 doesn't surface the tool; descriptions are long and carry version notes | #56, #62 |
| Connection change done via `raw_xml_replace` instead of `set_datasource_connection` | d06 (all and subset arms) | The dedicated tool silently converts a PBIDATASET source to the legacy provider | #71 |
| Matrix corner empty after the documented flow | t06 (subset arm) | `convert_to_matrix`/`set_tablix_corner` don't build a real matrix | #31 |

### What the baseline did not measure

This is important when reading the 97%. When it ran, the grader checked only the property each case asked
for, plus `verify_report`'s `valid` flag, and lint caught almost none of the structural damage then. The
review found 19 critical bugs where a tool returns success but leaves the file structurally broken, e.g.
tablix column-hierarchy leaves ≠ body columns, chart series ≠ hierarchy members, duplicate report-item
names, XSD-invalid output committed as `valid: true`. None of the 55 cases exercised those paths. #60 added
the structural gate and the cases below.

## Structural gate (#60)

`grade()` runs `structural_violations(report, fixture)` on every case whose file changed:

- **What counts:** every error-severity finding of `validate_report` (parse, XSD) and of `lint_report`
  (all rules). That includes `tablix-structure` (hierarchy leaves vs body columns/rows, cells per row,
  spans, header depth, corner: `core/tablix_headers.shape_findings`), `chart-structure` (series vs
  series-hierarchy leaves, data points vs category leaves: `core/chart_shape.hierarchy_findings`),
  `duplicate-name` (report-wide name registry), dangling references, and the rest. Warnings don't count.
  If any rule in `GATE_RULES` stops running (renamed, dropped), the gate raises instead of quietly losing
  coverage, and the case lands in `errors.jsonl`.
- **Baseline-aware:** only errors the starting fixture did not already have count, matched as a multiset
  of `validate.issue_key` (the identity `verify_report(baseline_path=...)` uses). A pre-existing error is
  not corruption; a second copy of it is.
- **Scoring:** `success` needs the case's own check *and* a clean gate. `grade.corrupted` is 1.0 when the
  edit introduced any error, whether or not the check passed. `explanation.check` keeps the check's own
  verdict and `explanation.corrupted` lists up to five violations. `summarize()` prints
  `corrupted k/n` next to success.
- **Selftest:** besides oracle / null / wrong, a `corrupt` mode replays `CORRUPT` solutions that pass their
  case's check but break the file, one per gate component: tablix-structure (t03), chart-structure (c05),
  duplicate-name (s07) and XSD (l06). Each must keep `check == "ok"` and still fail with `corrupted == 1`.
  The oracle must also leave every case uncorrupted.

### Cases for the paths the review broke

| Path | Case | Fixture | Oracle |
|---|---|---|---|
| nested row groups | t08 | fleet | `add_row_group` ×2 (inner first; each wraps the whole hierarchy) |
| spans | t09 | fleet | `add_static_row` → `set_cell_span` |
| subtotals on a Report Builder–shaped tablix | t10 | fleet_rb | `add_subtotal_row` on the header-less `Site` group, grand total kept last |
| `add_column_group` | t11 | fleet | `add_column_group` |
| chart series add/remove | c07, c08 | fleet, fleet_rb | `add_chart_series` → `remove_chart_series`; `remove_chart_series` |
| `set_textbox_runs` on a styled cell | s10 | fleet_rb | `set_textbox_runs` keeping the header's run, box and paragraph style |
| repeating header row | t12 | fleet_rb | **known gap #67** |
| descending sort | t13 | fleet | **known gap #66** |
| rename | p09 | fleet_rb | `rename_parameter` (title, table filter, query parameter, prompt layout) |

- **Known gaps.** `Case.known_gap` marks a request no tool can do correctly yet: t12 (no tool sets
  `RepeatOnNewPage` on the static header member) and t13 (`set_group_sort` has no direction; negation can't
  reverse a text sort). They have no oracle, which `selftest` skips, but it still checks that doing nothing
  fails them. Both checks are reachable today only through `raw_xml_replace`. In real runs they count as
  failures, and `summarize()` prints `known-gap cases passed k/n`. When #66 / #67 land, add an oracle and
  drop `known_gap`.
- **t08 is lenient on purpose.** `add_row_group` also wraps the column-header row, so the header repeats per
  group (#65, M5). t08 checks only the nesting. Tighten it when #65 lands.
- **Comparing with the baseline.** New runs have 65 cases, and two of them are known gaps (so the ceiling is
  96.9% until #66/#67 land). Compare against `baseline` / `v1` / `v2` on the original 55 ids (pass them with
  `--cases`, or filter `results.jsonl`), and report the new ten separately.

## Re-running

```bash
# from the repo root; needs ANTHROPIC_API_KEY in the environment (selftest and cases don't)
uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness selftest     # offline, free; CI runs it
uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness cases        # regenerate CASES.md
uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness run \
    --variant v3 --model claude-haiku-5-5 --arm all --reps 2 --concurrency 6
```

### Budget and model policy (from the repo owner)

- **Use Haiku only** unless asked otherwise. Don't add Sonnet or Opus comparison arms on your own initiative.
- **A stated budget is a ceiling, not a target.** Calibrate on a few cases first
  (`--reps 1 --cases t01,l04,c02,s03,q02` cost $0.05), then run only what answers the question.
- **Cost by arm.** The `all` arm costs about 10× the tokens of `search`, because ~41k tokens of tool
  definitions are re-sent every turn (prompt-cached).

### Pitfalls hit while producing this baseline

- **One variant per model × arm.** Results are keyed by `(case, rep)` inside `runs/<variant>/`, and resumed
  runs skip keys that already exist. Running a second arm into the same variant silently skips every case.
  Variant names must match `baseline|v<N>`. Record model and arm in `runs/<variant>/change.md`, which
  `harness.py` writes for non-baseline variants.
- **Harness approval.** `runs/_state.json` stores a hash of `harness.py`, `cases.py` and
  `fixtures/build_fixtures.py`. Any edit to those, including formatter or pre-commit changes, makes `run`
  exit with "harness files changed since last approval". Review the diff, then pass `--approve-harness`
  once. The hash was refreshed on 2026-11-03 after pre-commit's ruff-format pass, which was whitespace
  only, and again on 2026-11-05 for #60 (structural gate, 10 cases, `build_fleet_rb`), after reviewing that
  diff. That refresh ran `check_harness(approve=True)` directly, because `run --approve-harness` starts a
  paid run straight after storing the hash. `selftest` and `cases` never check the hash.
- **`ANTHROPIC_BASE_URL`.** Inside a Claude Code session this is set to the session's own proxy. Unset it
  (and `ANTHROPIC_AUTH_TOKEN`) before running, or requests go to the wrong endpoint. The harness also
  raises `ServedModelMismatch` if the served model differs from `--model`.
- **Shell word-splitting.** In zsh, `set -- $spec` does not split, so `--arm` came through empty and every
  run died in argparse. Write the arguments out explicitly in loops.
- **Pre-commit on `runs/`.** The end-of-file fixer adds a newline to every trace JSON. That's harmless, but
  expect hundreds of touched files on the first commit of new runs.

## After fixes land

1. Re-run all three arms as new variants (`v3` all, `v4` search, `v5` subset) with the same model and reps,
   and compare against the table above.
2. Expected movement:
   - `search` should close most of its 10-point gap after #56/#62 (descriptions, toolsets).
   - t04/s09/i01/i02 should pass after #59.
   - t05 should pass after #66.
   - d06 should pass after #71.
   - t12 / t13 become passable after #67 / #66 (then give them oracles and drop `known_gap`).
3. Report the `corrupted` count alongside success. A run that edits correctly but breaks structure already
   counts as a failure. The committed baseline predates the gate, so re-run it once the critical fixes
   land (#60's acceptance asks for that); that is a paid run and needs the owner's go-ahead.
