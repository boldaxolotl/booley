"""Split a printed command back into arguments as its host shell would read it."""

from __future__ import annotations

import os
import re
import shlex

_POWERSHELL_WORD = re.compile(r"'((?:[^']|'')*)'|(\S+)")


def split_host_command(command: str, *, windows: bool = os.name == "nt") -> list[str]:
    """Split a printed command as its host shell would: POSIX sh, or PowerShell literals."""
    if not windows:
        try:
            return shlex.split(command)
        except ValueError as error:
            message = f"printed command does not parse back: {error} in {command!r}"
            raise AssertionError(message) from error
    words: list[str] = []
    position = 0
    command = command.strip()
    while position < len(command):
        if command[position].isspace():
            position += 1
            continue
        match = _POWERSHELL_WORD.match(command, position)
        if match is None or (match.end() < len(command) and not command[match.end()].isspace()):
            message = f"printed command does not parse back: No closing quotation in {command!r}"
            raise AssertionError(message)
        quoted, bare = match.groups()
        words.append(quoted.replace("''", "'") if quoted is not None else bare)
        position = match.end()
    if words[:1] == ["&"]:
        words = words[1:]
    return words
