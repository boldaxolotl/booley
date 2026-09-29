"""Refuse to run on a Ticket Board still in the pre-ADR-0065 layout.

Two leftovers of the old layout make current board code wrong rather than
merely untidy, and there is no automatic migration (Projects migrate by hand):

- documents under ``board/<state>/`` are invisible, because current code reads
  only ``board/<slug>.md`` and the state record beside it, so the board looks
  empty and the runner idles;
- files Git still tracks under ``tickets/board/`` or ``tickets/state/`` are
  live state the ignore patterns cannot hide, so every transition leaves
  changes in the checkout that block the next Ticket's completion.

Doctor reports each problem as a FAIL; board commands and ``booley run``
raise :class:`LegacyBoardLayoutError` before touching anything.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.paths import changelog_path

from .board_layout import (
    HISTORY_DIR_NAME,
    STATE_DIR_NAME,
    board_root,
    legacy_state_directories,
)
from .lifecycle import BOARD_DIR_NAME

logger = logging.getLogger(__name__)

MIGRATION_GUIDE = (
    "https://github.com/boldaxolotl/Booley/blob/main/docs/user/USAGE.md"
    "#migrating-a-ticket-board-to-state-records"
)

_GIT_TIMEOUT_SECONDS = 10


class LegacyBoardLayoutError(RuntimeError):
    """The Ticket Board needs a manual migration before any board command runs."""


@dataclass(frozen=True)
class LayoutProblem:
    """One reason the board is not in the current layout, with its fix."""

    summary: str
    fix: str


def legacy_state_files(tickets_dir: Path) -> list[Path]:
    """Return every file under the old ``board/<state>/`` directories, sorted.

    Empty leftover directories hold no Ticket and are not reported.
    """
    files: list[Path] = []
    for directory in legacy_state_directories(tickets_dir):
        files.extend(path for path in directory.rglob("*") if path.is_file())
    return sorted(files)


def tracked_live_state_files(tickets_dir: Path) -> list[str]:
    """Return files Git tracks under ``board/`` or ``state/``, relative to *tickets_dir*.

    Reads the index, so a tracked file already deleted from disk still counts:
    the next commit would keep it. Returns an empty list when *tickets_dir* is
    not in a Git repository or Git cannot run; the guard cannot see the index
    then (a failure other than "not a repository" is logged), and the
    legacy-directory check still applies.
    """
    if not tickets_dir.is_dir():
        return []
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", BOARD_DIR_NAME, STATE_DIR_NAME],
            cwd=tickets_dir,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Cannot check tracked Ticket state in %s: %s", tickets_dir, exc)
        return []
    if result.returncode != 0:
        if "not a git repository" not in result.stderr:
            logger.warning(
                "Cannot check tracked Ticket state in %s: git ls-files exited %d: %s",
                tickets_dir,
                result.returncode,
                result.stderr.strip(),
            )
        return []
    return sorted(name for name in result.stdout.split("\0") if name)


def legacy_layout_problems(tickets_dir: Path) -> list[LayoutProblem]:
    """Return what keeps *tickets_dir* from the current board layout (empty: none)."""
    problems: list[LayoutProblem] = []

    stranded = legacy_state_files(tickets_dir)
    if stranded:
        board = board_root(tickets_dir)
        holders = sorted({path.relative_to(board).parts[0] for path in stranded})
        names = ", ".join(f"{BOARD_DIR_NAME}/{name}/" for name in holders)
        verb = "holds" if len(holders) == 1 else "hold"
        problems.append(
            LayoutProblem(
                summary=(
                    f"{names} {verb} {len(stranded)} file(s) in the old per-state layout; "
                    "Booley cannot see these Tickets"
                ),
                fix=(
                    f"move each document to {BOARD_DIR_NAME}/<slug>.md with a "
                    f"{STATE_DIR_NAME}/<slug>.json record (done and archived Tickets go "
                    f"to {HISTORY_DIR_NAME}/), then delete the old directories"
                ),
            )
        )

    tracked = tracked_live_state_files(tickets_dir)
    if tracked:
        problems.append(
            LayoutProblem(
                summary=(
                    f"Git tracks {len(tracked)} file(s) under {BOARD_DIR_NAME}/ or "
                    f"{STATE_DIR_NAME}/; live Ticket state must stay untracked"
                ),
                fix=f"{_untrack_command(tickets_dir)} && commit the removal",
            )
        )
    return problems


def _untrack_command(tickets_dir: Path) -> str:
    """Return the shell command that stops Git tracking live Ticket state.

    ``-C <tickets>`` runs it in the repository that tracks the files, as the
    check does, even when a stealth ``.booley_project`` is its own repository
    nested in the Project's. ``--ignore-unmatch``: an old board never tracked
    ``state/``, and ``git rm`` otherwise refuses the whole command over the
    unmatched path.
    """
    return (
        f"git -C {shlex.quote(str(tickets_dir))} rm -r --cached --ignore-unmatch"
        f" -- {BOARD_DIR_NAME} {STATE_DIR_NAME}"
    )


def migration_pointer() -> str:
    """Return where the manual migration steps are written down."""
    return f"see {MIGRATION_GUIDE} and the release notes in {changelog_path()}"


def require_current_layout(tickets_dir: Path) -> None:
    """Raise :class:`LegacyBoardLayoutError` unless the board is in the current layout."""
    problems = legacy_layout_problems(tickets_dir)
    if not problems:
        return
    lines = [f"Ticket Board at {tickets_dir} needs a manual migration to state records:"]
    lines.extend(f"  - {problem.summary}\n    fix: {problem.fix}" for problem in problems)
    lines.append(f"Migration steps: {migration_pointer()}")
    raise LegacyBoardLayoutError("\n".join(lines))
