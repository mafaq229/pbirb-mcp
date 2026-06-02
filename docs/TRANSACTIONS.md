# Transactions

v0.4.0 introduced two transaction styles for multi-edit RDL sessions:

1. **Stateful transactions** — `start_editing_transaction` /
   `commit_editing_transaction` / `cancel_editing_transaction`.
   Multi-turn editing where each tool call carries a `transaction_id`.
2. **Atomic batch** — `apply_edits(path, ops=[...])`. Single-call
   sequence; commits if every op succeeds, rolls back if any fails.

Both eliminate the per-edit parse + serialise + atomic-rename cost:
a 20-edit session that used to do 20 disk cycles now does 1.

## When to use which

| Pattern | Use this |
|---|---|
| The LLM is planning N edits up front, all knowable | `apply_edits` — one call, atomic, simpler error handling |
| The LLM wants to read state between edits (`get_textbox` mid-flight) | Stateful transaction — open, edit, read, edit, commit |
| The LLM wants to preview without ever touching disk | `dry_run_edit` (NOT a transaction — clones to tempfile, returns diff, discards) |
| The LLM wants to repair a stale-but-not-broken file | `restore_from_backup` after `backup_report` |

## Stateful transaction lifecycle

```
LLM → server:  start_editing_transaction({path: "/path/to/report.rdl"})
server → LLM:  {transaction_id: "abc123...", path: "...", expires_at: 1779042000.0}

LLM → server:  add_body_textbox({transaction_id: "abc123...",
                                 name: "Title", text: "Q1 2026", ...})
server → LLM:  {name: "Title", kind: "Textbox"}

LLM → server:  set_textbox_style({transaction_id: "abc123...",
                                  textbox_name: "Title", font_weight: "Bold"})
server → LLM:  {textbox: "Title", kind: "Textbox", changed: ["FontWeight"]}

LLM → server:  commit_editing_transaction({transaction_id: "abc123..."})
server → LLM:  {transaction_id: "abc123...", path: "...", saved: true,
                verify: {valid: true, issues: [...], rules_run: [...]}}
```

Up to the commit, the disk file is untouched. The commit:

1. Runs `lint_report` on the in-memory tree.
2. If any `severity: 'error'` issue surfaces → aborts with
   `saved: false` and leaves the transaction OPEN so the caller can
   fix the offending state and re-commit. Warnings don't abort.
3. On success, clears the in-transaction flag and calls `doc.save()`
   — the atomic `.tmp` + `os.replace` flow. One disk write.

`cancel_editing_transaction` discards the in-memory tree without
saving. Useful when the LLM realises mid-session that an approach
won't work.

### Timeout

Orphaned transactions (not committed or cancelled) auto-expire
after `PBIRB_MCP_TRANSACTION_TIMEOUT_S` seconds (default 600).
Expiry is lazy — swept on every transaction-aware dispatch call.
No background thread.

```bash
PBIRB_MCP_TRANSACTION_TIMEOUT_S=1800 pbirb-mcp  # 30-min sessions
```

## Atomic batch via `apply_edits`

```
LLM → server:  apply_edits({
                 path: "/path/to/report.rdl",
                 ops: [
                   {tool: "add_body_textbox", args: {...}},
                   {tool: "add_row_group", args: {...}},
                   {tool: "convert_to_matrix", args: {...}},
                 ]
               })
server → LLM:  {applied: [{tool: "add_body_textbox", ok: true, result: {...}},
                          {tool: "add_row_group", ok: true, result: {...}},
                          {tool: "convert_to_matrix", ok: true, result: {...}}],
                committed: true,
                verify: {valid: true, ...}}
```

Internally, `apply_edits`:

1. Opens an internal transaction via `start_editing_transaction`.
2. Dispatches each op through the JSON-RPC `tools/call` path with
   `transaction_id` injected. Each op's `args["path"]` (if present)
   is silently stripped — the dispatcher injects the registered
   abspath from the transaction.
3. On any op failure: cancels the transaction. Disk untouched.
4. On all-ok: commits. Lint runs against the in-memory tree before
   the atomic save.

The `applied` list always records every op attempted, including
the one that failed (if any). Use it to debug rollback causes.

## `apply_edits` vs `dry_run_edit`

| | `dry_run_edit` | `apply_edits` |
|---|---|---|
| Touches the real file? | **Never** — clones to a tempfile, discards | Yes, on success |
| Atomicity | N/A (no real-file mutation) | All-or-nothing |
| Returns | `{applied, diff, verify}` | `{applied, committed, verify}` |
| Use for | Preview a plan | Land the plan |

Common pattern: `dry_run_edit` first to preview the diff, inspect
`verify.issues`, then `apply_edits` to commit if it looks right.

## Edge cases

### Two transactions on the same path

`start_editing_transaction` refuses with `ValueError("an active
transaction already owns ...")`. Cancel or commit the first
before starting a second.

### Path normalisation

The registry keys on `Path(path).resolve()` — so `/tmp/foo.rdl`,
`/private/tmp/foo.rdl` (macOS symlink), and `./foo.rdl` (relative)
all normalise to the same canonical key. Subsequent edit tools
that pass `transaction_id` get routed to the canonical path
regardless of which form they originally used.

### Reading state during a transaction

`describe_report`, `get_textbox`, `get_tablixes`, etc. all read
through `RDLDocument.open(path)`. While a transaction owns the
path, those reads return the **in-memory in-flight tree** (NOT
disk). This is by design — consistent process-wide view of the
in-flight state. To explicitly read disk-only state, the caller
would need to bypass the registry (e.g., shell out to a separate
`pbirb-mcp` subprocess — each has its own registry).

### Transaction-id leakage in errors

Errors raised inside handlers that include `transaction_id` in
their kwargs would otherwise echo the id through `str(exc)`. The
dispatcher pops `transaction_id` BEFORE unpacking kwargs, so
handlers never see it. Errors can't reference it. The id is
opaque to user-visible output.

### Concurrency

The stdio MCP server processes one request at a time by protocol.
The registry is a plain dict — no locks. If you ever build a
multi-threaded handler dispatch, the in-memory tree shared inside
a transaction would need its own synchronisation. Out of scope
for v0.4.0.

## Quick reference

| Tool | Purpose | Returns |
|---|---|---|
| `start_editing_transaction(path)` | Open a transaction | `{transaction_id, path, expires_at}` |
| `commit_editing_transaction(transaction_id)` | Flush + save once | `{transaction_id, path, saved, verify}` |
| `cancel_editing_transaction(transaction_id)` | Discard | `{transaction_id, path, discarded}` |
| `apply_edits(path, ops)` | Atomic batch | `{applied, committed, verify}` |
| `dry_run_edit(path, ops)` | Preview without writing | `{applied, diff, verify}` |
| `backup_report(path)` | Snapshot | `{source, backup, size_bytes}` |
| `restore_from_backup(backup_path)` | Restore from snapshot | `{source, restored_to, bytes_restored}` |
