"""Remove recognized legacy Sandbox identities from the mounted Project checkout."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from booley.runtime.incontainer_git_identity import (
    GitIdentity,
    GitIdentityError,
    _worktree_config_path,
    load_git_identity,
)


def _config(checkout: Path, target: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Read direct file contents; neither command-scope settings nor includes
    # may turn a human file into a recognized agent identity.
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")
    }
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), "config", "--file", str(target), "--no-includes", *args],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitIdentityError(f"cannot inspect legacy worktree identity: {exc}") from exc


def _values(checkout: Path, target: Path, key: str) -> list[str]:
    result = _config(checkout, target, "--get-all", key)
    if result.returncode not in (0, 1):
        raise GitIdentityError(f"cannot read legacy worktree config: {result.stderr.strip()}")
    return result.stdout.splitlines() if result.returncode == 0 else []


def _remove_pair(checkout: Path, staged: Path) -> None:
    for key in ("user.name", "user.email"):
        result = _config(checkout, staged, "--unset-all", key)
        if result.returncode != 0:
            raise GitIdentityError(
                f"cannot stage legacy identity cleanup: {result.stderr.strip()}"
            )


def cleanup_git_identity(checkout: Path, identity: GitIdentity) -> None:
    """Atomically remove one complete recognized pair, preserving other Git settings."""
    target = _worktree_config_path(checkout)
    lock = target.with_name(target.name + ".lock")
    if not target.exists() and not target.is_symlink():
        return
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise GitIdentityError(f"cannot acquire Git config lock {lock}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            mode = target.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise GitIdentityError(f"legacy worktree config is not a regular file: {target}")
            names = _values(checkout, target, "user.name")
            emails = _values(checkout, target, "user.email")
            recognized = {(identity.name, identity.email), ("Dev", "dev@localhost")}
            if len(names) != 1 or len(emails) != 1 or (names[0], emails[0]) not in recognized:
                return
            with target.open("rb") as source:
                shutil.copyfileobj(source, stream)
            os.fchmod(stream.fileno(), stat.S_IMODE(mode))
        _remove_pair(checkout, lock)
        lock.replace(target)
    except OSError as exc:
        raise GitIdentityError(f"cannot clean legacy worktree identity: {exc}") from exc
    finally:
        lock.unlink(missing_ok=True)


def main() -> None:
    """Clean the designated Interactive Project checkout, never a Ticket apply path."""
    project_dir = Path(os.environ.get("BOOLEY_PROJECT_DIR", "/booley-project"))
    checkout = Path(os.environ.get("BOOLEY_GIT_CHECKOUT", Path.cwd()))
    cleanup_git_identity(checkout, load_git_identity(project_dir))


if __name__ == "__main__":
    main()
