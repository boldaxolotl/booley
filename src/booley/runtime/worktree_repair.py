"""Repair linked metadata only after proving its owner, ref and raw association."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.worktree_paths import relative_worktree_paths, worktree_state_dir
from booley.runtime.worktree_relocation import refresh_relative_worktree_config


class WorktreeRepairError(RuntimeError):
    """Existing linked metadata cannot be safely reconciled."""


@dataclass(frozen=True)
class _Proof:
    owner: Path
    checkout: Path
    administration: Path
    ref: str


def _git(owner: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(owner), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeRepairError(f"worktree repair Git failed: {exc}") from exc
    if result.returncode:
        raise WorktreeRepairError(f"worktree repair Git failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise WorktreeRepairError(f"unreadable worktree metadata {path}: {exc}") from exc


def _association_paths(owner: Path, checkout: Path, common: Path) -> tuple[Path, ...]:
    """Known old mount spellings plus owner-recorded primary checkout spelling."""
    primary_line = _git(owner, "worktree", "list", "--porcelain").splitlines()[0]
    primary = Path(primary_line.removeprefix("worktree "))
    paths = [checkout]
    with contextlib.suppress(ValueError):
        paths.append(primary / checkout.relative_to(owner))
    state = worktree_state_dir(owner)
    try:
        suffix = checkout.relative_to(state)
    except ValueError:
        return tuple(paths)
    if state.is_relative_to(owner):
        paths.extend((Path("/booley-project") / suffix, Path("/work/.booley_project") / suffix))
    return tuple(paths)


def _prove(owner: Path, checkout: Path, ref: str) -> _Proof:
    detached = re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref) is not None
    if not ref.startswith("refs/heads/") and not detached:
        raise WorktreeRepairError(f"expected a full Ticket ref or recorded commit, got {ref!r}")
    raw = _text(checkout / ".git")
    if not raw.startswith("gitdir: "):
        raise WorktreeRepairError(f"malformed worktree gitfile: {checkout}")
    pointer = Path(raw.removeprefix("gitdir: "))
    common = Path(_git(owner, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    admin = common / "worktrees" / pointer.name
    if not admin.is_dir():
        raise WorktreeRepairError(f"lost worktree registration: {admin}; do not prune")
    if _text(admin / "HEAD") != (ref if detached else f"ref: {ref}"):
        raise WorktreeRepairError(f"worktree owner ref does not match {ref}: {admin}")
    if (admin / _text(admin / "commondir")).resolve() != common.resolve():
        raise WorktreeRepairError(f"foreign common Git directory: {admin}")
    candidates = _association_paths(owner, checkout, common)
    associated = any(
        Path(os.path.normpath(candidate / pointer)).resolve() == admin.resolve()
        for candidate in candidates
    )
    known_admin = Path("/work/.git/worktrees") / admin.name
    known_pair = Path("/work/.booley_project/.git/worktrees") / admin.name
    known_alias_pair = Path("/booley-project/.git/worktrees") / admin.name
    associated = associated or any(
        Path(os.path.normpath(candidate / pointer)) == known_admin for candidate in candidates
    )
    if owner == worktree_state_dir(owner):
        associated = (
            associated
            or pointer in {known_pair, known_alias_pair}
            or any(
                Path(os.path.normpath(candidate / pointer)) in {known_pair, known_alias_pair}
                for candidate in candidates
            )
        )
    if not associated and pointer != known_admin:
        raise WorktreeRepairError(f"foreign or unassociated worktree gitfile: {checkout}")
    reverse = Path(_text(admin / "gitdir"))
    backlink = reverse if reverse.is_absolute() else Path(os.path.normpath(admin / reverse))
    if backlink not in tuple(candidate / ".git" for candidate in candidates):
        raise WorktreeRepairError(f"foreign or unassociated reverse worktree link: {admin}")
    matches = [
        path
        for path in (common / "worktrees").iterdir()
        if path.is_dir() and _text(path / "HEAD") == f"ref: {ref}"
    ]
    if not detached and matches != [admin]:
        raise WorktreeRepairError(f"ambiguous owner registration for ref {ref}")
    return _Proof(owner, checkout, admin, ref)


def _metadata_flag(relative: bool) -> tuple[str, ...]:
    match = re.match(r"git version (\d+)\.(\d+)", _git(Path.cwd(), "--version"))
    if match is None or tuple(map(int, match.groups())) < (2, 48):
        return ()
    return ("--relative-paths" if relative else "--no-relative-paths",)


def repair_ticket_worktree(owner: Path, checkout: Path, ref: str) -> None:
    """Reconcile a known Ticket checkout without reconstructing lost Git state.

    Callers hold the existing Ticket/repository mutation lock. Relative repair
    in a Sandbox requires its immutable alias layout; old layouts are deferred.
    """
    if not ref.startswith("refs/heads/"):
        raise WorktreeRepairError(f"expected a recorded Ticket ref, got {ref!r}")
    owner, checkout = owner.resolve(), checkout.resolve()
    state = worktree_state_dir(owner)
    if not checkout.is_relative_to(state / "worktrees"):
        raise WorktreeRepairError(f"worktree outside the selected Ticket root: {checkout}")
    if owner == Path("/work") and not relative_worktree_paths(owner):
        raise WorktreeRepairError(
            "regenerate and recreate the compatible Sandbox before worktree repair"
        )
    proof = _prove(owner, checkout, ref)
    _apply_proof(proof, relative=relative_worktree_paths(owner))


def _apply_proof(proof: _Proof, *, relative: bool) -> None:
    _git(proof.owner, "config", "gc.worktreePruneExpire", "never")
    _git(proof.owner, "worktree", "repair", *_metadata_flag(relative), str(proof.checkout))
    refresh_relative_worktree_config(proof.checkout)
    identity = (
        _git(proof.checkout, "symbolic-ref", "HEAD")
        if proof.ref.startswith("refs/heads/")
        else _git(proof.checkout, "rev-parse", "HEAD")
    )
    if identity != proof.ref:
        raise WorktreeRepairError(f"repaired worktree ref identity changed: {proof.checkout}")
    common = _git(proof.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if Path(common).resolve() != proof.administration.parent.parent.resolve():
        raise WorktreeRepairError(f"repaired worktree owner identity changed: {proof.checkout}")
    if Path(_git(proof.checkout, "rev-parse", "--show-toplevel")).resolve() != proof.checkout:
        raise WorktreeRepairError(f"repaired worktree checkout identity changed: {proof.checkout}")


def repair_ticket_workspace(
    root: Path, checkout: Path, outer_ref: str, project_ref: str | None = None
) -> None:
    """Preflight both owner associations before repairing a paired Ticket tree."""
    from booley.runtime.project_dir import resolve_checkout_project_dir
    from booley.runtime.project_repositories import resolve_inner_project_repo

    root, checkout = root.resolve(), checkout.resolve()
    if not checkout.is_relative_to(worktree_state_dir(root) / "worktrees"):
        raise WorktreeRepairError(
            f"Ticket checkout is outside its selected worktree root: {checkout}"
        )
    if root == Path("/work") and not relative_worktree_paths(root):
        raise WorktreeRepairError(
            "regenerate and recreate the compatible Sandbox before worktree repair"
        )
    from booley.core.file_lock import release_file_lock, wait_for_file_lock

    lock_path = worktree_state_dir(root) / "worktrees/.locks/_parent_git.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        wait_for_file_lock(handle, timeout_s=30)
        try:
            proofs = [_prove(root, checkout, outer_ref)]
            paired = resolve_checkout_project_dir(checkout)
            if paired.is_relative_to(checkout) and (paired / ".git").is_file():
                owner = resolve_inner_project_repo(root)
                if owner is None or project_ref is None:
                    raise WorktreeRepairError(
                        "paired worktree repair requires its recorded owner and Ticket ref"
                    )
                proofs.append(_prove(owner.resolve(), paired.resolve(), project_ref))
            for proof in proofs:
                _apply_proof(proof, relative=relative_worktree_paths(root))
        finally:
            release_file_lock(handle)


def repair_acceptance_worktrees(root: Path, rows: tuple[tuple[Path, Path, str], ...]) -> None:
    """Preflight transaction-owned detached candidates before replay cleanup."""
    from booley.core.file_lock import release_file_lock, wait_for_file_lock

    root = root.resolve()
    state = worktree_state_dir(root)
    lock_path = state / "worktrees/.locks/_parent_git.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        wait_for_file_lock(handle, timeout_s=30)
        try:
            proofs = []
            for owner, recorded_checkout, prepared_sha in rows:
                checkout = recorded_checkout.resolve()
                if not checkout.is_relative_to(state / ".runtime/acceptance-worktrees"):
                    raise WorktreeRepairError(
                        f"retained acceptance checkout is outside its journal root: {checkout}"
                    )
                raw = _text(checkout / ".git")
                if not raw.startswith("gitdir: "):
                    raise WorktreeRepairError(f"malformed retained candidate gitfile: {checkout}")
                admin_name = Path(raw.removeprefix("gitdir: ")).name
                common = Path(
                    _git(owner, "rev-parse", "--path-format=absolute", "--git-common-dir")
                )
                head = _text(common / "worktrees" / admin_name / "HEAD")
                proof = _prove(owner.resolve(), checkout, head)
                if head.startswith("ref: "):
                    raise WorktreeRepairError(
                        "retained acceptance candidate is no longer detached"
                    )
                _git(owner, "merge-base", "--is-ancestor", prepared_sha, head)
                proofs.append(proof)
            for proof in proofs:
                _apply_proof(proof, relative=relative_worktree_paths(root))
        finally:
            release_file_lock(handle)
