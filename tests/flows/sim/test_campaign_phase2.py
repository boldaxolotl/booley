from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import BASELINE_TARGET_PARAM, cycle_count_criterion_key
from booley.flows.endpoint_acceptance import record_acceptance
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.sim.acceptance import AcceptanceContext, SimulationAcceptanceCoordinator
from booley.flows.sim.campaign.bundle import (
    SnapshotAttemptIdentity,
    authenticate_executable_snapshot,
    create_executable_snapshot,
)
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulator_bundle,
)
from booley.flows.sim.campaign.coordinator import CampaignOutcome, CampaignRecoveryStatus
from booley.flows.sim.campaign.facts import AcceptanceFacts, decode_acceptance_facts
from booley.flows.sim.campaign.run_directory import (
    claimed_run_directory,
    expand_run_directory,
)
from booley.flows.sim.config import parse_run_cwd_template, resolve_pre_sim_build_access
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.runtime_inputs import materialize_campaign_runtime_inputs
from booley.runtime.endpoint_execution import EXIT_CANCELLED, EndpointOutcome


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _recovery_status(tmp_path: Path) -> CampaignRecoveryStatus:
    return CampaignRecoveryStatus(
        tmp_path / "manifest.json",
        "sha256:" + "1" * 64,
        (),
        (),
        (),
    )


def _facts() -> dict[str, object]:
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "manifest_sha256": "sha256:" + "1" * 64,
        "origin": {"execution_id": "", "invocation_id": 1},
        "target": {
            "vlnv": "acme:lib:dut:1",
            "name": "sim",
            "selector": "sim",
            "project_identity": "project",
            "revision": "abc",
            "role": "candidate",
            "display_name": "sim",
        },
        "required_suite": {
            "names": ["smoke"],
            "default_invocation": False,
            "source_sha256": "sha256:" + "2" * 64,
        },
        "prerequisites": [],
        "consumed_results": [],
        "observations": [],
        "coverage_reference": None,
    }


def _observation(test: str | None) -> dict[str, object]:
    return {
        "work_item_id": "item:0000:0123456789abcdef",
        "role": "candidate",
        "revision": "abc",
        "target": _facts()["target"],
        "result_sha256": "sha256:" + "3" * 64,
        "test": test,
        "execution": "completed",
        "failure_class": None,
        "functional": "pass",
        "assertions": "clean",
        "assertion_count": 0,
        "detail": {},
        "cycle_count": None,
    }


def _simulation_outcome(
    tmp_path: Path,
    observations: list[dict[str, object]],
    *,
    required: tuple[str, ...] = ("half", "full"),
    complete: bool = True,
    role: str = "candidate",
) -> CampaignOutcome:
    document = _facts()
    target = dict(document["target"])  # type: ignore[arg-type]
    target["role"] = role
    document["target"] = target
    document["required_suite"] = {
        "names": list(required),
        "default_invocation": not required,
        "source_sha256": "sha256:" + "2" * 64,
    }
    normalized = []
    for observation in observations:
        normalized.append(observation | {"target": target, "role": role})
    document["observations"] = normalized
    facts = AcceptanceFacts(document)
    passing = all(
        item["execution"] == "completed"
        and item["functional"] == "pass"
        and item["assertions"] != "dirty"
        for item in normalized
    )
    selected = {item["test"] for item in normalized}
    acceptance_ready = (
        complete
        and passing
        and (
            len(normalized) == 1 and None in selected
            if not required
            else set(required).issubset(selected)
        )
    )
    return CampaignOutcome(
        tmp_path / "manifest.json",
        tmp_path / "summary.json",
        facts.document["target"],  # type: ignore[arg-type]
        tuple(facts.document["observations"]),  # type: ignore[arg-type]
        "pass" if passing else "fail",
        complete,
        None,
        facts,
        acceptance_ready,
        _recovery_status(tmp_path),
    )


def _simulation_changes(
    tmp_path: Path,
    criteria: dict[str, dict[str, object]],
    outcome: CampaignOutcome,
):
    class Recorder:
        def __init__(self) -> None:
            self.changes = ()

        def record_or_verify_transaction(self, _state, changes, **_kwargs):
            self.changes = tuple(changes)
            return SimpleNamespace(transaction_id="a" * 64)

    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria(dict.fromkeys(criteria, True), criterion_params=criteria, strict=True)
    state.save()
    recorder = Recorder()
    result = SimulationAcceptanceCoordinator().reconcile(
        outcome,
        AcceptanceContext(
            "ticket",
            {"slug": "ticket"},
            "0" * 32,
            state,
            recorder,  # type: ignore[arg-type]
            "invocation",
        ),
    )
    return result, recorder.changes


def _named_params(selector: str) -> dict[str, object]:
    return {
        "target": "acme:lib:dut:1#sim",
        "_target_selector": "sim",
        "test_selector": selector,
        "required_tests": [selector],
        "minimum_total": 1,
    }


def _cycle_outcome(
    tmp_path: Path,
    *,
    current: int,
    baseline: int = 100,
    baseline_selector: str = "sim_base",
    baseline_test: str = "smoke",
    manifest_owner: str = "550e8400-e29b-41d4-a716-446655440000",
) -> CampaignOutcome:
    document = _facts()
    observation = _observation("smoke")
    observation["target"] = document["target"]
    observation["cycle_count"] = current
    document["observations"] = [observation]
    document["prerequisites"] = [
        _cycle_prerequisite(baseline, baseline_selector, baseline_test, manifest_owner)
    ]
    facts = AcceptanceFacts(document)
    return CampaignOutcome(
        tmp_path / "manifest.json",
        tmp_path / "summary.json",
        facts.document["target"],  # type: ignore[arg-type]
        tuple(facts.document["observations"]),  # type: ignore[arg-type]
        "pass",
        True,
        None,
        facts,
        True,
        _recovery_status(tmp_path),
    )


def _cycle_prerequisite(
    baseline: int, selector: str, test: str, manifest_owner: str
) -> dict[str, object]:
    campaign_id = "550e8400-e29b-41d4-a716-446655440000"
    return {
        "role": "cycle_count_baseline",
        "manifest": {
            "path_base": "origin_invocation",
            "path": "targets/sim_base/campaign/manifest.json",
            "bytes": 10,
            "sha256": "sha256:" + "4" * 64,
            "kind": "simulation_campaign_manifest",
            "owner": manifest_owner,
        },
        "campaign_id": campaign_id,
        "target": {
            "vlnv": "acme:lib:dut:1",
            "name": selector,
            "selector": selector,
            "project_identity": "project",
            "revision": "base-revision",
            "role": "cycle_count_baseline",
            "display_name": selector,
        },
        "work_item_id": "item:0000:fedcba9876543210",
        "result": {
            "path_base": "origin_invocation",
            "path": "targets/sim_base/campaign/work-items/item/result.json",
            "bytes": 10,
            "sha256": "sha256:" + "5" * 64,
            "kind": "simulation_result",
            "owner": "550e8400-e29b-41d4-a716-446655440001",
        },
        "cycle_observation": {
            "test": test,
            "cycle_count": baseline,
            "unit": "cycles",
        },
    }


def _cycle_reconciliation(
    tmp_path: Path,
    *,
    threshold: dict[str, object],
    outcome: CampaignOutcome,
    explicit_baseline: bool = True,
):
    class Recorder:
        def __init__(self) -> None:
            self.changes = ()

        def record_or_verify_transaction(self, _state, changes, **_kwargs):
            self.changes = tuple(changes)
            return SimpleNamespace(transaction_id="a" * 64)

    key = cycle_count_criterion_key("sim", "smoke")
    params = {
        "target": "acme:lib:dut:1#sim",
        "_target_selector": "sim",
        "test": "smoke",
        **threshold,
    }
    if explicit_baseline and any("increase_" in name or "reduce_" in name for name in threshold):
        params[BASELINE_TARGET_PARAM] = "sim_base"
    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria({key: True}, criterion_params={key: params}, strict=True)
    state.save()
    recorder = Recorder()
    result = SimulationAcceptanceCoordinator().reconcile(
        outcome,
        AcceptanceContext(
            "ticket",
            {"slug": "ticket"},
            "0" * 32,
            state,
            recorder,  # type: ignore[arg-type]
            "invocation",
        ),
    )
    return result, recorder.changes


def test_acceptance_facts_are_canonical_immutable_and_exact() -> None:
    facts = AcceptanceFacts(_facts())
    assert facts.canonical_bytes() == canonical_json_bytes(_facts())
    assert facts.sha256.startswith("sha256:")
    with pytest.raises(TypeError):
        facts.document["campaign_id"] = "changed"  # type: ignore[index]
    invalid = _facts() | {"unexpected": True}
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        decode_acceptance_facts(canonical_json_bytes(invalid))


def test_owned_run_directory_is_marked_and_cleaned(tmp_path: Path) -> None:
    run = expand_run_directory(
        "run/{campaign}/{test}",
        project_root=tmp_path,
        campaign_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
        target_key="sim",
        work_item_key="0001-abcdef",
        attempt_key="0001-id",
    )
    identity = {
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "work_item_id": "item:0000:0123456789abcdef",
        "attempt_id": "550e8400-e29b-41d4-a716-446655440001",
    }
    with claimed_run_directory(run, identity=identity) as directory:
        assert directory.is_dir()
        (directory / "output.txt").write_text("evidence", encoding="utf-8")
    assert not run.path.exists()


@pytest.mark.parametrize("template", ["run/{unknown}", "run/{test!r}", "run/{test:>4}", "run/{"])
def test_run_directory_parser_rejects_unsupported_formatting(template: str) -> None:
    with pytest.raises(ValueError):
        parse_run_cwd_template(template)


def test_pre_sim_build_access_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "booley.flows.sim.config._sim_config",
        lambda _root: {"pre_sim_build_access": "unsafe"},
    )
    with pytest.raises(ValueError, match="pre_sim_build_access"):
        resolve_pre_sim_build_access(Path())


def test_executable_snapshot_detects_post_launch_mutation(tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    executable = bundle_root / "simv"
    executable.write_bytes(b"x")
    artifact = {
        "path": "simv",
        "bytes": 1,
        "sha256": _sha(b"x"),
        "kind": "simulator_executable",
    }
    bundle_document = json.loads(
        (Path(__file__).parents[1] / "fixtures/simulation_campaign/bundle.json").read_bytes()
    )
    bundle_document["artifacts"] = [artifact]
    bundle_document["inventory_sha256"] = _sha(canonical_json_bytes([artifact])[:-1])
    bundle_document["snapshot_inventory_sha256"] = bundle_document["inventory_sha256"]
    bundle = decode_simulator_bundle(canonical_json_bytes(bundle_document))
    build_result = {
        "path": "private-build/build-result.json",
        "bytes": 1,
        "sha256": "sha256:" + "a" * 64,
        "kind": "bundle_build_result",
        "owner": "550e8400-e29b-41d4-a716-446655440000",
        "build_attempt_id": "550e8400-e29b-41d4-a716-446655440000",
        "state": "ready",
        "sharing": "shared_variant",
    }
    snapshot_root = tmp_path / "attempt" / "snapshot"
    snapshot = create_executable_snapshot(
        bundle=bundle,
        bundle_root=bundle_root,
        snapshot_root=snapshot_root,
        identity=SnapshotAttemptIdentity(
            campaign_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            manifest_sha256="sha256:" + "1" * 64,
            workload_sha256="sha256:" + "2" * 64,
            work_item_id="item:0000:0123456789abcdef",
            attempt_id="550e8400-e29b-41d4-a716-446655440001",
        ),
        build_result=build_result,
        created_at="2026-09-21T10:00:01Z",
    )
    assert authenticate_executable_snapshot(snapshot, snapshot_root).startswith("sha256:")
    (snapshot_root / "simv").chmod(0o600)
    (snapshot_root / "simv").write_bytes(b"y")
    with pytest.raises(SimulationCampaignIntegrityError, match="changed"):
        authenticate_executable_snapshot(snapshot, snapshot_root)


def test_literal_runtime_input_removes_only_attempt_owned_materialization(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "input.bin").write_bytes(b"runtime input")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    run = tmp_path / "literal-run"
    run.mkdir()
    retained = run / "retained.txt"
    retained.write_text("project-owned", encoding="utf-8")
    declaration = {
        "declaration_id": "sha256:" + "1" * 64,
        "source_artifact_path": "input.bin",
        "destination": "generated/nested/input.bin",
    }

    with materialize_campaign_runtime_inputs(
        bundle_root=bundle,
        attempt_root=attempt,
        run_cwd=run,
        declarations=(declaration,),
        owned_run_directory=False,
    ) as bindings:
        assert (run / "generated/nested/input.bin").read_bytes() == b"runtime input"
        assert bindings[0].owned is True

    assert not (run / "generated").exists()
    assert retained.read_text(encoding="utf-8") == "project-owned"
    assert (attempt / "runtime-inputs/generated/nested/input.bin").read_bytes() == b"runtime input"


def test_acceptance_coordinator_uses_record_or_verify(tmp_path: Path) -> None:
    class Recorder:
        def __init__(self) -> None:
            self.calls = []

        def record_or_verify_transaction(self, state, changes, **kwargs):
            self.calls.append((state, changes, kwargs))
            return type("Transaction", (), {"transaction_id": "a" * 64})()

    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria({"sim_pass_sim": True}, strict=True)
    state.save()
    recorder = Recorder()
    document = _facts()
    document["observations"] = [_observation("smoke")]
    facts = AcceptanceFacts(document)
    outcome = CampaignOutcome(
        tmp_path / "manifest.json",
        tmp_path / "summary.json",
        facts.document["target"],  # type: ignore[arg-type]
        (),
        "pass",
        True,
        None,
        facts,
        True,
        _recovery_status(tmp_path),
    )
    result = SimulationAcceptanceCoordinator().reconcile(
        outcome,
        AcceptanceContext(
            "ticket",
            {"slug": "ticket"},
            "0" * 32,
            state,
            recorder,  # type: ignore[arg-type]
            "invocation",
        ),
    )
    assert result.committed is True
    assert result.transaction_id == "a" * 64
    assert len(recorder.calls) == 1
    detail = recorder.calls[0][1][0].detail
    assert detail["selected_tests"] == ["smoke"]
    assert detail["passed_tests"] == ["smoke"]
    assert detail["failed_tests"] == []


def test_passing_subset_does_not_change_target_level_simulation_criterion(
    tmp_path: Path,
) -> None:
    class Recorder:
        def record_or_verify_transaction(self, *_args, **_kwargs):
            raise AssertionError("subset campaign must not publish target-level acceptance")

    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria({"sim_pass_sim": True}, strict=True)
    state.save()
    document = _facts()
    document["required_suite"] = {
        "names": ["smoke", "regression"],
        "default_invocation": False,
        "source_sha256": "sha256:" + "2" * 64,
    }
    document["observations"] = [_observation("smoke")]
    facts = AcceptanceFacts(document)
    outcome = CampaignOutcome(
        tmp_path / "manifest.json",
        tmp_path / "summary.json",
        facts.document["target"],  # type: ignore[arg-type]
        tuple(facts.document["observations"]),  # type: ignore[arg-type]
        "pass",
        True,
        None,
        facts,
        False,
        _recovery_status(tmp_path),
    )

    result = SimulationAcceptanceCoordinator().reconcile(
        outcome,
        AcceptanceContext(
            "ticket",
            {"slug": "ticket"},
            "0" * 32,
            state,
            Recorder(),  # type: ignore[arg-type]
            "invocation",
        ),
    )

    assert result.reason == "no_applicable_criteria"
    assert state.criteria["sim_pass_sim"].met is False


def test_named_simulation_criterion_uses_its_own_observation(tmp_path: Path) -> None:
    result, changes = _simulation_changes(
        tmp_path,
        {"sim_pass_sim_half": _named_params("half")},
        _simulation_outcome(tmp_path, [_observation("half")]),
    )

    assert result.committed is True
    assert [(change.key, change.met) for change in changes] == [("sim_pass_sim_half", True)]
    detail = changes[0].detail
    assert detail["test_selector"] == "half"
    assert detail["required_tests"] == ["half"]
    assert detail["selected_tests"] == ["half"]
    assert detail["passed_tests"] == ["half"]
    assert detail["failed_tests"] == []


@pytest.mark.parametrize(
    "failure",
    [
        {"functional": "fail", "failure_class": "design"},
        {"assertions": "dirty", "assertion_count": 1, "failure_class": "design"},
        {"execution": "timeout", "functional": "not_observed", "failure_class": "infrastructure"},
        {"execution": "crash", "functional": "not_observed", "failure_class": "infrastructure"},
    ],
)
def test_named_simulation_criterion_records_observed_failure(
    tmp_path: Path, failure: dict[str, object]
) -> None:
    observation = _observation("half") | failure
    _result, changes = _simulation_changes(
        tmp_path,
        {"sim_pass_sim_half": _named_params("half")},
        _simulation_outcome(tmp_path, [observation]),
    )

    assert [(change.key, change.met) for change in changes] == [("sim_pass_sim_half", False)]
    assert changes[0].detail["failed_tests"] == ["half"]


def test_named_simulation_criteria_are_reconciled_independently(tmp_path: Path) -> None:
    criteria = {
        "sim_pass_sim_half": _named_params("half"),
        "sim_pass_sim_full": _named_params("full"),
    }
    half = _observation("half")
    full = _observation("full") | {"functional": "fail", "failure_class": "design"}

    _result, changes = _simulation_changes(
        tmp_path,
        criteria,
        _simulation_outcome(tmp_path, [half, full]),
    )

    assert [(change.key, change.met) for change in changes] == [
        ("sim_pass_sim_half", True),
        ("sim_pass_sim_full", False),
    ]
    assert changes[0].detail["selected_tests"] == ["half"]
    assert changes[0].detail["failed_tests"] == []
    assert changes[1].detail["selected_tests"] == ["full"]
    assert changes[1].detail["failed_tests"] == ["full"]


def test_named_simulation_run_does_not_change_unobserved_sibling(tmp_path: Path) -> None:
    _result, changes = _simulation_changes(
        tmp_path,
        {
            "sim_pass_sim_half": _named_params("half"),
            "sim_pass_sim_full": _named_params("full"),
        },
        _simulation_outcome(tmp_path, [_observation("half")]),
    )

    assert [change.key for change in changes] == ["sim_pass_sim_half"]


def test_legacy_named_selector_and_default_invocation_are_supported(tmp_path: Path) -> None:
    legacy = _named_params("half")
    legacy.pop("required_tests")
    _result, named = _simulation_changes(
        tmp_path,
        {"sim_pass_sim_half": legacy},
        _simulation_outcome(tmp_path, [_observation("half")]),
    )
    _result, default = _simulation_changes(
        tmp_path / "default",
        {"sim_pass_sim": {"test_selector": "all"}},
        _simulation_outcome(tmp_path, [_observation(None)], required=()),
    )

    assert named[0].met is True
    assert named[0].detail["required_tests"] == ["half"]
    assert default[0].met is True
    assert default[0].detail["required_tests"] == ["default"]
    assert default[0].detail["selected_tests"] == ["default"]


@pytest.mark.parametrize(
    ("complete", "role"), [(False, "candidate"), (True, "cycle_count_baseline")]
)
def test_nonterminal_or_baseline_campaign_does_not_change_simulation_criteria(
    tmp_path: Path, complete: bool, role: str
) -> None:
    result, changes = _simulation_changes(
        tmp_path,
        {"sim_pass_sim_half": _named_params("half")},
        _simulation_outcome(
            tmp_path,
            [_observation("half")],
            complete=complete,
            role=role,
        ),
    )

    assert result.reason == "no_applicable_criteria"
    assert changes == ()


@pytest.mark.parametrize(
    ("current", "expected"),
    [(89, True), (90, True), (91, False), (100, False), (110, False)],
)
def test_cycle_acceptance_applies_relative_direction_and_inclusive_threshold(
    tmp_path: Path, current: int, expected: bool
) -> None:
    result, changes = _cycle_reconciliation(
        tmp_path,
        threshold={"cycle_count_reduce_at_least": 10},
        outcome=_cycle_outcome(tmp_path, current=current),
    )

    assert result.committed is True
    assert changes[0].met is expected
    assert changes[0].detail["evaluation"]["checks"][0]["threshold"] == 10


def test_absolute_cycle_acceptance_does_not_require_baseline(tmp_path: Path) -> None:
    outcome = _cycle_outcome(tmp_path, current=95)
    document = dict(outcome.acceptance_facts.document)
    document["prerequisites"] = []
    facts = AcceptanceFacts(document)
    outcome = CampaignOutcome(
        outcome.manifest_path,
        outcome.summary_path,
        outcome.target,
        outcome.observations,
        outcome.aggregate_grade,
        outcome.complete,
        outcome.coverage_reference,
        facts,
        outcome.acceptance_ready,
        outcome.recovery,
    )
    _result, changes = _cycle_reconciliation(
        tmp_path,
        threshold={"cycle_count_max": 100},
        outcome=outcome,
    )

    assert changes[0].met is True
    assert changes[0].detail["baseline_observation"] == "not_required"


def test_same_target_relative_cycle_acceptance_uses_implicit_candidate_baseline(
    tmp_path: Path,
) -> None:
    _result, changes = _cycle_reconciliation(
        tmp_path,
        threshold={"cycle_count_reduce_at_least": 10},
        outcome=_cycle_outcome(tmp_path, current=90, baseline_selector="sim"),
        explicit_baseline=False,
    )

    assert changes[0].met is True
    assert changes[0].detail["baseline_observation"] == "observed"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"baseline_selector": "wrong"}, "mismatched"),
        ({"baseline_test": "other"}, "mismatched"),
        ({"manifest_owner": "550e8400-e29b-41d4-a716-446655440099"}, "mismatched"),
    ],
)
def test_relative_cycle_acceptance_fails_closed_on_mismatched_prerequisite_binding(
    tmp_path: Path, mutation: dict[str, str], expected: str
) -> None:
    _result, changes = _cycle_reconciliation(
        tmp_path,
        threshold={"cycle_count_reduce_at_least": 10},
        outcome=_cycle_outcome(tmp_path, current=90, **mutation),
    )

    assert changes[0].met is False
    assert changes[0].detail["baseline_observation"] == expected


def test_generic_endpoint_delegates_campaign_acceptance_to_owning_flow() -> None:
    received: list[tuple[object, ...]] = []
    endpoint = SimpleNamespace(
        _simulation_campaign_outcomes=("campaign-outcome",),
        flow=SimpleNamespace(record_campaign_acceptance=received.append),
    )

    record_acceptance(
        endpoint,  # type: ignore[arg-type]
        PreparedExecution(None, None, False, False),
        EndpointOutcome(),
    )

    assert received == [("campaign-outcome",)]


def test_cancelled_campaign_suppresses_acceptance_publication() -> None:
    received: list[tuple[object, ...]] = []
    endpoint = SimpleNamespace(
        _pending_criteria_set=("sim",),
        _simulation_campaign_outcomes=("partial-outcome",),
        flow=SimpleNamespace(record_campaign_acceptance=received.append),
    )

    record_acceptance(
        endpoint,  # type: ignore[arg-type]
        PreparedExecution(None, None, False, False),
        EndpointOutcome(exit_code=EXIT_CANCELLED),
    )

    assert received == []
    assert endpoint._pending_criteria_set == ()


def test_all_candidate_manifests_publish_before_first_baseline_work() -> None:
    calls: list[tuple[str, object]] = []

    class ProcessDeath(BaseException):
        pass

    class Campaign:
        def publish_new(self, request):
            calls.append(("publish", request))

        def run(self, request):
            calls.append(("run", request))
            raise ProcessDeath

    baseline = object()
    candidate = object()
    with pytest.raises(ProcessDeath):
        SimulateFlow._publish_and_run_campaign_requests(  # type: ignore[arg-type]
            Campaign(), [baseline], [candidate]
        )

    assert calls == [
        ("publish", baseline),
        ("publish", candidate),
        ("run", baseline),
    ]
