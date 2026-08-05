# Portal MCP Server

An MCP (Model Context Protocol) server purpose-built for **interactive programs**.
Start, monitor, read/write I/O, and control interactive processes — all through
MCP tool calls.

**Use for:** SSH remote connections, GDB debugging, database CLIs (psql/mysql),
REPL environments, and other programs requiring sustained bidirectional interaction.

**Not for:** Simple one-shot commands (`ls`, `echo`, etc.) — use the built-in Shell
tool for those.

## Installation

```bash
pip install -e .
```

Requires Python 3.11+.

## Uninstall

```bash
pip uninstall portal-mcp
```

## Configuration

Add to your MCP client configuration (e.g., Claude Code):

```json
{
  "mcpServers": {
    "portal": {
      "command": "portal-mcp"
    }
  }
}
```

Or with explicit Python path:

```json
{
  "mcpServers": {
    "portal": {
      "command": "python",
      "args": ["-m", "portal_mcp.server"]
    }
  }
}
```

The `PORTAL_DB_PATH` environment variable controls the SQLite database
location (default: `.portal/portal.db` in the working directory). The
database is created fresh on every server startup.

## One-Click Install

Copy the prompt from [`llms-install.md`](llms-install.md) into your AI agent to automatically install and configure Portal.

## Tools

| Tool | Description |
|------|-------------|
| `process_start` | Start a subprocess with optional args, cwd, env, timeout |
| `process_read` | Read output records (stdout/stderr/stdin/both) within a time window |
| `process_write` | Write content to a process's stdin |
| `process_signal` | Send an OS-native signal to a process |
| `process_list` | List all managed processes with summary info |
| `process_inspect` | Get detailed info about a process |
| `process_kill` | Kill a single process (output data retained) |
| `process_kill_all` | Kill all managed processes |
| `process_clear` | Clear I/O records for a process |
| `process_cleanup` | Remove a terminated process and all its data |

### process_start

Start a subprocess and begin capturing its output.

- `command` (required): Executable or command to run
- `args` (optional): List of command-line arguments
- `cwd` (optional): Working directory
- `env` (optional): Environment variables (merged with current env)
- `timeout_ms` (optional): Idle timeout in milliseconds (0 = no timeout)

Returns: `id`, `os_pid`, `status`

### process_read

Read captured output from a process. Resets the idle timer.

- `id` (required): Internal process ID
- `source` (optional): `stdout`, `stderr`, `stdin`, or `both` (default: `both`)
- `duration` (optional): How far back to read (default: 1000)
- `unit` (optional): Time unit — `ns`, `us`, `ms`, `s` (default: `ms`)

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

## Process Lifecycle

```
START → RUNNING → EXITED  → (read-only, data retained)
                → KILLED  → (read-only, data retained)
                → timeout → KILL + CLEANUP (data removed)
```

READ and WRITE operations reset the idle timer, preventing timeout kills.

## ANSI Stripping

All color codes and cursor movement sequences (`\x1b[...m`, `\x1b[...J`,
etc.) are stripped from output before storage. Content is otherwise stored
as-is, preserving whatever newline conventions the process uses.

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
# Install dev dependencies
pip install -e .
pip install pytest pytest-asyncio

# Run tests
pytest tests/ -v
```

## License

MIT
