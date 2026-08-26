"""Parent-death watchdog for Specialist agent calls (SETUP-F-36).

A killed `docker exec` client used to leave the in-container endpoint and its
agent running detached, burning tokens with nobody reading the answer.
"""

from __future__ import annotations

import os
import time

import pytest

from booley.specialists import specialist
from booley.specialists.specialist import parent_death_watchdog

pytestmark = pytest.mark.skipif(os.name != "posix", reason="watchdog is POSIX-only")


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestParentDeathWatchdog:
    def test_fires_when_parent_pid_changes(self, monkeypatch):
        aborts: list[tuple[str, int]] = []
        monkeypatch.setattr(
            specialist,
            "_abort_orphaned_run",
            lambda label, ppid: aborts.append((label, ppid)),
        )
        real_ppid = 42
        reported = {"ppid": real_ppid}
        monkeypatch.setattr(os, "getppid", lambda: reported["ppid"])

        with parent_death_watchdog("reviewer", interval=0.01):
            # Reparented to init — exactly what a dead launcher looks like.
            reported["ppid"] = 1
            assert _wait_for(lambda: bool(aborts)), "watchdog never fired"

        assert aborts == [("reviewer", real_ppid)]

    def test_quiet_while_the_parent_lives(self, monkeypatch):
        aborts: list[tuple[str, int]] = []
        monkeypatch.setattr(
            specialist,
            "_abort_orphaned_run",
            lambda label, ppid: aborts.append((label, ppid)),
        )
        with parent_death_watchdog("reviewer", interval=0.01):
            time.sleep(0.1)
        assert aborts == []

    def test_disabled_by_env(self, monkeypatch):
        aborts: list[tuple[str, int]] = []
        monkeypatch.setattr(
            specialist,
            "_abort_orphaned_run",
            lambda label, ppid: aborts.append((label, ppid)),
        )
        monkeypatch.setenv("BOOLEY_PARENT_WATCHDOG", "0")
        reported = {"ppid": os.getppid()}
        monkeypatch.setattr(os, "getppid", lambda: reported["ppid"])

        with parent_death_watchdog("reviewer", interval=0.01):
            reported["ppid"] = 1
            time.sleep(0.05)
        assert aborts == []

    def test_thread_stops_with_the_context(self, monkeypatch):
        """No leaked poller: the thread must exit when the call returns."""
        import threading

        monkeypatch.setattr(os, "getppid", lambda: 42)
        before = {t.name for t in threading.enumerate()}
        with parent_death_watchdog("reviewer", interval=0.01):
            assert any(t.name == "parent-watchdog-reviewer" for t in threading.enumerate())
        assert _wait_for(
            lambda: {t.name for t in threading.enumerate()} <= before | {"MainThread"},
            timeout=1.0,
        )

    def test_abort_signals_own_process_group(self, monkeypatch):
        """The agent CLI is a child — signal the group, not just ourselves."""
        import signal

        calls: list[tuple[str, int, int]] = []
        monkeypatch.setattr(specialist, "_descendant_pids", lambda _pid: [])
        monkeypatch.setattr(specialist.os, "getpgid", lambda _pid: os.getpid())
        monkeypatch.setattr(
            specialist.os,
            "killpg",
            lambda pgid, sig: calls.append(("killpg", pgid, sig)),
        )
        monkeypatch.setattr(
            specialist.os,
            "kill",
            lambda pid, sig: calls.append(("kill", pid, sig)),
        )

        specialist._abort_orphaned_run("reviewer", 42)

        assert calls == [("killpg", os.getpid(), signal.SIGTERM)]

    def test_abort_kills_the_agent_when_we_do_not_lead_the_group(self, monkeypatch):
        """The whole point of the watchdog: the agent CLI must not survive.

        A directly launched Specialist need not be a process-group leader
        (MCP dispatch sets ``start_new_session``), and the Codex/Claude CLI is
        spawned as a plain child — so killpg is unavailable and killing just
        ourselves would leave the expensive half burning tokens.
        """
        import signal

        calls: list[tuple[str, int, int]] = []
        agent_pid = os.getpid() + 1000
        monkeypatch.setattr(specialist, "_descendant_pids", lambda _pid: [agent_pid])
        # Not the group leader — the accident this must not depend on.
        monkeypatch.setattr(specialist.os, "getpgid", lambda _pid: os.getpid() + 7)
        monkeypatch.setattr(
            specialist.os,
            "killpg",
            lambda pgid, sig: calls.append(("killpg", pgid, sig)),
        )
        monkeypatch.setattr(
            specialist.os,
            "kill",
            lambda pid, sig: calls.append(("kill", pid, sig)),
        )

        specialist._abort_orphaned_run("reviewer", 42)

        assert ("kill", agent_pid, signal.SIGTERM) in calls
        assert calls[-1] == ("kill", os.getpid(), signal.SIGTERM)
        assert not any(c[0] == "killpg" for c in calls)

    def test_abort_survives_a_descendant_that_already_exited(self, monkeypatch):
        """Racy /proc snapshot: an ESRCH on one pid must not skip the rest."""
        killed: list[int] = []
        monkeypatch.setattr(specialist, "_descendant_pids", lambda _pid: [111, 222])
        monkeypatch.setattr(specialist.os, "getpgid", lambda _pid: os.getpid() + 7)

        def _kill(pid, sig):
            if pid == 111:
                raise ProcessLookupError(pid)
            killed.append(pid)

        monkeypatch.setattr(specialist.os, "kill", _kill)

        specialist._abort_orphaned_run("reviewer", 42)

        assert killed == [222, os.getpid()]


class TestDescendantPids:
    """The /proc walk that makes teardown independent of the process group."""

    def test_finds_a_real_child_and_its_own_child(self):
        import subprocess

        # sh -> sleep: a child and a grandchild, both ours.
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & wait"])
        try:
            assert _wait_for(lambda: len(specialist._descendant_pids(os.getpid())) >= 2)
            pids = specialist._descendant_pids(os.getpid())
            assert proc.pid in pids
            assert os.getpid() not in pids  # never signal ourselves twice
            # The grandchild (`sleep`) is reached transitively — that is the
            # process actually holding the money in the real case.
            assert any(p not in (proc.pid, os.getpid()) for p in pids)
        finally:
            proc.kill()
            proc.wait()

    def test_is_the_shared_reaper_walk_not_a_second_copy(self):
        """One /proc walk in the codebase, not two that can drift apart."""
        from booley.runtime import process_tree

        assert specialist._descendant_pids is process_tree.descendant_pids
