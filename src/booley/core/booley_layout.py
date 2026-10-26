"""Read-only layout/refusal boundary for the Booley Directory transition."""

from __future__ import annotations

import hashlib
import os
from enum import StrEnum
from pathlib import Path

from booley.core.checkout_role import is_booley_source_checkout
from booley.core.private_store import PrivateStore
from booley.core.user_paths import config_dir

BOOLEY_DIR_NAME = ".booley"
RETIRED_DIR_NAME = ".booley_project"
RETIRED_ENV = "BOOLEY_PROJECT_DIR"
# Doctor's scratch root inside the Booley Directory; deep checks write Flow reports below it.
DOCTOR_TMP_DIR = Path("tmp") / "doctor"


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


def git_common_directory(root: Path) -> Path | None:
    """Read the stable Git administration path, including temporarily broken links."""
    marker = root / ".git"
    if marker.is_file():
        raw = marker.read_text(encoding="utf-8").strip()
        if not raw.startswith("gitdir: "):
            raise BooleyLayoutError(f"invalid Git worktree marker: {marker}")
        admin = Path(raw.removeprefix("gitdir: "))
        marker = admin if admin.is_absolute() else root / admin
        if not marker.is_dir() and RETIRED_DIR_NAME in marker.parts:
            raise BooleyLayoutError(
                "interrupted directory migration left a retired Git pointer; run `booley init` from the primary Project Root"
            )
        common = marker / "commondir"
        if common.is_file():
            marker = (marker / common.read_text(encoding="utf-8").strip()).resolve()
        # Retain a missing administrative path: a stale pointer cannot prove no journal.
        return marker.resolve()
    return marker.resolve() if marker.is_dir() else None


def layout_metadata(root: Path) -> Path:
    """Stable coordination state outside the moved directory and non-Git RTL trees."""
    common = git_common_directory(root)
    if common is not None:
        return common / "booley"
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    return config_dir() / "directory-migrations" / key


def ensure_layout_metadata(root: Path) -> Path:
    """Create coordination storage without weakening host-private config directories."""
    directory = layout_metadata(root)
    if git_common_directory(root) is None:
        return PrivateStore(
            directory, config_dir().parent, "directory migration", BooleyLayoutError
        ).ensure_directory()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def pending_journal(root: Path) -> Path | None:
    """Find a pending migration without invoking Git or creating files."""
    journal = layout_metadata(root) / "directory-migration.json"
    return journal if journal.is_file() else None
