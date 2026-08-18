"""Garbage-collection of stale worktree locks.

Prunes lock entries under ``worktrees/.locks/`` whose owner process is dead
(or whose age exceeds a threshold when no PID is readable). Extracted from
``workspace`` so the workspace-preparation module keeps only worktree/branch
creation logic.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    """Check whether a process is still running (cross-platform)."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _prune_stale_worktree_locks(
    project_root: Path,
    max_age_s: float = 300,
) -> None:
    """Remove worktree locks whose owner process is dead.

    Runs proactively before worktree creation so that blocked tickets can
    recover without manual intervention. Handles both lock flavours used
    under ``worktrees/.locks/``:

      * ``*.mkdir.lock/`` directories with a ``pid`` file inside (created
        by the bash fallback path in ``worktree_create.sh``).
      * Plain ``*.lock`` files containing a PID (created by flock-based
        acquirers; the PID is stamped after acquisition).

    Detection strategy is uniform across both:
      1. PID file → instant liveness check (kills locks left by
         ``TerminateProcess``, which doesn't fire shell EXIT traps).
      2. Age-based fallback (mtime > max_age_s) when no readable PID.
    """
    locks_dir = project_root / ".booley_project" / "worktrees" / ".locks"
    if not locks_dir.is_dir():
        return

    now = time.time()
    for entry in locks_dir.iterdir():
        _prune_one_lock(entry, now, max_age_s)


def release_worktree_locks(project_root: Path, name: str) -> None:
    """Drop the lock entries a finished worktree left behind (F-54).

    ``worktree_create.sh`` acquires ``<name>.lock`` (flock) or
    ``<name>.mkdir.lock/`` (fallback) to serialize creation of one worktree
    name, but the flock flavour only releases the *advisory lock* on exit —
    the zero-byte file survives, so the next ticket's GC reaps it as stale.
    Once the worktree itself is gone, nothing can legitimately be waiting on
    that name, so teardown can clean up after itself.

    Never touches the shared ``_parent_git`` locks: those serialize parent-repo
    mutations across *all* worktrees and may be held by another live ticket.
    """
    locks_dir = project_root / ".booley_project" / "worktrees" / ".locks"
    if not locks_dir.is_dir() or not name or name.startswith("_parent_git"):
        return

    lock_file = locks_dir / f"{name}.lock"
    if lock_file.is_file():
        logger.info("Releasing worktree lock file %s at teardown", lock_file.name)
        with contextlib.suppress(OSError):
            lock_file.unlink()

    lock_dir = locks_dir / f"{name}.mkdir.lock"
    if lock_dir.is_dir():
        logger.info("Releasing worktree lock %s at teardown", lock_dir.name)
        shutil.rmtree(lock_dir, ignore_errors=True)


def _prune_one_lock(entry: Path, now: float, max_age_s: float) -> None:
    """Inspect a single lock entry (dir or file) and remove it if stale."""
    if entry.is_dir() and entry.name.endswith(".mkdir.lock"):
        pid_file = entry / "pid"
        pid = _read_pid_file(pid_file) if pid_file.exists() else None
        if pid is not None:
            if _pid_alive(pid):
                return
            logger.info(
                "Removing worktree lock %s (owner PID %d is dead)",
                entry.name,
                pid,
            )
            shutil.rmtree(entry, ignore_errors=True)
            return
        # No readable PID — age-based fallback.
        if _entry_age(entry, now) >= max_age_s:
            logger.info(
                "Removing worktree lock %s (no PID file, exceeded max age)",
                entry.name,
            )
            shutil.rmtree(entry, ignore_errors=True)
        return

    if entry.is_file() and entry.name.endswith(".lock"):
        pid = _read_pid_file(entry)
        if pid is not None:
            if _pid_alive(pid):
                return
            logger.info(
                "Removing worktree lock file %s (owner PID %d is dead)",
                entry.name,
                pid,
            )
            with contextlib.suppress(OSError):
                entry.unlink()
            return
        if _entry_age(entry, now) >= max_age_s:
            logger.info(
                "Removing worktree lock file %s (no PID, exceeded max age)",
                entry.name,
            )
            with contextlib.suppress(OSError):
                entry.unlink()


def _read_pid_file(path: Path) -> int | None:
    """Parse a PID file. Returns None on corrupt content or missing file."""
    try:
        text = path.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def _entry_age(entry: Path, now: float) -> float:
    """Best-effort mtime age in seconds; returns 0 on stat failure."""
    try:
        return now - entry.stat().st_mtime
    except OSError:
        return 0.0
