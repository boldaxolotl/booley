"""``booley worktree list``: one read-only row of state per interactive worktree.

Interactive worktrees are the direct children of ``<Main Booley Directory>/worktrees/``
that Git registers. Every Git call runs with ``GIT_OPTIONAL_LOCKS=0`` so a status
read never takes ``index.lock`` from under a working agent, and at bytes level so
non-UTF-8 paths and file contents neither crash the listing nor change the bytes
``git patch-id`` hashes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booley.config.goals import quiet_after
from booley.core.booley_dir import (
    own_booley_dir_repo_path,
    resolve_common_booley_dir,
    resolve_main_checkout_root,
)
from booley.goals.store import GoalStore, GoalStoreError
from booley.mcp.session_registry import SessionRegistry, namespace
from booley.runtime.git_environment import inherited_git_environment
from booley.runtime.handles import HandleStore
from booley.runtime.project_repositories import parse_porcelain_z
from booley.runtime.timefmt import format_age
from booley.runtime.worktree_usage import WORKTREE_NAME, last_used

_GIT_TIMEOUT_S = 60

PAIRED: Literal["Paired Checkout"] = "Paired Checkout"
SNAPSHOT: Literal["Snapshot"] = "Snapshot"

_HEADER = (
    "NAME",
    "BRANCH",
    "BOOLEY DIR",
    "UNCOMMITTED",
    "UNMERGED",
    "SESSION",
    "GOAL",
    "LAST USED",
)


class WorktreeListError(RuntimeError):
    """A Git command needed to describe one worktree failed."""


@dataclass(frozen=True)
class WorktreeState:
    """What ``booley worktree list`` reports for one worktree."""

    name: str
    path: Path
    branch: str | None  # ``None``: detached HEAD
    kind: Literal["Paired Checkout", "Snapshot"]
    uncommitted: int
    paired_uncommitted: int | None
    unmerged: int
    paired_unmerged: int | None
    sessions: tuple[str, ...]
    goal: str | None
    last_used: float | None


# ---------------------------------------------------------------------------
# Git access
# ---------------------------------------------------------------------------


def _run(cwd: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    environment = inherited_git_environment()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_LITERAL_PATHSPECS"] = "1"
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=environment,
            input=stdin,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeListError(f"git {args[0]} could not run in {cwd}: {exc}") from exc


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Return stdout of a Git command that must succeed."""
    result = _run(cwd, *args, stdin=stdin)
    if result.returncode != 0:
        detail = " ".join(result.stderr.decode("utf-8", "replace").split())
        raise WorktreeListError(f"git {args[0]} failed in {cwd}: {detail}")
    return result.stdout


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _merge_target(repo: Path) -> str:
    """The branch ref ``repo`` has checked out, else its HEAD commit."""
    result = _run(repo, "symbolic-ref", "--quiet", "HEAD")
    if result.returncode == 0:
        return _text(result.stdout).strip()
    return _text(_git(repo, "rev-parse", "HEAD")).strip()


# ---------------------------------------------------------------------------
# Counting changes
# ---------------------------------------------------------------------------


def _count_uncommitted(repo: Path, excluded_untracked: str | None) -> int:
    """Count ``git status`` entries, skipping untracked ones under *excluded_untracked*."""
    changes = parse_porcelain_z(_text(_git(repo, "status", "--porcelain=v1", "-z")))
    count = 0
    for change in changes:
        under_excluded = excluded_untracked is not None and (
            change.path == excluded_untracked or change.path.startswith(excluded_untracked + "/")
        )
        if change.status == "??" and under_excluded:
            continue
        count += 1
    return count


def _patch_ids(repo: Path, patch: bytes) -> list[str]:
    output = _git(repo, "patch-id", "--stable", stdin=patch)
    return [line.split()[0] for line in _text(output).splitlines() if line.split()]


# Diff flags that make a patch independent of the user's Git configuration: colour
# codes or rename detection would change the text `git patch-id` hashes.
_PLAIN_DIFF = ("--no-color", "--no-ext-diff", "--no-renames")


def _squashed_commits(repo: Path, upstream: str, base: str) -> set[str]:
    """Commits on HEAD since *base* whose combined diff landed upstream as one commit.

    A squash commit may have been made from any point of the branch, so every
    commit since *base* (merges included) is tried as the squashed tip; the one
    with the most ancestors that match wins. A tip's ancestry, not its position in a
    date-ordered list, decides what it covers, so a side branch merged in later and
    a commit before a merge of main are never mistaken for squashed work.
    """
    paths = _git(repo, "diff", *_PLAIN_DIFF, "--name-only", "-z", base, "HEAD")
    pathspec = [item for item in _text(paths).split("\0") if item]
    if not pathspec:
        return set()
    landed = _git(
        repo, "log", "-p", *_PLAIN_DIFF, "--no-merges", f"{base}..{upstream}", "--", *pathspec
    )
    candidates = set(_patch_ids(repo, landed))
    if not candidates:
        return set()
    squashed: set[str] = set()
    for tip in _text(_git(repo, "rev-list", f"{base}..HEAD")).split():
        cumulative = _git(repo, "diff", *_PLAIN_DIFF, base, tip)
        if candidates.isdisjoint(_patch_ids(repo, cumulative)):
            continue
        covered = set(_text(_git(repo, "rev-list", f"{base}..{tip}")).split())
        if len(covered) > len(squashed):
            squashed = covered
    return squashed


def _count_unmerged(repo: Path, upstream: str) -> int:
    """Count commits on HEAD that are not on *upstream*, squash merges included.

    ``git cherry`` drops commits patch-equivalent to an upstream commit. A branch
    squash-merged as one commit is not patch-equivalent per commit, so the commits
    under the tip whose combined diff matches an upstream commit are dropped too;
    only ``+`` commits outside that set count.
    """
    cherry = _text(_git(repo, "cherry", upstream, "HEAD")).splitlines()
    unmerged = [line[2:] for line in cherry if line.startswith("+ ")]
    if not unmerged:
        return 0
    base_result = _run(repo, "merge-base", upstream, "HEAD")
    if base_result.returncode != 0:
        return len(unmerged)
    base = _text(base_result.stdout).strip()
    return len(set(unmerged) - _squashed_commits(repo, upstream, base))


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    """Facts shared by every worktree of one listing."""

    common: Path
    target: str
    sessions: dict[Path, list[str]]
    goals: GoalStore


def _registered_worktrees(main: Path) -> list[tuple[Path, str | None]]:
    """Registered worktrees after the main checkout as ``(path, branch ref or None)``."""
    raw = _git(main, "worktree", "list", "--porcelain", "-z")
    records: list[list[str]] = [[]]
    for field in _text(raw).split("\0"):
        if field:
            records[-1].append(field)
        elif records[-1]:
            records.append([])
    entries: list[tuple[Path, str | None]] = []
    for record in records[1:]:
        if not record or not record[0].startswith("worktree "):
            continue
        branch = next((f[7:] for f in record if f.startswith("branch ")), None)
        entries.append(
            (Path(os.fsdecode(record[0][9:].encode("utf-8", "surrogateescape"))), branch)
        )
    return entries


def _session_labels(common: Path, now: float, diagnostics: list[str]) -> dict[Path, list[str]]:
    """Session handles by resolved checkout path, as the Dashboard shows them."""
    registry = SessionRegistry(common, quiet_after=quiet_after(common))
    snapshot = registry.visible_snapshot(now=now, scope=namespace())
    handles, handle_diagnostics = HandleStore(common).snapshot()
    # A project that never ran a Session has no registry directory: not a problem.
    if registry.root.exists():
        diagnostics.extend(snapshot.diagnostics)
    diagnostics.extend(handle_diagnostics)
    labels: dict[Path, list[str]] = {}
    for row in snapshot.rows:
        key = row.attribution.key
        checkout = Path(row.attribution.work_dir).resolve()
        labels.setdefault(checkout, []).append(handles.get(key, key))
    return labels


def _active_goal(context: _Context, worktree: Path, diagnostics: list[str]) -> str | None:
    try:
        record = context.goals.active_for_worktree(worktree)
    except GoalStoreError as exc:
        diagnostics.append(f"Goal Mode of worktree {worktree.name} is unavailable: {exc}")
        return None
    return None if record is None else record.id


def _describe(
    context: _Context, path: Path, branch_ref: str | None, diagnostics: list[str]
) -> WorktreeState:
    booley_dir = own_booley_dir_repo_path(path)
    nested = path / booley_dir
    paired = (nested / ".git").is_file()
    paired_uncommitted = paired_unmerged = None
    if paired:
        paired_uncommitted = _count_uncommitted(nested, None)
        paired_unmerged = _count_unmerged(nested, _merge_target(context.common))
    return WorktreeState(
        name=path.name,
        path=path,
        branch=None if branch_ref is None else branch_ref.removeprefix("refs/heads/"),
        kind=PAIRED if paired else SNAPSHOT,
        uncommitted=_count_uncommitted(path, booley_dir.as_posix()),
        paired_uncommitted=paired_uncommitted,
        unmerged=_count_unmerged(path, context.target),
        paired_unmerged=paired_unmerged,
        sessions=tuple(sorted(context.sessions.get(path.resolve(), []))),
        goal=_active_goal(context, path, diagnostics),
        last_used=last_used(context.common, path.name),
    )


def collect_worktrees(project_root: Path, *, now: float) -> tuple[list[WorktreeState], list[str]]:
    """Describe every interactive worktree of the Project; return ``(states, diagnostics)``.

    Works from the main checkout or from any worktree. A registered worktree whose
    directory is gone is no row but is named in a diagnostic.
    """
    common = resolve_common_booley_dir(project_root)
    main = resolve_main_checkout_root(project_root)
    diagnostics: list[str] = []
    context = _Context(
        common=common,
        target=_merge_target(main),
        sessions=_session_labels(common, now, diagnostics),
        goals=GoalStore(common, lock_timeout_s=0),
    )
    container = (common / "worktrees").resolve()
    states: list[WorktreeState] = []
    for path, branch_ref in _registered_worktrees(main):
        if path.parent.resolve() != container or not WORKTREE_NAME.fullmatch(path.name):
            continue
        if not path.exists():
            diagnostics.append(f"worktree {path.name} is registered but its directory is missing")
            continue
        states.append(_describe(context, path, branch_ref, diagnostics))
    return sorted(states, key=lambda state: state.name), diagnostics


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def _paired_cell(count: int, paired: int | None) -> str:
    return str(count) if paired is None else f"{count}, paired {paired}"


def _row(state: WorktreeState, now: float) -> tuple[str, ...]:
    return (
        state.name,
        state.branch or "detached",
        state.kind,
        _paired_cell(state.uncommitted, state.paired_uncommitted),
        _paired_cell(state.unmerged, state.paired_unmerged),
        ", ".join(state.sessions) or "-",
        state.goal or "-",
        "never" if state.last_used is None else format_age(state.last_used, now=now),
    )


def render(states: list[WorktreeState], *, now: float) -> str:
    """Render the fixed-column table, or ``No worktrees.``."""
    if not states:
        return "No worktrees."
    rows = [_HEADER, *(_row(state, now) for state in states)]
    widths = [max(len(row[column]) for row in rows) for column in range(len(_HEADER))]
    lines = (
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        for row in rows
    )
    return "\n".join(line.rstrip() for line in lines)


def run(project_root: Path) -> int:
    """Print the table to stdout and diagnostics as warnings to stderr."""
    now = time.time()
    try:
        states, diagnostics = collect_worktrees(project_root, now=now)
    except WorktreeListError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(render(states, now=now))
    for diagnostic in diagnostics:
        print(f"WARNING: {diagnostic}", file=sys.stderr)
    return 0
