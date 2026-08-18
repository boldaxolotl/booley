"""Cross-process EDA-tool mutex for resource-constrained execution.

Prevents multiple pipeline instances from running RAM-hungry EDA tools
(Vivado, Yosys) simultaneously on the same machine.

Lock files live in the project dir's tickets/locks/<name>.lock. The OS releases locks
automatically on process exit, so stale lock files don't cause deadlocks.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

# Reuse platform-specific locking from ticket_board
from booley.ticket_board.helpers import lock_fd, unlock_fd


def _find_locks_dir() -> Path:
    """Locate a writable locks directory for cross-process EDA-tool mutexes.

    Uses the same tickets-dir convention as ticket_board.helpers but
    deliberately skips BOOLEY_PROJECT_DIR, which may point to a read-only
    mount inside Docker (/booley-project is root-owned).

    Detection order:
    1. TICKETS_DIR env var (test isolation)
    2. PROJECT_ROOT env var → tickets/locks under that root
    3. Walk up from CWD to find .git → tickets/locks under that root
    Fallback: system temp dir when the computed path isn't writable.
    """
    if "TICKETS_DIR" in os.environ:
        locks_dir = Path(os.environ["TICKETS_DIR"]) / "locks"
    else:
        if "PROJECT_ROOT" in os.environ:
            root = Path(os.environ["PROJECT_ROOT"])
        else:
            root = Path.cwd().resolve()
            while root != root.parent:
                if (root / ".git").exists() and root.name != ".booley":
                    break
                root = root.parent

        sibling = root / ".booley_project"
        if sibling.is_dir():
            locks_dir = sibling / "tickets" / "locks"
        else:
            locks_dir = root / ".booley" / "project" / "tickets" / "locks"

    try:
        locks_dir.mkdir(parents=True, exist_ok=True)
        return locks_dir
    except PermissionError:
        import tempfile

        fallback = Path(tempfile.gettempdir()) / "booley_locks"
        fallback.mkdir(parents=True, exist_ok=True)
        print(f"[lock] {locks_dir} not writable, using {fallback}", file=sys.stderr)
        return fallback


def eda_tool_lock_env_var() -> str:
    """Derive lock env var from project config to stay project-agnostic (§6).

    Public so that callers (run_sim_batch, run_yosys_syn) can set the same
    env var when propagating lock state to child processes.
    """
    try:
        from booley.project_config import ENV_PREFIX

        return f"_{ENV_PREFIX}_EDA_TOOL_LOCK_HELD"
    except (ImportError, AttributeError):
        return "_BOOLEY_EDA_TOOL_LOCK_HELD"


def _acquire_lock_fd(lock_file, name: str, timeout_s: int) -> None:
    """Spin-wait to acquire flock, raising TimeoutError on deadline."""
    deadline = time.monotonic() + timeout_s
    waited = False
    while True:
        try:
            lock_fd(lock_file)
            # Write PID for diagnostics
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()))
            lock_file.flush()
            break
        except (BlockingIOError, OSError) as err:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Could not acquire {name} lock within {timeout_s}s. "
                    f"Another process may be stuck."
                ) from err
            if not waited:
                print(
                    f"[lock] Waiting for {name} lock (held by another process)...", file=sys.stderr
                )
                waited = True
            time.sleep(1.0)
    if waited:
        print(f"[lock] Acquired {name} lock.", file=sys.stderr)


@contextlib.contextmanager
def eda_tool_lock(name: str, timeout_s: int = 1800) -> contextlib.AbstractContextManager[None]:
    """Acquire a named cross-process lock. Blocks until available.

    Args:
        name: Lock name (e.g. "vivado", "yosys"). Maps to lock file.
        timeout_s: Max seconds to wait for lock (default: 30 min).

    Raises:
        TimeoutError: If lock cannot be acquired within timeout_s.

    Nested calls: If the project's EDA_TOOL_LOCK_HELD env var contains this lock
    name, the lock is skipped (lets an EDA tool that already holds the lock spawn
    a child runner for the same EDA tool without self-deadlocking).
    """
    env_var = eda_tool_lock_env_var()
    held = os.environ.get(env_var, "")
    if name in held.split(","):
        yield
        return

    locks_dir = _find_locks_dir()
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / f"{name}.lock"

    acquired = False
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            _acquire_lock_fd(lock_file, name, timeout_s)
            acquired = True
            yield
        finally:
            if acquired:
                with contextlib.suppress(OSError):
                    unlock_fd(lock_file)
