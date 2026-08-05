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
