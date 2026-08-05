# Portal MCP Server — PTY Support Design Spec

## Overview

Portal currently spawns subprocesses with OS pipes (`asyncio.create_subprocess_exec`), so programs that check `isatty()` (ssh, gdb, psql, interactive REPLs) change behavior, and full-screen TUIs (vim, htop) are unusable. This spec adds **virtual PTY support** so Portal can drive the full range of interactive programs, plus a **program registry** that accumulates which programs need a PTY.

Existing pipe mode, its 10 tools, the SQLite session schema, and all current tests remain unchanged.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Enablement | `pty: bool = false` parameter on `process_start` | Pipe mode stays default for backward compatibility — but the schema default is NOT a recommendation; see PTY Decision Rules |
| PTY backend (Windows) | `pywinpty>=3.0.5` (ConPTY) | Official ConPTY binding (winpty-rs); 3.0.5 fixed the broken 3.0.4 wheel packaging; ships cp313 win_amd64 wheels, requires Python >=3.10, ConPTY needs Win10 1809+ |
| PTY backend (POSIX) | `ptyprocess` | Same API shape as pywinpty (by design), pure Python, no hard deps |
| Backend abstraction | Thin `spawn()` dispatch in `pty_backend.py` | Both backends mirror each other's API (verified against source); POSIX path not runtime-verified on this machine (Windows), tests skipped |
| Screen emulation | `pyte>=0.8.2` + a minimal alternate-screen swap in `PtyProcess` | pyte is pure-Python but does **not** emulate the alternate screen buffer (1047/1048/1049); vim/htop/less all use it — see Alt-Screen Emulation |
| Record granularity (PTY) | Chunk-based records instead of line-based | `readline()` blocks forever on prompts without `\n` and TUI redraws |
| Screen exposure | New `process_screen` tool | Read records = history, read screen = current state; separate concerns |
| Registry location | Platform app-data dir + `PORTAL_DATA_DIR` override | Machine-global, survives venv rebuilds, independent of cwd and install method |
| Registry population | Agent writes via `program_record` only | Judgment is the agent's job (it observes behavior: "not a tty" errors, hangs, TUI rendering); the server only stores confirmed facts — no fragile server-side pattern matching |

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
  │           ├── Reader thread: blocking read() → asyncio queue
  │           │     via loop.call_soon_threadsafe (guarded, daemon)
  │           ├── Loop-side consumer task: queue → pyte feed + strip + DB insert
  │           ├── pyte emulator: primary + alternate screens (in-memory, live)
  │           └── DB record stream (chunk granularity, source=1)
  ├── Database (session)    — unchanged, stays at <cwd>/.portal/portal.db
  ├── Registry               — new, global, platform app-data dir
  └── ansi.py               — unchanged
```

Backend facts in this spec (spawn signature, read/write types, EOF behavior, `terminate()` semantics, `os.kill` mapping) were verified against pywinpty 3.0.5 / ptyprocess / pyte 0.8.2 sources and CPython 3.11/3.13 `posixmodule.c`.

## New Modules

### `portal_mcp/pty_backend.py` — cross-platform spawn dispatch

Thin layer unifying pywinpty (Windows) and ptyprocess (POSIX). Verified API surface (both libraries, source-confirmed):

| Aspect | pywinpty (Win) | ptyprocess (POSIX) |
|--------|----------------|--------------------|
| `spawn(argv, cwd, env, dimensions=(rows, cols))` | ✓ | ✓ (default (24, 80), rows first on both) |
| `read(n)` | returns `str` | returns `bytes` |
| `write(s)` | `str` | `bytes` |
| `setwinsize(rows, cols)` | ✓ | ✓ |
| `kill(sig)` — **requires sig** | ✓ | ✓ |
| `isalive()` / `pid` / `terminate(force=)` / `close()` | ✓ | ✓ |
| EOF signal | `EOFError` (but see empty-read caveat) | `EOFError` (both EOF paths) |

Normalization and rules in `pty_backend`:

- `read()` → if `str`, use as-is; if `bytes`, decode `errors="replace"`. **An empty result is NOT EOF** — pywinpty legitimately returns `''` for an empty blocking read while the child is alive; and after `terminate()` (which calls `cancel_io()`) it returns `''` forever without raising. EOF is detected by `EOFError` **or** `not isalive()`.
- `kill(sig)` requires the signal argument on both backends. `PtyProcess.kill()` (no-arg, mirroring `ManagedProcess`) is defined as `backend.kill(SIGKILL)` — on Windows CPython's `os.kill` maps any non-CTRL signal to `TerminateProcess`, so this hard-terminates with exit code 9.
- Spawn env: merge `os.environ` first (identical to pipe mode's merge in `manager.start`), then inject `TERM=xterm-256color` if absent. pywinpty resolves `argv[0]` against the provided env's PATH — passing a partial env would break command resolution.
- **Lazy imports only**: `winpty` / `pyte` / `ptyprocess` are imported inside `spawn()` (and the emulator factory), never at module level of any module the existing tests import — the new deps are not installed on every machine, and eager imports would break the whole existing suite at collection.
- Guard `loop.call_soon_threadsafe` callbacks with `try/except RuntimeError` (closed loop during shutdown/teardown).
- `dimensions` is normalized to `(rows, cols)` internally (both backends already agree; kept explicit so a version change can't silently transpose).

### `portal_mcp/pty_process.py` — `PtyProcess` class

Interface-parity twin of `ManagedProcess` — the exact public surface `ProcessManager` calls:

| Member | Contract |
|--------|----------|
| `spawn(command, args, cwd, env, cols=80, rows=24)` | via `pty_backend`; reader thread starts here (daemon=True) |
| `os_pid` (property) | from backend `.pid` — `manager.start` needs it for `update_os_pid` (no `asyncio.subprocess.Process` exists in PTY mode) |
| `start_background_readers(db)` | no-op in PTY mode (reader already started at spawn) — keeps `manager.start` un-branched |
| `write_stdin(db, content)` | encode + write to PTY input; DB record source=0; **same signature as `ManagedProcess`** (manager.write calls exactly this) |
| `send_signal(sig)` | resolve name-or-int, then: SIGTERM→`terminate()`, SIGKILL→`kill()`, CTRL_C_EVENT→write `\u0003` (see Signals) |
| `kill()` | `backend.kill(SIGKILL)`; then `backend.close()` to unblock the reader; join thread with timeout |
| `terminate()` | `backend.terminate()` — immediate hard kill on Windows (see Signals) |
| `wait_exit()` | poll `isalive()` / exit event with timeout; may return `None` (ConPTY exposes no exit code) |
| `status` | running / exited / killed — same semantics as `ManagedProcess` |

**I/O pipeline** (the core dual-consumer split):

```
Reader thread (daemon): raw chunk = backend.read()
    → queue.put_nowait(chunk)  via loop.call_soon_threadsafe
Loop-side consumer task (per process):
    → pyte.Stream().feed(chunk)           ← raw VT bytes; cursor movement must be preserved
    → alt-screen scanner on chunk stream  ← toggles primary/alternate pyte screens
    → strip_ansi(chunk) + strip bare \r   → DB record (source=1)
```

All `pyte.Screen` mutation and all DB writes happen **on the event loop** (consumer task) — never in the reader thread (aiosqlite is loop-bound; pyte state would need locks). This also means `process_screen` reads the emulator with no synchronization issues.

**Reader loop exit rules**: break on `EOFError`, on `not isalive()`, and on repeated empty reads with `not isalive()` (defensive); `''` alone is never treated as EOF. On kill/terminate/shutdown, `backend.close()` unblocks a thread stuck in blocking `read()`. Threads are `daemon=True`; `kill`/`terminate`/`shutdown` join with a timeout (never unbounded — a blocked read must not hang pytest or `asyncio.run` exit).

**Exit code**: pywinpty exposes no exit code. `wait_exit()` returns `None` on Windows PTY processes; DB `exit_code` stays NULL and `process_inspect` shows it — documented, agents must not rely on exit codes for PTY processes on Windows.

### Alt-Screen Emulation (vim / htop / less)

pyte 0.8.x records DECSET 1047/1048/1049 as inert flags — there is no buffer swap, so TUIs would draw over the primary screen and their final frame would persist after exit. `PtyProcess` implements a minimal swap:

- Two `pyte.Screen` instances: primary and alternate (same dimensions).
- The consumer task scans the chunk stream for `ESC[?1047h`, `ESC[?1048h`, `ESC[?1049h` and the matching `l` codes, toggling the active screen. `1049h` clears the alternate screen on entry; `1047h`/`1048h` do not (and cursor save/restore via 1048 is not emulated — documented limitation).
- `process_screen` returns the active screen plus a `buffer` field (`"primary"` / `"alternate"`) so agents know which one they are looking at.
- Both screens are resized in lockstep with `setwinsize` (`Screen.resize`).

## Output Model for PTY Processes

1. **Single merged stream** — stdout and stderr both go to the console. Records only ever contain `source` 0 (writes) and 1 (output); `process_read(source="stderr")` returns empty for PTY processes. API unchanged; documented in `process_read` and `process_inspect` descriptions.
2. **Chunk granularity** — each consumer-task batch is one DB record. Records are NOT line-aligned: a line may span records and a record may hold several lines; prompts may arrive without a trailing newline. `process_read` API unchanged (already time-window based). Documented in `process_read` description.
3. **Echo** — input written to the PTY appears in the output stream (cooked/line mode; keep the child in cooked mode — never set raw mode). Documented in `process_write` description.
4. **Line endings** — ConPTY emits `\r\n`; records strip bare `\r` (not just `\r\n`, so progress spinners writing lone `\r` don't leave stray CRs). pyte parses `\r\n` itself.
5. **Control characters** — Ctrl+C = `\u0003`, Ctrl+D = `\u0004` written via `process_write` (JSON strings support them). This is the reliable way to send Ctrl+C under ConPTY — pywinpty's own `sendintr()` does exactly `write('\x03')`, and `GenerateConsoleCtrlEvent` cannot target a ConPTY child (it does not share the caller's console).
6. **TUI record streams are garbled fragments** — cursor jumps stripped of ANSI. Documented in `process_screen` description: TUIs read via `process_screen`, normal interactive programs via `process_read`.
7. **No exit code** — see PtyProcess section. `process_inspect` shows `exit_code: null` for PTY processes on Windows.

## MCP Tool Changes

### `process_start` — new parameter

- Input adds `pty: bool = false`. Everything else (command/args/cwd/env/timeout_ms, output `{id, os_pid, status}`) unchanged.
- Tool description carries the rule explicitly (schema default and rule must not fight):
  > `pty` — default `false` for backward compatibility; this is NOT a recommendation. Set `true` for anything interactive or TUI (ssh, gdb, psql/mysql, REPLs, vim, htop, top, less; anything with -i/-it/-t flags). Consult `program_query` for this executable BEFORE starting. When in doubt, `true` — a non-interactive program tolerates a PTY; an interactive one without one hangs. Exception: one-shot commands that page output (git log/diff, less) — prefer pipe mode with `--no-pager`/`GIT_PAGER=cat`.

### `process_screen` (new)

```
Input:  id (required), cols/rows (optional)
Output: { id, status, buffer, rows, cols, cursor_x, cursor_y, content }
        buffer   = "primary" | "alternate" (active pyte screen)
        content  = screen.display lines joined with \n (pyte 0.8 API: Screen(columns, lines), .display)
```

- **Pure snapshot when `cols`/`rows` are omitted** — snapshots at the current size, no resize side effect. A read tool must not silently relayout a vim session.
- Passing `cols`/`rows` explicitly resizes the live PTY (`setwinsize(rows, cols)`, emulator resized in lockstep) and then snapshots — documented as a mutation.
- Screen state is live and remains queryable after process exit **until `process_cleanup`** — no DB persistence.
- Errors: "Process N is not a PTY process" for pipe-mode processes; unknown-id error same as existing tools.
- `process_screen` reads touch the idle timer, same as `process_read`.

### `program_query` (new)

```
Input:  program (required, executable name)
Output (hit):  { program, needs_pty, notes, confirmed_count }
Output (miss): { program, known: false }
```

A miss is a **normal result, not an error** — it means "apply the decision table". Call BEFORE `process_start`. `confirmed_count >= 2` means settled; a single-confirmation entry is a hint — re-verify on first use in a session.

### `program_record` (new)

```
Input:  program (required), needs_pty (required), notes (optional)
Effect: upsert; needs_pty overwrites; confirmed_count increments ONLY when the
        new value matches the stored value — a flip (correction) resets count
        to 1; notes replace if provided, else retained; last_confirmed_at = now
```

Corrections must not look like reinforcement: counting a flip as agreement would poison `confirmed_count`'s "settled" meaning.

### Naming

The new registry tools keep the `program_*` prefix (not `process_program_*`): they operate on program metadata, a distinct domain from live `process_*` lifecycle tools. `process_screen` stays `process_*` because it manages a live process's state.

## Signals & Lifecycle

| Operation | Windows (ConPTY) | POSIX (ptyprocess) |
|-----------|------------------|--------------------|
| Ctrl+C | `process_write` with `\u0003` (write, wait, then terminate if still alive) | same (standard) |
| SIGTERM / terminate | `terminate()` — **immediate hard kill** (TerminateProcess, exit code 2). pywinpty's `terminate()` calls `kill(SIGINT)`, and Windows `os.kill` maps SIGINT to TerminateProcess; **no graceful step exists** | SIGTERM |
| SIGKILL / kill | `kill(SIGKILL)` → TerminateProcess (exit code 9) | SIGKILL |
| Exit detection | `EOFError` **or** `not isalive()` → status exited; `exit_code` = None | `EOFError` → exited, real exit code |

- `PtyProcess.send_signal(sig)` resolves names itself: SIGTERM→`terminate()`, SIGKILL→`kill()`, CTRL_C_EVENT→write `\u0003` (matches pipe-mode's documented CTRL_C_EVENT support without relying on the signal API). `manager.send_signal` passes the raw name through for PTY processes.
- `process_signal` tool description documents **both** mappings (pipe vs PTY) — the description is static per platform, so it lists both: "For PTY processes: SIGTERM → terminate (hard kill on Windows), SIGKILL → kill, CTRL_C_EVENT → Ctrl+C; for a graceful interrupt use `process_write` with `\u0003`."
- Timeout monitor logic unchanged: timeout → `PtyProcess.kill()` → cleanup via the existing shared code path.
- Known limitations (documented): on Windows there is no graceful terminate — the only graceful interrupt is `\u0003`; `process_kill` force-kills the main process and ConPTY-spawned children may linger (same stance as pipe mode, no process-tree cleanup promise); exit code unavailable for PTY processes on Windows.

## PTY Decision Rules (documented in MCP instructions + tool descriptions)

| Signal | Examples | Decision |
|--------|----------|----------|
| Program checks `isatty()` | ssh, gdb, psql, mysql, telnet, REPLs | `pty: true` |
| Full-screen TUI | vim, htop, top, less, man | `pty: true` |
| Interactive flags | `-i` / `-it` / `-t` (docker, kubectl) | `pty: true` — flags go in `args`; the `pty` param grants the *local* terminal; tools like docker need both |
| One-shot scripts / batch | `python -c`, dir, build commands | `pty: false` (default) |
| Paged one-shots | git log/diff, mvn test, less | **pipe mode + `--no-pager` / `GIT_PAGER=cat`** — with `pty: true` they sit in the pager and look hung |

**Default rule**: when in doubt, use `pty: true` — the cost asymmetry (a non-interactive program tolerates a PTY; an interactive one without one hangs/degrades) favors it. Portal's purpose is interactive programs. (This coexists with the `pty: false` schema default: that default is for backward compatibility, not guidance.)

**Escalation loop** (documented workflow):
1. Start with pipe mode (or registry verdict). If unsure which way to go, prefer pipe mode for one-shot-lookalikes and `pty: true` for interactive-lookalikes.
2. **Hang check**: read with a generous window (`duration=10000`, unit `ms` — the default 1s window misses older output and looks identical to a hang). Treat as a hang only if reads keep returning empty AND `process_list` shows `io_count` unchanged after several seconds. Set a `timeout_ms` on every escalated attempt so a real hang self-terminates.
3. On hang: kill → restart with `pty: true`.
4. **Reverse leg**: if a `pty: true` process sits in a pager or behaves wrongly for a one-shot command, send `q` (or `\u0003`), kill, restart without `pty`, and record `needs_pty=false`.
5. Record every first-encounter conclusion via `program_record` — **including negatives** (`needs_pty=false` when it ran fine without a PTY). The registry is agent-populated; it only helps future sessions if every encounter is recorded.

**Decision priority**: registry hit → use it (applies to the program's default invocation; check `notes` for flag-specific caveats — e.g. a `docker` entry may only cover `docker run -it`, not `docker build`; store distinguishing flags in `notes` when recording); miss → decision-table heuristic; still unsure → `pty: true`.

## Program Registry

### Storage

- File: `<data_dir>/programs.db`, where `data_dir` = `PORTAL_DATA_DIR` env var if set, else platform app-data dir:
  - Windows: `%APPDATA%\portal-mcp\`
  - macOS: `~/Library/Application Support/portal-mcp/`
  - Linux: `$XDG_DATA_HOME/portal-mcp/` (default `~/.local/share/portal-mcp`)
- **Persistent across sessions** — never deleted on startup. `create_server`'s startup unlink touches only the session `db_path`.
- Path resolution in new `portal_mcp/paths.py` (~15 lines, no new dependency). **`PORTAL_DATA_DIR` is read on every call** — no module-level caching, so tests can monkeypatch it.
- Session DB `portal.db` stays at `<cwd>/.portal/portal.db` — per-project session data is intentionally cwd-scoped.
- Registry connection: one aiosqlite connection, loop-bound, opened via `registry.open()` / closed via `registry.close()` — same pattern as `Database`.

### Schema

```sql
CREATE TABLE programs (
    program           TEXT PRIMARY KEY,          -- canonical: basename, lowercase, .exe stripped
    needs_pty         INTEGER NOT NULL,          -- 0/1
    notes             TEXT DEFAULT '',
    confirmed_count   INTEGER NOT NULL DEFAULT 1,
    last_confirmed_at INTEGER NOT NULL           -- ns timestamp
);
```

Starts empty; no seed list. Canonicalization (basename, strip `.exe`, lowercase) is applied identically on write **and** query so lookups never miss entries recorded under another spelling.

### Wiring

- `ProcessManager(db, registry=None)` — optional injection; `None` keeps every existing `ProcessManager(db)` test construction working unchanged.
- `create_server` initializes `paths` + registry alongside the session DB; `main` closes both on exit.
- Registry methods: `query(program)` → hit/miss dicts; `record(program, needs_pty, notes)` → upsert with the flip-reset semantics above.

### Write path

Single write path: `program_record`, called by the agent after it has observed a program's behavior (e.g. pipe mode printed "not a tty" or hung → restart with `pty: true` and record; a script ran fine without a PTY → record `needs_pty=false`). The server does no pattern matching — it only stores what the agent confirms.

## Documentation Placement

Every behavioral fact maps to exactly one agent-facing surface, with drafted wording:

| Fact | Surface | Drafted wording |
|------|---------|-----------------|
| Decision table, default rule, priority, escalation loop, feedback loop | MCP instructions text (server.py) | Condensed from PTY Decision Rules section |
| `pty` param semantics + decision triggers | `process_start` description | Quoted in process_start section above |
| PTY records are chunks, not lines; stderr merged | `process_read` description | "For PTY processes stderr is merged into stdout (source=stderr reads return empty) and records are arbitrary chunks, not lines — a line may span multiple records. Prompts may arrive without a trailing newline. Read with a generous `duration` — the default window is only 1s." |
| TUI records are garbled; use `process_screen` | `process_screen` description | "For full-screen TUIs (vim, htop, less): the record stream is garbled fragments — use this tool instead of `process_read`. Screen remains queryable after exit until `process_cleanup`. Snapshot is pure; `cols`/`rows` resize the live PTY." |
| Echo; `\u0003` interrupt | `process_write` description | "Input you write reappears in the output stream (terminal echo) — treat it as your own input, not program output, and do not re-send it. To interrupt a PTY process, send `\u0003` (Ctrl+C); a `KeyboardInterrupt` traceback in output is expected, not an error. `process_signal`/`process_kill` are hard-stop fallbacks." |
| PTY inspect counts | `process_inspect` description | "For PTY processes: `stderr_count` is always 0 (merged stream) and I/O counts are chunk-based, not line-based." |
| `-i`/`-it`/`-t` vs `pty` distinction | MCP instructions text | "`-i`/`-it`/`-t` flags belong in the command's `args` — they configure the *program's* terminal. The `pty` param grants the *local* terminal. Tools like docker need both." |
| Registry value loop | MCP instructions text | "Record every conclusion after a first encounter — including negative ones (`needs_pty=false` when the program ran fine without a PTY)." |
| Full semantics reference | README (EN + zh) | Sections for pty param, process_screen, registry, decision rules |

The existing MCP instructions text additionally needs two edits (they become wrong under this spec): (a) the workflow step "`process_signal` or `process_kill` — stop the process" gains an interrupt step first: "To interrupt a PTY process, `process_write` `\u0003`; `process_signal`/`process_kill` are fallbacks." (b) the bullet "Use interactive flags: `-i`, `--interactive`, `-t`, `--tty`" is clarified per the table row above.

## Error Handling & Edge Cases

| Scenario | Behavior |
|----------|----------|
| PTY spawn failure (e.g. ConPTY unavailable) | Same as pipe mode: clean up DB rows and tables, raise — no half-started state |
| UTF-8 multibyte split across chunks | Only exists on the POSIX bytes path (ConPTY output is already decoded text); `decode(errors="replace")`; unit-tested with a fake backend |
| Empty `read()` result | NOT EOF — pywinpty returns `''` for empty blocking reads while alive; exit needs `EOFError` or `not isalive()` |
| `terminate()`-induced `''` loop | `cancel_io()` makes `read()` return `''` forever without EOF — reader also breaks on `not isalive()`; kill/terminate path calls `backend.close()` |
| `setwinsize` argument order | `(rows, cols)` on both backends (verified); normalized in `pty_backend` |
| `process_screen` on pipe-mode process | Error: "Process N is not a PTY process" |
| `process_screen` on unknown id | Error (same as existing tools) |
| Reader thread death | Mark process exited; same handling as pipe mode reader failure |
| Blocking `write()` | pywinpty `write()` is a blocking C call on the loop — large payloads could stall it; cap writes (chunk large payloads) and/or offload to executor; tests prove no deadlock with `asyncio.wait_for` |
| Thread teardown | daemon threads; `kill`/`terminate`/`shutdown` close backend + join with timeout — a blocked read must not hang teardown |
| `TERM` missing in env | Injected as `TERM=xterm-256color` at spawn (after merging `os.environ`) |
| `exit_code` | `None` for PTY processes on Windows (ConPTY exposes none) — documented; agents must not rely on it |

## Testing

**Foundation** (critical for a machine where the new deps are not yet installed):
- Lazy imports in production code (see pty_backend); every PTY test starts with `pytest.importorskip("winpty")` / `"pyte"` (+ `"ptyprocess"` for POSIX tests) so a fresh checkout **skips**, never errors.
- New `tests/conftest.py`: shared fixtures/helpers — `registry(tmp_path)` (monkeypatches `PORTAL_DATA_DIR`; **never touches the real `%APPDATA%` registry**), `manager_factory`, and the deterministic polling helpers:
  - `wait_for_output(mgr, pid, needle, timeout=5.0)` — loop `mgr.read` on joined content until substring found
  - `wait_for_status(mgr, pid, status, timeout=5.0)`
  - No bare `asyncio.sleep` for output assertions; child scripts stay alive (`time.sleep(30)` + teardown kill), never print-and-exit.
- Test scripts spawn `sys.executable` (never bare `"python"` — Store alias/py launcher), and `-c` code avoids double quotes (pywinpty builds its own command line).

**`tests/test_pty.py`** (runs on this Windows machine):
1. **isatty proof (race-free)**: `python -c "import sys; sys.exit(0 if sys.stdout.isatty() else 1)"` → PTY mode exit 0, pipe mode exit 1. Secondary check: `print(sys.stdout.isatty(), sys.stdin.isatty())` → `True True` for PTY.
2. **Prompt without newline**: `python -c "input('Password: ')"` → wait for `"Password:"` (joined records) → write the secret → read back (echo appears via cooked-mode; assert substrings on **joined** records only, never per-record content — ConPTY splits chunks arbitrarily).
3. **Screen snapshot**: script draws a 2×2 grid with ANSI using `sys.stdout.write` + explicit `flush()` (line-buffered stdout on a tty would flush *and* scroll), **no trailing newline**, `time.sleep(30)`; assert positionally on `screen.display` lines (`lines[r][c] == "X"`, not full-line equality — pyte pads with trailing spaces); spawn with the same dims passed to `process_screen` (a resize triggers a full repaint stream that can scroll the emulated screen mid-feed).
4. **Alt-screen**: script emits `\x1b[?1049h`, draws a marker, sleeps; assert `buffer == "alternate"` and the marker is visible; then `\x1b[?1049l` → `buffer == "primary"` with the pre-TUI content intact.
5. **Ctrl+C**: script `input()` hung → `wait_for_output(..., ">")` until the prompt is visible **before** writing `\u0003`; then within a `wait_for` deadline assert `status == "exited"` **or** `"KeyboardInterrupt" in joined records`; accept exit code in `(1, 0, -1)`; on failure: kill + dump captured records.
6. **Echo / line endings**: written content appears in output; `"\r" not in "".join(records)` (assert on joined content; implementation strips bare `\r`).
7. **Resize (asymmetric, catches arg-order bugs)**: spawn 100 cols × 30 rows, assert BOTH `cols` and `rows` in `process_screen` output (a (rows,cols)/(cols,rows) swap fails loudly); then `setwinsize` to e.g. 120×40 and assert the emulator resized in lockstep. Spawn-time geometry asserted separately.
8. **Errors**: `process_screen` on a pipe-mode process errors; `process_screen` on unknown id errors.
9. **Merged-stream contract**: `process_read(source="stderr")` returns empty for a PTY process that writes to stderr.
10. **Screen persists after exit**: script prints and exits; `process_screen` still returns content (until `process_cleanup`).
11. **Large write does not deadlock**: big payload wrapped in `asyncio.wait_for(..., 5)`.
12. **Timeout monitor kills PTY process**: `timeout_ms=300` + `start_monitor()`, poll `process_inspect` → not found within 3s.
13. **Spawn failure cleans up**: bogus binary + `pty=True` at manager level → raises AND `get_process(id) is None`, `proc_<id>` table dropped, manager dict empty.
14. **Kill vs terminate status paths**: both land on `killed`, teardown safe.
15. **TERM injection**: script prints `os.environ.get("TERM")` → `xterm-256color`.
16. **Reader-thread death**: fake backend whose `read()` raises once → status flips to exited.
17. **Decode layer unit test**: fake bytes-returning backend → decode normalization (`errors="replace"`), `str` passthrough, empty-`''`-not-EOF.
18. **Teardown discipline**: every PTY test ends asserting `mgr.list_all() == []`; fixture teardown kills + `backend.close()` + join-with-timeout, wrapped in `asyncio.wait_for`; threadsafe callbacks swallow `RuntimeError` (closed loop).

**`tests/test_registry.py`**:
- CRUD; upsert reinforcement (`confirmed_count` increments on matching value); **flip resets count to 1**; miss shape `{program, known: false}`; canonicalization (`.exe`/case) on query and record.
- Cross-startup persistence without an MCP client: two `Registry` instances over the same tmp dir (set via `PORTAL_DATA_DIR`) — no "run server twice" ceremony.
- `create_server`'s startup unlink touches only the session DB, never the registry file.

**POSIX**: ptyprocess-specific tests marked `skipif(sys.platform == "win32")` + `importorskip("ptyprocess")` (SIGTERM/SIGKILL semantics, real exit codes, raw-bytes EOF). Locally untestable — the code path is preserved for other platforms/CI.

## Dependencies

```toml
dependencies = [
    "mcp>=1.0.0",
    "aiosqlite>=0.20.0",
    "pyte>=0.8.2",                # pulls wcwidth (transitive) for wide-char rendering
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
│   │                       # + MCP instructions edits (interrupt step, -i/-it/-t vs pty, decision rules)
│   ├── manager.py          # start() branches on pty; registry injection (default None); screen methods
│   ├── process.py          # unchanged
│   ├── pty_process.py      # NEW: PtyProcess (reader thread, consumer task, alt-screen, chunk records)
│   ├── pty_backend.py      # NEW: pywinpty/ptyprocess dispatch + normalization (lazy imports)
│   ├── registry.py         # NEW: persistent registry DB layer
│   ├── paths.py            # NEW: platform data-dir resolution (per-call env read)
│   ├── database.py         # unchanged
│   └── ansi.py             # unchanged
├── pyproject.toml          # + dependencies with platform markers
├── README.md / README_zh.md # new tools, PTY semantics, registry, decision rules
├── llms-install.md         # dependency line: mcp, aiosqlite, pyte, pywinpty (Windows) / ptyprocess (POSIX)
└── tests/
    ├── conftest.py         # NEW: fixtures + wait_for_output/wait_for_status helpers
    ├── test_pty.py         # NEW
    └── test_registry.py    # NEW
```

## Out of Scope

- Seed list for the registry (starts empty by design).
- Automatic `pty: "auto"` detection mode and server-side error-pattern capture — unreliable; agent judgment + decision rules + registry cover it.
- Graceful terminate on Windows — no OS mechanism exists; `\u0003` is the only graceful interrupt.
- Full alt-screen fidelity (cursor save/restore via 1048, history preservation) — only the buffer swap is emulated.
- Screen-state persistence to DB (live in-memory only, until cleanup).
- Process-tree cleanup on Windows kill.
