"""Run a launch callable as a forked child that owns the terminal, as `sudo` does.

`booley chat` used to replace itself with the agent CLI. The agent now runs as a
child of Booley so the parent can later own locks and exit steps (ADR 0082,
"Launching the agent"). This module keeps the interactive behavior of the old
exec: Ctrl-C reaches only the agent, Ctrl-Z and `fg` work, closing the terminal
ends the agent, and the agent's exit status becomes Booley's.

Two modes, chosen once at entry:

- Terminal mode: stdin and stdout are the controlling terminal and Booley's
  group is its foreground group. The child leads its own process group and holds
  the terminal; the parent is never the foreground group, so tty-generated
  signals do not reach it and nothing doubles.
- Shared-group mode: otherwise (a pipe, a redirect, a background launch). The
  child stays in Booley's group, the kernel's group-wide job control stops and
  continues both, and the parent ignores only SIGINT and SIGQUIT.

The launch window runs from the signal setup in `_supervise` to the agent's exec.
A terminal signal in that window must act on the agent as it would just after the
exec, so neither process lets it be dropped or turned into a traceback: both hold
SIGINT and SIGQUIT (and SIGTSTP in terminal mode) blocked, which keeps them pending
instead of discarding them. The parent re-sends its pending copy to the child
and then releases the child through a pipe; the child unblocks just before exec in
`exec_agent`, so the signal acts on the agent's process exactly once.

This module must import on Windows (`booley.py` imports `chat_cmd` for every
command), so POSIX-only names are referenced inside functions only.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import traceback
from collections.abc import Callable, Set
from dataclasses import dataclass
from typing import Any

_RESTORED_NAMES = (
    "SIGINT",
    "SIGQUIT",
    "SIGTSTP",
    "SIGTTOU",
    "SIGTERM",
    "SIGHUP",
    "SIGCHLD",
)
_TTY_SIGNAL_NAMES = ("SIGINT", "SIGQUIT", "SIGTSTP")
_FORWARDED_NAMES = ("SIGTERM", "SIGHUP")
_EXEC_FAILED = 2

# Signals the forked child keeps blocked until `exec_agent`. Process state, like the
# signal mask it describes: empty everywhere except in a child set up by `_run_child`.
_held_until_exec: set[signal.Signals] = set()


def _signal_numbers(names: tuple[str, ...]) -> list[signal.Signals]:
    return [getattr(signal, name) for name in names]


@dataclass(frozen=True)
class ChildExit:
    """How the agent ended, as the exit hook of `run_foreground_child` sees it.

    `status` is the raw wait status. `forwarded` says the parent relayed SIGHUP or SIGTERM
    to the agent: the terminal was closed or Booley was asked to stop.
    """

    status: int
    forwarded: bool


def foreground_tty() -> bool:
    """Return whether stdin/stdout are a terminal on which Booley is the foreground group.

    A pipe on stdout (`booley chat | less`) or a tty that is not our controlling
    terminal (ENOTTY) must not move the terminal away from its real owner.
    """
    if not (os.isatty(0) and os.isatty(1)):
        return False
    try:
        return os.tcgetpgrp(0) == os.getpgrp()
    except OSError:
        return False


def _flush_std() -> None:
    """Flush stdout and stderr, tolerating closed (None) or broken streams.

    `booley chat 2>&-` leaves `sys.stderr` as None; a flush must never keep the
    child from `os._exit` or crash the parent before the fork.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.flush()


def _propagate(status: int) -> int:
    """Turn the child's wait status into Booley's own exit code or death by signal."""
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    number = os.WTERMSIG(status)
    import resource

    # No spurious Python core file after an agent SIGQUIT/SIGSEGV.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if number != signal.SIGKILL:  # SIGKILL's disposition cannot change
        signal.signal(number, signal.SIG_DFL)
    os.kill(os.getpid(), number)
    return 128 + number


class _ForegroundRun:
    """One supervised launch: fork, hand over the terminal, wait, report."""

    def __init__(
        self, launch: Callable[[], int], after_exit: Callable[[ChildExit], None] | None = None
    ) -> None:
        self._launch = launch
        self._after_exit = after_exit
        # Set by `_forward`: a hang-up or termination request reached the agent.
        self._forwarded = False
        self._terminal = foreground_tty()
        self._entry = {
            number: signal.getsignal(number) for number in _signal_numbers(_RESTORED_NAMES)
        }
        self._child: int | None = None
        # Terminal signals that must survive the launch window, held blocked until exec.
        # SIGTSTP is held only with a terminal: in a shared group the kernel stops both
        # processes, and a child stopped alone would hang the parent's plain wait.
        self._held = frozenset(
            _signal_numbers(_TTY_SIGNAL_NAMES if self._terminal else ("SIGINT", "SIGQUIT"))
        )
        # The handover pipe: the child waits on the read end until the parent has
        # re-sent its pending signals and closes its ends.
        self._handover_read: int | None = None
        self._handover_write: int | None = None

    # -- signal dispositions -------------------------------------------------

    def _restore_entry(self) -> None:
        """Put back the dispositions Booley started with (a nohup SIG_IGN survives)."""
        for number, handler in self._entry.items():
            if handler is not None:  # None: installed from C, cannot be re-set
                signal.signal(number, handler)

    def _install_before_fork(self) -> None:
        # An inherited SIG_IGN for SIGCHLD would make waitpid lose the status.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        if self._terminal:
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        # SIGINT, SIGQUIT and SIGTSTP stay as they were: held blocked across the fork,
        # so the child inherits the entry dispositions and the parent sets its own
        # before the child is released (`_start_parent`).

    def _install_parent_tty_handling(self) -> None:
        """Set the parent's dispositions for the held signals, after the child got its copies."""
        if self._terminal:
            for number in _signal_numbers(_TTY_SIGNAL_NAMES):
                signal.signal(number, self._on_tty_signal)
        else:
            # SIG_IGN also discards a copy that arrived after the drain; the child,
            # in the same group and holding the signal, got that one too.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGQUIT, signal.SIG_IGN)

    def _close_handover(self) -> None:
        """Close whichever ends of the handover pipe are still open."""
        for end in (self._handover_read, self._handover_write):
            if end is not None:
                with contextlib.suppress(OSError):
                    os.close(end)
        self._handover_read = self._handover_write = None

    def _wait_for_handover(self) -> None:
        """Child: block until the parent has closed the pipe (or died), then close it."""
        if self._handover_read is not None:
            with contextlib.suppress(OSError):
                os.read(self._handover_read, 1)  # nothing is ever written: returns at EOF
        self._close_handover()

    def _install_forwarders(self) -> None:
        """Forward termination requests, unless they were ignored at entry (nohup)."""
        for number in _signal_numbers(_FORWARDED_NAMES):
            if self._entry[number] != signal.SIG_IGN:
                signal.signal(number, self._forward)

    # -- handlers (parent) ---------------------------------------------------

    def _forward(self, number: int, _frame: Any) -> None:
        """Relay SIGTERM/SIGHUP to the agent's group (terminal mode) or the agent."""
        # Recorded even when the agent is already reaped: a hang-up that races the reap
        # still means the terminal is gone.
        self._forwarded = True
        if self._child is None:
            return
        send = os.killpg if self._terminal else os.kill
        with contextlib.suppress(ProcessLookupError):
            send(self._child, number)

    def _on_tty_signal(self, number: int, _frame: Any) -> None:
        """Repair a lost terminal handover; a no-op in the normal state.

        After Ctrl-Z, `bg`, then `fg` of the running job the shell only moves the
        terminal to this process's group, with no SIGCONT. The first tty signal
        then reaches us instead of the agent: pass the terminal and the signal on.
        """
        if self._child is None or not self._is_foreground():
            return
        self._hand_to_child()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._child, number)

    # -- terminal helpers ----------------------------------------------------

    @staticmethod
    def _is_foreground() -> bool:
        """Return whether Booley's group owns the terminal (a dead tty counts as no)."""
        try:
            return os.tcgetpgrp(0) == os.getpgrp()
        except OSError:
            return False

    def _hand_to_child(self) -> None:
        """Give the terminal to the agent's group; SIGTTOU is ignored in terminal mode."""
        if self._child is not None:
            with contextlib.suppress(OSError):
                os.tcsetpgrp(0, self._child)

    def _take_back(self) -> None:
        """Reclaim the terminal only if the child still holds it (never steal from the shell)."""
        if self._child is None:
            return
        with contextlib.suppress(OSError):
            if os.tcgetpgrp(0) == self._child:
                os.tcsetpgrp(0, os.getpgrp())

    # -- child ---------------------------------------------------------------

    def _run_child(self, outer_mask: Set[int]) -> None:
        """Become the agent's process: own group, own terminal, entry dispositions."""
        code = _EXEC_FAILED
        try:
            if self._handover_write is not None:
                os.close(self._handover_write)  # only the parent may hold the write end
                self._handover_write = None
            if self._terminal:
                with contextlib.suppress(OSError):
                    os.setpgid(0, 0)
                signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                with contextlib.suppress(OSError):
                    os.tcsetpgrp(0, os.getpgrp())
            # The parent re-sends its pending copies before it closes the pipe, so no
            # release below can act on a signal twice.
            self._wait_for_handover()
            self._restore_entry()
            # Keep the launch-window signals blocked (a signal blocked at entry stays
            # blocked through exec); `exec_agent` releases the rest just before exec.
            signal.pthread_sigmask(signal.SIG_SETMASK, {*outer_mask, *self._held})
            _held_until_exec.update(self._held - outer_mask)
            code = self._launch()
        except BaseException:  # noqa: BLE001 — a child must never unwind into the parent's stack
            traceback.print_exc()
        finally:
            try:
                _flush_std()
            finally:
                os._exit(code)

    # -- parent --------------------------------------------------------------

    def _forward_held_signals(self) -> None:
        """Re-send the parent's pending held signals to the child, consuming the parent's copy.

        A copy is pending here if it arrived before the fork (pending signals are not
        inherited), or reached only the parent (terminal mode, between the child's
        `setpgid` and the terminal handover), or reached both; the last merges with the
        child's own pending copy, so the agent still sees one.
        """
        assert self._child is not None
        # Single-threaded by the launch's contract: no other thread can take the shared
        # copy between `sigpending` and `sigwait`, which would block the wait forever.
        for number in sorted(signal.sigpending() & self._held):
            signal.sigwait({number})
            with contextlib.suppress(ProcessLookupError):
                os.kill(self._child, number)

    def _start_parent(self, child: int, outer_mask: Set[int]) -> None:
        """Record the child, give it its group and terminal, pass on held signals, then accept signals."""
        self._child = child
        if self._terminal:  # both sides do it: race-free
            with contextlib.suppress(OSError):
                os.setpgid(child, child)
            self._hand_to_child()
        self._forward_held_signals()
        self._install_parent_tty_handling()
        self._close_handover()  # releases the child
        self._install_forwarders()
        signal.pthread_sigmask(signal.SIG_SETMASK, outer_mask)

    def _wait_once(self) -> int:
        flags = os.WUNTRACED if self._terminal else 0
        assert self._child is not None
        return os.waitpid(self._child, flags)[1]

    def _wait(self) -> int:
        """Wait for the child to end, relaying its stops in terminal mode."""
        status = self._wait_once()
        while os.WIFSTOPPED(status):  # ends when the child exits or dies
            self._relay_stop(os.WSTOPSIG(status))
            status = self._wait_once()
        return status

    def _relay_stop(self, stop_signal: int) -> None:
        """Handle a child stop, then resume the child.

        A SIGTTIN/SIGTTOU stop while Booley owns the terminal (`fg` of a running
        job, which sends no SIGCONT) only needs the terminal handed over. Any
        other stop (Ctrl-Z, or a background read) suspends Booley's own job, so
        the shell regains the prompt; after `fg` the terminal goes to the agent.
        """
        assert self._child is not None
        repair_only = stop_signal in (signal.SIGTTIN, signal.SIGTTOU) and self._is_foreground()
        if not repair_only:
            self._suspend_job()
        if self._is_foreground():
            self._hand_to_child()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._child, signal.SIGCONT)

    def _suspend_job(self) -> None:
        """Stop Booley's whole job (siblings in a pipeline too) and wait for SIGCONT.

        SIGTSTP rather than SIGSTOP: the kernel discards it for an orphaned group
        (for example a session leader), so a parent nobody could resume never
        stops and Ctrl-Z degrades to an immediate resume instead of a hung terminal.
        """
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        try:
            os.killpg(os.getpgrp(), signal.SIGTSTP)
        finally:
            signal.signal(signal.SIGTSTP, self._on_tty_signal)

    def _supervise(self) -> int | None:
        """Fork the agent and wait for it; return the raw wait status, None if fork failed."""
        _flush_std()
        outer_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, [*_signal_numbers(_FORWARDED_NAMES), *self._held]
        )
        self._install_before_fork()
        try:
            try:
                # A pipe that cannot be made falls back to the in-process launch, like a fork.
                self._handover_read, self._handover_write = os.pipe()
                child = os.fork()
            except OSError:
                self._restore_entry()
                return None
            if child == 0:
                self._run_child(outer_mask)
            self._start_parent(child, outer_mask)
            try:
                return self._wait()
            finally:
                self._end_signal_handling()
        finally:
            # Every exit restores the mask and the hold; a faked fork in tests ends in
            # an exception here, and must not leave the worker's signals blocked. The
            # mask goes last: a held signal still pending then acts (KeyboardInterrupt
            # for SIGINT), and the cleanup must already be done by then.
            self._close_handover()
            _held_until_exec.clear()
            signal.pthread_sigmask(signal.SIG_SETMASK, outer_mask)

    def _end_signal_handling(self) -> None:
        """Stop relaying as soon as the child is reaped, then return the terminal."""
        if self._terminal:
            for number in _signal_numbers(_TTY_SIGNAL_NAMES):
                signal.signal(number, signal.SIG_IGN)
        self._take_back()
        self._child = None

    def execute(self) -> int:
        """Run the launch as a foreground child and return the code to exit with."""
        try:
            status = self._supervise()
        finally:
            self._restore_entry()
        if status is None:  # fork failed: today's in-process exec
            return self._launch()
        # Before `_propagate`, which may end this process by the agent's own signal.
        if self._after_exit is not None:
            self._after_exit(ChildExit(status, self._forwarded))
        return _propagate(status)


def exec_agent(executable: str, argv: list[str]) -> None:
    """Replace the process with the agent; the launch's last step.

    In the forked child, signals held through the launch window are released here,
    just before the exec: a pending SIGINT or SIGQUIT then ends this process by the
    signal's default action, as it would end the agent just after exec. Elsewhere
    (no fork, a failed fork, a test) nothing is held and this is a plain `os.execvp`.
    Returns only through an exception from `execvp`; the handlers and mask are then
    restored for the launch's error handling.
    """
    released = sorted(_held_until_exec)
    if not released:
        os.execvp(executable, argv)
    else:
        saved = {number: signal.getsignal(number) for number in released}
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        try:
            for number, handler in saved.items():
                if handler not in (signal.SIG_IGN, signal.SIG_DFL):
                    signal.signal(number, signal.SIG_DFL)  # exec resets caught signals anyway
            if signal.SIGQUIT in released and signal.SIGQUIT in signal.sigpending():
                import resource

                # Death is certain; leave no Python core file (as `_propagate` does).
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            signal.pthread_sigmask(signal.SIG_UNBLOCK, released)
            os.execvp(executable, argv)
        finally:
            for number, handler in saved.items():
                if handler is not None:
                    signal.signal(number, handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def run_foreground_child(
    launch: Callable[[], int], *, after_exit: Callable[[ChildExit], None] | None = None
) -> int:
    """Run *launch* in a forked child and return the child's exit status.

    *launch* execs the agent and does not return on success; on failure it
    returns the exit code. Without `os.fork` (Windows), or when forking fails,
    it runs in this process, exactly like the previous in-process exec.

    *after_exit* runs once the agent was reaped, with the terminal handed back and
    Booley's own signal dispositions restored, and before Booley takes on the agent's
    status. It does not run when the launch ran in this process. It must not change
    the status Booley exits with.
    """
    if not hasattr(os, "fork"):
        return launch()
    return _ForegroundRun(launch, after_exit).execute()
