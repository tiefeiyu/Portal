"""Tests for ManagedProcess."""
import asyncio
import os
import signal
import sys
import time
import tempfile
import pytest
from portal_mcp.database import Database
from portal_mcp.process import ManagedProcess


@pytest.fixture
async def db_for_proc():
    """Database fixture scoped to process tests."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.initialize()
    yield db
    processes = await db.get_all_processes()
    for p in processes:
        try:
            await db.cleanup_process(p["id"])
        except Exception:
            pass
    await db.close()
    if os.path.exists(path):
        os.unlink(path)


async def _create_test_process(
    db, python_code: str, timeout_ms: int = 0
) -> ManagedProcess:
    """Helper: create a ManagedProcess running a Python snippet."""
    proc_id = await db.create_process(
        command=sys.executable,
        args=["-c", python_code],
        cwd=None,
        env=None,
        timeout_ms=timeout_ms,
        started_at=time.time_ns(),
    )
    await db.create_proc_table(proc_id)
    subproc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        python_code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
    )
    mp = ManagedProcess(
        proc_id=proc_id,
        process=subproc,
        timeout_ms=timeout_ms,
    )
    await db.update_os_pid(proc_id, subproc.pid)
    return mp


class TestManagedProcessBasic:
    async def test_properties(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc, "import time; time.sleep(0.1)"
        )
        assert mp.id > 0
        assert mp.os_pid > 0
        assert mp.status == "running"

    async def test_wait_exit(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc, "import sys; sys.exit(42)"
        )
        exit_code = await mp.wait_exit()
        assert exit_code == 42

    async def test_kill(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc, "import time; time.sleep(60)"
        )
        assert mp.status == "running"
        await mp.kill()
        try:
            await asyncio.wait_for(mp.wait_exit(), timeout=3)
        except asyncio.TimeoutError:
            pass
        assert mp.status in ("exited", "killed")


class TestManagedProcessIO:
    async def test_reads_stdout(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; sys.stdout.write('hello\\n'); "
            "sys.stdout.write('world\\n')",
        )
        await mp.start_background_readers(db_for_proc)
        await mp.wait_exit()

        records = await db_for_proc.read_records(mp.id, [1], 0)
        contents = "".join(r["content"] for r in records)
        assert "hello" in contents
        assert "world" in contents

    async def test_reads_stderr(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; sys.stderr.write('error msg\\n')",
        )
        await mp.start_background_readers(db_for_proc)
        await mp.wait_exit()

        records = await db_for_proc.read_records(mp.id, [2], 0)
        contents = "".join(r["content"] for r in records)
        assert "error msg" in contents

    async def test_strips_ansi_from_output(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; sys.stdout.write('\\x1b[31mred\\x1b[0m\\n')",
        )
        await mp.start_background_readers(db_for_proc)
        await mp.wait_exit()

        records = await db_for_proc.read_records(mp.id, [1], 0)
        contents = "".join(r["content"] for r in records)
        assert "red" in contents
        assert "\x1b" not in contents

    async def test_write_stdin(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; "
            "data = sys.stdin.readline(); "
            "sys.stdout.write(data); "
            "sys.stdout.flush()",
        )
        await mp.start_background_readers(db_for_proc)
        await mp.write_stdin(db_for_proc, "input data\n")
        await mp.wait_exit()

        records = await db_for_proc.read_records(mp.id, [1], 0)
        contents = "".join(r["content"] for r in records)
        assert "input data" in contents

    async def test_write_stdin_records_in_db(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; sys.stdin.readline()",
        )
        await mp.write_stdin(db_for_proc, "hello stdin")
        stdin_records = await db_for_proc.read_records(mp.id, [0], 0)
        assert len(stdin_records) == 1
        assert stdin_records[0]["content"] == "hello stdin"
        assert stdin_records[0]["source"] == 0


class TestSignalHandling:
    async def test_send_signal(self, db_for_proc):
        if sys.platform == "win32":
            pytest.skip("Signal tests only on POSIX")
        mp = await _create_test_process(
            db_for_proc,
            "import signal, sys; "
            "def handler(s, f): sys.exit(0); "
            "signal.signal(signal.SIGUSR1, handler); "
            "import time; time.sleep(30)",
        )
        try:
            await mp.send_signal(signal.SIGUSR1)
            await asyncio.wait_for(mp.wait_exit(), timeout=3)
        except asyncio.TimeoutError:
            await mp.kill()
            raise
        assert mp.status == "exited"

    async def test_send_signal_to_exited_process_raises(self, db_for_proc):
        mp = await _create_test_process(db_for_proc, "pass")
        await mp.wait_exit()
        sig = signal.SIGTERM if hasattr(signal, "SIGTERM") else 15
        with pytest.raises(RuntimeError, match="not running"):
            await mp.send_signal(sig)
