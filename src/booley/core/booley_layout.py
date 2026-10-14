"""Read-only layout/refusal boundary for the Booley Directory transition."""

from __future__ import annotations

import hashlib
import os
from enum import StrEnum
from pathlib import Path

from booley.core.checkout_role import is_booley_source_checkout
from booley.core.user_paths import config_dir

BOOLEY_DIR_NAME = ".booley"
RETIRED_DIR_NAME = ".booley_project"
RETIRED_ENV = "BOOLEY_PROJECT_DIR"


class BooleyLayoutError(RuntimeError):
    """Project layout requires explicit initialization or human reconciliation."""


class Layout(StrEnum):
    """The observed local directory arrangement, without following links."""

    ABSENT = "absent"
    CURRENT = "current"
    RETIRED = "migration-needed"


def validate_environment() -> None:
    """Reject the obsolete selection variable even when it is empty."""
    if RETIRED_ENV in os.environ:
        raise BooleyLayoutError(f"{RETIRED_ENV} is retired; unset it and use BOOLEY_DIR")


def reject_retired_path(path: Path) -> None:
    """An explicit selection cannot bypass the hard cut."""
    if RETIRED_DIR_NAME in path.parts:
        raise BooleyLayoutError(f"{path} needs migration; run `booley init` from its Project Root")
    if any(
        a == BOOLEY_DIR_NAME and b == "project"
        for a, b in zip(path.parts, path.parts[1:], strict=False)
    ):
        raise BooleyLayoutError(f"unsupported older .booley/project layout: {path}")


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _safe_directory(path: Path) -> None:
    if _present(path) and (path.is_symlink() or not path.is_dir()):
        raise BooleyLayoutError(f"unsafe Booley Directory (expected a real directory): {path}")


def classify_layout(root: Path) -> Layout:
    """Classify a checkout; collisions and unsupported layouts always refuse."""
    current, retired = root / BOOLEY_DIR_NAME, root / RETIRED_DIR_NAME
    if not current.is_symlink():
        _safe_directory(current)
    _safe_directory(retired)
    if _present(current) and _present(retired):
        raise BooleyLayoutError(
            f"both Booley Directories exist: {retired} and {current}; preserve both trees. "
            "Run migration before pulling a tracked rename; do not `git add -A` local "
            "runtime state. See the changelog's manual recovery procedure."
        )
    if current.is_dir():
        if _present(current / "project") or is_booley_source_checkout(current):
            raise BooleyLayoutError(f"unsupported older Booley source/project layout at {current}")
        return Layout.CURRENT
    return Layout.RETIRED if retired.is_dir() else Layout.ABSENT


def require_current_layout(root: Path) -> None:
    """Diagnose local old-only Projects before ancestor/config/cache fallbacks."""
    if pending_journal(root) is not None:
        raise BooleyLayoutError(f"interrupted directory migration at {root}; run `booley init`")
    if classify_layout(root) is Layout.RETIRED:
        raise BooleyLayoutError(
            f"{root / RETIRED_DIR_NAME} needs migration to {root / BOOLEY_DIR_NAME}; "
            "run `booley init` from the Project Root"
        )


def validate_selection(path: Path) -> None:
    """Validate explicit and cached selections without adopting an old layout."""
    validate_environment()
    reject_retired_path(path.absolute())
    if path.name == BOOLEY_DIR_NAME:
        require_current_layout(path.parent)


def pending_journal(root: Path) -> Path | None:
    """Find a Git-backed pending migration without invoking Git or creating files."""
    marker = root / ".git"
    if marker.is_file():
        raw = marker.read_text().strip()
        if not raw.startswith("gitdir: "):
            raise BooleyLayoutError(f"invalid Git worktree marker: {marker}")
        admin = Path(raw.removeprefix("gitdir: "))
        admin = admin if admin.is_absolute() else root / admin
        common = admin / "commondir"
        marker = (admin / common.read_text().strip()).resolve() if common.is_file() else admin
    if marker.exists():
        journal = marker / "booley" / "directory-migration.json"
    else:
        key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
        journal = config_dir() / "directory-migrations" / key / "directory-migration.json"
    return journal if journal.is_file() else None
