"""Interactive worktree naming and the last-used stamp.

``booley worktree new <name>`` creates ``<Main Booley Directory>/worktrees/<name>``.
Every Booley tool call routed to such a worktree touches
``<Main Booley Directory>/worktrees/.last-used/<name>``; the stamp is that file's
mtime, so one atomic ``utime`` records it and nothing needs parsing. Names cannot
start with ``.``, so the stamp directory never collides with a worktree.
"""

from __future__ import annotations

import re
from pathlib import Path

from booley.runtime.safe_storage import refuse_symlinks

# One path component: letters, digits, '.', '_' or '-', starting with a letter or digit.
# The packaged worktree_create.sh enforces the same rule.
WORKTREE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_STAMP_DIR = ".last-used"


def interactive_worktree_name(path: Path, common: Path) -> str | None:
    """Return the worktree name when *path* is inside ``<common>/worktrees/<name>``.

    Only a linked worktree (its ``.git`` is a file) directly under ``worktrees/``
    counts. Both sides are resolved so the Sandbox spelling of the Main Booley
    Directory compares equal to the host one.
    """
    try:
        relative = path.resolve().relative_to((common / "worktrees").resolve())
    except ValueError:
        return None
    if not relative.parts:
        return None
    name = relative.parts[0]
    if not WORKTREE_NAME.fullmatch(name) or not (common / "worktrees" / name / ".git").is_file():
        return None
    return name


def stamp_worktree(common: Path, name: str) -> None:
    """Record now as worktree *name*'s last use."""
    stamp = common / "worktrees" / _STAMP_DIR / name
    refuse_symlinks(stamp, booley_dir=common)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()


def record_worktree_use(work_dir: Path, common: Path) -> None:
    """Stamp the interactive worktree containing *work_dir*, if it is one."""
    name = interactive_worktree_name(work_dir, common)
    if name is not None:
        stamp_worktree(common, name)


def last_used(common: Path, name: str) -> float | None:
    """Return worktree *name*'s last-used time (epoch seconds), or ``None`` if never."""
    try:
        return (common / "worktrees" / _STAMP_DIR / name).stat().st_mtime
    except FileNotFoundError:
        return None
