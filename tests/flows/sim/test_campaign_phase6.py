"""Release-gate integrity, scale, and retention checks for Simulation Campaigns."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

try:
    import resource
except ImportError:  # pragma: no cover - Windows compatibility
    resource = None  # type: ignore[assignment]

from booley.criteria.state import DevelopmentState
from booley.flows.sim.acceptance import record_campaign_acceptance
from booley.flows.sim.campaign import (
    SimulationCampaignWorkItemError,
    authenticate_work_item,
    inspect_retained_campaign,
)
from booley.flows.sim.campaign.codec import (
    RECORD_MAX_BYTES,
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_bundle_build_attempt,
    decode_bundle_build_result,
    decode_simulation_attempt,
    decode_simulation_result,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignPreviewRequest,
    NewCampaignRunRequest,
    ResumeCampaignPreviewRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
    SimulationCampaignCancellationError,
    WorkExecutionRequest,
    _acceptance_ready,
)
from booley.flows.sim.campaign.model import SimulationResult, create_simulation_campaign_plan
from booley.flows.sim.campaign.planning import manifest_digest
from booley.flows.sim.campaign.resume import ValidatedManifestNode, ValidatedResumeManifest
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation
from booley.flows.sim.flow import SimulateFlow
from booley.runtime.endpoint_execution import EXIT_CANCELLED, EXIT_ERROR
from booley.runtime.execution_records import ExecutionId, atomic_write_json, execution_paths
from booley.ticket_board.flow_execution import TicketAcceptanceRecorder
from tests.flows.sim.test_campaign_phase3_adversarial import _completed
from tests.flows.sim.test_campaign_phase3_integrity import _admission, _manifest_for

_STAMP = "2026-09-22T00:00:00Z"


class _RawDocument:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def canonical_bytes(self) -> bytes:
        return self._raw


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _peak_rss() -> int:
    if resource is None:
        return 0
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _reference(path: str, raw: bytes, kind: str, owner: str) -> dict[str, object]:
    return {"path": path, "bytes": len(raw), "sha256": _digest(raw), "kind": kind, "owner": owner}


class _NoEdaExecutor:
    """Publish a fully authenticated design-failure chain without spawning EDA."""

    def __init__(self) -> None:
        self.calls = 0

    def prepare_attempt(self, request: WorkExecutionRequest) -> None:
        raise AssertionError("unmanaged campaigns must not prepare child attempts")

    def execute(self, request: WorkExecutionRequest) -> SimulationResult:
        self.calls += 1
        attempt, build_attempt, build_result, result = self.documents(request)
        request.store.publish_attempt(request.attempt_directory, attempt)
        build_root = request.attempt_directory / "private-build"
        build_root.mkdir()
        request.store.publish_build_attempt(build_root, build_attempt)
        request.store.publish_build_result(build_root, build_result)
        return result

    def documents(self, request: WorkExecutionRequest, *, validate: bool = True):
        common = _common(request)
        attempt = self._attempt(request, common, validate=validate)
        build_attempt = self._build_attempt(request, common, validate=validate)
        build_path = request.attempt_directory / "private-build/build-attempt.json"
        build_result = self._build_result(
            request, common, build_path, build_attempt, validate=validate
        )
        result_path = request.attempt_directory / "private-build/build-result.json"
        result = self._result(request, common, result_path, build_result, validate=validate)
        return attempt, build_attempt, build_result, result

    @staticmethod
    def _attempt(request: WorkExecutionRequest, common: dict[str, object], *, validate: bool):
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        run_cwd = cast(Mapping[str, object], workload["run_cwd"])
        document = {
            "$schema": "booley.simulation-attempt/v1",
            **common,
            "attempt_id": request.attempt_id,
            "attempt_ordinal": request.attempt_ordinal,
            "producer_invocation_id": request.producer_invocation_id,
            "build_variant_id": request.work_item["build_variant_id"],
            "run_directory": {
                "kind": run_cwd["kind"],
                "configured": run_cwd["configured"],
                "resolved": str(request.project_root),
                "collision_key": str(request.project_root),
                "owned": False,
            },
            "child_execution_id": request.child_execution_id,
            "child_entry_sha256": request.child_entry_sha256,
            "pre_sim_build_access": workload["pre_sim_build_access"],
            "policy": {"timeout_seconds": None, "no_kill": False, "diagnostic": False},
            "started_at": _STAMP,
        }
        raw = canonical_json_bytes(document)
        return decode_simulation_attempt(raw) if validate else _RawDocument(raw)

    @staticmethod
    def _build_attempt(
        request: WorkExecutionRequest, common: dict[str, object], *, validate: bool
    ):
        workload = cast(Mapping[str, object], request.manifest.document["workload"])
        eda = cast(Mapping[str, object], workload["eda"])
        document = {
            "$schema": "booley.bundle-build-attempt/v1",
            **{key: common[key] for key in ("campaign_id", "manifest_sha256", "workload_sha256")},
            "build_variant_id": request.work_item["build_variant_id"],
            "build_attempt_id": request.attempt_id,
            "build_attempt_ordinal": 1,
            "producer_invocation_id": request.producer_invocation_id,
            "sharing": "private_work_item",
            "owner": {
                "work_item_id": request.work_item["work_item_id"],
                "simulation_attempt_id": request.attempt_id,
            },
            "tool_provenance": {
                "eda_kind": eda["kind"],
                "eda_version": eda["version"],
                "adapter_contract_version": workload["adapter_contract_version"],
            },
            "started_at": _STAMP,
        }
        raw = canonical_json_bytes(document)
        return decode_bundle_build_attempt(raw) if validate else _RawDocument(raw)

    @staticmethod
    def _build_result(request, common, attempt_path, attempt, *, validate):
        raw = attempt.canonical_bytes()
        document = {
            "$schema": "booley.bundle-build-result/v1",
            **{key: common[key] for key in ("campaign_id", "manifest_sha256", "workload_sha256")},
            "build_variant_id": request.work_item["build_variant_id"],
            "build_attempt": _reference(
                attempt_path.name, raw, "bundle_build_attempt", request.attempt_id
            )
            | {"build_attempt_id": request.attempt_id},
            "state": "design_failure",
            "phase": "compile",
            "finished_at": _STAMP,
            "elapsed_seconds": 0.0,
            "bundle": None,
            "observation": {
                "class": "design",
                "code": "synthetic",
                "message": "synthetic no-EDA build failure",
                "detail": {},
            },
            "evidence": [],
        }
        raw = canonical_json_bytes(document)
        return decode_bundle_build_result(raw) if validate else _RawDocument(raw)

    @staticmethod
    def _result(request, common, result_path, build_result, *, validate):
        raw = build_result.canonical_bytes()
        selection = cast(Mapping[str, object], request.work_item["selection"])
        names = cast(tuple[str, ...], selection.get("names", ()))
        observation = {
            "test": names[0] if names else None,
            "execution": "blocked_by_build",
            "failure_class": "design",
            "functional": "not_observed",
            "assertions": "not_observed",
            "assertion_count": 0,
            "detail": {},
            "cycle_count": None,
        }
        document = {
            "$schema": "booley.simulation-result/v1",
            **common,
            "attempt_id": request.attempt_id,
            "attempt_ordinal": request.attempt_ordinal,
            "producer_invocation_id": request.producer_invocation_id,
            "state": "blocked_by_build",
            "build_result": _reference(
                "private-build/" + result_path.name, raw, "bundle_build_result", request.attempt_id
            )
            | {
                "build_attempt_id": request.attempt_id,
                "state": "design_failure",
                "sharing": "private_work_item",
            },
            "bundle_id": None,
            "finished_at": _STAMP,
            "elapsed_seconds": 0.0,
            "executable_snapshot": None,
            "runtime_inputs": [],
            "observations": [observation],
            "grade": "fail",
            "diagnostics": [],
            "evidence": [],
        }
        raw = canonical_json_bytes(document)
        return decode_simulation_result(raw) if validate else _RawDocument(raw)


def _common(request: WorkExecutionRequest) -> dict[str, object]:
    fingerprints = cast(Mapping[str, object], request.manifest.document["fingerprints"])
    return {
        "campaign_id": request.manifest.document["campaign_id"],
        "manifest_sha256": manifest_digest(request.manifest),
        "workload_sha256": fingerprints["workload_sha256"],
        "work_item_id": request.work_item["work_item_id"],
    }


@pytest.mark.parametrize(
    "defect", ["corrupt", "truncate", "replace", "symlink", "hardlink", "resize"]
)
def test_terminal_authority_defect_matrix_fails_closed(tmp_path: Path, defect: str) -> None:
    (tmp_path / ".booley_project").mkdir()
    completed = _completed(tmp_path)
    result = completed.store.work_item_directory(completed.item_id) / "result.json"
    original = result.with_name("original-result.json")
    result.rename(original)
    if defect == "corrupt":
        result.write_bytes(b"{}\n")
    elif defect == "truncate":
        result.write_bytes(original.read_bytes()[:23])
    elif defect == "replace":
        replacement = json.loads(original.read_bytes())
        replacement["attempt_id"] = "0" * 32
        result.write_bytes(canonical_json_bytes(replacement))
    elif defect == "symlink":
        result.symlink_to(original.name)
    elif defect == "hardlink":
        os.link(original, result)
    else:
        result.write_bytes(b"x" * (RECORD_MAX_BYTES + 1))

    with pytest.raises(SimulationCampaignIntegrityError):
        completed.store.scan()


def test_public_campaign_inspection_returns_typed_evidence_without_writes(
    tmp_path: Path,
) -> None:
    completed = _completed(tmp_path)
    completed.store.regenerate_summary()
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in completed.store.root.rglob("*")
        if path.is_file()
    }

    evidence = authenticate_work_item(completed.store.manifest_path, completed.item_id)
    status = inspect_retained_campaign(completed.store.manifest_path)

    assert evidence.manifest_path == completed.store.manifest_path
    assert evidence.manifest_sha256 == completed.store.manifest_sha256()
    assert evidence.work_item_id == completed.item_id
    assert evidence.work_item["work_item_id"] == completed.item_id
    assert evidence.result.document["work_item_id"] == completed.item_id
    assert status.manifest_path == completed.store.manifest_path
    assert status.summary_path == completed.store.summary_path
    assert status.completed == (completed.item_id,)
    assert status.pending == status.interrupted == ()
    assert status.summary_complete
    assert status.summary_completed_matches
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before} == before


def test_public_work_item_authentication_rejects_unknown_and_unfinished_items(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(_manifest_for(("smoke",)))
    item = cast(tuple[Mapping[str, object], ...], store.load_manifest().document["work_items"])[0]
    work_item_id = cast(str, item["work_item_id"])

    with pytest.raises(SimulationCampaignWorkItemError, match="no exact terminal"):
        authenticate_work_item(store.manifest_path, "item:9999:0000000000000000")
    with pytest.raises(SimulationCampaignWorkItemError, match="no exact terminal"):
        authenticate_work_item(store.manifest_path, work_item_id)


def test_public_campaign_inspection_distinguishes_recovery_and_summary_status(
    tmp_path: Path,
) -> None:
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(_manifest_for(("smoke",)))
    store.regenerate_summary()
    pending = inspect_retained_campaign(store.manifest_path)
    assert pending.pending and not pending.interrupted
    assert not pending.summary_complete and pending.summary_completed_matches

    work_item_id = pending.pending[0]
    store.allocate_attempt_directory(work_item_id, str(uuid.uuid4()))
    store.regenerate_summary()
    interrupted = inspect_retained_campaign(store.manifest_path)
    assert interrupted.interrupted == (work_item_id,)
    assert not interrupted.pending


def test_public_campaign_inspection_rejects_malformed_summary_but_reports_mismatch(
    tmp_path: Path,
) -> None:
    completed = _completed(tmp_path)
    completed.store.regenerate_summary()
    summary = json.loads(completed.store.summary_path.read_bytes())
    summary["complete"] = False
    completed.store.summary_path.write_bytes(canonical_json_bytes(summary))

    status = inspect_retained_campaign(completed.store.manifest_path)
    assert not status.summary_complete
    assert status.summary_completed_matches

    summary["complete"] = True
    summary["completed"] = []
    completed.store.summary_path.write_bytes(canonical_json_bytes(summary))
    status = inspect_retained_campaign(completed.store.manifest_path)
    assert status.summary_complete
    assert not status.summary_completed_matches

    del summary["complete"]
    completed.store.summary_path.write_bytes(canonical_json_bytes(summary))
    with pytest.raises(SimulationCampaignIntegrityError, match="retention fields"):
        inspect_retained_campaign(completed.store.manifest_path)


@pytest.mark.parametrize("defect", ["symlink", "hardlink", "oversize"])
def test_public_work_item_authentication_retains_store_file_rejections(
    tmp_path: Path, defect: str
) -> None:
    completed = _completed(tmp_path)
    result = completed.store.work_item_directory(completed.item_id) / "result.json"
    original = result.with_name("original-result.json")
    result.rename(original)
    if defect == "symlink":
        result.symlink_to(original.name)
    elif defect == "hardlink":
        result.hardlink_to(original)
    else:
        result.write_bytes(b"x" * (RECORD_MAX_BYTES + 1))

    with pytest.raises(SimulationCampaignIntegrityError):
        authenticate_work_item(completed.store.manifest_path, completed.item_id)


def test_public_work_item_authentication_rejects_file_changed_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = _completed(tmp_path)
    manifest = completed.store.manifest_path
    original_read = os.read
    swapped = False

    def swap_after_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        raw = original_read(descriptor, count)
        if not swapped:
            swapped = True
            replacement = manifest.with_name("original-manifest.json")
            manifest.rename(replacement)
            manifest.write_bytes(replacement.read_bytes())
        return raw

    monkeypatch.setattr(os, "read", swap_after_read)

    with pytest.raises(SimulationCampaignIntegrityError, match="changed during read"):
        authenticate_work_item(manifest, completed.item_id)


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


def test_maximum_campaign_previews_authenticated_mixed_resume_without_eda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    manifest = _manifest_for(tuple(f"case_{index:04d}" for index in range(1_000)))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    invocation = tmp_path / "reports" / "sim" / "1"
    store = CampaignStore(invocation / "targets" / "sim" / "campaign")
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

    store = CampaignStore(tmp_path / "reports" / "sim" / "1" / "targets" / "sim" / "campaign")
    store.publish_manifest(manifest)
    resumed = SimulationCampaign().preview(_resume_request(store, tmp_path))
    assert resumed.manifest == manifest.document


def test_resume_inspection_rejects_manifest_replaced_after_validation(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "reports" / "sim" / "1" / "targets" / "sim" / "campaign")
    store.publish_manifest(_manifest_for(("smoke",)))
    validated = _resume_request(store, tmp_path).validated

    store.manifest_path.unlink()
    store.publish_manifest(_manifest_for(("replacement",)))

    with pytest.raises(
        SimulationCampaignIntegrityError,
        match="changed after resume validation",
    ):
        SimulationCampaign().inspect_resume(validated)


@pytest.mark.parametrize("cancelled", [False, True])
def test_resume_endpoint_refreshes_recovery_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancelled: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    store = CampaignStore(tmp_path / "reports" / "sim" / "1" / "targets" / "sim" / "campaign")
    store.publish_manifest(_manifest_for(("smoke",)))
    validated = _resume_request(store, project).validated
    observed = SimulationCampaign().inspect_resume(validated)
    work_item_id = observed.pending[0]
    invocation = tmp_path / "reports" / "sim" / "2"
    invocation.mkdir(parents=True)
    flow = SimulateFlow()
    flow.context._args = SimpleNamespace(report_dir=tmp_path / "reports", work_dir=project)
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: invocation)

    def fail_after_attempt(*_args: object) -> None:
        store.allocate_attempt_directory(work_item_id, str(uuid.uuid4()))
        if cancelled:
            raise SimulationCampaignCancellationError("cancelled")
        raise OSError("failed")

    monkeypatch.setattr(flow, "_execute_validated_resume", fail_after_attempt)
    with flow.context.publication_resources:
        result = flow._execute_campaign_resume(validated, _admission(), observed)

    assert result.exit_code == (EXIT_CANCELLED if cancelled else EXIT_ERROR)
    assert result.detail["completed"] == []
    assert result.detail["interrupted"] == [work_item_id]
    assert result.detail["pending"] == []


def test_resume_execution_uses_a_fresh_scan_after_preview(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    first_invocation = tmp_path / "reports" / "sim" / "1"
    store = CampaignStore(first_invocation / "targets" / "sim" / "campaign")
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

    second_executor = _NoEdaExecutor()
    second_outcome = SimulationCampaign(second_executor).run(
        ResumeCampaignRunRequest(
            preview_request.validated,
            preview_request.current_plan,
            project,
            None,
            CampaignPolicy(),
            tmp_path / "reports" / "sim" / "3",
            _admission(),
        )
    )

    assert second_executor.calls == 0
    assert second_outcome.recovery.completed == first_outcome.recovery.completed


def test_acceptance_readiness_requires_strict_superset_grade() -> None:
    manifest = cast(
        object,
        SimpleNamespace(
            document={
                "required_suite": {
                    "names": ("required",),
                    "default_invocation": False,
                }
            }
        ),
    )
    observations = (
        {
            "test": "required",
            "execution": "completed",
            "functional": "pass",
            "assertions": "clean",
        },
        {
            "test": "extra",
            "execution": "completed",
            "functional": "fail",
            "assertions": "clean",
        },
    )

    assert _acceptance_ready(manifest, observations, True, "fail") is False  # type: ignore[arg-type]


def _acceptance_endpoint(state, recorder, invocation):
    return SimpleNamespace(
        state=state,
        _acceptance_recorder=recorder,
        _invocation_id="1",
        args=SimpleNamespace(diagnostic=False),
        _reserved_invocation_dir=invocation,
        _simulation_acceptance_outcomes=(),
        _pending_criteria_set=(),
    )


def _one_item_outcome(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    invocation = tmp_path / "reports" / "sim" / "1"
    request = NewCampaignRunRequest(
        plan=create_simulation_campaign_plan(_manifest_for(("smoke",))),
        project_root=project,
        report_root=None,
        policy=CampaignPolicy(),
        invocation_directory=invocation,
        admission=_admission(),
    )
    return SimulationCampaign(_NoEdaExecutor()).run(request), invocation


def test_acceptance_result_reference_hashes_exact_stored_bytes(tmp_path: Path) -> None:
    outcome, invocation = _one_item_outcome(tmp_path)
    consumed = outcome.acceptance_facts.document["consumed_results"]
    reference = consumed[0]["result"]  # type: ignore[index]
    raw = (invocation / reference["path"]).read_bytes()  # type: ignore[index]
    assert raw.endswith(b"\n")
    assert reference["sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()  # type: ignore[index]


def _ledger_bytes(log_dir: Path) -> dict[Path, bytes]:
    root = log_dir / "acceptance"
    return {path.relative_to(log_dir): path.read_bytes() for path in root.rglob("*.json")}


def test_public_campaign_outcome_replays_exact_acceptance_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    outcome, invocation = _one_item_outcome(tmp_path)
    assert outcome.complete is True
    assert outcome.recovery.completed
    assert outcome.recovery.interrupted == ()
    assert outcome.recovery.pending == ()

    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "ticket"
    state.ticket_type = "implementation"
    state.init_criteria({"sim_pass_sim": True}, strict=True)
    state.criteria["sim_pass_sim"].met = True
    state.save()
    archived = state_path.read_bytes()
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    recorder = TicketAcceptanceRecorder(log_dir=tmp_path / "logs", ticket_identity=identity)

    record_campaign_acceptance(_acceptance_endpoint(state, recorder, invocation), (outcome,))
    first_state = DevelopmentState.load(state_path)
    assert first_state.criteria["sim_pass_sim"].met is False
    assert len(first_state.acceptance_transactions) == 1
    projection = outcome.manifest_path.parents[1] / "simulation.json"
    first_projection = projection.read_bytes()
    first_ledger = _ledger_bytes(tmp_path / "logs")
    assert any("intents" in path.parts for path in first_ledger)
    assert any("transactions" in path.parts for path in first_ledger)
    assert any(path.name == "record.json" for path in first_ledger)

    state_path.write_bytes(archived)
    replayed = DevelopmentState.load(state_path)
    record_campaign_acceptance(_acceptance_endpoint(replayed, recorder, invocation), (outcome,))
    final_state = DevelopmentState.load(state_path)
    final_ledger = _ledger_bytes(tmp_path / "logs")
    assert final_ledger == first_ledger
    assert projection.read_bytes() == first_projection
    assert final_state.acceptance_transactions == first_state.acceptance_transactions
    assert final_state.criteria["sim_pass_sim"].met is False


def _retained_invocation(tmp_path: Path) -> tuple[Path, Path, CampaignStore]:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".booley_project").mkdir()
    complete = _completed(source)
    project_data = tmp_path / "project-data"
    reports = project_data / ".runtime" / "flow-reports"
    invocation = reports / "sim" / "1"
    target = invocation / "targets" / "sim"
    target.mkdir(parents=True)
    shutil.move(str(complete.store.root), target / "campaign")
    store = CampaignStore(target / "campaign")
    store.regenerate_summary()
    (target / "simulation.json").write_bytes(
        canonical_json_bytes(
            {
                "complete": True,
                "target": "sim",
                "campaign_manifest": str(store.manifest_path),
                "campaign_summary": str(store.summary_path),
            }
        )
    )
    (invocation / "progress.json").write_text(
        json.dumps(
            {
                "flow": "sim",
                "targets": ["sim"],
                "completed_targets": ["sim"],
                "pending_targets": [],
            }
        ),
        encoding="utf-8",
    )
    return reports, project_data, store


def _publish_retired_child(
    project_data: Path, store: CampaignStore, *, digit: str = "1"
) -> ExecutionId:
    execution_id = ExecutionId(digit * 32)
    attempt_id = f"{digit * 8}-{digit * 4}-4{digit * 3}-8{digit * 3}-{digit * 12}"
    entry = {
        "$schema": "booley.simulation-campaign-child-entry/v1",
        "child_execution_id": execution_id,
        "parent_execution_id": "2" * 32,
        "campaign_id": str(store.load_manifest().document["campaign_id"]),
        "manifest_path": str(store.manifest_path.resolve()),
        "manifest_sha256": store.manifest_sha256(),
        "work_item_id": "item:0000:0000000000000000",
        "attempt_id": attempt_id,
        "attempt_ordinal": 1,
        "attempt_relative_path": "work-items/0001-0000000000000000/attempts/0001-" + attempt_id,
        "runtime_context_sha256": "sha256:" + "4" * 64,
    }
    entry_raw = canonical_json_bytes(entry)
    terminal = {
        "schema_version": 1,
        "state": "terminal",
        "runtime_identity": None,
        "supervisor": None,
        "leader": None,
        "exit_code": 0,
        "tree_terminal": True,
        "terminal_cause": "completed",
        "updated_at": "2026-09-22T00:00:00Z",
    }
    terminal_raw = canonical_json_bytes(terminal)
    retirement = {
        "$schema": "booley.simulation-campaign-child-retirement/v1",
        "child_execution_id": execution_id,
        "entry_sha256": "sha256:" + hashlib.sha256(entry_raw).hexdigest(),
        "execution_terminal_sha256": "sha256:" + hashlib.sha256(terminal_raw).hexdigest(),
        "lease_id": None,
        "token_absent": True,
        "terminal_cause": "completed",
    }
    retirement_raw = canonical_json_bytes(retirement)
    project_children = project_data / ".runtime" / "campaign-child-executions"
    campaign_children = store.root / "child-executions"
    for root in (project_children, campaign_children):
        (root / "entries").mkdir(parents=True, exist_ok=True)
        (root / "retired").mkdir(parents=True, exist_ok=True)
        (root / "entries" / f"{execution_id}.json").write_bytes(entry_raw)
        (root / "retired" / f"{execution_id}.json").write_bytes(retirement_raw)
    atomic_write_json(execution_paths(execution_id, project_dir=project_data).record, terminal)
    return execution_id


def test_complete_campaign_pruning_releases_exact_project_child_pair(
    tmp_path: Path,
) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    execution_id = _publish_retired_child(project_data, store)

    prune_invocation(reports, 1)

    project_children = project_data / ".runtime" / "campaign-child-executions"
    assert not (project_children / "entries" / f"{execution_id}.json").exists()
    assert not (project_children / "retired" / f"{execution_id}.json").exists()
    assert list((reports / "sim" / ".pruned-1").iterdir()) == []


@pytest.mark.parametrize("defect", ["substituted", "missing"])
def test_pruning_rejects_invalid_project_child_pair(tmp_path: Path, defect: str) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    execution_id = _publish_retired_child(project_data, store)
    project_entry = (
        project_data / ".runtime/campaign-child-executions/entries" / f"{execution_id}.json"
    )
    if defect == "substituted":
        project_entry.write_bytes(b"{}\n")
    else:
        project_entry.unlink()

    with pytest.raises(CampaignRetentionError, match=r"invalid schema|disagrees|disappeared"):
        prune_invocation(reports, 1)

    assert (reports / "sim" / "1").is_dir()
    assert project_entry.is_file() is (defect == "substituted")


def test_pruning_rejects_complete_results_before_acceptance_projection(
    tmp_path: Path,
) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    projection = store.root.parent / "simulation.json"
    document = json.loads(projection.read_bytes())
    document["complete"] = False
    projection.write_bytes(canonical_json_bytes(document))

    with pytest.raises(CampaignRetentionError, match="acceptance projections"):
        prune_invocation(reports, 1)

    assert store.manifest_path.is_file()


@pytest.mark.parametrize(
    ("registry", "defect"),
    [
        ("entries", "missing"),
        ("retired", "missing"),
        ("released", "extra"),
        ("entries", "extra"),
        ("retired", "extra"),
    ],
)
def test_pruning_rejects_campaign_child_inventory_disagreement(
    tmp_path: Path, registry: str, defect: str
) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    execution_id = _publish_retired_child(project_data, store)
    directory = store.root / "child-executions" / registry
    directory.mkdir(exist_ok=True)
    path = directory / f"{execution_id}.json"
    if defect == "missing":
        path.unlink()
    else:
        (directory / f"{'9' * 32}.json").write_bytes(b"{}\n")

    with pytest.raises(CampaignRetentionError, match="inventories disagree"):
        prune_invocation(reports, 1)

    assert (reports / "sim" / "1").is_dir()


def test_pruning_rejects_project_entry_missing_from_campaign_mirror(
    tmp_path: Path,
) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    _publish_retired_child(project_data, store)
    orphan = _publish_retired_child(project_data, store, digit="5")
    campaign = store.root / "child-executions"
    (campaign / "entries" / f"{orphan}.json").unlink()
    (campaign / "retired" / f"{orphan}.json").unlink()

    with pytest.raises(CampaignRetentionError, match="no exact campaign mirror"):
        prune_invocation(reports, 1)


@pytest.mark.parametrize("defect", ["noncanonical", "hardlink", "oversize"])
def test_pruning_rejects_unsafe_campaign_child_record(tmp_path: Path, defect: str) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    execution_id = _publish_retired_child(project_data, store)
    entry = store.root / "child-executions/entries" / f"{execution_id}.json"
    raw = entry.read_bytes()
    if defect == "noncanonical":
        entry.write_text(json.dumps(json.loads(raw), indent=2), encoding="utf-8")
    elif defect == "oversize":
        entry.write_bytes(b"{" + b" " * (1024 * 1024) + b"}\n")
    else:
        source = tmp_path / "hardlink-source"
        entry.rename(source)
        os.link(source, entry)

    with pytest.raises(CampaignRetentionError, match=r"canonical|regular|size ceiling"):
        prune_invocation(reports, 1)


def test_pruning_rejects_terminal_schema_extras(tmp_path: Path) -> None:
    reports, project_data, store = _retained_invocation(tmp_path)
    execution_id = _publish_retired_child(project_data, store)
    terminal = execution_paths(execution_id, project_dir=project_data).record
    document = json.loads(terminal.read_bytes())
    document["unexpected"] = True
    atomic_write_json(terminal, document)

    with pytest.raises(CampaignRetentionError, match="tree-terminal proof"):
        prune_invocation(reports, 1)
