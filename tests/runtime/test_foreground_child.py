"""In-process coverage for `run_foreground_child` paths a forked pty test cannot save."""

from __future__ import annotations

import os
import signal
import threading

import pytest

from booley.runtime import foreground_child

_needs_fork = pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork and signals")


class _ChildExitError(Exception):
    """Stands in for `os._exit` so the forked-child branch can run in-process."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def test_without_fork_the_launch_runs_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "fork", raising=False)
    calls: list[str] = []

    def launch() -> int:
        calls.append("launch")
        return 3

    assert foreground_child.run_foreground_child(launch) == 3
    assert calls == ["launch"]


@_needs_fork
def test_the_child_restores_entry_dispositions_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exit_child(code: int) -> None:
        raise _ChildExitError(code)

    monkeypatch.setattr(foreground_child.os, "fork", lambda: 0)
    monkeypatch.setattr(foreground_child.os, "_exit", exit_child)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)  # e.g. `nohup booley chat`
    seen: dict[str, object] = {}

    def launch() -> int:
        seen["hup"] = signal.getsignal(signal.SIGHUP)
        seen["int"] = signal.getsignal(signal.SIGINT)
        return 5

    try:
        entry_int = signal.getsignal(signal.SIGINT)
        with pytest.raises(_ChildExitError) as raised:
            foreground_child.run_foreground_child(launch)
    finally:
        signal.signal(signal.SIGHUP, previous)

    assert raised.value.code == 5
    assert seen == {"hup": signal.SIG_IGN, "int": entry_int}


@_needs_fork
# Signal names, not numbers: Windows has no SIGKILL, and collection runs there too.
@pytest.mark.parametrize("name", ["SIGTERM", "SIGKILL"])
def test_a_signal_death_is_re_raised_on_the_parent(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    number: signal.Signals = getattr(signal, name)
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(signal, "signal", lambda sig, action: events.append(("signal", sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: events.append(("kill", (pid, sig))))
    monkeypatch.setattr("resource.setrlimit", lambda *_args: events.append(("rlimit", None)))

    # os.kill returns here, so the fallback code is observable.
    assert foreground_child._propagate(int(number)) == 128 + number

    expected_reset = [] if number == signal.SIGKILL else [("signal", number)]
    assert events == [("rlimit", None), *expected_reset, ("kill", (os.getpid(), number))]


@_needs_fork
def test_a_failed_fork_falls_back_to_the_in_process_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_fork() -> int:
        raise OSError("pids limit")

    monkeypatch.setattr(foreground_child.os, "fork", refuse_fork)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)

    assert foreground_child.run_foreground_child(lambda: 4) == 4


@_needs_fork
def test_a_launch_that_raises_ends_the_child_with_the_failure_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def exit_child(code: int) -> None:
        raise _ChildExitError(code)

    def explode() -> int:
        raise RuntimeError("launch blew up")

    monkeypatch.setattr(foreground_child.os, "fork", lambda: 0)
    monkeypatch.setattr(foreground_child.os, "_exit", exit_child)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)

    with pytest.raises(_ChildExitError) as raised:
        foreground_child.run_foreground_child(explode)

    assert raised.value.code == 2
    assert "launch blew up" in capsys.readouterr().err


@_needs_fork
def test_a_tty_that_is_not_our_controlling_terminal_is_not_terminal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_a_controlling_tty(_fd: int) -> int:
        raise OSError("ENOTTY")

    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(foreground_child.os, "tcgetpgrp", not_a_controlling_tty)

    assert foreground_child.foreground_tty() is False
    assert foreground_child._ForegroundRun._is_foreground() is False


@_needs_fork
def test_forwarding_waits_for_a_child_and_targets_it(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        foreground_child.os, "kill", lambda pid, number: sent.append((pid, number))
    )
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)
    run = foreground_child._ForegroundRun(lambda: 0)

    run._forward(signal.SIGTERM, None)  # no child yet: nothing to signal
    run._child = 4321
    run._forward(signal.SIGHUP, None)

    assert sent == [(4321, signal.SIGHUP)]


@_needs_fork
def test_a_closed_stderr_does_not_stop_the_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """`booley chat 2>&-` leaves sys.stderr None; flushing it must not raise."""

    def exit_child(code: int) -> None:
        raise _ChildExitError(code)

    monkeypatch.setattr(foreground_child.sys, "stderr", None)
    monkeypatch.setattr(foreground_child.os, "fork", lambda: 0)
    monkeypatch.setattr(foreground_child.os, "_exit", exit_child)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)

    with pytest.raises(_ChildExitError) as raised:
        foreground_child.run_foreground_child(lambda: 2)

    assert raised.value.code == 2


# -- signals held across the launch window -----------------------------------------


def _current_mask() -> frozenset[int]:
    return frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, []))


@pytest.fixture
def fake_terminal_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Make `_ForegroundRun` believe it owns a terminal without touching process groups."""
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(foreground_child, "foreground_tty", lambda: True)
    monkeypatch.setattr(
        foreground_child.os, "setpgid", lambda pid, group: calls.append(("setpgid", pid))
    )
    monkeypatch.setattr(
        foreground_child.os, "tcsetpgrp", lambda _fd, group: calls.append(("tcsetpgrp", group))
    )
    monkeypatch.setattr(foreground_child.os, "tcgetpgrp", lambda _fd: os.getpgrp())
    return calls


def _fake_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the forked-child branch in-process: the 'child' closes the only write end."""

    def exit_child(code: int) -> None:
        raise _ChildExitError(code)

    monkeypatch.setattr(foreground_child.os, "fork", lambda: 0)
    monkeypatch.setattr(foreground_child.os, "_exit", exit_child)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)


@_needs_fork
def test_the_child_holds_launch_signals_until_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_child(monkeypatch)
    outer_mask = _current_mask()
    seen: dict[str, object] = {}

    def launch() -> int:
        seen["mask"] = _current_mask()
        seen["release"] = set(foreground_child._held_until_exec)
        return 5

    with pytest.raises(_ChildExitError):
        foreground_child.run_foreground_child(launch)

    held = {signal.SIGINT, signal.SIGQUIT}
    assert seen == {"mask": outer_mask | held, "release": held}
    # The in-process stand-in must not leak the hold into the test worker.
    assert _current_mask() == outer_mask
    assert foreground_child._held_until_exec == set()


@_needs_fork
def test_a_terminal_child_also_holds_ctrl_z_and_takes_the_terminal_first(
    monkeypatch: pytest.MonkeyPatch, fake_terminal_calls: list[tuple[str, int]]
) -> None:
    _fake_child(monkeypatch)
    seen: dict[str, object] = {}

    def launch() -> int:
        seen["held"] = {signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP} <= _current_mask()
        seen["calls"] = list(fake_terminal_calls)
        return 5

    with pytest.raises(_ChildExitError):
        foreground_child.run_foreground_child(launch)

    assert seen == {
        "held": True,
        "calls": [("setpgid", 0), ("tcsetpgrp", os.getpgrp())],
    }


@_needs_fork
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("name", ["SIGINT", "SIGQUIT", None])
def test_the_parent_forwards_a_held_signal_to_the_child_once(
    monkeypatch: pytest.MonkeyPatch,
    fake_terminal_calls: list[tuple[str, int]],
    terminal: bool,
    name: str | None,
) -> None:
    """A copy that reached only the parent is consumed and re-sent, never left to kill it."""
    monkeypatch.setattr(foreground_child, "foreground_tty", lambda: terminal)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: terminal)
    child = 4242
    sent: list[tuple[int, int]] = []
    during_wait: dict[str, object] = {}

    def fork_after_signalling_itself() -> int:
        if name is not None:
            # Thread-directed: a pytest-xdist worker has other threads that would take
            # (and die of) a process-directed copy; this thread has it blocked.
            signal.pthread_kill(threading.get_ident(), getattr(signal, name))
        return child

    def wait_for_child(pid: int, _flags: int) -> tuple[int, int]:
        during_wait["pending"] = signal.sigpending()
        during_wait["disposition"] = signal.getsignal(signal.SIGINT)
        return pid, 0

    monkeypatch.setattr(foreground_child.os, "fork", fork_after_signalling_itself)
    monkeypatch.setattr(foreground_child.os, "waitpid", wait_for_child)
    monkeypatch.setattr(
        foreground_child.os, "kill", lambda pid, number: sent.append((pid, number))
    )
    outer_mask = _current_mask()
    entry_int = signal.getsignal(signal.SIGINT)

    assert foreground_child.run_foreground_child(lambda: 0) == 0

    assert sent == ([] if name is None else [(child, getattr(signal, name))])
    assert during_wait["pending"] == set()  # the parent's own copy is gone
    # Shared: the parent ignores them; terminal: the repair handler is installed.
    assert (during_wait["disposition"] == signal.SIG_IGN) is (not terminal)
    assert _current_mask() == outer_mask
    assert signal.getsignal(signal.SIGINT) == entry_int


@_needs_fork
def test_the_hold_is_released_when_the_fork_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_fork() -> int:
        raise OSError("pids limit")

    monkeypatch.setattr(foreground_child.os, "fork", refuse_fork)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)
    outer_mask = _current_mask()

    assert foreground_child.run_foreground_child(lambda: 4) == 4

    assert _current_mask() == outer_mask


@_needs_fork
def test_a_pipe_that_cannot_be_made_falls_back_to_the_in_process_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_pipe() -> tuple[int, int]:
        raise OSError("too many open files")

    monkeypatch.setattr(foreground_child.os, "pipe", refuse_pipe)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)
    outer_mask = _current_mask()

    assert foreground_child.run_foreground_child(lambda: 4) == 4

    assert _current_mask() == outer_mask


@_needs_fork
def test_a_signal_pending_when_the_fork_fails_acts_after_the_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The released signal may raise (KeyboardInterrupt); the launch's state must be gone first."""
    state_when_it_acted: list[tuple[set[signal.Signals], frozenset[int]]] = []

    def caught(_number: int, _frame: object) -> None:
        state_when_it_acted.append((set(foreground_child._held_until_exec), _current_mask()))

    def fork_after_signalling_itself() -> int:
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)  # blocked: stays pending
        raise OSError("pids limit")

    outer_mask = _current_mask()
    previous = signal.signal(signal.SIGINT, caught)
    monkeypatch.setattr(foreground_child.os, "fork", fork_after_signalling_itself)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)
    try:
        assert foreground_child.run_foreground_child(lambda: 4) == 4
    finally:
        signal.signal(signal.SIGINT, previous)

    assert state_when_it_acted == [(set(), outer_mask)]


@_needs_fork
def test_a_quit_pending_but_not_held_leaves_the_core_limit_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits: list[object] = []
    monkeypatch.setattr(foreground_child, "_held_until_exec", {signal.SIGINT})
    monkeypatch.setattr(signal, "sigpending", lambda: {signal.SIGQUIT})
    monkeypatch.setattr(signal, "pthread_sigmask", lambda _how, _signals: set())
    monkeypatch.setattr("resource.setrlimit", lambda *args: limits.append(args))
    monkeypatch.setattr(foreground_child.os, "execvp", lambda _exe, _argv: None)

    foreground_child.exec_agent("agent", ["agent"])

    assert limits == []


@_needs_fork
def test_without_a_hold_the_agent_exec_is_a_plain_execvp(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(foreground_child.os, "execvp", lambda exe, argv: calls.append((exe, argv)))

    foreground_child.exec_agent("agent", ["agent", "--flag"])

    assert calls == [("agent", ["agent", "--flag"])]


@_needs_fork
def test_a_failed_agent_exec_restores_the_caught_dispositions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def caught(_number: int, _frame: object) -> None:  # a Python handler, as Booley's own
        pass

    monkeypatch.setattr(foreground_child, "_held_until_exec", {signal.SIGINT})
    previous = signal.signal(signal.SIGINT, caught)
    outer_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGINT])
    try:
        with pytest.raises(FileNotFoundError):
            foreground_child.exec_agent("booley-no-such-agent-cli", ["booley-no-such-agent-cli"])
        assert signal.getsignal(signal.SIGINT) is caught
        assert signal.SIGINT in _current_mask()  # still held: the launch goes on to fail
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, outer_mask)
        signal.signal(signal.SIGINT, previous)


@_needs_fork
def test_a_pending_quit_costs_no_core_file_and_acts_before_the_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order matters: default disposition and core limit first, unblock, then exec."""
    events: list[str] = []

    def caught(_number: int, _frame: object) -> None:
        pass

    def record_mask(how: int, _signals: object) -> set[signal.Signals]:
        events.append(f"mask:{how}:{signal.getsignal(signal.SIGQUIT) == signal.SIG_DFL}")
        return set()

    def refuse_exec(_exe: str, _argv: list[str]) -> None:
        events.append("exec")
        raise FileNotFoundError

    monkeypatch.setattr(foreground_child, "_held_until_exec", {signal.SIGQUIT})
    monkeypatch.setattr(signal, "sigpending", lambda: {signal.SIGQUIT})
    monkeypatch.setattr(signal, "pthread_sigmask", record_mask)
    monkeypatch.setattr("resource.setrlimit", lambda *_args: events.append("rlimit"))
    monkeypatch.setattr(foreground_child.os, "execvp", refuse_exec)
    previous = signal.signal(signal.SIGQUIT, caught)
    try:
        with pytest.raises(FileNotFoundError):
            foreground_child.exec_agent("agent", ["agent"])
    finally:
        signal.signal(signal.SIGQUIT, previous)

    unblock = f"mask:{signal.SIG_UNBLOCK}:True"
    restore = f"mask:{signal.SIG_SETMASK}:False"  # handler back before the mask is restored
    assert events == [f"mask:{signal.SIG_BLOCK}:False", "rlimit", unblock, "exec", restore]


# -- the exit hook ---------------------------------------------------------------


def _fake_parent_of(monkeypatch: pytest.MonkeyPatch, events: list[tuple[object, ...]]) -> None:
    """Pose as the parent of a forked child (pid 4242) that is signalled from outside."""
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(foreground_child.os, "fork", lambda: 4242)
    monkeypatch.setattr(
        foreground_child.os, "kill", lambda pid, number: events.append(("kill", pid, number))
    )

    def propagate(status: int) -> int:
        events.append(("propagate", status))
        return 129

    monkeypatch.setattr(foreground_child, "_propagate", propagate)


@_needs_fork
def test_after_exit_sees_the_status_and_forwarded_hangup_before_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug caught: a hook that runs after `_propagate` (which ends Booley by the agent's own
    # signal), or a hang-up the parent relayed that the hook cannot tell from a plain exit.
    events: list[tuple[object, ...]] = []
    _fake_parent_of(monkeypatch, events)
    died_of_hangup = int(signal.SIGHUP)

    def wait_for_child(pid: int, _flags: int) -> tuple[int, int]:
        # The terminal closes while the agent runs: the parent's handler relays the hang-up.
        signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
        return pid, died_of_hangup

    monkeypatch.setattr(foreground_child.os, "waitpid", wait_for_child)
    entry = signal.signal(signal.SIGHUP, signal.SIG_DFL)  # a worker may have started ignoring it
    try:
        code = foreground_child.run_foreground_child(
            lambda: 0, after_exit=lambda end: events.append(("after_exit", end))
        )
    finally:
        signal.signal(signal.SIGHUP, entry)

    assert events == [
        ("kill", 4242, signal.SIGHUP),
        ("after_exit", foreground_child.ChildExit(died_of_hangup, forwarded=True)),
        ("propagate", died_of_hangup),
    ]
    assert code == 129


@_needs_fork
def test_after_exit_reports_a_plain_exit_as_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[object, ...]] = []
    _fake_parent_of(monkeypatch, events)
    monkeypatch.setattr(foreground_child.os, "waitpid", lambda pid, _flags: (pid, 3 << 8))

    foreground_child.run_foreground_child(
        lambda: 0, after_exit=lambda end: events.append(("after_exit", end))
    )

    assert events == [
        ("after_exit", foreground_child.ChildExit(3 << 8, forwarded=False)),
        ("propagate", 3 << 8),
    ]


@_needs_fork
def test_after_exit_does_not_run_when_the_launch_ran_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_fork() -> int:
        raise OSError("pids limit")

    monkeypatch.setattr(foreground_child.os, "fork", refuse_fork)
    monkeypatch.setattr(foreground_child.os, "isatty", lambda _fd: False)
    called: list[foreground_child.ChildExit] = []

    code = foreground_child.run_foreground_child(lambda: 4, after_exit=called.append)

    assert (code, called) == (4, [])
