"""Tests for platform data-dir resolution."""
import os
import sys
from pathlib import Path
import pytest
from portal_mcp.paths import (
    default_data_dir,
    ensure_local_gitignore,
    registry_db_path,
)


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


def test_ensure_local_gitignore_creates_star_file(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ensure_local_gitignore(scratch)
    assert (scratch / ".gitignore").read_bytes() == b"*\n"


def test_ensure_local_gitignore_keeps_existing_file(tmp_path):
    """The user may have put their own rules there; never overwrite."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    ignore = scratch / ".gitignore"
    ignore.write_bytes(b"!keep-me\n")
    ensure_local_gitignore(scratch)
    assert ignore.read_bytes() == b"!keep-me\n"


def test_ensure_local_gitignore_swallows_oserror(tmp_path):
    """Writing must be best-effort: an unwritable target (here a path
    under a regular file, so open() raises FileNotFoundError on
    Windows) returns silently instead of blocking server startup."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_bytes(b"")
    ensure_local_gitignore(blocker / "nested")  # must not raise
    assert blocker.read_bytes() == b""
