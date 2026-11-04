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

1. Refuses (`saved: false`, `error: "file changed on disk …"`,
   transaction left OPEN) if the file changed on disk since the
   transaction started — e.g. Report Builder saved it meanwhile.
   Pass `force: true` to overwrite those changes deliberately.
2. Verifies the in-memory tree: structural checks + the bundled RDL
   XSD + every lint rule.
3. Compares against the **baseline** recorded at
   `start_editing_transaction`. Each issue carries `new: true|false`;
   `verify` adds `introduced_count`, `preexisting_count` and
   `introduced_error_count`. Only a NEW `severity: 'error'` issue
   aborts (`saved: false`, transaction left OPEN so the caller can
   fix it and re-commit). Errors already in the file don't block an
   unrelated edit; warnings never abort. `verify.valid` stays honest —
   it's `false` while any error remains, old or new.
4. On success, clears the in-transaction flag and calls `doc.save()`
   — the atomic `.tmp` + `os.replace` flow. One disk write. If that
   save fails (permissions, disk full) the error is returned, the
   transaction stays open and in-memory, and the commit can be
   retried — or cancelled with the disk untouched.

Every op routed through a transaction is snapshotted first: an op
that raises after a partial mutation is rolled back, so a half-applied
op is never committed.

`cancel_editing_transaction` discards the in-memory tree without
saving. Useful when the LLM realises mid-session that an approach
won't work.

### Timeout

Orphaned transactions (not committed or cancelled) auto-expire
after `PBIRB_MCP_TRANSACTION_TIMEOUT_S` seconds (default 600).
Expiry is lazy — swept on every registry lookup (every `open` of a
file, every routed call, commit and cancel). No background thread.
An expired transaction can't be committed; edits without its id go
straight to disk again.

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

1. Checks every op up front. Transaction control
   (`start_/commit_/cancel_editing_transaction`), the batch tools
   themselves (`apply_edits`, `dry_run_edit`) and tools that write
   outside the batch's tree (`create_report`, `duplicate_report`,
   `backup_report`, `restore_from_backup`) are refused with
   `{"error_type": "OpNotAllowed", "tool", "op_index"}` before
   anything is opened. The same allow-list applies to `dry_run_edit`.
2. Opens an internal transaction via `start_editing_transaction`.
3. Dispatches each op through the JSON-RPC `tools/call` path with
   `transaction_id` injected. Each op's `args["path"]` (if present)
   is silently stripped — the dispatcher injects the registered
   abspath from the transaction.
4. On any op failure (an `isError`, or a result reporting
   `committed: false` / `saved: false`): cancels the transaction.
   Disk untouched.
5. On all-ok: commits — XSD + lint against the in-memory tree, new
   errors only (see above), then the atomic save.

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

`start_editing_transaction` refuses with
`TransactionConflictError("an active transaction already owns ...")`.
Cancel or commit the first before starting a second. A hardlink to a
held file is the same file (the registry also keys on
`(st_dev, st_ino)`), so it's refused too.

### Path normalisation

The registry keys on `Path(path).resolve()` — so `/tmp/foo.rdl`,
`/private/tmp/foo.rdl` (macOS symlink), and `./foo.rdl` (relative)
all normalise to the same canonical key. Subsequent edit tools
that pass `transaction_id` get routed to the canonical path
regardless of which form they originally used.

### Reading state during a transaction

Transactions are isolated. A call that carries the transaction's
`transaction_id` reads and edits the **in-memory in-flight tree**;
`validate_report` / `verify_report` / `dry_run_edit` called that way
check or preview that tree too. A call WITHOUT the id reads the
committed (on-disk) state, and any attempt to write the held path
without the id — an edit tool, `restore_from_backup`, … — fails with
`TransactionConflictError` and leaves the file unchanged.
`duplicate_report` works either way and never touches the source's
ids.

Every tool that works on a report file declares the optional
`transaction_id` parameter in its `inputSchema`.

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
