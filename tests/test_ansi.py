"""Tests for ANSI stripping."""
import pytest
from portal_mcp.ansi import strip_ansi


class TestStripAnsi:
    def test_removes_color_codes(self):
        assert strip_ansi("\x1b[31mred text\x1b[0m") == "red text"

    def test_removes_bold_and_dim(self):
        assert strip_ansi("\x1b[1mbold\x1b[0m \x1b[2mdim\x1b[0m") == "bold dim"

    def test_removes_cursor_movement(self):
        assert strip_ansi("\x1b[2J\x1b[Hhello") == "hello"

    def test_removes_extended_colors(self):
        assert strip_ansi("\x1b[38;5;196mred\x1b[0m") == "red"
        assert strip_ansi("\x1b[48;2;255;0;0m\x1b[38;2;0;255;0mtext\x1b[0m") == "text"

    def test_passes_through_plain_text(self):
        assert strip_ansi("hello world") == "hello world"
        assert strip_ansi("line1\nline2") == "line1\nline2"

    def test_handles_empty_string(self):
        assert strip_ansi("") == ""

    def test_handles_only_ansi(self):
        assert strip_ansi("\x1b[31m\x1b[0m") == ""

    def test_removes_complex_ansi_sequences(self):
        # Bold + red FG + green BG + underline
        text = "\x1b[1;31;42;4mstyled\x1b[0m normal"
        assert strip_ansi(text) == "styled normal"
