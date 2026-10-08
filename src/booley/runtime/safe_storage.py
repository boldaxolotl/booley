"""Refuse redirected paths for advisory runtime storage."""

from pathlib import Path


def refuse_symlinks(path: Path) -> None:
    """Refuse redirected storage, including ancestors and dangling links."""
    for parent in path.parents:
        if parent.exists() and not parent.is_dir():
            raise OSError(f"storage ancestor is not a directory: {parent}")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise OSError(f"symlink storage is unavailable: {item}")
