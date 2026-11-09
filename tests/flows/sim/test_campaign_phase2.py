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
from booley.flows.execution_persistence import EvidenceDiscarded
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
from booley.flows.sim.campaign.facts import AcceptanceFacts
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

        def acceptance_identity(self):
            return {}

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
    record_acceptance(flow, PreparedExecution(None, None, False, False), outcome)


def test_simulation_hook_keeps_partial_acceptance_when_recording_fails(monkeypatch) -> None:
    flow = _flow_with_campaign_outcomes("first", "second")

    def commit_one_then_fail(outcomes: tuple[object, ...]) -> None:
        assert outcomes == flow._simulation_campaign_outcomes
        flow._simulation_acceptance_outcomes = (
            SimpleNamespace(committed=True, transaction_id="tx-1", reason="accepted"),
        )
        raise OSError("second Target recording failed")

    monkeypatch.setattr(flow, "record_campaign_acceptance", commit_one_then_fail)
    calls = _spy_hook(monkeypatch, flow)
    outcome = EndpointOutcome()

    with pytest.raises(OSError, match="second Target recording failed"):
        _record_final(flow, outcome)

    assert calls == ["hook"]
    assert outcome.detail["acceptance"] == {
        "status": "partial",
        "targets": [
            {"target": "first", "committed": True, "transaction_id": "tx-1", "reason": "accepted"}
        ],
    }


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
