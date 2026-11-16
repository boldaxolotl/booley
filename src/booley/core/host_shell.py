"""Host-shell quoting for commands Booley prints for the user to paste."""

from __future__ import annotations

import os
import shlex

__all__ = ["quote_arg"]


def quote_arg(arg: str) -> str:
    """Quote one argument for the host shell: POSIX ``shlex.quote``, PowerShell on Windows."""
    if os.name == "nt":
        return "'" + arg.replace("'", "''") + "'"
    return shlex.quote(arg)
