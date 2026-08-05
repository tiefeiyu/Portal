"""Tests for PTY support."""
import asyncio
import os
import signal
import sys
import time
import tempfile

import pytest

# Windows Python has no SIGKILL constant; os.kill(pid, 9) maps to
# TerminateProcess there, matching POSIX SIGKILL semantics.
if not hasattr(signal, "SIGKILL"):
    signal.SIGKILL = 9

from portal_mcp.database import Database
from portal_mcp.pty_backend import _decode, normalize_env
from portal_mcp.pty_process import PtyProcess


class TestNormalizeEnv:
    def test_merges_os_environ(self, monkeypatch):
        monkeypatch.setenv("PORTAL_TEST_SENTINEL", "keep")
        env = normalize_env(None)
        assert env["PORTAL_TEST_SENTINEL"] == "keep"

    def test_caller_env_wins(self, monkeypatch):
        monkeypatch.setenv("PORTAL_TEST_SENTINEL", "base")
        env = normalize_env({"PORTAL_TEST_SENTINEL": "caller"})
        assert env["PORTAL_TEST_SENTINEL"] == "caller"

    def test_injects_term(self, monkeypatch):
        monkeypatch.delenv("TERM", raising=False)
        env = normalize_env(None)
        assert env["TERM"] == "xterm-256color"

    def test_preserves_existing_term(self, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        env = normalize_env(None)
        assert env["TERM"] == "xterm"


class TestDecode:
    def test_str_passthrough(self):
        assert _decode("hello") == "hello"

    def test_bytes_decode(self):
        assert _decode(b"hello") == "hello"

    def test_bad_utf8_replaced(self):
        assert _decode(b"\xff\xfe") == "��"


class FakeHandle:
    """In-memory PtyHandle for unit tests."""

    def __init__(self, chunks=("hello chunk",), raise_on_read=False):
        self.pid = 12345
        self.exitstatus = None
        self._chunks = list(chunks)
        self._raise_on_read = raise_on_read
        self.written = []
        self.resized = []
        self.killed = []
        self.terminated = False
        self.closed = False
        self.alive = True

    def read(self):
        if self._raise_on_read:
            self._raise_on_read = False
            raise EOFError("boom")
        if self._chunks:
            return self._chunks.pop(0)
        self.alive = False
        raise EOFError("end")

    def write(self, s):
        self.written.append(s)

    def setwinsize(self, rows, cols):
        self.resized.append((rows, cols))

    def kill(self, sig):
        self.killed.append(sig)
        self.alive = False

    def terminate(self):
        self.terminated = True
        self.alive = False

    def isalive(self):
        return self.alive

    def close(self):
        self.closed = True
        self.alive = False


@pytest.fixture
async def pty_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.initialize()
    yield db
    await db.close()
    if os.path.exists(path):
        os.unlink(path)


async def _make_pid(db, command="fake", args=None):
    pid = await db.create_process(
        command=command, args=args or [], cwd=None, env=None,
        timeout_ms=0, started_at=time.time_ns(),
    )
    await db.create_proc_table(pid)
    return pid


async def wait_records(db, pid, needle, timeout=5.0):
    """Poll DB records until needle appears in joined content."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = await db.read_records(pid, [0, 1], 0)
        contents = "".join(r["content"] for r in records)
        if needle in contents:
            return contents
        await asyncio.sleep(0.05)
    raise AssertionError(f"needle {needle!r} not seen in records: {contents!r}")


async def wait_status(mp, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mp.status == status:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"status never became {status!r} (is {mp.status!r})")


async def wait_screen(mp, needle, timeout=5.0):
    """Poll mp.screen() until needle appears in the content."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = mp.screen()
        if needle in snap["content"]:
            return snap
        await asyncio.sleep(0.05)
    raise AssertionError(f"needle {needle!r} never appeared on screen")


class TestFakeHandlePipeline:
    async def test_reader_stores_records(self, pty_db):
        pid = await _make_pid(pty_db, command="fake")
        mp = PtyProcess(pid, FakeHandle(chunks=("alpha\n", "beta\n")), 0)
        await mp.start(pty_db)
        await wait_records(pty_db, pid, "alpha")
        contents = await wait_records(pty_db, pid, "beta")
        assert "alpha" in contents and "beta" in contents

    async def test_empty_read_is_not_eof(self, pty_db):
        """'' must not terminate the reader; EOF needs EOFError/isalive."""

        class EmptyHandle(FakeHandle):
            def read(self):
                return ""  # NOT EOF per spec

        pid = await _make_pid(pty_db, command="fake")
        mp = PtyProcess(pid, EmptyHandle(chunks=()), 0)
        await mp.start(pty_db)
        await asyncio.sleep(0.3)
        assert mp.status == "running"
        mp._handle.alive = False
        mp._handle.close()  # unblock pattern
        await asyncio.wait_for(mp.wait_exit(), timeout=3)
        assert mp.status == "exited"

    async def test_reader_death_marks_exited(self, pty_db):
        pid = await _make_pid(pty_db, command="fake")
        mp = PtyProcess(pid, FakeHandle(raise_on_read=True), 0)
        await mp.start(pty_db)
        await wait_status(mp, "exited")

    async def test_write_stdin_records(self, pty_db):
        pid = await _make_pid(pty_db, command="fake")
        handle = FakeHandle()
        mp = PtyProcess(pid, handle, 0)
        await mp.start(pty_db)
        await mp.write_stdin(pty_db, "hello stdin")
        assert handle.written == ["hello stdin"]
        records = await pty_db.read_records(pid, [0], 0)
        assert len(records) == 1
        assert records[0]["content"] == "hello stdin"

    async def test_kill_closes_handle(self, pty_db):
        pid = await _make_pid(pty_db, command="fake")
        handle = FakeHandle()
        mp = PtyProcess(pid, handle, 0)
        await mp.start(pty_db)
        await mp.kill()
        assert handle.killed == [signal.SIGKILL]
        assert handle.closed is True
        assert mp.status == "killed"

    async def test_terminate(self, pty_db):
        pid = await _make_pid(pty_db, command="fake")
        handle = FakeHandle()
        mp = PtyProcess(pid, handle, 0)
        await mp.start(pty_db)
        await mp.terminate()
        assert handle.terminated is True
        # wait_exit must never hang after terminate (exit-monitor path)
        await asyncio.wait_for(mp.wait_exit(), timeout=3)

    async def test_send_signal_mapping(self, pty_db):
        pid = await _make_pid(pty_db, command="fake")
        handle = FakeHandle()
        mp = PtyProcess(pid, handle, 0)
        await mp.start(pty_db)
        await mp.send_signal("SIGTERM")
        assert handle.terminated is True
        handle.alive = True  # reuse for next signal
        await mp.send_signal("CTRL_C_EVENT")
        assert handle.written == ["\x03\r"]  # CR flushes ConPTY line buffer
        handle.alive = True
        await mp.send_signal("SIGKILL")  # must come last — kill sets status
        assert handle.killed == [signal.SIGKILL]


class TestRealConPTY:
    """Real ConPTY tests — each skipped when winpty/pyte are missing.

    The importorskip lives in an autouse fixture (NOT at module level)
    so the fake-handle unit tests above still run without winpty.
    """

    @pytest.fixture(autouse=True)
    def _require_pty_deps(self):
        pytest.importorskip("winpty")
        pytest.importorskip("pyte")

    async def test_isatty_pty_process(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable,
            ["-c",
             "import sys; "
             "sys.exit(0 if sys.stdout.isatty() and sys.stdin.isatty() else 1)"],
            None, None,
        )
        await asyncio.wait_for(mp.wait_exit(), timeout=10)
        assert mp.status == "exited"
        assert mp._handle.exitstatus is None  # Windows ConPTY contract

    async def test_prompt_without_newline(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable, ["-c", "input('Password: ')"],
            None, None,
        )
        await wait_records(pty_db, pid, "Password:")
        await mp.write_stdin(pty_db, "secret123\r")
        await wait_records(pty_db, pid, "secret123")  # cooked-mode echo
        await mp.kill()

    async def test_echo_and_no_carriage_return(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable,
            ["-c", "import sys; sys.stdout.write('line one\\n'); "
             "sys.stdout.flush(); import time; time.sleep(30)"],
            None, None,
        )
        await wait_records(pty_db, pid, "line one")
        records = await pty_db.read_records(pid, [0, 1], 0)
        assert "\r" not in "".join(r["content"] for r in records)
        await mp.kill()

    async def test_merged_stderr(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable,
            ["-c",
             "import sys; "
             "sys.stderr.write('err to console\\n'); "
             "sys.stdout.write('out to console\\n'); "
             "sys.stdout.flush(); import time; time.sleep(30)"],
            None, None,
        )
        await wait_records(pty_db, pid, "err to console")
        contents = await wait_records(pty_db, pid, "out to console")
        assert "err to console" in contents
        stderr_records = await pty_db.read_records(pid, [2], 0)
        assert stderr_records == []
        await mp.kill()

    async def test_term_injected(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable,
            ["-c",
             "import os, sys; "
             "sys.stdout.write(os.environ.get('TERM', 'MISSING')); "
             "sys.stdout.flush(); import time; time.sleep(30)"],
            None, None,
        )
        await wait_records(pty_db, pid, "xterm-256color")
        await mp.kill()

    async def test_ctrl_c_interrupts(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable, ["-c", "input('> ')"], None, None,
        )
        # ConPTY renders the prompt's trailing space as a cursor move,
        # so only the ">" itself lands in the records.
        await wait_records(pty_db, pid, ">")
        # Under ConPTY a bare \x03 is line-buffered and never becomes a
        # Ctrl+C; a following CR commits the line and triggers it.
        await mp.write_stdin(pty_db, "\x03\r")
        try:
            await asyncio.wait_for(mp.wait_exit(), timeout=10)
        except asyncio.TimeoutError:
            await mp.kill()
            records = await pty_db.read_records(pid, [0, 1], 0)
            raise AssertionError(
                f"ctrl-c did not exit: {''.join(r['content'] for r in records)!r}"
            )
        records = await pty_db.read_records(pid, [0, 1], 0)
        joined = "".join(r["content"] for r in records)
        assert mp.status == "exited" or "KeyboardInterrupt" in joined
        assert mp._handle.exitstatus in (None, 0, 1, -1)

    async def test_screen_snapshot_grid(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable,
            ["-c",
             "import sys, time; "
             "print('XX'); "
             "sys.stdout.write('YY'); "
             "sys.stdout.flush(); time.sleep(30)"],
            None, None, cols=80, rows=24,
        )
        snap = await wait_screen(mp, "XX")
        assert snap["buffer"] == "primary"
        lines = snap["content"].split("\n")
        assert lines[0][:2] == "XX"
        assert lines[1][:2] == "YY"
        await mp.kill()

    async def test_alt_screen_swap(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable,
            ["-c",
             "import sys, time; "
             "sys.stdout.write('before'); "
             "sys.stdout.write('\\x1b[?1049h'); "
             "sys.stdout.write('\\x1b[2J\\x1b[H'); "
             "sys.stdout.write('TUI FRAME'); "
             "sys.stdout.flush(); time.sleep(30)"],
            None, None,
        )
        snap = await wait_screen(mp, "TUI FRAME")
        assert snap["buffer"] == "alternate"
        assert "TUI FRAME" in snap["content"]
        await mp.kill()

    async def test_screen_persists_after_exit(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable,
            ["-c", "print('last words')"], None, None,
        )
        await asyncio.wait_for(mp.wait_exit(), timeout=10)
        snap = mp.screen()
        assert "last words" in snap["content"]

    async def test_resize_asymmetric(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable,
            ["-c", "import time; time.sleep(30)"],
            None, None, cols=100, rows=30,
        )
        await asyncio.sleep(0.5)
        snap = mp.screen()
        assert snap["cols"] == 100
        assert snap["rows"] == 30
        await mp.resize(40, 120)
        snap = mp.screen()
        assert snap["cols"] == 120
        assert snap["rows"] == 40
        await mp.kill()

    async def test_large_write_no_deadlock(self, pty_db):
        pid = await _make_pid(pty_db, command=sys.executable)
        mp = await PtyProcess.create(
            pty_db, pid, sys.executable,
            ["-c", "import sys; data = sys.stdin.readline(); "
             "sys.stdout.write('got %d' % len(data.rstrip('\\r\\n'))); "
             "sys.stdout.flush(); import time; time.sleep(30)"],
            None, None,
        )
        await asyncio.sleep(0.5)
        # readline needs a line terminator — the 4096-char payload plus
        # CR is split into a 4096-char write plus a 1-char write by the
        # chunker, still exercising the 4096-char write cap.
        payload = "x" * 4096 + "\r"
        await asyncio.wait_for(
            mp.write_stdin(pty_db, payload), timeout=5
        )
        await wait_records(pty_db, pid, "got 4096")
        await mp.kill()
