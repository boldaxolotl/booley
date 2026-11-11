"""Criteria publication after Simulation Campaign terminal results."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.criteria.state import CriterionEntry, DevelopmentState
from booley.criteria.templates import BASELINE_TARGET_PARAM, cycle_count_criterion_key
from booley.flows.endpoint_acceptance import record_acceptance
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.execution_persistence import EvidenceDiscarded
from booley.flows.prepared_invocation import OrdinaryRunInvocation
from booley.flows.sim.acceptance import (
    AcceptanceContext,
    SimulationAcceptanceCoordinator,
    record_campaign_acceptance,
)
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignOutcome,
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
    _acceptance_ready,
)
from booley.flows.sim.campaign.facts import AcceptanceFacts
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.flows.test_runs import build_test_run
from booley.runtime.endpoint_execution import EXIT_CANCELLED, EndpointOutcome
from tests.criteria.ledger_support import FixtureRecorder
from tests.flows.sim.campaign_test_support import (
    _acceptance_facts,
    _admission,
    _manifest_for,
    _NoEdaExecutor,
    _observation,
    _recorded_campaign,
    _recovery_status,
    _simulation_outcome,
)


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _simulation_changes(
    tmp_path: Path,
    criteria: dict[str, dict[str, object]],
    outcome: CampaignOutcome,
):
    class Recorder:
        def __init__(self) -> None:
            self.changes = ()

        def acceptance_identity(self):
            return {}

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
    smoke = build_test_run("smoke")
    recorded = _recorded_campaign(tmp_path, (smoke,))
    document = _acceptance_facts(recorded.digest)
    observation = recorded.fact(smoke, **_observation("smoke"))
    observation["target"] = document["target"]
    observation["cycle_count"] = current
    document["observations"] = [observation]
    document["prerequisites"] = [
        _cycle_prerequisite(baseline, baseline_selector, baseline_test, manifest_owner)
    ]
    facts = AcceptanceFacts(document)
    return CampaignOutcome(
        recorded.manifest_path,
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
    baseline_run = build_test_run(test)
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
            # The Test Run the baseline observed, as the coordinator records it.
            "test_run": baseline_run.name,
            "test_run_fingerprint": baseline_run.fingerprint,
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

        def acceptance_identity(self):
            return {}

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


def _v2_facts_with_coverage_and_cycle_baseline() -> dict[str, object]:
    """v2 facts as built by the coordinator, with Coverage and Cycle Count baseline."""
    campaign_id = "12345678-1234-4234-9234-123456789abc"
    baseline_id = "12345678-1234-4234-9234-123456789abd"
    attempt_id = "87654321-4321-4432-a234-cba987654321"
    work_item_id = "item:0000:0123456789abcdef"
    target = {
        "vlnv": "acme:demo:counter:1.0",
        "name": "sim_counter",
        "selector": "sim_counter",
        "project_identity": "project",
        "revision": "abc",
        "role": "candidate",
        "display_name": "sim_counter",
    }

    def evidence(path: str, kind: str, owner: str, sha: str, size: int) -> dict[str, object]:
        return {
            "path_base": "origin_invocation",
            "path": path,
            "bytes": size,
            "sha256": "sha256:" + sha,
            "kind": kind,
            "owner": owner,
        }

    return {
        "$schema": "booley.simulation-acceptance-facts/v2",
        "campaign_id": campaign_id,
        "manifest_sha256": "sha256:" + "a" * 64,
        "origin": {"execution_id": "", "invocation_id": 12},
        "target": target,
        "required_suite": {
            "names": ["reset"],
            "default_invocation": False,
            "source_sha256": "sha256:" + "b" * 64,
        },
        "prerequisites": [
            {
                "role": "cycle_count_baseline",
                "manifest": evidence(
                    "baseline/manifest.json",
                    "simulation_campaign_manifest",
                    baseline_id,
                    "4" * 64,
                    10,
                ),
                "campaign_id": baseline_id,
                "target": target | {"role": "cycle_count_baseline"},
                "work_item_id": work_item_id,
                "result": evidence(
                    "baseline/result.json", "simulation_result", attempt_id, "4" * 64, 10
                ),
                "cycle_observation": {
                    "test": "reset",
                    "cycle_count": 1200,
                    "unit": "cycles",
                    "test_run": "reset@1",
                    "test_run_fingerprint": "sha256:" + "5" * 64,
                },
            }
        ],
        "consumed_results": [],
        "observations": [],
        "coverage_reference": {
            "reference": evidence(
                "targets/sim_counter/coverage.json",
                "coverage_campaign_reference",
                campaign_id,
                "36bef548ff917c687c66ecc28a01417429b7631c785dfd1397a1b714a757dfa2",
                851,
            ),
            "document": {
                "$schema": "booley.coverage-campaign-reference/v1",
                "coverage_campaign": {
                    "bytes": 5386,
                    "campaign_id": "campaign:sim_counter:2026-09-01T10:30:00Z",
                    "path": "campaign/work-items/0001-0123456789abcdef/attempts/"
                    f"0001-{attempt_id}/coverage-campaign/coverage.json",
                    "path_base": "origin_target",
                    "schema": "booley.coverage-campaign/v5",
                    "sha256": "sha256:"
                    "27bf29435ca78e6e4020cbaecd5915b9bb126c99093329f418e02a64e4745437",
                },
                "origin_invocation_id": 12,
                "producer_invocation_id": 19,
                "simulation_attempt_id": attempt_id,
                "simulation_campaign_id": campaign_id,
                "simulation_manifest_sha256": "sha256:" + "a" * 64,
                "simulation_work_item_id": work_item_id,
                "target": {
                    "identity": "acme:demo:counter:1.0#sim_counter",
                    "selector": "sim_counter",
                },
            },
        },
    }


def test_acceptance_facts_hold_the_built_document_without_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facts keep the coordinator's bytes and digest, deep-frozen, with no decode."""
    document = _v2_facts_with_coverage_and_cycle_baseline()

    def no_decode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("acceptance facts must not decode JSON")

    with monkeypatch.context() as patch:
        patch.setattr(json, "loads", no_decode)
        facts = AcceptanceFacts(document)
        raw = facts.canonical_bytes()
        digest = facts.sha256

    # Pinned from the base commit of #2991, where these facts passed full validation.
    assert hashlib.sha256(raw).hexdigest() == (
        "097b6a7468db2ef2d3a4a6992cea148c5f34998ca4e5131162797e7bee5ad0e9"
    )
    assert digest == "sha256:5081a781d4df061f6f5d9a340644b6e73afe7a49aca5d17e26a8920530fb2545"
    prerequisite = facts.document["prerequisites"][0]  # type: ignore[index]
    with pytest.raises(TypeError):
        prerequisite["cycle_observation"]["cycle_count"] = 0  # type: ignore[index]
    with pytest.raises(AttributeError):
        facts.document["prerequisites"].append({})  # type: ignore[union-attr]


def test_acceptance_coordinator_records_selected_test_detail(tmp_path: Path) -> None:
    class Recorder:
        def __init__(self) -> None:
            self.calls = []

        def acceptance_identity(self):
            return {}

        def record_or_verify_transaction(self, state, changes, **kwargs):
            self.calls.append((state, changes, kwargs))
            return type("Transaction", (), {"transaction_id": "a" * 64})()

    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria({"sim_pass_sim": True}, strict=True)
    state.save()
    recorder = Recorder()
    smoke = build_test_run("smoke")
    recorded = _recorded_campaign(tmp_path, (smoke,))
    document = _acceptance_facts(recorded.digest)
    document["observations"] = [recorded.fact(smoke, **_observation("smoke"))]
    facts = AcceptanceFacts(document)
    outcome = CampaignOutcome(
        recorded.manifest_path,
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
    assert len(recorder.calls) == 1
    detail = recorder.calls[0][1][0].detail
    assert detail["selected_tests"] == ["smoke"]
    assert detail["passed_tests"] == ["smoke"]
    assert detail["failed_tests"] == []


def test_passing_subset_does_not_change_target_level_simulation_criterion(
    tmp_path: Path,
) -> None:
    class Recorder:
        def acceptance_identity(self):
            return {}

        def record_or_verify_transaction(self, *_args, **_kwargs):
            raise AssertionError("subset campaign must not publish target-level acceptance")

    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria({"sim_pass_sim": True}, strict=True)
    state.save()
    smoke = build_test_run("smoke")
    recorded = _recorded_campaign(tmp_path, (smoke,), ("smoke", "regression"))
    document = _acceptance_facts(recorded.digest)
    document["required_suite"] = {
        "names": ["smoke", "regression"],
        "default_invocation": False,
        "source_sha256": "sha256:" + "2" * 64,
    }
    document["observations"] = [recorded.fact(smoke, **_observation("smoke"))]
    facts = AcceptanceFacts(document)
    outcome = CampaignOutcome(
        recorded.manifest_path,
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

    if failure["failure_class"] == "infrastructure":
        assert changes == ()
        return
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
    [(90, True), (91, False)],
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


def _flow_with_campaign_outcomes(*selectors: str) -> SimulateFlow:
    """A Simulation Flow holding one stand-in Campaign outcome per selector."""
    flow = SimulateFlow()
    flow._simulation_campaign_outcomes = tuple(
        SimpleNamespace(target={"selector": selector}) for selector in selectors
    )
    return flow


def _spy_hook(monkeypatch, flow: SimulateFlow) -> list[str]:
    """Record each call of the Flow's declared acceptance hook, then delegate to it."""
    calls: list[str] = []
    hook = flow.record_flow_owned_acceptance

    def record(outcome: EndpointOutcome) -> bool:
        calls.append("hook")
        return hook(outcome)

    monkeypatch.setattr(flow, "record_flow_owned_acceptance", record)
    return calls


def _record_final(flow: SimulateFlow, outcome: EndpointOutcome) -> None:
    record_acceptance(
        flow,
        PreparedExecution(None, None, False, False, OrdinaryRunInvocation(EndpointOutcome)),
        outcome,
    )


def test_simulation_hook_propagates_discarded_evidence(monkeypatch) -> None:
    flow = _flow_with_campaign_outcomes("first")
    flow._pending_criteria_set = ("sim",)

    def discard(_outcomes: tuple[object, ...]) -> None:
        raise EvidenceDiscarded("Goal Mode abandoned")

    monkeypatch.setattr(flow, "record_campaign_acceptance", discard)
    calls = _spy_hook(monkeypatch, flow)
    outcome = EndpointOutcome()

    _record_final(flow, outcome)

    assert calls == ["hook"]
    assert flow.evidence_discarded == "Goal Mode abandoned"
    assert flow._pending_criteria_set == ()
    assert "acceptance" not in outcome.detail


def test_cancelled_campaign_suppresses_acceptance_publication(monkeypatch) -> None:
    flow = _flow_with_campaign_outcomes("partial")
    flow._pending_criteria_set = ("sim",)
    received: list[tuple[object, ...]] = []
    monkeypatch.setattr(flow, "record_flow_owned_acceptance", lambda _outcome: received.append(()))

    _record_final(flow, EndpointOutcome(exit_code=EXIT_CANCELLED))

    assert received == []
    assert flow._pending_criteria_set == ()


def test_all_candidate_manifests_publish_before_first_baseline_work() -> None:
    calls: list[tuple[str, object]] = []

    class ProcessDeath(BaseException):
        pass

    class Campaign:
        def preflight(self, request):
            calls.append(("preflight", request))

        def publish_new(self, request):
            calls.append(("publish", request))

        def run(self, request):
            calls.append(("run", request))
            raise ProcessDeath

    baseline = SimpleNamespace(
        project_root=Path("baseline-project"),
        plan=SimpleNamespace(
            manifest=SimpleNamespace(
                document={
                    "target": {
                        "selector": "sim",
                        "vlnv": "acme:lib:dut:1",
                        "name": "sim",
                        "revision": "abc123",
                    }
                }
            )
        ),
    )
    candidate = object()
    with pytest.raises(ProcessDeath):
        SimulateFlow._publish_and_run_campaign_requests(  # type: ignore[arg-type]
            Campaign(), [baseline], [candidate]
        )

    assert calls == [
        ("preflight", baseline),
        ("preflight", candidate),
        ("publish", baseline),
        ("publish", candidate),
        ("run", baseline),
    ]


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
    endpoint = SimulateFlow()
    endpoint.configure_flow_execution(recorder)
    endpoint._args = SimRequest(target="sim", report_dir=invocation.parents[1])
    endpoint._state = state
    endpoint._reserved_invocation_dir = invocation
    return endpoint


def _one_item_outcome(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
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
    recorder = FixtureRecorder(log_dir=tmp_path / "logs", identity=identity)

    record_campaign_acceptance(_acceptance_endpoint(state, recorder, invocation), (outcome,))
    first_state = DevelopmentState.load(state_path)
    assert first_state.criteria["sim_pass_sim"].met is False
    assert len(first_state.acceptance_transactions) == 1
    projection = outcome.manifest_path.parents[1] / "simulation.json"
    projection_document = json.loads(projection.read_bytes())
    assert projection_document["tests"][0]["error_tail"] == ("synthetic no-EDA build failure")
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


def test_campaign_acceptance_reads_identity_through_the_public_recorder_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    outcome, invocation = _one_item_outcome(tmp_path)
    state = DevelopmentState.load(tmp_path / "state.json")
    state.slug = "ticket"
    state.init_criteria({"sim_pass_sim": True}, strict=True)
    state.save()
    identity = {"generation": "f" * 32}
    received: list[Mapping[str, object]] = []

    class PublicIdentityRecorder:
        """Recorder exposing its identity only through ``acceptance_identity``."""

        def acceptance_identity(self) -> Mapping[str, object]:
            return identity

        def record_changes(self, *_args: object, **_kwargs: object) -> None:
            return None

        def record_or_verify_transaction(self, _state, _changes, **kwargs):
            received.append(kwargs["identity"])
            return SimpleNamespace(transaction_id="a" * 64)

    endpoint = _acceptance_endpoint(state, PublicIdentityRecorder(), invocation)
    record_campaign_acceptance(endpoint, (outcome,))

    assert received == [identity]


def test_campaign_projection_error_tail_rejects_malformed_detail(tmp_path: Path) -> None:
    outcome, invocation = _one_item_outcome(tmp_path)
    malformed_observation = dict(outcome.observations[0], detail=[])
    malformed = replace(outcome, observations=(malformed_observation,))
    state = DevelopmentState.load(tmp_path / "state.json")
    recorder = FixtureRecorder(log_dir=tmp_path / "logs", identity={})

    with pytest.raises(SimulationCampaignIntegrityError, match="detail"):
        record_campaign_acceptance(_acceptance_endpoint(state, recorder, invocation), (malformed,))


def _named_campaign_outcome(tmp_path: Path):
    original, invocation = _one_item_outcome(tmp_path)
    document = json.loads(original.acceptance_facts.canonical_bytes())
    document["required_suite"]["names"] = ["half", "full"]
    document["required_suite"]["default_invocation"] = False
    observation = document["observations"][0]
    observation.update(
        test="half",
        execution="completed",
        failure_class=None,
        functional="pass",
        assertions="clean",
    )
    facts = AcceptanceFacts(document)
    return replace(
        original,
        target=facts.document["target"],
        observations=tuple(facts.document["observations"]),
        aggregate_grade="pass",
        acceptance_facts=facts,
        acceptance_ready=False,
    ), invocation


def _named_campaign_state(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "ticket"
    state.ticket_type = "implementation"
    key = "sim_pass_sim_half"
    params = {
        "target": "acme:lib:dut:1#sim",
        "_target_selector": "sim",
        "test_selector": "half",
        "required_tests": ["half"],
        "minimum_total": 1,
    }
    state.init_criteria(
        {key: True, "_report_submitted": True},
        criterion_params={key: params},
        strict=True,
    )
    state.set_criterion("_report_submitted", True)
    state.save()
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    recorder = FixtureRecorder(log_dir=tmp_path / "logs", identity=identity)
    return state_path, state, key, recorder


def test_named_campaign_acceptance_persists_criterion_and_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    outcome, invocation = _named_campaign_outcome(tmp_path)
    state_path, state, key, recorder = _named_campaign_state(tmp_path)
    endpoint = _acceptance_endpoint(state, recorder, invocation)

    record_campaign_acceptance(endpoint, (outcome,))
    endpoint.finish_execution(
        PreparedExecution(None, None, False, False, OrdinaryRunInvocation(EndpointOutcome)),
        EndpointOutcome(),
        started=None,
        acceptance_recorded=True,
    )

    persisted = DevelopmentState.load(state_path)
    assert persisted.criteria[key].met is True
    assert persisted.criteria[key].detail["selected_tests"] == ["half"]
    assert persisted.timeline[-1]["criteria_set"] == [key]


def test_infrastructure_abort_preserves_simulation_criterion(tmp_path: Path) -> None:
    from booley.flows.sim.acceptance import SimulationAcceptanceCoordinator

    original, _invocation = _named_campaign_outcome(tmp_path)
    document = json.loads(original.acceptance_facts.canonical_bytes())
    document["observations"][0].update(
        execution="aborted",
        failure_class="infrastructure",
        functional="not_observed",
        assertions="not_observed",
        cycle_count=None,
        detail={"termination": "disk_budget", "reason": "disk guard"},
    )
    facts = AcceptanceFacts(document)
    outcome = replace(
        original,
        observations=tuple(facts.document["observations"]),
        acceptance_facts=facts,
        aggregate_grade="error",
    )
    _path, state, key, _recorder = _named_campaign_state(tmp_path)
    previous = True
    state.criteria[key].met = previous
    state.criteria[key].detail = {"prior": "retained"}
    state.criteria["cycle_count_sim_half"] = CriterionEntry(
        met=previous,
        detail={"prior": "cycle retained"},
        params={
            "target": "acme:lib:dut:1#sim",
            "_target_selector": "sim",
            "test": "half",
            "max_cycles": 100,
        },
    )
    before = state.to_dict()
    assert SimulationAcceptanceCoordinator._derive_changes(outcome, state) == []
    assert state.to_dict() == before
