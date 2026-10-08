"""Active Simulation lifecycle observations at the persisted execution seam."""

import json
from unittest.mock import MagicMock, patch

from booley.flows.base import SubprocessResult
from booley.flows.progress_lifecycle import progress_document
from booley.flows.sim.adapter_transport import AdapterResult, write_adapter_result
from booley.flows.sim.execution import SimulationExecution, SimulationOptions
from tests.flows.sim.test_execution_engine import (
    _compile_surface_patch,
    _handle,
    _inspection,
    _prepared,
)


def test_authenticated_build_publishes_execution_before_blocking_invoker(tmp_path, monkeypatch):
    from booley.flows.sim.live_progress import (
        LiveProgressSink,
        install_progress,
        publish_checkpoint,
    )

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    handle = _handle(tmp_path)
    prepared = _prepared(handle, cocotb=False)
    path = tmp_path / "reports/sim/1/progress.json"
    sink = LiveProgressSink(path, tmp_path, "current-run")
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    session = MagicMock()
    session.new_generation.return_value = prepared.work_root
    session.capture_inputs.return_value = {}
    session.try_reuse.return_value = None
    execution._build_session = session
    observations = []
    box = {}

    def invoke(command, *, timeout):
        del timeout
        document = json.loads(path.read_text())
        observations.append(document)
        assert document["complete"] is False
        assert document["pending_targets"] == ["sim"]
        stage = document["active"][0]
        if "BOOLEY_BUILD_STAGE" in command[-1]:
            assert stage["stage"] == "building"
            return SubprocessResult(returncode=0, stdout="BOOLEY_BUILD_STAGE token=abc123 rc=0\n")
        assert document["phase"] == "running"
        assert stage["stage"] == "executing"
        assert stage["log"]["live"] is True
        assert (tmp_path / stage["log"]["path"]).is_file()
        write_adapter_result(
            box["attempt"].identity,
            AdapterResult(passed=True, inconclusive=False, sva_errors=0, tests=()),
        )
        return SubprocessResult(returncode=0)

    execution._invoke = invoke
    with (
        install_progress(sink),
        _compile_surface_patch(handle),
        patch(
            "booley.flows.sim.execution.engine.TargetCatalog.build",
            return_value=_inspection(cocotb=False),
        ),
        patch("booley.flows.sim.execution.engine.prepare_simulation_build", return_value=prepared),
        patch("booley.flows.sim.execution.engine.new_attempt_token", return_value="abc123"),
    ):
        publish_checkpoint(
            path,
            progress_document(
                flow="sim",
                run_id="current-run",
                phase="starting",
                targets=["sim"],
                completed_targets=[],
                detail={},
                extra={"mode": "simulate"},
            ),
        )
        attempt = execution._prepare_attempt(handle, ())
        box["attempt"] = attempt
        result = execution._execute_fresh_adapter(handle, attempt)
    assert result.result is not None
    assert len(observations) == 2


def _checkpoint(path, *, completed=(), coverage=False, phase="starting"):
    from booley.flows.sim.live_progress import publish_checkpoint

    publish_checkpoint(
        path,
        progress_document(
            flow="sim",
            run_id="current-run",
            phase=phase,
            targets=["active", "sim"],
            completed_targets=completed,
            detail={"active": {"verdict": "pass"}} if completed else {},
            extra={"mode": "simulate", "coverage": coverage},
        ),
    )


def test_concurrent_attempts_retain_results_and_close_before_late_event(tmp_path, monkeypatch):  # noqa: PLR0915 — one bounded interleaving verifies preserved checkpoints and terminal closure
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    from booley.flows.sim.live_progress import (
        LiveProgressSink,
        attempt_scope,
        install_progress,
        observe_stage,
    )

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    path = tmp_path / "progress.json"
    sink = LiveProgressSink(path, tmp_path, "current-run")
    both = Barrier(3, timeout=5)
    first_release, second_release = Event(), Event()
    writes = []
    from booley.flows.sim import live_progress

    original = live_progress.write_progress_json

    def publish(*args, **kwargs):
        writes.append(kwargs.get("lock_timeout_s"))
        original(*args, **kwargs)

    monkeypatch.setattr(live_progress, "write_progress_json", publish)

    def worker(name, release):
        with install_progress(sink), attempt_scope(name, attempt_id=name):
            observe_stage(name, "executing")
            both.wait()
            assert release.wait(5)
            observe_stage(name, "building")

    with install_progress(sink), ThreadPoolExecutor(max_workers=2) as pool:
        _checkpoint(path, completed=["active"], coverage=True)
        first = pool.submit(worker, "one", first_release)
        second = pool.submit(worker, "two", second_release)
        try:
            both.wait()
            _checkpoint(path, completed=["active"], coverage=True)
            doc = json.loads(path.read_text())
            assert doc["phase"] == "running" and len(doc["active"]) == 2
            assert doc["coverage"] is True and doc["completed_targets"] == ["active"]
            assert doc["detail"]["active"] == {"verdict": "pass"}
            first_release.set()
            first.result(timeout=5)
            doc = json.loads(path.read_text())
            assert doc["phase"] == "running" and len(doc["active"]) == 1
            _checkpoint(path, completed=["active"], coverage=True, phase="aborted")
            second_release.set()
            second.result(timeout=5)
            final = json.loads(path.read_text())
            assert final["phase"] == "aborted" and "active" not in final
            assert len(writes) <= 9
        finally:
            first_release.set()
            second_release.set()


def test_terminal_retry_remains_possible_after_event_admission_closes(tmp_path, monkeypatch):
    import pytest

    from booley.flows.progress_lifecycle import ProgressLifecycle, ProgressPublicationError
    from booley.flows.sim import live_progress
    from booley.flows.sim.live_progress import LiveProgressSink, install_progress, observe_stage

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    path = tmp_path / "progress.json"
    original = live_progress.write_progress_json

    def publish(path, document, **kwargs):
        if document["phase"] == "complete":
            observe_stage("sim", "executing")
            raise OSError("terminal disk failure")
        original(path, document, **kwargs)

    monkeypatch.setattr(live_progress, "write_progress_json", publish)
    with install_progress(LiveProgressSink(path, tmp_path, "current-run")):
        _checkpoint(path)
        with (
            pytest.raises(ProgressPublicationError),
            ProgressLifecycle(lambda phase: _checkpoint(path, phase=phase)) as lifecycle,
        ):
            lifecycle.complete()
        observe_stage("sim", "executing")
    assert json.loads(path.read_text())["phase"] == "aborted"
    assert "active" not in json.loads(path.read_text())


def test_advisory_lock_failures_retry_without_changing_checkpoint_authority(tmp_path, monkeypatch):
    from booley.flows.sim import live_progress
    from booley.flows.sim.live_progress import LiveProgressSink, install_progress, observe_stage

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    path = tmp_path / "progress.json"
    sink = LiveProgressSink(path, tmp_path, "current-run")
    original = live_progress.write_progress_json
    errors = iter([TimeoutError("contended"), ValueError("nonregular lock"), OSError("full")])

    def publish(path, document, **kwargs):
        if kwargs.get("lock_timeout_s") == 0.05:
            error = next(errors, None)
            if error is not None:
                raise error
        original(path, document, **kwargs)

    monkeypatch.setattr(live_progress, "write_progress_json", publish)
    with install_progress(sink):
        _checkpoint(path, completed=["active"])
        for stage in ("preparing", "building", "executing", "postprocessing"):
            observe_stage("sim", stage)
        doc = json.loads(path.read_text())
        assert doc["phase"] == "running"
        assert doc["completed_targets"] == ["active"]
        assert doc["observation_health"]["error"] == "OSError: full"
        _checkpoint(path, completed=["active"], phase="complete")
    assert json.loads(path.read_text())["complete"] is True


def test_active_pointer_requires_exact_owned_root_token_and_header(tmp_path, monkeypatch):
    from booley.flows.sim.live_progress import (
        LiveProgressSink,
        attempt_scope,
        install_progress,
        observe_stage,
    )

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    path = tmp_path / "progress.json"
    root = tmp_path / "current"
    other = tmp_path / "older"
    root.mkdir()
    other.mkdir()
    sink = LiveProgressSink(path, tmp_path, "current-run")
    with install_progress(sink), attempt_scope("sim"):
        _checkpoint(path)
        observe_stage(
            "sim", "preparing", evidence_root=root, attempt_token="owned", initialize_log=True
        )
        observe_stage("sim", "executing", evidence_root=root, attempt_token="owned")
        assert json.loads(path.read_text())["active"][0]["log"]["live"] is True
        (other / "run.log").write_text((root / "run.log").read_text())
        for invalid_root, token in ((other, "owned"), (root, "stale")):
            observe_stage("sim", "executing", evidence_root=invalid_root, attempt_token=token)
            assert "log" not in json.loads(path.read_text())["active"][0]
        (root / "run.log").write_text("[BOOLEY RUN_LOG] run=foreign flow=sim target=sim\n")
        observe_stage("sim", "executing", evidence_root=root, attempt_token="owned")
        assert "log" not in json.loads(path.read_text())["active"][0]
        (root / "run.log").unlink()
        observe_stage("sim", "executing", evidence_root=root, attempt_token="owned")
        assert "log" not in json.loads(path.read_text())["active"][0]
        protected = tmp_path / "protected"
        protected.write_text("unchanged")
        (root / "run.log").symlink_to(protected)
        observe_stage(
            "sim", "executing", evidence_root=root, attempt_token="owned", initialize_log=True
        )
        assert protected.read_text() == "unchanged"
        assert "log" not in json.loads(path.read_text())["active"][0]


def test_ephemeral_baseline_is_removed_before_cleanup_and_never_completes_candidate(
    tmp_path, monkeypatch
):
    from booley.flows.sim.live_progress import (
        LiveProgressSink,
        attempt_scope,
        install_progress,
        observe_stage,
    )

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    project = tmp_path / "project"
    project.mkdir()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    path = project / "progress.json"
    with install_progress(LiveProgressSink(path, project, "current-run")):
        _checkpoint(path)
        with attempt_scope(
            "older_target", role="baseline", revision="abc", ephemeral_root=baseline
        ):
            observe_stage(
                "older_target",
                "executing",
                evidence_root=baseline,
                attempt_token="baseline",
                initialize_log=True,
            )
            doc = json.loads(path.read_text())
            assert doc["phase"] == "baseline"
            assert doc["active"][0]["revision"] == "abc"
            assert doc["active"][0]["log"]["path"] == str(baseline / "run.log")
            assert doc["completed_targets"] == []
        assert "active" not in json.loads(path.read_text())


def test_nonregular_progress_lock_is_observational_and_recovers(tmp_path, monkeypatch):
    from booley.flows.sim.live_progress import LiveProgressSink, install_progress, observe_stage

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    path = tmp_path / "progress.json"
    sink = LiveProgressSink(path, tmp_path, "current-run")
    with install_progress(sink):
        _checkpoint(path)
        lock = tmp_path / ".progress.lock"
        lock.unlink()
        lock.mkdir()
        observe_stage("sim", "executing")
        assert sink.error is not None
        assert json.loads(path.read_text())["phase"] == "starting"
        lock.rmdir()
        observe_stage("sim", "postprocessing")
        assert json.loads(path.read_text())["phase"] == "running"


def test_active_writer_uses_short_lock_timeout_and_mandatory_keeps_default(tmp_path, monkeypatch):
    from booley.flows import progress_lifecycle
    from booley.flows.sim.live_progress import LiveProgressSink, install_progress, observe_stage

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    timeouts = []

    def wait(handle, *, timeout_s):
        del handle
        timeouts.append(timeout_s)
        if timeout_s == 0.05:
            raise TimeoutError("busy")

    monkeypatch.setattr(progress_lifecycle, "wait_for_file_lock", wait)
    monkeypatch.setattr(progress_lifecycle, "release_file_lock", lambda _: None)
    path = tmp_path / "progress.json"
    sink = LiveProgressSink(path, tmp_path, "current-run")
    with install_progress(sink):
        _checkpoint(path)
        observe_stage("sim", "executing")
        _checkpoint(path, phase="aborted")
    assert timeouts == [5, 0.05, 5]
    assert json.loads(path.read_text())["phase"] == "aborted"


def test_reap_and_coverage_supersede_remove_active_pointers(tmp_path):
    from booley.flows.progress_lifecycle import (
        repair_progress_after_reap,
        supersede_progress,
        write_progress_json,
    )

    root = tmp_path / "reports"
    path = root / "sim/1/progress.json"
    document = progress_document(
        flow="sim",
        run_id="killed",
        phase="running",
        targets=["sim"],
        completed_targets=[],
        detail={},
        extra={
            "coverage": True,
            "active": [{"stage": "executing", "log": {"path": "old/run.log", "live": True}}],
        },
    )
    write_progress_json(path, document)
    assert not repair_progress_after_reap([root], "sim", "other-run")
    assert repair_progress_after_reap([root], "sim", "killed")
    repaired = json.loads(path.read_text())
    assert repaired["phase"] == "aborted" and "active" not in repaired
    write_progress_json(path, document)
    assert supersede_progress(path, new_invocation=2, new_run_id="new")
    superseded = json.loads(path.read_text())
    assert superseded["coverage"] is True and "active" not in superseded
    assert superseded["phase"] == "superseded"


def test_coverage_build_and_launch_stages_survive_observation_failure(tmp_path, monkeypatch):
    from booley.flows.sim import live_progress
    from booley.flows.sim.live_progress import LiveProgressSink, attempt_scope, install_progress
    from tests.flows.sim.test_verilator_coverage_execution import (
        _build_coverage,
        _execution_fixture,
        _run_request,
    )

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    execution, target, raw, _captured = _execution_fixture(tmp_path, monkeypatch)
    path = tmp_path / "progress.json"
    original_invoke = execution._invoke
    seen = []

    def invoke(command, *, timeout):
        if command != ["verilator", "--version"]:
            document = json.loads(path.read_text())
            active = document["active"][0]
            seen.append(active["stage"])
            assert active["log"]["live"] is True
            assert document["complete"] is False and document["coverage"] is True
        return original_invoke(command, timeout=timeout)

    execution._invoke = invoke
    sink = LiveProgressSink(path, tmp_path, "current-run")
    with install_progress(sink), attempt_scope("sim", identity=target.identity):
        _checkpoint(path, coverage=True)
        build = _build_coverage(execution, target)
        run = execution.run(_run_request(target, raw))
        original_write = live_progress.write_progress_json

        def failing_write(path, document, **kwargs):
            if kwargs.get("lock_timeout_s") == 0.05:
                raise ValueError("nonregular observational lock")
            original_write(path, document, **kwargs)

        monkeypatch.setattr(live_progress, "write_progress_json", failing_write)
        execution._invoke = original_invoke
        retry = execution.run(_run_request(target, raw))
        _checkpoint(path, phase="complete", coverage=True)
    assert build.success is True
    assert run == retry
    assert run.verdict == "pass" and raw.exists()
    assert seen == ["building", "executing"]
    assert sink.error is not None


def test_failed_mandatory_checkpoint_does_not_become_worker_base(tmp_path, monkeypatch):
    import pytest

    from booley.flows.sim import live_progress
    from booley.flows.sim.live_progress import LiveProgressSink, install_progress, observe_stage

    monkeypatch.setenv("BOOLEY_RUN_ID", "current-run")
    path = tmp_path / "progress.json"
    original = live_progress.write_progress_json
    with install_progress(LiveProgressSink(path, tmp_path, "current-run")):
        _checkpoint(path)

        def publish(path, document, **kwargs):
            if not kwargs:
                raise OSError("required checkpoint unavailable")
            original(path, document, **kwargs)

        monkeypatch.setattr(live_progress, "write_progress_json", publish)
        with pytest.raises(OSError):
            _checkpoint(path, completed=["active"])
        observe_stage("sim", "executing")
        document = json.loads(path.read_text())
        assert document["completed_targets"] == []
        assert document["detail"] == {}


def test_hdl_backend_exceptions_are_visible_before_child_completion(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from booley.flows.run_log import begin_run_log
    from booley.flows.sim.backends import shared, verilator

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    log = begin_run_log(evidence, flow="sim", target="sim", run="hdl-live")
    observed = Event()
    children = []
    popen = subprocess.Popen
    observe = shared.RunLogProgress.observe

    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs, stdin=subprocess.PIPE)
        children.append(child)
        return child

    def observe_line(progress, line):
        observe(progress, line)
        observed.set()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(shared, "RUN_LOG_PROGRESS_INTERVAL_S", 0)
    monkeypatch.setattr(shared.RunLogProgress, "observe", observe_line)
    command = [
        sys.executable,
        "-u",
        "-c",
        "import sys; print('CPU exception handler'); sys.stdin.readline()",
    ]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            verilator._stream_output,
            command,
            tmp_path,
            os.environ.copy(),
            5,
            None,
            None,
            work_dir=evidence,
        )
        try:
            assert observed.wait(5)
            assert not future.done()
            assert "CPU exception handler" in log.read_text()
            assert "run=hdl-live" in log.read_text()
        finally:
            if children:
                children[0].stdin.write("release\n")
                children[0].stdin.flush()
                children[0].stdin.close()
            future.result(timeout=6)
