"""Criteria publication after Simulation Record terminal results."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.endpoint_acceptance import record_acceptance
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.prepared_invocation import OrdinaryRunInvocation
from booley.flows.sim.acceptance import publish_simulation_record_projections
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record.codec import (
    SimulationRecordIntegrityError,
)
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordPolicy,
    _acceptance_ready,
)
from booley.flows.sim.record.facts import AcceptanceFacts
from booley.flows.sim.record.model import create_simulation_record_plan
from booley.runtime.endpoint_execution import EXIT_CANCELLED, EndpointOutcome
from tests.flows.sim.simulation_record_test_support import (
    _admission,
    _manifest_for,
    _NoEdaExecutor,
)


def _v2_facts_with_coverage_and_cycle_baseline() -> dict[str, object]:
    """v2 facts as built by the coordinator, with Coverage and Cycle Count baseline."""
    simulation_record_id = "12345678-1234-4234-9234-123456789abc"
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
        "campaign_id": simulation_record_id,
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
                simulation_record_id,
                "36bef548ff917c687c66ecc28a01417429b7631c785dfd1397a1b714a757dfa2",
                851,
            ),
            "document": {
                "$schema": "booley.coverage-record-reference/v1",
                "coverage_record": {
                    "bytes": 5386,
                    "coverage_record_id": "campaign:sim_counter:2026-09-01T10:30:00Z",
                    "path": "record/work-items/0001-0123456789abcdef/attempts/"
                    f"0001-{attempt_id}/coverage-record/coverage.json",
                    "path_base": "origin_target",
                    "schema": "booley.coverage-record/v1",
                    "sha256": "sha256:"
                    "27bf29435ca78e6e4020cbaecd5915b9bb126c99093329f418e02a64e4745437",
                },
                "origin_invocation_id": 12,
                "producer_invocation_id": 19,
                "simulation_attempt_id": attempt_id,
                "simulation_record_id": simulation_record_id,
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

    # Pinned from #2991 (base commit), re-pinned for the Record paths of #3685 and the Coverage Record names of #3688.
    assert hashlib.sha256(raw).hexdigest() == (
        "673e3f58cc435bafd241831f96d5056142721a0b7a26bd27d1be031868983a00"
    )
    assert digest == "sha256:82777c907f41c2966c91f1e426c7d5b0efff39d67b431ffd8b91ceaa813994b9"
    prerequisite = facts.document["prerequisites"][0]  # type: ignore[index]
    with pytest.raises(TypeError):
        prerequisite["cycle_observation"]["cycle_count"] = 0  # type: ignore[index]
    with pytest.raises(AttributeError):
        facts.document["prerequisites"].append({})  # type: ignore[union-attr]


def _flow_with_simulation_record_outcomes(*selectors: str) -> SimulateFlow:
    """A Simulation Flow holding one stand-in Simulation Record outcome per selector."""
    flow = SimulateFlow()
    flow._simulation_record_outcomes = tuple(
        SimpleNamespace(target={"selector": selector}) for selector in selectors
    )
    return flow


def _record_final(flow: SimulateFlow, outcome: EndpointOutcome) -> None:
    record_acceptance(
        flow,
        PreparedExecution(None, None, False, False, OrdinaryRunInvocation(EndpointOutcome)),
        outcome,
    )


def test_cancelled_simulation_record_suppresses_acceptance_publication(monkeypatch) -> None:
    flow = _flow_with_simulation_record_outcomes("partial")
    flow._pending_criteria_set = ("sim",)
    received: list[tuple[object, ...]] = []
    monkeypatch.setattr(flow, "record_flow_owned_acceptance", lambda _outcome: received.append(()))

    _record_final(flow, EndpointOutcome(exit_code=EXIT_CANCELLED))

    assert received == []
    assert flow._pending_criteria_set == ()


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


def _one_item_outcome(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    invocation = tmp_path / "reports" / "sim" / "1"
    request = NewSimulationRecordRunRequest(
        plan=create_simulation_record_plan(_manifest_for(("smoke",))),
        project_root=project,
        report_root=None,
        policy=SimulationRecordPolicy(),
        invocation_directory=invocation,
        admission=_admission(),
    )
    return SimulationRecord(_NoEdaExecutor()).run(request), invocation


def test_acceptance_result_reference_hashes_exact_stored_bytes(tmp_path: Path) -> None:
    outcome, invocation = _one_item_outcome(tmp_path)
    consumed = outcome.acceptance_facts.document["consumed_results"]
    reference = consumed[0]["result"]  # type: ignore[index]
    raw = (invocation / reference["path"]).read_bytes()  # type: ignore[index]
    assert raw.endswith(b"\n")
    assert reference["sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()  # type: ignore[index]


def test_public_simulation_record_outcome_replays_exact_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    outcome, _invocation = _one_item_outcome(tmp_path)
    assert outcome.complete is True
    assert outcome.recovery.completed
    assert outcome.recovery.interrupted == ()
    assert outcome.recovery.pending == ()

    publish_simulation_record_projections(tmp_path / "project", (outcome,))
    projection = outcome.manifest_path.parents[1] / "simulation.json"
    projection_document = json.loads(projection.read_bytes())
    assert projection_document["tests"][0]["error_tail"] == "synthetic no-EDA build failure"
    first_projection = projection.read_bytes()

    publish_simulation_record_projections(tmp_path / "project", (outcome,))
    assert projection.read_bytes() == first_projection
    # Publishing a projection records no state and no acceptance ledger.
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "logs").exists()


def test_simulation_record_projection_error_tail_rejects_malformed_detail(tmp_path: Path) -> None:
    outcome, _invocation = _one_item_outcome(tmp_path)
    malformed_observation = dict(outcome.observations[0], detail=[])
    malformed = replace(outcome, observations=(malformed_observation,))

    with pytest.raises(SimulationRecordIntegrityError, match="detail"):
        publish_simulation_record_projections(None, (malformed,))
