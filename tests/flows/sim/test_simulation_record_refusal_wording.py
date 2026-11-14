"""Each defensive refusal on the Simulation Record path names the Simulation Record."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.sim import acceptance, coverage_analysis_input, record_retention
from booley.flows.sim import flow as sim_flow
from booley.flows.sim.coverage_analysis_input import CoverageAnalysisError
from booley.flows.sim.record import flow_planning, inspection, projection, resume, store
from booley.flows.sim.record.capacity import (
    SimulationRecordCapacity,
    SimulationRecordCapacityError,
)
from booley.flows.sim.record.coordinator import SimulationRecord, SimulationRecordPolicy
from booley.flows.sim.record.model import SimulationRecordDocument
from booley.flows.sim.record.scheduler import (
    BoundedSimulationRecordScheduler,
    SimulationRecordSchedulingError,
)
from booley.flows.sim.record.store import SimulationRecordIntegrityError
from booley.flows.sim.record_layout import RECORD_DIRECTORY
from booley.flows.sim.record_retention import SimulationRecordRetentionError
from booley.mcp import goal_generated_inputs
from booley.mcp.goal_generated_inputs import LifecycleError


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


def test_a_memory_admitted_record_must_hold_a_flow_token() -> None:
    capacity = SimulationRecordCapacity.__new__(SimulationRecordCapacity)
    capacity._admission = SimpleNamespace(outer_token=None)  # type: ignore[attr-defined]
    with pytest.raises(SimulationRecordCapacityError, match="a memory-admitted record holds"):
        capacity._item_thread_count(None)  # type: ignore[arg-type]


def test_a_stopped_scheduler_refuses_a_collision_permit() -> None:
    scheduler = BoundedSimulationRecordScheduler.__new__(BoundedSimulationRecordScheduler)
    scheduler._stop = threading.Event()
    scheduler._stop.set()
    scheduler._collision_condition = threading.Condition()
    scheduler._active_collisions = set()
    permit = scheduler._collision_permit("key")
    with pytest.raises(SimulationRecordSchedulingError, match="record shutdown requested"):
        permit.__enter__()


def test_store_refusals(tmp_path: Path) -> None:
    with pytest.raises(SimulationRecordIntegrityError, match="record path escapes store"):
        store._require_safe_parents(tmp_path / "a", tmp_path / "b")
    with pytest.raises(SimulationRecordIntegrityError, match="expected exact Simulation Record"):
        store.collect_pre_sim_firings(tmp_path / "x" / "other.json")
    with pytest.raises(SimulationRecordIntegrityError, match="expected exact record/manifest"):
        inspection._store_for_manifest(tmp_path / "x" / "other.json")


def test_projection_identity_must_be_a_version_four_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projection, "_validate_common", lambda _value: None)
    monkeypatch.setattr(projection, "_validate_test_runs", lambda _value: None)
    value: dict[str, Any] = dict.fromkeys(
        (
            "flow",
            "mode",
            "target",
            "target_identity",
            "passed",
            "inconclusive",
            "tests",
            "simulation_record_manifest",
            "complete",
            "test_runs",
        )
    )
    value["$schema"] = projection.SIMULATION_PROJECTION_SCHEMA
    for bad in ("not-a-uuid", "00000000-0000-1000-8000-000000000000"):
        value["simulation_record_id"] = bad
        with pytest.raises(ValueError, match="Simulation Record identity is invalid"):
            projection.decode_simulation_projection(value, coverage_expected=False)


def test_planning_refusals() -> None:
    handle = SimpleNamespace(identity="a")
    inspected = SimpleNamespace(handle=handle, flow_options={"cocotb_module": ""})
    preview = SimpleNamespace(target_identity="a")
    check = flow_planning._require_ordinary_hdl_inputs
    with pytest.raises(SimulationRecordIntegrityError, match="Target facts disagree"):
        check(handle, inspected, SimpleNamespace(target_identity="b"), "candidate", "r")  # type: ignore[arg-type]
    with pytest.raises(SimulationRecordIntegrityError, match="role is invalid"):
        check(handle, inspected, preview, "other", "r")  # type: ignore[arg-type]
    with pytest.raises(SimulationRecordIntegrityError, match="revision is required"):
        check(handle, inspected, preview, "candidate", "")  # type: ignore[arg-type]
    run = SimpleNamespace(test="t", name="n")
    preview = SimpleNamespace(target_identity="a", run_groups=[(run, run)], groups=(("t",),))
    inspected = SimpleNamespace(handle=handle, flow_options={"cocotb_module": "m"})
    with pytest.raises(SimulationRecordIntegrityError, match="Test Runs must be unique"):
        flow_planning._validate_coarse_inputs(
            handle,  # type: ignore[arg-type]
            inspected,  # type: ignore[arg-type]
            preview,  # type: ignore[arg-type]
            ["t"],
            "cocotb_batch",
            False,
        )


def test_a_prerequisite_must_name_the_recorded_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = tmp_path / "linked" / RECORD_DIRECTORY / "manifest.json"
    entry = {
        "manifest": {"sha256": "digest", "bytes": 0},
        "simulation_record_id": "expected",
        "target": {},
    }
    node = SimpleNamespace(manifest=SimpleNamespace(document={"prerequisites": [entry]}))
    loaded = SimpleNamespace(
        manifest=SimpleNamespace(document={"simulation_record_id": "other"}), sha256="digest"
    )
    monkeypatch.setattr(resume, "linked_manifest_path", lambda _invocation, _ref: linked)
    monkeypatch.setattr(resume, "_load_node", lambda _path, _booley_dir: loaded)
    monkeypatch.setattr(resume, "encode_simulation_record_manifest", lambda _manifest: b"")
    with pytest.raises(SimulationRecordIntegrityError, match="prerequisite record identity"):
        resume._walk_prerequisites(node, tmp_path, frozenset(), [], None)  # type: ignore[arg-type]


def test_acceptance_refuses_malformed_record_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    def evidence(document: dict[str, object]) -> Any:
        return SimpleNamespace(
            outcome=SimpleNamespace(
                acceptance_facts=SimpleNamespace(document=document), complete=True
            )
        )

    with pytest.raises(TypeError, match="Simulation Record acceptance Target is not a mapping"):
        acceptance.SimulationAcceptanceCoordinator._derive_changes(
            evidence({"target": []}),
            None,  # type: ignore[arg-type]
        )
    monkeypatch.setattr(acceptance, "_matching_simulation_keys", lambda *_args: ["key"])
    given = evidence({"required_suite": []})
    with pytest.raises(TypeError, match="Simulation Record acceptance suite is malformed"):
        acceptance._simulation_changes(
            given.outcome,
            None,  # type: ignore[arg-type]
            {"role": "candidate"},
            "name",
            given,
        )
    endpoint = SimpleNamespace(
        args=SimpleNamespace(work_dir=None, diagnostic=False),
        state=SimpleNamespace(_file_path=None, slug="slug"),
        _acceptance_recorder=SimpleNamespace(acceptance_identity=lambda: None),
        _invocation_id="invocation",
        record_report_criteria=lambda: None,
    )
    monkeypatch.setattr(acceptance, "_detail_stamper", lambda _endpoint: None)
    with pytest.raises(TypeError, match="invalid Simulation Record outcome"):
        acceptance.record_simulation_record_acceptance(endpoint, (object(),))  # type: ignore[arg-type]


def test_reference_projection_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        coverage_analysis_input,
        "resolve_artifact_reference",
        lambda *_args, **_kwargs: artifact,
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
    assert "Target is invalid" in refusal({"simulation_record_id": "owner"}, {"target": 1})
    assert "manifest disagrees with reference" in refusal(
        {"simulation_record_id": "owner"},
        {"target": {}, "simulation_record_id": "other"},
    )


def test_retention_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "a" / "b" / "c"
    status = SimpleNamespace(
        simulation_record_id="owner", manifest_path=tmp_path / "elsewhere.json"
    )
    monkeypatch.setattr(record_retention, "invocation_booley_dir", lambda _root: None)
    monkeypatch.setattr(
        record_retention,
        "resolve_artifact_reference",
        lambda *_args, **_kwargs: SimpleNamespace(path=tmp_path / "manifest.json"),
    )
    with pytest.raises(SimulationRecordRetentionError, match="projection manifest disagrees"):
        record_retention._validate_authenticated_projection(
            target,
            status,  # type: ignore[arg-type]
            {"simulation_record_id": "owner"},
        )
    record = target / RECORD_DIRECTORY
    record.mkdir(parents=True)
    (record / "manifest.json").write_bytes(b"{}")
    (record / ".lock").write_bytes(b"")
    inventory = SimpleNamespace(retention_files=[], coverage_directories=[])
    monkeypatch.setattr(
        record_retention, "inspect_retained_simulation_record", lambda *_a, **_k: inventory
    )
    monkeypatch.setattr(
        record_retention, "authenticate_retained_simulation_record_inventory", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(record_retention, "dependency_receipt_files", lambda _manifest: set())
    monkeypatch.setattr(record_retention, "_valid_lock_sentinel", lambda _lock: False)
    with pytest.raises(SimulationRecordRetentionError, match="Simulation Record lock content"):
        record_retention._target_owned_files(tmp_path, target)


def test_flow_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_generated_input_producer_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    raw = b"{}\n"
    reports = tmp_path / "flow-reports"
    for name in ("one", "two"):
        (reports / name).mkdir(parents=True)
        (reports / name / "manifest.json").write_bytes(raw)
    envelope = {
        "manifest_sha256": "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest(),
        "campaign_id": "owner",
        "origin": "origin",
    }
    producer = {"simulation_record_id": "owner", "origin": "origin"}
    monkeypatch.setattr(
        goal_generated_inputs,
        "decode_simulation_record_manifest",
        lambda _raw: SimpleNamespace(document=producer),
    )
    with pytest.raises(LifecycleError, match="ambiguous duplicate Simulation Record manifests"):
        goal_generated_inputs._producer_manifest(tmp_path, envelope)
    producer["origin"] = "foreign"
    with pytest.raises(LifecycleError, match="foreign producer identity"):
        goal_generated_inputs._producer_manifest(tmp_path, envelope)
