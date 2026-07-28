"""Tests for ProcessManager."""
import asyncio
import os
import signal
import sys
import tempfile
import pytest
from portal_mcp.database import Database
from portal_mcp.manager import ProcessManager


@pytest.fixture
async def manager():
    """Create a ProcessManager with a fresh temp database."""
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


class TestStartProcess:
    async def test_start_simple_command(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('hello')"],
        )
        assert result["id"] > 0
        assert result["os_pid"] > 0
        assert result["status"] == "running"

    async def test_start_with_options(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import os; print(os.getenv('FOO'))"],
            env={"FOO": "bar_value"},
            timeout_ms=60000,
        )
        assert result["status"] == "running"
        await asyncio.sleep(0.3)
        records = await manager.read(result["id"], "stdout", 3000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "bar_value" in contents

    async def test_start_invalid_command(self, manager):
        with pytest.raises(Exception):
            await manager.start(
                command="/nonexistent/path/to/binary_xyz_123",
                args=[],
            )


class TestRead:
    async def test_read_stdout(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('hello stdout')"],
        )
        await asyncio.sleep(0.3)
        records = await manager.read(result["id"], "stdout", 3000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "hello stdout" in contents

    async def test_read_stderr(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c",
                  "import sys; sys.stderr.write('hello stderr\\n')"],
        )
        await asyncio.sleep(0.3)
        records = await manager.read(result["id"], "stderr", 3000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "hello stderr" in contents

    async def test_read_both(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c",
                  "import sys; "
                  "sys.stdout.write('out\\n'); "
                  "sys.stderr.write('err\\n')"],
        )
        await asyncio.sleep(0.3)
        records = await manager.read(result["id"], "both", 3000, "ms")
        sources = {r["source"] for r in records}
        assert 1 in sources
        assert 2 in sources

    async def test_read_nonexistent_process(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.read(99999, "both", 1000, "ms")

    async def test_read_resets_idle_timer(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(2)"],
            timeout_ms=5000,
        )
        await asyncio.sleep(0.1)
        await manager.read(result["id"], "both", 1000, "ms")
        proc_info = await manager.inspect(result["id"])
        assert proc_info["status"] == "running"


class TestWrite:
    async def test_write_stdin(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c",
                  "import sys; "
                  "data = sys.stdin.readline(); "
                  "print(data, end='')"],
        )
        await manager.write(result["id"], "test input\n")
        await asyncio.sleep(0.3)
        records = await manager.read(result["id"], "stdout", 3000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "test input" in contents

    async def test_write_to_exited_process(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "pass"],
        )
        await asyncio.sleep(0.5)
        with pytest.raises(ValueError, match="not running"):
            await manager.write(result["id"], "data")


class TestSignal:
    async def test_terminate_process(self, manager):
        if sys.platform == "win32":
            pytest.skip("SIGTERM is POSIX-only")
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
        )
        await manager.send_signal(result["id"], signal.SIGTERM)
        await asyncio.sleep(0.5)
        proc_info = await manager.inspect(result["id"])
        assert proc_info["status"] in ("exited", "killed")

    async def test_signal_to_nonexistent(self, manager):
        sig = signal.SIGTERM if hasattr(signal, "SIGTERM") else 15
        with pytest.raises(ValueError, match="not found"):
            await manager.send_signal(99999, sig)


class TestListAll:
    async def test_empty_list(self, manager):
        result = await manager.list_all()
        assert result == []

    async def test_multiple_processes(self, manager):
        await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(2)"],
        )
        await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(2)"],
        )
        processes = await manager.list_all()
        assert len(processes) == 2
        for p in processes:
            assert "id" in p
            assert "os_pid" in p
            assert "status" in p
            assert "timeout_ms" in p
            assert "inactive_duration_ms" in p
            assert "io_count" in p


class TestInspect:
    async def test_inspect_running(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(2)"],
            timeout_ms=10000,
        )
        info = await manager.inspect(result["id"])
        assert info["id"] == result["id"]
        assert info["status"] == "running"
        assert info["timeout_ms"] == 10000
        assert "io_count" in info
        assert "stdout_count" in info
        assert "stderr_count" in info
        assert "stdin_count" in info

    async def test_inspect_nonexistent(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.inspect(99999)


class TestKill:
    async def test_kill_running(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
        )
        await manager.do_kill(result["id"])
        info = await manager.inspect(result["id"])
        assert info["status"] in ("exited", "killed")

    async def test_kill_nonexistent(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.do_kill(99999)


class TestKillAll:
    async def test_kills_all_running(self, manager):
        r1 = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
        )
        r2 = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
        )
        await manager.kill_all()
        for rid in [r1["id"], r2["id"]]:
            info = await manager.inspect(rid)
            assert info["status"] in ("exited", "killed")


class TestClear:
    async def test_clear_records(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('data')"],
        )
        await asyncio.sleep(0.5)
        counts = (await manager.inspect(result["id"]))["io_count"]
        assert counts > 0
        await manager.clear(result["id"])
        counts = (await manager.inspect(result["id"]))["io_count"]
        assert counts == 0


class TestCleanup:
    async def test_cleanup_exited_process(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "pass"],
        )
        await asyncio.sleep(0.3)
        await manager.do_cleanup(result["id"])
        with pytest.raises(ValueError, match="not found"):
            await manager.inspect(result["id"])

    async def test_cleanup_running_rejected(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(10)"],
        )
        with pytest.raises(ValueError, match="running"):
            await manager.do_cleanup(result["id"])

    async def test_cleanup_nonexistent(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.do_cleanup(99999)


class TestTimeoutMonitor:
    async def test_timeout_kills_idle_process(self, manager):
        await manager.start_monitor()
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            timeout_ms=300,
        )
        await asyncio.sleep(2)
        with pytest.raises(ValueError, match="not found"):
            await manager.inspect(result["id"])

    async def test_activity_prevents_timeout(self, manager):
        await manager.start_monitor()
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            timeout_ms=2000,
        )
        # Keep touching the process to prevent timeout
        for _ in range(5):
            await asyncio.sleep(0.2)
            await manager.read(result["id"], "both", 1000, "ms")
        info = await manager.inspect(result["id"])
        assert info["status"] == "running"
