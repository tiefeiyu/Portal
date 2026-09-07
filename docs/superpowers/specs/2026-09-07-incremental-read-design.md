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
process. The model just calls it repeatedly in a loop; no time math, no
duplicated output.

Existing tools, the session schema's other columns, and all current
tests remain unchanged.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Tool shape | New tool `process_read_new` | Keeps `process_read`'s time-window contract untouched; a separate tool is discoverable and its semantics writable in one description |
| Cursor ownership | Server-side, automatic | Model needs no extra argument; matches "read what's new since last read" phrasing in the request |
| Cursor storage | `read_cursor INTEGER NOT NULL DEFAULT 0` column on `processes` | Portal's state is all in the session DB; DB-level code is unit-testable (house pattern — see PTY spec Decision: registry/session storage). One column per process, zero extra tables, cursor dies with the process row at cleanup |
| Cursor key | Implicit `rowid` of `proc_<id>` tables, ascending | Insertion order is the natural "new output" order; immune to equal-nanosecond timestamps (which would duplicate or skip records under a timestamp cursor) |
| Cursor scope | One cursor per process, covering stdout+stderr | Per-source cursors add state for a case the agent doesn't need; a filtered `source` param is omitted — see Output Model |
| Relationship to `process_read` | **Independent cursor** | Time-window reads keep their semantics; mixing them would silently swallow new output whenever a window read lands after an incremental read |
| `process_clear` | Resets cursor to 0 | After `DELETE FROM proc_<id>`, SQLite reallocates rowids from 1 — a stale high cursor would make new records invisible. Reset makes "clear, then read_new" mean "everything since the clear" |
| PTY processes | Same path, no special case | PTY records are also inserted via `db.insert_record` (chunk granularity, merged stderr); read_new returns them in chunk order — full-screen TUIs still use `process_screen` |
| Naming | `process_read_new` | Mirrors `process_read`; "new" == "since last read" |

## Architecture

```
MCP Client (LLM)
       │ process_read_new(id)          (new; id only — no time args)
       ▼
Portal MCP Server
  ├── Tool Handlers (server.py)
  │     ├── process_read_new (new)     → manager.read_new(id)
  │     └── existing tools (unchanged)
  ├── ProcessManager.read_new(id)
  │     ├── validate process exists    (same error as process_read)
  │     ├── touch(id)                  (idle-timer reset, same as read)
  │     └── db.read_new_records(...)   → (records, next_cursor)
  ├── Database
  │     ├── processes.read_cursor      (new column, default 0)
  │     ├── read_new_records(proc_id, after_rowid)
  │     │     SELECT rowid, timestamp, source, content
  │     │     FROM proc_<id> WHERE rowid > ? AND source IN (1,2)
  │     │     ORDER BY rowid
  │     ├── advance_cursor(proc_id, rowid)
  │     │     UPDATE processes SET read_cursor = MAX(read_cursor, ?)
  │     └── clear_records(proc_id)     (now also resets read_cursor = 0)
  └── (registry, ansi.py, process.py, pty_process.py — unchanged)
```

### Cursor semantics (the contract)

1. A call returns every stdout/stderr record with `rowid > read_cursor`,
   in insertion order, then sets `read_cursor` to the **max rowid of the
   records it returned** (not the table max) — records inserted
   concurrently during the read have higher rowids and are picked up by
   the next call; nothing is skipped or duplicated.
2. `UPDATE ... SET read_cursor = MAX(read_cursor, ?)` makes concurrent
   calls monotonic.
3. `process_read` never moves this cursor; `process_clear` resets it.
4. There is no `source` argument: the tool always returns stdout+stderr
   merged (the same set `process_read`'s default `both` covers; stdin
   log records are excluded, consistent with `both`). The cursor tracks
   the returned stdout/stderr records only — a stdin-only tail leaves
   the cursor where it was, which is safe (stdin records are never
   returned by this tool; nothing is skipped or duplicated).
5. Cursor state is gone on restart, exactly like all other session DB
   state (the DB is per-instance and recreated on startup).

## MCP Tool Change

### `process_read_new` (new)

```
Input:  id (required, integer) — internal process ID, same as process_read
Output: same format as process_read:
        "[<timestamp>] [STDIN/STDOUT/STDERR] <content>" joined lines;
        "No new output." when the cursor has caught up
```

Tool description (drafted):

> Read new output from a process — records produced since the last call
> to this tool. The server remembers the read position; there is no time
> window, so repeated calls return each record exactly once, in insertion
> order. Resets the process idle timer, like process_read.
>
> `process_read` (time-window) does not affect this tool's cursor;
> `process_clear` resets it (next call returns everything since the
> clear). PTY processes: stderr is merged into stdout and records are
> arbitrary chunks, not lines — same caveats as `process_read`. For
> full-screen TUIs the record stream is garbled fragments — use
> `process_screen`.

## Implementation Sketch

### `portal_mcp/database.py`

- `CREATE TABLE processes (...)` gains `read_cursor INTEGER NOT NULL DEFAULT 0`.
- `initialize()`: defensive `ALTER TABLE processes ADD COLUMN read_cursor ...`
  if a stale DB file without the column is opened directly (normal
  `create_server` startup unlinks the file first, so this is purely a
  safety net for direct `Database()` use in tests).
- New `read_new_records(proc_id, after_rowid)` → `(records, next_cursor)`:
  one SELECT returning `rowid, timestamp, source, content` for
  `rowid > ? AND source IN (1,2) ORDER BY rowid`; `next_cursor` is the
  last row's rowid, or `after_rowid` when no rows.
- New `advance_cursor(proc_id, rowid)`:
  `UPDATE processes SET read_cursor = MAX(read_cursor, ?) WHERE id = ?`.
- `clear_records(proc_id)`: after the DELETE, set `read_cursor = 0`.

### `portal_mcp/manager.py`

- New `read_new(proc_id)`:
  get process (raise `ValueError("Process N not found")` on miss, same
  as `read`) → `touch(proc_id)` → `db.read_new_records(proc_id,
  cursor)` → `db.advance_cursor(proc_id, next_cursor)` → return
  `(records, moved)` records list with `timestamp/source/content` keys
  (same shape as `read`, for reuse of the `source_label` rendering).

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
| Process cleaned up | Cursor row is gone with the process — nothing special |
| `process_clear` then new output | Cursor reset to 0; first `read_new` returns everything since the clear |
| Records written during a read | Rowids higher than `next_cursor`; returned by the next call (cursor is the returned max, not the table max) |
| Concurrent `read_new` calls | `MAX(read_cursor, ?)` is monotonic; each record still returned exactly once (the advancing call wins) |
| Equal-nanosecond records | Rowid order is total and stable — no duplicates or skips; `process_read`'s timestamp order is unaffected |
| PTY chunk records | Returned as stored (chunks, merged stderr) — same caveats as `process_read` |
| Empty result | "No new output." — not an error; cursor unchanged |
| Idle timeout | Same as `process_read`: the call `touch`es the process |

## Testing

**`tests/test_database.py`** (extend):
1. `read_new_records` returns only rows above the given rowid, ascending, source-filtered to stdout/stderr (stdin rows stored but not returned).
2. Cursor advance: `advance_cursor` persists; `MAX(read_cursor, ?)` never moves backward.
3. `clear_records` resets `read_cursor` to 0; subsequent inserts are returned from the start again (guards the rowid-reuse case).
4. `next_cursor` equals max returned rowid; equals `after_rowid` for empty reads.
5. `processes` table has `read_cursor` column with default 0; stale-DB `ALTER TABLE` safety net works.

**`tests/test_manager.py`** (extend):
1. `read_new` twice in a row: disjoint records; second call empty → no output (records arrive between calls via a child that prints, sleeps, prints).
2. `process_read` (time-window) does not move the `read_new` cursor — a same-call `read` + `read_new` still returns the full new output.
3. Unknown id raises `ValueError`.
4. Clear then `read_new`: returns records inserted after the clear.
5. `read_new` touches the idle timer (inactive_duration_ms resets — assert via `process_inspect`).

**`tests/test_server.py`** (extend):
1. `process_read_new` is listed in tools; call with `{id}` returns the expected format; empty case returns "No new output.".

Spawned test executables: `sys.executable` with `-c` sleep scripts and
the existing conftest polling helpers (`wait_for_output`), consistent
with the current suite.

## File Changes

```
Portal/
├── portal_mcp/
│   ├── server.py           # + process_read_new tool + instructions text edit
│   ├── manager.py          # + read_new()
│   └── database.py         # + read_cursor column, read_new_records(),
│                           #   advance_cursor(), clear_records reset
├── README.md / README_zh.md  # + process_read_new section
└── tests/
    ├── test_database.py    # + cursor tests
    ├── test_manager.py     # + read_new semantics tests
    └── test_server.py      # + tool registration/call test
```

## Out of Scope

- Client-supplied cursor / marker parameters — server-side cursor only
  (user decision).
- Per-source cursors.
- Cursor persistence across restarts — session DB is per-instance and
  rebuilt on startup; the same holds for all other session state.
- TUI screen diffs — `process_screen` remains the tool for full-screen
  programs.
- Changing `process_read`, `process_clear`, or any existing tool's
  contract (only the reset side effect on `process_clear`).
