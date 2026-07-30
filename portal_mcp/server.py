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


SERVER_NAME = "portal"
SERVER_VERSION = "0.1.0"


def _signal_help() -> str:
    """Platform-specific signal help text."""
    import signal

    common = "Send a signal to a process by name or number."
    if sys.platform == "win32":
        return (
            f"{common} Windows supports: "
            "CTRL_C_EVENT (0), CTRL_BREAK_EVENT (1). "
            "SIGTERM is mapped to TerminateProcess."
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
        db_path: Path to SQLite database. Defaults to 'portal.db'
            in the current directory.

    Returns:
        Tuple of (ProcessManager, Database) for testing.
    """
    if db_path is None:
        db_path = "portal.db"

    # Delete old database for fresh start
    if os.path.exists(db_path):
        os.unlink(db_path)

    db = Database(db_path)
    await db.initialize()

    manager = ProcessManager(db)
    await manager.start_monitor()

    return manager, db


def main():
    """Entry point for the Portal MCP server."""
    import asyncio

    async def run():
        db_path = os.environ.get("PORTAL_DB_PATH", "portal.db")
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
                        "internal process ID, OS PID, and initial status."
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
                        },
                        "required": ["command"],
                    },
                ),
                types.Tool(
                    name="process_read",
                    description=(
                        "Read output from a process. Reads records "
                        "from the specified time window. Resets the "
                        "process idle timer."
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
                        "Only available while the process is running."
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
                        "I/O counts."
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
                        "`-t`, `--tty`\n"
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
                        "5. `process_signal` or `process_kill` — stop "
                        "the process\n"
                        "6. `process_cleanup` — remove finished process "
                        "data\n"
                        "\n"
                        "Also use `process_list` to see what's running "
                        "and `process_inspect` to check details on a "
                        "specific process.\n"
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

    asyncio.run(run())


if __name__ == "__main__":
    main()
