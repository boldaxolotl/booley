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


def resolve_inner_project_repo(project_root: Path) -> Path | None:
    """Return this checkout's project dir only when it is its own Git repo."""
    try:
        project_dir = resolve_checkout_project_dir(project_root).resolve()
    except FileNotFoundError:
        return None
    if not (project_dir / ".git").is_dir():
        return None
    result = run_git(project_dir, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    try:
        top = Path(result.stdout.strip()).resolve()
    except OSError:
        return None
    return project_dir if top == project_dir else None


def paired_project_repository(checkout_root: Path) -> RepositoryCheckout | None:
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
