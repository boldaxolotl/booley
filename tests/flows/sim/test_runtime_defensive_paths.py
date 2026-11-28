from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows import artifact_durability
from booley.flows.sim.build_session import SimulationBuildSlotError
from booley.flows.sim.execution import pre_sim
from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
from booley.flows.sim.verilator_coverage_execution import VerilatorCoverageExecution
from booley.runtime import execution_recovery
from booley.runtime.execution_records import RUNTIME_EXECUTION_ENV, ExecutionId

_KILLED = 57
_LEADER = {"pid": 1, "identity_scope": "windows", "start_token": 1}


class _FakeHook(SimpleNamespace):
    """A spawned hook that, like a ``Popen``, is hashed by identity."""

    __hash__ = object.__hash__


class _Processes:
    def __init__(self) -> None:
        self.registered = []
        self.unregistered = []

    def register(self, process) -> None:
        self.registered.append(process)

    def unregister(self, process) -> None:
        self.unregistered.append(process)


def _fire_supervised(tmp_path: Path, monkeypatch, scope, popen):
    """Run one Pre-Sim Command through the public entry under *scope* with a fake spawn."""
    monkeypatch.setattr(pre_sim, "current_supervised_execution", lambda: scope)
    monkeypatch.setattr(pre_sim, "MeasuredPopen", popen)
    monkeypatch.setattr(pre_sim, "own_process_group", id)  # boundary: no OS process to own
    handle = SimpleNamespace(project_root=tmp_path.resolve(), selector="lite")
    return run_pre_sim_commands(
        handle,
        test_names=("smoke",),
        build_root=tmp_path / "build",
        eda_tool="icarus",
        timeout_s=1,
        resource_flags=(),
        commands=("true",),
    )


def test_supervised_pre_sim_process_propagates_identity_and_unregisters(
    tmp_path: Path, monkeypatch
) -> None:
    process = _FakeHook(
        pid=4_194_305,
        returncode=0,
        reaped_peak_bytes=None,
        communicate=lambda timeout=None: ("out", "err"),
    )
    processes = _Processes()
    scope = SimpleNamespace(
        cancelled=lambda: False, execution_id=ExecutionId("e" * 32), processes=processes
    )
    popen_envs: list[dict[str, str]] = []

    def popen(*_args, **kwargs):
        popen_envs.append(kwargs["env"])
        return process

    evidence = _fire_supervised(tmp_path, monkeypatch, scope, popen)

    # The child sees the caller's environment plus the supervising execution's identity.
    assert [env[RUNTIME_EXECUTION_ENV] for env in popen_envs] == ["e" * 32]
    assert [env["BOOLEY_BUILD_ROOT"] for env in popen_envs] == [str(tmp_path / "build")]
    assert evidence is not None and evidence.status == "passed"
    assert (evidence.stdout_tail, evidence.stderr_tail) == ("out", "err")
    assert processes.registered == processes.unregistered == [process]


def test_supervised_pre_sim_process_kills_after_communication_failure(
    tmp_path: Path, monkeypatch
) -> None:
    calls = 0

    def communicate(timeout=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("failed")
        return "", ""

    process = _FakeHook(
        pid=4_194_305, returncode=1, reaped_peak_bytes=None, communicate=communicate
    )
    processes = _Processes()
    scope = SimpleNamespace(cancelled=lambda: False, execution_id=None, processes=processes)
    killed: list[object] = []
    monkeypatch.setattr(pre_sim, "kill_process_tree", killed.append)
    with pytest.raises(RuntimeError, match="failed"):
        _fire_supervised(tmp_path, monkeypatch, scope, lambda *_args, **_kwargs: process)
    assert killed == [process]
    assert processes.unregistered == [process]


def test_coverage_execution_requires_authenticated_artifacts_and_containment(
    tmp_path: Path,
) -> None:
    # Private seam: an unprepared execution is unreachable through `prepare`, which sets both
    # fields; the guards behind `authenticated_image` need them unset or forged.
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
        execution.bind_authenticated_attempt(
            tmp_path / "bundle", tmp_path / "evidence", tmp_path / "run", lambda: None
        )


def test_recovery_short_circuits_unsafe_or_live_executions(monkeypatch, tmp_path: Path) -> None:
    execution_id = ExecutionId("e" * 32)
    paths = SimpleNamespace(record=tmp_path / "record")
    monkeypatch.setattr(execution_recovery, "execution_paths", lambda *_a, **_k: paths)
    monkeypatch.setattr(execution_recovery, "child_context_matches", lambda *_a: False)
    monkeypatch.setattr(execution_recovery, "is_older_child_context", lambda *_a: False)
    assert execution_recovery.recover_execution(execution_id, booley_dir=tmp_path) is False

    monkeypatch.setattr(execution_recovery, "child_context_matches", lambda *_a: True)
    monkeypatch.setattr(
        execution_recovery, "read_json", lambda _p: {"state": "terminal", "tree_terminal": True}
    )
    assert execution_recovery.recover_execution(execution_id, booley_dir=tmp_path) is True


@pytest.mark.parametrize("leader_live", [True, False])
def test_windows_recovery_waits_for_live_leader_then_publishes(
    monkeypatch, tmp_path: Path, leader_live: bool
) -> None:
    execution_id = ExecutionId("e" * 32)
    monkeypatch.setattr(execution_recovery, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        execution_recovery, "execution_paths", lambda *_a, **_k: SimpleNamespace(record=tmp_path)
    )
    monkeypatch.setattr(execution_recovery, "child_context_matches", lambda *_a: True)
    monkeypatch.setattr(execution_recovery, "request_cancellation", lambda *_a, **_k: None)
    monkeypatch.setattr(
        execution_recovery,
        "read_json",
        lambda _p: {"state": "running", "leader": _LEADER if leader_live else None},
    )
    monkeypatch.setattr(
        execution_recovery,
        "observe_process",
        lambda _identity: SimpleNamespace(state=execution_recovery.RUNNING),
    )
    changes: list[Any] = []
    monkeypatch.setattr(
        execution_recovery,
        "update_execution_record",
        lambda _paths, change: changes.append(change),
    )

    recovered = execution_recovery.recover_execution(execution_id, booley_dir=tmp_path)

    assert recovered is not leader_live
    if leader_live:
        assert changes == []
    else:
        (change,) = changes
        record = change(None)
        assert record["state"] == "terminal"
        assert record["terminal_cause"] == "orphan_recovered"


class _DyingWriter:
    """A writer that writes half of the first chunk and dies without any cleanup."""

    def __init__(self, writer) -> None:
        self._writer = writer

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def write(self, data: bytes) -> None:
        self._writer.write(data[: len(data) // 2])
        self._writer.flush()
        os._exit(_KILLED)

    def flush(self) -> None:
        self._writer.flush()


def _publish(kind: str, destination: Path, raw: bytes, *, mode: int = 0o600) -> None:
    if kind == "create":
        artifact_durability.durable_create(destination, raw, mode=mode)
        return
    source = destination.parent / f"source-{destination.name}"
    source.write_bytes(raw)
    artifact_durability.durable_copy(source, destination, mode=mode)


@pytest.mark.parametrize("kind", ["create", "copy"])
def test_durable_publication_refuses_an_existing_name(tmp_path: Path, kind: str) -> None:
    destination = tmp_path / "record"
    _publish(kind, destination, b"first", mode=0o400)

    with pytest.raises(FileExistsError):
        _publish(kind, destination, b"second")

    assert destination.read_bytes() == b"first"
    if os.name == "nt":  # Windows reports only the read-only bit
        assert not destination.stat().st_mode & stat.S_IWRITE
    else:
        assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    assert (
        sorted(path.name for path in tmp_path.iterdir() if path.name.endswith(".booley-staged"))
        == []
    )


@pytest.mark.parametrize("kind", ["create", "copy"])
def test_durable_publication_failure_leaves_no_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    destination = tmp_path / "record"

    def broken_fdopen(*_args, **_kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(artifact_durability.os, "fdopen", broken_fdopen)
    with pytest.raises(RuntimeError, match="write failed"):
        _publish(kind, destination, b"value")

    assert not destination.exists()
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".record.")] == []


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_durable_publication_killed_mid_write_publishes_nothing(tmp_path: Path) -> None:
    real_fdopen = os.fdopen
    for kind in ("create", "copy"):
        directory = tmp_path / kind
        directory.mkdir()
        destination = directory / "record"
        pid = os.fork()
        if pid == 0:  # the worker: die inside the write, as a SIGKILL would
            try:
                os.fdopen = lambda *args, **kwargs: _DyingWriter(real_fdopen(*args, **kwargs))  # type: ignore[assignment]
                _publish(kind, destination, b"0123456789")
            finally:
                os._exit(3)
        _, status = os.waitpid(pid, 0)

        assert os.waitstatus_to_exitcode(status) == _KILLED, kind
        assert not destination.exists(), kind
        staged = [path for path in directory.iterdir() if path.name.endswith(".booley-staged")]
        assert len(staged) == 1, kind
        artifact_durability.discard_staged_files(directory)
        assert not any(path.name.endswith(".booley-staged") for path in directory.iterdir()), kind


def test_durable_directory_accepts_concurrent_creation(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "shared"
    original_mkdir = Path.mkdir

    def concurrent_mkdir(path: Path, *args, **kwargs) -> None:
        if path == destination and not path.exists():
            original_mkdir(path)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", concurrent_mkdir)

    artifact_durability.durable_directory(destination)

    assert destination.is_dir()
