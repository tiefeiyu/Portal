# PTY Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add virtual PTY support (ConPTY on Windows, ptyprocess on POSIX) to the Portal MCP server so agents can drive TTY-detecting programs and full-screen TUIs, plus a persistent agent-populated program registry that records which executables need a PTY.

**Architecture:** `process_start` gains an opt-in `pty` flag; `ProcessManager.start` branches between the existing pipe-mode `ManagedProcess` and a new `PtyProcess` that wraps a normalized `pty_backend` handle (pywinpty/ptyprocess) with a daemon reader thread feeding an asyncio consumer task, which drives two pyte screens (primary + alternate) for `process_screen` and writes chunk records to the session DB. A new `Registry` (persistent, platform app-data dir) backs the `program_query`/`program_record` tools.

**Tech Stack:** Python 3.11+, asyncio, aiosqlite, mcp SDK, pyte>=0.8.2, pywinpty>=3.0.5 (Windows), ptyprocess (POSIX).

## Global Constraints

- Language: Python 3.11+; async model: asyncio; MCP SDK: official `mcp` package.
- Dependencies to add (pyproject.toml): `pyte>=0.8.2`, `pywinpty>=3.0.5 ; sys_platform == 'win32'`, `ptyprocess ; sys_platform != 'win32'`.
- **Lazy imports rule:** `winpty`, `pyte`, `ptyprocess` may be imported ONLY inside functions (never at module top level of `portal_mcp/*.py`). The existing test suite must stay green on a machine where these packages are not installed.
- **PTY facts (verified against sources):** `spawn(argv, cwd, env, dimensions=(rows, cols))` — rows first on both backends; `setwinsize(rows, cols)`; pywinpty `read()` returns `str` and raises `EOFError` (but may return `''` while alive and forever after `terminate()`); ptyprocess `read()` returns `bytes` and also raises `EOFError`; both `kill()` methods REQUIRE a signal argument; on Windows `os.kill` maps every non-CTRL signal to `TerminateProcess` — there is NO graceful terminate, the only graceful interrupt is writing `\u0003`.
- pyte 0.8.x API: `pyte.Screen(columns, lines)` (columns first), `screen.display` (property, list of str), `screen.cursor.x/y`, `screen.resize(lines=..., columns=...)`, `pyte.Stream().feed(str)`. pyte does NOT implement the alternate screen buffer — PtyProcess must.
- Empty `read()` result is NOT EOF. EOF is `EOFError` or `not isalive()`.
- Test scripts must spawn `sys.executable` (never bare `"python"`); `-c` snippets must avoid double quotes; child scripts stay alive (`time.sleep(30)`) and teardown kills them — never print-and-exit.
- Registry file `<data_dir>/programs.db`: never deleted on startup; `PORTAL_DATA_DIR` env var read on EVERY call (no module-level caching).
- Commit messages in English, Conventional Commits format.
- The 10 existing pipe-mode tools, the session DB schema, `process.py`, `database.py`, `ansi.py` stay unchanged.

---

### Task 1: `paths.py` — platform data-dir resolution

**Files:**
- Create: `portal_mcp/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces: `default_data_dir() -> Path` (no args, per-call env read) and `registry_db_path() -> Path` (= `default_data_dir() / "programs.db"`). Used by Task 3 (server wiring).

- [ ] **Step 1: Write the failing tests**

`tests/test_paths.py`:

```python
"""Tests for platform data-dir resolution."""
import os
import sys
from pathlib import Path
import pytest
from portal_mcp.paths import default_data_dir, registry_db_path


def test_override_env_wins(monkeypatch, tmp_path):
    custom = tmp_path / "custom"
    monkeypatch.setenv("PORTAL_DATA_DIR", str(custom))
    assert default_data_dir() == custom


def test_windows_appdata(monkeypatch):
    monkeypatch.delenv("PORTAL_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    monkeypatch.setattr(sys, "platform", "win32")
    assert default_data_dir() == (
        Path(r"C:\Users\test\AppData\Roaming") / "portal-mcp"
    )


def test_posix_xdg(monkeypatch):
    monkeypatch.delenv("PORTAL_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg")
    assert default_data_dir() == Path("/xdg") / "portal-mcp"


def test_posix_default_home(monkeypatch, tmp_path):
    monkeypatch.delenv("PORTAL_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert default_data_dir() == tmp_path / ".local" / "share" / "portal-mcp"


def test_env_read_per_call_no_caching(monkeypatch, tmp_path):
    """PORTAL_DATA_DIR must be read every call (tests rely on monkeypatch)."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    monkeypatch.setenv("PORTAL_DATA_DIR", str(first))
    assert default_data_dir() == first
    monkeypatch.setenv("PORTAL_DATA_DIR", str(second))
    assert default_data_dir() == second


def test_registry_db_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_DATA_DIR", str(tmp_path))
    assert registry_db_path() == tmp_path / "programs.db"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'portal_mcp.paths'`

- [ ] **Step 3: Write the implementation**

`portal_mcp/paths.py`:

```python
"""Platform data-dir resolution for persistent Portal data."""
import os
import sys
from pathlib import Path


def default_data_dir() -> Path:
    """Return the machine-global data directory for Portal.

    Uses PORTAL_DATA_DIR if set (read fresh on every call — tests
    monkeypatch it), else the platform app-data dir.
    """
    override = os.environ.get("PORTAL_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home()
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(
            os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
        )
    return Path(base) / "portal-mcp"


def registry_db_path() -> Path:
    """Return the path of the persistent program registry database."""
    return default_data_dir() / "programs.db"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_paths.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/paths.py tests/test_paths.py
git commit -m "feat(paths): add platform data-dir resolution with PORTAL_DATA_DIR override"
```

---

### Task 2: `registry.py` — persistent program registry layer

**Files:**
- Create: `portal_mcp/registry.py`
- Create: `tests/conftest.py` (registry isolation fixture — needed before any test touches the registry)
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `portal_mcp.paths.registry_db_path()` (Task 1).
- Produces: `canonicalize_program(name: str) -> str`; `class Registry` with `__init__(db_path: str)`, `async open()`, `async close()`, `async query(program: str) -> dict`, `async record(program: str, needs_pty: bool, notes: str | None = None) -> dict`. Used by Task 3 (manager + server wiring) and Task 7 (tools).

- [ ] **Step 1: Write conftest.py (registry isolation)**

`tests/conftest.py` — autouse for ALL tests so `create_server` (Task 3) never touches the real `%APPDATA%\portal-mcp\`:

```python
"""Shared fixtures for the Portal test suite."""
import os
import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_registry_dir(tmp_path_factory):
    """Point PORTAL_DATA_DIR at a temp dir for the whole test session.

    The registry is machine-global and persistent; tests must never
    touch the real one.
    """
    registry_dir = tmp_path_factory.mktemp("portal-registry")
    os.environ["PORTAL_DATA_DIR"] = str(registry_dir)
    yield
    os.environ.pop("PORTAL_DATA_DIR", None)
```

- [ ] **Step 2: Write the failing tests**

`tests/test_registry.py`:

```python
"""Tests for the persistent program registry."""
import os
import time
import tempfile
import pytest
from portal_mcp.registry import Registry, canonicalize_program


@pytest.fixture
async def registry(tmp_path):
    reg = Registry(str(tmp_path / "programs.db"))
    await reg.open()
    yield reg
    await reg.close()


class TestCanonicalize:
    def test_basename_lowercase(self):
        assert canonicalize_program("SSH.EXE") == "ssh"
        assert canonicalize_program("C:\\Tools\\Git\\bin\\GIT.EXE") == "git"
        assert canonicalize_program("python") == "python"

    def test_strips_exe(self):
        assert canonicalize_program("vim.exe") == "vim"


class TestQuery:
    async def test_miss_shape(self, registry):
        assert await registry.query("ssh") == {
            "program": "ssh", "known": False
        }

    async def test_hit_shape(self, registry):
        await registry.record("ssh", True, notes="interactive")
        result = await registry.query("ssh")
        assert result["known"] is True
        assert result["needs_pty"] is True
        assert result["notes"] == "interactive"
        assert result["confirmed_count"] == 1

    async def test_query_normalizes(self, registry):
        await registry.record("SSH.EXE", True)
        assert (await registry.query("ssh"))["known"] is True


class TestRecord:
    async def test_insert(self, registry):
        result = await registry.record("gdb", True)
        assert result["confirmed_count"] == 1
        assert result["needs_pty"] is True
        assert result["last_confirmed_at"] <= time.time_ns()

    async def test_reinforcement_increments(self, registry):
        await registry.record("gdb", True)
        await registry.record("gdb", True)
        assert (await registry.query("gdb"))["confirmed_count"] == 2

    async def test_flip_resets_count(self, registry):
        await registry.record("python", True)
        await registry.record("python", True)
        await registry.record("python", False)
        result = await registry.query("python")
        assert result["needs_pty"] is False
        assert result["confirmed_count"] == 1

    async def test_notes_replace_or_retain(self, registry):
        await registry.record("python", True, notes="first")
        await registry.record("python", True, notes="second")
        assert (await registry.query("python"))["notes"] == "second"
        await registry.record("python", True)  # no notes -> retain
        assert (await registry.query("python"))["notes"] == "second"


class TestPersistence:
    async def test_survives_restart(self, tmp_path):
        path = str(tmp_path / "programs.db")
        reg1 = Registry(path)
        await reg1.open()
        await reg1.record("ssh", True, notes="interactive")
        await reg1.close()

        reg2 = Registry(path)
        await reg2.open()
        result = await reg2.query("ssh")
        assert result["known"] is True
        assert result["needs_pty"] is True
        await reg2.close()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'portal_mcp.registry'`

- [ ] **Step 4: Write the implementation**

`portal_mcp/registry.py`:

```python
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

        Returns a hit dict {program, needs_pty, notes, confirmed_count}
        or a miss dict {program, known: False} — a miss is a NORMAL
        result meaning 'apply the decision table'.
        """
        canonical = canonicalize_program(program)
        cursor = await self._conn.execute(
            "SELECT program, needs_pty, notes, confirmed_count "
            "FROM programs WHERE program = ?",
            (canonical,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {"program": canonical, "known": False}
        return {
            "program": row["program"],
            "needs_pty": bool(row["needs_pty"]),
            "notes": row["notes"],
            "confirmed_count": row["confirmed_count"],
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_registry.py -v`
Expected: PASS (11 tests)

- [ ] **Step 6: Commit**

```bash
git add portal_mcp/registry.py tests/conftest.py tests/test_registry.py
git commit -m "feat(registry): add persistent program registry with flip-reset confirmation"
```

---

### Task 3: Manager + server registry wiring, `program_query`/`program_record` tools

**Files:**
- Modify: `portal_mcp/manager.py` (constructor, query_program/record_program methods, shutdown closes registry)
- Modify: `portal_mcp/server.py` (create_server opens registry; two new tools; main cleanup)

**Interfaces:**
- Consumes: `Registry` from Task 2.
- Produces: `ProcessManager(db, registry: Registry | None = None)`; `async query_program(program) -> dict`; `async record_program(program, needs_pty, notes=None) -> dict`; `create_server(db_path=None)` now wires a registry (return signature unchanged). Used by Task 7 (tool descriptions/handlers) and Task 8 (docs).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_manager.py`:

```python
from portal_mcp.registry import Registry


class TestRegistryWiring:
    async def test_query_roundtrip(self, tmp_path, manager):
        registry = Registry(str(tmp_path / "programs.db"))
        await registry.open()
        mgr = ProcessManager(manager._db, registry=registry)
        result = await mgr.query_program("ssh")
        assert result == {"program": "ssh", "known": False}
        await mgr.record_program("ssh", True, notes="interactive")
        assert (await mgr.query_program("ssh"))["needs_pty"] is True
        await registry.close()

    async def test_without_registry_raises(self, manager):
        with pytest.raises(ValueError, match="registry"):
            await manager.query_program("ssh")
        with pytest.raises(ValueError, match="registry"):
            await manager.record_program("ssh", True)
```

Append to `tests/test_server.py`:

```python
    async def test_registry_wired_through_create_server(self, portal):
        result = await portal.record_program("gdb", True)
        assert result["known"] is True
        assert result["needs_pty"] is True
        assert (await portal.query_program("GDB.EXE"))["needs_pty"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_manager.py::TestRegistryWiring tests/test_server.py::TestServerTools::test_registry_wired_through_create_server -v`
Expected: FAIL with `AttributeError: 'ProcessManager' object has no attribute 'query_program'`

- [ ] **Step 3: Write the implementation**

`portal_mcp/manager.py` — add import and constructor/methods:

```python
from portal_mcp.registry import Registry
```

Constructor (keep existing `self._db = db`, `self._processes = {}`, `self._monitor_task = None`):

```python
    def __init__(self, db: Database, registry: Registry | None = None):
        self._db = db
        self._registry = registry
        self._processes: dict[int, ManagedProcess] = {}
        self._monitor_task: asyncio.Task | None = None
```

Add methods (place after `kill_all`):

```python
    async def query_program(self, program: str) -> dict:
        """Look up a program in the registry (miss is a normal result)."""
        if self._registry is None:
            raise ValueError("Program registry not configured")
        return await self._registry.query(program)

    async def record_program(
        self, program: str, needs_pty: bool, notes: str | None = None
    ) -> dict:
        """Record a confirmed program fact in the registry."""
        if self._registry is None:
            raise ValueError("Program registry not configured")
        return await self._registry.record(program, needs_pty, notes)
```

Modify `shutdown()` — close the registry after killing processes (registry is optional; existing tests pass `None`):

```python
        self._processes.clear()
        if self._registry is not None:
            await self._registry.close()
```

`portal_mcp/server.py` — modify `create_server` (open registry via paths; keep signature and return tuple unchanged):

```python
from portal_mcp.registry import Registry
from portal_mcp.paths import registry_db_path
```

In `create_server`, after `manager = ProcessManager(db)`:

```python
    registry = Registry(str(registry_db_path()))
    await registry.open()

    manager = ProcessManager(db, registry=registry)
    await manager.start_monitor()
```

Register the two tools in `handle_list_tools` (after `process_cleanup` entry):

```python
                types.Tool(
                    name="program_query",
                    description=(
                        "Look up whether a program needs a PTY in the "
                        "persistent program registry. Call BEFORE "
                        "process_start. A miss is a normal result, not "
                        "an error — it means apply the decision rules "
                        "in the instructions. confirmed_count >= 2 "
                        "means settled; a single confirmation is a "
                        "hint — re-verify on first use."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "program": {
                                "type": "string",
                                "description": (
                                    "Executable name, e.g. 'ssh'."
                                ),
                            },
                        },
                        "required": ["program"],
                    },
                ),
                types.Tool(
                    name="program_record",
                    description=(
                        "Record a confirmed program fact in the "
                        "persistent registry after observing its "
                        "behavior: needs_pty true if it required a "
                        "terminal (TTY error, hang in pipe mode, or "
                        "TUI rendering with pty:true), false if it ran "
                        "fine without one. Record every first-encounter "
                        "conclusion, including negatives. notes should "
                        "carry flag-specific caveats (e.g. 'docker run "
                        "-it only, not docker build')."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "program": {
                                "type": "string",
                                "description": "Executable name.",
                            },
                            "needs_pty": {
                                "type": "boolean",
                                "description": (
                                    "Whether the program needs a PTY."
                                ),
                            },
                            "notes": {
                                "type": "string",
                                "description": (
                                    "Optional caveats or context."
                                ),
                            },
                        },
                        "required": ["program", "needs_pty"],
                    },
                ),
```

Add handlers in `handle_call_tool` (after the `process_cleanup` branch):

```python
                elif name == "program_query":
                    result = await manager.query_program(
                        arguments["program"]
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"{result['program']}: "
                                + (
                                    f"needs_pty={result['needs_pty']}, "
                                    f"confirmed x{result['confirmed_count']}"
                                    + (
                                        f" — {result['notes']}"
                                        if result.get("notes")
                                        else ""
                                    )
                                    if result.get("known")
                                    else "not in registry — apply decision rules"
                                )
                            ),
                        )
                    ]

                elif name == "program_record":
                    result = await manager.record_program(
                        program=arguments["program"],
                        needs_pty=arguments["needs_pty"],
                        notes=arguments.get("notes"),
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Recorded {result['program']}: "
                                f"needs_pty={result['needs_pty']} "
                                f"(confirmed x{result['confirmed_count']})."
                            ),
                        )
                    ]
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `pytest tests/ -v`
Expected: PASS — existing suite green (registry isolated by conftest autouse fixture) + 3 new tests.

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/manager.py portal_mcp/server.py tests/test_manager.py tests/test_server.py
git commit -m "feat(registry): wire registry through manager and add program_query/program_record tools"
```

---

### Task 4: Dependencies + `pty_backend.py` — cross-platform spawn dispatch

**Files:**
- Modify: `pyproject.toml`
- Create: `portal_mcp/pty_backend.py`
- Test: `tests/test_pty.py` (first tests — no winpty needed yet)

**Interfaces:**
- Consumes: nothing (stdlib; winpty/pyte/ptyprocess imported lazily INSIDE functions).
- Produces: `normalize_env(env: dict | None) -> dict` (os.environ copy + caller env + `TERM=xterm-256color` injection if absent); `_decode(data: str | bytes) -> str`; `class PtyHandle` protocol (duck-typed): `pid: int`, `read() -> str` (raises EOFError), `write(s: str)`, `setwinsize(rows, cols)`, `kill(sig)`, `terminate()`, `isalive() -> bool`, `close()`, optional `exitstatus` attribute; `spawn(command, args, cwd, env, rows=24, cols=80) -> PtyHandle`. Used by Task 5 (`PtyProcess.create`).

- [ ] **Step 1: Add dependencies and install them**

`pyproject.toml` — replace the dependencies block:

```toml
dependencies = [
    "mcp>=1.0.0",
    "aiosqlite>=0.20.0",
    "pyte>=0.8.2",
    "pywinpty>=3.0.5 ; sys_platform == 'win32'",
    "ptyprocess ; sys_platform != 'win32'",
]
```

Install for local development (Windows):

```bash
pip install pywinpty>=3.0.5 pyte>=0.8.2
```

- [ ] **Step 2: Write the failing tests (fake backend, no winpty needed)**

Append to `tests/test_pty.py` (new file — start it with the non-PTY tests):

```python
"""Tests for PTY support."""
import pytest
from portal_mcp.pty_backend import _decode, normalize_env


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
        assert _decode(b"\xff\xfe") == "\ufffd\ufffd"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_pty.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'portal_mcp.pty_backend'`

- [ ] **Step 4: Write the implementation**

`portal_mcp/pty_backend.py`:

```python
"""Cross-platform PTY spawn dispatch.

Windows -> pywinpty (ConPTY), POSIX -> ptyprocess. The two libraries
mirror each other's API by design; this module normalizes the
remaining differences behind a common PtyHandle duck-type.

IMPORTANT: winpty / ptyprocess / pyte are imported lazily INSIDE
functions only — the module must import cleanly on machines where
these packages are not installed (the pipe-mode suite depends on it).
"""
import os
import sys


def normalize_env(env: dict | None) -> dict:
    """Merge caller env into os.environ copy, inject TERM if absent.

    pywinpty resolves argv[0] against the provided env's PATH, so a
    partial env would break command resolution — always merge.
    """
    merged = os.environ.copy()
    if env:
        merged.update(env)
    merged.setdefault("TERM", "xterm-256color")
    return merged


def _decode(data: str | bytes) -> str:
    """Normalize a read result to str (pywinpty returns str, ptyprocess bytes)."""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def spawn(
    command: str,
    args: list[str],
    cwd: str | None,
    env: dict | None,
    rows: int = 24,
    cols: int = 80,
):
    """Spawn a program on a PTY and return a normalized PtyHandle.

    Raises the backend's exception on spawn failure (caller cleans up).
    """
    merged_env = normalize_env(env)
    argv = [command, *args]
    if sys.platform == "win32":
        return _WinPtyHandle.spawn(argv, cwd, merged_env, rows, cols)
    return _PosixPtyHandle.spawn(argv, cwd, merged_env, rows, cols)


class _WinPtyHandle:
    """Normalized wrapper over pywinpty.PtyProcess (ConPTY)."""

    @classmethod
    def spawn(cls, argv, cwd, env, rows, cols):
        from winpty import PtyProcess  # lazy import

        return cls(PtyProcess.spawn(
            argv, cwd=cwd, env=env, dimensions=(rows, cols)
        ))

    def __init__(self, pty):
        self._pty = pty

    @property
    def pid(self) -> int:
        return self._pty.pid

    @property
    def exitstatus(self):
        return None  # ConPTY exposes no exit code

    def read(self) -> str:
        # Returns str; raises EOFError at EOF. NOTE: '' is NOT EOF.
        return self._pty.read()

    def write(self, s: str) -> None:
        self._pty.write(s)

    def setwinsize(self, rows: int, cols: int) -> None:
        self._pty.setwinsize(rows, cols)

    def kill(self, sig: int) -> None:
        self._pty.kill(sig)

    def terminate(self) -> None:
        self._pty.terminate()

    def isalive(self) -> bool:
        return self._pty.isalive()

    def close(self) -> None:
        # Closes the socket fd, unblocking a read() stuck in the
        # internal daemon reader thread.
        try:
            self._pty.close()
        except Exception:
            pass


class _PosixPtyHandle:
    """Normalized wrapper over ptyprocess.PtyProcess."""

    @classmethod
    def spawn(cls, argv, cwd, env, rows, cols):
        from ptyprocess import PtyProcess  # lazy import

        return cls(PtyProcess.spawn(
            argv, cwd=cwd, env=env, dimensions=(rows, cols)
        ))

    def __init__(self, pty):
        self._pty = pty

    @property
    def pid(self) -> int:
        return self._pty.pid

    @property
    def exitstatus(self):
        return getattr(self._pty, "exitstatus", None)

    def read(self) -> str:
        # Returns bytes; raises EOFError at EOF on both EOF paths.
        return _decode(self._pty.read())

    def write(self, s: str) -> None:
        self._pty.write(s.encode("utf-8"))

    def setwinsize(self, rows: int, cols: int) -> None:
        self._pty.setwinsize(rows, cols)

    def kill(self, sig: int) -> None:
        self._pty.kill(sig)

    def terminate(self) -> None:
        self._pty.terminate()

    def isalive(self) -> bool:
        return self._pty.isalive()

    def close(self) -> None:
        try:
            self._pty.close()
        except Exception:
            pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_pty.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Verify the existing suite still collects without the new deps**

Run: `pytest tests/test_manager.py tests/test_server.py -v`
Expected: PASS — proves the lazy-import rule holds (no module-level import of winpty/pyte anywhere).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml portal_mcp/pty_backend.py tests/test_pty.py
git commit -m "feat(pty): add cross-platform pty_backend with lazy imports and env normalization"
```

---

### Task 5: `PtyProcess` — PTY process wrapper with pyte emulation

**Files:**
- Create: `portal_mcp/pty_process.py`
- Test: `tests/test_pty.py` (append)

**Interfaces:**
- Consumes: `portal_mcp.pty_backend.spawn` / `_decode` (Task 4); `Database`; `pyte` (lazy import inside the emulator factory).
- Produces: `class PtyProcess` with `is_pty = True` class attribute and interface parity with `ManagedProcess`: `id`, `os_pid`, `status` properties; `@classmethod async create(db, proc_id, command, args, cwd, env, timeout_ms=0, cols=80, rows=24) -> PtyProcess`; `async start_background_readers(db)` (no-op — thread+consumer start in create); `async write_stdin(db, content)`; `async send_signal(sig)`; `async kill()`; `async terminate()`; `async wait_exit() -> int | None`; `async resize(rows, cols)`; `screen() -> dict`; internal `_reader_loop()` (thread) and `_consume(db)` (loop task). Used by Task 6 (manager branch).

- [ ] **Step 1: Write the failing tests — fake-handle unit tests first**

Append to `tests/test_pty.py`:

```python
import asyncio
import os
import signal
import sys
import time
import tempfile
from portal_mcp.database import Database
from portal_mcp.pty_process import PtyProcess


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
        assert handle.killed == [signal.SIGKILL]
        assert handle.closed is True
        assert mp.status == "killed"

    async def test_terminate(self, pty_db):
        pid = await _make_pid(pty_db, command="fake")
        handle = FakeHandle()
        mp = PtyProcess(pid, handle, 0)
        await mp.start(pty_db)
        await mp.terminate()
        assert handle.terminated is True

    async def test_send_signal_mapping(self, pty_db):
        pid = await _make_pid(pty_db, command="fake")
        handle = FakeHandle()
        mp = PtyProcess(pid, handle, 0)
        await mp.start(pty_db)
        await mp.send_signal("SIGTERM")
        assert handle.terminated is True
        handle.alive = True  # reuse for next signal
        await mp.send_signal("CTRL_C_EVENT")
        assert handle.written == ["\x03"]
        handle.alive = True
        await mp.send_signal("SIGKILL")  # must come last — kill sets status
        assert handle.killed == [signal.SIGKILL]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pty.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'portal_mcp.pty_process'`

- [ ] **Step 3: Write the PtyProcess implementation (fake-handle tests pass; real-ConPTY tests come in Step 5)**

`portal_mcp/pty_process.py`:

```python
"""PtyProcess — PTY-mode twin of ManagedProcess with pyte emulation."""
import asyncio
import re
import signal
import threading
import time

from portal_mcp import pty_backend
from portal_mcp.ansi import strip_ansi
from portal_mcp.database import Database

# ESC [ ? 1047h / 1048h / 1049h  and the matching l (off) codes
_ALT_SCREEN_PATTERN = re.compile(r"\x1b\[\?10(47|48|49)([hl])")


def _make_screen(rows: int, cols: int):
    """Lazy-import pyte and build a (primary, alternate) screen pair."""
    import pyte  # lazy import

    primary = pyte.Screen(cols, rows)
    alternate = pyte.Screen(cols, rows)
    streams = (pyte.Stream(primary), pyte.Stream(alternate))
    return primary, alternate, streams


class PtyProcess:
    """Wraps a pty_backend.PtyHandle with a reader thread + pyte emulator.

    Interface parity with ManagedProcess so ProcessManager can drive
    both with the same lifecycle code (see manager.start).
    """

    is_pty = True

    def __init__(
        self,
        proc_id: int,
        handle,
        timeout_ms: int,
        cols: int = 80,
        rows: int = 24,
    ):
        self._id = proc_id
        self._handle = handle
        self._timeout_ms = timeout_ms
        self._cols = cols
        self._rows = rows
        self._exit_code: int | None = None
        self._killed = False
        self._status = "running"
        self._exited = asyncio.Event()
        self._stop = threading.Event()
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._consumer_task: asyncio.Task | None = None
        self._primary = None
        self._alternate = None
        self._streams = None
        self._active = "primary"

    @classmethod
    async def create(
        cls,
        db: Database,
        proc_id: int,
        command: str,
        args: list[str],
        cwd: str | None,
        env: dict | None,
        timeout_ms: int = 0,
        cols: int = 80,
        rows: int = 24,
    ) -> "PtyProcess":
        """Spawn via pty_backend and start the reader pipeline."""
        handle = pty_backend.spawn(
            command, args, cwd, env, rows=rows, cols=cols
        )
        mp = cls(proc_id, handle, timeout_ms, cols, rows)
        await mp.start(db)
        return mp

    @property
    def id(self) -> int:
        return self._id

    @property
    def os_pid(self) -> int:
        return self._handle.pid

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def status(self) -> str:
        if self._killed:
            return "killed"
        return self._status

    async def start(self, db: Database) -> None:
        """Start the reader thread and the loop-side consumer task."""
        self._primary, self._alternate, self._streams = _make_screen(
            self._rows, self._cols
        )
        self._queue = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._reader_loop, daemon=True, name=f"pty-reader-{self._id}"
        )
        self._thread.start()
        self._consumer_task = asyncio.create_task(self._consume(db))

    async def start_background_readers(self, db: Database) -> None:
        """No-op — the PTY reader starts in create()/start()."""

    def _reader_loop(self) -> None:
        """Blocking read loop in a daemon thread; pushes to asyncio queue.

        Exit rules: EOFError, any exception, or !isalive() — '' is
        never treated as EOF (pywinpty returns '' on empty reads and
        forever after terminate()).
        """
        while not self._stop.is_set():
            try:
                data = self._handle.read()
            except EOFError:
                self._put(("eof", None))
                break
            except Exception:
                self._put(("eof", None))
                break
            if data:
                self._put(("chunk", data))
            if not self._handle.isalive():
                self._put(("eof", None))
                break

    def _put(self, item) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            pass  # loop closed during shutdown/teardown

    async def _consume(self, db: Database) -> None:
        """Loop-side: feed pyte, scan alt-screen, strip, insert records."""
        try:
            while True:
                kind, data = await self._queue.get()
                if kind == "eof":
                    self._status = "exited"
                    self._exited.set()
                    return
                self._feed_screen(data)
                cleaned = strip_ansi(data).replace("\r", "")
                if cleaned:
                    await db.insert_record(
                        self._id, time.time_ns(), 1, cleaned
                    )
        except asyncio.CancelledError:
            pass

    def _feed_screen(self, chunk: str) -> None:
        """Feed raw VT bytes to the active screen, handling alt-screen."""
        if self._streams is None:
            return
        offset = 0
        for match in _ALT_SCREEN_PATTERN.finditer(chunk):
            before = chunk[offset:match.start()]
            if before:
                self._streams[0 if self._active == "primary" else 1].feed(before)
            code, mode = match.group(1), match.group(2)
            if mode == "h":
                if code == "1049":
                    self._alternate.reset()
                self._active = "alternate"
            else:
                self._active = "primary"
            offset = match.end()
        rest = chunk[offset:]
        if rest:
            self._streams[0 if self._active == "primary" else 1].feed(rest)

    async def write_stdin(self, db: Database, content: str) -> None:
        if self.status != "running":
            raise RuntimeError(
                f"Process {self._id} is not running (status={self.status})"
            )
        # write() is a blocking C call — chunk large payloads so no
        # single call stalls the event loop for long (spec: cap writes).
        for i in range(0, len(content), 4096):
            self._handle.write(content[i : i + 4096])
        await db.insert_record(self._id, time.time_ns(), 0, content)

    async def send_signal(self, sig) -> None:
        """Resolve a signal name-or-int to a PTY-appropriate action.

        SIGTERM -> terminate(), SIGKILL -> kill(),
        CTRL_C_EVENT -> write '\\x03' (the reliable Ctrl+C under ConPTY).
        """
        if self.status != "running":
            raise RuntimeError(
                f"Process {self._id} is not running (status={self.status})"
            )
        if isinstance(sig, str):
            if sig == "SIGTERM":
                await self.terminate()
                return
            if sig == "SIGKILL":
                await self.kill()
                return
            if sig == "CTRL_C_EVENT":
                self._handle.write("\x03")
                return
            sig = getattr(signal, sig, None)
            if sig is None:
                raise ValueError(f"Unknown signal: {sig}")
        if sig == signal.SIGTERM:
            await self.terminate()
        elif sig == signal.SIGKILL:
            await self.kill()
        else:
            raise ValueError(
                f"Unsupported signal for PTY process: {sig!r}"
            )

    async def kill(self) -> None:
        if self._killed:
            return
        self._killed = True
        self._stop.set()
        try:
            self._handle.kill(signal.SIGKILL)
        except Exception:
            pass
        self._handle.close()  # unblocks a stuck blocking read()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._consumer_task:
            self._consumer_task.cancel()
        # Wake wait_exit() so the exit-monitor task never hangs on a
        # killed process (consumer cancellation alone would leave it
        # waiting on _exited forever).
        self._exited.set()

    async def terminate(self) -> None:
        self._stop.set()
        try:
            self._handle.terminate()
        except Exception:
            pass
        self._handle.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    async def wait_exit(self) -> int | None:
        """Wait for the reader to signal EOF; may return None.

        ConPTY exposes no exit code — Windows PTY processes have
        exit_code=None (documented; agents must not rely on it).
        """
        await self._exited.wait()
        return self._handle.exitstatus

    async def resize(self, rows: int, cols: int) -> None:
        self._handle.setwinsize(rows, cols)
        self._rows, self._cols = rows, cols
        if self._primary is not None:
            self._primary.resize(lines=rows, columns=cols)
            self._alternate.resize(lines=rows, columns=cols)

    def screen(self) -> dict:
        """Snapshot of the active pyte screen."""
        active = (
            self._alternate if self._active == "alternate" else self._primary
        )
        if active is None:
            return {
                "buffer": self._active, "rows": self._rows,
                "cols": self._cols, "cursor_x": 0, "cursor_y": 0,
                "content": "",
            }
        return {
            "buffer": self._active,
            "rows": self._rows,
            "cols": self._cols,
            "cursor_x": active.cursor.x,
            "cursor_y": active.cursor.y,
            "content": "\n".join(active.display),
        }
```

- [ ] **Step 4: Run the fake-handle tests to verify they pass**

Run: `pytest tests/test_pty.py -v`
Expected: PASS — all fake-handle unit tests green (no winpty/pyte installed needed; pyte IS needed by `start` → the fake tests DO import pyte. If `pyte` is not installed, `_make_screen` raises — so these tests need pyte installed. Run `pip show pyte` first; Task 4 installed it.)

Note: `test_empty_read_is_not_eof` requires the `pid` variable used by `EmptyHandle` fixture — fix the leftover `handle = FakeHandle()` line by removing it (the fixture assigns `mp = PtyProcess(pid, EmptyHandle(chunks=()), 0)` directly).

- [ ] **Step 5: Write the real-ConPTY tests**

Append to `tests/test_pty.py` (all of these run on Windows with pywinpty installed):

```python
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
        assert mp._handle.exitstatus is None or mp._handle.exitstatus == 0

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
        await wait_records(pty_db, pid, "> ")
        await mp.write_stdin(pty_db, "\x03")
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
             "sys.stdout.write('\\x1b[H'); "
             "sys.stdout.write('XX\\r\\nYY'); "
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
            ["-c", "import sys; data = sys.stdin.read(65536); "
             "sys.stdout.write('got %d' % len(data)); "
             "sys.stdout.flush(); import time; time.sleep(30)"],
            None, None,
        )
        await asyncio.sleep(0.5)
        payload = "x" * 4096
        await asyncio.wait_for(
            mp.write_stdin(pty_db, payload), timeout=5
        )
        await wait_records(pty_db, pid, "got 4096")
        await mp.kill()
```

- [ ] **Step 6: Run the full PTY suite to verify it passes**

Run: `pytest tests/test_pty.py -v`
Expected: PASS — all unit + real-ConPTY tests green. If a test is flaky, fix by making the wait deterministic (wait_records / wait_status), never by adding bare sleeps.

- [ ] **Step 7: Commit**

```bash
git add portal_mcp/pty_process.py tests/test_pty.py
git commit -m "feat(pty): add PtyProcess with reader thread, pyte emulation and alt-screen swap"
```

---

### Task 6: Manager PTY branch — start/screen/signal integration

**Files:**
- Modify: `portal_mcp/manager.py` (start pty param, screen, send_signal passthrough, _watch_exit handles None exit code)
- Test: `tests/test_pty.py` (append manager-level tests) and `tests/test_manager.py` (pipe-mode regression — should be untouched and green)

**Interfaces:**
- Consumes: `PtyProcess` from Task 5 (imported LAZILY inside `start`).
- Produces: `ProcessManager.start(command, args=None, cwd=None, env=None, timeout_ms=0, pty=False) -> dict`; `async screen(proc_id, cols=None, rows=None) -> dict`; `send_signal` passes raw signal names through to PTY processes. Used by Task 7 (server tools).

- [ ] **Step 1: Write the failing manager-level tests**

Append to `tests/test_pty.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pty.py::TestManagerPty -v`
Expected: FAIL with `TypeError: ProcessManager.start() got an unexpected keyword argument 'pty'` (and `AttributeError: 'ProcessManager' object has no attribute 'screen'`)

- [ ] **Step 3: Implement the manager PTY branch**

`portal_mcp/manager.py`:

Modify `start` — add the `pty` parameter and branch (keep the entire existing pipe path intact):

```python
    async def start(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_ms: int = 0,
        pty: bool = False,
    ) -> dict:
        """Start a new subprocess.

        Args:
            pty: When True, spawn on a virtual PTY (ConPTY on Windows)
                instead of pipes — for programs that check isatty()
                or render full-screen TUIs.

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

        if pty:
            from portal_mcp.pty_process import PtyProcess  # lazy import

            try:
                mp = await PtyProcess.create(
                    self._db, proc_id, command, args, cwd, env,
                    timeout_ms=timeout_ms,
                )
            except Exception:
                await self._db.cleanup_process(proc_id)
                raise
            await self._db.update_os_pid(proc_id, mp.os_pid)
        else:
            # Merge env with current env if provided
            process_env = None
            if env:
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
            "os_pid": mp.os_pid,
            "status": "running",
        }
```

Add `screen` method (place after `read`):

```python
    async def screen(
        self, proc_id: int, cols: int | None = None, rows: int | None = None
    ) -> dict:
        """Snapshot the live screen of a PTY process.

        Pure snapshot when cols/rows are omitted (no resize side
        effect). Passing cols/rows resizes the live PTY first.
        Touches the idle timer like process_read.
        """
        proc = await self._db.get_process(proc_id)
        if proc is None:
            raise ValueError(f"Process {proc_id} not found")

        mp = self._processes.get(proc_id)
        if mp is None:
            raise ValueError(f"Process {proc_id} not found in manager")
        if not getattr(mp, "is_pty", False):
            raise ValueError(f"Process {proc_id} is not a PTY process")

        if cols is not None or rows is not None:
            new_rows = rows if rows is not None else mp.rows
            new_cols = cols if cols is not None else mp.cols
            await mp.resize(new_rows, new_cols)

        await self._db.touch(proc_id)
        result = mp.screen()
        result["id"] = proc_id
        result["status"] = mp.status
        return result
```

Modify `send_signal` — pass raw names through for PTY processes (the existing name→int resolution stays pipe-only):

```python
        mp = self._processes.get(proc_id)
        if mp is None:
            raise ValueError(f"Process {proc_id} not found in manager")

        # PTY processes resolve signal names themselves (their mapping
        # differs: SIGTERM -> terminate, SIGKILL -> kill, CTRL_C_EVENT
        # -> write \x03).
        if getattr(mp, "is_pty", False):
            await mp.send_signal(sig)
            return {"id": proc_id, "signal_sent": sig}

        # Convert string signal name to int (pipe mode)
        if isinstance(sig, str):
            sig_num = getattr(signal, sig, None)
            if sig_num is None:
                raise ValueError(f"Unknown signal: {sig}")
            sig = sig_num

        await mp.send_signal(sig)
        return {"id": proc_id, "signal_sent": sig}
```

Modify `_watch_exit` — accept `exit_code=None` (PTY):

```python
    async def _watch_exit(self, proc_id: int, mp: ManagedProcess) -> None:
        """Background task: wait for process exit and update DB."""
        try:
            exit_code = await mp.wait_exit()
            await self._db.update_status(proc_id, mp.status, exit_code)
        except Exception:
            pass
```

(No change needed — `update_status` already accepts `exit_code=None`. Verify by reading the existing body and confirming nothing requires an int.)

- [ ] **Step 4: Run all tests to verify they pass**

Run: `pytest tests/ -v`
Expected: PASS — existing suite green (pipe mode untouched) + new manager-level PTY tests.

- [ ] **Step 5: Commit**

```bash
git add portal_mcp/manager.py tests/test_pty.py
git commit -m "feat(manager): branch start() on pty flag, add screen() and PTY signal passthrough"
```

---

### Task 7: Server tools — `process_screen`, `pty` param, descriptions, MCP instructions

**Files:**
- Modify: `portal_mcp/server.py`

**Interfaces:**
- Consumes: `manager.screen` / `manager.query_program` / `manager.record_program` / `manager.start(pty=...)` (Tasks 3, 6).
- Produces: the completed tool surface + instructions text. Used by Task 8 (README).

- [ ] **Step 1: `process_start` — add the `pty` parameter and rule-carrying description**

Replace the `process_start` tool description and schema (server.py lines ~91-140):

```python
                    description=(
                        "Start a subprocess for interactive use. "
                        "Use this for interactive programs like SSH, "
                        "GDB, psql, python REPL, etc. — not for "
                        "simple one-shot commands. Returns the "
                        "internal process ID, OS PID, and initial "
                        "status.\n"
                        "\n"
                        "pty — default false for backward "
                        "compatibility; this is NOT a "
                        "recommendation. Set true for anything "
                        "interactive or TUI (ssh, gdb, psql/mysql, "
                        "REPLs, vim, htop, top, less; anything with "
                        "-i/-it/-t flags). Consult program_query for "
                        "this executable BEFORE starting. When in "
                        "doubt, true — a non-interactive program "
                        "tolerates a PTY; an interactive one without "
                        "one hangs. Exception: one-shot commands that "
                        "page output (git log/diff, less) — prefer "
                        "pipe mode with --no-pager/GIT_PAGER=cat."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": (
                                    "Executable or command to run."
                                ),
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
                                "additionalProperties": {
                                    "type": "string"
                                },
                                "description": (
                                    "Environment variables "
                                    "(merged with current env)."
                                ),
                            },
                            "timeout_ms": {
                                "type": "integer",
                                "description": (
                                    "Idle timeout in milliseconds. "
                                    "Process is killed and cleaned up "
                                    "if no tool interaction occurs for "
                                    "this duration. 0 = no timeout."
                                ),
                                "default": 0,
                            },
                            "pty": {
                                "type": "boolean",
                                "description": (
                                    "Run on a virtual PTY instead of "
                                    "pipes. True for programs that "
                                    "need a terminal (see tool "
                                    "description). Default false for "
                                    "backward compatibility — NOT a "
                                    "recommendation."
                                ),
                                "default": False,
                            },
                        },
                        "required": ["command"],
                    },
```

In `handle_call_tool`, pass the new argument:

```python
                if name == "process_start":
                    result = await manager.start(
                        command=arguments["command"],
                        args=arguments.get("args", []),
                        cwd=arguments.get("cwd"),
                        env=arguments.get("env"),
                        timeout_ms=arguments.get("timeout_ms", 0),
                        pty=arguments.get("pty", False),
                    )
```

- [ ] **Step 2: Update the existing tool descriptions (PTY semantics)**

`process_read` description — append:

```python
                        "\n"
                        "For PTY processes stderr is merged into "
                        "stdout (source=stderr reads return empty) "
                        "and records are arbitrary chunks, not lines "
                        "— a line may span multiple records. Prompts "
                        "may arrive without a trailing newline. Read "
                        "with a generous duration — the default "
                        "window is only 1s."
```

`process_write` description — replace:

```python
                    description=(
                        "Write content to a process's stdin. "
                        "Only available while the process is running.\n"
                        "\n"
                        "Input you write reappears in the output "
                        "stream (terminal echo) — treat it as your "
                        "own input, not program output, and do not "
                        "re-send it. To interrupt a PTY process, "
                        "send \u0003 (Ctrl+C); a KeyboardInterrupt "
                        "traceback in output is expected, not an "
                        "error. process_signal/process_kill are "
                        "hard-stop fallbacks."
                    ),
```

`process_signal` — append to the help text returned by `_signal_help` on Windows:

```python
            f"{common} Windows supports: "
            "CTRL_C_EVENT (0), CTRL_BREAK_EVENT (1). "
            "SIGTERM is mapped to TerminateProcess. "
            "For PTY processes: SIGTERM -> terminate (hard kill on "
            "Windows), SIGKILL -> kill, CTRL_C_EVENT -> Ctrl+C; for "
            "a graceful interrupt use process_write with \u0003."
```

`process_inspect` description — append:

```python
                        "\n"
                        "For PTY processes: stderr_count is always 0 "
                        "(merged stream) and I/O counts are "
                        "chunk-based, not line-based; exit_code is "
                        "null on Windows (ConPTY exposes none)."
```

- [ ] **Step 3: Register and handle `process_screen`**

In `handle_list_tools`, after the `process_cleanup` entry:

```python
                types.Tool(
                    name="process_screen",
                    description=(
                        "Snapshot the live screen of a PTY process. "
                        "For full-screen TUIs (vim, htop, less): the "
                        "record stream is garbled fragments — use "
                        "this tool instead of process_read. Screen "
                        "remains queryable after exit until "
                        "process_cleanup. Snapshot is pure; passing "
                        "cols/rows resizes the live PTY first. "
                        "buffer is 'primary' or 'alternate' (the "
                        "TUI's alternate screen)."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "Internal process ID.",
                            },
                            "cols": {
                                "type": "integer",
                                "description": (
                                    "Optional: resize width first."
                                ),
                            },
                            "rows": {
                                "type": "integer",
                                "description": (
                                    "Optional: resize height first."
                                ),
                            },
                        },
                        "required": ["id"],
                    },
                ),
```

In `handle_call_tool`, after the `process_cleanup` branch:

```python
                elif name == "process_screen":
                    snap = await manager.screen(
                        proc_id=arguments["id"],
                        cols=arguments.get("cols"),
                        rows=arguments.get("rows"),
                    )
                    return [
                        types.TextContent(
                            type="text",
                            text=(
                                f"Process {snap['id']} "
                                f"(status={snap['status']}, "
                                f"buffer={snap['buffer']}, "
                                f"{snap['cols']}x{snap['rows']}, "
                                f"cursor=({snap['cursor_x']},"
                                f"{snap['cursor_y']})):\n"
                                f"{snap['content']}"
                            ),
                        )
                    ]
```

- [ ] **Step 4: Update the MCP instructions text**

In `main()`'s `InitializationOptions(instructions=...)`:

(a) Replace the workflow step 5 line:

```
                        "5. `process_signal` or `process_kill` — stop "
                        "the process\n"
```

with:

```
                        "5. To interrupt a PTY process, `process_write` "
                        "with `\u0003` (Ctrl+C); `process_signal` / "
                        "`process_kill` are hard-stop fallbacks\n"
```

(b) Replace the interactive-flags bullet:

```
                        "- Use interactive flags: `-i`, `--interactive`, "
                        "`-t`, `--tty`\n"
```

with:

```
                        "- Use interactive flags: `-i`, `--interactive`, "
                        "`-t`, `--tty`. These flags go in the command's "
                        "`args` — they configure the program's own "
                        "terminal. The `pty` param on `process_start` "
                        "grants the LOCAL terminal. Tools like docker "
                        "need both.\n"
```

(c) Append a new section before the final "## Remember" section:

```
                        "## When a program needs `pty: true`\n"
                        "\n"
                        "Set `pty: true` on `process_start` for:\n"
                        "- Programs that check isatty(): ssh, gdb, "
                        "psql, mysql, telnet, interactive REPLs\n"
                        "- Full-screen TUIs: vim, htop, top, less, man\n"
                        "- Anything with -i/-it/-t flags\n"
                        "\n"
                        "Prefer pipe mode (pty: false, the default) "
                        "for one-shot scripts and batch commands — "
                        "and for one-shot commands that page output "
                        "(git log/diff, less) use --no-pager or "
                        "GIT_PAGER=cat, since with pty: true they sit "
                        "in the pager and look hung.\n"
                        "\n"
                        "When in doubt, use pty: true — a "
                        "non-interactive program tolerates a PTY; an "
                        "interactive one without one hangs.\n"
                        "\n"
                        "## Decision priority: registry first\n"
                        "\n"
                        "Before starting a program, call "
                        "`program_query`. A hit settles it (check "
                        "`notes` for flag-specific caveats — e.g. a "
                        "docker entry may only cover `docker run -it`, "
                        "not `docker build`). A miss is normal — apply "
                        "the rules above. confirmed_count >= 2 means "
                        "settled; a single confirmation is a hint — "
                        "re-verify on first use.\n"
                        "\n"
                        "## Hang check and escalation\n"
                        "\n"
                        "When a process seems hung, read with a "
                        "generous window (duration=10000, unit ms — "
                        "the default 1s window misses older output "
                        "and looks identical to a hang). Treat as a "
                        "hang only if reads keep returning empty AND "
                        "process_list shows io_count unchanged after "
                        "several seconds. Set timeout_ms on every "
                        "escalated attempt so a real hang "
                        "self-terminates. On hang: kill, restart with "
                        "pty: true. If a pty: true process sits in a "
                        "pager, send q (or \u0003), kill, restart "
                        "without pty.\n"
                        "\n"
                        "## Registry feedback loop\n"
                        "\n"
                        "Record every first-encounter conclusion via "
                        "`program_record` — including negatives "
                        "(needs_pty=false when the program ran fine "
                        "without a PTY). The registry is "
                        "agent-populated; it only helps future "
                        "sessions if every encounter is recorded.\n"
                        "\n"
```

- [ ] **Step 5: Run all tests to verify they pass**

Run: `pytest tests/ -v`
Expected: PASS — existing suite green; no test changes needed in this task.

- [ ] **Step 6: Verify the server module imports and tool surface is wired**

Run:

```bash
python -c "
import asyncio, os, tempfile
from portal_mcp.server import create_server

async def main():
    fd, db_path = tempfile.mkstemp(suffix='.db'); os.close(fd)
    manager, db = await create_server(db_path)
    # Registry wired through create_server:
    await manager.record_program('ssh', True)
    print(await manager.query_program('ssh'))
    await manager.shutdown()
    await db.close()
    os.unlink(db_path)

asyncio.run(main())
"
```

Expected: `{'program': 'ssh', 'needs_pty': True, 'notes': '', 'confirmed_count': 1}` — proves `create_server` wires the registry and `main()`'s cleanup path (manager.shutdown) closes it.

- [ ] **Step 7: Commit**

```bash
git add portal_mcp/server.py
git commit -m "feat(server): add process_screen tool, pty param, PTY semantics in descriptions and instructions"
```

---

### Task 8: Documentation — README (EN + zh) and llms-install.md

**Files:**
- Modify: `README.md`, `README_zh.md`, `llms-install.md`

**Interfaces:**
- Consumes: everything from Tasks 1-7 (tool surface, semantics, registry).

- [ ] **Step 1: Update `llms-install.md` dependency line**

Find the line mentioning dependencies and replace it:

```markdown
Dependencies: `mcp`, `aiosqlite`, `pyte`, plus `pywinpty` (Windows) / `ptyprocess` (POSIX).
```

- [ ] **Step 2: Add PTY section to `README.md`** (English)

Add a section after the tools table (or after the current tool description):

```markdown
## Virtual PTY support

By default Portal runs programs on OS pipes. Programs that check
`isatty()` (ssh, gdb, psql, interactive REPLs) or render full-screen
TUIs (vim, htop, less) need a virtual PTY instead: pass `"pty": true`
to `process_start`. On Windows this uses ConPTY (Windows 10 1809+);
on POSIX it uses ptyprocess.

PTY mode differences from pipe mode:
- stdout and stderr are merged into one console stream (`process_read`
  with `source="stderr"` returns empty)
- records are arbitrary chunks, not lines — a line may span multiple
  records, and prompts may arrive without a trailing newline
- input you write is echoed back into the output stream (real terminal
  behavior) — treat echoes as your own input
- to interrupt a PTY process, write `\u0003` (Ctrl+C) via
  `process_write`; a `KeyboardInterrupt` traceback is expected output
- exit code is `null` on Windows (ConPTY exposes none)

`process_screen` snapshots the live screen of a PTY process — use it
for full-screen TUIs, whose record streams are garbled fragments.
Passing `cols`/`rows` resizes the live PTY first; omitting them is a
pure snapshot. The screen stays queryable after exit until
`process_cleanup`.

### Choosing `pty`

| Signal | Examples | Decision |
|--------|----------|----------|
| Checks `isatty()` | ssh, gdb, psql, mysql, telnet, REPLs | `pty: true` |
| Full-screen TUI | vim, htop, top, less, man | `pty: true` |
| Interactive flags | `-i` / `-it` / `-t` | `pty: true` |
| One-shot / batch | `python -c`, build commands | `pty: false` |
| Paged one-shots | git log/diff, less | pipe mode + `--no-pager`/`GIT_PAGER=cat` |

When in doubt, use `pty: true` — a non-interactive program tolerates
a PTY; an interactive one without one hangs.

### Program registry

`program_query` / `program_record` maintain a persistent, machine-global
registry (`programs.db` in the platform app-data dir, or
`PORTAL_DATA_DIR` if set) of which executables need a PTY. Agents
record conclusions after first encounters — including negatives.
Repeated confirmation increments `confirmed_count` (>= 2 means
settled); recording the opposite value resets it (a correction).
```

- [ ] **Step 3: Add the equivalent section to `README_zh.md`** (Chinese, same structure)

```markdown
## 虚拟 PTY 支持

默认情况下 Portal 使用系统管道运行程序。检查 `isatty()` 的程序（ssh、
gdb、psql、交互式 REPL）和全屏 TUI（vim、htop、less）需要虚拟 PTY：
给 `process_start` 传 `"pty": true` 即可。Windows 上基于 ConPTY
（Windows 10 1809+），POSIX 上基于 ptyprocess。

PTY 模式与管道模式的差异：
- stdout 与 stderr 合并为单一控制台流（`process_read` 的
  `source="stderr"` 恒为空）
- 记录是任意块，不是行——一行可能跨多条记录，提示符可能没有换行
- 写入的输入会回显到输出流（真实终端行为）——回显是输入，不是输出
- 中断 PTY 进程：用 `process_write` 发送 `\u0003`（Ctrl+C）；
  `KeyboardInterrupt` 回溯是预期输出
- Windows 上退出码为 `null`（ConPTY 不提供）

`process_screen` 对 PTY 进程做实时屏幕快照——全屏 TUI 的记录流是
乱码片段，请用此工具读取。传 `cols`/`rows` 会先调整实时终端尺寸，
省略则为纯快照。进程退出后屏幕仍可查询，直到 `process_cleanup`。

### 何时使用 `pty`

| 信号 | 例子 | 判定 |
|------|------|------|
| 检查 `isatty()` | ssh、gdb、psql、mysql、telnet、REPL | `pty: true` |
| 全屏 TUI | vim、htop、top、less、man | `pty: true` |
| 交互式 flags | `-i` / `-it` / `-t` | `pty: true` |
| 一次性脚本/批处理 | `python -c`、构建命令 | `pty: false` |
| 分页输出的一次性命令 | git log/diff、less | 管道模式 + `--no-pager`/`GIT_PAGER=cat` |

拿不准时用 `pty: true`——非交互程序容忍 PTY，交互程序没有 PTY 会挂起。

### 程序注册表

`program_query` / `program_record` 维护一个跨会话、机器全局的注册表
（`programs.db`，位于平台应用数据目录，可用 `PORTAL_DATA_DIR` 覆盖），
记录哪些可执行文件需要 PTY。Agent 在首次遇到程序后回写结论（包括
"不需要"的负例）。重复确认递增 `confirmed_count`（>= 2 视为已定案）；
记录相反值会重置计数（视为修正）。
```

- [ ] **Step 4: Verify the docs reference real facts**

Run: `python -c "from portal_mcp.server import _signal_help; print(_signal_help())"`
Expected: output contains the PTY mapping text.

- [ ] **Step 5: Commit**

```bash
git add README.md README_zh.md llms-install.md
git commit -m "docs: document PTY mode, decision rules and program registry"
```

---

### Task 9: Final verification pass

**Files:**
- None (verification only).

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -v`
Expected: PASS — every test green (existing pipe-mode suite + test_paths + test_registry + test_pty).

- [ ] **Step 2: Verify lazy-import rule end-to-end**

Run: `python -c "import portal_mcp.server, portal_mcp.manager, portal_mcp.database; print('pipe-mode imports OK')"`
Expected: prints without importing winpty/pyte (no ModuleNotFoundError).

- [ ] **Step 3: Verify the registry survives a server restart**

Run:

```bash
python -c "
import asyncio, tempfile, os
from portal_mcp.registry import Registry
async def main():
    d = tempfile.mkdtemp()
    r = Registry(os.path.join(d, 'programs.db'))
    await r.open(); await r.record('ssh', True, 'interactive'); await r.close()
    r2 = Registry(os.path.join(d, 'programs.db'))
    await r2.open(); print(await r2.query('ssh')); await r2.close()
asyncio.run(main())
"
```

Expected: `{'program': 'ssh', 'needs_pty': True, 'notes': 'interactive', 'confirmed_count': 1}`

- [ ] **Step 4: Manual end-to-end smoke test with a real MCP client**

In Claude Code (or any MCP client), after reconfiguring the server:

1. `process_start` with `{"command": "python", "args": ["-c", "import sys; print(sys.stdout.isatty())"], "pty": true}` → read shows `True`
2. Same without `pty` → read shows `False`
3. `process_start` vim (or `python -m pdb`), use `process_screen` to see the rendered screen
4. `program_record` for a program, restart the server, `program_query` still returns the entry

Expected: all four behave per the spec's documented semantics.
