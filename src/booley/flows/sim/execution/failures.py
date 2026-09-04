"""Shared classification for expected Simulation infrastructure failures."""

from __future__ import annotations

import re
from pathlib import Path

_MISSING_EXE_PATTERNS = (
    re.compile(
        r"^(?:[\w./-]*(?:sh|bash|dash))(?::\s*(?:line\s+)?\d+)?:\s*"
        r"(?P<name>[\w.+-]+):\s*(?:command\s+)?not found",
        re.MULTILINE,
    ),
    re.compile(
        r"^make(?:\[\d+\])?:\s*(?P<name>[\w.+-]+):\s*"
        r"(?:command not found|no such file or directory)",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(r"^(?P<name>[\w.+-]+):\s*command not found", re.MULTILINE),
    re.compile(r"could not invoke\s+(?P<name>[\w.+-]+)\b"),
    re.compile(r"No such file or directory:\s*'(?P<name>[\w+-]+)'"),
)


def find_missing_executable(text: str) -> str | None:
    """Return the absent executable named by a recognized boundary error."""
    for pattern in _MISSING_EXE_PATTERNS:
        match = pattern.search(text)
        if match:
            return Path(match.group("name")).name
    return None


__all__ = ["find_missing_executable"]
