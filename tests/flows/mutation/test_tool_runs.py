"""The Mutation Flow's tool boundary: a run is captured, measured and recorded."""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

from booley.flows import job_resources
from booley.flows.base import SubprocessResult
from booley.flows.job_resources import JobResourceMonitor, ResourceLabel
from booley.flows.mutation.tool_runs import run_tool
from booley.runtime import resource_history
from tests.canned_process_support import canned_command
from tests.time_budget import hang_guard_s, scaled

_LABEL = ResourceLabel("default", "sim_build")


def test_a_failed_tool_is_recorded_with_its_outcome(tmp_path: Path) -> None:
    done = run_tool(
        canned_command(SubprocessResult(returncode=3, stdout="out", stderr="err")),
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
        run_tool(sleeper, label=_LABEL, cwd=tmp_path, env=os.environ, timeout=scaled(1))

    assert "started" in str(raised.value.stdout)
    [record] = resource_history.load_records(tmp_path)
    assert record["outcome"] == "timeout"


def test_a_tool_that_reports_its_own_timeout_is_recorded_as_one(tmp_path: Path) -> None:
    run_tool(
        canned_command(SubprocessResult(returncode=1, stdout="it timed out")),
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
        run_tool(["booley-no-such-tool"], label=_LABEL, cwd=tmp_path, env=os.environ)

    assert resource_history.load_records(tmp_path) == []


def test_a_monitor_that_cannot_start_kills_the_tool_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("sampler cannot start")

    monkeypatch.setattr(JobResourceMonitor, "start", refuse)
    sleeper = [sys.executable, "-c", f"import time; time.sleep({hang_guard_s()})"]

    with pytest.raises(RuntimeError, match="sampler cannot start"):
        run_tool(sleeper, label=_LABEL, cwd=tmp_path, env=os.environ)

    assert resource_history.load_records(tmp_path) == []


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is POSIX")
@pytest.mark.parametrize(
    ("limit_reached", "kill", "outcome"),
    [(True, "sandbox_limit", "oom_kill"), (False, "host", "host_oom_kill")],
)
def test_a_tool_the_oom_killer_took_names_which_kill_it_was(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_reached: bool,
    kill: str,
    outcome: str,
) -> None:
    # The container's OOM counter rises over the run; the Sandbox's own limit event only
    # when it reached its limit.
    counter = itertools.count()

    def events() -> job_resources.MemoryEvents:
        kills = next(counter)
        return job_resources.MemoryEvents(kills, kills if limit_reached else 0)

    monkeypatch.setattr(job_resources, "cgroup_memory_events", events)
    killed = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]

    done = run_tool(killed, label=_LABEL, cwd=tmp_path, env=os.environ, timeout=hang_guard_s())

    assert done.oom_kill == kill
    [record] = resource_history.load_records(tmp_path)
    assert record["outcome"] == outcome
