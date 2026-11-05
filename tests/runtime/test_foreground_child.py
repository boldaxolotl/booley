"""In-process coverage for `run_foreground_child` paths a forked pty test cannot save."""

from __future__ import annotations

import os
import signal

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
@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGKILL])
def test_a_signal_death_is_re_raised_on_the_parent(
    monkeypatch: pytest.MonkeyPatch, number: signal.Signals
) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(signal, "signal", lambda sig, action: events.append(("signal", sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: events.append(("kill", (pid, sig))))
    monkeypatch.setattr("resource.setrlimit", lambda *_args: events.append(("rlimit", None)))

    # os.kill returns here, so the fallback code is observable.
    assert foreground_child._propagate(int(number)) == 128 + number

    expected_reset = [] if number == signal.SIGKILL else [("signal", number)]
    assert events == [("rlimit", None), *expected_reset, ("kill", (os.getpid(), number))]
