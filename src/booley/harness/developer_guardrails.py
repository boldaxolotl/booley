"""Developer Agent guardrails -- post-turn safety checks.

Provides:
  - Code write detection: uncommitted changes not from a Specialist
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.git import git_run

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirtyFile:
    """One ``git status --porcelain`` entry."""

    path: str
    status: str


class GitStatusError(RuntimeError):
    """Raised when guardrails cannot inspect worktree dirtiness."""


def _parse_porcelain_z(stdout: str) -> list[DirtyFile]:
    """Parse ``git status --porcelain -z`` into dirty entries.

    Each record is ``XY <path>`` terminated by NUL.  Renames and copies append
    the *original* path as a second NUL-terminated field, which must be consumed
    or it would be misread as the next record's status code.
    """
    fields = [f for f in stdout.split("\0") if f]
    files: list[DirtyFile] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        if "R" in status or "C" in status:
            index += 1  # skip the origin path that follows a rename/copy
        if path:
            files.append(DirtyFile(path=path, status=status))
    return files


def check_uncommitted_code_statuses(worktree: Path) -> list[DirtyFile]:
    """Return uncommitted/untracked paths with porcelain status codes.

    Parsed from ``-z`` (NUL-delimited) output, never the default line format.
    Plain porcelain C-quotes any path holding a space or a non-ASCII byte
    (``?? "docs spec.pdf"``), and those quotes used to be harmless only because
    a quoted path could never match a Scope entry and was therefore discarded.
    Now that out-of-Scope paths are committed rather than dropped, a quoted
    string would reach ``git add`` as a literal pathspec and fail the whole
    commit -- taking the in-Scope work with it.  ``-z`` emits raw bytes.
    """
    try:
        # Expand untracked directories to individual files.  The default
        # porcelain output may collapse a new tree to ``?? rtl/``, which cannot
        # be matched against file-level ticket scopes such as ``rtl/dut.sv``.
        result = git_run(
            worktree,
            ["status", "--porcelain", "-z", "--untracked-files=all", "--ignore-submodules"],
            timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise GitStatusError(
                f"git status failed in {worktree} (rc={result.returncode}): {detail}"
            )
        return _parse_porcelain_z(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise GitStatusError(f"git status timed out in {worktree}") from exc
    except (FileNotFoundError, OSError) as exc:
        raise GitStatusError(f"git status could not run in {worktree}: {exc}") from exc
