"""Pair a user worktree with committed inputs from its Versioned Booley Directory."""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.booley_dir import own_booley_dir
from booley.runtime.docs_links import sandbox_doc_path
from booley.runtime.file_lock import release_file_lock, wait_for_file_lock
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.incontainer_git_identity import (
    GitIdentityError,
    apply_git_identity,
    load_git_identity,
)
from booley.runtime.project_repositories import (
    GitDirectoryInspectionError,
    common_git_dir,
    git_directories,
    is_standalone_git_repository,
    parse_porcelain_z,
    run_git,
)
from booley.runtime.worktree_paths import worktree_creation_config

_REMOVAL_DOC = f'{sandbox_doc_path("WORKTREES.md")} → "Removing a worktree"'


class ProjectPairingError(RuntimeError):
    """A user worktree cannot preserve committed Project inputs."""


@dataclass(frozen=True)
class ProjectPairing:
    """A created Paired Checkout and the uncommitted source paths it left out."""

    left_out: tuple[str, ...]


def refuse_paired_checkout(booley_dir: Path) -> None:
    """Refuse pairing when *booley_dir* is a Paired Checkout (its ``.git`` is a file)."""
    if (booley_dir / ".git").is_file():
        raise ProjectPairingError(
            "cannot pair a worktree with a Paired Checkout; "
            "run booley worktree prepare in a worktree of the main checkout"
        )


def project_pairing_source(project_root: Path) -> Path | None:
    """Select a Versioned Booley Directory; refuse creation from a Paired Checkout."""
    source = own_booley_dir(project_root)
    refuse_paired_checkout(source)
    if (source / ".git").is_dir():
        # Git skips an unusable .git directory and keeps searching upward, so a
        # broken Booley Directory's repository would otherwise resolve to the outer one.
        resolved = Path(_require_git(source, "rev-parse", "--absolute-git-dir").strip())
        if resolved.resolve() != (source / ".git").resolve():
            raise ProjectPairingError(
                f"cannot read the Booley Directory's repository at {source}: Git resolved {resolved}"
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
        raise ProjectPairingError("commit the Booley Directory's repository first")
    _require_git(source, "check-ref-format", "--branch", branch)
    if run_git(source, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        repository = f"git -C {shlex.quote(str(source))}"
        raise ProjectPairingError(
            f"branch {shlex.quote(branch)} already exists in the Booley Directory's repository; "
            f"merge it ({repository} merge {shlex.quote(branch)}) or delete it "
            f"({repository} branch -D {shlex.quote(branch)}), then retry"
        )
    return head.stdout.strip()


def _left_out_paths(source: Path) -> tuple[str, ...]:
    """Uncommitted and untracked paths of *source*: what a checkout of its HEAD omits."""
    status = _require_git(
        source, "status", "--porcelain", "-z", "--untracked-files=all", "--ignore-submodules"
    )
    return tuple(change.path for change in parse_porcelain_z(status))


def _refuse_registered(source: Path, nested: Path) -> None:
    """Refuse when the Project already registers a checkout at *nested*.

    Rollback removes the registration at that path, so it must only ever be one
    this run created (for example, not a Ticket checkout whose directory is gone).
    """
    listing = _require_git(source, "worktree", "list", "--porcelain")
    target = nested.resolve()
    for line in listing.splitlines():
        if line.startswith("worktree ") and Path(line[len("worktree ") :]).resolve() == target:
            raise ProjectPairingError(
                f"the Booley Directory's repository already registers a checkout at {nested}; "
                "in the same Sandbox, preserve needed work and evidence first "
                f"as described in {_REMOVAL_DOC}, then remove it with "
                f"`git -C {shlex.quote(str(source.resolve()))} worktree remove "
                f"{shlex.quote(str(target))}`; inspect locks before unlocking or forcing removal"
            )


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
    nested = own_booley_dir(worktree)
    if nested.is_symlink() or not nested.resolve().is_relative_to(worktree.resolve()):
        raise ProjectPairingError(
            f"Worktree Booley Directory escapes its containing worktree: {nested}"
        )
    if (nested / ".git").exists():
        raise ProjectPairingError(
            f"refusing to replace a Booley Directory with a Git checkout: {nested}"
        )
    if nested.exists():
        safe_rmtree(nested)
    return nested


def _configure_checkout(source: Path, nested: Path) -> None:
    # Like the outer worktree's setup, keep this repository capability enabled across
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


def remove_worktree_registration(repository: Path, worktree: Path) -> None:
    """Remove *worktree* from *repository*: its files and its own administrative entry.

    Idempotent: a worktree that is already gone is a no-op, and one whose
    directory is gone loses only its entry. Locks do not stop it.
    """
    result = run_git(repository, "worktree", "remove", "--force", "--force", str(worktree))
    if not result.returncode:
        return
    # An interrupted add may leave only its administrative entry. Never prune
    # other missing checkouts: users may still intend to restore those paths.
    if worktree.exists():
        safe_rmtree(worktree)
    common = common_git_dir(repository)
    if common is None:
        raise ProjectPairingError(f"cannot find Git common directory: {repository}")
    registrations = common / "worktrees"
    if not registrations.is_dir():
        return
    for admin in registrations.iterdir():
        pointer = admin / "gitdir"
        if admin.is_symlink() or not pointer.is_file():
            continue
        target = Path(pointer.read_text(encoding="utf-8").strip())
        if not target.is_absolute():
            target = admin / target
        if target.resolve() == (worktree / ".git").resolve():
            safe_rmtree(admin)


def _rollback_pair(source: Path, nested: Path, branch: str, base: str) -> None:
    """Remove this run's registration and branch.

    ``worktree add -b`` creates the branch before checking out, so a failed or
    interrupted add can leave it behind. The locked pre-check proved the branch
    absent, so a branch still at *base* is this run's; a moved one is kept.
    """
    remove_worktree_registration(source, nested)
    tip = run_git(source, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if tip.returncode == 0 and tip.stdout.strip() == base:
        _require_git(source, "branch", "-D", branch)


def pair_project_worktree(
    project_root: Path, worktree: Path, name: str, *, source: Path | None
) -> ProjectPairing | None:
    """Replace the script's snapshot with a new Project branch, rolling back failures.

    The checkout is made from the source's HEAD commit, so uncommitted and
    untracked files are left out; they are returned for the caller to report.
    ``None`` means the Project has no Versioned Booley Directory to pair.
    """
    if source is None:
        return None
    branch = f"booley-worktree/{name}"
    with _project_lock(source):
        base = _require_base(source, branch)
        left_out = _left_out_paths(source)
        _refuse_registered(source, own_booley_dir(worktree))
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
            _require_git(source, "config", f"branch.{branch}.booleyBase", base)
            _configure_checkout(source, nested)
        except BaseException as exc:
            try:
                _rollback_pair(source, nested, branch, base)
            except (OSError, ProjectPairingError) as cleanup:
                exc.add_note(f"paired rollback failed: {cleanup}")
                if isinstance(exc, Exception):
                    raise ProjectPairingError(f"{exc}; paired rollback failed: {cleanup}") from exc
            if isinstance(exc, (GitIdentityError, GitDirectoryInspectionError, ValueError)):
                raise ProjectPairingError(str(exc)) from exc
            raise
    return ProjectPairing(left_out)


def rollback_project_pairing(source: Path, worktree: Path, name: str) -> None:
    """Undo :func:`pair_project_worktree` after a later creation step failed.

    The recorded base marks the branch as this run's; a missing record keeps the
    branch (only the registration goes), as :func:`_rollback_pair` does for a moved one.
    """
    branch = f"booley-worktree/{name}"
    key = f"branch.{branch}.booleyBase"
    with _project_lock(source):
        recorded = run_git(source, "config", "--get", key)
        base = recorded.stdout.strip() if recorded.returncode == 0 else ""
        _rollback_pair(source, own_booley_dir(worktree), branch, base)
        # Deleting the branch already dropped the key; a kept branch still carries it.
        run_git(source, "config", "--unset", key)


def kept_paired_branches(source: Path) -> list[str]:
    """Name each ``booley-worktree/*`` branch of *source* that no registered checkout has."""
    return list(_paired_branches_without_checkout(source))


def _paired_branches_without_checkout(source: Path) -> dict[str, bool]:
    """Map each such branch to whether all its commits are on the branch *source* has checked out.

    Every registration Git lists counts as a checkout, a prunable or locked one too;
    an unreadable branch counts as unmerged, and so does every branch while *source*
    is detached, because no branch then holds the commits.
    """
    listing = _require_git(source, "worktree", "list", "--porcelain")
    registered = {line.removeprefix("branch ") for line in listing.split("\n")}
    refs = _require_git(
        source, "for-each-ref", "--format=%(refname)", "refs/heads/booley-worktree/"
    )
    attached = run_git(source, "symbolic-ref", "--quiet", "HEAD").returncode == 0
    return {
        ref.removeprefix("refs/heads/"): attached
        and run_git(source, "merge-base", "--is-ancestor", ref, "HEAD").returncode == 0
        for ref in refs.split("\n")
        if ref and ref not in registered
    }


def prune_missing_checkouts(source: Path) -> None:
    """Drop *source*'s registrations whose folders are gone; Git keeps locked ones.

    An explicit prune ignores ``gc.worktreePruneExpire=never``, which pairing sets.
    Then each ``booley-worktree/*`` branch no checkout has is deleted when all its
    commits are on the branch checked out in *source*; ``git branch -d`` refuses
    any other, and Doctor reports what stays.

    Raises:
        ProjectPairingError: a folder still exists but lost its ``.git`` pointer
            (a cut-short removal), so Git would drop its index with the registration.
    """
    with _project_lock(source):
        folder = ""
        for line in _require_git(source, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                folder = line.removeprefix("worktree ")
            elif line.startswith("prunable") and Path(folder).exists():
                raise ProjectPairingError(
                    f"{folder} is registered but lost its .git file; "
                    "remove that folder or restore the file, then run prepare again"
                )
        _require_git(source, "worktree", "prune")
        for branch, merged in _paired_branches_without_checkout(source).items():
            if merged:
                run_git(source, "branch", "-d", branch)
