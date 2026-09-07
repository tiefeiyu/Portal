# Portal MCP Server — Incremental Read (`process_read_new`) Design Spec

## Overview

`process_read` reads records by a time window extending back from "now":
each call must be given a `duration`/`unit`, and repeated calls re-return
overlapping output. The agent is responsible for tracking time windows
itself, and the default 1s window silently misses older output (the
"hang check" guidance exists because of this).

This spec adds a new tool, **`process_read_new`**, that returns only the
records produced **since the last call to itself** — an incremental,
cursor-based read where the server remembers the read position per
process and per output source. The model just calls it repeatedly in a
loop; no time math, no duplicated output.

Existing tools, the session schema's other columns, and all current
tests remain unchanged.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Tool shape | New tool `process_read_new` | Keeps `process_read`'s time-window contract untouched; a separate tool is discoverable and its semantics writable in one description |
| Cursor ownership | Server-side, automatic | Model needs no extra argument; matches "read what's new since last read" |
| Cursor storage | `read_cursors TEXT NOT NULL DEFAULT '{}'` column on `processes`, JSON `{"1": n, "2": m}` (per source code) | Portal's state is all in the session DB; JSON TEXT columns already used for `args`/`env` (house pattern); cursor dies with the process row at cleanup |
| Cursor granularity | **One cursor per source** (`stdout`, `stderr`) | A shared cursor plus a `source` filter silently skips other-source records written between two narrow reads. Per-source cursors make any read pattern loss-free by construction |
| Cursor key | Implicit `rowid` of `proc_<id>` tables, ascending | Insertion order is the natural "new output" order; immune to equal-nanosecond timestamps (which would duplicate or skip records under a timestamp cursor) |
| Source param | `source: "stdout" \| "stderr" \| "both"` (default `both`), **no `stdin`** | User requirement: the tool must be filterable by source. `stdin` records are a write-log, not program output — excluded like `process_read`'s `both`; `process_read` remains available for them |
| Relationship to `process_read` | **Independent cursors** | Time-window reads keep their semantics; mixing them would silently swallow new output whenever a window read lands after an incremental read |
| `process_clear` | Resets cursors to `{}` | After `DELETE FROM proc_<id>`, SQLite reallocates rowids from 1 — stale high cursors would make new records invisible. Reset makes "clear, then read_new" mean "everything since the clear" |
| Atomicity | Cursor advance is one **atomic single-statement UPDATE** with per-source `MAX` against the stored JSON (`json_set`/`json_extract`), **conditioned on a clear-generation token** (`processes.read_gen`, bumped by `clear_records`) captured in the snapshot — on a mismatched generation the UPDATE is a no-op and the method re-snapshots and retries (bounded) | Two concurrent read_new calls could otherwise read-row/advance interleaved and regress a cursor; the `MAX`-guarded UPDATE is atomic so monotonicity holds. The generation token closes the read_new-vs-clear race for **all** snapshots (including first reads whose old cursor is 0): a clear landing between snapshot and advance can never leave a stale cursor re-raised past the reset, so post-clear records (rowids restart at 1) are never skipped |
| PTY processes | Same path, no special case | PTY records are also inserted via `db.insert_record` (chunk granularity, merged stderr into stdout); full-screen TUIs still use `process_screen` |
| Naming | `process_read_new` | Mirrors `process_read`; "new" == "since last read of that source" |

## Architecture

```
MCP Client (LLM)
       │ process_read_new(id, source="both")   (new)
       ▼
Portal MCP Server
  ├── Tool Handlers (server.py)
  │     ├── process_read_new (new)     → manager.read_new(id, source)
  │     └── existing tools (unchanged)
  ├── ProcessManager.read_new(id, source)
  │     ├── validate process exists    (same error as process_read)
  │     ├── touch(id)                  (idle-timer reset, same as read)
  │     └── db.read_new_records(...)   → (records, next_cursors)
  ├── Database
  │     ├── processes.read_cursors     (new JSON column, default '{}')
  │     ├── processes.read_gen         (new clear-generation counter, default 0)
  │     ├── read_new_records(proc_id, source_codes)
  │     │     SELECT read_cursors, read_gen FROM processes
  │     │     SELECT rowid, timestamp, source, content
  │     │     FROM proc_<id>
  │     │     WHERE (source = 1 AND rowid > :c1)
  │     │        OR (source = 2 AND rowid > :c2)     ← only requested codes
  │     │     ORDER BY rowid
  │     │     → per-source next cursors = max returned rowid per code
  │     │     UPDATE processes SET read_cursors = json_set(
  │     │         read_cursors, '$."1"', MAX(COALESCE(json_extract(
  │     │             read_cursors, '$."1"'), 0), :next1), ...)
  │     │     WHERE id = :pid AND read_gen = :snap_gen
  │     │     ← one atomic statement; MAX vs current stored value makes
  │     │       concurrent calls monotonic; the generation condition
  │     │       makes a clear racing the snapshot a no-op (rowcount 0 →
  │     │       re-snapshot and retry, bounded; never re-raises a
  │     │       reset cursor, so post-clear records are never skipped)
  │     └── clear_records(proc_id)     (resets read_cursors = '{}' AND
  │                                     bumps read_gen; also drops the
  │                                     stale proc_<id> tables at
  │                                     initialize())
  └── (registry, ansi.py, process.py, pty_process.py — unchanged)
```

### Cursor semantics (the contract)

1. A call returns records with `rowid > <cursor of that source>` for the
   requested source codes, in insertion order, then sets each requested
   source's cursor to the **max rowid returned for it** (not the table
   max) — records inserted concurrently during the read have higher
   rowids and are picked up by the next call; nothing is skipped or
   duplicated.
2. Only the cursors of the **requested sources** advance. `source=stdout`
   never touches the stderr cursor and vice versa — a narrow read can
   never cause a record of another source to fall behind the cursor.
   `source=both` advances both.
3. `process_read` never moves these cursors; `process_clear` resets
   them to `{}`.
4. `source` accepts `stdout | stderr | both`, default `both`. `stdin` is
   rejected (`ValueError`) — it's a write-log, not output; `process_read`
   remains available for it.
5. Cursor state is gone on restart, exactly like all other session DB
   state (the DB is per-instance and recreated on startup).

## MCP Tool Change

### `process_read_new` (new)

```
Input:  id (required, integer) — internal process ID, same as process_read
        source (optional, enum ["stdout", "stderr", "both"], default "both")
Output: same format as process_read:
        "[<timestamp>] [STDIN/STDOUT/STDERR] <content>" joined lines;
        "No new output." when all requested cursors are caught up
```

Tool description (drafted):

> Read new output from a process — records produced since the last call
> to this tool for the requested source. The server remembers the read
> position per process and per source; there is no time window, so
> repeated calls return each record exactly once, in insertion order.
> Only the requested source's cursor advances — reading stdout never
> causes stderr records to be skipped, and vice versa. Resets the
> process idle timer, like process_read.
>
> `process_read` (time-window) does not affect this tool's cursors;
> `process_clear` resets them (next call returns everything since the
> clear). PTY processes: stderr is merged into stdout and records are
> arbitrary chunks, not lines — same caveats as `process_read`. For
> full-screen TUIs the record stream is garbled fragments — use
> `process_screen`.

## Implementation Sketch

### `portal_mcp/database.py`

- `CREATE TABLE processes (...)` gains
  `read_cursors TEXT NOT NULL DEFAULT '{}'` (JSON: `{"1": n, "2": m}`)
  and `read_gen INTEGER NOT NULL DEFAULT 0` (clear-generation counter).
- `initialize()`: defensive `ALTER TABLE ... ADD COLUMN` for both
  columns if a stale DB file without them is opened directly (normal
  `create_server` startup unlinks the file first, so this is purely a
  safety net for direct `Database()` use in tests); leftover stale
  `proc_<id>` record tables from previous runs are dropped (their
  process rows were just deleted — keeps the safety-net path from
  surfacing previous-session records).
- New `read_new_records(proc_id, source_codes: list[int]) -> tuple[
  records, next_cursors]` (cursors and generation are read from the DB
  internally, not passed by the caller):
  - empty `source_codes` → `ValueError`; unknown process → `ValueError`.
  - parse cursor JSON + snapshot `read_gen` → build the `OR`-per-source
    predicate → `SELECT rowid, timestamp, source, content ... ORDER BY
    rowid` → `next_cursors` = per-source max of returned rowids (the
    previous cursor value when a source returned no rows) → single
    atomic `UPDATE processes SET read_cursors = json_set(... MAX(
    json_extract(...), :next) ...) WHERE id = :pid AND read_gen =
    :snapshot_gen` (per-source `MAX` — monotonic under concurrent
    calls; generation condition — a clear racing the snapshot makes the
    UPDATE a no-op, rowcount 0).
  - rowcount 0 → re-snapshot and retry (bounded, 5 attempts); exhausts →
    `RuntimeError` (a clear kept racing; no safe advance possible).
- `clear_records(proc_id)`: after the DELETE, set `read_cursors = '{}'`
  and `read_gen = read_gen + 1` in one UPDATE.

### `portal_mcp/manager.py`

- New `read_new(proc_id, source="both")`:
  validate process exists (`ValueError("Process N not found")`, same as
  `read`) → reject `stdin` via `_source_to_codes`-equivalent (codes
  `[1]`, `[2]`, `[1,2]` only) → `touch(proc_id)` →
  `db.read_new_records(...)` → return records with
  `timestamp/source/content` keys (same shape as `read`, for reuse of
  the `source_label` rendering).

### `portal_mcp/server.py`

- New tool entry in `handle_list_tools` and branch in
  `handle_call_tool`; render identical to `process_read` (reuse its
  label/join logic); "No new output." when empty.
- MCP instructions text: the "Typical workflow" gains a mention of
  `process_read_new` as the loop-friendly alternative to `process_read`
  (e.g. step 2: "`process_read_new` — check what's new since the last
  read (or `process_read` with a time window)"; plus a one-line note in
  the hang-check section that `timeout_ms` hangs are diagnosed with a
  generous `process_read` window, not `process_read_new`).

### Docs

- `README.md` + `README_zh.md`: add `process_read_new` to the tool
  tables/sections and the typical workflow.
- `llms-install.md`: no change (no new dependency).

## Error Handling & Edge Cases

| Scenario | Behavior |
|----------|----------|
| Unknown process id | Same error as `process_read`: `ValueError("Process N not found")` |
| `source` = `stdin` | `ValueError` — not part of the enum; write-log only |
| Process cleaned up | Cursors are gone with the process — nothing special |
| `process_clear` then new output | Cursors reset to `{}`; first `read_new` returns everything since the clear |
| Records written during a read | Rowids higher than `next_cursors`; returned by the next call (cursors = returned max, not table max) |
| Concurrent `read_new` calls | Atomic `MAX`-guarded UPDATE: cursors advance monotonically (no regression); each caller returns exactly one snapshot — a given record is returned at most once per caller |
| `process_clear` racing a `read_new` snapshot | Generation token: the stale advance UPDATE no-ops (rowcount 0) and the call retries from the post-clear state — a reset cursor is never re-raised, so records written after the clear (rowids restart at 1) are never skipped; bounded retries, `RuntimeError` if clears race indefinitely |
| Narrow reads (`stdout` then `stderr`) | Per-source cursors: no other-source record ever falls behind a cursor |
| Equal-nanosecond records | Rowid order is total and stable — no duplicates or skips; `process_read`'s timestamp order is unaffected |
| PTY chunk records | Returned as stored (chunks, merged stderr in stdout) — same caveats as `process_read` |
| Empty result | "No new output." — not an error; cursors unchanged |
| Idle timeout | Same as `process_read`: the call `touch`es the process |

## Testing

**`tests/test_database.py`** (extend):
1. `read_new_records` returns only rows above the per-source cursor, ascending, filtered to requested source codes (stdin rows stored but never returned by read_new).
2. Cursor advance: per-source cursors persist and are independent — advancing stdout leaves stderr untouched.
3. `clear_records` resets `read_cursors` to `{}`; subsequent inserts are returned from the start again (guards the rowid-reuse case).
4. `next_cursors` equals max returned rowid per source; equals input cursors for sources with no rows.
5. `processes` table has `read_cursors` column with default `'{}'`; stale-DB `ALTER TABLE` safety net works.

**`tests/test_manager.py`** (extend):
1. `read_new` twice in a row: disjoint records; second call empty → "no output" (records arrive between calls via a child that prints, sleeps, prints).
2. Narrow-read loss-free interleave: child writes stdout, stderr, stdout; `read_new(source="stderr")` then `read_new(source="stdout")` — the first stdout record is still returned (per-source cursors).
3. `read_new` default (`both`) advances both cursors.
4. `process_read` (time-window) does not move the `read_new` cursors.
5. Unknown id raises `ValueError`; `source="stdin"` raises `ValueError`.
6. Clear then `read_new`: returns records inserted after the clear.
7. `read_new` touches the idle timer (inactive_duration_ms resets — assert via `process_inspect`).

**`tests/test_server.py`** (extend):
1. `process_read_new` is listed in tools; call with `{id}` (and with `{id, source}`) returns the expected format; empty case returns "No new output.".

Spawned test executables: `sys.executable` with `-c` sleep scripts and
the existing conftest polling helpers (`wait_for_output`), consistent
with the current suite.

## File Changes

```
Portal/
├── portal_mcp/
│   ├── server.py           # + process_read_new tool + instructions text edit
│   ├── manager.py          # + read_new()
│   └── database.py         # + read_cursors column, read_new_records(),
│                           #   clear_records cursor reset
├── README.md / README_zh.md  # + process_read_new section
└── tests/
    ├── test_database.py    # + cursor tests
    ├── test_manager.py     # + read_new semantics tests
    └── test_server.py      # + tool registration/call test
```

## Out of Scope

- Client-supplied cursor / marker parameters — server-side cursors only
  (user decision).
- `source=stdin` for `process_read_new` — `process_read` covers it.
- Cursor persistence across restarts — session DB is per-instance and
  rebuilt on startup; the same holds for all other session state.
- TUI screen diffs — `process_screen` remains the tool for full-screen
  programs.
- Changing `process_read`, `process_clear`, or any existing tool's
  contract (only the reset side effect on `process_clear`).
