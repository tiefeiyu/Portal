# Portal — MCP Server for Interactive Programs

An MCP (Model Context Protocol) server purpose-built for **interactive
programs**. Start, monitor, read/write I/O, and control processes that
need sustained bidirectional interaction — all through MCP tool calls.
Where a one-shot Shell tool hangs on interactive programs, Portal is
designed for them.

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)
![MCP 1.x](https://img.shields.io/badge/MCP-1.x-purple)
![Windows](https://img.shields.io/badge/Windows-supported-important)
![POSIX](https://img.shields.io/badge/POSIX-supported-blue)
![uv](https://img.shields.io/badge/uv-managed-blue)
![License](https://img.shields.io/badge/License-MIT-green)

## Why Portal

AI agents (Claude, Cursor, …) can drive **interactive programs** the way
they drive a shell — minus the hanging:

- **Interactive programs, end to end** — start, read/write I/O, signal,
  and kill processes that require sustained bidirectional interaction
  (SSH, GDB, database CLIs, REPLs, full-screen TUIs).
- **Real terminal (PTY) support** — ConPTY on Windows (10 1809+) and
  ptyprocess on POSIX. Programs that check `isatty()` (ssh, gdb, psql,
  REPLs) behave exactly as they do in a human-run terminal.
- **Full-screen TUI snapshots** — `process_screen` captures the live
  screen of vim, htop, less and other full-screen TUIs, whose raw record
  streams are garbled fragments.
- **Never hangs, never orphans** — a per-process idle timeout kills
  stuck programs and cleans up their data.
- **Learns from experience** — a persistent, machine-global registry
  (`program_query` / `program_record`) remembers which executables need
  a PTY, with a confirmation counter so settled facts stick.
- **Clean output** — ANSI escape sequences stripped; I/O history stored
  in SQLite with timestamps, queryable per source (stdout / stderr /
  stdin).
- **Cross-platform** — Windows and POSIX, Python 3.11+, run via uv with
  zero installation.

### When to use it

| Situation | Examples | Use |
|-----------|----------|-----|
| Interactive programs | ssh, gdb, psql, mysql, telnet, REPLs | **Portal** |
| Full-screen TUIs | vim, htop, less, top, man | **Portal** |
| One-shot commands | `ls`, `echo`, build commands | built-in Shell tool |

## Quick Start

No package installation needed — Portal runs directly with `uv`:

```bash
uv sync   # optional: pre-create .venv; `uv run` auto-syncs on first launch
```

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+. Dependencies are
managed by uv from `pyproject.toml`: `mcp` (1.x), `aiosqlite`, `pyte`, plus
`pywinpty` (Windows) / `ptyprocess` (POSIX).

### MCP client configuration

Add to your MCP client configuration (e.g., Claude Code), substituting
`<PORTAL_PATH>` with the absolute path of this repo:

```json
{
  "mcpServers": {
    "portal": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "<PORTAL_PATH>", "portal-mcp"]
    }
  }
}
```

Claude Code one-liner:

```bash
claude mcp add portal -- uv run --project "<PORTAL_PATH>" portal-mcp
```

The `PORTAL_DB_PATH` environment variable controls the SQLite database
location. By default each server instance uses its own file,
`.portal/portal-<pid>-<timestamp>.db` in the working directory — created
fresh on every startup, kept as timestamped history, and named
per-instance so a lingering or concurrent server can never lock the
file and block a new one.

### One-Click Install

Copy the prompt from [`llms-install.md`](llms-install.md) into your AI
agent to automatically install and configure Portal.

## MCP Tools

| Tool | Description |
|------|-------------|
| `process_start` | Start a subprocess with optional args, cwd, env, timeout |
| `process_read` | Read output records (stdout/stderr/stdin/both) within a time window |
| `process_read_new` | Read output produced since the last read (per source, server-side cursor) |
| `process_write` | Write content to a process's stdin |
| `process_signal` | Send an OS-native signal to a process |
| `process_list` | List all managed processes with summary info |
| `process_inspect` | Get detailed info about a process |
| `process_kill` | Kill a single process (output data retained) |
| `process_kill_all` | Kill all managed processes |
| `process_clear` | Clear I/O records for a process |
| `process_cleanup` | Remove a terminated process and all its data |
| `process_screen` | Snapshot the live screen of a PTY process (for full-screen TUIs) |
| `program_query` | Look up whether a program needs a PTY (persistent registry) |
| `program_record` | Record a confirmed program fact in the persistent registry |

## Tools Reference

### process_start

Start a subprocess and begin capturing its output.

- `command` (required): Executable or command to run
- `args` (optional): List of command-line arguments
- `cwd` (optional): Working directory
- `env` (optional): Environment variables (merged with current env)
- `timeout_ms` (optional): Idle timeout in milliseconds (0 = no timeout)
- `pty` (optional, default `false`): Run on a virtual PTY instead of
  pipes — set `true` for programs that check `isatty()` (ssh, gdb,
  psql, REPLs) or render full-screen TUIs (vim, htop, less); when in
  doubt, use `true` (see [Virtual PTY support](#virtual-pty-support))

Returns: `id`, `os_pid`, `status`

### process_read

Read captured output from a process. Resets the idle timer.

- `id` (required): Internal process ID
- `source` (optional): `stdout`, `stderr`, `stdin`, or `both` (default: `both`)
- `duration` (optional): How far back to read (default: 1000)
- `unit` (optional): Time unit — `ns`, `us`, `ms`, `s` (default: `ms`)

Returns: List of records with `timestamp`, `source`, `content`

### process_read_new

Read output produced since the last call to this tool, for the requested
source. Resets the idle timer.

- `id` (required): Internal process ID
- `source` (optional): `stdout`, `stderr`, or `both` (default: `both`)

The server remembers the read position per process and per source, so
repeated calls return each record exactly once, in insertion order.
Only the requested source's cursor advances — reading `stdout` never
causes `stderr` records to be skipped and vice versa. `process_read`
(time-window) does not affect these cursors; `process_clear` resets
them.

Returns: List of records with `timestamp`, `source`, `content`

### process_write

Write content to the process's stdin. Only works while running.

- `id` (required): Internal process ID
- `content` (required): String to write

### process_signal

Send an OS signal. On Windows, supports `CTRL_C_EVENT` (0) and
`CTRL_BREAK_EVENT` (1). On POSIX, supports the full signal set
(SIGTERM, SIGKILL, SIGINT, etc.).

- `id` (required): Internal process ID
- `signal` (required): Signal name or number

### process_list

List all managed processes.

Returns: Array of `{id, os_pid, status, timeout_ms, inactive_duration_ms, io_count}`

### process_inspect

Get full details of a single process.

- `id` (required): Internal process ID

Returns: All metadata plus `io_count`, `stdout_count`, `stderr_count`, `stdin_count`

### process_kill

Kill a single process. Output data is retained for later reading.

- `id` (required): Internal process ID

### process_kill_all

Kill all managed processes immediately.

### process_clear

Clear all I/O records for a process. The table remains, records are deleted.

- `id` (required): Internal process ID

### process_cleanup

Remove a terminated process and all its data. Only allowed for `exited`
or `killed` processes.

- `id` (required): Internal process ID

## Virtual PTY Support

By default Portal runs programs on OS pipes. Programs that check
`isatty()` (ssh, gdb, psql, interactive REPLs) or render full-screen
TUIs (vim, htop, less) need a virtual PTY instead: pass `"pty": true`
to `process_start`. On Windows this uses ConPTY (Windows 10 1809+);
on POSIX it uses ptyprocess.

PTY mode differences from pipe mode:
- stdout and stderr are merged into one console stream (`process_read`
  with `source="stderr"` returns empty)
- records are arbitrary chunks, not lines — a line may span multiple
  records, and prompts may arrive without a trailing newline
- input you write is echoed back into the output stream (real terminal
  behavior) — treat echoes as your own input
- to interrupt a PTY process, write `` and a carriage return as two separate writes (Ctrl+C then Enter — split delivery is measurably more reliable) via
  `process_write`; a `KeyboardInterrupt` traceback is expected output
- `process_signal` on PTY processes supports only SIGTERM (terminate,
  a hard kill on Windows), SIGKILL (kill) and CTRL_C_EVENT (graceful
  Ctrl+C); other signals are rejected
- exit code is `null` on Windows (ConPTY exposes none)

`process_screen` snapshots the live screen of a PTY process — use it
for full-screen TUIs, whose record streams are garbled fragments.
Passing `cols`/`rows` resizes the live PTY first; omitting them is a
pure snapshot. The screen stays queryable after exit until
`process_cleanup`.

### Choosing `pty`

| Signal | Examples | Decision |
|--------|----------|----------|
| Checks `isatty()` | ssh, gdb, psql, mysql, telnet, REPLs | `pty: true` |
| Full-screen TUI | vim, htop, top, less, man | `pty: true` |
| Interactive flags | `-i` / `-it` / `-t` | `pty: true` |
| One-shot / batch | `python -c`, build commands | `pty: false` |
| Paged one-shots | git log/diff, less | pipe mode + `--no-pager`/`GIT_PAGER=cat` |

When in doubt, use `pty: true` — a non-interactive program tolerates
a PTY; an interactive one without one hangs.

### Program registry

`program_query` / `program_record` maintain a persistent, machine-global
registry (`programs.db` in the platform app-data dir, or
`PORTAL_DATA_DIR` if set) of which executables need a PTY. Agents
record conclusions after first encounters — including negatives.
Repeated confirmation increments `confirmed_count` (>= 2 means
settled); recording the opposite value resets it (a correction).

## Process Lifecycle

```
START → RUNNING → EXITED  → (read-only, data retained)
                → KILLED  → (read-only, data retained)
                → timeout → KILL + CLEANUP (data removed)
```

READ and WRITE operations reset the idle timer, preventing timeout kills.

## ANSI Stripping

All color codes and cursor movement sequences (`\x1b[...m`, `\x1b[...J`,
etc.) are stripped from output before storage. In pipe mode content is
otherwise stored as-is, preserving whatever newline conventions the
process uses; in PTY mode bare carriage returns (`\r`) are also removed
(ConPTY emits `\r\n`).

## Architecture

```
MCP Client
    │ JSON-RPC (stdio)
    ▼
Portal MCP Server
  ├── ProcessManager (lifecycle + timeout monitor)
  │     └── ManagedProcess (per-process wrapper)
  │           └── asyncio.subprocess.Process
  └── Database (SQLite via aiosqlite)
        ├── processes table (metadata)
        └── proc_<id> tables (I/O records)
```

## Development

```bash
# Sync the uv environment (includes pytest)
uv sync

# Run tests
uv run pytest tests/ -v
```

## License

[MIT](LICENSE) — free to use, modify, distribute, and integrate into
commercial projects, with attribution.

## Author

Developed and maintained by [TieFeiyu](https://github.com/tiefeiyu).
