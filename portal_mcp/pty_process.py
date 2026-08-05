"""PtyProcess — PTY-mode twin of ManagedProcess with pyte emulation."""
import asyncio
import re
import signal
import threading
import time

from portal_mcp import pty_backend
from portal_mcp.ansi import strip_ansi
from portal_mcp.database import Database

# ESC [ ? 1047h / 1048h / 1049h  and the matching l (off) codes.
# Under pywinpty's ConPTY backend the console renders a child-written
# ESC (0x1B) as '?', so the sequence arrives as '?[?1049h'; match both
# the raw VT form and the ConPTY-rendered form.
_ALT_SCREEN_PATTERN = re.compile(r"(?:\x1b|\?)\[\?10(47|48|49)([hl])")

# Windows Python lacks the SIGKILL constant; os.kill(pid, 9) maps to
# TerminateProcess there, matching POSIX SIGKILL semantics.
_SIGKILL = getattr(signal, "SIGKILL", 9)


def _make_screen(rows: int, cols: int):
    """Lazy-import pyte and build a (primary, alternate) screen pair."""
    import pyte  # lazy import

    primary = pyte.Screen(cols, rows)
    alternate = pyte.Screen(cols, rows)
    streams = (pyte.Stream(primary), pyte.Stream(alternate))
    return primary, alternate, streams


class PtyProcess:
    """Wraps a pty_backend.PtyHandle with a reader thread + pyte emulator.

    Interface parity with ManagedProcess so ProcessManager can drive
    both with the same lifecycle code (see manager.start).
    """

    is_pty = True

    def __init__(
        self,
        proc_id: int,
        handle,
        timeout_ms: int,
        cols: int = 80,
        rows: int = 24,
    ):
        self._id = proc_id
        self._handle = handle
        self._timeout_ms = timeout_ms
        self._cols = cols
        self._rows = rows
        self._exit_code: int | None = None
        self._killed = False
        self._status = "running"
        self._exited = asyncio.Event()
        self._stop = threading.Event()
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._consumer_task: asyncio.Task | None = None
        self._primary = None
        self._alternate = None
        self._streams = None
        self._active = "primary"

    @classmethod
    async def create(
        cls,
        db: Database,
        proc_id: int,
        command: str,
        args: list[str],
        cwd: str | None,
        env: dict | None,
        timeout_ms: int = 0,
        cols: int = 80,
        rows: int = 24,
    ) -> "PtyProcess":
        """Spawn via pty_backend and start the reader pipeline."""
        handle = pty_backend.spawn(
            command, args, cwd, env, rows=rows, cols=cols
        )
        mp = cls(proc_id, handle, timeout_ms, cols, rows)
        try:
            await mp.start(db)
        except Exception:
            # No half-started state: a failure inside start() (e.g. the
            # lazy pyte import in _make_screen) must not leak the
            # spawned child and its ConPTY handle. Best-effort teardown
            # — swallow secondary failures and re-raise the original.
            mp._stop.set()
            try:
                mp._handle.kill(_SIGKILL)
            except Exception:
                pass
            try:
                await asyncio.to_thread(mp._handle.close)
            except Exception:
                pass
            if mp._thread and mp._thread.is_alive():
                mp._thread.join(timeout=2.0)
            if mp._consumer_task:
                mp._consumer_task.cancel()
            raise
        return mp

    @property
    def id(self) -> int:
        return self._id

    @property
    def os_pid(self) -> int:
        return self._handle.pid

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def status(self) -> str:
        if self._killed:
            return "killed"
        return self._status

    async def start(self, db: Database) -> None:
        """Start the reader thread and the loop-side consumer task."""
        self._primary, self._alternate, self._streams = _make_screen(
            self._rows, self._cols
        )
        self._queue = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._reader_loop, daemon=True, name=f"pty-reader-{self._id}"
        )
        self._thread.start()
        self._consumer_task = asyncio.create_task(self._consume(db))

    async def start_background_readers(self, db: Database) -> None:
        """No-op — the PTY reader starts in create()/start()."""

    def _reader_loop(self) -> None:
        """Blocking read loop in a daemon thread; pushes to asyncio queue.

        Exit rules: EOFError, any exception, or !isalive() — '' is
        never treated as EOF (pywinpty returns '' on empty reads and
        forever after terminate()).
        """
        while not self._stop.is_set():
            try:
                data = self._handle.read()
            except EOFError:
                self._put(("eof", None))
                break
            except Exception:
                self._put(("eof", None))
                break
            if data:
                self._put(("chunk", data))
            if not self._handle.isalive():
                self._put(("eof", None))
                break

    def _put(self, item) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            pass  # loop closed during shutdown/teardown

    async def _consume(self, db: Database) -> None:
        """Loop-side: feed pyte, scan alt-screen, strip, insert records."""
        try:
            while True:
                kind, data = await self._queue.get()
                try:
                    if kind == "eof":
                        self._status = "exited"
                        self._exited.set()
                        return
                    self._feed_screen(data)
                    cleaned = strip_ansi(data).replace("\r", "")
                    if cleaned:
                        try:
                            await db.insert_record(
                                self._id, time.time_ns(), 1, cleaned
                            )
                        except Exception:
                            # DB closed during shutdown — drop the remaining
                            # records but keep draining so eof still sets
                            # _exited (wait_exit contract preserved).
                            pass
                except Exception:
                    # A consumer-side failure (feed/strip) must not strand
                    # the process in "running": mark exited and release
                    # wait_exit() so the exit monitor never hangs.
                    self._status = "exited"
                    self._exited.set()
                    return
        except asyncio.CancelledError:
            pass

    def _feed_screen(self, chunk: str) -> None:
        """Feed raw VT bytes to the active screen, handling alt-screen."""
        if self._streams is None:
            return
        offset = 0
        for match in _ALT_SCREEN_PATTERN.finditer(chunk):
            before = chunk[offset:match.start()]
            if before:
                self._streams[0 if self._active == "primary" else 1].feed(before)
            code, mode = match.group(1), match.group(2)
            if mode == "h":
                if code == "1049":
                    self._alternate.reset()
                self._active = "alternate"
            else:
                self._active = "primary"
            offset = match.end()
        rest = chunk[offset:]
        if rest:
            self._streams[0 if self._active == "primary" else 1].feed(rest)

    async def write_stdin(self, db: Database, content: str) -> None:
        if self.status != "running":
            raise RuntimeError(
                f"Process {self._id} is not running (status={self.status})"
            )
        # write() is a blocking C call — chunk large payloads so no
        # single call stalls the event loop for long (spec: cap writes).
        for i in range(0, len(content), 4096):
            self._handle.write(content[i : i + 4096])
        await db.insert_record(self._id, time.time_ns(), 0, content)

    async def send_signal(self, sig) -> None:
        """Resolve a signal name-or-int to a PTY-appropriate action.

        SIGTERM -> terminate(), SIGKILL -> kill(),
        CTRL_C_EVENT -> write '\x03' and '\r' as separate writes with a
        short pause (CR flushes ConPTY's line buffer; split delivery
        avoids a measured ~200ms input-state race).
        """
        if self.status != "running":
            raise RuntimeError(
                f"Process {self._id} is not running (status={self.status})"
            )
        if isinstance(sig, str):
            if sig == "SIGTERM":
                await self.terminate()
                return
            if sig == "SIGKILL":
                await self.kill()
                return
            if sig == "CTRL_C_EVENT":
                # Under ConPTY cooked mode a bare \x03 is line-buffered
                # and never becomes Ctrl+C; the CR commits the line and
                # triggers the interrupt. Send the two characters as
                # SEPARATE writes with a short pause: a single \x03\r
                # write races when it lands within ~200ms of the
                # previous line commit (measured ~5/8 vs 8/8 delivery
                # on Win11/pywinpty).
                self._handle.write("\x03")
                await asyncio.sleep(0.1)
                self._handle.write("\r")
                return
            resolved = getattr(signal, sig, None)
            if resolved is None:
                raise ValueError(f"Unknown signal: {sig}")
            sig = resolved
        if sig == signal.SIGTERM:
            await self.terminate()
        elif sig == _SIGKILL:  # Windows Python has no signal.SIGKILL
            await self.kill()
        else:
            raise ValueError(
                f"Unsupported signal for PTY process: {sig!r}"
            )

    async def kill(self) -> None:
        if self._killed:
            return
        self._killed = True
        self._stop.set()
        try:
            self._handle.kill(_SIGKILL)
        except Exception:
            pass
        # close() is a blocking C call that can stall for seconds on
        # some backends — offload it so the event loop never freezes.
        await asyncio.to_thread(self._handle.close)  # unblocks a stuck read()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._consumer_task:
            self._consumer_task.cancel()
        # Wake wait_exit() so the exit-monitor task never hangs on a
        # killed process (consumer cancellation alone would leave it
        # waiting on _exited forever).
        self._exited.set()

    async def terminate(self) -> None:
        self._stop.set()
        try:
            self._handle.terminate()
        except Exception:
            pass
        await asyncio.to_thread(self._handle.close)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        # Same backstop as kill(): if the reader thread exited at the
        # _stop check without pushing eof, wait_exit() would hang on
        # _exited forever (the exit monitor awaits it).
        self._exited.set()

    async def wait_exit(self) -> int | None:
        """Wait for the reader to signal EOF; may return None.

        ConPTY exposes no exit code — Windows PTY processes have
        exit_code=None (documented; agents must not rely on it).
        """
        await self._exited.wait()
        return self._handle.exitstatus

    async def resize(self, rows: int, cols: int) -> None:
        self._handle.setwinsize(rows, cols)
        self._rows, self._cols = rows, cols
        if self._primary is not None:
            self._primary.resize(lines=rows, columns=cols)
            self._alternate.resize(lines=rows, columns=cols)

    def screen(self) -> dict:
        """Snapshot of the active pyte screen."""
        active = (
            self._alternate if self._active == "alternate" else self._primary
        )
        if active is None:
            return {
                "buffer": self._active, "rows": self._rows,
                "cols": self._cols, "cursor_x": 0, "cursor_y": 0,
                "content": "",
            }
        return {
            "buffer": self._active,
            "rows": self._rows,
            "cols": self._cols,
            "cursor_x": active.cursor.x,
            "cursor_y": active.cursor.y,
            "content": "\n".join(active.display),
        }
