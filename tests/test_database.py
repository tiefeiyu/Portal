"""Tests for database layer."""
import os
import tempfile
import pytest
from portal_mcp.database import Database, PortalError


@pytest.fixture
async def db():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    await database.initialize()
    yield database
    await database.close()
    os.unlink(path)


class TestDatabaseInitialize:
    async def test_creates_processes_table(self, db):
        cursor = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='processes'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "processes"

    async def test_initialize_clears_old_data(self, db):
        pid = await db.create_process("cmd", [], None, None, 5000, 1000000)
        await db.create_proc_table(pid)
        await db.initialize()
        processes = await db.get_all_processes()
        assert len(processes) == 0


class TestCreateProcess:
    async def test_returns_auto_increment_id(self, db):
        pid1 = await db.create_process(
            "echo", ["hello"], None, None, 5000, 1000000000
        )
        pid2 = await db.create_process(
            "echo", ["world"], None, None, 0, 2000000000
        )
        assert pid1 == 1
        assert pid2 == 2

    async def test_stores_all_fields(self, db):
        pid = await db.create_process(
            command="python",
            args=["-c", "print('hi')"],
            cwd="/tmp",
            env={"FOO": "bar"},
            timeout_ms=10000,
            started_at=1234567890000000000,
        )
        proc = await db.get_process(pid)
        assert proc is not None
        assert proc["command"] == "python"
        assert "\"print('hi')\"" in proc["args"]
        assert proc["cwd"] == "/tmp"
        assert "FOO" in proc["env"]
        assert proc["timeout_ms"] == 10000
        assert proc["status"] == "running"
        assert proc["started_at"] == 1234567890000000000
        assert proc["last_active_at"] == 1234567890000000000
        assert proc["exit_code"] is None


class TestProcTable:
    async def test_create_and_drop(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        cursor = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (f"proc_{pid}",),
        )
        row = await cursor.fetchone()
        assert row is not None
        await db.cleanup_process(pid)
        cursor = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (f"proc_{pid}",),
        )
        row = await cursor.fetchone()
        assert row is None


class TestInsertAndRead:
    async def test_insert_and_read_by_source(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        await db.insert_record(pid, 1000, 1, "stdout line 1")
        await db.insert_record(pid, 2000, 2, "stderr line 1")
        await db.insert_record(pid, 3000, 1, "stdout line 2")

        records = await db.read_records(pid, [1], 0)
        assert len(records) == 2
        assert all(r["source"] == 1 for r in records)
        assert records[0]["content"] == "stdout line 1"
        assert records[1]["content"] == "stdout line 2"

        records = await db.read_records(pid, [1, 2], 0)
        assert len(records) == 3

        records = await db.read_records(pid, [1, 2], 1500)
        assert len(records) == 2


class TestUpdateStatus:
    async def test_update_to_exited(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.update_status(pid, "exited", 0)
        proc = await db.get_process(pid)
        assert proc["status"] == "exited"
        assert proc["exit_code"] == 0

    async def test_update_to_killed(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.update_status(pid, "killed", -9)
        proc = await db.get_process(pid)
        assert proc["status"] == "killed"
        assert proc["exit_code"] == -9


class TestTouch:
    async def test_updates_last_active_at(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 1000000)
        proc = await db.get_process(pid)
        assert proc["last_active_at"] == 1000000
        await db.touch(pid)
        proc = await db.get_process(pid)
        assert proc["last_active_at"] > 1000000


class TestGetProcess:
    async def test_returns_none_for_missing(self, db):
        proc = await db.get_process(999)
        assert proc is None


class TestGetAllProcesses:
    async def test_returns_empty_list_initially(self, db):
        processes = await db.get_all_processes()
        assert processes == []

    async def test_returns_all_processes(self, db):
        await db.create_process("cmd1", [], None, None, 0, 0)
        await db.create_process("cmd2", [], None, None, 0, 0)
        processes = await db.get_all_processes()
        assert len(processes) == 2
        assert processes[0]["id"] == 1
        assert processes[1]["id"] == 2


class TestIoCount:
    async def test_counts_by_source(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        await db.insert_record(pid, 1000, 1, "a")
        await db.insert_record(pid, 2000, 1, "b")
        await db.insert_record(pid, 3000, 2, "c")
        await db.insert_record(pid, 4000, 0, "d")

        counts = await db.io_count(pid)
        assert counts == {"total": 4, "stdout": 2, "stderr": 1, "stdin": 1}


class TestClearRecords:
    async def test_clears_all_records(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        await db.insert_record(pid, 1000, 1, "a")
        await db.insert_record(pid, 2000, 2, "b")
        await db.clear_records(pid)
        counts = await db.io_count(pid)
        assert counts["total"] == 0


class TestCleanupProcess:
    async def test_removes_process_and_table(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        await db.cleanup_process(pid)
        assert await db.get_process(pid) is None

    async def test_cleanup_nonexistent_is_noop(self, db):
        await db.cleanup_process(999)
