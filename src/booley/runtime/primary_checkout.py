"""Locate the primary checkout that owns a repository's Git metadata."""

from __future__ import annotations

import os
from pathlib import Path

from booley.core.booley_layout import BooleyLayoutError, git_common_directory
from booley.runtime import platform_paths


def _git(root: Path, *args: str) -> str:
    """Run bounded, observational Git without index refresh or lazy fetch."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}
    result = platform_paths.run_captured_bounded(
        ["git", "-C", str(root), *args], timeout=30, env=env, errors="strict"
    )
    if result.returncode:
        raise BooleyLayoutError(f"git {' '.join(args)} in {root}: {result.stderr.strip()}")
    return result.stdout.strip()


def primary_checkout(repository: Path) -> Path:
    """A primary gitfile shares its admin/common directory; linked checkouts do not."""
    admin = Path(_git(repository, "rev-parse", "--absolute-git-dir")).resolve()
    common = Path(
        _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()
    if admin == common:
        return repository.resolve()
    raw = _git(repository, "worktree", "list", "--porcelain").splitlines()[0]
    candidate = Path(raw.removeprefix("worktree ")).resolve()
    if git_common_directory(candidate) == common:
        return candidate
    configured = platform_paths.run_captured_bounded(
        ["git", "--git-dir", str(common), "config", "--get", "core.worktree"],
        timeout=30,
        errors="strict",
    )
    if configured.returncode == 0:
        candidate = (common / configured.stdout.strip()).resolve()
        if git_common_directory(candidate) == common:
            return candidate
    raise BooleyLayoutError(
        "cannot locate the primary Project Root from this linked checkout; "
        f"its Git metadata is at {common}"
    )
