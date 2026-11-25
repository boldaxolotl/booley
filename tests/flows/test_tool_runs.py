"""The Mutation Flow's tool boundary: a run is captured, measured and recorded."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.job_resources import JobResourceMonitor, MeasuredPopen, ResourceLabel
from booley.flows.tool_runs import run_tool
from booley.runtime import resource_history
from tests.canned_process_support import canned_command
from tests.oom_events_support import rising_oom_events
from tests.pipe_holding_tree import (
    heartbeat_tree,
    parent_argv,
    wait_for_tree_start,
    wait_until_heartbeat_stops,
)
from tests.time_budget import hang_guard_s, scaled

_LABEL = ResourceLabel("default", "sim_build")


def test_a_failed_tool_is_recorded_with_its_outcome(tmp_path: Path) -> None:
    done = run_tool(
        canned_command(SubprocessResult(returncode=3, stdout="out", stderr="err")),
        flow="mutation",
        label=_LABEL,
        cwd=tmp_path,
        env=os.environ,
        timeout=hang_guard_s(),
    )

    assert (done.returncode, done.stdout, done.stderr) == (3, "out", "err")
    [record] = resource_history.load_records(tmp_path)
    assert (record["flow"], record["outcome"], record["returncode"]) == (
        "mutation",
        "exit_nonzero",
        3,
    )


def test_a_timed_out_tool_is_recorded_and_keeps_its_partial_output(tmp_path: Path) -> None:
    sleeper = [
        sys.executable,
        "-c",
        f"import time; print('started', flush=True); time.sleep({hang_guard_s()})",
    ]

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_tool(
            sleeper, flow="mutation", label=_LABEL, cwd=tmp_path, env=os.environ, timeout=scaled(1)
        )

    assert "started" in str(raised.value.stdout)
    [record] = resource_history.load_records(tmp_path)
    assert record["outcome"] == "timeout"


def test_a_tool_that_reports_its_own_timeout_is_recorded_as_one(tmp_path: Path) -> None:
    run_tool(
        canned_command(SubprocessResult(returncode=1, stdout="it timed out")),
        flow="mutation",
        label=_LABEL,
        cwd=tmp_path,
        env=os.environ,
        timeout=hang_guard_s(),
        timed_out=lambda text: "timed out" in text,
    )

    [record] = resource_history.load_records(tmp_path)
    assert record["outcome"] == "timeout"


def test_a_tool_that_cannot_start_raises_and_records_nothing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_tool(
            ["booley-no-such-tool"], flow="mutation", label=_LABEL, cwd=tmp_path, env=os.environ
        )

    assert resource_history.load_records(tmp_path) == []


def test_a_monitor_that_cannot_start_kills_the_tool_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("sampler cannot start")

    monkeypatch.setattr(JobResourceMonitor, "start", refuse)
    sleeper = [sys.executable, "-c", f"import time; time.sleep({hang_guard_s()})"]

    with pytest.raises(RuntimeError, match="sampler cannot start"):
        run_tool(sleeper, flow="mutation", label=_LABEL, cwd=tmp_path, env=os.environ)

    assert resource_history.load_records(tmp_path) == []


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is POSIX")
@pytest.mark.parametrize(
    ("limit_reached", "kill", "outcome"),
    [(True, "sandbox_limit", "oom_kill"), (False, "host", "host_oom_kill")],
)
def test_a_tool_the_oom_killer_took_names_which_kill_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit_reached: bool, kill: str, outcome: str
) -> None:
    rising_oom_events(monkeypatch, limit_reached=limit_reached)
    killed = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]

    done = run_tool(
        killed, flow="mutation", label=_LABEL, cwd=tmp_path, env=os.environ, timeout=hang_guard_s()
    )

    assert done.oom_kill == kill
    [record] = resource_history.load_records(tmp_path)
    assert record["outcome"] == outcome


def _start_monitor_after_tree_start(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """Hold the run's wait until the tool's child exists, so no deadline beats it."""
    real_start = JobResourceMonitor.start

    def start(self: JobResourceMonitor, pid: int) -> None:
        real_start(self, pid)
        wait_for_tree_start(paths)

    monkeypatch.setattr(JobResourceMonitor, "start", start)


@pytest.mark.parametrize(
    "mode",
    [pytest.param("live-until-stop", id="tool-running"), pytest.param("exit", id="tool-exited")],
)
def test_a_timed_out_tool_leaves_none_of_its_tree_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    with heartbeat_tree(tmp_path) as paths:
        _start_monitor_after_tree_start(monkeypatch, paths)

        with pytest.raises(subprocess.TimeoutExpired):
            run_tool(
                parent_argv(tmp_path, sys.executable, paths, mode),
                flow="mutation",
                label=_LABEL,
                cwd=tmp_path,
                env=os.environ,
                timeout=scaled(1),
            )

        assert not paths["done"].exists(), "the run waited for the tool's child to end"
        # taskkill /T cannot reach a child whose tool already exited: Windows has no group.
        if not (mode == "exit" and os.name == "nt"):
            assert wait_until_heartbeat_stops(paths["heartbeat"]), (
                "the tool's child was left running"
            )


def test_an_interrupted_tool_leaves_none_of_its_tree_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is the stdlib's ``Popen.communicate`` (outside Booley), the one place a real
    # Ctrl-C lands while the run waits on the tool's pipes.
    real_communicate = MeasuredPopen.communicate
    first_timeouts: list[float | None] = []

    def communicate(self: MeasuredPopen, *args: object, **kwargs: float | None) -> tuple[str, str]:
        if first_timeouts:  # the drain after the stop
            return real_communicate(self, *args, **kwargs)  # type: ignore[arg-type]
        first_timeouts.append(kwargs.get("timeout"))
        wait_for_tree_start(paths)
        with pytest.raises(subprocess.TimeoutExpired):
            real_communicate(self, timeout=0.05)
        raise KeyboardInterrupt

    with heartbeat_tree(tmp_path) as paths:
        monkeypatch.setattr(MeasuredPopen, "communicate", communicate)

        with pytest.raises(KeyboardInterrupt):
            run_tool(
                parent_argv(tmp_path, sys.executable, paths, "live-until-stop"),
                flow="mutation",
                label=_LABEL,
                cwd=tmp_path,
                env=os.environ,
            )

        assert wait_until_heartbeat_stops(paths["heartbeat"]), "the tool's child was left running"
    [first] = first_timeouts
    assert first is not None and first <= 1, "an untimed wait cannot be interrupted"


def test_a_tool_killed_mid_character_still_times_out_and_is_recorded(tmp_path: Path) -> None:
    writer = [
        sys.executable,
        "-c",
        f"import sys, time; sys.stdout.buffer.write(b'\\xe2'); sys.stdout.flush(); "
        f"time.sleep({hang_guard_s()})",
    ]

    with pytest.raises(subprocess.TimeoutExpired):
        run_tool(
            writer, flow="mutation", label=_LABEL, cwd=tmp_path, env=os.environ, timeout=scaled(1)
        )

    [record] = resource_history.load_records(tmp_path)
    assert record["outcome"] == "timeout"
