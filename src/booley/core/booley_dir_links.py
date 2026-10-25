"""The one rule for a symlinked Booley Directory entry.

Only the exact ``<root>/.booley`` entry (the Booley Directory) may be a followed
symlink; every link above or below it stays refused. Places that walk a path
without following links call :func:`is_booley_dir_entry` to learn which single
component they may follow. Places that compare a resolved path with the lexical
Booley Directory call :func:`lexical_booley_path` to put both in one spelling.
Stdlib-only so ``core`` and ``fusesoc`` can share it.
"""

from __future__ import annotations

from pathlib import Path


def is_booley_dir_entry(path: Path, booley_dir: Path | None) -> bool:
    """Whether *path* is exactly the Booley Directory entry (lexical comparison only).

    No resolve and no ``..`` folding, so an ancestor alias or a spelling through
    another link never matches.
    """
    return booley_dir is not None and path.absolute() == booley_dir.absolute()


def lexical_booley_path(booley_dir: Path, path: Path) -> Path:
    """Re-spell a path below the entry's physical target as ``booley_dir / rel``.

    Returns *path* unchanged unless *booley_dir* itself is a symlink, so it is the
    identity in the plain layout for any caller spelling (including an aliased
    Project Root). Paths not below the entry's target are returned unchanged.
    """
    if not booley_dir.is_symlink():
        return path
    try:
        return booley_dir / path.relative_to(booley_dir.resolve())
    except ValueError:
        return path
