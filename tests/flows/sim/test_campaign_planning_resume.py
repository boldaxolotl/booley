"""Planning and resume behavior of Simulation Campaigns."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.endpoint_admission import AdmissionGate
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignPreviewRequest,
    ResumeCampaignPreviewRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
    SimulationCampaignCancellationError,
    WorkExecutionRequest,
)
from booley.flows.sim.campaign.model import (
    create_simulation_campaign_plan,
)
from booley.flows.sim.campaign.planning import manifest_digest
from booley.flows.sim.campaign.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
)
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.config import parse_run_cwd_template, resolve_pre_sim_build_access
from booley.flows.sim.flow import SimulateFlow
from booley.runtime.endpoint_execution import EXIT_CANCELLED, EXIT_ERROR
from booley.runtime.execution_records import (
    ExecutionId,
)
from booley.runtime.job_slots import (
    CLASS_MEMORY,
)
from tests.flows.sim.campaign_test_support import (
    _admission,
    _CampaignAdmissionEndpoint,
    _interrupted_campaign,
    _manifest_for,
    _NoEdaExecutor,
)


def test_run_directory_parser_rejects_an_unclosed_format_field() -> None:
    with pytest.raises(ValueError):
        parse_run_cwd_template("run/{")


def test_pre_sim_build_access_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "booley.flows.sim.config._sim_config",
        lambda _root: {"pre_sim_build_access": "unsafe"},
    )
    with pytest.raises(ValueError, match="pre_sim_build_access"):
        resolve_pre_sim_build_access(Path())


def _child_parent_ids(project: Path) -> set[str]:
    """Parent execution IDs named by the lane context sidecars under the Booley Directory."""
    contexts = (project / ".booley/.runtime/executions").glob("*/context.json")
    return {json.loads(path.read_text())["parent_execution_id"] for path in contexts}


@pytest.mark.parametrize("runtime_execution_id", ["b" * 32, None])
def test_interrupted_managed_campaign_exact_resume_preserves_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_execution_id: str | None,
) -> None:
    case = _interrupted_campaign(tmp_path, monkeypatch, runtime_execution_id)
    assert not case.store.summary_path.exists()
    assert len(case.state.completion_order) == 3
    parent_ids = _child_parent_ids(case.project)
    if runtime_execution_id is not None:
        assert parent_ids == {runtime_execution_id}
    assert len(parent_ids) == 1 and _CampaignAdmissionEndpoint._invocation_id not in parent_ids
    ExecutionId(next(iter(parent_ids)))

    case.fault_enabled[0] = False
    summaries, outcomes = _resume_campaign_twice(case, runtime_execution_id)
    assert summaries[0] == summaries[1]
    assert all(outcome.complete for outcome in outcomes)
    assert len(case.state.completion_order) == 3
    assert case.store.load_manifest().document["origin"]["execution_id"] == (
        runtime_execution_id or ""
    )
    assert case.slots.snapshot(CLASS_MEMORY) == ([], [])


def _resume_campaign_twice(case, runtime_execution_id):
    request_args = (
        case.validated,
        case.plan,
        case.project,
        case.invocation.parent,
        CampaignPolicy(),
        case.invocation,
    )
    summaries = []
    outcomes = []
    for _ in range(2):
        with AdmissionGate(
            _CampaignAdmissionEndpoint(case.slots, runtime_execution_id)  # type: ignore[arg-type]
        ).enter() as admission:
            outcomes.append(case.campaign.run(ResumeCampaignRunRequest(*request_args, admission)))
        summaries.append(case.store.summary_path.read_bytes())
    return summaries, outcomes


try:
    import resource
except ImportError:  # pragma: no cover - Windows compatibility
    resource = None  # type: ignore[assignment]


def _peak_rss() -> int:
    if resource is None:
        return 0
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _seed_authenticated_states(
    store: CampaignStore, project: Path, executor: _NoEdaExecutor
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
            CampaignPolicy(),
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


def _resume_request(store: CampaignStore, project: Path):
    manifest = store.load_manifest()
    validated = ValidatedResumeManifest(
        ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest)),
        (),
        (),
    )
    return ResumeCampaignPreviewRequest(
        validated=validated,
        current_plan=create_simulation_campaign_plan(manifest),
        project_root=project,
        report_root=None,
        policy=CampaignPolicy(),
    )


# Windows CI: 3x the slowest observed duration (tests/timeout_headroom.py).
@pytest.mark.timeout(90)
def test_maximum_campaign_previews_authenticated_mixed_resume_without_eda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    manifest = _manifest_for(tuple(f"case_{index:04d}" for index in range(1_000)))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    invocation = tmp_path / "reports" / "sim" / "1"
    store = CampaignStore(invocation / "targets" / "sim" / "record")
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
    status = SimulationCampaign().inspect_resume(request.validated)

    before_rss = _peak_rss()
    started = time.monotonic()
    first = store.regenerate_summary()
    preview = SimulationCampaign().preview(request)
    elapsed = time.monotonic() - started
    peak_delta = _peak_rss() - before_rss
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
    assert elapsed < 15
    assert peak_delta < 128 * 1024


def test_campaign_previews_expose_the_complete_public_contract(tmp_path: Path) -> None:
    manifest = _manifest_for(("smoke",))
    plan = create_simulation_campaign_plan(manifest)
    preview = SimulationCampaign().preview(
        NewCampaignPreviewRequest(plan, tmp_path, None, CampaignPolicy())
    )
    assert preview.normalized_selection == (
        manifest.document["work_items"][0]["selection"],  # type: ignore[index]
    )
    assert preview.workload == manifest.document["workload"]

    store = CampaignStore(tmp_path / "reports" / "sim" / "1" / "targets" / "sim" / "record")
    store.publish_manifest(manifest)
    resumed = SimulationCampaign().preview(_resume_request(store, tmp_path))
    assert resumed.manifest == manifest.document


@pytest.mark.parametrize("cancelled", [False, True])
def test_resume_endpoint_refreshes_recovery_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancelled: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    store = CampaignStore(tmp_path / "reports" / "sim" / "1" / "targets" / "sim" / "record")
    store.publish_manifest(_manifest_for(("smoke",)))
    validated = _resume_request(store, project).validated
    observed = SimulationCampaign().inspect_resume(validated)
    work_item_id = observed.pending[0]
    invocation = tmp_path / "reports" / "sim" / "2"
    invocation.mkdir(parents=True)
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=tmp_path / "reports", work_dir=project)
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: invocation)

    def fail_after_attempt(*_args: object) -> None:
        store.allocate_attempt_directory(work_item_id, str(uuid.uuid4()))
        if cancelled:
            raise SimulationCampaignCancellationError("cancelled")
        raise OSError("failed")

    monkeypatch.setattr(flow, "_write_campaign_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flow, "_run_validated_resume_campaign", fail_after_attempt)
    with flow.publication_resources:
        result = flow._execute_campaign(
            _admission(), flow._resume_campaign_run(validated, observed)
        )

    assert result.exit_code == (EXIT_CANCELLED if cancelled else EXIT_ERROR)
    assert result.detail["completed"] == []
    assert result.detail["interrupted"] == [work_item_id]
    assert result.detail["pending"] == []


def test_resume_execution_uses_a_fresh_scan_after_preview(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    first_invocation = tmp_path / "reports" / "sim" / "1"
    store = CampaignStore(first_invocation / "targets" / "sim" / "record")
    store.publish_manifest(_manifest_for(("smoke",)))
    preview_request = _resume_request(store, project)

    preview = SimulationCampaign().preview(preview_request)
    assert preview.recovery.pending

    first_executor = _NoEdaExecutor()
    first_outcome = SimulationCampaign(first_executor).run(
        ResumeCampaignRunRequest(
            preview_request.validated,
            preview_request.current_plan,
            project,
            None,
            CampaignPolicy(),
            tmp_path / "reports" / "sim" / "2",
            _admission(),
        )
    )
    assert first_executor.calls == 1
    assert first_outcome.recovery.pending == ()

    manifest_bytes = store.manifest_path.read_bytes()
    second_executor = _NoEdaExecutor()
    second_outcome = SimulationCampaign(second_executor).run(
        ResumeCampaignRunRequest(
            preview_request.validated,
            preview_request.current_plan,
            project,
            None,
            CampaignPolicy(timeout_seconds=42, build_timeout_seconds=17, result_verbosity="full"),
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
    store = CampaignStore(tmp_path / "reports" / "sim" / "1" / "targets" / "sim" / "record")
    store.publish_manifest(_manifest_for(("smoke",)))
    validated = _resume_request(store, tmp_path).validated
    store.manifest_path.unlink()

    status = SimulationCampaign().inspect_resume(validated)

    assert status.manifest_sha256 == validated.sha256
