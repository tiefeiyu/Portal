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
        assert _decode(b"\xff\xfe") == "��"
