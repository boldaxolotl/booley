"""Private authoring worktrees and Acceptance Basis publication helpers."""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from booley.fusesoc import fusesoc_registry
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.project_dir import (
    checkout_project_dir_relative_to,
    resolve_project_dir,
    runtime_dir,
)
from booley.runtime.project_prepare import prepare_project
from booley.runtime.ticket_repositories import (
    TicketRepository,
    paired_project_repository,
    resolve_inner_project_repo,
    ticket_project_worktree,
)
from booley.ticket_board.contract_path_policy import is_static_contract_path

from .acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
    authored_ticket_record,
    canonical_json,
    record_relative_path,
)
from .acceptance_targets import (
    canonical_contract_bindings,
    contract_control_paths,
    criterion_targets,
    resolve_commit,
    validate_criterion_targets,
    validate_targets_for_seal,
)
from .frontmatter import parse_frontmatter
from .git_status import parse_porcelain_v1_z
from .persistence import WriteOnceConflictError, atomic_write_once
from .validation import validate_ticket_fields

_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_GENERATION_PREFIX = "booley-generation"


class ContractOperationError(RuntimeError):
    """A contract authoring or sealing transaction could not complete."""


@dataclass(frozen=True)
class ContractWorktrees:
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
class _SealInputs:
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


def _strict_branch_sha(repository: Path, branch: str) -> str | None:
    ref = f"refs/heads/{branch}"
    result = _git(repository, "rev-parse", "--verify", "--quiet", ref)
    if result.returncode == 0:
        return resolve_commit(repository, result.stdout.strip())
    if result.returncode == 1:
        return None
    detail = (result.stderr or result.stdout).strip()
    raise ContractOperationError(
        f"could not inspect contract branch {branch!r} in {repository} "
        f"(rc={result.returncode}): {detail}"
    )


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
    if requested.startswith("refs/heads/"):
        requested = requested.removeprefix("refs/heads/")
    if _strict_branch_sha(repository, requested) is None:
        raise ContractOperationError(
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
        raise ContractOperationError("project_destination_ref must be a full refs/heads/... name")
    base_branch = _project_base_branch(source, requested)
    base_sha = _full_commit(source, base_branch)
    return _ProjectOpenPlan(source, base_branch, base_sha)


def _plan_open_attachment(
    repository: Path, worktree: Path, branch: str, base_sha: str
) -> _OpenAttachment:
    existing_sha = _strict_branch_sha(repository, branch)
    if existing_sha is not None and existing_sha != base_sha:
        raise ContractOperationError(
            f"contract branch {branch!r} already points at {existing_sha[:12]}, "
            f"not destination baseline {base_sha[:12]}"
        )
    if worktree.exists() or worktree.is_symlink():
        raise ContractOperationError(f"contract worktree path already exists: {worktree}")
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
    """Whether an existing worktree proves ownership of a legacy ticket branch."""
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
        except ContractOperationError as exc:
            current = _strict_branch_sha(attachment.repository, attachment.branch)
            if current is not None:
                raise ContractOperationError(
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
    except ContractOperationError:
        attachment.partial_path = attachment.worktree.exists() or attachment.worktree.is_symlink()
        raise
    attachment.worktree_attached = True
    if _full_commit(attachment.worktree, "HEAD") != attachment.base_sha:
        raise ContractOperationError(
            f"contract destination moved while attaching {attachment.branch!r}"
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
    except ContractOperationError:
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
    except ContractOperationError as exc:
        return str(exc)
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip()
    return f"could not restore upstream for {attachment.branch!r}: {detail}"


def _remove_attachment_worktree(attachment: _OpenAttachment) -> tuple[list[str], bool]:
    failures: list[str] = []
    try:
        registered = _registered_worktree(attachment.repository, attachment.worktree)
    except ContractOperationError as exc:
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
        except ContractOperationError as exc:
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
    except ContractOperationError as exc:
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
            except ContractOperationError as exc:
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
        raise ContractOperationError(f"destination branch {outer_branch!r} moved during preflight")
    if project is None:
        return
    if _full_commit(project.source, project.base_branch) != project.base_sha:
        raise ContractOperationError(
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
            raise ContractOperationError(f"invalid draft generation descriptor: {path}") from exc
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value):
            return value
        raise ContractOperationError(f"invalid draft generation descriptor: {path}")
    value = secrets.token_hex(8)
    payload = (json.dumps({"generation": value}, sort_keys=True) + "\n").encode()
    try:
        created = atomic_write_once(path, payload)
    except WriteOnceConflictError as exc:
        raise ContractOperationError(f"conflicting draft generation descriptor: {path}") from exc
    if not created:
        return _draft_generation(root, slug)
    return value


def _generation_branch(generation: str, slug: str) -> str:
    return f"{_GENERATION_PREFIX}/{generation}/{slug}"


def open_contract(
    project_root: Path | str,
    ticket_path: Path | str,
    slug: str,
) -> ContractWorktrees:
    """Create the outer and optional project-data authoring worktrees."""
    if not _SAFE_SLUG_RE.fullmatch(slug):
        raise ContractOperationError(f"unsafe ticket slug: {slug!r}")
    root = Path(project_root).resolve()
    fields, _body = parse_frontmatter(Path(ticket_path).read_text(encoding="utf-8"))
    branch = fields.get("branch")
    if not isinstance(branch, str) or not branch:
        raise ContractOperationError("ticket has no destination branch")
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
) -> ContractWorktrees:
    branch = fields.get("branch")
    if not isinstance(branch, str) or not branch:
        raise ContractOperationError("ticket has no destination branch")
    ticket_branch = _generation_branch(generation, slug)
    project_plan = _preflight_project_repository(
        root, branch, fields.get("project_destination_ref")
    )
    outer_base = _full_commit(root, branch)
    if outer.is_dir() and _worktree_owns_branch(root, outer, ticket_branch):
        paired = _resume_project_attachment(root, ticket, slug, outer, ticket_branch, project_plan)
        return ContractWorktrees(
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
        _prepare_contract_project(root, outer, ticket, slug)
    except Exception as exc:
        rollback_failures = _rollback_open(outer_attachment, project_attachment)
        if rollback_failures:
            raise ContractOperationError(
                f"contract opening failed: {exc}; rollback incomplete: "
                + "; ".join(rollback_failures)
            ) from exc
        raise
    project = project_attachment.worktree if project_attachment is not None else None
    project_base = project_plan.base_sha if project_plan is not None else ""
    return ContractWorktrees(outer, project, outer_base, project_base, generation)


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
            raise ContractOperationError("unexpected paired project contract worktree")
        _prepare_contract_project(root, outer, ticket, slug)
        return None
    if paired is not None:
        if not _worktree_owns_branch(project.source, paired.worktree, ticket_branch):
            raise ContractOperationError("paired project contract worktree has the wrong branch")
        _prepare_contract_project(root, outer, ticket, slug)
        return paired.worktree
    attachment = _project_open_attachment(project, outer, ticket_branch)
    if attachment is None:  # Defensive: project is known to be present above.
        raise ContractOperationError("paired project attachment could not be planned")
    try:
        _create_attachment(attachment)
        _set_attachment_upstream(attachment, project.base_branch)
        _prepare_contract_project(root, outer, ticket, slug)
    except Exception as exc:
        failures, _clear = _rollback_attachment(attachment)
        if failures:
            raise ContractOperationError(
                f"project attachment recovery failed: {exc}; rollback incomplete: "
                + "; ".join(failures)
            ) from exc
        raise
    return attachment.worktree


def _prepare_contract_project(root: Path, outer: Path, ticket: Path, slug: str) -> None:
    """Run the same deterministic preparation used by ticket execution."""
    from booley.flows.execution import flow_enabled

    result = prepare_project(
        root,
        outer,
        slug=slug,
        ticket_path=ticket,
        sim_flow_enabled=flow_enabled("sim", outer),
    )
    if not result.ok:
        raise ContractOperationError(result.error)


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
    try:
        prefix = checkout_project_dir_relative_to(surface_root).as_posix().rstrip("/") + "/"
    except (FileNotFoundError, ValueError) as exc:
        raise ContractOperationError(str(exc)) from exc
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
    from .target_finalization import validate_remove_targets_for_seal

    errors.extend(validate_remove_targets_for_seal(fields, worktree))
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
    _prepare_contract_project(root, outer, ticket, slug)
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


def _sealed_participants(
    slug: str,
    fields: dict[str, object],
    outer: Path,
    outer_sha: str,
    project: Path | None,
    project_sha: str,
) -> tuple[BasisParticipant, ...]:
    """Freeze repository routing while every authoring checkout is available."""
    destination = fields.get("branch")
    if not isinstance(destination, str) or not destination:
        raise ContractOperationError("ticket has no destination branch")
    participants = [
        BasisParticipant(
            role="outer",
            authoring_sha=outer_sha,
            ticket_ref=f"refs/heads/{_current_branch(outer)}",
            destination_ref=f"refs/heads/{destination}",
            destination_sha=_full_commit(outer, destination),
        )
    ]
    if project is not None:
        upstream = _require_git(project, "rev-parse", "--abbrev-ref", "@{upstream}")
        participants.append(
            BasisParticipant(
                role="project",
                authoring_sha=project_sha,
                ticket_ref=f"refs/heads/{_current_branch(project)}",
                destination_ref=f"refs/heads/{upstream}",
                destination_sha=_full_commit(project, upstream),
            )
        )
    return tuple(participants)


def _record_path(prepared: _SealInputs, slug: str) -> tuple[Path, bool]:
    project_owner = prepared.project is not None
    owner = prepared.project if project_owner else prepared.outer
    if owner is None:
        raise ContractOperationError("Acceptance Basis record has no repository owner")
    relative_dir = record_relative_path(prepared.outer, project_participant=project_owner)
    return owner / relative_dir / f"{slug}.json", project_owner


def _write_authored_record(
    prepared: _SealInputs,
    slug: str,
    fields: dict[str, object],
    body: str,
) -> tuple:
    binding_specs = criterion_targets(fields.get("criteria"))
    bindings = canonical_contract_bindings(prepared.outer, binding_specs)
    try:
        payload = canonical_json(authored_ticket_record(fields, body, bindings))
    except AcceptanceBasisError as exc:
        raise ContractOperationError(str(exc)) from exc
    path, project_owner = _record_path(prepared, slug)
    try:
        atomic_write_once(path, payload, mode=0o644)
    except WriteOnceConflictError as exc:
        raise ContractOperationError(f"Acceptance Basis record already exists: {path}") from exc
    repository = prepared.project if project_owner else prepared.outer
    changes = prepared.project_changes if project_owner else prepared.outer_changes
    if repository is None:
        raise ContractOperationError("Acceptance Basis record owner disappeared")
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
) -> AcceptanceBasis:
    """Validate and commit authoring state without publishing the Board transition."""
    prepared = _prepare_seal(project_root, ticket_path, slug)
    fields, body = parse_frontmatter(prepared.ticket.read_text(encoding="utf-8"))
    if effective_fields is not None:
        fields = effective_fields
    basis_inputs = _prepare_basis_inputs(prepared, slug, fields, body)
    return _commit_acceptance_basis(prepared, slug, fields, *basis_inputs)


def _prepare_basis_inputs(
    prepared: _SealInputs,
    slug: str,
    fields: dict[str, object],
    body: str,
) -> tuple[tuple, tuple[str, ...]]:
    from .target_finalization import canonical_remove_targets

    removals = tuple(canonical_remove_targets(fields, prepared.outer))
    bindings = tuple(_write_authored_record(prepared, slug, fields, body))
    return bindings, removals


def _commit_acceptance_basis(
    prepared: _SealInputs,
    slug: str,
    fields: dict[str, object],
    bindings: tuple,
    removals: tuple[str, ...],
) -> AcceptanceBasis:
    outer_start = _full_commit(prepared.outer, "HEAD")
    project_start = _full_commit(prepared.project, "HEAD") if prepared.project is not None else ""
    outer_sha = outer_start
    project_sha = project_start
    created_keepalives: list[tuple[Path, str, str]] = []
    try:
        if prepared.project is not None:
            project_sha = _commit_changes(
                prepared.project,
                prepared.project_changes,
                f"chore({slug}): publish project Acceptance Basis",
            )
        outer_sha = _commit_changes(
            prepared.outer,
            prepared.outer_changes,
            f"chore({slug}): publish Acceptance Basis",
        )
        basis = AcceptanceBasis(
            _sealed_participants(
                slug,
                fields,
                prepared.outer,
                outer_sha,
                prepared.project,
                project_sha,
            ),
            bindings,
            removals,
        )
        created_keepalives = _publish_basis_keepalives(
            basis,
            prepared.outer,
            prepared.project,
        )
    except Exception:
        for repository, ref, sha in reversed(created_keepalives):
            _git(repository, "update-ref", "-d", ref, sha)
        _restore_unpublished_commit(prepared.outer, outer_start, outer_sha)
        if prepared.project is not None:
            _restore_unpublished_commit(prepared.project, project_start, project_sha)
        raise
    return basis


def _publish_basis_keepalives(
    basis: AcceptanceBasis,
    outer: Path,
    project: Path | None,
) -> list[tuple[Path, str, str]]:
    created: list[tuple[Path, str, str]] = []
    try:
        for participant in basis.participants:
            repository = outer if participant.role == "outer" else project
            if repository is None:
                raise ContractOperationError(
                    f"Acceptance Basis {participant.role} repository disappeared"
                )
            ref = f"refs/booley/bases/{basis.basis_id}/{participant.role}"
            current = _git(repository, "rev-parse", "--verify", "--quiet", ref)
            if current.returncode == 0:
                if current.stdout.strip() != participant.authoring_sha:
                    raise ContractOperationError(f"keepalive ref {ref} names another commit")
                continue
            _require_git(repository, "update-ref", ref, participant.authoring_sha, "")
            created.append((repository, ref, participant.authoring_sha))
    except Exception:
        for repository, ref, sha in reversed(created):
            _git(repository, "update-ref", "-d", ref, sha)
        raise
    return created


def _restore_unpublished_commit(repository: Path, start_sha: str, current_sha: str) -> None:
    """Move an unpublished branch back while preserving authored changes staged."""
    if current_sha != start_sha:
        _require_git(repository, "reset", "--soft", start_sha)


def _require_ancestor(repository: Path, ancestor: str, descendant: str, message: str) -> None:
    result = _git(repository, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise ContractOperationError(message)
    detail = (result.stderr or result.stdout).strip()
    raise ContractOperationError(
        f"git merge-base --is-ancestor failed in {repository} (rc={result.returncode}): {detail}"
    )


def pin_basis_refs(
    project_root: Path | str,
    contract: AcceptanceBasis,
    *,
    slug: str,
    destination_branch: str,
    exact_ticket_heads: bool = False,
) -> dict[str, str]:
    """Validate canonical routing and resolve mutable Ticket refs once."""
    root = Path(project_root).resolve()
    source = resolve_inner_project_repo(root)
    participants = {participant.role: participant for participant in contract.participants}
    expected_roles = {"outer", "project"} if source is not None else {"outer"}
    if set(participants) != expected_roles:
        raise ContractOperationError("Acceptance Basis participants do not match this project")
    sources: dict[str, str] = {}
    for role, participant in participants.items():
        repository = root if role == "outer" else source
        if repository is None:
            raise ContractOperationError(f"Acceptance Basis {role} repository is unavailable")
        sources[role] = _validate_basis_participant(
            repository,
            participant,
            slug=slug,
            destination_branch=destination_branch,
            exact_ticket_head=exact_ticket_heads,
        )
    return sources


def _validate_basis_participant(
    repository: Path,
    participant: BasisParticipant,
    *,
    slug: str,
    destination_branch: str,
    exact_ticket_head: bool,
) -> str:
    role = participant.role
    ticket_ref = participant.ticket_ref
    destination_ref = participant.destination_ref
    if role == "outer" and destination_ref != f"refs/heads/{destination_branch}":
        raise ContractOperationError(
            f"Acceptance Basis outer destination does not match Ticket {slug!r}"
        )
    authoring = _full_commit(repository, participant.authoring_sha)
    destination_identity = _full_commit(repository, participant.destination_sha)
    destination = _full_commit(repository, destination_ref)
    source_sha = _full_commit(repository, ticket_ref)
    if exact_ticket_head and source_sha != authoring:
        raise ContractOperationError(f"ticket ref {ticket_ref!r} moved after enqueue preparation")
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
    contract: AcceptanceBasis,
    *,
    slug: str,
    destination_branch: str,
    exact_ticket_heads: bool = False,
) -> list[str]:
    """Verify canonical Acceptance Basis refs without materialized worktrees."""
    try:
        pin_basis_refs(
            project_root,
            contract,
            slug=slug,
            destination_branch=destination_branch,
            exact_ticket_heads=exact_ticket_heads,
        )
    except (ContractOperationError, ValueError) as exc:
        return [str(exc)]
    return []


def reset_basis_worktrees(
    project_root: Path | str,
    slug: str,
    contract: AcceptanceBasis,
    requested_branch: str,
) -> None:
    """Discard implementation state and restore the recorded authoring checkouts."""
    root = Path(project_root).resolve()
    _full_commit(root, contract.outer_sha)
    source = resolve_inner_project_repo(root)
    _validate_reset_project_source(source, contract)
    outer = resolve_project_dir(root) / "worktrees" / slug
    paired = paired_project_repository(outer) if outer.is_dir() else None
    _remove_contract_worktrees(root, outer, paired, source)
    outer_participant = next(row for row in contract.participants if row.role == "outer")
    outer_branch = outer_participant.ticket_ref.removeprefix("refs/heads/")
    _require_git(root, "update-ref", outer_participant.ticket_ref, contract.outer_sha)
    _attach_worktree(root, outer, outer_branch, contract.outer_sha)
    _restore_project_contract(source, outer, slug, requested_branch, contract)
    errors = validate_basis_refs(root, contract, slug=slug, destination_branch=requested_branch)
    if errors:
        raise ContractOperationError(
            "could not restore the Acceptance Basis: " + "; ".join(errors)
        )


def _validate_reset_project_source(source: Path | None, contract: AcceptanceBasis) -> None:
    if contract.project_sha and source is None:
        raise ContractOperationError("Acceptance Basis project repository is unavailable")
    if source is not None and not contract.project_sha:
        raise ContractOperationError("Acceptance Basis has no project repository commit")
    if source is not None:
        _full_commit(source, contract.project_sha)


def _restore_project_contract(
    source: Path | None,
    outer: Path,
    slug: str,
    requested_branch: str,
    contract: AcceptanceBasis,
) -> None:
    if source is None:
        return
    participant = next(row for row in contract.participants if row.role == "project")
    branch = participant.ticket_ref.removeprefix("refs/heads/")
    destination = ticket_project_worktree(outer)
    _require_git(source, "update-ref", participant.ticket_ref, contract.project_sha)
    _attach_worktree(source, destination, branch, contract.project_sha)
    base_branch = participant.destination_ref.removeprefix("refs/heads/")
    _require_git(source, "branch", f"--set-upstream-to={base_branch}", branch)


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
