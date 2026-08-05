"""Portal MCP Server — entry point and tool registration."""
import os
import sys

from mcp.server import Server
from mcp.server.models import (
    InitializationOptions,
    ServerCapabilities,
)
import mcp.server.stdio
import mcp.types as types
from portal_mcp.database import Database
from portal_mcp.manager import ProcessManager
from portal_mcp.paths import registry_db_path
from portal_mcp.registry import Registry


SERVER_NAME = "portal"
SERVER_VERSION = "0.1.0"

def _default_db_path() -> str:
    """Per-instance session DB path.

    One SQLite file per server instance (pid + short random suffix):
    the session DB is created fresh on every startup, and a
    per-instance name means a lingering/zombie server — or a second
    instance in the same process (tests) — can never lock the file and
    block a new instance from starting in the same working directory.
    """
    from uuid import uuid4

    return os.path.join(
        ".portal", f"portal-{os.getpid()}-{uuid4().hex[:8]}.db"
    )


def _signal_help() -> str:
    """Platform-specific signal help text."""
    import signal

    common = "Send a signal to a process by name or number."
    if sys.platform == "win32":
        return (
            f"{common} Windows supports: "
            "CTRL_C_EVENT (0), CTRL_BREAK_EVENT (1). "
            "SIGTERM is mapped to TerminateProcess. "
            "For PTY processes: SIGTERM -> terminate (hard kill on "
            "Windows), SIGKILL -> kill, CTRL_C_EVENT -> Ctrl+C; for "
            "a graceful interrupt use process_write with \u0003 followed by a carriage return."
        )
    else:
        names = [
            s
            for s in dir(signal)
            if s.startswith("SIG") and not s.startswith("SIG_")
        ]
        return (
            f"{common} Available signals: {', '.join(sorted(names))}."
        )


async def create_server(
    db_path: str | None = None,
) -> tuple[ProcessManager, Database]:
    """Create and configure the Portal MCP server.

    Args:
        db_path: Path to SQLite database. Defaults to a per-instance
            '.portal/portal-<pid>.db' in the current directory (fresh
            per startup; pid-scoped so concurrent instances never
            collide on the file lock).

    Returns:
        Tuple of (ProcessManager, Database) for testing.
    """
    if db_path is None:
        db_path = _default_db_path()

    # Create the database directory if it doesn't exist
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # Delete old database for fresh start
    if os.path.exists(db_path):
        os.unlink(db_path)

    db = Database(db_path)
    await db.initialize()

    registry = Registry(str(registry_db_path()))
    await registry.open()

    manager = ProcessManager(db, registry=registry)
    await manager.start_monitor()

    return manager, db


def main():
    """Entry point for the Portal MCP server."""
    import asyncio

    async def run():
        db_path = os.environ.get("PORTAL_DB_PATH", _default_db_path())
        manager, db = await create_server(db_path)
        server = Server(SERVER_NAME, version=SERVER_VERSION)

        @server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name="process_start",
                    description=(
                        "Start a subprocess for interactive use. "
                        "Use this for interactive programs like SSH, "
                        "GDB, psql, python REPL, etc. — not for "
                        "simple one-shot commands. Returns the "
                        "internal process ID, OS PID, and initial "
                        "status.\n"
                        "\n"
                        "pty — default false for backward "
                        "compatibility; this is NOT a "
                        "recommendation. Set true for anything "
                        "interactive or TUI (ssh, gdb, psql/mysql, "
                        "REPLs, vim, htop, top, less; anything with "
                        "-i/-it/-t flags). Consult program_query for "
                        "this executable BEFORE starting. When in "
                        "doubt, true — a non-interactive program "
                        "tolerates a PTY; an interactive one without "
                        "one hangs. Exception: one-shot commands that "
                        "page output (git log/diff, less) — prefer "
                        "pipe mode with --no-pager/GIT_PAGER=cat."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": (
                                    "Executable or command to run."
                                ),
                            },
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Command-line arguments.",
                                "default": [],
                            },
                            "cwd": {
                                "type": "string",
                                "description": "Working directory.",
                            },
                            "env": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "string"
                                },
                                "description": (
                                    "Environment variables "
                                    "(merged with current env)."
                                ),
                            },
                            "timeout_ms": {
                                "type": "integer",
                                "description": (
                                    "Idle timeout in milliseconds. "
                                    "Process is killed and cleaned up "
                                    "if no tool interaction occurs for "
                                    "this duration. 0 = no timeout."
                                ),
                                "default": 0,
                            },
                            "pty": {
                                "type": "boolean",
                                "description": (
                                    "Run on a virtual PTY instead of "
                                    "pipes. True for programs that "
                                    "need a terminal (see tool "
                                    "description). Default false for "
                                    "backward compatibility — NOT a "
                                    "recommendation."
                                ),
                                "default": False,
                            },
                        },
                        "required": ["command"],
                    },
                ),
                types.Tool(
                    name="process_read",
                    description=(
                        "Read output from a process. Reads records "
                        "from the specified time window. Resets the "
                        "process idle timer.\n"
                        "\n"
                        "For PTY processes stderr is merged into "
                        "stdout (source=stderr reads return empty) "
                        "and records are arbitrary chunks, not lines "
                        "— a line may span multiple records. Prompts "
                        "may arrive without a trailing newline. Read "
                        "with a generous duration — the default "
                        "window is only 1s."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                            "source": {
                                "type": "string",
                                "enum": [
                                    "stdout", "stderr", "stdin", "both"
                                ],
                                "description": (
                                    "Which output stream to read."
                                ),
                                "default": "both",
                            },
                            "duration": {
                                "type": "integer",
                                "description": (
                                    "How far back to read "
                                    "(in the specified unit)."
                                ),
                                "default": 1000,
                            },
                            "unit": {
                                "type": "string",
                                "enum": ["ns", "us", "ms", "s"],
                                "description": (
                                    "Time unit for duration."
                                ),
                                "default": "ms",
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="process_write",
                    description=(
                        "Write content to a process's stdin. "
                        "Only available while the process is running.\n"
                        "\n"
                        "Input you write reappears in the output "
                        "stream (terminal echo) — treat it as your "
                        "own input, not program output, and do not "
                        "re-send it. To interrupt a PTY process, "
                        "send \u0003 followed by a carriage return (Ctrl+C then Enter — ConPTY is line-buffered, the CR triggers it); a KeyboardInterrupt "
                        "traceback in output is expected, not an "
                        "error. process_signal/process_kill are "
                        "hard-stop fallbacks."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                            "content": {
                                "type": "string",
                                "description": (
                                    "Content to write to stdin."
                                ),
                            },
                        },
                        "required": ["id", "content"],
                    },
                ),
                types.Tool(
                    name="process_signal",
                    description=_signal_help(),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                            "signal": {
                                "type": "string",
                                "description": (
                                    "OS signal name (e.g., SIGTERM, "
                                    "SIGKILL, SIGINT) or signal number."
                                ),
                            },
                        },
                        "required": ["id", "signal"],
                    },
                ),
                types.Tool(
                    name="process_list",
                    description=(
                        "List all managed processes with summary info: "
                        "id, os_pid, status, timeout, idle duration, "
                        "and I/O record count."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                types.Tool(
                    name="process_inspect",
                    description=(
                        "Get detailed information about a single "
                        "process including all metadata and per-stream "
                        "I/O counts.\n"
                        "\n"
                        "For PTY processes: stderr_count is always 0 "
                        "(merged stream) and I/O counts are "
                        "chunk-based, not line-based; exit_code is "
                        "null on Windows (ConPTY exposes none)."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="process_kill",
                    description=(
                        "Kill a single process. Its output data is "
                        "retained for reading. Use process_cleanup "
                        "to remove it."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="process_kill_all",
                    description="Kill all managed processes.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                types.Tool(
                    name="process_clear",
                    description=(
                        "Clear all I/O records for a process. "
                        "The process table remains, but records "
                        "are deleted."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="process_cleanup",
                    description=(
                        "Remove a terminated process and all its data. "
                        "Only allowed for processes with status "
                        "'exited' or 'killed'."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="process_screen",
                    description=(
                        "Snapshot the live screen of a PTY process. "
                        "For full-screen TUIs (vim, htop, less): the "
                        "record stream is garbled fragments — use "
                        "this tool instead of process_read. Screen "
                        "remains queryable after exit until "
                        "process_cleanup. Snapshot is pure; passing "
                        "cols/rows resizes the live PTY first. "
                        "buffer is 'primary' or 'alternate' (the "
                        "TUI's alternate screen)."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                            "cols": {
                                "type": "integer",
                                "description": (
                                    "Optional: resize width first."
                                ),
                            },
                            "rows": {
                                "type": "integer",
                                "description": (
                                    "Optional: resize height first."
                                ),
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="program_query",
                    description=(
                        "Look up whether a program needs a PTY in the "
                        "persistent program registry. Call BEFORE "
                        "process_start. A miss is a normal result, not "
                        "an error — it means apply the decision rules "
                        "in the instructions. confirmed_count >= 2 "
                        "means settled; a single confirmation is a "
                        "hint — re-verify on first use."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "program": {
                                "type": "string",
                                "description": (
                                    "Executable name, e.g. 'ssh'."
                                ),
                            },
                        },
                        "required": ["program"],
                    },
                ),
                types.Tool(
                    name="program_record",
                    description=(
                        "Record a confirmed program fact in the "
                        "persistent registry after observing its "
                        "behavior: needs_pty true if it required a "
                        "terminal (TTY error, hang in pipe mode, or "
                        "TUI rendering with pty:true), false if it ran "
                        "fine without one. Record every first-encounter "
                        "conclusion, including negatives. notes should "
                        "carry flag-specific caveats (e.g. 'docker run "
                        "-it only, not docker build')."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "program": {
                                "type": "string",
                                "description": "Executable name.",
                            },
                            "needs_pty": {
                                "type": "boolean",
                                "description": (
                                    "Whether the program needs a PTY."
                                ),
                            },
                            "notes": {
                                "type": "string",
                                "description": (
                                    "Optional caveats or context."
                                ),
                            },
                        },
                        "required": ["program", "needs_pty"],
                    },
                ),
            ]

        @server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict
        ) -> list[types.TextContent]:
            try:
                if name == "process_start":
                    result = await manager.start(
                        command=arguments["command"],
                        args=arguments.get("args", []),
                        cwd=arguments.get("cwd"),
                        env=arguments.get("env"),
                        timeout_ms=arguments.get("timeout_ms", 0),
                        pty=arguments.get("pty", False),
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Process started. "
                                f"ID={result['id']}, "
                                f"OS_PID={result['os_pid']}, "
                                f"status={result['status']}"
                            ),
                        )
                    ]

                elif name == "process_read":
                    records = await manager.read(
                        proc_id=arguments["id"],
                        source=arguments.get("source", "both"),
                        duration=arguments.get("duration", 1000),
                        unit=arguments.get("unit", "ms"),
                    )
                    if not records:
                        return [
                            types.TextContent(
                                type="text",
                                text=(
                                    "No output records found in the "
                                    "specified time window."
                                ),
                            )
                        ]
                    source_label = {0: "STDIN", 1: "STDOUT", 2: "STDERR"}
                    lines = []
                    for r in records:
                        label = source_label.get(
                            r["source"], f"SRC{r['source']}"
                        )
                        lines.append(
                            f"[{r['timestamp']}] [{label}] "
                            f"{r['content']}"
                        )
                    return [
                        types.TextContent(
                            type="text", text="".join(lines)
                        )
                    ]

                elif name == "process_write":
                    result = await manager.write(
                        proc_id=arguments["id"],
                        content=arguments["content"],
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Written {result['bytes_written']} "
                                f"bytes to process {result['id']} stdin."
                            ),
                        )
                    ]

                elif name == "process_signal":
                    sig = arguments["signal"]
                    try:
                        sig = int(sig)
                    except ValueError:
                        pass
                    result = await manager.send_signal(
                        proc_id=arguments["id"],
                        sig=sig,
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Signal {result['signal_sent']} sent "
                                f"to process {result['id']}."
                            ),
                        )
                    ]

                elif name == "process_list":
                    processes = await manager.list_all()
                    if not processes:
                        return [
                            types.TextContent(
                                type="text",
                                text="No managed processes.",
                            )
                        ]
                    lines = [
                        "ID | OS_PID | STATUS  | TIMEOUT_MS | "
                        "INACTIVE_MS | IO_COUNT",
                        "-" * 65,
                    ]
                    for p in processes:
                        lines.append(
                            f"{p['id']:2} | {p['os_pid']:6} | "
                            f"{p['status']:7} | "
                            f"{p['timeout_ms']:10} | "
                            f"{p['inactive_duration_ms']:11} | "
                            f"{p['io_count']:8}"
                        )
                    return [
                        types.TextContent(
                            type="text", text="\n".join(lines)
                        )
                    ]

                elif name == "process_inspect":
                    info = await manager.inspect(arguments["id"])
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Process {info['id']}:\n"
                                f"  OS PID:     {info['os_pid']}\n"
                                f"  Status:     {info['status']}\n"
                                f"  Command:    {info['command']}\n"
                                f"  Args:       {info['args']}\n"
                                f"  CWD:        {info['cwd']}\n"
                                f"  Env:        {info['env']}\n"
                                f"  Timeout:    {info['timeout_ms']} ms\n"
                                f"  Exit Code:  {info['exit_code']}\n"
                                f"  IO Records: {info['io_count']} "
                                f"total "
                                f"(stdout={info['stdout_count']}, "
                                f"stderr={info['stderr_count']}, "
                                f"stdin={info['stdin_count']})\n"
                            ),
                        )
                    ]

                elif name == "process_kill":
                    result = await manager.do_kill(arguments["id"])
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Process {result['id']} killed. "
                                f"Status: {result['status']}."
                            ),
                        )
                    ]

                elif name == "process_kill_all":
                    result = await manager.kill_all()
                    return [
                        types.TextContent(
                            type="text",
                            text=f"Killed {result['killed']} process(es).",
                        )
                    ]

                elif name == "process_clear":
                    result = await manager.clear(arguments["id"])
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Process {result['id']} "
                                f"I/O records cleared."
                            ),
                        )
                    ]

                elif name == "process_cleanup":
                    result = await manager.do_cleanup(arguments["id"])
                    return [
                        types.TextContent(
                            type="text",
                            text=f"Process {result['id']} cleaned up.",
                        )
                    ]

                elif name == "process_screen":
                    snap = await manager.screen(
                        proc_id=arguments["id"],
                        cols=arguments.get("cols"),
                        rows=arguments.get("rows"),
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Process {snap['id']} "
                                f"(status={snap['status']}, "
                                f"buffer={snap['buffer']}, "
                                f"{snap['cols']}x{snap['rows']}, "
                                f"cursor=({snap['cursor_x']},"
                                f"{snap['cursor_y']})):\n"
                                f"{snap['content']}"
                            ),
                        )
                    ]

                elif name == "program_query":
                    result = await manager.query_program(
                        arguments["program"]
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"{result['program']}: "
                                + (
                                    f"needs_pty={result['needs_pty']}, "
                                    f"confirmed x{result['confirmed_count']}"
                                    + (
                                        f" — {result['notes']}"
                                        if result.get("notes")
                                        else ""
                                    )
                                    if result.get("known")
                                    else "not in registry — apply decision rules"
                                )
                            ),
                        )
                    ]

                elif name == "program_record":
                    result = await manager.record_program(
                        program=arguments["program"],
                        needs_pty=arguments["needs_pty"],
                        notes=arguments.get("notes"),
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Recorded {result['program']}: "
                                f"needs_pty={result['needs_pty']} "
                                f"(confirmed x{result['confirmed_count']})."
                            ),
                        )
                    ]

                else:
                    return [
                        types.TextContent(
                            type="text",
                            text=f"Unknown tool: {name}",
                        )
                    ]

            except ValueError as e:
                return [
                    types.TextContent(type="text", text=f"Error: {e}")
                ]
            except RuntimeError as e:
                return [
                    types.TextContent(type="text", text=f"Error: {e}")
                ]
            except Exception as e:
                return [
                    types.TextContent(
                        type="text",
                        text=f"Unexpected error: "
                        f"{type(e).__name__}: {e}",
                    )
                ]

        async with mcp.server.stdio.stdio_server() as (
            read_stream,
            write_stream,
        ):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=SERVER_NAME,
                    server_version=SERVER_VERSION,
                    instructions=(
                        "# Portal — Interactive Process Management\n"
                        "\n"
                        "Portal manages interactive subprocesses. Use portal "
                        "tools whenever a command needs to be interacted "
                        "with — not just run once.\n"
                        "\n"
                        "## Core rule\n"
                        "\n"
                        "Think of Bash/PowerShell as \"run this and tell me "
                        "what it printed.\"\n"
                        "\n"
                        "Think of portal as \"start this, and I'll talk "
                        "back to it.\"\n"
                        "\n"
                        "If the task requires any back-and-forth with the "
                        "running process, portal is the only right choice.\n"
                        "\n"
                        "## When to use portal\n"
                        "\n"
                        "Use portal for commands that:\n"
                        "- Ask questions or wait for input (passwords, "
                        "confirmations, selections, Y/n prompts)\n"
                        "- Invoke any REPL, shell, or console environment "
                        "(language REPLs, database CLIs, interactive "
                        "debuggers, system shells, application consoles…)\n"
                        "- Keep running until you tell them to stop "
                        "(servers in foreground, monitors, watchers, "
                        "live-tailing logs, build/dev watch modes…)\n"
                        "- Emit progressive output you need to check "
                        "mid-run before deciding what to do next\n"
                        "- Run over a remote connection (SSH, serial "
                        "consoles, telnet, any remote-exec tool)\n"
                        "- Use interactive flags: `-i`, `--interactive`, "
                        "`-t`, `--tty`. These flags go in the command's "
                        "`args` — they configure the program's own "
                        "terminal. The `pty` param on `process_start` "
                        "grants the LOCAL terminal. Tools like docker "
                        "need both.\n"
                        "- Are long-running and you might need to inspect, "
                        "signal, or interact with them partway through\n"
                        "\n"
                        "Use Bash/PowerShell only for fire-and-forget "
                        "one-shot commands: run → print output → exit. "
                        "If there is any chance the command will wait for "
                        "you, use portal.\n"
                        "\n"
                        "## Typical workflow\n"
                        "\n"
                        "1. `process_start` — start the command (set "
                        "timeout_ms on long-idle processes)\n"
                        "2. `process_read`  — check what it printed so "
                        "far\n"
                        "3. `process_write` — send input when it's "
                        "waiting\n"
                        "4. Repeat 2–3 as the conversation with the "
                        "process continues\n"
                        "5. To interrupt a PTY process, `process_write` "
                        "with `\u0003` then a carriage return (Ctrl+C + Enter — ConPTY is line-buffered); `process_signal` / "
                        "`process_kill` are hard-stop fallbacks\n"
                        "6. `process_cleanup` — remove finished process "
                        "data\n"
                        "\n"
                        "Also use `process_list` to see what's running "
                        "and `process_inspect` to check details on a "
                        "specific process.\n"
                        "\n"
                        "## When a program needs `pty: true`\n"
                        "\n"
                        "Set `pty: true` on `process_start` for:\n"
                        "- Programs that check isatty(): ssh, gdb, "
                        "psql, mysql, telnet, interactive REPLs\n"
                        "- Full-screen TUIs: vim, htop, top, less, man\n"
                        "- Anything with -i/-it/-t flags\n"
                        "\n"
                        "Prefer pipe mode (pty: false, the default) "
                        "for one-shot scripts and batch commands — "
                        "and for one-shot commands that page output "
                        "(git log/diff, less) use --no-pager or "
                        "GIT_PAGER=cat, since with pty: true they sit "
                        "in the pager and look hung.\n"
                        "\n"
                        "When in doubt, use pty: true — a "
                        "non-interactive program tolerates a PTY; an "
                        "interactive one without one hangs.\n"
                        "\n"
                        "## Decision priority: registry first\n"
                        "\n"
                        "Before starting a program, call "
                        "`program_query`. A hit settles it (check "
                        "`notes` for flag-specific caveats — e.g. a "
                        "docker entry may only cover `docker run -it`, "
                        "not `docker build`). A miss is normal — apply "
                        "the rules above. confirmed_count >= 2 means "
                        "settled; a single confirmation is a hint — "
                        "re-verify on first use.\n"
                        "\n"
                        "## Hang check and escalation\n"
                        "\n"
                        "When a process seems hung, read with a "
                        "generous window (duration=10000, unit ms — "
                        "the default 1s window misses older output "
                        "and looks identical to a hang). Treat as a "
                        "hang only if reads keep returning empty AND "
                        "process_list shows io_count unchanged after "
                        "several seconds. Set timeout_ms on every "
                        "escalated attempt so a real hang "
                        "self-terminates. On hang: kill, restart with "
                        "pty: true. If a pty: true process sits in a "
                        "pager, send q then Enter (or \u0003 + Enter), kill, restart "
                        "without pty.\n"
                        "\n"
                        "## Registry feedback loop\n"
                        "\n"
                        "Record every first-encounter conclusion via "
                        "`program_record` — including negatives "
                        "(needs_pty=false when the program ran fine "
                        "without a PTY). The registry is "
                        "agent-populated; it only helps future "
                        "sessions if every encounter is recorded.\n"
                        "\n"
                        "## Remember\n"
                        "\n"
                        "Bash/PowerShell cannot write to stdin mid-flight."
                        " If your next step is \"type into the running "
                        "process,\" you should have started it with "
                        "portal in the first place. When in doubt, portal "
                        "first — you can always fire-and-forget with "
                        "portal too."
                    ),
                    capabilities=ServerCapabilities(
                        tools=types.ToolsCapability(),
                    ),
                ),
            )

        # Cleanup on exit
        await manager.shutdown()
        await db.close()
        # Best-effort: remove this instance's session DB (per-instance
        # files would otherwise accumulate in .portal/).
        if not os.environ.get("PORTAL_DB_PATH") and os.path.exists(db_path):
            try:
                os.unlink(db_path)
            except OSError:
                pass

    asyncio.run(run())


if __name__ == "__main__":
    main()
