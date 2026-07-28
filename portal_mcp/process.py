"""ManagedProcess — wraps an asyncio subprocess with I/O capture."""
import asyncio
import time

from portal_mcp.ansi import strip_ansi
from portal_mcp.database import Database


class ManagedProcess:
    """Wraps an asyncio.subprocess.Process with background I/O capture.

    Reads from stdout and stderr in background tasks, stripping ANSI
    sequences and writing each line to the per-process SQLite table.

    The caller (ProcessManager) is responsible for:
    - Creating the subprocess via asyncio.create_subprocess_exec
    - Creating the DB process row and proc_<id> table
    - Constructing this wrapper
    - Calling start_background_readers() after construction
    """

    def __init__(
        self,
        proc_id: int,
        process: asyncio.subprocess.Process,
        timeout_ms: int,
    ):
        self._id = proc_id
        self._process = process
        self._timeout_ms = timeout_ms
        self._reader_tasks: list[asyncio.Task] = []
        self._exit_code: int | None = None
        self._killed = False

    @property
    def id(self) -> int:
        return self._id

    @property
    def os_pid(self) -> int:
        return self._process.pid

    @property
    def status(self) -> str:
        if self._killed:
            return "killed"
        if self._exit_code is not None:
            return "exited"
        return "running"

    async def start_background_readers(self, db: Database) -> None:
        """Start background tasks that read stdout and stderr.

        Must be called after construction. The tasks run until the
        process pipes are closed (process exits).
        """
        if self._process.stdout:
            self._reader_tasks.append(
                asyncio.create_task(
                    self._read_pipe(db, self._process.stdout, source=1)
                )
            )
        if self._process.stderr:
            self._reader_tasks.append(
                asyncio.create_task(
                    self._read_pipe(db, self._process.stderr, source=2)
                )
            )

    async def _read_pipe(
        self, db: Database, pipe: asyncio.StreamReader, source: int
    ) -> None:
        """Read lines from a pipe, strip ANSI, and insert into DB."""
        try:
            while True:
                line = await pipe.readline()
                if not line:
                    break
                content = line.decode("utf-8", errors="replace")
                content = strip_ansi(content)
                await db.insert_record(
                    self._id, time.time_ns(), source, content
                )
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def write_stdin(self, db: Database, content: str) -> None:
        """Write content to the process stdin and record it in the DB."""
        if self.status != "running":
            raise RuntimeError(
                f"Process {self._id} is not running (status={self.status})"
            )
        if self._process.stdin is None:
            raise RuntimeError("Process stdin is not available")
        self._process.stdin.write(content.encode("utf-8"))
        await self._process.stdin.drain()
        await db.insert_record(
            self._id, time.time_ns(), 0, content
        )

    async def send_signal(self, sig: int) -> None:
        """Send an OS signal to the process."""
        if self.status != "running":
            raise RuntimeError(
                f"Process {self._id} is not running (status={self.status})"
            )
        self._process.send_signal(sig)

    async def wait_exit(self) -> int:
        """Wait for the process to exit and return the exit code."""
        if self._exit_code is not None:
            return self._exit_code
        self._exit_code = await self._process.wait()
        # Wait for background readers to finish reading remaining output
        if self._reader_tasks:
            done, _ = await asyncio.wait(
                self._reader_tasks, timeout=5.0
            )
            for task in self._reader_tasks:
                if not task.done():
                    task.cancel()
        return self._exit_code

    async def kill(self) -> None:
        """Force-kill the process."""
        if self._killed:
            return
        self._killed = True
        try:
            self._process.kill()
        except ProcessLookupError:
            pass
        # Wait briefly for process to die
        try:
            self._exit_code = await asyncio.wait_for(
                self._process.wait(), timeout=3.0
            )
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        # Cancel background readers
        for task in self._reader_tasks:
            if not task.done():
                task.cancel()
