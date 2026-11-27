"""Read-only layout and refusal boundary for the Booley Directory."""

from __future__ import annotations

import hashlib
import os
import re
from enum import StrEnum
from pathlib import Path

from booley.core.checkout_role import is_booley_source_checkout
from booley.core.private_store import PrivateStore
from booley.core.user_paths import config_dir

BOOLEY_DIR_NAME = ".booley"
RETIRED_DIR_NAME = ".booley_project"
RETIRED_ENV = "BOOLEY_PROJECT_DIR"
UPGRADE_THROUGH_0_4 = (
    "upgrade through Booley 0.4.x and run its `booley init` first, then upgrade to this release"
)
# Doctor's scratch root inside the Booley Directory; deep checks write Flow reports below it.
DOCTOR_TMP_DIR = Path("tmp") / "doctor"
# Job logs inside the Booley Directory: one subdirectory per MCP session, whose Flow
# reports live at ``<session>/.runtime/flow-reports``, plus ``standalone/`` for
# Standalone Job logs.
JOB_LOGS_DIR_NAME = "job_logs"
# Goal Records inside the Booley Directory: one subdirectory per Goal id, whose Flow
# reports live at ``<goal-id>/.runtime/flow-reports``.
GOALS_DIR_NAME = "goals"
# A Goal slug is a short lowercase path segment; a Goal id is ``<slug>-<compact UTC
# timestamp>``. Leftover Goal Record directories can outlive Goal Mode, so code that
# only needs to recognise one (Specialist workspace isolation; the Goal Record store and
# recorder also reuse the id pattern) reads the names here.
GOAL_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
GOAL_SLUG_MAX_LENGTH = 64
GOAL_ID_PATTERN = re.compile(rf"(?P<slug>{GOAL_SLUG_PATTERN.pattern})-\d{{8}}T\d{{6}}Z")
# The per-record directory of receipts and reviewer inputs.
GOAL_RECORD_LOGS_DIR = "logs"


class BooleyLayoutError(RuntimeError):
    """Project layout requires explicit initialization or human reconciliation."""


def retired_layout_error(subject: str) -> BooleyLayoutError:
    """The one refusal for a Booley 0.3.x leftover, which this release does not migrate."""
    return BooleyLayoutError(
        f"{subject} is retired: this release does not migrate Booley 0.3.x "
        f"Projects; {UPGRADE_THROUGH_0_4}"
    )


class Layout(StrEnum):
    """The observed local directory arrangement, without following links."""

    ABSENT = "absent"
    CURRENT = "current"


def validate_environment() -> None:
    """Reject the obsolete selection variable even when it is empty."""
    if RETIRED_ENV in os.environ:
        raise retired_layout_error(RETIRED_ENV)


def reject_retired_path(path: Path) -> None:
    """An explicit selection cannot bypass the hard cut."""
    if RETIRED_DIR_NAME in path.parts:
        raise retired_layout_error(str(path))
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
    """Classify a checkout's Booley Directory; unsupported layouts refuse."""
    current = root / BOOLEY_DIR_NAME
    if not current.is_symlink():
        _safe_directory(current)
    if current.is_dir():
        if _present(current / "project") or is_booley_source_checkout(current):
            raise BooleyLayoutError(f"unsupported older Booley source/project layout at {current}")
        return Layout.CURRENT
    return Layout.ABSENT


def require_current_layout(root: Path) -> None:
    """Refuse 0.3.x leftovers before ancestor/config/cache fallbacks."""
    journal = pending_journal(root)
    if journal is not None:
        raise retired_layout_error(f"the interrupted migration journal {journal}")
    if _present(root / RETIRED_DIR_NAME):
        raise retired_layout_error(str(root / RETIRED_DIR_NAME))
    classify_layout(root)


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
            raise retired_layout_error(f"the Git pointer {root / '.git'}")
        common = marker / "commondir"
        if common.is_file():
            marker = (marker / common.read_text(encoding="utf-8").strip()).resolve()
        # Retain a missing administrative path: a stale pointer cannot prove no journal.
        return marker.resolve()
    return marker.resolve() if marker.is_dir() else None


def layout_metadata(root: Path) -> Path:
    """Coordination state beside the Git common directory, or in host config outside Git."""
    common = git_common_directory(root)
    if common is not None:
        return common / "booley"
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    # The folder name is where 0.4.x wrote its journal, so it must not change.
    return config_dir() / "directory-migrations" / key


def ensure_layout_metadata(root: Path) -> Path:
    """Create coordination storage without weakening host-private config directories."""
    directory = layout_metadata(root)
    if git_common_directory(root) is None:
        return PrivateStore(
            directory, config_dir().parent, "Project coordination", BooleyLayoutError
        ).ensure_directory()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def pending_journal(root: Path) -> Path | None:
    """Find an unfinished 0.4.x migration journal without invoking Git or creating files."""
    journal = layout_metadata(root) / "directory-migration.json"
    return journal if journal.is_file() else None


def is_goal_record_name(name: str) -> bool:
    """Whether *name* has the form of a Goal id, as opposed to ``history`` or ``locks``."""
    match = GOAL_ID_PATTERN.fullmatch(name)
    return match is not None and len(match["slug"]) <= GOAL_SLUG_MAX_LENGTH
