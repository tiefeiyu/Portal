"""ANSI escape sequence stripping."""
import re

# Matches CSI sequences: ESC [ params letter
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text.

    Strips CSI (Control Sequence Introducer) sequences which include
    color codes, cursor movement, and text formatting escape codes.

    Args:
        text: Input string potentially containing ANSI sequences.

    Returns:
        String with all ANSI escape sequences removed.
    """
    return _ANSI_PATTERN.sub("", text)
