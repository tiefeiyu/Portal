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

        # pywinpty defaults to its legacy winpty-agent backend, which
        # echoes input but never delivers it to the child (only Ctrl+C
        # is special-cased). Force the real ConPTY backend so that
        # interactive writes actually reach the child.
        prev = os.environ.get("PYWINPTY_BACKEND")
        os.environ["PYWINPTY_BACKEND"] = "1"
        try:
            return cls(PtyProcess.spawn(
                argv, cwd=cwd, env=env, dimensions=(rows, cols)
            ))
        finally:
            if prev is None:
                os.environ.pop("PYWINPTY_BACKEND", None)
            else:
                os.environ["PYWINPTY_BACKEND"] = prev

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
