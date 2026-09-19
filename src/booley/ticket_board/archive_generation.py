"""Validate and release the exact generation recorded by a Ticket."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.project_dir import resolve_project_dir, runtime_dir
from booley.ticket_board.ticket_repositories import (
    resolve_inner_project_repo,
    ticket_project_worktree,
)

from .ticket_baseline import ticket_baseline_from_machine, worktree_for_ref
from .workspace_ops import load_draft_generation


class ArchiveGenerationError(RuntimeError):
    """Generation ownership or release could not be proved."""


@dataclass(frozen=True)
class _Participant:
    role: str
    repository: Path
    ref: str
    sha: str | None
    worktree: Path | None


def _git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArchiveGenerationError(f"git {' '.join(args)} in {repository}: {exc}") from exc
    return result


def _require_git(repository: Path, *args: str) -> str:
    result = _git(repository, *args)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or "no diagnostic"
        raise ArchiveGenerationError(f"git {' '.join(args)} in {repository} failed: {detail}")
    return result.stdout.strip()


def _ref_sha(repository: Path, ref: str) -> str | None:
    result = _git(repository, "show-ref", "--verify", "--hash", ref)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise ArchiveGenerationError(
            f"could not inspect {ref} in {repository}: {(result.stderr or result.stdout).strip()}"
        )
    sha = result.stdout.strip()
    if _require_git(repository, "cat-file", "-t", sha) != "commit":
        raise ArchiveGenerationError(f"{ref} in {repository} does not name a commit")
    return sha


def _common_dir(repository: Path) -> Path:
    raw = _require_git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(raw).resolve()


def _records(repository: Path) -> list[tuple[Path, str | None]]:
    lines = _require_git(repository, "worktree", "list", "--porcelain").splitlines()
    records: list[tuple[Path, str | None]] = []
    path: Path | None = None
    ref: str | None = None
    for line in [*lines, ""]:
        if line.startswith("worktree "):
            path = Path(line[9:])
        elif line.startswith("branch "):
            ref = line[7:]
        elif not line and path is not None:
            records.append((path, ref))
            path, ref = None, None
    return records


def _participant(
    role: str, repository: Path, ref: str, canonical: Path, baseline: str | None
) -> _Participant:
    sha = _ref_sha(repository, ref)
    if sha is not None and baseline is not None:
        result = _git(repository, "merge-base", "--is-ancestor", baseline, sha)
        if result.returncode != 0:
            raise ArchiveGenerationError(
                f"{role} {ref} no longer descends from Ticket baseline {baseline}"
            )
    matches = [path for path, branch in _records(repository) if branch == ref]
    if len(matches) > 1:
        raise ArchiveGenerationError(f"{role} {ref} has multiple worktrees: {matches}")
    worktree = worktree_for_ref(repository, ref)
    if worktree is not None:
        if worktree.resolve() != canonical.resolve():
            raise ArchiveGenerationError(f"{role} {ref} is attached at unexpected path {worktree}")
        if _common_dir(worktree) != _common_dir(repository):
            raise ArchiveGenerationError(f"{role} {ref} at {worktree} has another Git owner")
        if _require_git(worktree, "symbolic-ref", "--quiet", "HEAD") != ref:
            raise ArchiveGenerationError(f"{role} {ref} at {worktree} changed branch")
    elif canonical.exists() or canonical.is_symlink():
        raise ArchiveGenerationError(
            f"{role} canonical Ticket Workspace {canonical} has unexpected contents"
        )
    if worktree is not None and sha is None:
        raise ArchiveGenerationError(f"{role} {ref} has a worktree but no branch")
    return _Participant(role, repository, ref, sha, worktree)


def _draft_ref(root: Path, slug: str, canonical: Path) -> str | None:
    descriptor = runtime_dir(root) / "acceptance" / "drafts" / f"{slug}.json"
    if descriptor.exists():
        return f"refs/heads/booley-generation/{load_draft_generation(root, slug)}/{slug}"
    if canonical.exists() or canonical.is_symlink():
        raise ArchiveGenerationError(
            f"draft {slug} has a workspace but no generation descriptor: {canonical}"
        )
    if _git(root, "rev-parse", "--is-inside-work-tree").returncode:
        return None
    repositories = [root, resolve_inner_project_repo(root)]
    for repository in (item for item in repositories if item is not None):
        refs = _require_git(
            repository, "for-each-ref", "--format=%(refname)", "refs/heads/booley-generation"
        )
        if any(ref.endswith(f"/{slug}") for ref in refs.splitlines()):
            raise ArchiveGenerationError(f"draft {slug} has generation refs without a descriptor")
        if any(
            ref and ref.startswith("refs/heads/booley-generation/") and ref.endswith(f"/{slug}")
            for _, ref in _records(repository)
        ):
            raise ArchiveGenerationError(
                f"draft {slug} has generation worktrees without a descriptor"
            )
    return None


def _ticket_refs(
    root: Path, slug: str, status: str, fields: dict, canonical: Path, project: Path | None
) -> dict[str, str]:
    """Resolve stored identities without guessing a generation from the slug."""
    if status == "draft":
        ref = _draft_ref(root, slug, canonical)
        if ref is None:
            return {}
        return {"outer": ref} if project is None else {"outer": ref, "project": ref}
    if "machine" in fields:
        basis = ticket_baseline_from_machine(fields["machine"])
        refs = {row.role: row.ticket_ref for row in basis.participants}
        if "project" in refs and project is None:
            raise ArchiveGenerationError(f"project repository is unavailable for {slug}")
        if "project" not in refs and project is not None:
            raise ArchiveGenerationError(f"Ticket {slug} lacks its configured project participant")
        return refs
    # Older Tickets without a machine identity have no safe generation target.
    if _draft_ref(root, slug, canonical) is not None:
        raise ArchiveGenerationError(
            f"Ticket {slug} has generation residue without machine identity"
        )
    return {}


def _validate_refs(root: Path, slug: str, refs: dict[str, str]) -> None:
    pattern = re.compile(rf"refs/heads/booley-generation/[0-9a-f]{{16,32}}/{re.escape(slug)}")
    if any(pattern.fullmatch(ref) is None for ref in refs.values()):
        raise ArchiveGenerationError(f"Ticket {slug} has invalid generation refs: {refs}")
    if len(set(refs.values())) != 1:
        raise ArchiveGenerationError(f"Ticket {slug} has conflicting generation refs")
    descriptor = runtime_dir(root) / "acceptance" / "drafts" / f"{slug}.json"
    if descriptor.exists():
        token = load_draft_generation(root, slug)
        if any(ref != f"refs/heads/booley-generation/{token}/{slug}" for ref in refs.values()):
            raise ArchiveGenerationError(f"Ticket {slug} has a conflicting draft descriptor")


def plan_generation(root: Path, slug: str, status: str, fields: dict) -> tuple[_Participant, ...]:
    """Preflight all participants before modifying either repository."""
    canonical = resolve_project_dir(root) / "worktrees" / slug
    project = resolve_inner_project_repo(root)
    if project is None and fields.get("project_destination_ref"):
        raise ArchiveGenerationError(f"project repository is unavailable for {slug}")
    nested = ticket_project_worktree(canonical)
    if project is None and ((nested / ".git").exists() or (nested / ".git").is_symlink()):
        raise ArchiveGenerationError(
            f"paired project workspace exists for {slug} but its source repository is unavailable"
        )
    refs = _ticket_refs(root, slug, status, fields, canonical, project)
    if not refs:
        return ()
    _validate_refs(root, slug, refs)
    baselines = (
        {
            row.role: row.authoring_sha
            for row in ticket_baseline_from_machine(fields["machine"]).participants
        }
        if status != "draft" and "machine" in fields
        else {}
    )
    roles = (("outer", root),) if project is None else (("outer", root), ("project", project))
    return tuple(
        _participant(
            role,
            repository,
            refs[role],
            canonical if role == "outer" else ticket_project_worktree(canonical),
            baselines.get(role),
        )
        for role, repository in roles
    )


def release_generation(plan: tuple[_Participant, ...]) -> None:
    """Release paired worktree first, then outer, then exact refs with CAS."""
    for row in sorted(plan, key=lambda item: item.role != "project"):
        if row.worktree is not None:
            if (
                _common_dir(row.worktree) != _common_dir(row.repository)
                or _require_git(row.worktree, "symbolic-ref", "--quiet", "HEAD") != row.ref
                or worktree_for_ref(row.repository, row.ref) != row.worktree
            ):
                raise ArchiveGenerationError(
                    f"{row.role} {row.ref} at {row.worktree} changed before removal"
                )
            _require_git(row.repository, "worktree", "remove", "--force", str(row.worktree))
    for row in sorted(plan, key=lambda item: item.role != "project"):
        if row.sha is not None:
            _require_git(row.repository, "update-ref", "-d", row.ref, row.sha)
