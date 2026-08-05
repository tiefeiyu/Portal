"""Persistent program registry — which programs need a PTY.

Populated exclusively by the agent via program_record; the server
stores confirmed facts and does no pattern matching.
"""
import os
import time

import aiosqlite


def canonicalize_program(name: str) -> str:
    """Normalize an executable name for registry keying.

    Basename, lowercase, .exe stripped — applied identically on write
    and query so lookups never miss entries recorded under another
    spelling.
    """
    base = os.path.basename(name.replace("\\", "/"))
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.lower()


class Registry:
    """Persistent SQLite registry, one loop-bound aiosqlite connection."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Open the connection and ensure the schema exists.

        Never deletes the file — the registry is persistent across
        server restarts (unlike the session database).
        """
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS programs (
                program           TEXT PRIMARY KEY,
                needs_pty         INTEGER NOT NULL,
                notes             TEXT DEFAULT '',
                confirmed_count   INTEGER NOT NULL DEFAULT 1,
                last_confirmed_at INTEGER NOT NULL
            )
            """
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def query(self, program: str) -> dict:
        """Look up a program.

        Returns a hit dict {program, known: True, needs_pty, notes,
        confirmed_count, last_confirmed_at} or a miss dict
        {program, known: False} — a miss is a NORMAL result meaning
        'apply the decision table'.
        """
        canonical = canonicalize_program(program)
        cursor = await self._conn.execute(
            "SELECT program, needs_pty, notes, confirmed_count, "
            "last_confirmed_at FROM programs WHERE program = ?",
            (canonical,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"program": canonical, "known": False}
        return {
            "program": row["program"],
            "known": True,
            "needs_pty": bool(row["needs_pty"]),
            "notes": row["notes"],
            "confirmed_count": row["confirmed_count"],
            "last_confirmed_at": row["last_confirmed_at"],
        }

    async def record(
        self, program: str, needs_pty: bool, notes: str | None = None
    ) -> dict:
        """Upsert a program fact.

        needs_pty overwrites. confirmed_count increments ONLY when the
        new value matches the stored value; a flip (correction) resets
        it to 1 — corrections must not look like reinforcement. notes
        replace if provided, else retained.
        """
        canonical = canonicalize_program(program)
        existing = await self.query(canonical)
        now = time.time_ns()
        if existing["known"]:
            new_count = (
                existing["confirmed_count"] + 1
                if existing["needs_pty"] == needs_pty
                else 1
            )
            new_notes = notes if notes is not None else existing["notes"]
            await self._conn.execute(
                "UPDATE programs SET needs_pty = ?, notes = ?, "
                "confirmed_count = ?, last_confirmed_at = ? "
                "WHERE program = ?",
                (
                    1 if needs_pty else 0,
                    new_notes,
                    new_count,
                    now,
                    canonical,
                ),
            )
        else:
            await self._conn.execute(
                "INSERT INTO programs "
                "(program, needs_pty, notes, confirmed_count, "
                " last_confirmed_at) VALUES (?, ?, ?, 1, ?)",
                (canonical, 1 if needs_pty else 0, notes or "", now),
            )
        await self._conn.commit()
        return await self.query(canonical)
