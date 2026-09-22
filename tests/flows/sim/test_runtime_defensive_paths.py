from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim import campaign_durability
from booley.flows.sim.build_session import SimulationBuildSlotError
from booley.flows.sim.execution import pre_sim
from booley.flows.sim.verilator_coverage_execution import VerilatorCoverageExecution
from booley.runtime import execution_recovery, supervised_execution
from booley.runtime.execution_records import ExecutionId


class _Processes:
    def __init__(self) -> None:
        self.registered = []
        self.unregistered = []

    def register(self, process) -> None:
        self.registered.append(process)

    def unregister(self, process) -> None:
        self.unregistered.append(process)


def test_supervised_pre_sim_process_propagates_identity_and_unregisters(monkeypatch) -> None:
    process = SimpleNamespace(returncode=0, communicate=lambda timeout=None: ("out", "err"))
    processes = _Processes()
    scope = SimpleNamespace(
        cancelled=lambda: False,
        execution_id=ExecutionId("e" * 32),
        processes=processes,
    )
    monkeypatch.setattr(pre_sim, "current_supervised_execution", lambda: scope)
    monkeypatch.setattr(pre_sim.subprocess, "Popen", lambda *_args, **kwargs: process)
    result = pre_sim._run_pre_sim_process(["tool"], cwd=Path(), env={}, timeout=1)
    assert (result.args, result.returncode, result.stdout, result.stderr) == (
        ["tool"],
        0,
        "out",
        "err",
    )
    assert processes.registered == processes.unregistered == [process]


def test_supervised_pre_sim_process_honors_cancellation(monkeypatch) -> None:
    scope = SimpleNamespace(cancelled=lambda: True, execution_id=None, processes=_Processes())
    monkeypatch.setattr(pre_sim, "current_supervised_execution", lambda: scope)
    result = pre_sim._run_pre_sim_process(["tool"], cwd=Path(), env={}, timeout=1)
    assert result.returncode == 125


def test_supervised_pre_sim_process_kills_after_communication_failure(monkeypatch) -> None:
    calls = 0

    def communicate(timeout=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("failed")
        return "", ""

    process = SimpleNamespace(returncode=1, communicate=communicate)
    processes = _Processes()
    scope = SimpleNamespace(cancelled=lambda: False, execution_id=None, processes=processes)
    monkeypatch.setattr(pre_sim, "current_supervised_execution", lambda: scope)
    monkeypatch.setattr(pre_sim.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(pre_sim, "kill_process_tree", lambda selected: None)
    with pytest.raises(RuntimeError, match="failed"):
        pre_sim._run_pre_sim_process(["tool"], cwd=Path(), env={}, timeout=1)
    assert processes.unregistered == [process]


def test_supervised_command_heartbeat_and_wrapper(monkeypatch, tmp_path: Path) -> None:
    generations = []
    scope = SimpleNamespace(execution_id=ExecutionId("e" * 32), project_data=tmp_path)
    monkeypatch.setattr(
        supervised_execution,
        "execution_paths",
        lambda *_args, **_kwargs: SimpleNamespace(record=tmp_path / "record"),
    )
    monkeypatch.setattr(
        supervised_execution,
        "write_attachment_heartbeat",
        lambda _paths, generation: generations.append(generation),
    )
    wrapped, environment, heartbeat = supervised_execution.wrap_supervised_command(
        ["tool", "arg"], scope
    )
    assert wrapped[-2:] == ["tool", "arg"]
    assert environment[supervised_execution.RUNTIME_EXECUTION_ENV] == "e" * 32
    heartbeat.start()
    heartbeat.stop()
    assert generations == [1]


def test_coverage_execution_requires_authenticated_artifacts_and_containment(
    tmp_path: Path,
) -> None:
    execution = object.__new__(VerilatorCoverageExecution)
    execution._prepared = None
    execution._artifact_paths = ()
    with pytest.raises(SimulationBuildSlotError, match="not authorized"):
        execution.authenticated_image()

    prepared = SimpleNamespace(build_root=tmp_path / "build")
    execution._prepared = prepared
    with pytest.raises(SimulationBuildSlotError, match="no artifacts"):
        execution.authenticated_image()

    execution._artifact_paths = (tmp_path / "outside",)
    with pytest.raises(SimulationBuildSlotError, match="escapes"):
        execution.bind_authenticated_attempt(tmp_path / "snapshot", tmp_path / "run")


def test_recovery_short_circuits_unsafe_or_live_executions(monkeypatch, tmp_path: Path) -> None:
    execution_id = ExecutionId("e" * 32)
    paths = SimpleNamespace(record=tmp_path / "record")
    monkeypatch.setattr(execution_recovery, "execution_paths", lambda *_a, **_k: paths)
    monkeypatch.setattr(execution_recovery, "child_context_matches", lambda *_a: False)
    assert execution_recovery.recover_execution(execution_id, project_dir=tmp_path) is False

    monkeypatch.setattr(execution_recovery, "child_context_matches", lambda *_a: True)
    monkeypatch.setattr(
        execution_recovery, "read_json", lambda _p: {"state": "terminal", "tree_terminal": True}
    )
    assert execution_recovery.recover_execution(execution_id, project_dir=tmp_path) is True


def test_windows_recovery_waits_for_live_leader_then_publishes(monkeypatch) -> None:
    execution_id = ExecutionId("e" * 32)
    identity = object()
    monkeypatch.setattr(
        execution_recovery.ProcessIdentity,
        "from_payload",
        lambda _payload: identity,
    )
    monkeypatch.setattr(
        execution_recovery,
        "observe_process",
        lambda _identity: SimpleNamespace(state=execution_recovery.RUNNING),
    )
    assert not execution_recovery._recover_windows_execution(
        execution_id, {"leader": {}}, project_dir=None
    )

    published = []
    monkeypatch.setattr(execution_recovery.ProcessIdentity, "from_payload", lambda _payload: None)
    monkeypatch.setattr(
        execution_recovery,
        "_publish_recovered_terminal",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )
    assert execution_recovery._recover_windows_execution(execution_id, None, project_dir=None)
    assert published


def test_durable_publication_removes_partial_destination_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    destination = tmp_path / "record"

    class BrokenWriter:
        def __enter__(self):
            destination.write_bytes(b"partial")
            raise RuntimeError("write failed")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(campaign_durability.os, "open", lambda *_args: 99)
    monkeypatch.setattr(campaign_durability.os, "close", lambda _fd: None)
    monkeypatch.setattr(campaign_durability.os, "fdopen", lambda *_args, **_kwargs: BrokenWriter())
    with pytest.raises(RuntimeError, match="write failed"):
        campaign_durability.durable_create(destination, b"value")
    assert not destination.exists()
