"""Each defensive refusal on the Simulation Record path names the Simulation Record."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.sim import acceptance, coverage_analysis_input
from booley.flows.sim import flow as sim_flow
from booley.flows.sim.acceptance import publish_simulation_record_projections
from booley.flows.sim.coverage_analysis_input import CoverageAnalysisError
from booley.flows.sim.record import decode_simulation_projection, store
from booley.flows.sim.record.capacity import SimulationRecordCapacityError
from booley.flows.sim.record.codec import encode_simulation_record_manifest
from booley.flows.sim.record.coordinator import SimulationRecord, SimulationRecordPolicy
from booley.flows.sim.record.flow_planning import (
    plan_coarse_simulation_record,
    plan_ordinary_hdl_simulation_record,
)
from booley.flows.sim.record.inspection import authenticate_work_item
from booley.flows.sim.record.model import SimulationRecordDocument
from booley.flows.sim.record.planning import finalize_manifest
from booley.flows.sim.record.resume import validate_resume_manifest
from booley.flows.sim.record.store import SimulationRecordIntegrityError
from booley.flows.sim.record_layout import RECORD_DIRECTORY
from booley.flows.test_runs import build_test_run
from booley.runtime.job_slots import SlotStore
from tests.flows.sim.simulation_record_test_support import _capacity, _managed, _outer_claim
from tests.flows.sim.test_simulation_record_flow_planning import _planning_fixture
from tests.flows.sim.test_simulation_record_manifest_codec import _linked_simulation_record_stores
from tests.flows.sim.test_test_run_evidence_rows import _RESET, _outcome


def test_policy_refuses_non_positive_timeouts() -> None:
    with pytest.raises(ValueError, match="record timeout must be positive"):
        SimulationRecordPolicy(timeout_seconds=0)
    with pytest.raises(ValueError, match="record build timeout must be positive"):
        SimulationRecordPolicy(build_timeout_seconds=0)


def test_run_without_an_executor_is_refused() -> None:
    with pytest.raises(RuntimeError, match="SimulationRecord requires a serial work executor"):
        SimulationRecord().run(None)  # type: ignore[arg-type]


def test_record_document_must_be_an_object() -> None:
    with pytest.raises(TypeError, match="record document must be an object"):
        SimulationRecordDocument([1])  # type: ignore[arg-type]


def test_a_memory_admitted_record_must_hold_a_flow_token(tmp_path: Path) -> None:
    store = SlotStore(tmp_path / "slots")
    outer = _outer_claim(store)
    # An outer token that is no slot token: AdmissionContext itself refuses None.
    capacity = _capacity(_managed(store, object()), tmp_path)
    try:
        with (
            pytest.raises(SimulationRecordCapacityError, match="a memory-admitted record holds"),
            capacity.child_permit(
                "item",
                attempt_id="attempt",
                attempt_ordinal=1,
                attempt_directory=Path("attempt"),
                publish=lambda _child: None,
                memory=capacity.work_item_claim({}),  # type: ignore[arg-type]
            ),
        ):
            pytest.fail("body must not run")
    finally:
        store.release(outer)


def test_store_refusals(tmp_path: Path) -> None:
    # Private seam: the escape check has no public caller that passes a path
    # outside the store.
    with pytest.raises(SimulationRecordIntegrityError, match="record path escapes store"):
        store._require_safe_parents(tmp_path / "a", tmp_path / "b")
    with pytest.raises(SimulationRecordIntegrityError, match="expected exact Simulation Record"):
        store.collect_pre_sim_firings(tmp_path / "x" / "other.json")
    with pytest.raises(SimulationRecordIntegrityError, match="expected exact record/manifest"):
        authenticate_work_item(tmp_path / "x" / "other.json", "item")


def test_projection_identity_must_be_a_version_four_uuid(tmp_path: Path) -> None:
    outcome = _outcome(tmp_path, _RESET)
    publish_simulation_record_projections(tmp_path, (outcome,))
    path = outcome.manifest_path.parents[1] / "simulation.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    for bad in ("not-a-uuid", "00000000-0000-1000-8000-000000000000"):
        value["simulation_record_id"] = bad
        with pytest.raises(ValueError, match="Simulation Record identity is invalid"):
            decode_simulation_projection(value, coverage_expected=False)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"preview": {"target_identity": "b"}}, "Target facts disagree"),
        ({"role": "other"}, "role is invalid"),
        ({"revision": ""}, "revision is required"),
    ],
    ids=["target-facts", "role", "revision"],
)
def test_ordinary_planning_refusals(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    with pytest.raises(SimulationRecordIntegrityError, match=message):
        plan_ordinary_hdl_simulation_record(
            handle=handle,
            inspection=inspection,
            preview=replace(preview, **overrides.get("preview", {})),
            groups=preview.groups,
            required_suite=("reset", "count"),
            revision=overrides.get("revision", "abc123"),
            invocation_id=1,
            execution_id="",
            trace=False,
            role=overrides.get("role", "candidate"),
        )


def test_coarse_planning_refuses_duplicate_test_runs(tmp_path: Path) -> None:
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    # An aggregate may collect many runs of one Test, but never two equal Test Runs.
    run = build_test_run("count")
    preview = replace(preview, groups=(("count",),), run_groups=((run, run),))
    with pytest.raises(SimulationRecordIntegrityError, match="Test Runs must be unique"):
        plan_coarse_simulation_record(
            handle=handle,
            inspection=inspection,
            preview=preview,
            selected_tests=("count",),
            required_suite=("reset", "count"),
            revision="abc123",
            invocation_id=1,
            execution_id="",
            trace=False,
            kind="coverage_aggregate",
        )


def test_a_prerequisite_must_name_the_recorded_identity(tmp_path: Path) -> None:
    candidate, candidate_store, _baseline, _baseline_store = _linked_simulation_record_stores(
        tmp_path
    )
    document = json.loads(encode_simulation_record_manifest(candidate))
    document.pop("fingerprints")
    document["prerequisites"][0]["simulation_record_id"] = "550e8400-e29b-41d4-a716-446655440999"
    candidate_store.manifest_path.write_bytes(
        encode_simulation_record_manifest(finalize_manifest(document))
    )
    with pytest.raises(SimulationRecordIntegrityError, match="prerequisite record identity"):
        validate_resume_manifest(candidate_store.manifest_path, project_root=tmp_path)


def test_projection_publication_refuses_a_foreign_outcome() -> None:
    with pytest.raises(TypeError, match="invalid Simulation Record outcome"):
        acceptance.publish_simulation_record_projections(None, (object(),))


def test_reference_projection_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Private seams: the public read (read_coverage_record) rejects a tampered
    # projection, pointer or manifest earlier, through hash and schema checks,
    # so these branches are reachable only by calling _reference_projection with
    # facts that disagree; _safe_path is stubbed because the paths are fictional.
    public = tmp_path / "coverage.json"
    manifest_path = (tmp_path / RECORD_DIRECTORY / "manifest.json").absolute()
    artifact = SimpleNamespace(path=public)
    monkeypatch.setattr(coverage_analysis_input, "crossed_booley_dir_entry", lambda _p: None)
    monkeypatch.setattr(coverage_analysis_input, "_safe_path", lambda _p: None)
    monkeypatch.setattr(
        coverage_analysis_input,
        "resolve_artifact_reference",
        lambda *_args, **kwargs: SimpleNamespace(
            path=manifest_path
            if kwargs["expected_kind"] == "simulation_record_manifest"
            else public
        ),
    )
    resolved = SimpleNamespace(
        reference=SimpleNamespace(document={"simulation_record_id": "owner"})
    )
    projection_doc = SimpleNamespace(document={"simulation_record_id": "owner"})

    def refusal(
        document: dict[str, object], manifest_document: dict[str, object], **kwargs: Any
    ) -> str:
        authenticated = SimpleNamespace(
            manifest=SimpleNamespace(document=manifest_document), **kwargs
        )
        projection_doc.document = document
        with pytest.raises(CoverageAnalysisError) as caught:
            coverage_analysis_input._reference_projection(
                public,
                projection_doc,  # type: ignore[arg-type]
                resolved,  # type: ignore[arg-type]
                authenticated,  # type: ignore[arg-type]
            )
        return str(caught.value)

    assert "projection owner disagrees" in refusal({"simulation_record_id": "x"}, {})
    monkeypatch.setattr(
        coverage_analysis_input, "resolve_artifact_reference", lambda *_args, **_kwargs: artifact
    )
    assert "projection pointers disagree" in refusal(
        {"simulation_record_id": "owner"}, {"target": 1}
    )
    monkeypatch.setattr(
        coverage_analysis_input,
        "resolve_artifact_reference",
        lambda *_args, **kwargs: SimpleNamespace(
            path=manifest_path
            if kwargs["expected_kind"] == "simulation_record_manifest"
            else public
        ),
    )
    assert "manifest disagrees with reference" in refusal(
        {"simulation_record_id": "owner"}, {"target": {}, "simulation_record_id": "other"}
    )


def test_flow_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    # Private seams: the record-count bound and the complete/phase guard are
    # reachable only with values a real run never produces: 4001 records,
    # and complete=True for a non-final phase, which SimulateFlow never passes.
    # _attach_published_pre_sim and _write_simulation_record_progress are the
    # guarded methods; _reserved_invocation_dir, _retain_pre_sim_after_scan_error,
    # _simulation_record_structured_details and _mode are stubbed so only the
    # guard runs.
    class Many:
        def glob(self, _pattern: str) -> list[Path]:
            return [Path(str(index)) for index in range(4001)]

    class Invocation:
        def __truediv__(self, _name: str) -> Many:
            return Many()

    flow = sim_flow.SimulateFlow.__new__(sim_flow.SimulateFlow)
    flow._reserved_invocation_dir = Invocation()  # type: ignore[assignment]
    seen: list[BaseException] = []

    def retain(result: object, exc: BaseException) -> object:
        seen.append(exc)
        return result

    monkeypatch.setattr(flow, "_retain_pre_sim_after_scan_error", retain, raising=False)
    flow._attach_published_pre_sim("result")  # type: ignore[arg-type]
    assert "invocation Simulation Record count exceeds bound" in str(seen[0])

    monkeypatch.setattr(flow, "reserve_invocation_dir", Path, raising=False)
    monkeypatch.setattr(sim_flow, "progress_document", lambda **_kwargs: {"complete": True})
    monkeypatch.setattr(sim_flow, "_simulation_record_structured_details", lambda *_a: {})
    monkeypatch.setattr(
        sim_flow.SimulateFlow, "_mode", property(lambda _self: SimpleNamespace(value="run"))
    )
    with pytest.raises(ValueError, match="Simulation Record progress complete and phase"):
        flow._write_simulation_record_progress([], [], phase="running", complete=False)
