"""SQLite database layer for Portal MCP server."""
import json
import time

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
                exit_code INTEGER,
                read_cursors TEXT NOT NULL DEFAULT '{}',
                read_gen INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Clean any leftover data from previous runs
        await self._conn.execute("DELETE FROM processes")
        await self._conn.commit()
        # Safety net: a stale DB file opened without the columns (callers
        # that skip create_server's startup unlink) gets them added.
        cursor = await self._conn.execute("PRAGMA table_info(processes)")
        names = {row[1] for row in await cursor.fetchall()}
        if "read_cursors" not in names:
            await self._conn.execute(
                "ALTER TABLE processes ADD COLUMN "
                "read_cursors TEXT NOT NULL DEFAULT '{}'"
            )
        if "read_gen" not in names:
            await self._conn.execute(
                "ALTER TABLE processes ADD COLUMN "
                "read_gen INTEGER NOT NULL DEFAULT 0"
            )
        # Drop leftover per-process record tables from previous runs —
        # their process rows were just deleted; the tables are stale.
        cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'proc\\_%' ESCAPE '\\'"
        )
        for row in await cursor.fetchall():
            await self._conn.execute(f'DROP TABLE IF EXISTS "{row[0]}"')
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
            f"CREATE TABLE IF NOT EXISTS proc_{proc_id} ("
            f"  timestamp INTEGER NOT NULL,"
            f"  source INTEGER NOT NULL,"
            f"  content TEXT NOT NULL"
            f")"
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

    async def read_new_records(
        self, proc_id: int, source_codes: list[int]
    ) -> tuple[list[dict], dict[int, int]]:
        """Read records newer than each source's cursor and advance cursors.

        Cursors are stored per source code as JSON in
        processes.read_cursors and advanced by one atomic UPDATE that
        applies a per-source MAX against the currently stored value, so
        concurrent read_new calls can never regress a cursor.
        processes.read_gen is a clear-generation counter bumped by
        clear_records; the advance UPDATE is conditioned on the read_gen
        this call snapshotted, so a process_clear landing between the
        snapshot and the advance makes the UPDATE a no-op (rowcount 0)
        and the method re-snapshots and retries from the new state. A
        stale snapshot can therefore never re-raise a reset cursor past
        the post-clear rowids (which restart at 1), and post-clear
        records are never skipped. Uses JSON1 (json_set/json_extract —
        bundled with CPython's sqlite3).

        Args:
            proc_id: Internal process ID.
            source_codes: Source codes to return and to advance
                ([1]=stdout, [2]=stderr). Must be non-empty.

        Returns:
            Tuple of (records, next_cursors): records in insertion
            (rowid) order with timestamp/source/content keys (same shape
            as read_records); next_cursors maps each requested source
            code to its cursor value after this read.

        Raises:
            ValueError: unknown process id, or empty source_codes.
            RuntimeError: a concurrent clear raced this call on every
                attempt; no safe advance was possible.
        """
        if not source_codes:
            raise ValueError("source_codes must not be empty")

        for _ in range(5):
            cursor = await self._conn.execute(
                "SELECT read_cursors, read_gen FROM processes "
                "WHERE id = ?",
                (proc_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError(f"Process {proc_id} not found")
            cursors = json.loads(row[0]) if row[0] else {}
            cursors = {int(k): v for k, v in cursors.items()}
            gen = row[1]

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

            # Atomic per-source MAX against the current stored JSON value,
            # conditioned on the snapshot generation: if the rowcount is 0
            # a clear landed between our snapshot and the advance, so no
            # cursor has been moved and the loop retries from the new state.
            set_parts = []
            params: list = []
            for code in source_codes:
                path = f'$."{code}"'
                set_parts.append(
                    f"'{path}', "
                    f"MAX(COALESCE(json_extract(read_cursors, '{path}'), 0), ?)"
                )
                params.append(next_cursors[code])
            cursor = await self._conn.execute(
                "UPDATE processes SET read_cursors = json_set(read_cursors, "
                + ", ".join(set_parts)
                + ") WHERE id = ? AND read_gen = ?",
                (*params, proc_id, gen),
            )
            await self._conn.commit()
            if cursor.rowcount > 0:
                return records, next_cursors
        raise RuntimeError(
            f"Process {proc_id}: could not advance read cursors after "
            "repeated concurrent clears"
        )

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
        # SQLite reuses rowids starting at 1 once the table is emptied —
        # reset cursors and bump the clear generation so records written
        # after the clear stay visible to read_new and a stale in-flight
        # read_new's advance no-ops (see read_new_records).
        await self._conn.execute(
            "UPDATE processes SET read_cursors = '{}', "
            "read_gen = read_gen + 1 WHERE id = ?",
            (proc_id,),
        )
        await self._conn.commit()

    async def cleanup_process(self, proc_id: int) -> None:
        """Drop the process I/O table and delete the process row."""
        await self._conn.execute(f"DROP TABLE IF EXISTS proc_{proc_id}")
        await self._conn.execute(
            "DELETE FROM processes WHERE id = ?", (proc_id,)
        )
        await self._conn.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
