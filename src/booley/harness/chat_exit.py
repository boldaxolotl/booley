"""What `booley chat` does when a worktree session ends, and at every launch (ADR 0082, "Exit").

When the agent of a worktree session exits on a terminal, `exit_step` lists the work
discarding the worktree would lose and asks whether to keep or discard it. Booley never
merges: a discard runs `rm`'s non-forced removal and asks a second time before forcing
one. With no terminal (a pipe, a redirect, a closed or hung-up terminal), or when the
agent did not exit by itself, the worktree is kept without a prompt and one line says
where it is.

`collect_at_launch` runs `gc` at the start of each launch and prints only what it removed.

This module is imported for every command, so POSIX-only names (`os.WIFEXITED`) are
referenced inside functions only.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from booley.config.worktree import gc_older_than
from booley.core.booley_dir import resolve_common_booley_dir
from booley.harness.worktree_gc import collect_garbage
from booley.harness.worktree_rm import (
    REMOVAL_FAILURES,
    Removal,
    WorktreeRemovalRefusedError,
    discard_losses,
    refusal_details,
    remove_worktree,
    report_removal,
)
from booley.runtime.foreground_child import ChildExit, foreground_tty
from booley.runtime.worktree_usage import record_worktree_use, stamp_worktree

_KEEP_ANSWERS = frozenset({"", "k", "keep"})
_DISCARD_ANSWERS = frozenset({"d", "discard"})
_YES_ANSWERS = frozenset({"y", "yes"})
# Unrecognized answers to the keep-or-discard question before the worktree is kept.
_MAX_ATTEMPTS = 3
# The name is at most one path component; this bounds the read of the pipe.
_NAME_MAX_BYTES = 256


class CreatedWorktree:
    """Tells the parent which worktree the forked child made.

    The parent makes the pipe before the fork. The child calls `record` once the worktree
    exists; the parent calls `name` after the child exited. Python's pipes are not
    inheritable, so the agent never holds either end.
    """

    def __init__(self) -> None:
        self._read, self._write = os.pipe()
        os.set_blocking(self._read, False)

    def record(self, name: str) -> None:
        """Child: say that worktree *name* was created."""
        os.write(self._write, name.encode())

    def name(self) -> str | None:
        """Parent: the recorded name, or ``None`` when the child never made a worktree."""
        try:
            data = os.read(self._read, _NAME_MAX_BYTES)
        except BlockingIOError:  # the parent holds the write end too: nothing was written
            return None
        return data.decode() or None

    def close(self) -> None:
        """Close both ends."""
        for end in (self._read, self._write):
            with contextlib.suppress(OSError):
                os.close(end)


def _write(text: str) -> None:
    """Write *text* to stderr; a closed, hung-up or absent stderr loses it silently.

    A real stderr is written through its descriptor: text that failed to reach a hung-up
    terminal would otherwise stay in Python's buffer, and the interpreter would then
    replace the agent's exit code with 120 when it flushes at exit.
    """
    stream = sys.stderr
    if stream is None:  # `booley chat 2>&-`: print(file=None) would use stdout
        return
    with contextlib.suppress(OSError, ValueError):
        stream.flush()
        try:
            descriptor = stream.fileno()
        except (OSError, ValueError):  # not a real file (a capture, an embedding host)
            stream.write(text)
            stream.flush()
            return
        os.write(descriptor, text.encode(stream.encoding or "utf-8", "backslashreplace"))


def _say(text: str) -> None:
    """Print one line to stderr."""
    _write(text + "\n")


def _ask(question: str) -> str:
    """Ask *question* on stderr and return the lowercased answer; EOF raises ``EOFError``."""
    _write(question)
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.strip().lower()


def _may_prompt(end: ChildExit) -> bool:
    """Whether to ask: the agent ended by itself and a terminal can show and answer the question.

    A hung-up terminal fails the terminal test too, which also covers `nohup`, where
    SIGHUP is ignored and so never forwarded.
    """
    return not end.forwarded and os.WIFEXITED(end.status) and foreground_tty() and os.isatty(2)


class _EndedSession:
    """The worktree of a session whose agent has exited, and what to ask about it."""

    def __init__(self, project_root: Path, name: str, path: Path) -> None:
        self.project_root = project_root
        self.name = name
        self.path = path

    def keep(self) -> None:
        _say(f"Kept worktree {self.name}: {self.path}")

    def offer(self) -> None:
        """List what discard would lose, ask, and act on the answer."""
        try:
            losses = discard_losses(self.project_root, self.name)
        except REMOVAL_FAILURES as exc:
            _say(f"Cannot tell what discard would lose: {exc}")
            self.keep()
            return
        _say(self._summary(losses))
        if self._wants_discard():
            self._discard()
        else:
            self.keep()

    def _summary(self, losses: list[str]) -> str:
        lines = [f"Session {self.name.upper()} has ended. Worktree {self.name}: {self.path}"]
        if losses:
            lines.append("Work not in main yet:")
            lines += [f"  - {loss}" for loss in losses]
            lines.append(
                "Discard deletes the uncommitted changes and keeps the branches. "
                "Booley never merges: merge what you need first."
            )
        else:
            lines.append("Nothing would be lost: no uncommitted changes, no commits outside main.")
        return "\n".join(lines)

    def _wants_discard(self) -> bool:
        """Ask keep or discard; keep is the default and the answer after repeated nonsense."""
        for _ in range(_MAX_ATTEMPTS):
            answer = _ask(f"Keep or discard worktree {self.name}? [K/d] ")
            if answer in _KEEP_ANSWERS:
                return False
            if answer in _DISCARD_ANSWERS:
                return True
            _say("Answer k to keep the worktree or d to discard it.")
        return False

    def _remove(self, *, force: bool) -> Removal | WorktreeRemovalRefusedError | None:
        """Run `rm`; ``None`` after saying why the worktree is still there (or half removed)."""
        try:
            return remove_worktree(self.project_root, self.name, force=force)
        except WorktreeRemovalRefusedError as refusal:
            return refusal
        except KeyboardInterrupt:
            # `rm` records what it began, so running it again finishes the removal.
            # `rm` records what it began; a forced removal needs `--force` again to finish.
            forced = " --force" if force else ""
            _say(f"Removal interrupted; run booley worktree rm {self.name}{forced} to finish it.")
        except REMOVAL_FAILURES as exc:
            _say(f"ERROR: {exc}")
            self.keep()
        return None

    def _discard(self) -> None:
        """Remove without force; when `rm` refuses, show why and ask again before forcing."""
        outcome = self._remove(force=False)
        if isinstance(outcome, WorktreeRemovalRefusedError):
            _say(
                "\n".join(
                    [f"Worktree {self.name} cannot be removed safely:", *refusal_details(outcome)]
                )
            )
            if _ask("Remove it anyway and lose this work? [y/N] ") not in _YES_ANSWERS:
                self.keep()
                return
            outcome = self._remove(force=True)
        if isinstance(outcome, Removal):
            _say(report_removal(self.name, outcome))


def exit_step(
    project_root: Path, created: CreatedWorktree, release_lock: Callable[[], None], end: ChildExit
) -> None:
    """Offer to discard the worktree of the session that just ended, or keep it quietly.

    Runs in Booley after the agent was reaped and never changes Booley's exit code. Does
    nothing when the child made no worktree (setup failed or was interrupted).
    """
    name = created.name()
    if name is None:
        return
    # The session is over: an `always` launch must not keep main locked while the prompt
    # waits, and the end is a use, so another launch's `gc` leaves the worktree alone.
    release_lock()
    common = resolve_common_booley_dir(project_root)
    with contextlib.suppress(OSError, ValueError):
        stamp_worktree(common, name)
    session = _EndedSession(project_root, name, common / "worktrees" / name)
    if not _may_prompt(end):
        session.keep()
        return
    try:
        session.offer()
    except (KeyboardInterrupt, EOFError):
        session.keep()


def stamp_in_place_worktree(project_root: Path) -> None:
    """Count a launch from inside a Booley worktree as a use of it.

    The agent that starts there gets no worktree of its own and may make no Booley tool
    call for a while; without the stamp another launch's `gc` could remove the worktree
    under it once it sat idle past `gc_older_than`.
    """
    with contextlib.suppress(*REMOVAL_FAILURES):
        record_worktree_use(Path.cwd(), resolve_common_booley_dir(project_root))


def collect_at_launch(project_root: Path) -> None:
    """Run `gc` as a launch begins and print only what it removed.

    Kept worktrees, failures, an invalid `gc_older_than` and a listing error stay silent:
    `booley worktree gc` shows why something was kept, and Doctor fails a bad age.
    """
    try:
        age = gc_older_than(resolve_common_booley_dir(project_root))
        outcomes = collect_garbage(project_root, age, now=time.time())
    except REMOVAL_FAILURES:
        return
    for outcome in outcomes:
        if outcome.status == "removed":
            _say(outcome.text)
