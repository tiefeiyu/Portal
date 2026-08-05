"""ProcessManager — manages the lifecycle of all ManagedProcess instances."""
import asyncio
import os
import signal
import time

from portal_mcp.database import Database
from portal_mcp.process import ManagedProcess
from portal_mcp.registry import Registry


def _parse_duration(duration: int, unit: str) -> int:
    """Convert duration + unit to nanoseconds."""
    multipliers = {
        "ns": 1,
        "us": 1_000,
        "ms": 1_000_000,
        "s": 1_000_000_000,
    }
    if unit not in multipliers:
        raise ValueError(
            f"Unknown unit '{unit}'. Supported: ns, us, ms, s"
        )
    return duration * multipliers[unit]


def _source_to_codes(source: str) -> list[int]:
    """Map source string to list of DB source codes."""
    source_map = {
        "stdout": [1],
        "stderr": [2],
        "stdin": [0],
        "both": [1, 2],
    }
    if source not in source_map:
        raise ValueError(
            f"Unknown source '{source}'. "
            f"Supported: stdout, stderr, stdin, both"
        )
    return source_map[source]


class ProcessManager:
    """Manages all subprocesses.

    Maintains a dict of ManagedProcess instances indexed by internal ID.
    Runs a background timeout monitor that kills idle processes.
    """

    def __init__(self, db: Database, registry: Registry | None = None):
        self._db = db
        self._registry = registry
        self._processes: dict[int, ManagedProcess] = {}
        self._monitor_task: asyncio.Task | None = None

    async def start_monitor(self) -> None:
        """Start the background timeout monitor."""
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self) -> None:
        """Periodically check and kill timed-out processes."""
        while True:
            try:
                await asyncio.sleep(1)
                await self._check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _check_timeouts(self) -> None:
        """Check all running processes for timeout violations."""
        now = time.time_ns()
        processes = await self._db.get_all_processes()
        for proc in processes:
            if proc["status"] != "running":
                continue
            timeout_ms = proc["timeout_ms"]
            if timeout_ms <= 0:
                continue
            inactive_ns = now - proc["last_active_at"]
            if inactive_ns > timeout_ms * 1_000_000:
                mp = self._processes.get(proc["id"])
                if mp:
                    try:
                        await mp.kill()
                    except Exception:
                        pass
                await self._db.update_status(proc["id"], "killed")
                await self._db.cleanup_process(proc["id"])
                self._processes.pop(proc["id"], None)

    async def start(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_ms: int = 0,
        pty: bool = False,
    ) -> dict:
        """Start a new subprocess.

        Args:
            pty: When True, spawn on a virtual PTY (ConPTY on Windows)
                instead of pipes — for programs that check isatty()
                or render full-screen TUIs.

        Returns:
            Dict with id, os_pid, status keys.
        """
        args = args or []
        now = time.time_ns()
        proc_id = await self._db.create_process(
            command=command,
            args=args,
            cwd=cwd,
            env=env,
            timeout_ms=timeout_ms,
            started_at=now,
        )
        await self._db.create_proc_table(proc_id)

        if pty:
            from portal_mcp.pty_process import PtyProcess  # lazy import

            try:
                mp = await PtyProcess.create(
                    self._db, proc_id, command, args, cwd, env,
                    timeout_ms=timeout_ms,
                )
            except Exception:
                await self._db.cleanup_process(proc_id)
                raise
            await self._db.update_os_pid(proc_id, mp.os_pid)
        else:
            # Merge env with current env if provided
            process_env = None
            if env:
                process_env = os.environ.copy()
                process_env.update(env)

            try:
                subproc = await asyncio.create_subprocess_exec(
                    command,
                    *args,
                    cwd=cwd,
                    env=process_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                )
            except Exception:
                await self._db.cleanup_process(proc_id)
                raise

            await self._db.update_os_pid(proc_id, subproc.pid)

            mp = ManagedProcess(
                proc_id=proc_id,
                process=subproc,
                timeout_ms=timeout_ms,
            )
            await mp.start_background_readers(self._db)
        self._processes[proc_id] = mp

        # Start an exit-monitor task to update DB status on exit
        asyncio.create_task(self._watch_exit(proc_id, mp))

        return {
            "id": proc_id,
            "os_pid": mp.os_pid,
            "status": "running",
        }

    async def _watch_exit(self, proc_id: int, mp: ManagedProcess) -> None:
        """Background task: wait for process exit and update DB."""
        try:
            exit_code = await mp.wait_exit()
            status = mp.status
            await self._db.update_status(proc_id, status, exit_code)
        except Exception:
            pass

    async def read(
        self,
        proc_id: int,
        source: str = "both",
        duration: int = 1000,
        unit: str = "ms",
    ) -> list[dict]:
        """Read output records from a process.

        Args:
            proc_id: Internal process ID.
            source: "stdout", "stderr", "stdin", or "both".
            duration: Time duration to read back.
            unit: Time unit: "ns", "us", "ms", "s".

        Returns:
            List of records with timestamp, source, content.
        """
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")

        duration_ns = _parse_duration(duration, unit)
        sources = _source_to_codes(source)
        since_ts = time.time_ns() - duration_ns

        # Touch the process to reset idle timer
        await self._db.touch(proc_id)

        return await self._db.read_records(proc_id, sources, since_ts)

    async def screen(
        self, proc_id: int, cols: int | None = None, rows: int | None = None
    ) -> dict:
        """Snapshot the live screen of a PTY process.

        Pure snapshot when cols/rows are omitted (no resize side
        effect). Passing cols/rows resizes the live PTY first.
        Touches the idle timer like process_read.
        """
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")

        mp = self._processes.get(proc_id)
        if mp is None:
            raise ValueError(f"Process {proc_id} not found in manager")
        if not getattr(mp, "is_pty", False):
            raise ValueError(f"Process {proc_id} is not a PTY process")

        if cols is not None or rows is not None:
            new_rows = rows if rows is not None else mp.rows
            new_cols = cols if cols is not None else mp.cols
            await mp.resize(new_rows, new_cols)

        await self._db.touch(proc_id)
        result = mp.screen()
        result["id"] = proc_id
        result["status"] = mp.status
        return result

    async def write(self, proc_id: int, content: str) -> dict:
        """Write content to the process stdin.

        Returns:
            Dict with id, bytes_written.
        """
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")
        if proc["status"] != "running":
            raise ValueError(
                f"Process {proc_id} is not running "
                f"(status={proc['status']})"
            )

        mp = self._processes.get(proc_id)
        if mp is None:
            raise ValueError(f"Process {proc_id} not found in manager")

        await mp.write_stdin(self._db, content)
        await self._db.touch(proc_id)

        return {
            "id": proc_id,
            "bytes_written": len(content.encode("utf-8")),
        }

    async def send_signal(self, proc_id: int, sig: int | str) -> dict:
        """Send an OS signal to a process.

        Args:
            proc_id: Internal process ID.
            sig: Signal number (int) or name (str like "SIGTERM").

        Returns:
            Dict with id, signal_sent.
        """
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")

        mp = self._processes.get(proc_id)
        if mp is None:
            raise ValueError(f"Process {proc_id} not found in manager")

        # PTY processes resolve signal names themselves (their mapping
        # differs: SIGTERM -> terminate, SIGKILL -> kill, CTRL_C_EVENT
        # -> write \x03).
        if getattr(mp, "is_pty", False):
            await mp.send_signal(sig)
            return {"id": proc_id, "signal_sent": sig}

        # Convert string signal name to int (pipe mode)
        if isinstance(sig, str):
            sig_num = getattr(signal, sig, None)
            if sig_num is None:
                raise ValueError(f"Unknown signal: {sig}")
            sig = sig_num

        await mp.send_signal(sig)
        return {"id": proc_id, "signal_sent": sig}

    async def list_all(self) -> list[dict]:
        """List all managed processes with summary info."""
        now = time.time_ns()
        processes = await self._db.get_all_processes()
        result = []
        for proc in processes:
            inactive_ns = now - proc["last_active_at"]
            inactive_ms = inactive_ns // 1_000_000
            try:
                counts = await self._db.io_count(proc["id"])
            except Exception:
                counts = {"total": 0}
            result.append({
                "id": proc["id"],
                "os_pid": proc["os_pid"],
                "status": proc["status"],
                "timeout_ms": proc["timeout_ms"],
                "inactive_duration_ms": inactive_ms,
                "io_count": counts["total"],
            })
        return result

    async def inspect(self, proc_id: int) -> dict:
        """Get detailed info about a single process."""
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")
        try:
            counts = await self._db.io_count(proc_id)
        except Exception:
            counts = {"total": 0, "stdout": 0, "stderr": 0, "stdin": 0}
        result = dict(proc)
        result["io_count"] = counts["total"]
        result["stdout_count"] = counts["stdout"]
        result["stderr_count"] = counts["stderr"]
        result["stdin_count"] = counts["stdin"]
        return result

    async def do_kill(self, proc_id: int) -> dict:
        """Kill a single process."""
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")

        mp = self._processes.get(proc_id)
        if mp and mp.status == "running":
            await mp.kill()
        await self._db.update_status(proc_id, "killed")
        return {"id": proc_id, "status": "killed"}

    async def kill_all(self) -> dict:
        """Kill all managed processes."""
        count = 0
        processes = await self._db.get_all_processes()
        for proc in processes:
            mp = self._processes.get(proc["id"])
            if mp and mp.status == "running":
                await mp.kill()
                await self._db.update_status(proc["id"], "killed")
                count += 1
        return {"killed": count}

    async def query_program(self, program: str) -> dict:
        """Look up a program in the registry (miss is a normal result)."""
        if self._registry is None:
            raise ValueError("Program registry not configured")
        return await self._registry.query(program)

    async def record_program(
        self, program: str, needs_pty: bool, notes: str | None = None
    ) -> dict:
        """Record a confirmed program fact in the registry."""
        if self._registry is None:
            raise ValueError("Program registry not configured")
        return await self._registry.record(program, needs_pty, notes)

    async def clear(self, proc_id: int) -> dict:
        """Clear I/O records for a process."""
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")
        await self._db.clear_records(proc_id)
        return {"id": proc_id, "cleared": True}

    async def do_cleanup(self, proc_id: int) -> dict:
        """Remove a terminated process and its data."""
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")
        if proc["status"] == "running":
            raise ValueError(
                f"Process {proc_id} is still running — "
                f"must be exited or killed before cleanup"
            )
        await self._db.cleanup_process(proc_id)
        self._processes.pop(proc_id, None)
        return {"id": proc_id, "cleaned_up": True}

    async def shutdown(self) -> None:
        """Shut down the manager: kill all processes, cancel monitor."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        for mp in self._processes.values():
            if mp.status == "running":
                try:
                    await mp.kill()
                except Exception:
                    pass
        self._processes.clear()
        if self._registry is not None:
            await self._registry.close()
