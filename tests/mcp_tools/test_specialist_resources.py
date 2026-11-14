"""A Specialist run appends one record of its agent CLI's resources to the history (#4067).

The agent call is a fake backend that starts a real child process, so the
monitor measures a real process tree. The child holds its memory until a
sample taken after it finished allocating has seen it, then exits: no sleep
decides the result.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from booley.config.agent import AgentSettings
from booley.core.models import AgentCallParams, AgentResult
from booley.flows import job_resources
from booley.mcp.base import EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.runtime import agent_config, resource_history
from booley.specialists.specialist import Specialist
from tests.mcp_tools.test_specialist import _env_with_state
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")

_MIB = 1 << 20
# Allocates (and touches) *mib* MiB, says so, and holds until released.
_CHILD = f"""
import pathlib, sys, time
held = b"x" * (int(sys.argv[1]) << 20)
pathlib.Path(sys.argv[2]).write_text("ready")
release = pathlib.Path(sys.argv[3])
deadline = time.monotonic() + {SUBPROCESS_TIMEOUT_S}
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""
_ALLOCATE_AND_EXIT = 'held = b"x" * (96 << 20)'


class _Sampler:
    """Wraps ``process_tree`` to tell when a sample has seen the agent child."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pid: int | None = None
        self.seen = threading.Event()
        real = job_resources.process_tree

        def watching(root_pid: int, **kwargs: Any):
            found = real(root_pid, **kwargs)
            if self.pid is not None and found and self.pid in found:
                self.seen.set()
            return found

        monkeypatch.setattr(job_resources, "process_tree", watching)


class MeasuredSpecialist(Specialist):
    name = "measured"
    description = "Resource history test Specialist"

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        pass

    def _build_prompt(self) -> str:
        return "go"

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        ok = bool(structured and structured.get("approved"))
        return McpToolResult(exit_code=EXIT_SUCCESS if ok else EXIT_FAILURE, criterion_met=ok)


class RefusingSpecialist(MeasuredSpecialist):
    """Refuses its arguments before any agent call."""

    def _validate_interactive_args(self) -> McpToolResult | None:
        return McpToolResult(exit_code=EXIT_FAILURE, report_text="refused")


class _Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.booley_dir = tmp_path / ".booley"
        self.booley_dir.mkdir()
        (self.booley_dir / "booley.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("BOOLEY_DIR", str(self.booley_dir))
        self.sampler = _Sampler(monkeypatch)
        self.mem_mib = 32
        self.respond = self._agent_child

    def _agent_child(self, _params: AgentCallParams) -> AgentResult:
        flags = [self.tmp_path / name for name in ("ready", "release")]
        for flag in flags:
            flag.unlink(missing_ok=True)
        argv = [sys.executable, "-c", _CHILD, str(self.mem_mib), str(flags[0]), str(flags[1])]
        child = subprocess.Popen(argv)
        try:
            deadline = time.monotonic() + SUBPROCESS_TIMEOUT_S
            while not flags[0].exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert flags[0].exists(), "the agent child never allocated its memory"
            self.sampler.pid = child.pid
            assert self.sampler.seen.wait(SUBPROCESS_TIMEOUT_S), "no sample saw the agent child"
            flags[1].write_text("go")
            child.wait(timeout=SUBPROCESS_TIMEOUT_S)
        finally:
            child.kill()
            child.wait()
            self.sampler.pid = None
            self.sampler.seen.clear()
        return AgentResult(structured={"approved": True})

    def install_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = self

        class FakeBackend:
            async def call(self, params, *, on_event=None, on_start=None):
                return env.respond(params)

        monkeypatch.setattr(agent_config, "_make_backend", lambda _cli, _auth: FakeBackend())
        agent_config.set_backend_config(
            agent_config.configure_backend(AgentSettings(default_cli="claude", clis=("claude",)))
        )

    def run(self, endpoint: Specialist, *extra: str) -> McpToolResult:
        state_file = self.tmp_path / "state.json"
        state_file.write_text("{}")
        argv = ["--work-dir", str(self.tmp_path), "--report-dir", str(self.tmp_path / "reports")]
        with patch.dict(os.environ, _env_with_state(state_file)):
            return endpoint.execute_cli(
                [*argv, "--transcript-dir", str(self.tmp_path / "t"), *extra]
            ).outcome

    def records(self) -> list[dict[str, Any]]:
        return resource_history.load_records(self.booley_dir)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "booley.runtime.runtime_context.container_only_error", lambda _command: None
    )
    monkeypatch.setattr(
        "booley.specialists.specialist.parent_death_watchdog",
        lambda *_: contextlib.nullcontext(),
    )
    setup = _Env(tmp_path, monkeypatch)
    setup.install_backend(monkeypatch)
    yield setup
    agent_config.set_backend_config(None)


def test_specialist_run_records_its_agent_process_tree(env: _Env) -> None:
    assert env.run(RefusingSpecialist()).report_text == "refused"
    assert env.records() == []  # no agent call: nothing to measure

    outcome = env.run(MeasuredSpecialist())

    (record,) = env.records()
    assert outcome.exit_code == EXIT_SUCCESS
    assert record["key"] == {
        "target": "",
        "phase": "specialist:measured",
        "flags": [],
        "test_run": "",
        "specialist": "measured",
    }
    assert record["peak_rss_mb"] >= 32
    assert record["cpu_user_s"] is not None and record["wall_s"] > 0
    assert (record["outcome"], record["returncode"]) == ("exit_zero", EXIT_SUCCESS)


class _HoldsMemory(MeasuredSpecialist):
    """Holds a large buffer, reaps a large child before its agent calls, then calls twice."""

    def _run(self) -> McpToolResult:
        held = b"x" * (128 * _MIB)
        subprocess.run([sys.executable, "-c", _ALLOCATE_AND_EXIT], check=True)
        super()._run()
        result = super()._run()
        assert len(held) == 128 * _MIB
        return result


def test_record_measures_only_the_agent_call_tree(env: _Env) -> None:
    outcome = env.run(_HoldsMemory())

    (record,) = env.records()  # two agent calls, one record
    assert outcome.exit_code == EXIT_SUCCESS
    assert 32 <= record["peak_rss_mb"] < 96


def test_unmeasurable_values_are_recorded_absent(env: _Env, monkeypatch) -> None:
    env.respond = lambda _params: AgentResult(structured={"approved": True})
    monkeypatch.setattr(job_resources, "process_tree", lambda *_a, **_k: None)
    monkeypatch.setattr(job_resources, "_children_cpu_s", lambda: None)
    monkeypatch.setattr(job_resources, "cgroup_oom_kill_count", lambda: None)
    monkeypatch.setattr(job_resources, "_container_memory_bytes", lambda: None)

    assert env.run(MeasuredSpecialist()).exit_code == EXIT_SUCCESS

    (record,) = env.records()
    for field in (
        "peak_memory_mb",
        "peak_rss_mb",
        "peak_tmp_mb",
        "cpu_user_s",
        "cpu_sys_s",
        "oom_kill_delta",
        "container_peak_delta_mb",
    ):
        assert record[field] is None, field
    assert record["wall_s"] is not None


def test_failed_measurement_never_changes_the_specialist_result(
    env: _Env, monkeypatch, caplog
) -> None:
    env.respond = lambda _params: AgentResult(structured={"approved": True})
    expected = env.run(MeasuredSpecialist())
    assert len(env.records()) == 1
    history = resource_history.history_path(env.booley_dir)
    history.unlink()

    def fail(error: Exception):
        def raiser(*_a, **_k):
            raise error

        return raiser

    # The monitor cannot start: the run is still recorded, with nothing measured.
    with monkeypatch.context() as patched:
        patched.setattr(job_resources.JobResourceMonitor, "start", fail(RuntimeError("no thread")))
        outcome = env.run(MeasuredSpecialist())
    assert (outcome.exit_code, outcome.report_text) == (expected.exit_code, expected.report_text)
    (record,) = env.records()
    assert record["peak_rss_mb"] is None and record["cpu_user_s"] is None
    assert record["wall_s"] is not None
    history.unlink()

    # The history cannot be written: a warning, no record.
    with monkeypatch.context() as patched:
        patched.setattr(job_resources, "append_records", fail(OSError("read-only")))
        outcome = env.run(MeasuredSpecialist())
    assert (outcome.exit_code, outcome.report_text) == (expected.exit_code, expected.report_text)
    assert not history.exists()
    assert "Job resource history not recorded" in caplog.text


def test_run_that_raises_is_recorded_and_still_raises(env: _Env) -> None:
    class BoomError(Exception):
        pass

    class Raises(MeasuredSpecialist):
        def _run(self) -> McpToolResult:
            super()._run()
            raise BoomError("boom")

    env.respond = lambda _params: AgentResult(structured={"approved": True})
    endpoint = Raises()
    # The endpoint runner reports the exception as it always did ...
    outcome = env.run(endpoint)
    assert outcome.exit_code == 2 and "BoomError: boom" in outcome.report_text
    assert [(r["outcome"], r["returncode"]) for r in env.records()] == [("error", None)]
    # ... because the measured run raises it again, unchanged.
    with pytest.raises(BoomError, match="boom"):
        endpoint._measured_run()

    assert len(env.records()) == 2
