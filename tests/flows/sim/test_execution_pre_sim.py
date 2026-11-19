"""Pre-Sim Commands live behind the Project-scoped execution seam."""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
from booley.runtime import resource_history
from booley.runtime.endpoint_execution import job_attempt, work_item_attempt_requeues
from booley.targets.domain import TargetHandle
from tests.oom_events_support import rising_oom_events
from tests.time_budget import hang_guard_s


def _project(root: Path, marker: str, command: str | None = None) -> TargetHandle:
    project = root / ".booley"
    project.mkdir(parents=True)
    command = command or f"printf {marker} > hook.txt"
    (project / "booley.toml").write_text(
        f'[flows.sim]\npre_run_commands = ["{command}"]\nrun_cwd = "."\n',
        encoding="utf-8",
    )
    return cast(
        TargetHandle,
        SimpleNamespace(project_root=root.resolve(), selector="sim"),
    )


def test_each_target_handle_resolves_its_own_project_configuration(tmp_path: Path) -> None:
    first_root = tmp_path / "current"
    second_root = tmp_path / "baseline"
    first = _project(first_root, "current")
    second = _project(second_root, "baseline")

    for handle in (first, second):
        outcome = run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=handle.project_root / "build",
            eda_tool="icarus",
            timeout_s=hang_guard_s(),
            resource_flags=(),
        )
        assert outcome is not None
        assert outcome.status == "passed"

    assert (first_root / "hook.txt").read_text(encoding="utf-8") == "current"
    assert (second_root / "hook.txt").read_text(encoding="utf-8") == "baseline"


def test_pre_sim_commands_record_their_peak_per_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each firing appends one ``sim_pre_run`` record keyed by the Target and the Job's flags."""
    handle = _project(tmp_path, "recorded")
    booley_dir = tmp_path / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    monkeypatch.delenv("BOOLEY_INTERNAL_DOCTOR_RUN", raising=False)

    for names in (("one",), ("two",)):
        outcome = run_pre_sim_commands(
            handle,
            test_names=names,
            build_root=tmp_path / "build",
            eda_tool="icarus",
            timeout_s=hang_guard_s(),
            resource_flags=("trace",),
        )
        assert outcome is not None and outcome.status == "passed"

    records = resource_history.load_records(booley_dir)
    assert len(records) == 2
    for record in records:
        assert record["key"] == {
            "target": "sim",
            "phase": "sim_pre_run",
            "flags": ["trace"],
            "test_run": "",
        }
        assert record["outcome"] == "exit_zero"
        assert record["wall_s"] is not None
        assert "peak_memory_mb" in record


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is POSIX")
@pytest.mark.parametrize(
    ("limit_reached", "outcome"), [(True, "oom_kill"), (False, "host_oom_kill")]
)
def test_an_oom_killed_pre_sim_command_is_its_work_items_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit_reached: bool, outcome: str
) -> None:
    """A kill at the Sandbox's limit requeues the work item; a host kill is named, not requeued."""
    handle = _project(tmp_path, "", command="kill -9 $$")
    booley_dir = tmp_path / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    monkeypatch.delenv("BOOLEY_INTERNAL_DOCTOR_RUN", raising=False)
    rising_oom_events(monkeypatch, limit_reached=limit_reached)

    with job_attempt(stop_on_oom=True, work_item=True) as attempt:
        evidence = run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build",
            eda_tool="icarus",
            timeout_s=hang_guard_s(),
            resource_flags=(),
        )
        assert evidence is not None and evidence.status == "failed"
        assert work_item_attempt_requeues(passed=False) is limit_reached

    assert attempt.host_oom_killed.is_set() is not limit_reached
    [record] = resource_history.load_records(booley_dir)
    assert record["outcome"] == outcome
