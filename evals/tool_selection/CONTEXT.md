# Tool-selection eval: context for the next agent

Read this before running or changing the eval. It records what the 2026-11-03 baseline found, how the runs
were done, and what to watch for when you re-run it after fixes.

The fix plan is the tracking issue **#113**: 95 issues in six milestones, M1–M6, from a full
empirical review of v0.4.1 @ `e1dbcce`. Issues #59 (EV-1) and #60 (EV-2) in that plan are about this eval itself.

## What it measures

Can a model, given a plain-English edit request and a report path, pick the right pbirb tools, call them
correctly, and leave the report in the requested state? The harness drives `MCPServer.handle_request`
in-process, the same `tools/call` path an MCP client uses. The grader then reads the edited `.rdl`
directly. There are 55 cases in 9 categories (data, parameters, tablix, style, layout, chart, interactivity,
question, decline), on synthetic fixtures in `fixtures/`. `CASES.md` lists them, and is generated from
`cases.py`.

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

### Why runs failed

All failures map to real tool gaps. None of them come from the harness.

| Pattern | Cases | Root cause | Issue |
|---|---|---|---|
| Model can't find which cell textbox is "the Asset Name column", so it gives up | t04, s09, i01, i02 (search arm) | Tools address cells by textbox name; `describe_report` doesn't list cell names, header text or bound fields | #59 |
| "Sort highest first" can't be done | t05 (search and subset arms) | `set_group_sort` has no direction and drops an existing `Descending` | #66 |
| Stops after `describe_report`: "no tool to remove a data source" | d01, d03, d06, i02, q03 (search arm) | BM25 doesn't surface the tool; descriptions are long and carry version notes | #56, #62 |
| Connection change done via `raw_xml_replace` instead of `set_datasource_connection` | d06 (all and subset arms) | The dedicated tool silently converts a PBIDATASET source to the legacy provider | #71 |
| Matrix corner empty after the documented flow | t06 (subset arm) | `convert_to_matrix`/`set_tablix_corner` don't build a real matrix | #31 |

### What the eval does not measure yet

This is important when reading the 97%. The grader checks only the property each case asked for. The
review found 19 critical bugs where a tool returns success but leaves the file structurally broken. Some
examples:
- tablix column-hierarchy leaves ≠ body columns;
- chart series ≠ hierarchy members;
- duplicate report-item names;
- XSD-invalid output committed as `valid: true`.

`validate_report` and `lint_report` pass on almost all of these. None of the 55 cases exercises those
paths: nested row groups, spans, subtotals on Report Builder–shaped tablixes, `add_column_group`, chart
series add/remove, `set_textbox_runs` on styled cells, a repeating header row, descending sort, rename.
#60 adds a structural gate and cases for each.

## Re-running

```bash
# from the repo root; needs ANTHROPIC_API_KEY in the environment
uv run --with anthropic==2.3.0 python -m evals.tool_selection.harness selftest     # offline, free
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
  only.
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
3. Once #60 lands, report the new `corrupted` count alongside success. A run that edits correctly but
   breaks structure must count as a failure.
