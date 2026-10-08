"""Literal regular-file ownership proof shared by current and retained attempts."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

from booley.goals.lifecycle import LifecycleError


def require_destination(path: Path, content: bytes, *, allow_owned: bool = True) -> bool:
    for ancestor in path.parents:
        if ancestor.is_symlink() or (ancestor.exists() and not ancestor.is_dir()):
            raise LifecycleError(f"publication ancestor conflicts with saved intent: {ancestor}")
    if not path.exists() and not path.is_symlink():
        return False
    if (
        path.is_symlink()
        or not path.is_file()
        or not allow_owned
        or path.read_bytes() != content
        or not summary_mode(path)
    ):
        raise LifecycleError(f"publication destination conflicts with frozen summary: {path}")
    return True


def summary_mode(path: Path) -> bool:
    """Windows exposes a read-only flag; Git's separate object proof remains 100644."""
    mode = stat.S_IMODE(path.stat().st_mode)
    return bool(mode & stat.S_IWRITE) if sys.platform == "win32" else not mode & 0o111
