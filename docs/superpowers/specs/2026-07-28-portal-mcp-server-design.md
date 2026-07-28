# Portal MCP Server — Design Spec

## Overview

Portal is an MCP (Model Context Protocol) server that manages subprocesses. It allows LLM clients to start arbitrary executables/scripts, read their stdout/stderr, write to their stdin, send signals, and manage process lifecycle — all through fine-grained MCP tool calls.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | Python 3.11+ | Best MCP SDK maturity, asyncio for async I/O, built-in sqlite3 |
| Distribution | pip (lightweight) | No single-binary packaging needed; target audience has Python |
| MCP SDK | `mcp` (official Python SDK) | Most mature Python MCP implementation |
| Async model | asyncio | Python MCP SDK uses asyncio; `asyncio.create_subprocess_exec` for pipes |
| Database | SQLite via `aiosqlite` | Async SQLite wrapper, no extra dependencies |
| Process ID | Internal auto-increment integer | Avoids OS PID reuse ambiguity |
| Output granularity | Per-read record with timestamp | Each chunk read from pipe = one DB row |

## Database Design

Database is created fresh on every MCP server startup (old file deleted).

### Global table: `processes`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK AUTOINCREMENT | Internal process ID |
| `os_pid` | INTEGER | OS-level process ID |
| `status` | TEXT | `running`, `exited`, or `killed` |
| `started_at` | INTEGER | Start timestamp in nanoseconds |
| `timeout_ms` | INTEGER | Idle timeout in milliseconds (0 = no timeout) |
| `last_active_at` | INTEGER | Last external tool interaction timestamp (ns) |
| `command` | TEXT | Executable path or command |
| `args` | TEXT | Command-line arguments (JSON array) |
| `cwd` | TEXT | Working directory |
| `env` | TEXT | Environment variables (JSON object) |
| `exit_code` | INTEGER | Exit code (null while running) |

### Per-process table: `proc_<id>`

Created when process is started, dropped when `process_cleanup` is called.

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | INTEGER | Nanosecond timestamp |
| `source` | INTEGER | 0 = stdin (write), 1 = stdout, 2 = stderr |
| `content` | TEXT | Content with ANSI control sequences stripped |

## Architecture

```
MCP Client (LLM)
       │ JSON-RPC (stdio)
       ▼
Portal MCP Server
  ├── Tool Handlers (10 fine-grained tools)
  │     └── Each maps to ProcessManager method
  ├── ProcessManager
  │     ├── Dict[id → ManagedProcess]
  │     ├── Timeout monitor (background asyncio Task, scans every 1s)
  │     └── DB access via Database layer
  ├── ManagedProcess (per-process)
  │     ├── asyncio.subprocess.Process
  │     ├── Background read tasks for stdout + stderr
  │     ├── stdin writer (synchronized)
  │     └── Status tracking + idle timer
  ├── Database (aiosqlite wrapper)
  │     ├── init() — create tables
  │     ├── create_proc_table(id)
  │     ├── insert_record(id, timestamp, source, content)
  │     ├── read_records(id, sources, since_timestamp)
  │     ├── io_size(id) — COUNT of records
  │     ├── clear_proc(id) — DELETE all records
  │     └── cleanup_proc(id) — DROP table + DELETE processes row
  └── ANSI Stripper
        └── Regex: \x1b\[[0-9;]*[a-zA-Z]
```

## MCP Tools

### `process_start`
Start a subprocess.
- **Input:** `command` (required), `args` (optional, list), `cwd` (optional), `env` (optional, dict), `timeout_ms` (optional, 0 = no timeout)
- **Output:** `id`, `os_pid`, `status`
- **Side effects:** Creates `proc_<id>` table, starts background read tasks for stdout/stderr

### `process_read`
Read output from a process.
- **Input:** `id` (required), `source` (optional: `stdout`/`stderr`/`both`, default `both`), `duration` (required), `unit` (optional: `ns`/`us`/`ms`/`s`, default `ms`)
- **Output:** List of records, each with `timestamp`, `source`, `content`
- **Side effects:** Resets idle timer (`last_active_at`)

### `process_write`
Write to process stdin.
- **Input:** `id` (required), `content` (required)
- **Side effects:** Writes content to stdin pipe, records in DB as source=0, resets idle timer

### `process_signal`
Send a signal to a process.
- **Input:** `id` (required), `signal` (required — OS-native signal name or number)
- **Platform notes:** Tool description documents available signals per platform. Windows supports `CTRL_C_EVENT`, `CTRL_BREAK_EVENT`, `SIGTERM` (mapped to TerminateProcess). POSIX supports full signal set.
- **Side effects:** May change process status to `killed` if signal is fatal

### `process_list`
List all managed processes.
- **Input:** None
- **Output:** Array of `{id, os_pid, status, timeout_ms, inactive_duration_ms, io_count}` for each process

### `process_inspect`
Inspect a single process in detail.
- **Input:** `id` (required)
- **Output:** All process metadata columns + `io_count` (total records in proc_<id> table) + `stdout_count`, `stderr_count`, `stdin_count`

### `process_kill`
Kill a single process.
- **Input:** `id` (required)
- Kills the OS process, marks status as `killed`, data retained for reading

### `process_kill_all`
Kill all managed processes.
- **Input:** None
- Kills every running process

### `process_clear`
Clear I/O buffer for a process.
- **Input:** `id` (required)
- Deletes all records from `proc_<id>` table (table remains)

### `process_cleanup`
Remove a terminated process and its data entirely.
- **Input:** `id` (required)
- Only allowed for processes with status `exited` or `killed`
- Drops `proc_<id>` table and deletes the `processes` row

## Process Lifecycle

```
               ┌── timeout ──→ KILL + CLEANUP ──→ removed
               │
START ──→ RUNNING ──→ EXITED ──→ (read-only, data retained)
               │
               ├── process_kill ──→ KILLED ──→ (read-only, data retained)
               │
               └── process_cleanup ──→ removed (only for exited/killed)
```

## ANSI Stripping

Before storing, all content is run through a regex filter:
- Pattern: `\x1b\[[0-9;]*[a-zA-Z]`
- Removes color codes, cursor movement, and other ANSI escape sequences
- No other content transformation is applied

## Timeout Monitor

A background asyncio task runs every 1 second:
1. Iterates all processes with `status == "running"`
2. Computes `inactive_duration = now_ns() - last_active_at`
3. If `timeout_ms > 0` and `inactive_duration > timeout_ms * 1_000_000`:
   - Kills the OS process
   - Calls process_cleanup to remove all data

## Cross-Platform Considerations

- **Process spawning:** `asyncio.create_subprocess_exec` works on all platforms
- **Signals:** Windows is limited; tool description documents the difference
- **Paths:** Use `pathlib.Path` for all filesystem operations
- **Line endings:** No special handling; data is stored as-is (except ANSI stripping)

## Project Structure

```
Portal/
├── portal_mcp/
│   ├── __init__.py
│   ├── server.py          # MCP Server entry point, tool registration
│   ├── manager.py         # ProcessManager
│   ├── process.py         # ManagedProcess class
│   ├── database.py        # SQLite operations
│   └── ansi.py            # ANSI escape sequence stripping
├── pyproject.toml          # Package metadata, dependencies, entry point
├── README.md
└── tests/
    ├── __init__.py
    └── test_portal.py
```

## Dependencies

- `mcp` — Official Python MCP SDK
- `aiosqlite` — Async SQLite wrapper
- Standard library: `asyncio`, `sqlite3`, `re`, `pathlib`, `json`, `time`

## Edge Cases & Error Handling

| Scenario | Behavior |
|----------|----------|
| Read from non-existent process | Return error with message |
| Write to exited/killed process | Return error — stdin closed |
| Signal to already-exited process | Return error — process not running |
| Cleanup running process | Return error — must kill first |
| Cleanup already-cleaned process | Return error — not found |
| Start with invalid command | Return error from subprocess |
| Process crashes during read | Background tasks catch exception, mark as exited |
| SQLite errors | Wrapped in PortalError, propagated to MCP |
