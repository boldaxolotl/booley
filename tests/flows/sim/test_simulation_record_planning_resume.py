"""Planning and resume behavior of Simulation Records."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from booley.flows.endpoint_admission import AdmissionGate
from booley.flows.sim.config import parse_run_cwd_template, resolve_pre_sim_build_access
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record.coordinator import (
    ResumeSimulationRecordPreviewRequest,
    ResumeSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordCancellationError,
    SimulationRecordPolicy,
    WorkExecutionRequest,
)
from booley.flows.sim.record.model import (
    create_simulation_record_plan,
)
from booley.flows.sim.record.planning import manifest_digest
from booley.flows.sim.record.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
)
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.sim.request import SimRequest
from booley.runtime.endpoint_execution import EXIT_CANCELLED, EXIT_ERROR
from booley.runtime.execution_records import (
    ExecutionId,
)
from booley.runtime.job_slots import (
    CLASS_MEMORY,
)
from tests.flows.sim.simulation_record_test_support import (
    _admission,
    _interrupted_simulation_record,
    _manifest_for,
    _NoEdaExecutor,
    _SimulationRecordAdmissionEndpoint,
)
from tests.flows.sim.test_simulation_record_resume_diagnostics import (
    _interrupted_simulation_record as _interrupted_project_record,
)
from tests.time_budget import scaled


def test_run_directory_parser_rejects_an_unclosed_format_field() -> None:
    with pytest.raises(ValueError):
        parse_run_cwd_template("run/{")


def test_pre_sim_build_access_is_strict(tmp_path: Path) -> None:
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley/booley.toml").write_text(
        '[flows.sim]\npre_sim_build_access = "unsafe"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="pre_sim_build_access"):
        resolve_pre_sim_build_access(tmp_path)


def _child_parent_ids(project: Path) -> set[str]:
    """Parent execution IDs named by the lane context sidecars under the Booley Directory."""
    contexts = (project / ".booley/.runtime/executions").glob("*/context.json")
    return {json.loads(path.read_text())["parent_execution_id"] for path in contexts}


@pytest.mark.parametrize("runtime_execution_id", ["b" * 32, None])
def test_interrupted_managed_simulation_record_exact_resume_preserves_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_execution_id: str | None,
) -> None:
    case = _interrupted_simulation_record(tmp_path, monkeypatch, runtime_execution_id)
    assert not case.store.summary_path.exists()
    assert len(case.state.completion_order) == 3
    parent_ids = _child_parent_ids(case.project)
    # Private seam: _invocation_id is the invocation id this stub admission endpoint carries;
    # it is a class attribute of the stub with no public accessor, and the test asserts the
    # lanes' parent id is the runtime execution id rather than that value.
    if runtime_execution_id is not None:
        assert parent_ids == {runtime_execution_id}
    assert (
        len(parent_ids) == 1
        and _SimulationRecordAdmissionEndpoint._invocation_id not in parent_ids
    )
    ExecutionId(next(iter(parent_ids)))

    case.fault_enabled[0] = False
    summaries, outcomes = _resume_simulation_record_twice(case, runtime_execution_id)
    assert summaries[0] == summaries[1]
    assert all(outcome.complete for outcome in outcomes)
    assert len(case.state.completion_order) == 3
    assert case.store.load_manifest().document["origin"]["execution_id"] == (
        runtime_execution_id or ""
    )
    assert case.slots.snapshot(CLASS_MEMORY) == ([], [])


def _resume_simulation_record_twice(case, runtime_execution_id):
    request_args = (
        case.validated,
        case.plan,
        case.project,
        case.invocation.parent,
        SimulationRecordPolicy(),
        case.invocation,
    )
    summaries = []
    outcomes = []
    for _ in range(2):
        with AdmissionGate(
            _SimulationRecordAdmissionEndpoint(case.slots, runtime_execution_id)  # type: ignore[arg-type]
        ).enter() as admission:
            outcomes.append(
                case.simulation_record.run(
                    ResumeSimulationRecordRunRequest(*request_args, admission)
                )
            )
        summaries.append(case.store.summary_path.read_bytes())
    return summaries, outcomes


def _seed_authenticated_states(
    store: SimulationRecordStore, project: Path, executor: _NoEdaExecutor
) -> None:
    manifest = store.load_manifest()
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    for index, item in enumerate(items[:2]):
        attempt_id = str(uuid.uuid4())
        item_id = cast(str, item["work_item_id"])
        digest = item_id.rsplit(":", 1)[-1]
        item_root = store.root / "work-items" / f"{cast(int, item['ordinal']) + 1:04d}-{digest}"
        directory = item_root / "attempts" / f"0001-{attempt_id}"
        directory.mkdir(parents=True)
        request = WorkExecutionRequest(
            store,
            manifest,
            item,
            attempt_id,
            1,
            directory,
            1,
            SimulationRecordPolicy(),
            _admission(),
            project,
        )
        attempt, build_attempt, build_result, result = executor.documents(request, validate=False)
        executor.calls += 1
        build_root = directory / "private-build"
        build_root.mkdir()
        (directory / "attempt.json").write_bytes(attempt.canonical_bytes())
        (build_root / "build-attempt.json").write_bytes(build_attempt.canonical_bytes())
        (build_root / "build-result.json").write_bytes(build_result.canonical_bytes())
        if index < 1:
            (item_root / "result.json").write_bytes(result.canonical_bytes())


def _resume_request(store: SimulationRecordStore, project: Path):
    manifest = store.load_manifest()
    validated = ValidatedResumeManifest(
        ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest)),
        (),
        (),
    )
    return ResumeSimulationRecordPreviewRequest(
        validated=validated,
        current_plan=create_simulation_record_plan(manifest),
        project_root=project,
        report_root=None,
        policy=SimulationRecordPolicy(),
    )


# Windows CI: 3x the slowest observed duration (tests/timeout_headroom.py).
@pytest.mark.timeout(90)
def test_maximum_simulation_record_previews_authenticated_mixed_resume_without_eda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    manifest = _manifest_for(tuple(f"case_{index:04d}" for index in range(1_000)))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    invocation = tmp_path / "reports" / "sim" / "1"
    store = SimulationRecordStore(invocation / "targets" / "sim" / "record")
    store.publish_manifest(manifest)
    executor = _NoEdaExecutor()
    _seed_authenticated_states(store, project, executor)
    recovery = store.scan()
    assert (len(recovery.complete), len(recovery.interrupted), len(recovery.pending)) == (
        1,
        1,
        998,
    )
    request = _resume_request(store, project)
    status = SimulationRecord().inspect_resume(request.validated)

    started = time.monotonic()
    first = store.regenerate_summary()
    preview = SimulationRecord().preview(request)
    elapsed = time.monotonic() - started
    first_bytes = store.summary_path.read_bytes()
    second = store.regenerate_summary()

    assert executor.calls == 2
    assert status.manifest_path == store.manifest_path
    assert status.manifest_sha256 == store.manifest_sha256()
    assert status.completed == recovery.complete
    assert status.interrupted == recovery.interrupted
    assert status.pending == recovery.pending
    assert (
        len(preview.recovery.completed),
        len(preview.recovery.interrupted),
        len(preview.recovery.pending),
    ) == (
        1,
        1,
        998,
    )
    assert first == second
    assert store.summary_path.read_bytes() == first_bytes
    assert elapsed < scaled(15)


@pytest.mark.parametrize("cancelled", [False, True])
def test_resume_endpoint_refreshes_recovery_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancelled: bool,
) -> None:
    reports, manifest, _executions = _interrupted_project_record(tmp_path, monkeypatch)
    store = SimulationRecordStore(manifest.parent)
    pending_before = store.scan().pending
    assert pending_before

    def fail_after_attempts(_executor: object, _request: object) -> None:
        # Every still-pending item gets an attempt directory before the run fails.
        for item_id in store.scan().pending:
            store.allocate_attempt_directory(item_id, str(uuid.uuid4()))
        if cancelled:
            raise SimulationRecordCancellationError("cancelled")
        raise OSError("failed")

    monkeypatch.setattr(
        "booley.flows.sim.flow_resume.OrdinaryHdlSerialExecutor.execute", fail_after_attempts
    )

    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )

    # The detail shows what the failed run left behind, not the pre-run view.
    assert result.exit_code == (EXIT_CANCELLED if cancelled else EXIT_ERROR)
    assert result.outcome.detail["completed"] == []
    assert result.outcome.detail["pending"] == []
    assert result.outcome.detail["interrupted"] == [
        item["work_item_id"] for item in store.load_manifest().document["work_items"]
    ]


def test_resume_execution_uses_a_fresh_scan_after_preview(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    first_invocation = tmp_path / "reports" / "sim" / "1"
    store = SimulationRecordStore(first_invocation / "targets" / "sim" / "record")
    store.publish_manifest(_manifest_for(("smoke",)))
    preview_request = _resume_request(store, project)

    preview = SimulationRecord().preview(preview_request)
    assert preview.recovery.pending

    first_executor = _NoEdaExecutor()
    first_outcome = SimulationRecord(first_executor).run(
        ResumeSimulationRecordRunRequest(
            preview_request.validated,
            preview_request.current_plan,
            project,
            None,
            SimulationRecordPolicy(),
            tmp_path / "reports" / "sim" / "2",
            _admission(),
        )
    )
    assert first_executor.calls == 1
    assert first_outcome.recovery.pending == ()

    manifest_bytes = store.manifest_path.read_bytes()
    second_executor = _NoEdaExecutor()
    second_outcome = SimulationRecord(second_executor).run(
        ResumeSimulationRecordRunRequest(
            preview_request.validated,
            preview_request.current_plan,
            project,
            None,
            SimulationRecordPolicy(timeout_seconds=42, build_timeout_seconds=17),
            tmp_path / "reports" / "sim" / "3",
            _admission(),
        )
    )

    assert second_executor.calls == 0
    assert second_outcome.recovery.completed == first_outcome.recovery.completed

    assert store.manifest_path.read_bytes() == manifest_bytes


def test_resume_inspection_scans_the_validated_manifest_without_rereading(
    tmp_path: Path,
) -> None:
    store = SimulationRecordStore(
        tmp_path / "reports" / "sim" / "1" / "targets" / "sim" / "record"
    )
    store.publish_manifest(_manifest_for(("smoke",)))
    validated = _resume_request(store, tmp_path).validated
    store.manifest_path.unlink()

    status = SimulationRecord().inspect_resume(validated)

    assert status.manifest_sha256 == validated.sha256
