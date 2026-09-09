"""Apply a Project's Git identity to its Interactive Mode checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, as_str, require_dict

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


def _table(value: Any, field: str) -> dict[str, Any]:
    try:
        return require_dict(value, field=field)
    except BoundaryError as exc:
        raise GitIdentityError(f"{field} must be a table") from exc


def _identity_field(git: Mapping[str, Any], field: str, default: str) -> str:
    value = git.get(field)
    if value is None:
        return default
    text = as_str(value)
    if text is None:
        raise GitIdentityError(f"[agent.git] {field} must be a string")
    text = text.strip()
    if not text:
        return default
    if any(character in text for character in ("\0", "\r", "\n")):
        raise GitIdentityError(f"[agent.git] {field} contains a forbidden control character")
    return text


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


def _worktree_config_path(checkout: Path) -> Path:
    raw_path = _require_git(checkout, "rev-parse", "--git-path", "config.worktree")
    path = Path(raw_path)
    return path if path.is_absolute() else checkout / path


def _stage_worktree_config(checkout: Path, target: Path, identity: GitIdentity) -> Path:
    try:
        descriptor, raw_staged = tempfile.mkstemp(
            prefix=f".{target.name}.booley-", dir=target.parent
        )
        os.close(descriptor)
        staged = Path(raw_staged)
        if target.exists():
            if not target.is_file():
                raise GitIdentityError(f"worktree Git config is not a file: {target}")
            shutil.copyfile(target, staged)
            shutil.copymode(target, staged)
        _require_git(
            checkout, "config", "--file", str(staged), "--replace-all", "user.name", identity.name
        )
        _require_git(
            checkout,
            "config",
            "--file",
            str(staged),
            "--replace-all",
            "user.email",
            identity.email,
        )
        return staged
    except (OSError, GitIdentityError) as exc:
        if "staged" in locals():
            staged.unlink(missing_ok=True)
        if isinstance(exc, GitIdentityError):
            raise
        raise GitIdentityError(f"cannot stage worktree Git identity: {exc}") from exc


def _worktree_config_enabled(checkout: Path) -> bool:
    result = _git(checkout, "config", "--local", "--get", "extensions.worktreeConfig")
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise GitIdentityError(f"cannot inspect worktree Git configuration: {detail}")
    return result.stdout.strip().lower() == "true"


def apply_git_identity(checkout: Path, identity: GitIdentity) -> None:
    """Set *identity* in the checkout's worktree-specific Git configuration."""
    normalized = GitIdentity(
        _identity_field({"name": identity.name}, "name", _DEFAULT_NAME),
        _identity_field({"email": identity.email}, "email", _DEFAULT_EMAIL),
    )
    target = _worktree_config_path(checkout)
    staged = _stage_worktree_config(checkout, target, normalized)
    try:
        enabled = _worktree_config_enabled(checkout)
        staged.replace(target)
    except OSError as exc:
        raise GitIdentityError(f"cannot publish worktree Git identity: {exc}") from exc
    finally:
        staged.unlink(missing_ok=True)
    if not enabled:
        _require_git(checkout, "config", "--local", "extensions.worktreeConfig", "true")


def main() -> None:
    """Apply the mounted Project configuration to the attached checkout."""
    project_dir = Path(os.environ.get("BOOLEY_PROJECT_DIR", "/booley-project"))
    identity = load_git_identity(project_dir)
    checkout = Path(os.environ.get("BOOLEY_GIT_CHECKOUT", Path.cwd()))
    apply_git_identity(checkout, identity)


if __name__ == "__main__":
    main()
