"""Pair a user worktree with committed inputs from its standalone Project."""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from booley.runtime.file_lock import release_file_lock, try_file_lock, wait_for_file_lock
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.incontainer_git_identity import (
    GitIdentityError,
    apply_git_identity,
    load_git_identity,
)
from booley.runtime.project_dir import PROJECT_DIR_NAME
from booley.runtime.project_gitignore import is_project_transient_path
from booley.runtime.project_repositories import (
    GitDirectoryInspectionError,
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
        transient = [change.path for change in changes if is_project_transient_path(change.path)]
        inputs = [change.path for change in changes if not is_project_transient_path(change.path)]
        remedies = []
        if transient:
            remedies.append(
                f"transient state: {', '.join(transient)}; Project .gitignore is missing "
                "current Booley patterns; rerun booley init, then commit the updated .gitignore"
            )
        if inputs:
            remedies.append(
                f"inputs: {', '.join(inputs)}; commit them in `{PROJECT_DIR_NAME}` first"
            )
        raise ProjectPairingError(
            "Project repository has uncommitted changes; " + "; ".join(remedies)
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
    # Like the outer script, keep this repository capability enabled across
    # removals/rollback. Enabling it is idempotent; checkout settings and identity
    # remain in config.worktree, and the primary checkout keeps its shared settings.
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


def pair_project_worktree(
    project_root: Path, worktree: Path, name: str, *, source: Path | None
) -> bool:
    """Replace the script's snapshot with a new Project branch, rolling back failures."""
    if source is None:
        return False
    branch = f"booley-worktree/{name}"
    with _project_lock(source):
        base = _require_base(source, branch)
        nested = _remove_copy(worktree)
        try:
            _require_git(source, "config", "gc.worktreePruneExpire", "never")
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
        except BaseException as exc:
            try:
                _rollback_pair(source, nested, branch)
            except (OSError, ProjectPairingError) as cleanup:
                exc.add_note(f"paired rollback failed: {cleanup}")
                if isinstance(exc, Exception):
                    raise ProjectPairingError(f"{exc}; paired rollback failed: {cleanup}") from exc
            if isinstance(exc, (GitIdentityError, GitDirectoryInspectionError, ValueError)):
                raise ProjectPairingError(str(exc)) from exc
            raise
    return True


def rollback_outer_worktree(project_root: Path, worktree: Path) -> None:
    """Remove the newly created outer checkout after paired rollback completes."""
    result = run_git(project_root, "worktree", "remove", "--force", str(worktree))
    if result.returncode:
        safe_rmtree(worktree)
    _require_git(project_root, "worktree", "prune")


def validate_project_pairing(source: Path | None, name: str) -> None:
    """Refuse invalid Project inputs before paying for the outer checkout."""
    if source is not None:
        _require_base(source, f"booley-worktree/{name}")


def worktree_removal_instructions(project_root: Path, worktree: Path, name: str) -> str:
    """Format absolute inner/outer removal and explicit Project branch cleanup."""
    root, outer = project_root.resolve(), worktree.resolve()
    source = root / PROJECT_DIR_NAME

    def quote(path: Path) -> str:
        return shlex.quote(str(path))

    return (
        f"Remove the paired Project first: git -C {quote(source)} worktree remove {quote(outer / PROJECT_DIR_NAME)}\n"
        f"Then remove the outer worktree: git -C {quote(root)} worktree remove {quote(outer)}\n"
        f"To reuse the name, delete the Project branch after preserving its commits: "
        f"git -C {quote(source)} branch -D {shlex.quote(f'booley-worktree/{name}')}"
    )


def remove_creation_lock(project_root: Path, name: str) -> None:
    """Remove this failed creation's per-name file only when it is unlocked."""
    locks = project_root / PROJECT_DIR_NAME / "worktrees" / ".locks"
    path = locks / f"{name}.lock"
    if not path.is_file() or (locks / f"{name}.mkdir.lock").exists():
        return
    with path.open("r+", encoding="utf-8") as handle, try_file_lock(handle) as acquired:
        if acquired and os.name != "nt":
            path.unlink()
    if acquired and os.name == "nt":
        path.unlink(missing_ok=True)
