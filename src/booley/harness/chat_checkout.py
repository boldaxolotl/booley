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
from typing import IO, TYPE_CHECKING

from booley.core.file_lock import LockContentionError, acquire_file_lock, release_file_lock
from booley.runtime.safe_storage import refuse_symlinks

if TYPE_CHECKING:  # chat_resume imports this module
    from booley.harness.chat_resume import ResumeTarget

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
    """Where the agent starts; `reason` explains a worktree in the banner.

    A *resume* places a resumed conversation (ADR 0082, Resume): in the worktree it ran in,
    or, with no `cwd`, in the main checkout with its own arguments.
    """

    worktree: bool
    reason: str | None = None
    resume: ResumeTarget | None = None


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


def _live_sessions(common: Path) -> list[tuple[Path, str]]:
    """The work directory and handle of each live Session in the retained registry rows.

    An unreadable registry reads as no Session: the lock is the authority, this only widens it.
    """
    from booley.config.goals import quiet_after
    from booley.mcp.session_registry import SessionRegistry, namespace
    from booley.runtime.handles import HandleStore

    try:
        registry = SessionRegistry(common, quiet_after=quiet_after(common))
        snapshot = registry.visible_snapshot(now=time.time(), scope=namespace())
    except (OSError, ValueError):
        return []
    labels, _ = HandleStore(common).snapshot()
    found: list[tuple[Path, str]] = []
    for row in snapshot.rows:
        work_dir = row.attribution.work_dir or row.attribution.default_work_dir
        if work_dir:
            found.append(
                (Path(work_dir).resolve(), labels.get(row.attribution.key, row.attribution.key))
            )
    return found


def main_checkout_session(common: Path, main_root: Path) -> str | None:
    """Return the handle of a live Session working in the main checkout, else ``None``.

    Uses the retained rows `worktree rm` and the Dashboard use.
    """
    for work_dir, label in _live_sessions(common):
        if work_dir == main_root.resolve():
            return label
    return None


def worktree_sessions(common: Path) -> dict[str, str]:
    """Map each worktree name to the handle of a live Session working inside it.

    Sees agents the conversation records miss: a Codex started by hand has no process key.
    """
    root = (common / "worktrees").resolve()
    found: dict[str, str] = {}
    for work_dir, label in _live_sessions(common):
        if work_dir.is_relative_to(root) and work_dir != root:
            found.setdefault(work_dir.relative_to(root).parts[0], label)
    return found


def choose_placement(
    mode: str, *, lock_held: bool, session: str | None, flagged: bool = False
) -> Placement:
    """Apply the `[worktree] chat` rules: `never` main, `always` worktree, `auto` by use.

    *flagged* marks an `always` that came from `--new-worktree` rather than the knob.
    """
    if mode == "never":
        return Placement(worktree=False)
    if mode == "always":
        return Placement(True, "--new-worktree" if flagged else '[worktree] chat = "always"')
    if not lock_held:
        return Placement(True, "the main checkout is in use by another booley chat")
    if session is not None:
        return Placement(True, f"Session {session} is working in the main checkout")
    return Placement(worktree=False)
