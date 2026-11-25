"""Raw-bytes Git for an explicit repository: the runner, commit-OID validation and literal tree rows."""

from __future__ import annotations

import re
import signal
import subprocess
from pathlib import Path

from booley.runtime import platform_paths
from booley.runtime.git_environment import inherited_git_environment
from booley.runtime.git_facts import is_rememberable, run_remembered


class FileCommitError(RuntimeError):
    """Git could not run or answer, or a commit OID is unusable."""


class _GitExitError(FileCommitError):
    """A Git child that exited nonzero; its message is its stderr, or how it ended."""

    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        command = "git " + " ".join(args)
        if stderr:
            message = stderr
        elif returncode < 0:
            try:
                name = signal.Signals(-returncode).name
            except ValueError:  # a real-time signal has no name
                name = f"signal {-returncode}"
            message = f"{command} was killed by {name}"
        else:
            message = f"{command} failed with exit status {returncode} and no error output"
        super().__init__(message)


def raw_git(
    repository: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    """Run explicit-repository Git with raw bytes and caller-owned environment overrides."""
    environment = {
        **inherited_git_environment(),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
    }
    environment.update(env or {})

    def run() -> subprocess.CompletedProcess[bytes]:
        return platform_paths.run_captured_bounded(
            ["git", *args],
            cwd=repository,
            env=environment,
            input=input_bytes,
            timeout=60,
            text=False,
        )

    try:
        if input_bytes is None and is_rememberable(args):
            result = run_remembered(["git", *args], repository, environment, text=False, run=run)
        else:
            result = run()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FileCommitError(f"Git publication command failed: {exc}") from exc
    if result.returncode:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise _GitExitError(args, result.returncode, stderr)
    return result.stdout


def validate_pin(repository: Path, pin: str) -> None:
    """A full canonical existing raw commit OID, before any write."""
    algorithm = raw_git(repository, "rev-parse", "--show-object-format").strip()
    length = 64 if algorithm == b"sha256" else 40
    if re.fullmatch(rf"[0-9a-f]{{{length}}}", pin) is None:
        raise FileCommitError("publication pin must be a full canonical commit OID")
    if raw_git(repository, "cat-file", "-t", pin).strip() != b"commit":
        raise FileCommitError("publication pin must name an existing commit")


def tree_rows(repository: Path, commit: str) -> dict[bytes, bytes]:
    """Literal recursive tree rows, preserving every path byte."""
    result: dict[bytes, bytes] = {}
    for row in raw_git(repository, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if row:
            metadata, name = row.split(b"\t", 1)
            result[name] = metadata
    return result
