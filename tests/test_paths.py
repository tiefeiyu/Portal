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
