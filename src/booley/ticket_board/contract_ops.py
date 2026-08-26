"""Authoring worktrees and publish-last sealing for Target contracts."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from booley.dev_support.contract_path_policy import is_static_contract_path
from booley.fusesoc import fusesoc_registry
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.project_dir import resolve_project_dir
from booley.runtime.ticket_repositories import (
    TicketRepository,
    paired_project_repository,
    project_ticket_branch,
    resolve_inner_project_repo,
    ticket_project_worktree,
)

from .frontmatter import parse_frontmatter, update_frontmatter
from .git_status import parse_porcelain_v1_z
from .target_contract import (
    TargetContract,
    build_contract,
    contract_control_paths,
    criterion_targets,
    resolve_commit,
    validate_criterion_targets,
    validate_targets_for_seal,
    verify_surface,
)
from .validation import validate_ticket_fields

_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_LEGACY_ARCHIVE_PREFIX = "booley-legacy-archive"


class ContractOperationError(RuntimeError):
    """A contract authoring or sealing transaction could not complete."""


@dataclass(frozen=True)
class ContractWorktrees:
    """Paths and initial revisions returned to the ticket author."""

    outer: Path
    project: Path | None
    outer_base_sha: str
    project_base_sha: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-ready command result."""
        return {
            "outer_worktree": str(self.outer),
            "project_worktree": str(self.project) if self.project is not None else "",
            "outer_base_sha": self.outer_base_sha,
            "project_base_sha": self.project_base_sha,
        }


@dataclass(frozen=True)
class _SealInputs:
    ticket: Path
    fields: dict
    outer: Path
    outer_changes: list[str]
    project: Path | None
    project_changes: list[str]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractOperationError(f"git {' '.join(args)} failed in {cwd}: {exc}") from exc


def _require_git(cwd: Path, *args: str) -> str:
    result = _git(cwd, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ContractOperationError(
            f"git {' '.join(args)} failed in {cwd} (rc={result.returncode}): {detail}"
        )
    return result.stdout.strip()


def _full_commit(repository: Path, ref: str) -> str:
    sha = _require_git(repository, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return resolve_commit(repository, sha)


def _branch_sha(repository: Path, branch: str) -> str:
    result = _git(repository, "rev-parse", "--verify", f"refs/heads/{branch}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _attach_worktree(repository: Path, destination: Path, branch: str, base_ref: str) -> str:
    base_sha = _full_commit(repository, base_ref)
    existing_sha = _branch_sha(repository, branch)
    if existing_sha and existing_sha != base_sha:
        raise ContractOperationError(
            f"contract branch {branch!r} already points at {existing_sha[:12]}, "
            f"not destination baseline {base_sha[:12]}"
        )
    if destination.exists():
        raise ContractOperationError(f"contract worktree path already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    args = ("worktree", "add", str(destination), branch)
    if not existing_sha:
        args = ("worktree", "add", "-b", branch, str(destination), base_sha)
    _require_git(repository, *args)
    return base_sha


def _current_branch(repository: Path) -> str:
    branch = _require_git(repository, "branch", "--show-current")
    if not branch:
        raise ContractOperationError(f"repository {repository} has a detached HEAD")
    return branch


def _project_base_branch(repository: Path, requested: str) -> str:
    return requested if _branch_sha(repository, requested) else _current_branch(repository)


def open_contract(
    project_root: Path | str,
    ticket_path: Path | str,
    slug: str,
    *,
    recover_legacy: bool = False,
) -> ContractWorktrees:
    """Create the outer and optional project-data authoring worktrees."""
    if not _SAFE_SLUG_RE.fullmatch(slug):
        raise ContractOperationError(f"unsafe ticket slug: {slug!r}")
    root = Path(project_root).resolve()
    fields, _body = parse_frontmatter(Path(ticket_path).read_text(encoding="utf-8"))
    branch = fields.get("branch")
    if not isinstance(branch, str) or not branch:
        raise ContractOperationError("ticket has no destination branch")
    outer = resolve_project_dir(root) / "worktrees" / slug
    if recover_legacy:
        _archive_legacy_worktrees(root, outer, slug)
    outer_base = _attach_worktree(root, outer, slug, branch)
    try:
        project, project_base = _open_project_contract(root, outer, slug, branch)
    except Exception:
        _git(root, "worktree", "remove", "--force", str(outer))
        raise
    return ContractWorktrees(outer, project, outer_base, project_base)


def _archive_legacy_worktrees(root: Path, outer: Path, slug: str) -> None:
    """Preserve blocked legacy branches, then clear their execution worktrees."""
    source = resolve_inner_project_repo(root)
    outer_sha = _branch_sha(root, slug)
    project_branch = project_ticket_branch(slug)
    project_sha = _branch_sha(source, project_branch) if source is not None else ""
    if outer_sha:
        _archive_ref(root, _legacy_archive(slug, outer_sha), outer_sha)
    if source is not None and project_sha:
        _archive_ref(source, _legacy_archive(slug, project_sha), project_sha)
    paired = paired_project_repository(outer) if outer.is_dir() else None
    _remove_contract_worktrees(root, outer, paired, source)
    _delete_branch(root, slug)
    if source is not None:
        _delete_branch(source, project_branch)


def _legacy_archive(slug: str, sha: str) -> str:
    return f"{_LEGACY_ARCHIVE_PREFIX}/{slug}/{sha[:12]}"


def _open_project_contract(
    root: Path, outer: Path, slug: str, requested_branch: str
) -> tuple[Path | None, str]:
    source = resolve_inner_project_repo(root)
    if source is None:
        return None, ""
    destination = ticket_project_worktree(outer)
    base_branch = _project_base_branch(source, requested_branch)
    branch = project_ticket_branch(slug)
    base_sha = _attach_worktree(source, destination, branch, base_branch)
    _require_git(source, "branch", f"--set-upstream-to={base_branch}", branch)
    return destination, base_sha


def _status_paths(repository: Path) -> list[str]:
    result = _git(
        repository,
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ContractOperationError(
            f"git status failed in {repository} (rc={result.returncode}): {detail}"
        )
    return [entry.path for entry in parse_porcelain_v1_z(result.stdout)]


def _local_manifest_paths(surface_root: Path, project_repository: bool) -> set[str]:
    paths = set(contract_control_paths(surface_root))
    if not project_repository:
        return paths
    prefix = ".booley_project/"
    return {path.removeprefix(prefix) for path in paths if path.startswith(prefix)}


def _is_authoring_path(repository: Path, path: str, manifest: set[str]) -> bool:
    if path in manifest:
        return True
    return not (repository / path).exists() and is_static_contract_path(path)


def _validate_authoring_changes(
    repository: Path, surface_root: Path, project_repository: bool
) -> list[str]:
    changed = _status_paths(repository)
    manifest = _local_manifest_paths(surface_root, project_repository)
    invalid = [path for path in changed if not _is_authoring_path(repository, path, manifest)]
    if invalid:
        raise ContractOperationError(
            "contract authoring worktree contains non-control changes: " + ", ".join(invalid)
        )
    return changed


def _commit_changes(repository: Path, paths: list[str], message: str) -> str:
    if paths:
        # Contract paths have already passed the manifest policy above. Force
        # them through user/global ignore rules because integrated projects
        # commonly hide ``.booley_project/`` while still tracking its control
        # files explicitly.
        _require_git(repository, "add", "-f", "--", *paths)
        staged = _git(repository, "diff", "--cached", "--quiet")
        if staged.returncode not in {0, 1}:
            raise ContractOperationError(
                f"could not inspect staged contract changes in {repository}"
            )
        if staged.returncode == 1:
            _require_git(repository, "commit", "-m", message)
    return _full_commit(repository, "HEAD")


def _seal_validation(
    fields: dict[str, object],
    body: str,
    worktree: Path,
    changed_targets: set[str],
) -> list[str]:
    errors = validate_ticket_fields(
        fields,
        body,
        check_files=False,
        check_git=False,
        project_root=worktree,
        check_tb_files=False,
    )
    errors.extend(validate_criterion_targets(fields, worktree))
    if errors:
        return errors
    with tempfile.TemporaryDirectory(prefix="booley-contract-dry-run-") as build_root:
        errors.extend(
            validate_targets_for_seal(
                fields,
                worktree,
                build_root,
                changed_targets=sorted(changed_targets),
            )
        )
    return errors


def _changed_targets(
    outer: Path,
    outer_changes: list[str],
    project: Path | None,
    project_changes: list[str],
) -> set[str]:
    """Return qualified selectors declared by changed, still-present core files."""
    selectors: set[str] = set()
    for repository, changes in ((outer, outer_changes), (project, project_changes)):
        if repository is None:
            continue
        for path in changes:
            core_file = repository / path
            if core_file.suffix.casefold() != ".core" or not core_file.is_file():
                continue
            try:
                doc = fusesoc_registry.read_core(core_file)
            except fusesoc_registry.FuseSocError as exc:
                raise ContractOperationError(str(exc)) from exc
            vlnv = doc.get("name")
            if not isinstance(vlnv, str) or not vlnv:
                raise ContractOperationError(f"changed .core has no valid name: {core_file}")
            selectors.update(
                f"{vlnv}#{target}" for target in fusesoc_registry.core_target_names(doc)
            )
    return selectors


def _prepare_seal(project_root: Path | str, ticket_path: Path | str, slug: str) -> _SealInputs:
    root = Path(project_root).resolve()
    ticket = Path(ticket_path)
    fields, body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    outer = resolve_project_dir(root) / "worktrees" / slug
    if not outer.is_dir():
        raise ContractOperationError(f"contract worktree is not open: {outer}")
    outer_changes = _validate_authoring_changes(outer, outer, project_repository=False)
    paired = paired_project_repository(outer)
    project = paired.worktree if paired is not None else None
    project_changes = (
        _validate_authoring_changes(project, outer, True) if project is not None else []
    )
    errors = _seal_validation(
        fields,
        body,
        outer,
        _changed_targets(outer, outer_changes, project, project_changes),
    )
    if errors:
        raise ContractOperationError("contract validation failed: " + "; ".join(errors))
    return _SealInputs(ticket, fields, outer, outer_changes, project, project_changes)


def seal_contract(project_root: Path | str, ticket_path: Path | str, slug: str) -> TargetContract:
    """Validate, commit all repositories, then atomically publish ticket metadata."""
    prepared = _prepare_seal(project_root, ticket_path, slug)
    ticket = prepared.ticket
    fields = prepared.fields
    outer = prepared.outer
    outer_changes = prepared.outer_changes
    project = prepared.project
    project_changes = prepared.project_changes
    outer_start = _full_commit(outer, "HEAD")
    project_start = _full_commit(project, "HEAD") if project is not None else ""
    outer_sha = outer_start
    project_sha = project_start
    try:
        if project is not None:
            project_sha = _commit_changes(
                project,
                project_changes,
                f"chore({slug}): seal project Target contract",
            )
        outer_sha = _commit_changes(outer, outer_changes, f"chore({slug}): seal Target contract")
        criterion_bindings = criterion_targets(fields.get("criteria"))
        contract = build_contract(
            outer,
            outer_sha=outer_sha,
            project_sha=project_sha,
            targets=(
                target
                for binding in criterion_bindings
                for target in (binding.target, binding.baseline)
            ),
            bindings=criterion_bindings,
        )
        update_frontmatter(
            ticket,
            {"base_sha": contract.outer_sha, "target_contract": contract.as_dict()},
        )
    except Exception:
        _restore_unpublished_commit(outer, outer_start, outer_sha)
        if project is not None:
            _restore_unpublished_commit(project, project_start, project_sha)
        raise
    return contract


def _restore_unpublished_commit(repository: Path, start_sha: str, current_sha: str) -> None:
    """Move an unpublished branch back while preserving authored changes staged."""
    if current_sha != start_sha:
        _require_git(repository, "reset", "--soft", start_sha)


def validate_open_seal(project_root: Path | str, slug: str, contract: TargetContract) -> list[str]:
    """Verify sealed refs and the still-open authoring checkout before enqueue."""
    root = Path(project_root).resolve()
    errors: list[str] = []
    try:
        resolve_commit(root, contract.outer_sha)
    except ValueError as exc:
        errors.append(f"target_contract.outer_sha: {exc}")
    branch_sha = _branch_sha(root, slug)
    if branch_sha != contract.outer_sha:
        errors.append(
            f"ticket branch {slug!r} points at {branch_sha or 'nothing'}, "
            f"expected target_contract.outer_sha {contract.outer_sha}"
        )
    outer = resolve_project_dir(root) / "worktrees" / slug
    if not outer.is_dir():
        errors.append(f"sealed contract worktree is missing: {outer}")
        return errors
    if _full_commit(outer, "HEAD") != contract.outer_sha:
        errors.append("sealed contract worktree HEAD does not match target_contract.outer_sha")
    errors.extend(_validate_project_seal(root, slug, outer, contract))
    try:
        verify_surface(contract, outer)
    except ValueError as exc:
        errors.append(str(exc))
    return errors


def reset_contract_worktrees(
    project_root: Path | str,
    slug: str,
    contract: TargetContract,
    requested_branch: str,
) -> None:
    """Discard implementation state and restore the sealed authoring checkouts."""
    root = Path(project_root).resolve()
    _full_commit(root, contract.outer_sha)
    source = resolve_inner_project_repo(root)
    _validate_reset_project_source(source, contract)
    outer = resolve_project_dir(root) / "worktrees" / slug
    paired = paired_project_repository(outer) if outer.is_dir() else None
    _remove_contract_worktrees(root, outer, paired, source)
    _delete_branch(root, slug)
    if source is not None:
        _delete_branch(source, project_ticket_branch(slug))
    _attach_worktree(root, outer, slug, contract.outer_sha)
    _restore_project_contract(source, outer, slug, requested_branch, contract)
    errors = validate_open_seal(root, slug, contract)
    if errors:
        raise ContractOperationError("could not restore sealed contract: " + "; ".join(errors))


def _validate_reset_project_source(source: Path | None, contract: TargetContract) -> None:
    if contract.project_sha and source is None:
        raise ContractOperationError("sealed project repository is unavailable")
    if source is not None and not contract.project_sha:
        raise ContractOperationError("sealed contract has no project repository commit")
    if source is not None:
        _full_commit(source, contract.project_sha)


def _restore_project_contract(
    source: Path | None,
    outer: Path,
    slug: str,
    requested_branch: str,
    contract: TargetContract,
) -> None:
    if source is None:
        return
    branch = project_ticket_branch(slug)
    destination = ticket_project_worktree(outer)
    _attach_worktree(source, destination, branch, contract.project_sha)
    base_branch = _project_base_branch(source, requested_branch)
    _require_git(source, "branch", f"--set-upstream-to={base_branch}", branch)


def _validate_project_seal(
    root: Path, slug: str, outer: Path, contract: TargetContract
) -> list[str]:
    source = resolve_inner_project_repo(root)
    paired = paired_project_repository(outer)
    if not contract.project_sha:
        return (
            [] if source is None and paired is None else ["target_contract.project_sha is missing"]
        )
    if source is None or paired is None:
        return ["target_contract.project_sha is set but the paired project repository is missing"]
    try:
        resolve_commit(source, contract.project_sha)
    except ValueError as exc:
        return [f"target_contract.project_sha: {exc}"]
    branch = project_ticket_branch(slug)
    errors = []
    if _branch_sha(source, branch) != contract.project_sha:
        errors.append(f"project contract branch {branch!r} does not match project_sha")
    if _full_commit(paired.worktree, "HEAD") != contract.project_sha:
        errors.append("paired project worktree HEAD does not match target_contract.project_sha")
    return errors


def revise_contract(
    project_root: Path | str,
    ticket_path: Path | str,
    slug: str,
    *,
    status: str,
    logs_dir: Path | str,
) -> ContractWorktrees:
    """Archive a seal, discard execution state, and reopen from destination baselines."""
    if status not in {"draft", "blocked"}:
        raise ContractOperationError(
            f"contract revision requires a draft or blocked ticket, got {status!r}"
        )
    root = Path(project_root).resolve()
    ticket = Path(ticket_path)
    fields, _body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    raw = fields.get("target_contract")
    if raw is None:
        raise ContractOperationError("ticket has no sealed Target contract to revise")
    contract = TargetContract.from_mapping(raw)
    archive = f"booley-contract-archive/{slug}/{contract.surface_digest[:12]}"
    outer = resolve_project_dir(root) / "worktrees" / slug
    paired = paired_project_repository(outer) if outer.is_dir() else None
    _archive_ref(root, archive, contract.outer_sha)
    source = resolve_inner_project_repo(root)
    if source is not None and contract.project_sha:
        _archive_ref(source, archive, contract.project_sha)
    _remove_contract_worktrees(root, outer, paired, source)
    _delete_branch(root, slug)
    if source is not None and contract.project_sha:
        _delete_branch(source, project_ticket_branch(slug))
    ticket = _reset_contract_ticket(ticket, fields, contract, status)
    safe_rmtree(Path(logs_dir) / slug)
    return open_contract(root, ticket, slug)


def _archive_ref(repository: Path, archive: str, source: str) -> None:
    source_sha = _full_commit(repository, source)
    archived_sha = _branch_sha(repository, archive)
    if archived_sha and archived_sha != source_sha:
        raise ContractOperationError(f"archive ref {archive!r} already names another commit")
    if not archived_sha:
        _require_git(repository, "branch", archive, source_sha)


def _remove_contract_worktrees(
    root: Path,
    outer: Path,
    paired: TicketRepository | None,
    project_source: Path | None,
) -> None:
    if paired is not None and project_source is not None:
        _require_git(project_source, "worktree", "remove", "--force", str(paired.worktree))
    if outer.exists():
        _require_git(root, "worktree", "remove", "--force", str(outer))


def _delete_branch(repository: Path, branch: str) -> None:
    if _branch_sha(repository, branch):
        _require_git(repository, "branch", "-D", branch)


def _reset_contract_ticket(
    ticket: Path,
    fields: dict[str, object],
    contract: TargetContract,
    status: str,
) -> Path:
    history = fields.get("target_contract_history")
    entries = list(history) if isinstance(history, list) else []
    entries.append(
        f"schema={contract.schema};outer={contract.outer_sha};project={contract.project_sha};"
        f"digest={contract.surface_digest}"
    )
    update_frontmatter(
        ticket,
        {"target_contract_history": entries},
        remove_keys=["target_contract", "base_sha", "created", "feature_branch"],
    )
    if status == "blocked":
        drafts = ticket.parent.parent / "drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        destination = drafts / ticket.name
        shutil.move(str(ticket), str(destination))
        return destination
    return ticket
