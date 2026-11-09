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

This module must import on Windows (`booley.py` imports `chat_cmd` for every
command), so POSIX-only names are referenced inside functions only.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import traceback
from collections.abc import Callable
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


def _signal_numbers(names: tuple[str, ...]) -> list[signal.Signals]:
    return [getattr(signal, name) for name in names]


def _foreground_tty() -> bool:
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

    def __init__(self, launch: Callable[[], int]) -> None:
        self._launch = launch
        self._terminal = _foreground_tty()
        self._entry = {
            number: signal.getsignal(number) for number in _signal_numbers(_RESTORED_NAMES)
        }
        self._child: int | None = None

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
            for number in _signal_numbers(_TTY_SIGNAL_NAMES):
                signal.signal(number, self._on_tty_signal)
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        else:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGQUIT, signal.SIG_IGN)

    def _install_forwarders(self) -> None:
        """Forward termination requests, unless they were ignored at entry (nohup)."""
        for number in _signal_numbers(_FORWARDED_NAMES):
            if self._entry[number] != signal.SIG_IGN:
                signal.signal(number, self._forward)

    # -- handlers (parent) ---------------------------------------------------

    def _forward(self, number: int, _frame: Any) -> None:
        """Relay SIGTERM/SIGHUP to the agent's group (terminal mode) or the agent."""
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

    def _run_child(self, outer_mask: set[signal.Signals]) -> None:
        """Become the agent's process: own group, own terminal, entry dispositions."""
        code = _EXEC_FAILED
        try:
            if self._terminal:
                with contextlib.suppress(OSError):
                    os.setpgid(0, 0)
                signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                with contextlib.suppress(OSError):
                    os.tcsetpgrp(0, os.getpgrp())
            self._restore_entry()
            signal.pthread_sigmask(signal.SIG_SETMASK, outer_mask)
            code = self._launch()
        except BaseException:  # noqa: BLE001 — a child must never unwind into the parent's stack
            traceback.print_exc()
        finally:
            try:
                _flush_std()
            finally:
                os._exit(code)

    # -- parent --------------------------------------------------------------

    def _start_parent(self, child: int, outer_mask: set[signal.Signals]) -> None:
        """Record the child, give it its group and terminal, then accept signals."""
        self._child = child
        if self._terminal:  # both sides do it: race-free
            with contextlib.suppress(OSError):
                os.setpgid(child, child)
            self._hand_to_child()
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
        outer_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _signal_numbers(_FORWARDED_NAMES))
        self._install_before_fork()
        try:
            child = os.fork()
        except OSError:
            self._restore_entry()
            signal.pthread_sigmask(signal.SIG_SETMASK, outer_mask)
            return None
        if child == 0:
            self._run_child(outer_mask)
        self._start_parent(child, outer_mask)
        try:
            return self._wait()
        finally:
            self._end_signal_handling()

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
        return _propagate(status)


def run_foreground_child(launch: Callable[[], int]) -> int:
    """Run *launch* in a forked child and return the child's exit status.

    *launch* execs the agent and does not return on success; on failure it
    returns the exit code. Without `os.fork` (Windows), or when forking fails,
    it runs in this process, exactly like the previous in-process exec.
    """
    if not hasattr(os, "fork"):
        return launch()
    return _ForegroundRun(launch).execute()
