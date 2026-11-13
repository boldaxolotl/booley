"""The measured FuseSoC setup runner: label, working directory, failures (#3787)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from booley.flows import job_resources
from booley.flows.base import SubprocessResult
from booley.flows.sim.build import measured_setup_runner, setup_timeout_s
from booley.flows.sim.config import DEFAULT_SIM_BUILD_TIMEOUT_MS


class _Invoker:
    """A process transport that records what it was asked and answers *result*."""

    def __init__(self, result: SubprocessResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], int, Path]] = []
        self.labels: list[job_resources.ResourceLabel | None] = []

    def __call__(self, command: list[str], *, timeout: int, cwd: Path) -> SubprocessResult:
        self.calls.append((command, timeout, cwd))
        self.labels.append(job_resources.current_label())
        return self.result


def _run(invoker: _Invoker, tmp_path: Path, command: list[str] | None = None):
    runner = measured_setup_runner(invoker, target="top", timeout_s=90)
    return runner(
        command or [sys.executable, "setup"],
        cwd=str(tmp_path),
        env={"IGNORED": "1"},
        capture_output=True,
        text=True,
    )


def test_setup_runs_in_the_project_labelled_as_sim_prepare_without_tests(tmp_path: Path) -> None:
    invoker = _Invoker(SubprocessResult(returncode=0, stdout="out", stderr="err"))
    job_resources.clear_label()

    completed = _run(invoker, tmp_path)

    assert invoker.calls == [([sys.executable, "setup"], 90, tmp_path)]
    # The label carries no Test names: the record is keyed by the request's sim_prepare key.
    assert invoker.labels == [job_resources.ResourceLabel("top", "sim_prepare")]
    assert job_resources.current_label() is None, "the label does not outlive the setup"
    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "out", "err")


def test_missing_program_raises_the_diagnostic_the_registry_words(tmp_path: Path) -> None:
    invoker = _Invoker(SubprocessResult(returncode=0))

    with pytest.raises(FileNotFoundError) as raised:
        _run(invoker, tmp_path, ["no-such-fusesoc-binary"])

    assert "No such file or directory" in str(raised.value)
    assert "no-such-fusesoc-binary" in str(raised.value)
    assert invoker.calls == []


def test_timeout_and_oom_are_named_in_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timed_out = _run(
        _Invoker(SubprocessResult(returncode=-9, stderr="partial", timed_out=True)), tmp_path
    )
    assert timed_out.returncode == -9
    assert timed_out.stderr == "partial\nFuseSoC setup timed out after 90 s"

    # Only the command's own OOM kill is blamed.
    killed = _run(
        _Invoker(SubprocessResult(returncode=-9, oom_killed=True, peak_rss_mb=812.4)), tmp_path
    )
    assert killed.stderr.startswith("FuseSoC setup failed while the Sandbox recorded an OOM kill")
    assert "peak RSS 812 MB" in killed.stderr
    unknown_peak = _run(_Invoker(SubprocessResult(returncode=-9, oom_killed=True)), tmp_path)
    assert "peak RSS unknown" in unknown_peak.stderr
    # Another Job's kill (the container counter) is not this setup's.
    other = _run(
        _Invoker(SubprocessResult(returncode=1, oom_kill_delta=1, stderr="boom")), tmp_path
    )
    assert other.stderr == "boom"


def test_setup_is_bounded_by_the_build_timeout_but_never_below_an_hour() -> None:
    hour = DEFAULT_SIM_BUILD_TIMEOUT_MS // 1000
    assert setup_timeout_s(60) == hour
    assert setup_timeout_s(hour) == hour
    assert setup_timeout_s(hour * 3) == hour * 3
