"""Repository discovery, path coordinates and bounded Git inspection for project checkouts."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.project_dir import PROJECT_DIR_NAME, resolve_checkout_project_dir


@dataclass(frozen=True)
class RepositoryCheckout:
    """One repository and its prefix within a composite checkout."""

    worktree: Path
    path_prefix: str = ""

    def local_path(self, checkout_path: str) -> str:
        """Translate a composite-root-relative path into this repository."""
        if not self.path_prefix:
            return checkout_path
        prefix = f"{self.path_prefix}/"
        if not checkout_path.startswith(prefix):
            raise ValueError(f"path {checkout_path!r} is outside {self.path_prefix!r}")
        return checkout_path.removeprefix(prefix)

    def prefixed_path(self, local_path: str) -> str:
        """Translate a repository-relative path into the composite checkout."""
        return f"{self.path_prefix}/{local_path}" if self.path_prefix else local_path


@dataclass(frozen=True)
class ProjectRepositoryChange:
    """One uncommitted repository-relative path."""

    path: str
    status: str


class RepositoryCheckoutError(RuntimeError):
    """A repository checkout could not preserve its identity."""


@dataclass(frozen=True)
class SymbolicBranchInspection:
    """The attached branch, or Git's explanation when it cannot be read."""

    branch: str | None
    result: subprocess.CompletedProcess[str]

    @property
    def detail(self) -> str:
        """Return Git's bounded failure output, if any."""
        return (self.result.stderr or self.result.stdout).strip()


def is_git_worktree_root(path: Path) -> bool:
    """Return whether ``path`` is a healthy Git worktree rooted at itself."""
    if not (path / ".git").exists():
        return False
    result = run_git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or not result.stdout.strip():
        return False
    try:
        return Path(result.stdout.strip()).resolve() == path.resolve()
    except OSError:
        return False


def is_standalone_git_repository(path: Path) -> bool:
    """Return whether ``path`` owns its healthy Git metadata directory."""
    return (path / ".git").is_dir() and is_git_worktree_root(path)


@dataclass(frozen=True)
class ProjectTopology:
    """Independent checkout-local linked and configured standalone selections."""

    paired: RepositoryCheckout | None = None
    standalone: Path | None = None


def project_topology(
    checkout_root: Path, *, discover_paired: bool = True, discover_standalone: bool = True
) -> ProjectTopology:
    """Inspect requested Project repository selections without changing their policy."""
    paired = _inspect_paired_project_repository(checkout_root) if discover_paired else None
    standalone = None
    if discover_standalone:
        try:
            project_dir = resolve_checkout_project_dir(checkout_root).resolve()
        except FileNotFoundError:
            pass
        else:
            if is_standalone_git_repository(project_dir):
                standalone = project_dir
    return ProjectTopology(paired, standalone)


def resolve_inner_project_repo(project_root: Path) -> Path | None:
    """Return this checkout's configured standalone Project repository."""
    return project_topology(project_root, discover_paired=False).standalone


def paired_project_repository(checkout_root: Path) -> RepositoryCheckout | None:
    """Return the fixed checkout-local linked Project, without resolving config."""
    return project_topology(checkout_root, discover_standalone=False).paired


def _inspect_paired_project_repository(checkout_root: Path) -> RepositoryCheckout | None:
    """Return the checkout's linked inner worktree, when one is installed."""
    nested = checkout_root / PROJECT_DIR_NAME
    if not (nested / ".git").is_file():
        return None
    result = run_git(nested, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RepositoryCheckoutError(
            f"paired project repository is unavailable at {nested}: {detail}"
        )
    try:
        top = Path(result.stdout.strip()).resolve()
        expected = nested.resolve()
    except OSError as exc:
        raise RepositoryCheckoutError(
            f"paired project repository cannot be resolved at {nested}: {exc}"
        ) from exc
    if top != expected:
        raise RepositoryCheckoutError(
            f"paired project repository has unexpected root {top}; expected {expected}"
        )
    return RepositoryCheckout(nested, PROJECT_DIR_NAME)


def common_git_dir(worktree: Path) -> Path | None:
    result = run_git(worktree, "rev-parse", "--git-common-dir")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = worktree / path
    try:
        return path.resolve()
    except OSError:
        return None


def ref_sha(source: Path, ref: str) -> str:
    result = run_git(source, "rev-parse", "--verify", ref)
    return result.stdout.strip() if result.returncode == 0 else ""


def inspect_symbolic_branch(worktree: Path) -> SymbolicBranchInspection:
    """Inspect the worktree's attached branch with bounded Git execution."""
    result = run_git(worktree, "symbolic-ref", "--quiet", "HEAD")
    ref = result.stdout.strip()
    if result.returncode == 0 and ref.startswith("refs/heads/"):
        return SymbolicBranchInspection(ref.removeprefix("refs/heads/"), result)
    return SymbolicBranchInspection(None, result)


def parse_porcelain_z(stdout: str) -> tuple[ProjectRepositoryChange, ...]:
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


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
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
