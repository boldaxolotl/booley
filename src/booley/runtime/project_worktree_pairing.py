"""Pair a user worktree with committed inputs from its standalone Project."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from booley.runtime.file_lock import release_file_lock, wait_for_file_lock
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.incontainer_git_identity import apply_git_identity, load_git_identity
from booley.runtime.project_dir import PROJECT_DIR_NAME
from booley.runtime.project_repositories import (
    common_git_dir,
    git_directories,
    is_standalone_git_repository,
    parse_porcelain_z,
    run_git,
)
from booley.runtime.worktree_paths import worktree_creation_config


class ProjectPairingError(RuntimeError):
    """A user worktree cannot preserve committed Project inputs."""


def project_pairing_source(project_root: Path) -> Path | None:
    """Select a standalone Project; refuse creation from a paired checkout."""
    source = project_root / PROJECT_DIR_NAME
    if (source / ".git").is_file():
        raise ProjectPairingError(
            "cannot create a worktree from a paired Project checkout; "
            "run booley worktree new from the primary workspace"
        )
    return source if is_standalone_git_repository(source) else None


def _require_git(repository: Path, *args: str) -> str:
    result = run_git(repository, *args)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ProjectPairingError(f"git {' '.join(args)} failed in {repository}: {detail}")
    return result.stdout


def _require_base(source: Path, branch: str) -> str:
    head = run_git(source, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode:
        raise ProjectPairingError("commit the Project repository first")
    status = _require_git(
        source, "status", "--porcelain", "-z", "--untracked-files=all", "--ignore-submodules"
    )
    changes = parse_porcelain_z(status)
    if changes:
        paths = ", ".join(change.path for change in changes)
        raise ProjectPairingError(
            f"Project repository has uncommitted changes: {paths}; "
            f"commit them in `{PROJECT_DIR_NAME}` first"
        )
    if run_git(source, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        raise ProjectPairingError(f"Project branch {branch!r} already exists; choose another name")
    return head.stdout.strip()


@contextmanager
def _project_lock(source: Path) -> Iterator[None]:
    common = common_git_dir(source)
    if common is None:
        raise ProjectPairingError(f"cannot find Project Git common directory: {source}")
    lock = common / "booley" / "project-worktree.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        wait_for_file_lock(handle, timeout_s=30)
        try:
            yield
        finally:
            release_file_lock(handle)


def _remove_copy(worktree: Path) -> Path:
    nested = worktree / PROJECT_DIR_NAME
    if nested.is_symlink() or not nested.resolve().is_relative_to(worktree.resolve()):
        raise ProjectPairingError(f"Project copy escapes its containing worktree: {nested}")
    if (nested / ".git").exists():
        raise ProjectPairingError(f"refusing to replace a versioned Project checkout: {nested}")
    safe_rmtree(nested)
    return nested


def _configure_checkout(source: Path, nested: Path) -> None:
    _require_git(source, "config", "extensions.worktreeConfig", "true")
    relative = os.path.relpath(nested.resolve(), git_directories(nested).git_dir).replace(
        os.sep, "/"
    )
    for key, value in (
        ("core.worktree", relative),
        ("core.autocrlf", "false"),
        ("submodule.recurse", "false"),
        ("diff.ignoreSubmodules", "all"),
        ("status.submoduleSummary", "false"),
    ):
        _require_git(nested, "config", "--worktree", key, value)
    _require_git(nested, "checkout", "-f")
    apply_git_identity(nested, load_git_identity(source))


def _rollback_pair(source: Path, nested: Path, branch: str) -> None:
    result = run_git(source, "worktree", "remove", "--force", str(nested))
    if result.returncode:
        safe_rmtree(nested)
    _require_git(source, "worktree", "prune")
    if run_git(source, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        _require_git(source, "branch", "-D", branch)


def pair_project_worktree(project_root: Path, worktree: Path, name: str) -> bool:
    """Replace the script's snapshot with a new Project branch, rolling back failures."""
    source = project_pairing_source(project_root)
    if source is None:
        return False
    branch = f"booley-worktree/{name}"
    with _project_lock(source):
        base = _require_base(source, branch)
        nested = _remove_copy(worktree)
        try:
            _require_git(
                source,
                "-c",
                "submodule.recurse=false",
                *worktree_creation_config(project_root),
                "worktree",
                "add",
                "-b",
                branch,
                str(nested),
                base,
            )
            _configure_checkout(source, nested)
        except (OSError, RuntimeError, ValueError) as exc:
            try:
                _rollback_pair(source, nested, branch)
            except (OSError, RuntimeError, ValueError) as cleanup:
                raise ProjectPairingError(f"{exc}; paired rollback failed: {cleanup}") from exc
            raise
    return True


def rollback_outer_worktree(project_root: Path, worktree: Path) -> None:
    """Remove the newly created outer checkout after paired rollback completes."""
    result = run_git(project_root, "worktree", "remove", "--force", str(worktree))
    if result.returncode:
        safe_rmtree(worktree)
    _require_git(project_root, "worktree", "prune")
