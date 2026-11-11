"""Where `booley chat` starts its agent: the main checkout or a worktree of its own (ADR 0082).

The main checkout is single-occupancy. A `booley chat` launch takes the main-checkout lock for
as long as its agent lives; a second launch finds it held and works in worktree `s<N>`.
Clients that take no lock (a plain `claude` tab, a VS Code panel) are still noticed through the
Session Registry, best effort.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from booley.core.file_lock import LockContentionError, acquire_file_lock, release_file_lock
from booley.runtime.safe_storage import refuse_symlinks

LOCK_RELATIVE_PATH = Path("runtime") / "chat-main.lock"


@dataclass
class MainLock:
    """The main-checkout lock of one launch: whether this process holds it."""

    held: bool
    _handle: IO[str] | None = None

    def release(self) -> None:
        """Give the lock up early; closing the file at the end of the launch is the backstop."""
        if self.held and self._handle is not None:
            release_file_lock(self._handle)
        self.held = False


@dataclass(frozen=True)
class Placement:
    """Where the agent starts; `reason` explains a worktree in the banner."""

    worktree: bool
    reason: str | None = None


@contextmanager
def main_checkout_lock(common: Path) -> Iterator[MainLock]:
    """Try the main-checkout lock without waiting and hold it for the context.

    Contention means another launch holds it. Storage that cannot be opened or locked is
    reported as a NOTE and treated as not held, so `--here` still starts and `auto` errs
    toward a worktree. The kernel drops the lock when the holder dies, and the descriptor
    is close-on-exec, so the agent never inherits it.
    """
    path = common / LOCK_RELATIVE_PATH
    try:
        refuse_symlinks(path, booley_dir=common)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8", newline="")
    except OSError as exc:
        print(f"NOTE: main-checkout lock unavailable: {exc}", file=sys.stderr)
        yield MainLock(held=False)
        return
    with handle:
        lock = MainLock(held=False, _handle=handle)
        try:
            acquire_file_lock(handle)
            lock.held = True
        except LockContentionError:
            pass
        except OSError as exc:
            print(f"NOTE: main-checkout lock unavailable: {exc}", file=sys.stderr)
        try:
            yield lock
        finally:
            lock.release()


def launch_checkout(cwd: Path, main_root: Path) -> str:
    """Classify the checkout *cwd* is in: ``main`` (the Project's), or ``other``.

    ``other`` covers a linked worktree, a different repository and no Git at all: the
    agent starts there in place. Project discovery maps a worktree back to the main
    checkout, so only the directory the user launched from tells them apart.
    """
    from booley.mcp.session_registry import git_checkout_metadata

    try:
        found = git_checkout_metadata(cwd.resolve())
    except (OSError, ValueError):
        return "other"
    if found is None:
        return "other"
    root, git_dir, common_dir = found
    return "main" if git_dir == common_dir and root.resolve() == main_root.resolve() else "other"


def main_checkout_session(common: Path, main_root: Path) -> str | None:
    """Return the handle of a live Session working in the main checkout, else ``None``.

    Uses the retained rows `worktree rm` and the Dashboard use. An unreadable registry
    reads as no Session: the lock is the authority, this check only widens it.
    """
    from booley.config.goals import quiet_after
    from booley.mcp.session_registry import SessionRegistry, namespace
    from booley.runtime.handles import HandleStore

    try:
        registry = SessionRegistry(common, quiet_after=quiet_after(common))
        snapshot = registry.visible_snapshot(now=time.time(), scope=namespace())
    except (OSError, ValueError):
        return None
    labels, _ = HandleStore(common).snapshot()
    for row in snapshot.rows:
        work_dir = row.attribution.work_dir or row.attribution.default_work_dir
        if work_dir and Path(work_dir).resolve() == main_root.resolve():
            return labels.get(row.attribution.key, row.attribution.key)
    return None


def choose_placement(
    mode: str, *, lock_held: bool, session: str | None, flagged: bool = False
) -> Placement:
    """Apply the `[chat] worktree` rules: `never` main, `always` worktree, `auto` by use.

    *flagged* marks an `always` that came from `--new-worktree` rather than the knob.
    """
    if mode == "never":
        return Placement(worktree=False)
    if mode == "always":
        return Placement(True, "--new-worktree" if flagged else '[chat] worktree = "always"')
    if not lock_held:
        return Placement(True, "the main checkout is in use by another booley chat")
    if session is not None:
        return Placement(True, f"Session {session} is working in the main checkout")
    return Placement(worktree=False)
