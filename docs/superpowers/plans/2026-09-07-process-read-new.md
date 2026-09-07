# process_read_new Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `process_read_new` MCP tool that returns only output produced since the last call, with per-source cursors (stdout/stderr) remembered server-side.

**Architecture:** Per-source read cursors stored as a JSON column (`read_cursors`) on the `processes` table. The database layer reads records newer than each source's cursor and advances them via one atomic `MAX`-guarded `UPDATE`; the manager adds `read_new()`; the server registers the tool. Cursors are fully independent from `process_read`'s time-window reads and reset by `process_clear`.

**Tech Stack:** Python 3.11+, aiosqlite, MCP 1.x, pytest (`asyncio_mode = auto`).

## Global Constraints

- Python `>=3.11`; deps unchanged (no new dependencies).
- DB layer style: plain SQL strings, one `await self._conn.commit()` per write, records list under `proc_<id>` with implicit `rowid`.
- Error messages (verbatim, tests match on substrings):
  - unknown proc: `ValueError(f"Process {proc_id} not found")` (already used by `read`)
  - unknown source: `ValueError(f"Unknown source '{source}'. Supported: stdout, stderr, both")`
- Source enum for `read_new`: `stdout | stderr | both` (default `both`); source codes `1`=stdout, `2`=stderr, `0`=stdin (stdin never returned by read_new, consistent with `process_read`'s `both`).
- Cursor key in JSON: source **code** as string key, e.g. `{"1": 5, "2": 3}` — JSON object keys are always strings; normalize to `int` after `json.loads`.
- All user-visible text (tool descriptions, README additions, MCP instructions) in English for README.md and matching Chinese for README_zh.md.
- Commit messages: English, Conventional Commits, single-line `-m`.
- Tests: `pytest` with `asyncio_mode = "auto"`, run via `uv run pytest ...`; always add `from ... import` / `import json` at the top of test files.

---

### Task 1: Database layer — `read_cursors` column + `read_new_records`

**Files:**
- Modify: `tests/test_database.py` (new tests, `import json` at top)
- Modify: `portal_mcp/database.py` (`initialize`, new `read_new_records`, `clear_records`)

**Interfaces:**
- Consumes: existing `Database` fixture (`db`), existing `create_process`/`create_proc_table`/`insert_record`.
- Produces:
  - `await db.read_new_records(proc_id: int, source_codes: list[int]) -> tuple[list[dict], dict[int, int]]` — records (`timestamp`/`source`/`content`, insertion order) + `next_cursors` mapping each requested source code to its new cursor value.
  - `processes.read_cursors TEXT NOT NULL DEFAULT '{}'` column (JSON `{"1": n, "2": m}`).
  - `clear_records` now also resets `read_cursors = '{}'`.

- [ ] **Step 1: Write the failing tests (append to `tests/test_database.py`)**

Add `import json` to the existing imports:

```python
"""Tests for database layer."""
import json
import os
import tempfile
import pytest
from portal_mcp.database import Database, PortalError
```

Append these test classes (after `TestInsertAndRead`):

```python
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
```

Extend `TestClearRecords` (in the existing class):

```python
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
```

Extend `TestDatabaseInitialize`:

```python
    async def test_initialize_adds_cursor_column_to_stale_db(self, tmp_path):
        """A stale DB whose processes table lacks read_cursors must not
        crash initialize() (safety net for direct Database() use)."""
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
        await database.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_database.py -v`
Expected: FAIL — `TypeError: read_new_records()` missing / no `read_cursors` column / `AttributeError`.

- [ ] **Step 3: Implement in `portal_mcp/database.py`**

In `initialize()`, add the column to `CREATE TABLE`:

```python
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
                exit_code INTEGER,
                read_cursors TEXT NOT NULL DEFAULT '{}'
            )
            """
```

And right after `await self._conn.commit()` at the end of `initialize()` (after the DELETE), add the defensive migration:

```python
        # Safety net: a stale DB file opened without the column (callers
        # that skip create_server's startup unlink) gets it added.
        cursor = await self._conn.execute("PRAGMA table_info(processes)")
        names = {row[1] for row in await cursor.fetchall()}
        if "read_cursors" not in names:
            await self._conn.execute(
                "ALTER TABLE processes ADD COLUMN "
                "read_cursors TEXT NOT NULL DEFAULT '{}'"
            )
            await self._conn.commit()
```

Add the new method (after `read_records`):

```python
    async def read_new_records(
        self, proc_id: int, source_codes: list[int]
    ) -> tuple[list[dict], dict[int, int]]:
        """Read records newer than each source's cursor and advance cursors.

        Cursors are stored per source code as JSON in
        processes.read_cursors. Only the requested sources' cursors
        advance; the advance is one atomic UPDATE that applies a
        per-source MAX against the currently stored value, so concurrent
        read_new calls can never regress a cursor.

        Args:
            proc_id: Internal process ID.
            source_codes: Source codes to return ([1]=stdout, [2]=stderr).

        Returns:
            Tuple of (records, next_cursors): records in insertion
            (rowid) order with timestamp/source/content keys (same shape
            as read_records); next_cursors maps each requested source
            code to its cursor value after this read.
        """
        cursor = await self._conn.execute(
            "SELECT read_cursors FROM processes WHERE id = ?", (proc_id,)
        )
        row = await cursor.fetchone()
        cursors = json.loads(row[0]) if row and row[0] else {}
        cursors = {int(k): v for k, v in cursors.items()}

        where = " OR ".join(
            f"(source = {code} AND rowid > ?)" for code in source_codes
        )
        cursor = await self._conn.execute(
            f"SELECT rowid, timestamp, source, content FROM proc_{proc_id} "
            f"WHERE {where} ORDER BY rowid",
            tuple(cursors.get(code, 0) for code in source_codes),
        )
        rows = await cursor.fetchall()
        records = [
            {"timestamp": r[1], "source": r[2], "content": r[3]}
            for r in rows
        ]

        next_cursors: dict[int, int] = {}
        for code in source_codes:
            code_max = max(
                (r[0] for r in rows if r[2] == code), default=None
            )
            next_cursors[code] = (
                code_max if code_max is not None else cursors.get(code, 0)
            )

        # Atomic per-source MAX against the current stored JSON value.
        set_parts = []
        params: list = []
        for code in source_codes:
            path = f'$."{code}"'
            set_parts.append(
                f"'{path}', "
                f"MAX(COALESCE(json_extract(read_cursors, '{path}'), 0), ?)"
            )
            params.append(next_cursors[code])
        await self._conn.execute(
            "UPDATE processes SET read_cursors = json_set(read_cursors, "
            + ", ".join(set_parts)
            + ") WHERE id = ?",
            (*params, proc_id),
        )
        await self._conn.commit()

        return records, next_cursors
```

In `clear_records`, add the cursor reset:

```python
    async def clear_records(self, proc_id: int) -> None:
        """Delete all I/O records for a process (table remains)."""
        await self._conn.execute(f"DELETE FROM proc_{proc_id}")
        # SQLite reuses rowids starting at 1 once the table is emptied —
        # reset cursors so records written after the clear stay visible
        # to read_new.
        await self._conn.execute(
            "UPDATE processes SET read_cursors = '{}' WHERE id = ?",
            (proc_id,),
        )
        await self._conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_database.py -v`
Expected: PASS (all tests, existing + new).

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/database.py tests/test_database.py
git commit -m "feat(db): add per-source incremental read cursors (read_new_records)"
```

---

### Task 2: Manager — `read_new`

**Files:**
- Modify: `tests/test_manager.py` (new tests)
- Modify: `portal_mcp/manager.py` (new method)

**Interfaces:**
- Consumes: `db.read_new_records(proc_id, source_codes) -> (records, next_cursors)` from Task 1.
- Produces:
  - `await manager.read_new(proc_id: int, source: str = "both") -> list[dict]` — records with `timestamp`/`source`/`content` (same shape as `read`).
  - Raises `ValueError("Process {proc_id} not found")` for unknown ids; `ValueError("Unknown source '{source}'. Supported: stdout, stderr, both")` for bad source.

- [ ] **Step 1: Write the failing tests (append to `tests/test_manager.py`)**

Add a new test class after the `TestRead` class:

```python
class TestReadNew:
    async def test_first_read_returns_all_output(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('hello new')"],
        )
        await asyncio.sleep(0.3)
        records = await manager.read_new(result["id"])
        assert "hello new" in "".join(r["content"] for r in records)

    async def test_second_read_returns_only_new_output(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=[
                "-c",
                "import sys, time; "
                "print('first', flush=True); "
                "time.sleep(1); "
                "print('second', flush=True)",
            ],
        )
        await asyncio.sleep(0.3)
        records = await manager.read_new(result["id"])
        assert "first" in "".join(r["content"] for r in records)
        await asyncio.sleep(1.2)
        records = await manager.read_new(result["id"])
        contents = "".join(r["content"] for r in records)
        assert "second" in contents
        assert "first" not in contents

    async def test_narrow_reads_do_not_skip_other_source(self, manager):
        # stdout record is written BEFORE the stderr one; a single
        # shared cursor would advance past it on the stderr read.
        result = await manager.start(
            command=sys.executable,
            args=[
                "-c",
                "import sys, time; "
                "print('out1', flush=True); "
                "time.sleep(0.05); "
                "sys.stderr.write('err1\\n'); "
                "sys.stderr.flush(); "
                "time.sleep(30)",
            ],
        )
        await asyncio.sleep(0.5)
        records = await manager.read_new(result["id"], "stderr")
        assert "err1" in "".join(r["content"] for r in records)
        records = await manager.read_new(result["id"], "stdout")
        assert "out1" in "".join(r["content"] for r in records)

    async def test_both_advances_every_cursor(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=[
                "-c",
                "import sys, time; "
                "print('out1', flush=True); "
                "sys.stderr.write('err1\\n'); "
                "sys.stderr.flush(); "
                "time.sleep(30)",
            ],
        )
        await asyncio.sleep(0.5)
        await manager.read_new(result["id"], "both")
        assert await manager.read_new(result["id"], "stdout") == []
        assert await manager.read_new(result["id"], "stderr") == []

    async def test_process_read_does_not_move_cursors(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "print('data')"],
        )
        await asyncio.sleep(0.3)
        records = await manager.read(result["id"], "both", 3000, "ms")
        assert "data" in "".join(r["content"] for r in records)
        records = await manager.read_new(result["id"])
        assert "data" in "".join(r["content"] for r in records)

    async def test_clear_resets_cursors(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=[
                "-c",
                "import time; "
                "print('a', flush=True); "
                "time.sleep(1); "
                "print('b', flush=True); "
                "time.sleep(30)",
            ],
        )
        await asyncio.sleep(0.3)
        records = await manager.read_new(result["id"])
        assert "a" in "".join(r["content"] for r in records)
        await manager.clear(result["id"])
        await asyncio.sleep(1.2)
        records = await manager.read_new(result["id"])
        contents = "".join(r["content"] for r in records)
        assert "b" in contents
        assert "a" not in contents

    async def test_unknown_source_rejected(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(1)"],
        )
        with pytest.raises(ValueError, match="Unknown source"):
            await manager.read_new(result["id"], "stdin")

    async def test_nonexistent_process_raises(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.read_new(99999)

    async def test_read_new_resets_idle_timer(self, manager):
        result = await manager.start(
            command=sys.executable,
            args=["-c", "import time; time.sleep(2)"],
            timeout_ms=5000,
        )
        await asyncio.sleep(0.1)
        await manager.read_new(result["id"])
        proc_info = await manager.inspect(result["id"])
        assert proc_info["status"] == "running"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_manager.py -v`
Expected: FAIL — `AttributeError: 'ProcessManager' object has no attribute 'read_new'`.

- [ ] **Step 3: Implement in `portal_mcp/manager.py`**

Add the method after `read` (line ~215 in current file):

```python
    async def read_new(self, proc_id: int, source: str = "both") -> list[dict]:
        """Read records produced since the last read_new for a source.

        The server remembers the read position per process and per
        source; repeated calls return each record exactly once, in
        insertion order. Only the requested source's cursor advances.
        Resets the idle timer like read.

        Args:
            proc_id: Internal process ID.
            source: "stdout", "stderr", or "both".

        Returns:
            List of records with timestamp, source, content.
        """
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")

        source_map = {"stdout": [1], "stderr": [2], "both": [1, 2]}
        if source not in source_map:
            raise ValueError(
                f"Unknown source '{source}'. "
                f"Supported: stdout, stderr, both"
            )

        # Touch the process to reset idle timer
        await self._db.touch(proc_id)

        records, _ = await self._db.read_new_records(
            proc_id, source_map[source]
        )
        return records
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_manager.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/manager.py tests/test_manager.py
git commit -m "feat(manager): add read_new incremental read"
```

---

### Task 3: Server — `process_read_new` tool + instructions text

**Files:**
- Modify: `tests/test_server.py` (new tests)
- Modify: `portal_mcp/server.py` (tool registration, handler branch, instructions text)

**Interfaces:**
- Consumes: `manager.read_new(proc_id, source="both")` from Task 2.
- Produces: MCP tool `process_read_new` with `id` (required int) and `source` (optional enum `["stdout", "stderr", "both"]`, default `"both"`); "No new output." text when empty.

- [ ] **Step 1: Write the failing tests (append to `tests/test_server.py`)**

Add two methods to the existing `TestServerTools` class:

```python
    async def test_process_read_new(self, portal):
        result = await portal.start(
            command=sys.executable,
            args=["-c", "print('fresh output')"],
        )
        await asyncio.sleep(0.3)
        records = await portal.read_new(result["id"])
        assert "fresh output" in "".join(r["content"] for r in records)
        assert await portal.read_new(result["id"]) == []

    async def test_process_read_new_with_source(self, portal):
        result = await portal.start(
            command=sys.executable,
            args=[
                "-c",
                "import sys; "
                "sys.stdout.write('only-stdout\\n'); "
                "sys.stderr.write('only-stderr\\n')",
            ],
        )
        await asyncio.sleep(0.3)
        records = await portal.read_new(result["id"], "stderr")
        contents = "".join(r["content"] for r in records)
        assert "only-stderr" in contents
        assert "only-stdout" not in contents
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL — `AttributeError: 'ProcessManager' object has no attribute 'read_new'` (manager method from Task 2 not in this task's baseline) — proceed only after Task 2 is merged; the NEW assertion here is the source filter behavior.

- [ ] **Step 3: Implement in `portal_mcp/server.py`**

Register the tool in `handle_list_tools` — insert a new `types.Tool(...)` entry right after the `process_read` tool (after line 259, before `process_write`):

```python
                types.Tool(
                    name="process_read_new",
                    description=(
                        "Read new output from a process — records "
                        "produced since the last call to this tool for "
                        "the requested source. The server remembers the "
                        "read position per process and per source; there "
                        "is no time window, so repeated calls return "
                        "each record exactly once, in insertion order. "
                        "Only the requested source's cursor advances — "
                        "reading stdout never causes stderr records to "
                        "be skipped, and vice versa. Resets the process "
                        "idle timer, like process_read.\n"
                        "\n"
                        "process_read (time-window) does not affect this "
                        "tool's cursors; process_clear resets them (next "
                        "call returns everything since the clear). PTY "
                        "processes: stderr is merged into stdout and "
                        "records are arbitrary chunks, not lines — same "
                        "caveats as process_read. For full-screen TUIs "
                        "the record stream is garbled fragments — use "
                        "process_screen."
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
                                "enum": [
                                    "stdout", "stderr", "both"
                                ],
                                "description": (
                                    "Which stream to read new output "
                                    "from."
                                ),
                                "default": "both",
                            },
                        },
                        "required": ["id"],
                    },
                ),
```

Add the handler branch in `handle_call_tool` right after the `process_read` branch (after line 571):

```python
                elif name == "process_read_new":
                    records = await manager.read_new(
                        proc_id=arguments["id"],
                        source=arguments.get("source", "both"),
                    )
                    if not records:
                        return [
                            types.TextContent(
                                type="text",
                                text="No new output.",
                            )
                        ]
                    source_label = {0: "STDIN", 1: "STDOUT", 2: "STDERR"}
                    lines = []
                    for r in records:
                        label = source_label.get(
                            r["source"], f"SRC{r['source']}"
                        )
                        lines.append(
                            f"[{r['timestamp']}] [{label}] "
                            f"{r['content']}"
                        )
                    return [
                        types.TextContent(
                            type="text", text="".join(lines)
                        )
                    ]
```

Edit the MCP instructions text — the "Typical workflow" step 2 line:

```python
                        "2. `process_read`  — check what it printed so "
                        "far\n"
```

becomes:

```python
                        "2. `process_read`  — check what it printed so "
                        "far (or `process_read_new` for output since "
                        "the last read)\n"
```

And the hang-check sentence — exact `old_string`/`new_string` for the Edit:

```python
old_string = (
    "                        \"the default 1s window misses older output \"\n"
    "                        \"and looks identical to a hang). Treat as a \"\n"
)
new_string = (
    "                        \"the default 1s window misses older output \"\n"
    "                        \"and looks identical to a hang). \"\n"
    "                        \"process_read_new is NOT the tool for \"\n"
    "                        \"this — it only returns post-cursor \"\n"
    "                        \"output and hides older output. Treat as a \"\n"
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/server.py tests/test_server.py
git commit -m "feat(server): add process_read_new MCP tool"
```

---

### Task 4: Documentation — README.md / README_zh.md

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`

**Interfaces:**
- Consumes: the shipped tool surface from Task 3.

- [ ] **Step 1: Update `README.md`**

In the `## MCP Tools` table (line ~101), add a row after `process_read`:

```markdown
| `process_read_new` | Read output produced since the last read (per source, server-side cursor) |
```

In the `## Tools Reference` section, add a `### process_read_new` subsection after `### process_read` (after line 141):

```markdown
### process_read_new

Read output produced since the last call to this tool, for the requested
source. Resets the idle timer.

- `id` (required): Internal process ID
- `source` (optional): `stdout`, `stderr`, or `both` (default: `both`)

The server remembers the read position per process and per source, so
repeated calls return each record exactly once, in insertion order.
Only the requested source's cursor advances — reading `stdout` never
causes `stderr` records to be skipped and vice versa. `process_read`
(time-window) does not affect these cursors; `process_clear` resets
them.

Returns: List of records with `timestamp`, `source`, `content`
```

- [ ] **Step 2: Update `README_zh.md`**

In the `## 工具列表` table (line ~54), add a row after `process_read`:

```markdown
| `process_read_new` | 读取自上次读取以来新产生的输出（按源分游标，服务端自动记忆） |
```

In the `### process_read` subsection, after it (line ~92), add:

```markdown
### process_read_new

读取自上次调用该工具以来新产生的输出（按源分别记录游标），并重置空闲计时器。

- `id`（必填）：内部进程 ID
- `source`（可选）：`stdout`、`stderr` 或 `both`，默认 `both`

服务端按进程、按源分别记忆读取位置，重复调用时每条记录恰好返回一次，
按插入顺序返回。只有所请求源的游标会推进 —— 读取 `stdout` 不会导致
`stderr` 记录被跳过，反之亦然。`process_read`（时间窗读取）不影响这些
游标；`process_clear` 会重置它们。

返回：记录列表，每条包含 `timestamp`、`source`、`content`
```

- [ ] **Step 3: Verify renders (targeted diff)**

Run: `git diff --stat README.md README_zh.md`
Expected: both files modified.

- [ ] **Step 4: Commit**

```bash
git add README.md README_zh.md
git commit -m "docs(readme): document process_read_new tool"
```

---

### Task 5: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -v`
Expected: all tests PASS (existing + new).

- [ ] **Step 2: Working tree check**

Run: `git status --porcelain`
Expected: clean (all four commits landed).

---

## Self-Review Notes

- **Spec coverage:** DB column + migration (T1), atomic MAX advance (T1), per-source independence (T1+T2), clear reset (T1+T2), process_read independence (T2), source enum + stdin rejection (T2), tool registration + instructions text + empty message (T3), README EN+zh (T4), verification (T5).
- **Type consistency:** `read_new_records(proc_id: int, source_codes: list[int]) -> tuple[list[dict], dict[int, int]]` throughout; `manager.read_new(proc_id: int, source: str = "both") -> list[dict]`; handler passes `arguments.get("source", "both")`.
- **Known window:** exec of `-c` scripts uses `sys.executable` and `flush=True` (buffered-pipe determinism), matching existing suite style.
