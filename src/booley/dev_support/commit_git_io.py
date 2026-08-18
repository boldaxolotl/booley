"""Git commit plumbing for code-modifying Specialists.

Extracted from ``specialist`` (principle 8): reading commit metadata and
reverting out-of-scope files via ``git`` subprocesses is one reason to
change (git plumbing), independent of agent invocation or commit-message
formatting. Consumed by ``specialist`` (commit / revert flow) and
``tb_coder`` (``_git_head_sha`` for before-SHA capture).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _save_files_to_logs(
    work_dir: Path,
    paths: list[str],
) -> list[dict[str, str]]:
    """Save copies of out-of-scope files to $BOOLEY_LOGS_DIR/reverted/."""
    import shutil as _shutil

    saved: list[dict[str, str]] = []
    logs_dir = Path(os.environ.get("BOOLEY_LOGS_DIR", ""))
    if not logs_dir or not logs_dir.is_dir():
        return saved
    dest_dir = logs_dir / "reverted"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for rel_path in paths:
        src = work_dir / rel_path
        if src.is_file():
            dest = dest_dir / rel_path.replace("/", "_")
            try:
                _shutil.copy2(str(src), str(dest))
                saved.append({"original_path": rel_path, "saved_path": str(dest)})
            except OSError:
                logger.warning("Failed to save %s before revert", rel_path)
    return saved


def _git_revert_files(work_dir: Path, paths: list[str]) -> None:
    """Revert tracked files via checkout and remove untracked via clean."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", *paths],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        tracked = set(result.stdout.splitlines()) if result.returncode == 0 else set()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        tracked = set()
    untracked = [p for p in paths if p not in tracked]
    tracked_list = [p for p in paths if p in tracked]

    try:
        if tracked_list:
            subprocess.run(
                ["git", "checkout", "--", *tracked_list],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        if untracked:
            subprocess.run(
                ["git", "clean", "-f", "--", *untracked],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        logger.info(
            "Reverted %d out-of-scope file(s) (%d tracked, %d untracked)",
            len(paths),
            len(tracked_list),
            len(untracked),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("Failed to revert out-of-scope files")


def _git_head_sha(work_dir: Path) -> str:
    """Return current HEAD SHA (full), or empty string on error."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _read_commit_info(work_dir: Path, before_sha: str) -> dict[str, Any]:
    """Read commit metadata for the diff ``before_sha..HEAD``."""
    sha_short, subject = _read_last_commit_log(work_dir)
    stat_summary, changed_files, file_stats = _read_numstat(work_dir, before_sha)
    return {
        "sha": sha_short,
        "subject": subject,
        "stat_summary": stat_summary,
        "changed_files": changed_files,
        "file_stats": file_stats,
    }


def _read_last_commit_log(work_dir: Path) -> tuple[str, str]:
    """Return (sha_short, subject) of the last commit."""
    try:
        log = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if log.returncode == 0:
            sha, _, subject = log.stdout.strip().partition(" ")
            return sha, subject
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "", ""


def _read_numstat(
    work_dir: Path,
    before_sha: str,
) -> tuple[str, list[str], dict[str, tuple[int, int]]]:
    """Parse ``git diff --numstat``. Returns (summary, files, file_stats)."""
    ref = before_sha or "HEAD~1"
    changed_files: list[str] = []
    file_stats: dict[str, tuple[int, int]] = {}
    try:
        numstat = subprocess.run(
            ["git", "diff", "--numstat", ref, "HEAD"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        total_add = total_del = 0
        for line in numstat.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                fname = parts[2].strip()
                if not fname:
                    continue
                changed_files.append(fname)
                try:
                    add, delete = int(parts[0]), int(parts[1])
                    file_stats[fname] = (add, delete)
                    total_add += add
                    total_del += delete
                except ValueError:
                    file_stats[fname] = (0, 0)
        n = len(changed_files)
        summary = f"{n} file{'s' if n != 1 else ''} changed, +{total_add} -{total_del}"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        summary = ""
    return summary, changed_files, file_stats
