"""Tests for PTY support."""
import asyncio
import os
import signal
import sys
import time
import tempfile

import pytest

from portal_mcp.database import Database
from portal_mcp.pty_backend import _decode, normalize_env
from portal_mcp.pty_process import PtyProcess, _SIGKILL


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
        assert handle.killed == [_SIGKILL]
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
        # split writes: \x03 then \r (CR flushes ConPTY's line buffer;
        # split delivery avoids a measured ~200ms input-state race)
        assert handle.written == ["\x03", "\r"]
        handle.alive = True
        await mp.send_signal("SIGKILL")  # must come last — kill sets status
        assert handle.killed == [_SIGKILL]

    async def test_send_signal_unsupported_int_raises_value_error(self, pty_db):
        """Numeric signals other than SIGTERM/SIGKILL raise ValueError.

        Regression: Windows Python defines no signal.SIGKILL constant.
        The int path used to compare against signal.SIGKILL directly,
        exploding with AttributeError (surfaced by the server as
        "Unexpected error") instead of the documented ValueError. SIGINT
        (2) works on both platforms via getattr.
        """
        pid = await _make_pid(pty_db, command="fake")
        handle = FakeHandle()
        mp = PtyProcess(pid, handle, 0)
        await mp.start(pty_db)
        with pytest.raises(ValueError, match="Unsupported signal"):
            await mp.send_signal(getattr(signal, "SIGINT", 2))
        assert handle.terminated is False
        assert handle.killed == []

    async def test_send_signal_int_sigkill_kills(self, pty_db):
        """The int SIGKILL path routes to kill(), not AttributeError."""
        pid = await _make_pid(pty_db, command="fake")
        handle = FakeHandle()
        mp = PtyProcess(pid, handle, 0)
        await mp.start(pty_db)
        await mp.send_signal(_SIGKILL)
        assert handle.killed == [_SIGKILL]
        assert mp.status == "killed"

    async def test_create_failure_tears_down_handle(self, pty_db, monkeypatch):
        """start() failing after spawn must not leak the child/handle."""
        from portal_mcp import pty_process

        handle = FakeHandle()
        monkeypatch.setattr(
            pty_process.pty_backend, "spawn",
            lambda command, args, cwd, env, **kwargs: handle,
        )

        async def fail_start(self, db):
            raise ImportError("pyte unavailable")

        monkeypatch.setattr(PtyProcess, "start", fail_start)
        pid = await _make_pid(pty_db, command="fake")
        with pytest.raises(ImportError):
            await PtyProcess.create(pty_db, pid, "fake", [], None, None)
        assert handle.killed == [_SIGKILL]
        assert handle.closed is True

    async def test_consumer_failure_marks_exited(self, pty_db, monkeypatch):
        """A feed/strip failure must exit the process, not strand it."""
        pid = await _make_pid(pty_db, command="fake")
        mp = PtyProcess(pid, FakeHandle(chunks=("boom\n",)), 0)

        def boom(chunk):
            raise RuntimeError("feed failed")

        monkeypatch.setattr(mp, "_feed_screen", boom)
        await mp.start(pty_db)
        await wait_status(mp, "exited")
        await asyncio.wait_for(mp.wait_exit(), timeout=3)


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
        # Split across two writes with a pause: a single \x03\r write
        # races ~5/8 vs 8/8 split delivery (measured, Win11/pywinpty).
        await mp.write_stdin(pty_db, "\x03")
        await asyncio.sleep(0.1)
        await mp.write_stdin(pty_db, "\r")
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


from portal_mcp.manager import ProcessManager


@pytest.fixture
async def pty_manager():
    pytest.importorskip("winpty")
    pytest.importorskip("pyte")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.initialize()
    mgr = ProcessManager(db)
    yield mgr
    await mgr.shutdown()
    await db.close()
    if os.path.exists(path):
        os.unlink(path)


async def wait_mgr_records(mgr, pid, needle, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = await mgr.read(pid, "both", 2000, "ms")
        contents = "".join(r["content"] for r in records)
        if needle in contents:
            return contents
        await asyncio.sleep(0.05)
    raise AssertionError(f"needle {needle!r} not seen: {contents!r}")


class TestManagerPty:
    async def test_start_pty_flag(self, pty_manager):
        result = await pty_manager.start(
            command=sys.executable,
            args=["-c",
                  "import sys; "
                  "sys.exit(0 if sys.stdout.isatty() else 1)"],
            pty=True,
        )
        assert result["status"] == "running"
        assert result["os_pid"] > 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = await pty_manager.inspect(result["id"])
            if info["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert info["status"] == "exited"

    async def test_pipe_mode_isatty_false(self, pty_manager):
        result = await pty_manager.start(
            command=sys.executable,
            args=["-c",
                  "import sys; "
                  "sys.exit(0 if sys.stdout.isatty() else 1)"],
        )
        assert result["status"] == "running"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = await pty_manager.inspect(result["id"])
            if info["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert info["status"] == "exited"
        assert info["exit_code"] == 1

    async def test_pty_exit_code_is_none(self, pty_manager):
        result = await pty_manager.start(
            command=sys.executable,
            args=["-c", "pass"],
            pty=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = await pty_manager.inspect(result["id"])
            if info["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert info["exit_code"] is None

    async def test_spawn_failure_cleans_up(self, pty_manager):
        with pytest.raises(Exception):
            await pty_manager.start(
                command="C:\\definitely\\missing\\binary_xyz_123.exe",
                pty=True,
            )
        assert await pty_manager.list_all() == []

    async def test_screen_pipe_mode_errors(self, pty_manager):
        result = await pty_manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
        )
        with pytest.raises(ValueError, match="not a PTY process"):
            await pty_manager.screen(result["id"])

    async def test_screen_unknown_id(self, pty_manager):
        with pytest.raises(ValueError, match="not found"):
            await pty_manager.screen(99999)

    async def test_screen_via_manager(self, pty_manager):
        result = await pty_manager.start(
            command=sys.executable,
            args=["-c",
                  "import sys, time; "
                  "sys.stdout.write('\\x1b[H'); "
                  "sys.stdout.write('SCREEN HERE'); "
                  "sys.stdout.flush(); time.sleep(30)"],
            pty=True,
        )
        deadline = time.monotonic() + 5
        snap = None
        while time.monotonic() < deadline:
            snap = await pty_manager.screen(result["id"])
            if "SCREEN HERE" in snap["content"]:
                break
            await asyncio.sleep(0.05)
        assert "SCREEN HERE" in snap["content"]
        assert snap["buffer"] == "primary"

    async def test_screen_resize_and_touch(self, pty_manager):
        result = await pty_manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            pty=True,
            timeout_ms=5000,
        )
        snap = await pty_manager.screen(result["id"], cols=60, rows=20)
        assert snap["cols"] == 60 and snap["rows"] == 20
        proc = await pty_manager.inspect(result["id"])
        assert proc["status"] == "running"  # screen read touched idle timer
        await pty_manager.do_kill(result["id"])

    async def test_timeout_monitor_kills_pty(self, pty_manager):
        await pty_manager.start_monitor()
        result = await pty_manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            pty=True,
            timeout_ms=300,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                await pty_manager.inspect(result["id"])
            except ValueError:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("PTY process not cleaned up by monitor")

    async def test_send_signal_pty(self, pty_manager):
        result = await pty_manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            pty=True,
        )
        await pty_manager.send_signal(result["id"], "SIGTERM")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = await pty_manager.inspect(result["id"])
            if info["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert info["status"] in ("exited", "killed")

    async def test_write_pty_echo_roundtrip(self, pty_manager):
        result = await pty_manager.start(
            command=sys.executable,
            args=["-c", "input('prompt> ')"],
            pty=True,
        )
        await wait_mgr_records(pty_manager, result["id"], "prompt>")
        await pty_manager.write(result["id"], "hello pty\r")
        await wait_mgr_records(pty_manager, result["id"], "hello pty")
        await pty_manager.do_kill(result["id"])
        assert await pty_manager.list_all() != []  # killed, retained
        await pty_manager.do_cleanup(result["id"])
