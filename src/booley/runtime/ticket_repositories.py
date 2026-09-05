"""Repository routing for Ticket Mode's paired project-data checkout."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from booley.ticket_board.workspace_ops import AuthoringWorkspace

from booley.runtime import git as runtime_git
from booley.runtime.agent_errors import BlockingError
from booley.runtime.filesystem_utils import safe_rmtree
from booley.runtime.project_dir import (
    PROJECT_DIR_NAME,
    resolve_checkout_project_dir,
    resolve_project_dir,
)

PROJECT_BRANCH_PREFIX = "booley-ticket/"
PROJECT_BOARD_PREFIX = "tickets/board/"


@dataclass(frozen=True)
class TicketRepository:
    """One repository participating in a ticket handoff."""

    worktree: Path
    path_prefix: str = ""

    def local_path(self, ticket_path: str) -> str:
        """Translate a ticket-root-relative path into this repository."""
        if not self.path_prefix:
            return ticket_path
        prefix = f"{self.path_prefix}/"
        if not ticket_path.startswith(prefix):
            raise ValueError(f"path {ticket_path!r} is outside {self.path_prefix!r}")
        return ticket_path.removeprefix(prefix)

    def ticket_path(self, local_path: str) -> str:
        """Translate a repository-relative path into the ticket checkout."""
        return f"{self.path_prefix}/{local_path}" if self.path_prefix else local_path


@dataclass(frozen=True)
class ProjectRepositoryChange:
    """One uncommitted path routed through a Ticket Workspace."""

    path: str
    status: str


class ProjectRepositoryStatusError(RuntimeError):
    """Raised when Git cannot report project-repository changes."""


class TicketWorkspaceError(RuntimeError):
    """A Ticket Workspace operation could not preserve its invariants."""


class WorkspaceMode(Enum):
    """Whether repository state is new or intentionally preserved."""

    FRESH = "fresh"
    RESUME = "resume"


class WorkspaceDisposition(Enum):
    """How a completed Ticket Workspace should be retired."""

    KEEP = "keep"
    MERGE = "merge"
    DISCARD = "discard"


@dataclass(frozen=True)
class TicketWorkspaceRequest:
    """Stable facts required to open one Ticket Workspace."""

    project_root: Path
    worktree: Path
    ticket_slug: str
    base: str
    ticket_scope: tuple[str, ...]
    mode: WorkspaceMode
    expected_sha: str = ""
    expected_ref: str = ""


class TicketWorkspace:
    """Own repository routing for one Ticket's authored changes."""

    def __init__(self, request: TicketWorkspaceRequest) -> None:
        self.request = request

    @classmethod
    def ensure_authoring(
        cls,
        project_root: Path | str,
        ticket_path: Path | str,
        slug: str,
    ) -> AuthoringWorkspace:
        """Idempotently materialize the Ticket generation's authoring checkout set."""
        from booley.ticket_board.workspace_ops import ensure_ticket_workspace

        return ensure_ticket_workspace(project_root, ticket_path, slug)

    @staticmethod
    def project_destination_ref(
        project_root: Path | str,
        destination_branch: str,
        requested_ref: str = "",
    ) -> str:
        """Resolve the paired project destination to a full local branch ref."""
        root = Path(project_root).resolve()
        source = resolve_inner_project_repo(root)
        if source is None:
            if requested_ref:
                raise TicketWorkspaceError(
                    "project_destination_ref requires a standalone project repository"
                )
            return ""
        ref = requested_ref or f"refs/heads/{destination_branch}"
        if not ref.startswith("refs/heads/"):
            raise TicketWorkspaceError(
                "project_destination_ref must be a full refs/heads/... name"
            )
        result = _git(source, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            suffix = f": {detail}" if detail else ""
            raise TicketWorkspaceError(
                f"paired project destination {ref!r} does not exist as a local branch{suffix}"
            )
        return ref

    @classmethod
    def open(cls, request: TicketWorkspaceRequest) -> TicketWorkspace:
        """Validate and open an existing outer Ticket worktree."""
        result = _git(request.worktree, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            raise TicketWorkspaceError(f"Ticket worktree is unavailable: {request.worktree}")
        try:
            top = Path(result.stdout.strip()).resolve()
        except OSError as exc:
            raise TicketWorkspaceError(
                f"Ticket worktree cannot be resolved: {request.worktree}"
            ) from exc
        if top != request.worktree.resolve():
            raise TicketWorkspaceError(f"Ticket worktree is not a Git root: {request.worktree}")
        return cls(request)

    @property
    def authored_project_dir(self) -> Path:
        """Project content exposed to the Developer Agent."""
        paired = paired_project_repository(self.request.worktree)
        if paired is not None:
            return paired.worktree
        return resolve_checkout_project_dir(self.request.worktree)

    def prepare(self) -> Path | None:
        """Prepare and validate the paired project checkout, when configured."""
        source = resolve_inner_project_repo(self.request.project_root)
        existing = paired_project_repository(self.request.worktree)
        if existing is not None:
            if source is None:
                raise TicketWorkspaceError(
                    "paired project checkout exists but its source repository is unavailable"
                )
            _verify_existing_worktree(
                existing.worktree,
                source,
                self.request.ticket_slug,
                self.request.expected_sha,
                self.request.expected_ref,
            )
            return existing.worktree

        if self.request.expected_ref:
            raise TicketWorkspaceError(
                "paired Acceptance Basis worktree is expected but unavailable"
            )

        if source is None:
            if scope_mentions_project_repo(
                list(self.request.ticket_scope)
            ) and _outer_ignores_project_dir(self.request.project_root):
                raise TicketWorkspaceError(
                    f"Scope names {PROJECT_DIR_NAME}/ but the hidden project directory "
                    "is not a standalone Git repository; run `booley init` and commit "
                    "the project repository before retrying"
                )
            return None

        _require_clean_source(source)
        destination = ticket_project_worktree(self.request.worktree)
        branch = project_ticket_branch(self.request.ticket_slug)
        base = _ticket_base_branch(source, self.request.base)
        _prepare_destination(destination)
        _git_or_raise(source, "worktree", "prune")
        _attach_branch(
            source,
            destination,
            branch,
            base,
            resume=self.request.mode is WorkspaceMode.RESUME,
            expected_sha=self.request.expected_sha,
        )
        _set_local_upstream(source, branch, base)
        return destination

    def pending_changes(self) -> tuple[ProjectRepositoryChange, ...]:
        """Return dirty paths across every repository in Ticket coordinates."""
        require_paired = bool(self.request.expected_sha) or (
            resolve_inner_project_repo(self.request.project_root) is not None
        )
        return pending_ticket_changes(self.request.worktree, require_paired=require_paired)

    def commit(self, paths: list[str], message: str) -> None:
        """Commit selected Ticket paths to their owning repositories."""
        repositories = ticket_repositories(self.request.worktree)
        prefixes = {
            repository.path_prefix for repository in repositories if repository.path_prefix
        }
        failures: list[str] = []
        for repository in repositories:
            selected = _paths_for_repository(repository, paths, prefixes)
            if not selected:
                continue
            try:
                runtime_git.commit_scope(repository.worktree, selected, message, literal=True)
            except BlockingError as exc:
                label = repository.path_prefix or "outer repository"
                failures.append(f"{label}: {exc}")
        if failures:
            raise TicketWorkspaceError("; ".join(failures))

    def finish(
        self,
        disposition: WorkspaceDisposition,
        message: str = "",
        *,
        cleanup: bool = True,
    ) -> tuple[bool, str]:
        """Finish the paired repository while preserving recovery state on failure."""
        return self.retire(
            self.request.project_root,
            self.request.ticket_slug,
            disposition,
            message,
            cleanup=cleanup,
        )

    @staticmethod
    def retire(
        project_root: Path,
        ticket_slug: str,
        disposition: WorkspaceDisposition,
        message: str = "",
        *,
        cleanup: bool = True,
    ) -> tuple[bool, str]:
        """Merge or discard one paired repository through the workspace boundary."""
        if disposition is WorkspaceDisposition.KEEP:
            return True, ""
        if disposition is WorkspaceDisposition.MERGE:
            ok, detail = merge_project_ticket_branch(
                project_root,
                ticket_slug,
                message,
            )
            if not ok:
                return False, detail
        if not cleanup:
            return True, ""
        if cleanup_project_ticket_branch(project_root, ticket_slug):
            return True, ""
        return False, "project repository cleanup failed"


def project_ticket_branch(slug: str) -> str:
    """Return the isolated inner-repository branch for *slug*."""
    return f"{PROJECT_BRANCH_PREFIX}{slug}"


def ticket_project_worktree(ticket_worktree: Path) -> Path:
    """Return the conventional project-data checkout nested in a ticket."""
    return ticket_worktree / PROJECT_DIR_NAME


def scope_mentions_project_repo(scope: list[str]) -> bool:
    """Whether ticket Scope explicitly names project-directory content."""
    return bool(project_repository_scope(scope))


def project_repository_scope(scope: list[str]) -> list[str]:
    """Translate ticket Scope entries for the inner repository's hook."""
    prefix = f"{PROJECT_DIR_NAME}/"
    translated: list[str] = []
    for raw_entry in scope:
        is_new = raw_entry.endswith(" [new]")
        entry = raw_entry.removesuffix(" [new]").removeprefix("./")
        if entry.rstrip("/") == PROJECT_DIR_NAME:
            translated.append("** [new]" if is_new else "**")
            continue
        if not entry.startswith(prefix):
            continue
        local = entry.removeprefix(prefix)
        translated.append(f"{local} [new]" if is_new else local)
    return translated


def resolve_inner_project_repo(project_root: Path) -> Path | None:
    """Return the resolved project dir only when it is its own Git repo."""
    configured = os.environ.get("BOOLEY_PROJECT_DIR")
    if configured and not Path(configured).is_dir():
        return None
    try:
        project_dir = resolve_project_dir(project_root).resolve()
    except FileNotFoundError:
        return None
    if not (project_dir / ".git").is_dir():
        return None
    result = _git(project_dir, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    try:
        top = Path(result.stdout.strip()).resolve()
    except OSError:
        return None
    return project_dir if top == project_dir else None


def project_repository_expected(project_root: Path) -> bool:
    """Whether this checkout is configured for a standalone project repository."""
    configured = os.environ.get("BOOLEY_PROJECT_DIR")
    if configured:
        configured_path = Path(configured)
        if (configured_path / ".git").exists():
            return True
    result = _git(project_root, "check-ignore", "-q", "--", PROJECT_DIR_NAME)
    return result.returncode == 0


def paired_project_repository(ticket_worktree: Path) -> TicketRepository | None:
    """Return the ticket's linked inner worktree, when one is installed."""
    nested = ticket_project_worktree(ticket_worktree)
    if not (nested / ".git").is_file():
        return None
    result = _git(nested, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise TicketWorkspaceError(
            f"paired project repository is unavailable at {nested}: {detail}"
        )
    try:
        top = Path(result.stdout.strip()).resolve()
        expected = nested.resolve()
    except OSError as exc:
        raise TicketWorkspaceError(
            f"paired project repository cannot be resolved at {nested}: {exc}"
        ) from exc
    if top != expected:
        raise TicketWorkspaceError(
            f"paired project repository has unexpected root {top}; expected {expected}"
        )
    return TicketRepository(nested, PROJECT_DIR_NAME)


def ticket_repositories(
    ticket_worktree: Path,
    *,
    require_paired: bool = False,
) -> tuple[TicketRepository, ...]:
    """Return every repository whose dirty state belongs to one ticket."""
    outer = TicketRepository(ticket_worktree)
    project = paired_project_repository(ticket_worktree)
    if project is None and require_paired:
        raise TicketWorkspaceError(
            f"paired project repository is expected but unavailable at "
            f"{ticket_project_worktree(ticket_worktree)}"
        )
    return (outer, project) if project is not None else (outer,)


def pending_ticket_changes(
    ticket_worktree: Path,
    *,
    require_paired: bool = False,
) -> tuple[ProjectRepositoryChange, ...]:
    """Inspect every repository without exposing path-prefix routing to callers."""
    changes: list[ProjectRepositoryChange] = []
    for repository in ticket_repositories(ticket_worktree, require_paired=require_paired):
        changes.extend(
            ProjectRepositoryChange(repository.ticket_path(change.path), change.status)
            for change in _repository_changes(repository.worktree)
        )
    return tuple(changes)


def _paths_for_repository(
    repository: TicketRepository,
    paths: list[str],
    project_prefixes: set[str],
) -> list[str]:
    if repository.path_prefix:
        prefix = f"{repository.path_prefix}/"
        selected = [path for path in paths if path.startswith(prefix)]
    else:
        selected = [
            path
            for path in paths
            if not any(path.startswith(f"{prefix}/") for prefix in project_prefixes)
        ]
    return [repository.local_path(path) for path in selected]


def _repository_changes(repository: Path) -> tuple[ProjectRepositoryChange, ...]:
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
        raise TicketWorkspaceError(
            f"git status failed in {repository} (rc={result.returncode}): {detail}"
        )
    return _parse_porcelain_z(result.stdout)


def blocking_project_repository_changes(
    repository: Path,
) -> tuple[ProjectRepositoryChange, ...]:
    """Return dirt that must block project worktree creation or merging.

    Ticket Board transitions deliberately mutate ``tickets/board/`` in the
    main project checkout. Booley leaves those moves unstaged, so they can
    coexist with a Git worktree or a merge whose branch cannot modify ticket
    state. Staged board changes remain blocking because Git may refuse to
    merge with a non-clean index.
    """
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
        raise ProjectRepositoryStatusError(
            f"git status failed in {repository} (rc={result.returncode}): {detail}"
        )
    return tuple(
        change
        for change in _parse_porcelain_z(result.stdout)
        if not _is_unstaged_board_change(change)
    )


def remove_project_worktree(project_root: Path, ticket_worktree: Path) -> None:
    """Remove a paired checkout before its containing checkout disappears."""
    nested = ticket_project_worktree(ticket_worktree)
    source = resolve_inner_project_repo(project_root)
    if source is None or not (nested / ".git").is_file():
        return
    _git(source, "worktree", "remove", "--force", str(nested))
    _git(source, "worktree", "prune")


def merge_project_ticket_branch(project_root: Path, slug: str, message: str) -> tuple[bool, str]:
    """Merge a ticket's paired project branch into its recorded base."""
    repository = resolve_inner_project_repo(project_root)
    if repository is None:
        if project_repository_expected(project_root):
            return False, "configured project repository is unavailable"
        return True, ""
    branch = project_ticket_branch(slug)
    if not _branch_exists(repository, branch):
        return False, f"expected project branch {branch!r} does not exist"
    base = _branch_upstream(repository, branch)
    if not base:
        return False, f"project branch {branch!r} has no recorded merge target"
    checkout = _find_checkout(repository, base)
    if checkout is not None:
        return _merge_in_checkout(checkout, branch, message)
    return _merge_in_temporary_worktree(repository, base, branch, message)


def cleanup_project_ticket_branch(project_root: Path, slug: str) -> bool:
    """Remove a ticket's paired project worktree and branch."""
    repository = resolve_inner_project_repo(project_root)
    if repository is None:
        return not project_repository_expected(project_root)
    branch = project_ticket_branch(slug)
    checkout = _find_checkout(repository, branch)
    if checkout is not None and checkout != repository:
        result = _git(repository, "worktree", "remove", "--force", str(checkout))
        if result.returncode != 0:
            return False
    _git(repository, "worktree", "prune")
    if not _branch_exists(repository, branch):
        return True
    return _git(repository, "branch", "-D", branch).returncode == 0


def _outer_ignores_project_dir(project_root: Path) -> bool:
    return _git(project_root, "check-ignore", "-q", "--", PROJECT_DIR_NAME).returncode == 0


def _require_clean_source(source: Path) -> None:
    try:
        blocking = blocking_project_repository_changes(source)
    except ProjectRepositoryStatusError as exc:
        raise TicketWorkspaceError(f"could not inspect project repository: {exc}") from exc
    if blocking:
        paths = ", ".join(change.path for change in blocking[:5])
        raise TicketWorkspaceError(
            f"project repository at {source} has uncommitted changes; commit or restore "
            f"them before starting or resuming a ticket: {paths}"
        )


def _current_branch(source: Path) -> str:
    result = _git(source, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.returncode != 0 or not result.stdout.strip():
        raise TicketWorkspaceError("project repository must have a checked-out base branch")
    return result.stdout.strip()


def _ticket_base_branch(source: Path, requested: str) -> str:
    if requested and _ref_sha(source, f"refs/heads/{requested}"):
        return requested
    return _current_branch(source)


def _prepare_destination(destination: Path) -> None:
    if not destination.exists():
        return
    if (destination / ".git").exists():
        raise TicketWorkspaceError(f"refusing to replace unexpected Git checkout at {destination}")
    safe_rmtree(destination)


def _attach_branch(
    source: Path,
    destination: Path,
    branch: str,
    base: str,
    *,
    resume: bool,
    expected_sha: str = "",
) -> None:
    branch_sha = _ref_sha(source, f"refs/heads/{branch}")
    base_sha = _ref_sha(source, f"refs/heads/{base}")
    if expected_sha and branch_sha != expected_sha:
        raise TicketWorkspaceError(
            f"project contract branch {branch!r} points at {branch_sha or 'nothing'}, "
            f"expected recorded project_sha {expected_sha}"
        )
    if branch_sha and not expected_sha and not resume and branch_sha != base_sha:
        raise TicketWorkspaceError(
            f"refusing to reset surviving project branch {branch!r}; resume or triage it first"
        )
    args = ("worktree", "add", str(destination), branch)
    if not branch_sha:
        args = ("worktree", "add", "-b", branch, str(destination), base)
    result = _git(source, *args)
    if result.returncode != 0:
        raise TicketWorkspaceError(
            f"could not create paired project worktree (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )


def _set_local_upstream(source: Path, branch: str, base: str) -> None:
    result = _git(source, "branch", f"--set-upstream-to={base}", branch)
    if result.returncode != 0:
        raise TicketWorkspaceError(
            f"could not record project merge target {base!r}: {result.stderr.strip()}"
        )


def _verify_existing_worktree(
    worktree: Path,
    source: Path,
    slug: str,
    expected_sha: str = "",
    expected_ref: str = "",
) -> None:
    result = _git(worktree, "branch", "--show-current")
    expected = expected_ref.removeprefix("refs/heads/") or project_ticket_branch(slug)
    if result.returncode != 0 or result.stdout.strip() != expected:
        raise TicketWorkspaceError(
            f"paired project worktree uses {result.stdout.strip()!r}, expected {expected!r}"
        )
    if _common_git_dir(source) != _common_git_dir(worktree):
        raise TicketWorkspaceError(
            "paired project checkout belongs to a different source repository"
        )
    upstream = _git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}")
    if upstream.returncode != 0 or not upstream.stdout.strip():
        raise TicketWorkspaceError(
            f"paired project branch {expected!r} has no recorded merge target"
        )
    if expected_sha and _ref_sha(worktree, "HEAD") != expected_sha:
        raise TicketWorkspaceError(
            f"paired project worktree HEAD does not match recorded project_sha {expected_sha}"
        )


def _common_git_dir(worktree: Path) -> Path | None:
    result = _git(worktree, "rev-parse", "--git-common-dir")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = worktree / path
    try:
        return path.resolve()
    except OSError:
        return None


def _ref_sha(source: Path, ref: str) -> str:
    result = _git(source, "rev-parse", "--verify", ref)
    return result.stdout.strip() if result.returncode == 0 else ""


def _parse_porcelain_z(stdout: str) -> tuple[ProjectRepositoryChange, ...]:
    """Parse NUL-delimited porcelain output, consuming rename origins."""
    fields = [field for field in stdout.split("\0") if field]
    changes: list[ProjectRepositoryChange] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        if "R" in status or "C" in status:
            index += 1
        if path:
            changes.append(ProjectRepositoryChange(path, status))
    return tuple(changes)


def _is_unstaged_board_change(change: ProjectRepositoryChange) -> bool:
    """Whether *change* is ordinary filesystem-backed board churn."""
    normalized = change.path.replace("\\", "/").removeprefix("./")
    unstaged = change.status == "??" or change.status.startswith(" ")
    return unstaged and normalized.startswith(PROJECT_BOARD_PREFIX)


def _merge_in_checkout(checkout: Path, branch: str, message: str) -> tuple[bool, str]:
    ticket_changes = _git(
        checkout,
        "diff",
        "--name-only",
        "-z",
        f"HEAD...{branch}",
        "--",
        "tickets/",
    )
    if ticket_changes.returncode != 0:
        return False, (ticket_changes.stderr or ticket_changes.stdout).strip()
    changed_tickets = [path for path in ticket_changes.stdout.split("\0") if path]
    if changed_tickets:
        return False, (
            f"project branch {branch!r} modifies Ticket Board state: "
            f"{', '.join(changed_tickets[:5])}"
        )
    try:
        blocking = blocking_project_repository_changes(checkout)
    except ProjectRepositoryStatusError as exc:
        return False, str(exc)
    if blocking:
        paths = ", ".join(change.path for change in blocking[:5])
        return False, f"project merge checkout at {checkout} has uncommitted changes: {paths}"
    result = _git(checkout, "merge", "--no-ff", branch, "-m", message)
    if result.returncode != 0:
        _git(checkout, "merge", "--abort")
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def _merge_in_temporary_worktree(
    repository: Path,
    base: str,
    branch: str,
    message: str,
) -> tuple[bool, str]:
    path = Path(tempfile.mkdtemp(prefix="booley_project_merge_"))
    add = _git(repository, "worktree", "add", str(path), base)
    if add.returncode != 0:
        safe_rmtree(path, protect_git_root=False)
        return False, add.stderr.strip()
    try:
        return _merge_in_checkout(path, branch, message)
    finally:
        _git(repository, "worktree", "remove", "--force", str(path))
        if path.exists():
            safe_rmtree(path, protect_git_root=False)


def _branch_upstream(repository: Path, branch: str) -> str:
    result = _git(repository, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _branch_exists(repository: Path, branch: str) -> bool:
    return (
        _git(repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    )


def _find_checkout(repository: Path, branch: str) -> Path | None:
    result = _git(repository, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return None
    current: Path | None = None
    wanted = f"branch refs/heads/{branch}"
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == wanted and current is not None:
            return current
    return None


def _git_or_raise(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = _git(cwd, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise TicketWorkspaceError(
            f"git {' '.join(args)} failed in {cwd} (rc={result.returncode}): {detail}"
        )
    return result


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
