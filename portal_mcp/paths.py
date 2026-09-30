"""Platform data-dir resolution for persistent Portal data."""
import os
import sys
from contextlib import suppress
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


def ensure_local_gitignore(directory: str | Path) -> None:
    """Write a `*` .gitignore inside a per-project scratch directory.

    Portal keeps its session databases in a scratch directory (such as
    `.portal/` next to the launch directory), which otherwise shows up
    as untracked clutter in the user's repository. A `.gitignore` that
    ignores everything keeps that directory out of `git status` without
    any action from the user.

    An existing `.gitignore` is left untouched — the user may have put
    their own rules there, and Portal has no business replacing them.
    Writing is best-effort: a read-only or missing directory must never
    keep the server from starting.
    """
    gitignore = Path(directory) / ".gitignore"
    with suppress(OSError):
        if not gitignore.exists():
            # Bytes, not text: text-mode writes translate "\n" to
            # "\r\n" on Windows, and the file should hold exactly "*\n".
            gitignore.write_bytes(b"*\n")
