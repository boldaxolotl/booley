"""Private authoring worktrees and Acceptance Basis publication helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from booley.fusesoc import fusesoc_registry
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.project_dir import resolve_project_dir, runtime_dir
from booley.runtime.project_prepare import prepare_project
from booley.runtime.ticket_repositories import (
    TicketRepository,
    paired_project_repository,
    resolve_inner_project_repo,
    ticket_project_worktree,
)
from booley.ticket_board.acceptance_path_policy import is_static_acceptance_path

from .acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
    authored_ticket_record,
    canonical_json,
    record_relative_path,
)
from .acceptance_targets import (
    acceptance_control_paths,
    canonical_acceptance_bindings,
    criterion_targets,
    resolve_commit,
    validate_acceptance_targets,
    validate_criterion_targets,
)
from .basis_publication import (
    ParticipantPreparation,
    load_basis_publication,
    publish_basis_commits,
)
from .frontmatter import parse_frontmatter
from .git_status import parse_porcelain_v1_z
from .helpers import TicketSlugError, validate_ticket_slug
from .persistence import WriteOnceConflictError, atomic_write_once
from .validation import validate_ticket_fields

_GENERATION_PREFIX = "booley-generation"


class AcceptanceBasisOperationError(RuntimeError):
    """A Ticket Workspace or Acceptance Basis transaction could not complete."""


@dataclass(frozen=True)
class AuthoringWorkspace:
    """Paths and initial revisions returned to the ticket author."""

    outer: Path
    project: Path | None
    outer_base_sha: str
    project_base_sha: str
    generation: str = ""

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-ready command result."""
        return {
            "outer_worktree": str(self.outer),
            "project_worktree": str(self.project) if self.project is not None else "",
            "outer_base_sha": self.outer_base_sha,
            "project_base_sha": self.project_base_sha,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class _BasisPreparation:
    ticket: Path
    fields: dict
    outer: Path
    outer_changes: list[str]
    project: Path | None
    project_changes: list[str]


@dataclass(frozen=True)
class _ProjectOpenPlan:
    source: Path
    base_branch: str
    base_sha: str


@dataclass
class _OpenAttachment:
    repository: Path
    worktree: Path
    branch: str
    base_sha: str
    branch_created: bool = False
    worktree_attached: bool = False
    partial_path: bool = False
    previous_upstream: str | None = None
    upstream_changed: bool = False


@dataclass(frozen=True)
class _ResetParticipant:
    repository: Path
    participant: BasisParticipant
    expected_head: str


@dataclass(frozen=True)
class BasisResetPlan:
    """Fully validated identities and checkout paths for one execution reset."""

    root: Path
    project_source: Path | None
    outer_worktree: Path
    paired_worktree: TicketRepository | None
    participants: tuple[_ResetParticipant, ...]
    basis_id: str
    requested_branch: str


def _git(
    cwd: Path,
    *args: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceBasisOperationError(
            f"git {' '.join(args)} failed in {cwd}: {exc}"
        ) from exc


def _require_git(
    cwd: Path,
    *args: str,
    environment: dict[str, str] | None = None,
) -> str:
    result = _git(cwd, *args, environment=environment)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AcceptanceBasisOperationError(
            f"git {' '.join(args)} failed in {cwd} (rc={result.returncode}): {detail}"
        )
    return result.stdout.strip()


def _full_commit(repository: Path, ref: str) -> str:
    sha = _require_git(repository, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return resolve_commit(repository, sha)


def _branch_sha(repository: Path, branch: str) -> str:
    result = _git(repository, "rev-parse", "--verify", f"refs/heads/{branch}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _strict_branch_sha(repository: Path, branch: str) -> str | None:
    ref = f"refs/heads/{branch}"
    result = _git(repository, "rev-parse", "--verify", "--quiet", ref)
    if result.returncode == 0:
        return resolve_commit(repository, result.stdout.strip())
    if result.returncode == 1:
        return None
    detail = (result.stderr or result.stdout).strip()
    raise AcceptanceBasisOperationError(
        f"could not inspect Ticket Workspace branch {branch!r} in {repository} "
        f"(rc={result.returncode}): {detail}"
    )


def _attach_worktree(repository: Path, destination: Path, branch: str, base_ref: str) -> str:
    base_sha = _full_commit(repository, base_ref)
    existing_sha = _branch_sha(repository, branch)
    if existing_sha and existing_sha != base_sha:
        raise AcceptanceBasisOperationError(
            f"Ticket Workspace branch {branch!r} already points at {existing_sha[:12]}, "
            f"not destination baseline {base_sha[:12]}"
        )
    if destination.exists():
        raise AcceptanceBasisOperationError(f"Ticket Workspace path already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    args = ("worktree", "add", str(destination), branch)
    if not existing_sha:
        args = ("worktree", "add", "-b", branch, str(destination), base_sha)
    _require_git(repository, *args)
    return base_sha


def _current_branch(repository: Path) -> str:
    branch = _require_git(repository, "branch", "--show-current")
    if not branch:
        raise AcceptanceBasisOperationError(f"repository {repository} has a detached HEAD")
    return branch


def _project_base_branch(repository: Path, requested: str) -> str:
    if requested.startswith("refs/heads/"):
        requested = requested.removeprefix("refs/heads/")
    if _strict_branch_sha(repository, requested) is None:
        raise AcceptanceBasisOperationError(
            f"paired project destination refs/heads/{requested} does not exist; "
            "set project_destination_ref explicitly"
        )
    return requested


def _preflight_project_repository(
    root: Path, requested_branch: str, project_destination_ref: object
) -> _ProjectOpenPlan | None:
    """Resolve a paired destination without consulting its live checkout."""
    source = resolve_inner_project_repo(root)
    if source is None:
        return None
    requested = project_destination_ref or f"refs/heads/{requested_branch}"
    if not isinstance(requested, str) or not requested.startswith("refs/heads/"):
        raise AcceptanceBasisOperationError(
            "project_destination_ref must be a full refs/heads/... name"
        )
    base_branch = _project_base_branch(source, requested)
    base_sha = _full_commit(source, base_branch)
    return _ProjectOpenPlan(source, base_branch, base_sha)


def _plan_open_attachment(
    repository: Path, worktree: Path, branch: str, base_sha: str
) -> _OpenAttachment:
    existing_sha = _strict_branch_sha(repository, branch)
    if existing_sha is not None and existing_sha != base_sha:
        raise AcceptanceBasisOperationError(
            f"Ticket Workspace branch {branch!r} already points at {existing_sha[:12]}, "
            f"not destination baseline {base_sha[:12]}"
        )
    if worktree.exists() or worktree.is_symlink():
        raise AcceptanceBasisOperationError(f"Ticket Workspace path already exists: {worktree}")
    return _OpenAttachment(repository, worktree, branch, base_sha)


def _registered_worktree(repository: Path, worktree: Path) -> bool:
    listing = _require_git(repository, "worktree", "list", "--porcelain")
    expected = worktree.resolve()
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        if Path(line.removeprefix("worktree ")).resolve() == expected:
            return True
    return False


def _worktree_owns_branch(repository: Path, worktree: Path, branch: str) -> bool:
    """Whether an existing worktree proves ownership of a Ticket generation branch."""
    if not worktree.is_dir() or not _registered_worktree(repository, worktree):
        return False
    return _require_git(worktree, "branch", "--show-current") == branch


def _create_attachment(attachment: _OpenAttachment) -> None:
    ref = f"refs/heads/{attachment.branch}"
    if _strict_branch_sha(attachment.repository, attachment.branch) is None:
        try:
            _require_git(
                attachment.repository,
                "update-ref",
                ref,
                attachment.base_sha,
                "0" * len(attachment.base_sha),
            )
        except AcceptanceBasisOperationError as exc:
            current = _strict_branch_sha(attachment.repository, attachment.branch)
            if current is not None:
                raise AcceptanceBasisOperationError(
                    f"{exc}; branch creation was not confirmed, so {ref} was retained"
                ) from exc
            raise
        attachment.branch_created = True
    attachment.worktree.parent.mkdir(parents=True, exist_ok=True)
    try:
        _require_git(
            attachment.repository,
            "worktree",
            "add",
            str(attachment.worktree),
            attachment.branch,
        )
    except AcceptanceBasisOperationError:
        attachment.partial_path = attachment.worktree.exists() or attachment.worktree.is_symlink()
        raise
    attachment.worktree_attached = True
    if _full_commit(attachment.worktree, "HEAD") != attachment.base_sha:
        raise AcceptanceBasisOperationError(
            f"Ticket Workspace destination moved while attaching {attachment.branch!r}"
        )


def _branch_upstream(repository: Path, branch: str) -> str | None:
    value = _require_git(
        repository,
        "for-each-ref",
        "--format=%(upstream:short)",
        f"refs/heads/{branch}",
    )
    return value or None


def _set_attachment_upstream(attachment: _OpenAttachment, upstream: str) -> None:
    attachment.previous_upstream = _branch_upstream(attachment.repository, attachment.branch)
    if attachment.previous_upstream == upstream:
        return
    try:
        _require_git(
            attachment.repository,
            "branch",
            f"--set-upstream-to={upstream}",
            attachment.branch,
        )
    except AcceptanceBasisOperationError:
        attachment.upstream_changed = (
            _branch_upstream(attachment.repository, attachment.branch) == upstream
        )
        raise
    attachment.upstream_changed = True


def _restore_attachment_upstream(attachment: _OpenAttachment) -> str | None:
    if not attachment.upstream_changed or attachment.branch_created:
        return None
    try:
        if attachment.previous_upstream is None:
            result = _git(
                attachment.repository,
                "branch",
                "--unset-upstream",
                attachment.branch,
            )
        else:
            result = _git(
                attachment.repository,
                "branch",
                f"--set-upstream-to={attachment.previous_upstream}",
                attachment.branch,
            )
    except AcceptanceBasisOperationError as exc:
        return str(exc)
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip()
    return f"could not restore upstream for {attachment.branch!r}: {detail}"


def _remove_attachment_worktree(attachment: _OpenAttachment) -> tuple[list[str], bool]:
    failures: list[str] = []
    try:
        registered = _registered_worktree(attachment.repository, attachment.worktree)
    except AcceptanceBasisOperationError as exc:
        return [str(exc)], False
    if registered and attachment.worktree_attached:
        try:
            result = _git(
                attachment.repository,
                "worktree",
                "remove",
                "--force",
                str(attachment.worktree),
            )
        except AcceptanceBasisOperationError as exc:
            return [str(exc)], False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return [f"could not remove {attachment.worktree}: {detail}"], False
        registered = False
    elif registered:
        failures.append(f"retained ambiguously created worktree {attachment.worktree}")
    elif attachment.partial_path and (
        attachment.worktree.exists() or attachment.worktree.is_symlink()
    ):
        try:
            if attachment.worktree.is_symlink():
                attachment.worktree.unlink()
            else:
                safe_rmtree(attachment.worktree)
        except (OSError, ValueError) as exc:
            failures.append(f"could not remove partial path {attachment.worktree}: {exc}")
    return failures, not registered


def _delete_created_branch(attachment: _OpenAttachment, registered: bool) -> str | None:
    if not attachment.branch_created or registered:
        return None
    error: str | None = None
    try:
        current = _strict_branch_sha(attachment.repository, attachment.branch)
    except AcceptanceBasisOperationError as exc:
        error = str(exc)
    else:
        if current is not None and current != attachment.base_sha:
            error = f"retained moved branch {attachment.branch!r} at {current[:12]}"
        elif current is not None:
            try:
                result = _git(
                    attachment.repository,
                    "update-ref",
                    "-d",
                    f"refs/heads/{attachment.branch}",
                    attachment.base_sha,
                )
            except AcceptanceBasisOperationError as exc:
                error = str(exc)
            else:
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout).strip()
                    error = f"could not delete created branch {attachment.branch!r}: {detail}"
    return error


def _rollback_attachment(attachment: _OpenAttachment) -> tuple[list[str], bool]:
    failures, worktree_clear = _remove_attachment_worktree(attachment)
    upstream_error = _restore_attachment_upstream(attachment)
    if upstream_error:
        failures.append(upstream_error)
    branch_error = _delete_created_branch(attachment, registered=not worktree_clear)
    if branch_error:
        failures.append(branch_error)
    return failures, worktree_clear


def _rollback_open(outer: _OpenAttachment, project: _OpenAttachment | None) -> list[str]:
    failures: list[str] = []
    project_clear = True
    if project is not None:
        project_failures, project_clear = _rollback_attachment(project)
        failures.extend(project_failures)
    if project_clear:
        outer_failures, _outer_clear = _rollback_attachment(outer)
        failures.extend(outer_failures)
    else:
        failures.append(f"retained outer worktree {outer.worktree} with paired state")
    return failures


def _validate_open_bases(
    root: Path,
    outer_branch: str,
    outer_sha: str,
    project: _ProjectOpenPlan | None,
) -> None:
    if _full_commit(root, outer_branch) != outer_sha:
        raise AcceptanceBasisOperationError(
            f"destination branch {outer_branch!r} moved during preflight"
        )
    if project is None:
        return
    if _full_commit(project.source, project.base_branch) != project.base_sha:
        raise AcceptanceBasisOperationError(
            f"paired project destination {project.base_branch!r} moved during preflight"
        )


def _generation_file(root: Path, slug: str) -> Path:
    directory = runtime_dir(root) / "acceptance" / "drafts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{slug}.json"


def _draft_generation(root: Path, slug: str) -> str:
    """Return the private generation token allocated for a draft."""
    path = _generation_file(root, slug)
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))["generation"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AcceptanceBasisOperationError(
                f"invalid draft generation descriptor: {path}"
            ) from exc
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value):
            return value
        raise AcceptanceBasisOperationError(f"invalid draft generation descriptor: {path}")
    value = secrets.token_hex(8)
    payload = (json.dumps({"generation": value}, sort_keys=True) + "\n").encode()
    try:
        created = atomic_write_once(path, payload)
    except WriteOnceConflictError as exc:
        raise AcceptanceBasisOperationError(
            f"conflicting draft generation descriptor: {path}"
        ) from exc
    if not created:
        return _draft_generation(root, slug)
    return value


def _generation_branch(generation: str, slug: str) -> str:
    return f"{_GENERATION_PREFIX}/{generation}/{slug}"


def ensure_ticket_workspace(
    project_root: Path | str,
    ticket_path: Path | str,
    slug: str,
) -> AuthoringWorkspace:
    """Create the outer and optional project-data authoring worktrees."""
    try:
        validate_ticket_slug(slug)
    except TicketSlugError as exc:
        raise AcceptanceBasisOperationError(str(exc)) from exc
    root = Path(project_root).resolve()
    fields, _body = parse_frontmatter(Path(ticket_path).read_text(encoding="utf-8"))
    branch = fields.get("branch")
    if not isinstance(branch, str) or not branch:
        raise AcceptanceBasisOperationError("ticket has no destination branch")
    generation = _draft_generation(root, slug)
    outer = resolve_project_dir(root) / "worktrees" / slug
    return _open_generation(root, Path(ticket_path), slug, fields, generation, outer)


def _open_generation(
    root: Path,
    ticket: Path,
    slug: str,
    fields: dict,
    generation: str,
    outer: Path,
) -> AuthoringWorkspace:
    branch = fields.get("branch")
    if not isinstance(branch, str) or not branch:
        raise AcceptanceBasisOperationError("ticket has no destination branch")
    ticket_branch = _generation_branch(generation, slug)
    project_plan = _preflight_project_repository(
        root, branch, fields.get("project_destination_ref")
    )
    outer_base = _full_commit(root, branch)
    if outer.is_dir() and _worktree_owns_branch(root, outer, ticket_branch):
        paired = _resume_project_attachment(root, ticket, slug, outer, ticket_branch, project_plan)
        return AuthoringWorkspace(
            outer,
            paired,
            outer_base,
            project_plan.base_sha if project_plan is not None else "",
            generation,
        )
    outer_attachment = _plan_open_attachment(root, outer, ticket_branch, outer_base)
    project_attachment = _project_open_attachment(project_plan, outer, ticket_branch)
    _validate_open_bases(root, branch, outer_base, project_plan)
    try:
        _create_attachment(outer_attachment)
        if project_attachment is not None and project_plan is not None:
            _create_attachment(project_attachment)
            _set_attachment_upstream(project_attachment, project_plan.base_branch)
        _prepare_workspace_project(root, outer, ticket, slug)
    except Exception as exc:
        rollback_failures = _rollback_open(outer_attachment, project_attachment)
        if rollback_failures:
            raise AcceptanceBasisOperationError(
                f"Ticket Workspace creation failed: {exc}; rollback incomplete: "
                + "; ".join(rollback_failures)
            ) from exc
        raise
    project = project_attachment.worktree if project_attachment is not None else None
    project_base = project_plan.base_sha if project_plan is not None else ""
    return AuthoringWorkspace(outer, project, outer_base, project_base, generation)


def _project_open_attachment(
    project: _ProjectOpenPlan | None,
    outer: Path,
    ticket_branch: str,
) -> _OpenAttachment | None:
    if project is None:
        return None
    return _plan_open_attachment(
        project.source,
        ticket_project_worktree(outer),
        ticket_branch,
        project.base_sha,
    )


def _resume_project_attachment(
    root: Path,
    ticket: Path,
    slug: str,
    outer: Path,
    ticket_branch: str,
    project: _ProjectOpenPlan | None,
) -> Path | None:
    """Finish a project attachment interrupted after the outer worktree was created."""
    paired = paired_project_repository(outer)
    if project is None:
        if paired is not None:
            raise AcceptanceBasisOperationError("unexpected paired project Ticket Workspace")
        _prepare_workspace_project(root, outer, ticket, slug)
        return None
    if paired is not None:
        if not _worktree_owns_branch(project.source, paired.worktree, ticket_branch):
            raise AcceptanceBasisOperationError(
                "paired project Ticket Workspace has the wrong branch"
            )
        _prepare_workspace_project(root, outer, ticket, slug)
        return paired.worktree
    attachment = _project_open_attachment(project, outer, ticket_branch)
    if attachment is None:  # Defensive: project is known to be present above.
        raise AcceptanceBasisOperationError("paired project attachment could not be planned")
    try:
        _create_attachment(attachment)
        _set_attachment_upstream(attachment, project.base_branch)
        _prepare_workspace_project(root, outer, ticket, slug)
    except Exception as exc:
        failures, _clear = _rollback_attachment(attachment)
        if failures:
            raise AcceptanceBasisOperationError(
                f"project attachment recovery failed: {exc}; rollback incomplete: "
                + "; ".join(failures)
            ) from exc
        raise
    return attachment.worktree


def _prepare_workspace_project(root: Path, outer: Path, ticket: Path, slug: str) -> None:
    """Run the same deterministic preparation used by ticket execution."""
    from booley.flows.execution import flow_enabled
    from booley.runtime.submodule_materialization import (
        SubmoduleMaterializationError,
        materialize_ticket_submodules,
    )

    try:
        materialize_ticket_submodules(root, outer)
    except SubmoduleMaterializationError as exc:
        raise AcceptanceBasisOperationError(f"Submodule setup failed offline: {exc}") from exc
    result = prepare_project(
        root,
        outer,
        slug=slug,
        ticket_path=ticket,
        sim_flow_enabled=flow_enabled("sim", outer),
    )
    if not result.ok:
        raise AcceptanceBasisOperationError(result.error)


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
        raise AcceptanceBasisOperationError(
            f"git status failed in {repository} (rc={result.returncode}): {detail}"
        )
    return [entry.path for entry in parse_porcelain_v1_z(result.stdout)]


def _local_manifest_paths(surface_root: Path, project_repository: bool) -> set[str]:
    paths = set(acceptance_control_paths(surface_root))
    paired = paired_project_repository(surface_root)
    if paired is None:
        return set() if project_repository else paths
    project_root = paired.path_prefix.rstrip("/")
    prefix = project_root + "/"
    if project_repository:
        return {path.removeprefix(prefix) for path in paths if path.startswith(prefix)}
    return {path for path in paths if path != project_root and not path.startswith(prefix)}


def _is_authoring_path(repository: Path, path: str, manifest: set[str]) -> bool:
    if path in manifest:
        return True
    return not (repository / path).exists() and is_static_acceptance_path(path)


def _validate_authoring_changes(
    repository: Path,
    surface_root: Path,
    project_repository: bool,
    recovery_paths: set[str],
) -> list[str]:
    changed = _status_paths(repository)
    manifest = _local_manifest_paths(surface_root, project_repository)
    invalid = [
        path
        for path in changed
        if path not in recovery_paths and not _is_authoring_path(repository, path, manifest)
    ]
    if invalid:
        raise AcceptanceBasisOperationError(
            "Ticket Workspace contains non-authoring changes: " + ", ".join(invalid)
        )
    return sorted(set(changed) | manifest)


def _staged_tree(repository: Path, paths: list[str]) -> tuple[str, str]:
    """Build a tree in an isolated index, leaving the authoring index untouched."""
    parent = _full_commit(repository, "HEAD")
    with tempfile.TemporaryDirectory(prefix="booley-basis-index-") as directory:
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        _require_git(repository, "read-tree", parent, environment=environment)
        if paths:
            _require_git(
                repository,
                "add",
                "-f",
                "--",
                *paths,
                environment=environment,
            )
        tree = _require_git(repository, "write-tree", environment=environment)
    return parent, tree


def _basis_validation(
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
    from .target_finalization import validate_acceptance_removals

    errors.extend(validate_acceptance_removals(fields, worktree))
    if errors:
        return errors
    with tempfile.TemporaryDirectory(prefix="booley-basis-dry-run-") as build_root:
        errors.extend(
            validate_acceptance_targets(
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
                raise AcceptanceBasisOperationError(str(exc)) from exc
            vlnv = doc.get("name")
            if not isinstance(vlnv, str) or not vlnv:
                raise AcceptanceBasisOperationError(
                    f"changed .core has no valid name: {core_file}"
                )
            selectors.update(
                f"{vlnv}#{target}"
                for target in fusesoc_registry.core_target_names(doc)
                if not fusesoc_registry.core_target_is_doctor_selftest(doc, target)
            )
    return selectors


def _prepare_basis(
    project_root: Path | str, ticket_path: Path | str, slug: str
) -> _BasisPreparation:
    root = Path(project_root).resolve()
    ticket = Path(ticket_path)
    fields, body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    outer = resolve_project_dir(root) / "worktrees" / slug
    if not outer.is_dir():
        raise AcceptanceBasisOperationError(f"Ticket Workspace is not open: {outer}")
    _prepare_workspace_project(root, outer, ticket, slug)
    paired = paired_project_repository(outer)
    project = paired.worktree if paired is not None else None
    record_path = (
        record_relative_path(outer, project_participant=project is not None) / f"{slug}.json"
    ).as_posix()
    outer_recovery = {record_path} if project is None else set()
    project_recovery = {record_path} if project is not None else set()
    outer_changes = _validate_authoring_changes(
        outer,
        outer,
        project_repository=False,
        recovery_paths=outer_recovery,
    )
    project_changes = (
        _validate_authoring_changes(
            project,
            outer,
            True,
            recovery_paths=project_recovery,
        )
        if project is not None
        else []
    )
    errors = _basis_validation(
        fields,
        body,
        outer,
        _changed_targets(outer, outer_changes, project, project_changes),
    )
    if errors:
        raise AcceptanceBasisOperationError(
            "Acceptance Basis validation failed: " + "; ".join(errors)
        )
    return _BasisPreparation(ticket, fields, outer, outer_changes, project, project_changes)


def _participant_preparations(
    slug: str,
    fields: dict[str, object],
    prepared: _BasisPreparation,
) -> tuple[ParticipantPreparation, ...]:
    """Freeze repository routing and staged trees before creating commits."""
    destination = fields.get("branch")
    if not isinstance(destination, str) or not destination:
        raise AcceptanceBasisOperationError("ticket has no destination branch")
    outer_parent, outer_tree = _staged_tree(prepared.outer, prepared.outer_changes)
    participants = [
        ParticipantPreparation(
            role="outer",
            ticket_ref=f"refs/heads/{_current_branch(prepared.outer)}",
            destination_ref=f"refs/heads/{destination}",
            destination_sha=_full_commit(prepared.outer, destination),
            expected_old_sha=outer_parent,
            tree_sha=outer_tree,
            message=f"chore({slug}): publish Acceptance Basis",
        )
    ]
    if prepared.project is not None:
        project_parent, project_tree = _staged_tree(prepared.project, prepared.project_changes)
        project_destination = fields.get("project_destination_ref")
        if not isinstance(project_destination, str) or not project_destination.startswith(
            "refs/heads/"
        ):
            raise AcceptanceBasisOperationError(
                "paired Ticket has no canonical project_destination_ref"
            )
        upstream = _require_git(
            prepared.project, "rev-parse", "--symbolic-full-name", "@{upstream}"
        )
        if upstream != project_destination:
            raise AcceptanceBasisOperationError(
                "paired Ticket Workspace upstream changed after authoring; "
                f"expected {project_destination}, found {upstream}"
            )
        participants.append(
            ParticipantPreparation(
                role="project",
                ticket_ref=f"refs/heads/{_current_branch(prepared.project)}",
                destination_ref=project_destination,
                destination_sha=_full_commit(prepared.project, project_destination),
                expected_old_sha=project_parent,
                tree_sha=project_tree,
                message=f"chore({slug}): publish project Acceptance Basis",
            )
        )
    return tuple(sorted(participants, key=lambda item: item.role))


def _record_path(prepared: _BasisPreparation, slug: str) -> tuple[Path, bool]:
    project_owner = prepared.project is not None
    owner = prepared.project if project_owner else prepared.outer
    if owner is None:
        raise AcceptanceBasisOperationError("Acceptance Basis record has no repository owner")
    relative_dir = record_relative_path(prepared.outer, project_participant=project_owner)
    return owner / relative_dir / f"{slug}.json", project_owner


def _write_authored_record(
    prepared: _BasisPreparation,
    slug: str,
    fields: dict[str, object],
    body: str,
) -> tuple:
    binding_specs = criterion_targets(fields.get("criteria"))
    bindings = canonical_acceptance_bindings(prepared.outer, binding_specs)
    try:
        payload = canonical_json(authored_ticket_record(fields, body, bindings))
    except AcceptanceBasisError as exc:
        raise AcceptanceBasisOperationError(str(exc)) from exc
    path, project_owner = _record_path(prepared, slug)
    try:
        atomic_write_once(path, payload, mode=0o644)
    except WriteOnceConflictError as exc:
        raise AcceptanceBasisOperationError(
            f"Acceptance Basis record already exists: {path}"
        ) from exc
    repository = prepared.project if project_owner else prepared.outer
    changes = prepared.project_changes if project_owner else prepared.outer_changes
    if repository is None:
        raise AcceptanceBasisOperationError("Acceptance Basis record owner disappeared")
    relative = path.relative_to(repository).as_posix()
    if relative not in changes:
        changes.append(relative)
    return bindings


def prepare_acceptance_basis(
    project_root: Path | str,
    ticket_path: Path | str,
    slug: str,
    *,
    effective_fields: dict[str, object] | None = None,
) -> tuple[AcceptanceBasis, str]:
    """Validate and commit authoring state without publishing the Board transition."""
    root = Path(project_root).resolve()
    ticket = Path(ticket_path)
    source_sha256 = hashlib.sha256(ticket.read_bytes()).hexdigest()
    fields, body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    if effective_fields is not None:
        fields = effective_fields
    effective_sha256 = hashlib.sha256(canonical_json({"fields": fields, "body": body})).hexdigest()
    existing = load_basis_publication(root, slug)
    if existing is not None:
        return publish_basis_commits(
            root,
            slug,
            source_sha256,
            effective_sha256,
            _authoring_repositories(root, slug),
        )
    prepared = _prepare_basis(project_root, ticket_path, slug)
    basis_inputs = _prepare_basis_inputs(prepared, slug, fields, body)
    bindings, removals = basis_inputs
    participants = _participant_preparations(slug, fields, prepared)
    return publish_basis_commits(
        root,
        slug,
        source_sha256,
        effective_sha256,
        _authoring_repositories(root, slug),
        operation_id=secrets.token_hex(16),
        participants=participants,
        bindings=bindings,
        removal_targets=removals,
    )


def _prepare_basis_inputs(
    prepared: _BasisPreparation,
    slug: str,
    fields: dict[str, object],
    body: str,
) -> tuple[tuple, tuple[str, ...]]:
    from .target_finalization import canonical_remove_targets

    removals = tuple(canonical_remove_targets(fields, prepared.outer))
    bindings = tuple(_write_authored_record(prepared, slug, fields, body))
    return bindings, removals


def _authoring_repositories(root: Path, slug: str) -> dict[str, Path]:
    outer = resolve_project_dir(root) / "worktrees" / slug
    repositories = {"outer": outer}
    paired = paired_project_repository(outer)
    if paired is not None:
        repositories["project"] = paired.worktree
    return repositories


def _require_ancestor(repository: Path, ancestor: str, descendant: str, message: str) -> None:
    result = _git(repository, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise AcceptanceBasisOperationError(message)
    detail = (result.stderr or result.stdout).strip()
    raise AcceptanceBasisOperationError(
        f"git merge-base --is-ancestor failed in {repository} (rc={result.returncode}): {detail}"
    )


def pin_basis_refs(
    project_root: Path | str,
    basis: AcceptanceBasis,
    *,
    slug: str,
    destination_branch: str,
    exact_ticket_heads: bool = False,
    exact_destination_heads: bool = False,
) -> dict[str, str]:
    """Validate canonical routing and resolve mutable Ticket refs once."""
    root = Path(project_root).resolve()
    source = resolve_inner_project_repo(root)
    participants = {participant.role: participant for participant in basis.participants}
    expected_roles = {"outer", "project"} if source is not None else {"outer"}
    if set(participants) != expected_roles:
        raise AcceptanceBasisOperationError(
            "Acceptance Basis participants do not match this project"
        )
    sources: dict[str, str] = {}
    for role, participant in participants.items():
        repository = root if role == "outer" else source
        if repository is None:
            raise AcceptanceBasisOperationError(
                f"Acceptance Basis {role} repository is unavailable"
            )
        sources[role] = _validate_basis_participant(
            repository,
            participant,
            slug=slug,
            destination_branch=destination_branch,
            exact_ticket_head=exact_ticket_heads,
            exact_destination_head=exact_destination_heads,
        )
    return sources


def _validate_basis_participant(
    repository: Path,
    participant: BasisParticipant,
    *,
    slug: str,
    destination_branch: str,
    exact_ticket_head: bool,
    exact_destination_head: bool,
) -> str:
    role = participant.role
    ticket_ref = participant.ticket_ref
    destination_ref = participant.destination_ref
    if role == "outer" and destination_ref != f"refs/heads/{destination_branch}":
        raise AcceptanceBasisOperationError(
            f"Acceptance Basis outer destination does not match Ticket {slug!r}"
        )
    authoring = _full_commit(repository, participant.authoring_sha)
    destination_identity = _full_commit(repository, participant.destination_sha)
    destination = _full_commit(repository, destination_ref)
    source_sha = _full_commit(repository, ticket_ref)
    if exact_ticket_head and source_sha != authoring:
        raise AcceptanceBasisOperationError(
            f"ticket ref {ticket_ref!r} moved after enqueue preparation"
        )
    if exact_destination_head and destination != destination_identity:
        raise AcceptanceBasisOperationError(
            f"destination ref {destination_ref!r} moved after enqueue preparation"
        )
    _require_ancestor(
        repository,
        authoring,
        source_sha,
        f"ticket ref {ticket_ref!r} does not descend from authored {role} commit {authoring}",
    )
    _require_ancestor(
        repository,
        destination_identity,
        destination,
        f"destination ref {destination_ref!r} rewrote its recorded history",
    )
    _require_ancestor(
        repository,
        destination_identity,
        authoring,
        f"authored {role} commit does not descend from its destination identity",
    )
    return source_sha


def validate_basis_refs(
    project_root: Path | str,
    basis: AcceptanceBasis,
    *,
    slug: str,
    destination_branch: str,
    exact_ticket_heads: bool = False,
    exact_destination_heads: bool = False,
) -> list[str]:
    """Verify canonical Acceptance Basis refs without materialized worktrees."""
    try:
        pin_basis_refs(
            project_root,
            basis,
            slug=slug,
            destination_branch=destination_branch,
            exact_ticket_heads=exact_ticket_heads,
            exact_destination_heads=exact_destination_heads,
        )
    except (AcceptanceBasisOperationError, ValueError) as exc:
        return [str(exc)]
    return []


def reset_basis_worktrees(
    project_root: Path | str,
    slug: str,
    basis: AcceptanceBasis,
    requested_branch: str,
    *,
    plan: BasisResetPlan | None = None,
) -> None:
    """Discard implementation state and restore the recorded authoring checkouts."""
    current_plan = plan or preflight_basis_reset(project_root, slug, basis, requested_branch)
    root = Path(project_root).resolve()
    if (
        current_plan.root != root
        or current_plan.basis_id != basis.basis_id
        or current_plan.requested_branch != requested_branch
    ):
        raise AcceptanceBasisOperationError(
            "Acceptance Basis reset plan does not match this request"
        )
    _apply_basis_reset(current_plan, basis, slug)


def _apply_basis_reset(
    plan: BasisResetPlan,
    basis: AcceptanceBasis,
    slug: str,
) -> None:
    """Apply one fully validated reset plan, preserving publish-last ordering."""
    root = plan.root
    source = plan.project_source
    outer = plan.outer_worktree
    _remove_authoring_worktrees(
        root,
        outer,
        plan.paired_worktree,
        source,
    )
    publication_order = {"project": 0, "outer": 1}
    for item in sorted(
        plan.participants,
        key=lambda row: publication_order[row.participant.role],
    ):
        _require_git(
            item.repository,
            "update-ref",
            item.participant.ticket_ref,
            item.participant.authoring_sha,
            item.expected_head,
        )
    outer_participant = basis.participant("outer")
    outer_branch = outer_participant.ticket_ref.removeprefix("refs/heads/")
    _attach_worktree(root, outer, outer_branch, outer_participant.authoring_sha)
    _restore_project_workspace(source, outer, basis)
    errors = validate_basis_refs(
        root,
        basis,
        slug=slug,
        destination_branch=plan.requested_branch,
        exact_ticket_heads=True,
    )
    if errors:
        raise AcceptanceBasisOperationError(
            "could not restore the Acceptance Basis: " + "; ".join(errors)
        )


def preflight_basis_reset(
    project_root: Path | str,
    slug: str,
    basis: AcceptanceBasis,
    requested_branch: str,
) -> BasisResetPlan:
    """Validate every reset identity and checkout before any state is mutated."""
    root = Path(project_root).resolve()
    source = resolve_inner_project_repo(root)
    _validate_reset_project_source(source, basis)
    heads = pin_basis_refs(
        root,
        basis,
        slug=slug,
        destination_branch=requested_branch,
    )
    outer = resolve_project_dir(root) / "worktrees" / slug
    paired = paired_project_repository(outer) if outer.is_dir() else None
    _validate_reset_worktrees(root, source, outer, paired, basis)
    reset_participants = _reset_participants(root, source, basis, heads)
    return BasisResetPlan(
        root,
        source,
        outer,
        paired,
        reset_participants,
        basis.basis_id,
        requested_branch,
    )


def _validate_reset_worktrees(
    root: Path,
    source: Path | None,
    outer: Path,
    paired: TicketRepository | None,
    basis: AcceptanceBasis,
) -> None:
    outer_participant = basis.participant("outer")
    outer_branch = outer_participant.ticket_ref.removeprefix("refs/heads/")
    if outer.exists() and not _worktree_owns_branch(root, outer, outer_branch):
        raise AcceptanceBasisOperationError(
            f"Ticket Workspace {outer} does not own {outer_participant.ticket_ref}"
        )
    if paired is not None:
        if source is None:
            raise AcceptanceBasisOperationError(
                "Acceptance Basis project repository is unavailable"
            )
        project_participant = basis.participant("project")
        branch = project_participant.ticket_ref.removeprefix("refs/heads/")
        if not _worktree_owns_branch(source, paired.worktree, branch):
            raise AcceptanceBasisOperationError(
                f"paired Ticket Workspace {paired.worktree} does not own "
                f"{project_participant.ticket_ref}"
            )


def _reset_participants(
    root: Path,
    source: Path | None,
    basis: AcceptanceBasis,
    heads: dict[str, str],
) -> tuple[_ResetParticipant, ...]:
    reset_participants: list[_ResetParticipant] = []
    for participant in basis.participants:
        repository = root if participant.role == "outer" else source
        if repository is None:
            raise AcceptanceBasisOperationError(
                f"Acceptance Basis {participant.role} repository is unavailable"
            )
        reset_participants.append(
            _ResetParticipant(repository, participant, heads[participant.role])
        )
    return tuple(reset_participants)


def _validate_reset_project_source(source: Path | None, basis: AcceptanceBasis) -> None:
    if basis.project_sha and source is None:
        raise AcceptanceBasisOperationError("Acceptance Basis project repository is unavailable")
    if source is not None and not basis.project_sha:
        raise AcceptanceBasisOperationError("Acceptance Basis has no project repository commit")
    if source is not None:
        _full_commit(source, basis.project_sha)


def _restore_project_workspace(
    source: Path | None,
    outer: Path,
    basis: AcceptanceBasis,
) -> None:
    if source is None:
        return
    participant = basis.participant("project")
    branch = participant.ticket_ref.removeprefix("refs/heads/")
    destination = ticket_project_worktree(outer)
    _attach_worktree(source, destination, branch, participant.authoring_sha)
    base_branch = participant.destination_ref.removeprefix("refs/heads/")
    _require_git(source, "branch", f"--set-upstream-to={base_branch}", branch)


def _remove_authoring_worktrees(
    root: Path,
    outer: Path,
    paired: TicketRepository | None,
    project_source: Path | None,
) -> None:
    if paired is not None and project_source is not None:
        _require_git(project_source, "worktree", "remove", "--force", str(paired.worktree))
    if outer.exists():
        _require_git(root, "worktree", "remove", "--force", str(outer))
