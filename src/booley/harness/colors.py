"""Terminal color utilities for harness output.

ANSI 256-color with automatic fallback for dumb terminals / pipes.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable


def _color_supported() -> bool:
    """Check if stdout supports ANSI color codes."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(sys.stdout, "isatty"):
        return False
    if not sys.stdout.isatty():
        return False
    term = os.environ.get("TERM", "")
    if term == "dumb":
        return False
    # Windows: modern terminals (WT, ConEmu, VSCode) support ANSI
    if sys.platform == "win32":
        # Enable VT processing on Windows 10+
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except (OSError, AttributeError, ValueError):
            pass
    return True


# Global flag -- evaluated once at import time
COLORS_ENABLED = _color_supported()


def in_vscode() -> bool:
    """True when stdout is VS Code's integrated terminal.

    VS Code sets ``TERM_PROGRAM=vscode`` for its terminal (including the one
    attached to a devcontainer). It matters because VS Code only makes OSC 8
    hyperlinks clickable for ``http``/``https`` -- ``file://`` links render as
    dead underlined text (microsoft/vscode#176812). Callers use this to fall
    back to plain file paths, which VS Code's own link detector *does* open.
    """
    return os.environ.get("TERM_PROGRAM", "").lower() == "vscode"


# --- ANSI codes ---
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_FG = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "gray": "\033[90m",
}


def _style(text: str, *codes: str) -> str:
    if not COLORS_ENABLED:
        return text
    return "".join(codes) + text + _RESET


# --- Public API ---


def bold(text: str) -> str:
    return _style(text, _BOLD)


def dim(text: str) -> str:
    return _style(text, _DIM)


def red(text: str) -> str:
    return _style(text, _FG["red"])


def green(text: str) -> str:
    return _style(text, _FG["green"])


def yellow(text: str) -> str:
    return _style(text, _FG["yellow"])


def gray(text: str) -> str:
    return _style(text, _FG["gray"])


def bold_green(text: str) -> str:
    return _style(text, _BOLD, _FG["green"])


def bold_red(text: str) -> str:
    return _style(text, _BOLD, _FG["red"])


def fg256(code: int) -> Callable[[str], str]:
    """Create a foreground color function from a 256-color code."""
    ansi = f"\033[38;5;{code}m"
    return lambda text: _style(text, ansi)


def bold_fg256(code: int) -> Callable[[str], str]:
    """Create a bold foreground color function from a 256-color code."""
    ansi = f"\033[38;5;{code}m"
    return lambda text: _style(text, _BOLD, ansi)


# --- Jewel Tones palette (B) — named helpers for use outside MCP endpoint boxes ---
# Names retained for stability; codes updated to match Palette B in terminal.py.
# Accent (UI chrome emphasis: headers, bars, code spans) — Dodger Blue
accent = fg256(39)
bold_accent = bold_fg256(39)
# Chrome (structural: box drawing, totals, neutral labels) — Mauve
chrome = fg256(145)
bold_chrome = bold_fg256(145)
# Amber (highlight: banner eyes, ticket slugs) — Orange in Palette B
amber = fg256(208)
bold_amber = bold_fg256(208)
# Lilac (review status on the board) — Lavender in Palette B
lilac = fg256(141)


# Matches CSI color/style codes plus OSC 8 hyperlink open/close markers
# (both ST ``ESC \\`` and BEL terminated) so padding math stays honest.
_ANSI_RE = re.compile(r"\033\[[0-9;]*m|\033\]8;;[^\033\007]*(?:\033\\|\007)")


def len_visible(text: str) -> int:
    """Length of *text* ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", text))


def hyperlink(text: str, uri: str) -> str:
    """Wrap *text* in an OSC 8 terminal hyperlink to *uri*.

    Modern terminals (VS Code integrated terminal, iTerm2, WezTerm,
    Windows Terminal) render this as a clickable link; with a ``file://``
    URI, VS Code opens the file in the editor on Ctrl/Cmd+Click. Gated on
    the same tty check as colors so piped output stays plain text.
    """
    if not COLORS_ENABLED or not uri:
        return text
    return f"\033]8;;{uri}\033\\{text}\033]8;;\033\\"
