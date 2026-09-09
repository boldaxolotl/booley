"""Apply a Project's Git identity to its Interactive Mode checkout."""

from __future__ import annotations

import os
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_NAME = "Dev"
_DEFAULT_EMAIL = "dev@localhost"
_GIT_TIMEOUT_S = 10


class GitIdentityError(RuntimeError):
    """The configured Git identity could not be safely applied."""


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """The default author and committer identity for one Project checkout."""

    name: str
    email: str


def _table(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GitIdentityError(f"{field} must be a table")
    return value


def _identity_field(git: Mapping[str, Any], field: str, default: str) -> str:
    value = git.get(field)
    if value is None:
        return default
    if not isinstance(value, str):
        raise GitIdentityError(f"[agent.git] {field} must be a string")
    value = value.strip()
    if not value:
        return default
    if any(character in value for character in ("\0", "\r", "\n")):
        raise GitIdentityError(f"[agent.git] {field} contains a forbidden control character")
    return value


def load_git_identity(project_dir: Path) -> GitIdentity:
    """Load ``[agent.git]`` from the Project's ``booley.toml``."""
    path = project_dir / "booley.toml"
    if not path.is_file():
        return GitIdentity(_DEFAULT_NAME, _DEFAULT_EMAIL)
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GitIdentityError(f"cannot read Git identity from {path}: {exc}") from exc
    agent = _table(data.get("agent", {}), "[agent]")
    git = _table(agent.get("git", {}), "[agent.git]")
    return GitIdentity(
        _identity_field(git, "name", _DEFAULT_NAME),
        _identity_field(git, "email", _DEFAULT_EMAIL),
    )


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or (exc.stdout or "").strip() or str(exc)
        raise GitIdentityError(f"Git identity setup could not run: {detail}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitIdentityError(f"Git identity setup could not run: {exc}") from exc


def _require_git(checkout: Path, *args: str) -> str:
    result = _git(checkout, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise GitIdentityError(f"Git identity setup failed: {detail}")
    return result.stdout.strip()


def _worktree_value(checkout: Path, key: str) -> str | None:
    result = _git(checkout, "config", "--worktree", "--get", key)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise GitIdentityError(f"cannot inspect worktree Git identity: {detail}")
    return result.stdout.rstrip("\n")


def _restore_worktree_value(checkout: Path, key: str, value: str | None) -> None:
    if value is None:
        result = _git(checkout, "config", "--worktree", "--unset-all", key)
        if result.returncode not in (0, 5):
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise GitIdentityError(f"cannot restore worktree Git identity: {detail}")
        return
    _require_git(checkout, "config", "--worktree", "--replace-all", key, value)


def apply_git_identity(checkout: Path, identity: GitIdentity) -> None:
    """Set *identity* in the checkout's worktree-specific Git configuration."""
    name = _identity_field({"name": identity.name}, "name", _DEFAULT_NAME)
    email = _identity_field({"email": identity.email}, "email", _DEFAULT_EMAIL)
    _require_git(checkout, "config", "extensions.worktreeConfig", "true")
    prior = {
        "user.name": _worktree_value(checkout, "user.name"),
        "user.email": _worktree_value(checkout, "user.email"),
    }
    try:
        _require_git(checkout, "config", "--worktree", "--replace-all", "user.name", name)
        _require_git(checkout, "config", "--worktree", "--replace-all", "user.email", email)
        if _worktree_value(checkout, "user.name") != name:
            raise GitIdentityError("worktree user.name verification failed")
        if _worktree_value(checkout, "user.email") != email:
            raise GitIdentityError("worktree user.email verification failed")
    except GitIdentityError as exc:
        try:
            for key, value in prior.items():
                _restore_worktree_value(checkout, key, value)
        except GitIdentityError as restore_exc:
            raise GitIdentityError(f"{exc}; rollback also failed: {restore_exc}") from exc
        raise


def main() -> None:
    """Apply the mounted Project configuration to the attached checkout."""
    project_dir = Path(os.environ.get("BOOLEY_PROJECT_DIR", "/booley-project"))
    identity = load_git_identity(project_dir)
    apply_git_identity(Path.cwd(), identity)


if __name__ == "__main__":
    main()
