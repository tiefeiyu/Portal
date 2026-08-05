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
