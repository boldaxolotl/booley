"""Where Goal Mode keeps its files (ADR 0067 D9).

Every Goal Record lives in the Project directory the caller resolved through
:mod:`booley.runtime.project_dir`; nothing here discovers or hardcodes it::

    <project dir>/goals/<goal-id>/record.json         the Goal Record
    <project dir>/goals/<goal-id>/lock                its record lock (D15)
    <project dir>/goals/<goal-id>/booley_state.json   Goal evidence state
    <project dir>/goals/<goal-id>/logs/               receipts and reviewer inputs
    <project dir>/goals/<goal-id>/.runtime/jobs/      Job records
    <project dir>/goals/<goal-id>/changes.jsonl       the Change Log
    <project dir>/goals/<goal-id>/SUMMARY.md          the Session Summary
    <project dir>/goals/<goal-id>/review-package.json the Review Package
    <project dir>/goals/locks/worktree-<hash>.lock    one worktree lock per worktree
    <checkout project dir>/goals/history/<goal-id>.md the committed summary

The committed summary is the one path in a *checkout's* own Project
directory: it is written inside the Goal worktree so it commits on the Goal
Branch. ``goals/*/`` is ignored and ``goals/history/`` kept by the Project
``.gitignore`` (:mod:`booley.runtime.project_gitignore`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from booley.goals.model import WorktreeIdentity
from booley.runtime.session_paths import RUNTIME_DIR, session_jobs_dir
from booley.runtime.timefmt import compact_utc_now

GOALS_DIR = "goals"
LOCKS_DIR = "locks"
HISTORY_DIR = "history"
RECORD_FILE = "record.json"
RECORD_LOCK_FILE = "lock"
STATE_FILE = "booley_state.json"
LOGS_DIR = "logs"
CHANGES_FILE = "changes.jsonl"
SUMMARY_FILE = "SUMMARY.md"
REVIEW_PACKAGE_FILE = "review-package.json"
# A record directory is built under this prefix and renamed into place.
STAGING_PREFIX = ".staging-"

# A Goal slug is what a human names the work; it becomes part of the Goal id
# and of the Goal Branch name, so it stays a short lowercase path segment.
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SLUG_MAX_LENGTH = 64
# ``<slug>-<compact UTC timestamp>``, see :func:`new_goal_id`.
GOAL_ID_PATTERN = re.compile(rf"(?P<slug>{SLUG_PATTERN.pattern})-\d{{8}}T\d{{6}}Z")


class GoalIdError(ValueError):
    """A Goal slug or Goal id does not have the required form."""


def validate_slug(slug: str) -> str:
    """Return *slug* when it is a valid Goal slug, else raise :class:`GoalIdError`."""
    if len(slug) > SLUG_MAX_LENGTH or not SLUG_PATTERN.fullmatch(slug):
        raise GoalIdError(
            f"Goal slug {slug!r} must be lowercase letters, digits, and single hyphens, "
            f"at most {SLUG_MAX_LENGTH} characters"
        )
    return slug


def new_goal_id(slug: str, *, timestamp: str | None = None) -> str:
    """Return ``<slug>-<compact UTC timestamp>`` for a new Goal Mode.

    *timestamp* defaults to :func:`booley.runtime.timefmt.compact_utc_now`.
    """
    goal_id = f"{validate_slug(slug)}-{timestamp or compact_utc_now()}"
    return validate_goal_id(goal_id)


def validate_goal_id(goal_id: str) -> str:
    """Return *goal_id* when it names a Goal Record, else raise :class:`GoalIdError`."""
    match = GOAL_ID_PATTERN.fullmatch(goal_id)
    if match is None or len(match["slug"]) > SLUG_MAX_LENGTH:
        raise GoalIdError(f"{goal_id!r} is not a Goal id (<slug>-<YYYYMMDDTHHMMSSZ>)")
    return goal_id


def goals_root(project_dir: Path) -> Path:
    """Directory holding every Goal Record of the Project."""
    return project_dir / GOALS_DIR


def locks_dir(project_dir: Path) -> Path:
    """Directory holding the Goal worktree locks."""
    return goals_root(project_dir) / LOCKS_DIR


def worktree_lock_file(project_dir: Path, identity: WorktreeIdentity) -> Path:
    """The worktree lock for *identity*, named by a digest of the identity (D15)."""
    digest = hashlib.sha256(identity.key.encode("utf-8")).hexdigest()
    return locks_dir(project_dir) / f"worktree-{digest}.lock"


def history_file(checkout_project_dir: Path, goal_id: str) -> Path:
    """The committed summary of *goal_id* inside a checkout's own Project directory."""
    return checkout_project_dir / GOALS_DIR / HISTORY_DIR / f"{validate_goal_id(goal_id)}.md"


@dataclass(frozen=True)
class GoalRecordPaths:
    """Every file of one Goal Record, rooted at its record directory."""

    root: Path

    @property
    def record_file(self) -> Path:
        """``record.json``: the Goal Record itself."""
        return self.root / RECORD_FILE

    @property
    def lock_file(self) -> Path:
        """The record lock held around every mutation of the record's files."""
        return self.root / RECORD_LOCK_FILE

    @property
    def state_file(self) -> Path:
        """The Goal Mode's ``booley_state.json``."""
        return self.root / STATE_FILE

    @property
    def logs_dir(self) -> Path:
        """Evidence receipts, reviewer inputs, and other run logs."""
        return self.root / LOGS_DIR

    @property
    def runtime_dir(self) -> Path:
        """Machine-owned runtime state of runs made for this Goal Mode."""
        return self.root / RUNTIME_DIR

    @property
    def jobs_dir(self) -> Path:
        """Job records of runs made for this Goal Mode."""
        return session_jobs_dir(self.runtime_dir)

    @property
    def changes_file(self) -> Path:
        """The append-only Change Log."""
        return self.root / CHANGES_FILE

    @property
    def summary_file(self) -> Path:
        """The Session Summary written at Finish."""
        return self.root / SUMMARY_FILE

    @property
    def review_package_file(self) -> Path:
        """The Review Package written at Finish."""
        return self.root / REVIEW_PACKAGE_FILE


def record_paths(project_dir: Path, goal_id: str) -> GoalRecordPaths:
    """The files of Goal Record *goal_id* in *project_dir*."""
    return GoalRecordPaths(goals_root(project_dir) / validate_goal_id(goal_id))
