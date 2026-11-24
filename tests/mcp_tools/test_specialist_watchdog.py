"""Parent-death watchdog for Specialist agent calls (SETUP-F-36).

A killed `docker exec` client used to leave the in-container endpoint and its
agent running detached, burning tokens with nobody reading the answer.
"""

from __future__ import annotations

import os
import signal
import time

import pytest

from booley.specialists import specialist
from booley.specialists.specialist import parent_death_watchdog
from tests.time_budget import hang_guard_s

pytestmark = pytest.mark.skipif(os.name != "posix", reason="watchdog is POSIX-only")

# Hang guard for the polling waits below; nothing asserts that it expires.
_HANG_S = hang_guard_s()


def _wait_for(predicate, timeout: float = _HANG_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def signals(monkeypatch):
    """Record the signals the watchdog sends instead of delivering them.

    ``install(descendants=(), leader=False, gone=())`` returns the list of
    ``(call, pid, signal)`` tuples; a pid in *gone* raises ``ProcessLookupError`` like a
    descendant that already exited.
    """

    def install(descendants=(), leader=False, gone=()):
        calls: list[tuple[str, int, int]] = []

        def kill(pid, sig):
            if pid in gone:
                raise ProcessLookupError(pid)
            calls.append(("kill", pid, sig))

        # The real /proc walk would find and signal pytest's own child processes.
        monkeypatch.setattr(specialist, "_descendant_pids", lambda _pid: list(descendants))
        monkeypatch.setattr(os, "getpgid", lambda _pid: os.getpid() if leader else os.getpid() + 7)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
        monkeypatch.setattr(os, "kill", kill)
        return calls

    return install


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        # The agent CLI is a child: signal the group when we lead it.
        ({"leader": True}, [("killpg", "self", signal.SIGTERM)]),
        # Not the group leader (MCP dispatch sets start_new_session): killpg is unavailable and
        # signalling only ourselves would leave the agent burning tokens.
        (
            {"descendants": [1234]},
            [("kill", 1234, signal.SIGTERM), ("kill", "self", signal.SIGTERM)],
        ),
        # A racy /proc snapshot: one descendant already gone must not skip the rest.
        (
            {"descendants": [111, 222], "gone": [111]},
            [("kill", 222, signal.SIGTERM), ("kill", "self", signal.SIGTERM)],
        ),
    ],
    ids=["group-leader", "not-leader", "descendant-already-gone"],
)
def test_watchdog_signals_the_agent_when_the_parent_dies(monkeypatch, signals, setup, expected):
    calls = signals(**setup)
    reported = {"ppid": 42}
    monkeypatch.setattr(os, "getppid", lambda: reported["ppid"])

    with parent_death_watchdog("reviewer", interval=0.01):
        reported["ppid"] = 1  # reparented to init: what a dead launcher looks like
        assert _wait_for(lambda: bool(calls)), "watchdog never fired"
        assert _wait_for(lambda: len(calls) >= len(expected))

    assert calls == [
        (name, os.getpid() if pid == "self" else pid, sig) for name, pid, sig in expected
    ]


@pytest.mark.parametrize("disabled", [False, True], ids=["parent-alive", "disabled-by-env"])
def test_watchdog_stays_quiet(monkeypatch, signals, disabled):
    calls = signals()
    reported = {"ppid": 42}
    monkeypatch.setattr(os, "getppid", lambda: reported["ppid"])
    if disabled:
        monkeypatch.setenv("BOOLEY_PARENT_WATCHDOG", "0")

    with parent_death_watchdog("reviewer", interval=0.01):
        reported["ppid"] = 1 if disabled else 42
        time.sleep(0.1)

    assert calls == []


def test_thread_stops_with_the_context(monkeypatch):
    """No leaked poller: the thread must exit when the call returns."""
    import threading

    monkeypatch.setattr(os, "getppid", lambda: 42)
    before = {t.name for t in threading.enumerate()}
    with parent_death_watchdog("reviewer", interval=0.01):
        assert any(t.name == "parent-watchdog-reviewer" for t in threading.enumerate())
    assert _wait_for(
        lambda: {t.name for t in threading.enumerate()} <= before | {"MainThread"},
    )


def test_is_the_shared_reaper_walk_not_a_second_copy():
    """One /proc walk in the codebase, not two that can drift apart."""
    from booley.runtime import process_tree

    # The alias is private to specialist.py: the walk is not reachable by any other name.
    assert specialist._descendant_pids is process_tree.descendant_pids
