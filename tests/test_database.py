"""Tests for database layer."""
import json
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

    async def test_initialize_adds_cursor_column_to_stale_db(self, tmp_path):
        """A stale DB whose processes table lacks read_cursors/read_gen
        must not crash initialize() (safety net for direct Database() use)."""
        import sqlite3

        path = str(tmp_path / "stale.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE processes ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "os_pid INTEGER, status TEXT NOT NULL DEFAULT 'running',"
            "started_at INTEGER NOT NULL,"
            "timeout_ms INTEGER NOT NULL DEFAULT 0,"
            "last_active_at INTEGER NOT NULL, command TEXT NOT NULL,"
            "args TEXT DEFAULT '[]', cwd TEXT, env TEXT DEFAULT '{}',"
            "exit_code INTEGER)"
        )
        conn.close()

        database = Database(path)
        await database.initialize()
        cursor = await database._conn.execute(
            "PRAGMA table_info(processes)"
        )
        names = [row[1] for row in await cursor.fetchall()]
        assert "read_cursors" in names
        assert "read_gen" in names
        await database.close()

    async def test_initialize_clears_old_data(self, db):
        pid = await db.create_process("cmd", [], None, None, 5000, 1000000)
        await db.create_proc_table(pid)
        await db.initialize()
        processes = await db.get_all_processes()
        assert len(processes) == 0
        # leftover per-process record tables die with their rows
        cursor = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (f"proc_{pid}",),
        )
        assert await cursor.fetchone() is None


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


class TestReadNewRecords:
    @pytest.fixture
    async def proc(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        return pid

    async def test_returns_all_records_on_first_read(self, db, proc):
        await db.insert_record(proc, 1000, 1, "out1")
        await db.insert_record(proc, 2000, 2, "err1")
        records, cursors = await db.read_new_records(proc, [1, 2])
        assert [r["content"] for r in records] == ["out1", "err1"]
        assert cursors == {1: 1, 2: 2}
        assert records[0]["timestamp"] == 1000
        assert records[0]["source"] == 1

    async def test_second_read_returns_nothing_new(self, db, proc):
        await db.insert_record(proc, 1000, 1, "out1")
        records, cursors = await db.read_new_records(proc, [1])
        assert [r["content"] for r in records] == ["out1"]
        records2, cursors2 = await db.read_new_records(proc, [1])
        assert records2 == []
        assert cursors2 == {1: 1}

    async def test_per_source_cursors_are_independent(self, db, proc):
        await db.insert_record(proc, 1000, 1, "out1")
        await db.insert_record(proc, 2000, 2, "err1")
        records, cursors = await db.read_new_records(proc, [1])
        assert [r["content"] for r in records] == ["out1"]
        assert cursors == {1: 1}
        # stderr cursor untouched: the stderr record is still "new"
        records, cursors = await db.read_new_records(proc, [2])
        assert [r["content"] for r in records] == ["err1"]
        assert cursors == {2: 2}

    async def test_stdin_records_never_returned(self, db, proc):
        await db.insert_record(proc, 1000, 0, "echo")
        await db.insert_record(proc, 2000, 1, "out1")
        records, cursors = await db.read_new_records(proc, [1, 2])
        assert [r["content"] for r in records] == ["out1"]
        assert cursors == {1: 2, 2: 0}

    async def test_cursor_update_is_max_guarded(self, db, proc):
        """A stale advance with a smaller cursor must not regress the
        stored value (the UPDATE applies MAX against current value)."""
        await db.insert_record(proc, 1000, 1, "out1")
        await db.insert_record(proc, 2000, 1, "out2")
        await db.read_new_records(proc, [1])  # cursor -> 2
        await db._conn.execute(
            'UPDATE processes SET read_cursors = json_set('
            '  read_cursors, \'$."1"\','
            "  MAX(COALESCE(json_extract(read_cursors, '$.\"1\"'), 0), 1)"
            ") WHERE id = ?",
            (proc,),
        )
        await db._conn.commit()
        row = await db.get_process(proc)
        assert json.loads(row["read_cursors"])["1"] == 2

    async def test_stale_advance_blocked_by_read_gen(self, db, proc):
        """A stale advance (snapshotted read_gen) after clear_records must
        be a no-op: the advance UPDATE is conditioned on read_gen, and
        clear bumps it — works even for old=0 first reads."""
        await db.insert_record(proc, 1000, 1, "out1")
        await db.insert_record(proc, 2000, 1, "out2")
        await db.read_new_records(proc, [1])        # cursor -> 2, gen 0
        await db.clear_records(proc)                # gen -> 1, cursor {}
        await db.insert_record(proc, 3000, 1, "b")  # rowid 1 again
        # stale in-flight advance under the OLD generation (0)
        cur = await db._conn.execute(
            "UPDATE processes SET read_cursors = json_set("
            "  read_cursors, '$.\"1\"',"
            "  MAX(COALESCE(json_extract(read_cursors, '$.\"1\"'), 0), 99)"
            ") WHERE id = ? AND read_gen = 0",
            (proc,),
        )
        await db._conn.commit()
        assert cur.rowcount == 0
        row = await db.get_process(proc)
        assert json.loads(row["read_cursors"]) == {}
        # a fresh read still sees the post-clear record
        records, _ = await db.read_new_records(proc, [1])
        assert [r["content"] for r in records] == ["b"]

    async def test_empty_source_codes_rejected(self, db, proc):
        with pytest.raises(ValueError, match="source_codes"):
            await db.read_new_records(proc, [])

    async def test_unknown_process_raises(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.read_new_records(999, [1])


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

    async def test_resets_read_cursors(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        await db.insert_record(pid, 1000, 1, "a")
        await db.read_new_records(pid, [1, 2])
        await db.clear_records(pid)
        row = await db.get_process(pid)
        assert json.loads(row["read_cursors"]) == {}
        # rowid restarts at 1 after DELETE — a stale cursor would skip
        await db.insert_record(pid, 2000, 1, "b")
        records, cursors = await db.read_new_records(pid, [1, 2])
        assert [r["content"] for r in records] == ["b"]
        assert cursors == {1: 1, 2: 0}

    async def test_clear_bumps_read_gen(self, db):
        """clear_records bumps read_gen (default 0 → 1), which is what
        makes a stale snapshot's advance no-op."""
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        assert (await db.get_process(pid))["read_gen"] == 0
        await db.insert_record(pid, 1000, 1, "a")
        await db.read_new_records(pid, [1, 2])
        await db.clear_records(pid)
        assert (await db.get_process(pid))["read_gen"] == 1


class TestCleanupProcess:
    async def test_removes_process_and_table(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        await db.cleanup_process(pid)
        assert await db.get_process(pid) is None

    async def test_cleanup_nonexistent_is_noop(self, db):
        await db.cleanup_process(999)
