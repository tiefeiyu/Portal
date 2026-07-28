# Portal MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Portal — an MCP server that manages subprocesses with full I/O capture, idle timeout, signal support, and SQLite-backed output storage.

**Architecture:** Layered Python asyncio application: `ansi.py` (pure utility) → `database.py` (aiosqlite wrapper) → `process.py` (per-process lifecycle + background I/O) → `manager.py` (collection management + timeout monitor) → `server.py` (MCP tool registration). Each layer depends only on layers below it.

**Tech Stack:** Python 3.11+, `mcp` SDK, `aiosqlite`, asyncio standard library.

## Global Constraints

- Python >= 3.11 required
- Dependencies: `mcp>=1.0.0`, `aiosqlite>=0.20.0`
- Database: SQLite, created fresh on every MCP server startup (old file deleted)
- Process ID: internal auto-increment integer, not OS PID
- All timestamps: nanosecond precision (`time.time_ns()`)
- ANSI stripping: regex `\x1b\[[0-9;]*[a-zA-Z]` applied before storage
- Source codes: 0=stdin, 1=stdout, 2=stderr
- Process statuses: `running`, `exited`, `killed`
- Idle timeout: triggered when no external tool call touches the process for timeout_ms
- Timeout action: kill OS process + cleanup data
- Cross-platform: tests use Python subprocesses for portability

---

### Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `portal_mcp/__init__.py`
- Create: `tests/__init__.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: package structure importable as `portal_mcp`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "portal-mcp"
version = "0.1.0"
description = "MCP server for managing subprocesses"
requires-python = ">=3.11"
dependencies = [
    "mcp>=1.0.0",
    "aiosqlite>=0.20.0",
]

[project.scripts]
portal-mcp = "portal_mcp.server:main"

[build-system]
requires = ["setuptools>=75.0"]
build-backend = "setuptools.build_meta"
```

- [ ] **Step 2: Create portal_mcp/__init__.py**

```python
"""Portal MCP Server — manage subprocesses from MCP clients."""
```

- [ ] **Step 3: Create tests/__init__.py**

```python
"""Tests for Portal MCP Server."""
```

- [ ] **Step 4: Create .gitignore**

```
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
.venv/
venv/
portal.db
.pytest_cache/
```

- [ ] **Step 5: Install dependencies and verify**

Run: `pip install -e ".[dev]"` (inside venv with dev deps) or `pip install mcp aiosqlite`
Run: `python -c "import portal_mcp; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore: scaffold project structure"
```

---

### Task 2: ANSI Stripper

**Files:**
- Create: `portal_mcp/ansi.py`
- Create: `tests/test_ansi.py`

**Interfaces:**
- Produces: `strip_ansi(text: str) -> str`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for ANSI stripping."""
import pytest
from portal_mcp.ansi import strip_ansi


class TestStripAnsi:
    def test_removes_color_codes(self):
        assert strip_ansi("\x1b[31mred text\x1b[0m") == "red text"

    def test_removes_bold_and_dim(self):
        assert strip_ansi("\x1b[1mbold\x1b[0m \x1b[2mdim\x1b[0m") == "bold dim"

    def test_removes_cursor_movement(self):
        assert strip_ansi("\x1b[2J\x1b[Hhello") == "hello"

    def test_removes_extended_colors(self):
        assert strip_ansi("\x1b[38;5;196mred\x1b[0m") == "red"
        assert strip_ansi("\x1b[48;2;255;0;0m\x1b[38;2;0;255;0mtext\x1b[0m") == "text"

    def test_passes_through_plain_text(self):
        assert strip_ansi("hello world") == "hello world"
        assert strip_ansi("line1\nline2") == "line1\nline2"

    def test_handles_empty_string(self):
        assert strip_ansi("") == ""

    def test_handles_only_ansi(self):
        assert strip_ansi("\x1b[31m\x1b[0m") == ""

    def test_removes_complex_ansi_sequences(self):
        # Bold + red FG + green BG + underline
        text = "\x1b[1;31;42;4mstyled\x1b[0m normal"
        assert strip_ansi(text) == "styled normal"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ansi.py -v`
Expected: ImportError or NameError (module/function not found)

- [ ] **Step 3: Implement strip_ansi**

```python
"""ANSI escape sequence stripping."""
import re

# Matches CSI sequences: ESC [ params letter
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text.

    Strips CSI (Control Sequence Introducer) sequences which include
    color codes, cursor movement, and text formatting escape codes.

    Args:
        text: Input string potentially containing ANSI sequences.

    Returns:
        String with all ANSI escape sequences removed.
    """
    return _ANSI_PATTERN.sub("", text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ansi.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/ansi.py tests/test_ansi.py
git commit -m "feat(ansi): add ANSI escape sequence stripper"
```

---

### Task 3: Database Layer

**Files:**
- Create: `portal_mcp/database.py`
- Create: `tests/test_database.py`

**Interfaces:**
- Consumes: `strip_ansi` from `portal_mcp.ansi`
- Produces:
  - `class PortalError(Exception)` — base exception
  - `class Database`:
    - `__init__(self, db_path: str)`
    - `async initialize() -> None`
    - `async create_process(command: str, args: list[str], cwd: str | None, env: dict[str, str] | None, timeout_ms: int, started_at: int) -> int`
    - `async create_proc_table(proc_id: int) -> None`
    - `async insert_record(proc_id: int, timestamp: int, source: int, content: str) -> None`
    - `async read_records(proc_id: int, sources: list[int], since_ts: int) -> list[dict]`
    - `async update_status(proc_id: int, status: str, exit_code: int | None = None) -> None`
    - `async touch(proc_id: int) -> None`
    - `async get_process(proc_id: int) -> dict | None`
    - `async get_all_processes() -> list[dict]`
    - `async io_count(proc_id: int) -> dict`
    - `async clear_records(proc_id: int) -> None`
    - `async cleanup_process(proc_id: int) -> None`
    - `async close() -> None`

- [ ] **Step 1: Write failing tests**

```python
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
        # Verify processes table exists
        cursor = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='processes'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "processes"

    async def test_initialize_clears_old_data(self, db):
        # Insert a process
        pid = await db.create_process("cmd", [], None, None, 5000, 1000000)
        await db.create_proc_table(pid)
        # Re-initialize
        await db.initialize()
        # Old data should be gone
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
        assert proc["args"] == '["-c", "print(\'hi\')"]'
        assert proc["cwd"] == "/tmp"
        assert proc["env"] == '{"FOO": "bar"}'
        assert proc["timeout_ms"] == 10000
        assert proc["status"] == "running"
        assert proc["started_at"] == 1234567890000000000
        assert proc["last_active_at"] == 1234567890000000000
        assert proc["exit_code"] is None


class TestProcTable:
    async def test_create_and_drop(self, db):
        pid = await db.create_process("cmd", [], None, None, 0, 0)
        await db.create_proc_table(pid)
        # Verify table exists
        cursor = await db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (f"proc_{pid}",),
        )
        row = await cursor.fetchone()
        assert row is not None
        # Cleanup should drop it
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

        # Read stdout only
        records = await db.read_records(pid, [1], 0)
        assert len(records) == 2
        assert all(r["source"] == 1 for r in records)
        assert records[0]["content"] == "stdout line 1"
        assert records[1]["content"] == "stdout line 2"

        # Read both
        records = await db.read_records(pid, [1, 2], 0)
        assert len(records) == 3

        # Read since timestamp
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
        # Should not raise
        await db.cleanup_process(999)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_database.py -v`
Expected: ImportError (module not found)

- [ ] **Step 3: Implement database.py**

```python
"""SQLite database layer for Portal MCP server."""
import json
import os
import time
from pathlib import Path

import aiosqlite


class PortalError(Exception):
    """Base exception for Portal errors."""


class Database:
    """Async SQLite database wrapper for process and I/O storage.

    The database is created fresh on each server startup. Old database
    file should be deleted before calling initialize().
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open connection and create the processes table.

        The database file should already be deleted before this call
        if a fresh start is desired.
        """
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                os_pid INTEGER,
                status TEXT NOT NULL DEFAULT 'running',
                started_at INTEGER NOT NULL,
                timeout_ms INTEGER NOT NULL DEFAULT 0,
                last_active_at INTEGER NOT NULL,
                command TEXT NOT NULL,
                args TEXT DEFAULT '[]',
                cwd TEXT,
                env TEXT DEFAULT '{}',
                exit_code INTEGER
            )
            """
        )
        # Clean any leftover data from previous runs
        # (if DB wasn't deleted, clear existing rows)
        await self._conn.execute("DELETE FROM processes")
        await self._conn.commit()

    async def create_process(
        self,
        command: str,
        args: list[str],
        cwd: str | None,
        env: dict[str, str] | None,
        timeout_ms: int,
        started_at: int,
    ) -> int:
        """Insert a new process row and return its auto-increment ID."""
        cursor = await self._conn.execute(
            """
            INSERT INTO processes
                (os_pid, status, started_at, timeout_ms, last_active_at,
                 command, args, cwd, env)
            VALUES (0, 'running', ?, ?, ?,
                    ?, ?, ?, ?)
            """,
            (
                started_at,
                timeout_ms,
                started_at,
                command,
                json.dumps(args) if args else "[]",
                cwd,
                json.dumps(env) if env else "{}",
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def create_proc_table(self, proc_id: int) -> None:
        """Create the per-process I/O records table."""
        await self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS proc_{proc_id} (
                timestamp INTEGER NOT NULL,
                source INTEGER NOT NULL,
                content TEXT NOT NULL
            )
            """
        )
        await self._conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_proc_{proc_id}_ts "
            f"ON proc_{proc_id}(timestamp)"
        )
        await self._conn.commit()

    async def insert_record(
        self, proc_id: int, timestamp: int, source: int, content: str
    ) -> None:
        """Insert a single I/O record into the process table."""
        await self._conn.execute(
            f"INSERT INTO proc_{proc_id} (timestamp, source, content) "
            f"VALUES (?, ?, ?)",
            (timestamp, source, content),
        )
        await self._conn.commit()

    async def read_records(
        self, proc_id: int, sources: list[int], since_ts: int
    ) -> list[dict]:
        """Read records from a process table.

        Args:
            proc_id: Internal process ID.
            sources: List of source codes to include (e.g., [1, 2]).
            since_ts: Only return records with timestamp >= this value.

        Returns:
            List of dicts with keys: timestamp, source, content.
        """
        placeholders = ",".join("?" * len(sources))
        cursor = await self._conn.execute(
            f"SELECT timestamp, source, content FROM proc_{proc_id} "
            f"WHERE source IN ({placeholders}) AND timestamp >= ? "
            f"ORDER BY timestamp ASC",
            (*sources, since_ts),
        )
        rows = await cursor.fetchall()
        return [
            {"timestamp": row[0], "source": row[1], "content": row[2]}
            for row in rows
        ]

    async def update_status(
        self, proc_id: int, status: str, exit_code: int | None = None
    ) -> None:
        """Update process status and optionally the exit code."""
        await self._conn.execute(
            "UPDATE processes SET status = ?, exit_code = ? WHERE id = ?",
            (status, exit_code, proc_id),
        )
        await self._conn.commit()

    async def update_os_pid(self, proc_id: int, os_pid: int) -> None:
        """Update the OS-level PID after process spawn."""
        await self._conn.execute(
            "UPDATE processes SET os_pid = ? WHERE id = ?",
            (os_pid, proc_id),
        )
        await self._conn.commit()

    async def touch(self, proc_id: int) -> None:
        """Update last_active_at to current time (nanoseconds)."""
        now = time.time_ns()
        await self._conn.execute(
            "UPDATE processes SET last_active_at = ? WHERE id = ?",
            (now, proc_id),
        )
        await self._conn.commit()

    async def get_process(self, proc_id: int) -> dict | None:
        """Get a single process by ID, or None if not found."""
        cursor = await self._conn.execute(
            "SELECT * FROM processes WHERE id = ?", (proc_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def get_all_processes(self) -> list[dict]:
        """Get all processes ordered by ID."""
        cursor = await self._conn.execute(
            "SELECT * FROM processes ORDER BY id ASC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def io_count(self, proc_id: int) -> dict:
        """Get record counts by source for a process.

        Returns:
            Dict with keys: total, stdout, stderr, stdin.
        """
        cursor = await self._conn.execute(
            f"SELECT source, COUNT(*) FROM proc_{proc_id} GROUP BY source"
        )
        rows = await cursor.fetchall()
        counts = {"total": 0, "stdout": 0, "stderr": 0, "stdin": 0}
        source_map = {1: "stdout", 2: "stderr", 0: "stdin"}
        for source, count in rows:
            key = source_map.get(source, "total")
            counts[key] = count
            counts["total"] += count
        return counts

    async def clear_records(self, proc_id: int) -> None:
        """Delete all I/O records for a process (table remains)."""
        await self._conn.execute(f"DELETE FROM proc_{proc_id}")
        await self._conn.commit()

    async def cleanup_process(self, proc_id: int) -> None:
        """Drop the process I/O table and delete the process row."""
        await self._conn.execute(f"DROP TABLE IF EXISTS proc_{proc_id}")
        await self._conn.execute("DELETE FROM processes WHERE id = ?", (proc_id,))
        await self._conn.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_database.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/database.py tests/test_database.py
git commit -m "feat(database): add async SQLite database layer"
```

---

### Task 4: ManagedProcess

**Files:**
- Create: `portal_mcp/process.py`
- Create: `tests/test_process.py`

**Interfaces:**
- Consumes: `Database` from `portal_mcp.database`, `strip_ansi` from `portal_mcp.ansi`
- Produces:
  - `class ManagedProcess`:
    - `id: int` (property)
    - `os_pid: int` (property)
    - `status: str` (property)
    - `async start_background_readers(db: Database) -> None`
    - `async write_stdin(db: Database, content: str) -> None`
    - `async send_signal(sig: int) -> None`
    - `async wait_exit() -> None`
    - `async kill() -> None`

Note: `ManagedProcess.__init__` takes `proc_id`, `process` (asyncio subprocess), `timeout_ms`, and `db`. The factory (in Task 5) creates the subprocess, creates the process via DB, then constructs ManagedProcess.

- [ ] **Step 1: Write failing tests**

```python
"""Tests for ManagedProcess."""
import asyncio
import os
import signal
import sys
import time
import pytest
from portal_mcp.process import ManagedProcess


@pytest.fixture
async def db_for_proc():
    """Database fixture scoped to process tests."""
    import tempfile
    from portal_mcp.database import Database

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    await db.initialize()
    yield db
    # Clean up all tables before close
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
        # On POSIX, status may not update synchronously
        # Give it a moment
        try:
            await asyncio.wait_for(mp.wait_exit(), timeout=3)
        except asyncio.TimeoutError:
            pass
        assert mp.status in ("exited", "killed")


class TestManagedProcessIO:
    async def test_reads_stdout(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; sys.stdout.write('hello\\n'); sys.stdout.write('world\\n')",
        )
        await mp.start_background_readers(db_for_proc)
        await mp.wait_exit()

        records = await db_for_proc.read_records(mp.id, [1], 0)
        contents = [r["content"] for r in records]
        assert "hello\n" in contents
        assert "world\n" in contents

    async def test_reads_stderr(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; sys.stderr.write('error msg\\n')",
        )
        await mp.start_background_readers(db_for_proc)
        await mp.wait_exit()

        records = await db_for_proc.read_records(mp.id, [2], 0)
        contents = [r["content"] for r in records]
        assert "error msg\n" in contents

    async def test_strips_ansi_from_output(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; sys.stdout.write('\\x1b[31mred\\x1b[0m\\n')",
        )
        await mp.start_background_readers(db_for_proc)
        await mp.wait_exit()

        records = await db_for_proc.read_records(mp.id, [1], 0)
        contents = [r["content"] for r in records]
        assert "red\n" in contents
        assert "\x1b" not in contents[0]

    async def test_write_stdin(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; data = sys.stdin.read(); sys.stdout.write(data)",
        )
        await mp.start_background_readers(db_for_proc)
        await mp.write_stdin(db_for_proc, "input data")
        await mp.wait_exit()

        records = await db_for_proc.read_records(mp.id, [1], 0)
        contents = "".join(r["content"] for r in records)
        assert "input data" in contents

    async def test_write_stdin_records_in_db(self, db_for_proc):
        mp = await _create_test_process(
            db_for_proc,
            "import sys; sys.stdin.read()",
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
        mp = await _create_test_process(
            db_for_proc, "pass"
        )
        await mp.wait_exit()
        with pytest.raises(RuntimeError, match="not running"):
            await mp.send_signal(signal.SIGTERM if hasattr(signal, "SIGTERM") else 15)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_process.py -v`
Expected: ImportError

- [ ] **Step 3: Implement process.py**

```python
"""ManagedProcess — wraps an asyncio subprocess with I/O capture."""
import asyncio
import time

from portal_mcp.ansi import strip_ansi
from portal_mcp.database import Database


_STDIN_CLOSED = object()


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_process.py -v`
Expected: 8 passed (some may be skipped on Windows)

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/process.py tests/test_process.py
git commit -m "feat(process): add ManagedProcess with background I/O capture"
```

---

### Task 5: ProcessManager

**Files:**
- Create: `portal_mcp/manager.py`
- Create: `tests/test_manager.py`

**Interfaces:**
- Consumes: `Database` from `portal_mcp.database`, `ManagedProcess` from `portal_mcp.process`
- Produces:
  - `class ProcessManager`:
    - `__init__(self, db: Database)`
    - `async start(command, args, cwd, env, timeout_ms) -> dict`
    - `async read(proc_id, source, duration, unit) -> list[dict]`
    - `async write(proc_id, content) -> dict`
    - `async send_signal(proc_id, sig) -> dict`
    - `async list_all() -> list[dict]`
    - `async inspect(proc_id) -> dict`
    - `async do_kill(proc_id) -> dict`
    - `async kill_all() -> dict`
    - `async clear(proc_id) -> dict`
    - `async do_cleanup(proc_id) -> dict`
    - `async shutdown() -> None`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for ProcessManager."""
import asyncio
import os
import signal
import sys
import tempfile
import time
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
    if os.path.exists(path):
        os.unlink(path)


class TestStartProcess:
    async def test_start_simple_command(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('hello')"],
        )
        assert "id" in result
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
        # Wait and read output
        await asyncio.sleep(0.5)
        records = await manager.read(result["id"], "stdout", 10000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "bar_value" in contents

    async def test_start_invalid_command(self, manager):
        with pytest.raises(Exception):
            await manager.start(
                command="/nonexistent/path/to/binary",
                args=[],
            )


class TestRead:
    async def test_read_stdout(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('hello stdout')"],
        )
        await asyncio.sleep(0.5)
        records = await manager.read(result["id"], "stdout", 5000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "hello stdout" in contents

    async def test_read_stderr(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import sys; sys.stderr.write('hello stderr\\n')"],
        )
        await asyncio.sleep(0.5)
        records = await manager.read(result["id"], "stderr", 5000, "ms")
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
        await asyncio.sleep(0.5)
        records = await manager.read(result["id"], "both", 5000, "ms")
        sources = {r["source"] for r in records}
        assert 1 in sources  # stdout
        assert 2 in sources  # stderr

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
        # Reading should reset the timer
        await manager.read(result["id"], "both", 1000, "ms")
        # Process should still be alive
        proc_info = await manager.inspect(result["id"])
        assert proc_info["status"] == "running"


class TestWrite:
    async def test_write_stdin(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import sys; data = sys.stdin.read(); print(data, end='')"],
        )
        await manager.write(result["id"], "test input")
        await asyncio.sleep(0.5)
        records = await manager.read(result["id"], "stdout", 5000, "ms")
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
            pytest.skip("Signal tests use SIGTERM which is POSIX-only")
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
        )
        sig = signal.SIGTERM
        await manager.send_signal(result["id"], sig)
        await asyncio.sleep(0.5)
        proc_info = await manager.inspect(result["id"])
        assert proc_info["status"] in ("exited", "killed")

    async def test_signal_to_nonexistent(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.send_signal(99999, signal.SIGTERM if hasattr(signal, "SIGTERM") else 15)


class TestListAll:
    async def test_empty_list(self, manager):
        result = await manager.list_all()
        assert result == []

    async def test_multiple_processes(self, manager):
        await manager.start(
            command=sys.executable, args=["-c", "import time; time.sleep(2)"]
        )
        await manager.start(
            command=sys.executable, args=["-c", "import time; time.sleep(2)"]
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
            command=sys.executable, args=["-c", "import time; time.sleep(30)"]
        )
        r2 = await manager.start(
            command=sys.executable, args=["-c", "import time; time.sleep(30)"]
        )
        await manager.kill_all()
        # Verify all killed
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
        # Verify there is data
        counts = (await manager.inspect(result["id"]))["io_count"]
        assert counts > 0
        # Clear
        await manager.clear(result["id"])
        counts = (await manager.inspect(result["id"]))["io_count"]
        assert counts == 0


class TestCleanup:
    async def test_cleanup_exited_process(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "pass"],
        )
        await asyncio.sleep(0.5)
        await manager.do_cleanup(result["id"])
        with pytest.raises(ValueError, match="not found"):
            await manager.inspect(result["id"])

    async def test_cleanup_running_rejected(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(10)"],
        )
        with pytest.raises(ValueError, match="must be exited or killed"):
            await manager.do_cleanup(result["id"])

    async def test_cleanup_nonexistent(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.do_cleanup(99999)


class TestTimeoutMonitor:
    async def test_timeout_kills_idle_process(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            timeout_ms=500,  # 500ms timeout
        )
        # Wait for timeout monitor to detect and kill
        await asyncio.sleep(2)
        with pytest.raises(ValueError, match="not found"):
            await manager.inspect(result["id"])

    async def test_activity_prevents_timeout(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            timeout_ms=2000,
        )
        # Keep touching the process
        for _ in range(3):
            await asyncio.sleep(0.3)
            await manager.read(result["id"], "both", 1000, "ms")
        # Process should still be alive
        info = await manager.inspect(result["id"])
        assert info["status"] == "running"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_manager.py -v`
Expected: ImportError

- [ ] **Step 3: Implement manager.py**

```python
"""ProcessManager — manages the lifecycle of all ManagedProcess instances."""
import asyncio
import json
import signal
import time
from typing import Any

from portal_mcp.database import Database
from portal_mcp.process import ManagedProcess


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
            f"Unknown source '{source}'. Supported: stdout, stderr, stdin, both"
        )
    return source_map[source]


class ProcessManager:
    """Manages all subprocesses.

    Maintains a dict of ManagedProcess instances indexed by internal ID.
    Runs a background timeout monitor that kills idle processes.
    """

    def __init__(self, db: Database):
        self._db = db
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
                # Timeout exceeded — kill and cleanup
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
    ) -> dict:
        """Start a new subprocess.

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

        # Merge env with current env if provided
        process_env = None
        if env:
            import os
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
            "os_pid": subproc.pid,
            "status": "running",
        }

    async def _watch_exit(self, proc_id: int, mp: ManagedProcess) -> None:
        """Background task: wait for process exit and update DB."""
        try:
            exit_code = await mp.wait_exit()
            status = mp.status  # "exited" or "killed"
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
        proc = self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")

        duration_ns = _parse_duration(duration, unit)
        sources = _source_to_codes(source)
        since_ts = time.time_ns() - duration_ns

        # Touch the process to reset idle timer
        await self._db.touch(proc_id)

        return await self._db.read_records(proc_id, sources, since_ts)

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
                f"Process {proc_id} is not running (status={proc['status']})"
            )

        mp = self._processes.get(proc_id)
        if mp is None:
            raise ValueError(f"Process {proc_id} not found in manager")

        await mp.write_stdin(self._db, content)
        await self._db.touch(proc_id)

        return {"id": proc_id, "bytes_written": len(content.encode("utf-8"))}

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

        # Convert string signal name to int
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_manager.py -v`
Expected: 19 passed (some skipped on Windows for signal tests)

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/manager.py tests/test_manager.py
git commit -m "feat(manager): add ProcessManager with timeout monitor and full lifecycle"
```

---

### Task 6: MCP Server Entry Point

**Files:**
- Create: `portal_mcp/server.py`
- Create: `tests/test_server.py`

**Interfaces:**
- Consumes: `ProcessManager` from `portal_mcp.manager`, `Database` from `portal_mcp.database`
- Produces: `main()` — entry point, creates MCP server, registers tools, runs via stdio

- [ ] **Step 1: Write integration test**

```python
"""Integration tests for the MCP server tools."""
import os
import sys
import tempfile
import pytest
from portal_mcp.server import create_server


class TestServerTools:
    """Test MCP tools by calling handler functions directly."""

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
        manager = portal
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('hello from test')"],
        )
        assert result["status"] == "running"
        import asyncio
        await asyncio.sleep(0.5)
        records = await manager.read(result["id"], "both", 5000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "hello from test" in contents

    async def test_process_write_and_echo(self, portal):
        manager = portal
        result = await manager.start(
            command=sys.executable,
            args=["-c",
                  "import sys; "
                  "line = sys.stdin.readline(); "
                  "print(f'echo: {line}', end='')"],
        )
        await manager.write(result["id"], "test message\n")
        import asyncio
        await asyncio.sleep(0.5)
        records = await manager.read(result["id"], "both", 5000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "echo: test message" in contents

    async def test_process_list(self, portal):
        manager = portal
        assert await manager.list_all() == []
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

    async def test_process_kill(self, portal):
        manager = portal
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
        )
        kill_result = await manager.do_kill(result["id"])
        assert kill_result["status"] == "killed"
        info = await manager.inspect(result["id"])
        assert info["status"] in ("exited", "killed")

    async def test_process_cleanup(self, portal):
        manager = portal
        result = await manager.start(
            command=sys.executable,
            args=["-c", "pass"],
        )
        import asyncio
        await asyncio.sleep(0.5)
        await manager.do_cleanup(result["id"])
        with pytest.raises(ValueError):
            await manager.inspect(result["id"])

    async def test_clear_records(self, portal):
        manager = portal
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('data')"],
        )
        import asyncio
        await asyncio.sleep(0.5)
        await manager.clear(result["id"])
        info = await manager.inspect(result["id"])
        assert info["io_count"] == 0

    async def test_inspect(self, portal):
        manager = portal
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(2)"],
            timeout_ms=10000,
        )
        info = await manager.inspect(result["id"])
        assert info["command"] == sys.executable
        assert "timeout_ms" in info
        assert info["timeout_ms"] == 10000

    async def test_ans_is_stripped(self, portal):
        manager = portal
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('\\x1b[31mred text\\x1b[0m')"],
        )
        import asyncio
        await asyncio.sleep(0.5)
        records = await manager.read(result["id"], "stdout", 5000, "ms")
        contents = "".join(r["content"] for r in records)
        assert "red text" in contents
        assert "\x1b" not in contents
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server.py -v`
Expected: ImportError

- [ ] **Step 3: Implement server.py**

```python
"""Portal MCP Server — entry point and tool registration."""
import os
import sys
import time

from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationCapabilities
from portal_mcp.database import Database
from portal_mcp.manager import ProcessManager

import mcp.server.stdio
import mcp.types as types


SERVER_NAME = "portal"
SERVER_VERSION = "0.1.0"


def _signal_help() -> str:
    """Platform-specific signal help text."""
    import signal
    common = "Send a signal to a process by name or number."
    if sys.platform == "win32":
        return (
            f"{common} Windows supports: "
            "CTRL_C_EVENT (0), CTRL_BREAK_EVENT (1). "
            "SIGTERM is mapped to TerminateProcess."
        )
    else:
        names = [
            s for s in dir(signal)
            if s.startswith("SIG") and not s.startswith("SIG_")
        ]
        return (
            f"{common} Available signals: {', '.join(sorted(names))}."
        )


async def create_server(db_path: str | None = None):
    """Create and configure the Portal MCP server.

    Args:
        db_path: Path to SQLite database. Defaults to 'portal.db'
            in the current directory.

    Returns:
        Tuple of (ProcessManager, Database) for testing.
    """
    if db_path is None:
        db_path = "portal.db"

    # Delete old database for fresh start
    if os.path.exists(db_path):
        os.unlink(db_path)

    db = Database(db_path)
    await db.initialize()

    manager = ProcessManager(db)
    await manager.start_monitor()

    return manager, db


def main():
    """Entry point for the Portal MCP server."""
    import asyncio

    async def run():
        db_path = os.environ.get("PORTAL_DB_PATH", "portal.db")
        manager, db = await create_server(db_path)
        server = Server(SERVER_NAME, version=SERVER_VERSION)

        @server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name="process_start",
                    description=(
                        "Start a subprocess. Returns the internal process ID, "
                        "OS PID, and initial status."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Executable or command to run.",
                            },
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Command-line arguments.",
                                "default": [],
                            },
                            "cwd": {
                                "type": "string",
                                "description": "Working directory.",
                            },
                            "env": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                                "description": "Environment variables (merged "
                                "with current env).",
                            },
                            "timeout_ms": {
                                "type": "integer",
                                "description": (
                                    "Idle timeout in milliseconds. Process is "
                                    "killed and cleaned up if no tool "
                                    "interaction occurs for this duration. "
                                    "0 means no timeout."
                                ),
                                "default": 0,
                            },
                        },
                        "required": ["command"],
                    },
                ),
                types.Tool(
                    name="process_read",
                    description=(
                        "Read output from a process. Reads records from the "
                        "specified time window. Resets the process idle timer."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                            "source": {
                                "type": "string",
                                "enum": ["stdout", "stderr", "stdin", "both"],
                                "description": "Which output stream to read.",
                                "default": "both",
                            },
                            "duration": {
                                "type": "integer",
                                "description": "How far back to read.",
                                "default": 1000,
                            },
                            "unit": {
                                "type": "string",
                                "enum": ["ns", "us", "ms", "s"],
                                "description": "Time unit for duration.",
                                "default": "ms",
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="process_write",
                    description=(
                        "Write content to a process's stdin. Only available "
                        "while the process is running."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Content to write to stdin.",
                            },
                        },
                        "required": ["id", "content"],
                    },
                ),
                types.Tool(
                    name="process_signal",
                    description=_signal_help(),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                            "signal": {
                                "type": "string",
                                "description": (
                                    "OS signal name (e.g., SIGTERM, SIGKILL, "
                                    "SIGINT) or signal number."
                                ),
                            },
                        },
                        "required": ["id", "signal"],
                    },
                ),
                types.Tool(
                    name="process_list",
                    description=(
                        "List all managed processes with summary info: "
                        "id, os_pid, status, timeout, idle duration, "
                        "and I/O record count."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                types.Tool(
                    name="process_inspect",
                    description=(
                        "Get detailed information about a single process "
                        "including all metadata and per-stream I/O counts."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="process_kill",
                    description=(
                        "Kill a single process. Its output data is retained "
                        "for reading. Use process_cleanup to remove it."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="process_kill_all",
                    description="Kill all managed processes.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                types.Tool(
                    name="process_clear",
                    description=(
                        "Clear all I/O records for a process. "
                        "The process table remains, but records are deleted."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                        },
                        "required": ["id"],
                    },
                ),
                types.Tool(
                    name="process_cleanup",
                    description=(
                        "Remove a terminated process and all its data. "
                        "Only allowed for processes with status 'exited' "
                        "or 'killed'."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                        },
                        "required": ["id"],
                    },
                ),
            ]

        @server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict
        ) -> list[types.TextContent]:
            try:
                if name == "process_start":
                    result = await manager.start(
                        command=arguments["command"],
                        args=arguments.get("args", []),
                        cwd=arguments.get("cwd"),
                        env=arguments.get("env"),
                        timeout_ms=arguments.get("timeout_ms", 0),
                    )
                    return [types.TextContent(
                        type="text",
                        text=f"Process started. ID={result['id']}, "
                        f"OS_PID={result['os_pid']}, "
                        f"status={result['status']}",
                    )]

                elif name == "process_read":
                    records = await manager.read(
                        proc_id=arguments["id"],
                        source=arguments.get("source", "both"),
                        duration=arguments.get("duration", 1000),
                        unit=arguments.get("unit", "ms"),
                    )
                    if not records:
                        return [types.TextContent(
                            type="text",
                            text="No output records found in the specified "
                            "time window.",
                        )]
                    # Format records
                    lines = []
                    for r in records:
                        source_label = {0: "STDIN", 1: "STDOUT", 2: "STDERR"}
                        label = source_label.get(r["source"], f"SRC{r['source']}")
                        lines.append(f"[{r['timestamp']}] [{label}] {r['content']}")
                    return [types.TextContent(
                        type="text",
                        text="".join(lines),
                    )]

                elif name == "process_write":
                    result = await manager.write(
                        proc_id=arguments["id"],
                        content=arguments["content"],
                    )
                    return [types.TextContent(
                        type="text",
                        text=f"Written {result['bytes_written']} bytes to "
                        f"process {result['id']} stdin.",
                    )]

                elif name == "process_signal":
                    sig = arguments["signal"]
                    # Try to parse as int if possible
                    try:
                        sig = int(sig)
                    except ValueError:
                        pass
                    result = await manager.send_signal(
                        proc_id=arguments["id"],
                        sig=sig,
                    )
                    return [types.TextContent(
                        type="text",
                        text=f"Signal {result['signal_sent']} sent to "
                        f"process {result['id']}.",
                    )]

                elif name == "process_list":
                    processes = await manager.list_all()
                    if not processes:
                        return [types.TextContent(
                            type="text",
                            text="No managed processes.",
                        )]
                    lines = ["ID | OS_PID | STATUS  | TIMEOUT_MS | INACTIVE_MS | IO_COUNT"]
                    lines.append("-" * 65)
                    for p in processes:
                        lines.append(
                            f"{p['id']:2} | {p['os_pid']:6} | "
                            f"{p['status']:7} | {p['timeout_ms']:10} | "
                            f"{p['inactive_duration_ms']:11} | {p['io_count']:8}"
                        )
                    return [types.TextContent(
                        type="text",
                        text="\n".join(lines),
                    )]

                elif name == "process_inspect":
                    info = await manager.inspect(arguments["id"])
                    return [types.TextContent(
                        type="text",
                        text=(
                            f"Process {info['id']}:\n"
                            f"  OS PID:     {info['os_pid']}\n"
                            f"  Status:     {info['status']}\n"
                            f"  Command:    {info['command']}\n"
                            f"  Args:       {info['args']}\n"
                            f"  CWD:        {info['cwd']}\n"
                            f"  Env:        {info['env']}\n"
                            f"  Timeout:    {info['timeout_ms']} ms\n"
                            f"  Exit Code:  {info['exit_code']}\n"
                            f"  IO Records: {info['io_count']} total "
                            f"(stdout={info['stdout_count']}, "
                            f"stderr={info['stderr_count']}, "
                            f"stdin={info['stdin_count']})\n"
                        ),
                    )]

                elif name == "process_kill":
                    result = await manager.do_kill(arguments["id"])
                    return [types.TextContent(
                        type="text",
                        text=f"Process {result['id']} killed. "
                        f"Status: {result['status']}.",
                    )]

                elif name == "process_kill_all":
                    result = await manager.kill_all()
                    return [types.TextContent(
                        type="text",
                        text=f"Killed {result['killed']} process(es).",
                    )]

                elif name == "process_clear":
                    result = await manager.clear(arguments["id"])
                    return [types.TextContent(
                        type="text",
                        text=f"Process {result['id']} I/O records cleared.",
                    )]

                elif name == "process_cleanup":
                    result = await manager.do_cleanup(arguments["id"])
                    return [types.TextContent(
                        type="text",
                        text=f"Process {result['id']} cleaned up.",
                    )]

                else:
                    return [types.TextContent(
                        type="text",
                        text=f"Unknown tool: {name}",
                    )]

            except ValueError as e:
                return [types.TextContent(
                    type="text",
                    text=f"Error: {e}",
                )]
            except RuntimeError as e:
                return [types.TextContent(
                    type="text",
                    text=f"Error: {e}",
                )]
            except Exception as e:
                return [types.TextContent(
                    type="text",
                    text=f"Unexpected error: {type(e).__name__}: {e}",
                )]

        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationCapabilities(
                    sampling={},
                    experimental={},
                    roots={},
                ),
                NotificationOptions(),
            )

        # Cleanup on exit
        await manager.shutdown()
        await db.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run integration tests**

Run: `pytest tests/test_server.py -v`
Expected: 8 passed

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass (some may be skipped on Windows)

- [ ] **Step 6: Commit**

```bash
git add portal_mcp/server.py tests/test_server.py
git commit -m "feat(server): add MCP server entry point with all 10 tools"
```

---

### Task 7: README

**Files:**
- Create: `README.md`

**Interfaces:** None

- [ ] **Step 1: Write README.md**

```markdown
# Portal MCP Server

An MCP server for managing subprocesses. Start, monitor, read/write I/O,
and control processes — all through MCP tool calls.

## Installation

```bash
pip install -e .
```

Requires Python 3.11+.

## Configuration

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "portal": {
      "command": "python",
      "args": ["-m", "portal_mcp.server"],
      "env": {
        "PORTAL_DB_PATH": "/path/to/portal.db"
      }
    }
  }
}
```

Or use the entry point:

```json
{
  "mcpServers": {
    "portal": {
      "command": "portal-mcp"
    }
  }
}
```

The `PORTAL_DB_PATH` environment variable controls where the SQLite
database is stored (default: `portal.db` in the working directory).
The database is created fresh on every server startup.

## Tools

| Tool | Description |
|------|-------------|
| `process_start` | Start a subprocess with optional args, cwd, env, timeout |
| `process_read` | Read stdout/stderr/stdin records within a time window |
| `process_write` | Write content to a process's stdin |
| `process_signal` | Send an OS signal to a process |
| `process_list` | List all managed processes with summary |
| `process_inspect` | Get detailed info about a process |
| `process_kill` | Kill a single process (data retained) |
| `process_kill_all` | Kill all managed processes |
| `process_clear` | Clear I/O records for a process |
| `process_cleanup` | Remove a terminated process and all its data |

## Process Lifecycle

```
START → RUNNING → EXITED  → (read-only, data retained)
              → KILLED  → (read-only, data retained)
              → timeout → KILL + CLEANUP (data removed)

READ/WRITE resets the idle timer (prevents timeout kills).
```

## License

MIT
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README with installation and usage guide"
```

---

### Task 8: Final Verification

- [ ] **Step 1: Install the package in development mode**

Run: `pip install -e .`
Expected: Success

- [ ] **Step 2: Run the full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 3: Verify MCP server starts**

Run: `python -m portal_mcp.server` (it will block on stdio — interrupt after confirming no crash)
Expected: No errors, server starts and waits for stdio input

- [ ] **Step 4: Final commit if any changes**

```bash
git status
# Commit any remaining changes if needed
```
