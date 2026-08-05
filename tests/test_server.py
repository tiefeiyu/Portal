"""Integration tests for the MCP server tools."""
import asyncio
import os
import sys
import tempfile
import pytest
from portal_mcp.server import create_server


class TestServerTools:
    """Test MCP tools by calling manager methods directly."""

    @pytest.fixture
    async def portal(self):
        """Create a full Portal server for testing."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        if os.path.exists(db_path):
            os.unlink(db_path)

        manager, db = await create_server(db_path)
        yield manager
        await manager.shutdown()
        await db.close()
        if os.path.exists(db_path):
            os.unlink(db_path)

    async def test_process_start_and_read(self, portal):
        result = await portal.start(
            command=sys.executable,
            args=["-c", "print('hello from test')"],
        )
        assert result["status"] == "running"
        await asyncio.sleep(0.3)
        records = await portal.read(result["id"], "both", 3000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "hello from test" in contents

    async def test_process_write_and_echo(self, portal):
        result = await portal.start(
            command=sys.executable,
            args=[
                "-c",
                "import sys; "
                "line = sys.stdin.readline(); "
                "print(f'echo: {line}', end='')",
            ],
        )
        await portal.write(result["id"], "test message\n")
        await asyncio.sleep(0.3)
        records = await portal.read(result["id"], "both", 3000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "echo: test message" in contents

    async def test_process_list(self, portal):
        assert await portal.list_all() == []
        await portal.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(2)"],
        )
        await portal.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(2)"],
        )
        processes = await portal.list_all()
        assert len(processes) == 2

    async def test_process_kill(self, portal):
        result = await portal.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
        )
        kill_result = await portal.do_kill(result["id"])
        assert kill_result["status"] == "killed"
        info = await portal.inspect(result["id"])
        assert info["status"] in ("exited", "killed")

    async def test_process_cleanup(self, portal):
        result = await portal.start(
            command=sys.executable, args=["-c", "pass"]
        )
        await asyncio.sleep(0.3)
        await portal.do_cleanup(result["id"])
        with pytest.raises(ValueError):
            await portal.inspect(result["id"])

    async def test_clear_records(self, portal):
        result = await portal.start(
            command=sys.executable, args=["-c", "print('data')"]
        )
        await asyncio.sleep(0.5)
        await portal.clear(result["id"])
        info = await portal.inspect(result["id"])
        assert info["io_count"] == 0

    async def test_inspect(self, portal):
        result = await portal.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(2)"],
            timeout_ms=10000,
        )
        info = await portal.inspect(result["id"])
        assert info["command"] == sys.executable
        assert info["timeout_ms"] == 10000

    async def test_ans_is_stripped(self, portal):
        result = await portal.start(
            command=sys.executable,
            args=["-c", "print('\\x1b[31mred text\\x1b[0m')"],
        )
        await asyncio.sleep(0.3)
        records = await portal.read(result["id"], "stdout", 3000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "red text" in contents
        assert "\x1b" not in contents

    async def test_registry_wired_through_create_server(self, portal):
        result = await portal.record_program("gdb", True)
        assert result["known"] is True
        assert result["needs_pty"] is True
        assert (await portal.query_program("GDB.EXE"))["needs_pty"] is True
