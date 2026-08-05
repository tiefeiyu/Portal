# Portal MCP Server — PTY Support Design Spec

## Overview

Portal currently spawns subprocesses with OS pipes (`asyncio.create_subprocess_exec`), so programs that check `isatty()` (ssh, gdb, psql, interactive REPLs) change behavior, and full-screen TUIs (vim, htop) are unusable. This spec adds **virtual PTY support** so Portal can drive the full range of interactive programs, plus a **program registry** that accumulates which programs need a PTY.

Existing pipe mode, its 10 tools, the SQLite schema, and all current tests remain unchanged.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Enablement | `pty: bool = false` parameter on `process_start` | Pipe mode stays default; zero breakage for existing callers |
| PTY backend (Windows) | `pywinpty>=3.0.5` (ConPTY) | Official ConPTY binding (winpty-rs), fixed release with wheels for Python 3.13 |
| PTY backend (POSIX) | `ptyprocess` | Same API shape as pywinpty (by design), pure Python, no hard deps |
| Backend abstraction | Thin `spawn()` dispatch in `pty_backend.py` (~30 lines) | Both backends deliberately mirror each other's API; POSIX path not runtime-verified on this machine (Windows), tests skipped |
| Screen emulation | `pyte>=0.8.2` | Pure-Python VT/xterm emulator; screen state held in memory per process |
| Record granularity (PTY) | Chunk-based records instead of line-based | `readline()` blocks forever on prompts without `\n` and TUI redraws |
| Screen exposure | New `process_screen` tool | Read records = history, read screen = current state; separate concerns |
| Registry location | Platform app-data dir + `PORTAL_DATA_DIR` override | Machine-global, survives venv rebuilds, independent of cwd and install method |
| Registry population | Agent writes (`program_record`) + automatic error-pattern capture | Registry must not depend on the agent remembering to record |
| Registry startup | Empty; no seed list | Cold start covered by documented decision rules; no stale static list to maintain |

## Architecture

```
MCP Client (LLM)
       │ JSON-RPC (stdio)
       ▼
Portal MCP Server
  ├── Tool Handlers
  │     ├── process_start (pty param)          process_screen (new)
  │     ├── program_query (new)                program_record (new)
  │     └── 10 existing pipe-mode tools (unchanged)
  ├── ProcessManager        ← start() branches on pty flag
  │     ├── ManagedProcess  (pipe mode — unchanged)
  │     └── PtyProcess      (new, PTY mode)
  │           ├── pty_backend: Windows→pywinpty(ConPTY) / POSIX→ptyprocess
  │           ├── Reader thread (blocking read) → asyncio queue
  │           │     via loop.call_soon_threadsafe
  │           ├── pyte screen emulator (in-memory, live)
  │           └── DB record stream (chunk granularity, source=1)
  ├── Database (session)    — unchanged, stays at <cwd>/.portal/portal.db
  ├── Registry DB           — new, global, platform app-data dir
  └── ansi.py               — unchanged
```

## New Modules

### `portal_mcp/pty_backend.py` — cross-platform spawn dispatch

Thin layer unifying pywinpty (Windows) and ptyprocess (POSIX). The two libraries deliberately mirror each other's `PtyProcess` API, so the abstraction is a platform guard plus normalization:

| Aspect | pywinpty (Win) | ptyprocess (POSIX) |
|--------|----------------|--------------------|
| `spawn(argv, cwd, env, dimensions=(rows, cols))` | ✓ | ✓ |
| `read(n)` | returns `str` | returns `bytes` |
| `write(s)` | `str` | `bytes` |
| `setwinsize(rows, cols)` | ✓ | ✓ |
| `isalive()` / `terminate()` / `kill()` / `pid` | ✓ | ✓ |
| EOF signal | `read()` raises `EOFError` | `read()` returns `b""` at EOF |

- Abstraction normalizes: `read()` → decode `errors="replace"`; EOF → raise `EOFError` on both.
- Both `read()` calls are blocking → the manager runs them in one dedicated reader thread per process, bridging to asyncio via `loop.call_soon_threadsafe` (single producer → FIFO → single consumer, order preserved).
- Imports are guarded per platform (`winpty` only imported on Windows; POSIX fallback on `ImportError`).
- Spawn env: inject `TERM=xterm-256color` if not present — otherwise TUIs degrade to dumb mode.

### `portal_mcp/pty_process.py` — `PtyProcess` class

Interface-aligned twin of `ManagedProcess` so `ProcessManager` handles both with the same lifecycle code:

- `spawn(command, args, cwd, env, cols=80, rows=24)` via `pty_backend`
- Reader thread: blocking `read()` loop → raw chunks into asyncio queue
- Per-chunk fan-out (the core dual-consumer split):
  - `pyte.Stream().feed(chunk)` — **raw** VT bytes (cursor movement must be preserved)
  - DB insert: `strip_ansi(chunk)` + strip `\r` → record (source=1)
- `write(content)`: encode and write to PTY input; recorded in DB as source=0; echo will appear in output (real terminal behavior)
- `resize(cols, rows)`: `setwinsize(rows, cols)` (note argument order)
- `kill()` / `terminate()` / status tracking / `wait_exit()` — same contracts as `ManagedProcess`
- Screen state lives in the `pyte.Screen` instance; **remains queryable after process exit until `process_cleanup`** — no DB persistence needed
- `process_screen` reads touch the idle timer, same as `process_read`

## Output Model for PTY Processes

1. **Single merged stream** — stdout and stderr both go to the console. Records only ever contain `source` 0 (writes) and 1 (output); `process_read(source="stderr")` returns empty for PTY processes. API unchanged; documented.
2. **Chunk granularity** — each `read()` return is one DB record (pywinpty buffers internally). `process_read` API unchanged (already time-window based); record granularity difference is documented.
3. **Echo** — input written to the PTY appears in the output stream (cooked mode). Documented so agents don't mistake echoes for program output.
4. **Line endings** — ConPTY emits `\r\n`; records strip `\r` for readability. pyte parses `\r\n` itself.
5. **Control characters** — Ctrl+C = `\u0003`, Ctrl+D = `\u0004` written via `process_write` (JSON strings support them). This is the reliable way to send Ctrl+C under ConPTY.
6. **TUI record streams are garbled fragments** — cursor jumps stripped of ANSI. Documented: TUIs read via `process_screen`, normal interactive programs via `process_read`.

## MCP Tool Changes

### `process_start` — new parameter

- Input adds `pty: bool = false`. Everything else (command/args/cwd/env/timeout_ms, output `{id, os_pid, status}`) unchanged.

### `process_screen` (new)

```
Input:  id (required), cols/rows (optional — resize first, then snapshot)
Output: { id, status, rows, cols, cursor_x, cursor_y, content }
        content = screen lines joined with \n (pyte dump)
```

- Error for pipe-mode processes: "Process N is not a PTY process"
- Resize defaults: 80×24; resizing works on live processes (`setwinsize`)

### `program_query` (new)

```
Input:  program (required, executable name)
Output: { program, needs_pty, source, notes, confirmed_count } or unknown
```

### `program_record` (new)

```
Input:  program (required), needs_pty (required), notes (optional)
Effect: upsert into registry; agent writes override auto-captured entries
```

## Signals & Lifecycle

| Operation | Windows (ConPTY) | POSIX (ptyprocess) |
|-----------|------------------|--------------------|
| Ctrl+C | `process_write` with `\u0003` | same (standard) |
| SIGTERM / terminate | pywinpty `terminate()` (graceful then force) | SIGTERM |
| SIGKILL / kill | `TerminateProcess` | SIGKILL |
| Exit detection | reader thread `EOFError` → status exited | EOF → same |

- `process_signal` kept; signal-name mapping updated in its tool description per platform (PTY processes on Windows: SIGTERM → terminate, SIGKILL → kill).
- Timeout monitor logic unchanged: timeout → `PtyProcess.kill()` → cleanup via the existing shared code path.
- Known limitation (documented): on Windows `process_kill` force-kills the main process; ConPTY-spawned children (e.g. ssh forwarding chains) may linger — same stance as pipe mode, no process-tree cleanup promise.

## PTY Decision Rules (documented in tool descriptions + MCP instructions)

| Signal | Examples | Decision |
|--------|----------|----------|
| Program checks `isatty()` | ssh, gdb, psql, mysql, telnet, REPLs | `pty: true` |
| Full-screen TUI | vim, htop, top, less, man | `pty: true` |
| Interactive flags | `-i` / `-it` / `-t` (docker, kubectl) | `pty: true` |
| One-shot scripts / batch | `python -c`, dir, build commands | `pty: false` (default) |

**Default rule**: when in doubt, use `pty: true` — the cost asymmetry (a non-interactive program tolerates a PTY; an interactive program without one hangs/degrades) favors it. Portal's purpose is interactive programs.

**Escalation loop** (documented workflow): pipe mode starts a process → no output / hangs → kill → restart with `pty: true`.

**Decision priority**: registry hit → use it; miss → decision-table heuristic; still unsure → `pty: true`.

## Program Registry

### Storage

- File: `<data_dir>/programs.db`, where `data_dir` = `PORTAL_DATA_DIR` env var if set, else platform app-data dir:
  - Windows: `%APPDATA%\portal-mcp\`
  - macOS: `~/Library/Application Support/portal-mcp/`
  - Linux: `$XDG_DATA_HOME/portal-mcp/` (default `~/.local/share/portal-mcp`)
- **Persistent across sessions** — never deleted on startup (unlike session `portal.db`).
- Path resolution in new `portal_mcp/paths.py` (~15 lines, no new dependency).
- Session DB `portal.db` stays at `<cwd>/.portal/portal.db` — per-project session data is intentionally cwd-scoped.

### Schema

```sql
CREATE TABLE programs (
    program           TEXT PRIMARY KEY,          -- canonical basename, lowercase
    needs_pty         INTEGER NOT NULL,          -- 0/1
    source            TEXT NOT NULL,             -- 'agent' | 'auto'
    notes             TEXT DEFAULT '',
    confirmed_count   INTEGER NOT NULL DEFAULT 1,
    last_confirmed_at INTEGER NOT NULL           -- ns timestamp
);
```

Starts empty; no seed list.

### Write paths

1. **Agent** (primary): `program_record` after observing a program's behavior.
2. **Automatic capture** (fallback): pipe-mode processes whose output matches known "needs TTY" error patterns **and** exit code != 0 are auto-inserted with `source='auto'`:
   - `not a tty` / `is not a terminal` / `stdin is not a tty`
   - `TERM environment variable not set`
   - `tcgetattr`
   - Exit-code condition guards against false positives (e.g. `echo "not a tty"`).
   - Agent `program_record` overrides auto entries.

## Error Handling & Edge Cases

| Scenario | Behavior |
|----------|----------|
| PTY spawn failure (e.g. ConPTY unavailable) | Same as pipe mode: clean up DB rows, raise — no half-started state |
| UTF-8 multibyte split across chunks | `decode(errors="replace")`; acceptable, agent reads by time window |
| `setwinsize` argument order | `(rows, cols)` on both backends — normalized in `pty_backend` |
| `process_screen` on pipe-mode process | Error: "Process N is not a PTY process" |
| `process_screen` on unknown id | Error (same as existing tools) |
| Reader thread dies unexpectedly | Mark process exited; same handling as pipe mode reader failure |
| `TERM` missing in env | Injected as `TERM=xterm-256color` at spawn |

## Testing

- **All existing pipe-mode tests stay green, unchanged** (regression baseline).
- New `tests/test_pty.py` (runs on this Windows machine):
  1. **isatty proof**: `python -c "import sys; print(sys.stdout.isatty())"` → PTY mode prints `True`, pipe mode prints `False`
  2. **Prompt without newline**: `python -c "input('Password: ')")` waits → write password → read back (pipe mode would hang; PTY chunk model works)
  3. **Screen snapshot**: script draws a 2×2 grid with ANSI → `process_screen` returns the grid
  4. **Ctrl+C**: `input()` hung → write `\u0003` → process exits with status exited
  5. **Echo**: written content appears in output; records contain no `\r`
  6. **Resize**: `setwinsize` changes dump dimensions
  7. **Errors**: `process_screen` on pipe-mode process errors
- New `tests/test_registry.py`:
  - CRUD, agent-overrides-auto, cross-startup persistence (run server twice, entry survives)
  - Auto-capture: matching output + nonzero exit → inserted; `echo "not a tty"` (exit 0) → **not** inserted
- POSIX backend tests: `pytest.mark.skipif` on Windows (code path preserved for other platforms).

## Dependencies

```toml
dependencies = [
    "mcp>=1.0.0",
    "aiosqlite>=0.20.0",
    "pyte>=0.8.2",
    "pywinpty>=3.0.5 ; sys_platform == 'win32'",
    "ptyprocess ; sys_platform != 'win32'",
]
```

## File Changes

```
Portal/
├── portal_mcp/
│   ├── __init__.py
│   ├── server.py           # + process_screen, program_query, program_record; process_start pty param
│   │                       # + MCP instructions: PTY decision rules, escalation loop, registry usage
│   ├── manager.py          # start() branches on pty; screen/registry methods
│   ├── process.py          # unchanged
│   ├── pty_process.py      # NEW: PtyProcess (thread reader, pyte emulator, chunk records)
│   ├── pty_backend.py      # NEW: pywinpty/ptyprocess dispatch + normalization
│   ├── registry.py         # NEW: persistent registry DB layer
│   ├── paths.py            # NEW: platform data-dir resolution
│   ├── database.py         # unchanged
│   └── ansi.py             # unchanged
├── pyproject.toml          # + dependencies with platform markers
├── README.md / README_zh.md # new tools, pty semantics, registry, usage examples
└── tests/
    ├── test_pty.py         # NEW
    └── test_registry.py    # NEW
```

## Out of Scope

- Seed list for the registry (starts empty by design).
- Automatic `pty: "auto"` detection mode — unreliable; agent judgment + decision rules + registry cover it.
- Screen-state persistence to DB (live in-memory only, until cleanup).
- Process-tree cleanup on Windows kill.
