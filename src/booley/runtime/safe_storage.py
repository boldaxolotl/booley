"""Refuse redirected paths for advisory runtime storage."""

from pathlib import Path

from booley.core.booley_dir_links import is_booley_dir_entry


def refuse_symlinks(path: Path, *, booley_dir: Path | None = None) -> None:
    """Refuse redirected storage, including ancestors and dangling links.

    The exact *booley_dir* entry, when given, may be a symlink; links above and
    below it are still refused.
    """
    for parent in path.parents:
        if parent.exists() and not parent.is_dir():
            raise OSError(f"storage ancestor is not a directory: {parent}")
    for item in (path, *path.parents):
        if item.is_symlink() and not is_booley_dir_entry(item, booley_dir):
            raise OSError(f"symlink storage is unavailable: {item}")
