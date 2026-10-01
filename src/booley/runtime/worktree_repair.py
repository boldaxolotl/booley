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
    spellings = _administration_spellings(owner, common, admin)
    _prove_pointer(checkout, pointer, admin, candidates, spellings)
    _prove_backlink(admin, candidates, spellings)
    if not detached and _registrations_for_ref(common, ref) != [admin]:
        raise WorktreeRepairError(f"ambiguous owner registration for ref {ref}")
    return _Proof(owner, checkout, admin, ref)


def _administration_spellings(owner: Path, common: Path, admin: Path) -> tuple[Path, ...]:
    if common.resolve() != (owner / ".git").resolve():
        return (admin,)
    if owner == worktree_state_dir(owner):
        return (
            admin,
            Path("/work/.booley_project/.git/worktrees") / admin.name,
            Path("/booley-project/.git/worktrees") / admin.name,
        )
    return (admin, Path("/work/.git/worktrees") / admin.name)


def _prove_pointer(checkout, pointer, admin, candidates, spellings) -> None:
    targets = tuple(Path(os.path.normpath(candidate / pointer)) for candidate in candidates)
    if not any(target.resolve() == admin.resolve() or target in spellings for target in targets):
        raise WorktreeRepairError(f"foreign or unassociated worktree gitfile: {checkout}")


def _prove_backlink(admin, candidates, spellings) -> None:
    reverse = Path(_text(admin / "gitdir"))
    targets = tuple(Path(os.path.normpath(spelling / reverse)) for spelling in spellings)
    expected = tuple(candidate / ".git" for candidate in candidates)
    if not any(target in expected for target in targets):
        raise WorktreeRepairError(f"foreign or unassociated reverse worktree link: {admin}")


def _registrations_for_ref(common: Path, ref: str) -> list[Path]:
    matches = []
    for path in (common / "worktrees").iterdir():
        if not path.is_dir():
            continue
        # An incomplete unrelated registration has no recorded ref to collide.
        # The selected registration's mandatory metadata was validated above.
        with contextlib.suppress(WorktreeRepairError):
            if _text(path / "HEAD") == f"ref: {ref}":
                matches.append(path)
    return matches


def _metadata_flag(relative: bool) -> tuple[str, ...]:
    from booley.runtime.worktree_paths import _git_supports_relative_paths

    if not _git_supports_relative_paths():
        return ()
    return ("--relative-paths" if relative else "--no-relative-paths",)


def _metadata_healthy(proof: _Proof) -> bool:
    pointer = Path(_text(proof.checkout / ".git").removeprefix("gitdir: "))
    backlink = Path(_text(proof.administration / "gitdir"))
    return (proof.checkout / pointer).resolve() == proof.administration.resolve() and (
        proof.administration / backlink
    ).resolve() == (proof.checkout / ".git").resolve()


def _repair_proofs(root: Path, proofs: list[_Proof]) -> None:
    relative = relative_worktree_paths(root)
    if root == Path("/work") and not relative:
        for proof in proofs:
            pointer = Path(_text(proof.checkout / ".git").removeprefix("gitdir: "))
            backlink = Path(_text(proof.administration / "gitdir"))
            if not pointer.is_absolute() or not backlink.is_absolute():
                raise WorktreeRepairError(
                    "regenerate and recreate the compatible Sandbox before relative worktree repair"
                )
    needs_repair = [proof for proof in proofs if not _metadata_healthy(proof)]
    if root != Path("/work") and needs_repair:
        from booley.runtime.session_runtime import SessionError, assert_worktree_repair_safe

        try:
            assert_worktree_repair_safe(root)
        except SessionError as exc:
            raise WorktreeRepairError(str(exc)) from exc
    for proof in proofs:
        if proof in needs_repair:
            _apply_proof(proof, relative=relative)
        else:
            _git(proof.owner, "config", "gc.worktreePruneExpire", "never")


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
    from booley.core.file_lock import release_file_lock, wait_for_file_lock

    lock_path = worktree_state_dir(root) / "worktrees/.locks/_parent_git.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        wait_for_file_lock(handle, timeout_s=30)
        try:
            proofs = [_prove(root, checkout, outer_ref)]
            paired = resolve_checkout_project_dir(checkout)
            if project_ref is not None and not (paired / ".git").is_file():
                raise WorktreeRepairError("recorded paired Ticket worktree metadata is missing")
            if paired.is_relative_to(checkout) and (paired / ".git").is_file():
                owner = resolve_inner_project_repo(root)
                if owner is None or project_ref is None:
                    raise WorktreeRepairError(
                        "paired worktree repair requires its recorded owner and Ticket ref"
                    )
                proofs.append(_prove(owner.resolve(), paired.resolve(), project_ref))
            _repair_proofs(root, proofs)
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
            _repair_proofs(root, proofs)
        finally:
            release_file_lock(handle)
