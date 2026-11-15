"""Refusals that name the Coverage Record keep saying so."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from booley.flows.sim import record_retention
from booley.flows.sim.record import coverage_execution
from booley.flows.sim.record_retention import SimulationRecordRetentionError
from booley.goals.proposals import ProposalError
from booley.mcp import coverage_evidence, goal_waivers
from booley.mcp.coverage_evidence import CoverageEvidenceError
from booley.mcp.goal_waivers import GoalWaiverService
from booley.specialists import coverage_analysis, coverage_analyst
from booley.specialists.coverage_analysis import CoverageAnalysisError


def _any(**fields: object) -> Any:
    return SimpleNamespace(**fields)


def _service(root: Path) -> GoalWaiverService:
    service = object.__new__(GoalWaiverService)
    service.root = root
    return service


@pytest.mark.parametrize(
    ("detail", "message"),
    [
        ({"tests": [{}]}, "Coverage Record returned invalid test evidence"),
        ({"tests": ()}, "Coverage Record returned no test evidence"),
    ],
)
def test_coverage_execution_refuses_unusable_test_lists(
    detail: dict[str, object], message: str
) -> None:
    with pytest.raises(coverage_execution.SimulationRecordIntegrityError, match=message):
        coverage_execution._coverage_tests(_any(detail=detail))


def test_coverage_execution_refuses_an_invalid_test_item() -> None:
    with pytest.raises(
        coverage_execution.SimulationRecordIntegrityError,
        match="Coverage Record test evidence is invalid",
    ):
        coverage_execution._coverage_test_outcome({"name": "t", "verdict": "bogus"})


def test_native_target_pruning_refuses_a_workload_without_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    manifest = target / record_retention.RECORD_DIRECTORY / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(record_retention, "_empty_abandoned_reservation", lambda _root: False)
    monkeypatch.setattr(
        record_retention, "_read_object", lambda _path: {"flow": "sim", "targets": ["t"]}
    )
    monkeypatch.setattr(record_retention, "target_report_directory", lambda _root, _s: target)
    status = _any(manifest=_any(document={"workload": {"coverage": False}}))
    monkeypatch.setattr(
        record_retention,
        "_inspect_simulation_record",
        lambda _target, _record: _any(status=status, abandoned=False),
    )
    with pytest.raises(
        SimulationRecordRetentionError, match="--native-target has no native Coverage Record"
    ):
        record_retention._validate_native_pruning_eligibility(tmp_path, "t")


def test_coverage_evidence_refuses_a_disagreeing_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text('{"target_identity": "t", "files": {}}', encoding="utf-8")
    monkeypatch.setattr(
        coverage_evidence, "read_coverage_record", lambda _path: _any(coverage_record=object())
    )
    monkeypatch.setattr(coverage_evidence, "verified_source_snapshot", lambda _r, _s: None)
    with pytest.raises(
        CoverageEvidenceError, match="Verified source snapshot disagrees with Coverage Record"
    ):
        coverage_evidence._load_session("record", str(tmp_path), str(snapshot))


def test_goal_waivers_refuse_evidence_without_a_coverage_record_reference(tmp_path: Path) -> None:
    with pytest.raises(ProposalError, match="no authenticated Coverage Record reference"):
        _service(tmp_path)._resolved({})


def test_goal_waivers_refuse_a_differing_coverage_record_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    resolved = _any(loaded=_any(coverage_record=object()))
    monkeypatch.setattr(service, "_resolved", lambda _detail: resolved)
    monkeypatch.setattr(goal_waivers, "encode_coverage_record", lambda _r: {"evaluation": "a"})
    with pytest.raises(ProposalError, match="Coverage Record evaluation differs"):
        service._validate_coverage_record({"evaluation": "b"})


def test_goal_waivers_refuse_a_candidate_outside_the_offered_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    (tmp_path / "a.sv").write_bytes(b"x")
    item = _any(
        binding=_any(campaign_path="p", manifest_sha256="m", point_store_sha256="s"),
        proposal=_any(source="a.sv"),
        candidate_id="c",
        as_provisional=lambda: None,
    )
    resolved = _any(
        loaded=_any(
            coverage_record=object(),
            summary=_any(manifest_sha256="m", point_store=_any(sha256="s")),
        )
    )
    monkeypatch.setattr(goal_waivers, "criterion_from_evaluation", lambda _r: None)
    monkeypatch.setattr(
        goal_waivers, "evaluate_provisional_coverage", lambda *_a, **_k: _any(offered_ids=())
    )
    with pytest.raises(ProposalError, match="exact source/Coverage Record point"):
        service._validate_candidate(
            cast(Any, item), resolved, {"coverage_campaign_reference": {"path": "p"}}
        )


def test_goal_waivers_refuse_a_changed_selected_coverage_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.booley_dir = tmp_path
    service.record = cast(Any, object())
    proposal = _any(
        waiver=_any(
            policy=_any(to_json=lambda: {}),
            candidate_id="c",
            candidate_digest="d",
            campaign_reference=_any(to_json=lambda: {"path": "new"}),
        ),
        goal_key="g",
    )
    monkeypatch.setattr(goal_waivers, "rebased_policy", lambda _p: "same")
    monkeypatch.setattr(goal_waivers, "waiver_policy_fingerprint", lambda _r: "same")
    monkeypatch.setattr(service, "_candidate", lambda _id: _any(to_json=lambda: {}))
    monkeypatch.setattr(goal_waivers, "digest", lambda _v: "d")
    monkeypatch.setattr(goal_waivers, "GoalStore", lambda _d: object())
    monkeypatch.setattr(goal_waivers, "load_goal_state", lambda _s, _r: object())
    monkeypatch.setattr(
        goal_waivers,
        "selected_observations",
        lambda _r, _s, _d: {"g": {"detail": {"coverage_campaign_reference": {"path": "old"}}}},
    )
    with pytest.raises(ProposalError, match="selected Coverage Record changed since proposal"):
        service._revalidate(cast(Any, proposal))


def test_coverage_analysis_refuses_an_unusable_normalization() -> None:
    record = _any(
        collector=_any(native_format={"compatibility": "compatible"}),
        collection={"status": "complete"},
        normalization={"status": "failed"},
    )
    with pytest.raises(CoverageAnalysisError, match="Coverage Record normalization is not usable"):
        coverage_analysis._eligibility(record)


def test_coverage_analyst_has_no_prompt_of_its_own() -> None:
    specialist = object.__new__(coverage_analyst.CoverageAnalystSpecialist)
    with pytest.raises(NotImplementedError, match="validated Coverage Record seam"):
        specialist._build_prompt()


def test_coverage_analyst_refuses_a_non_numeric_invocation_id() -> None:
    record = _any(invocation={"id": "abc"})
    with pytest.raises(CoverageAnalysisError, match="invocation id must be numeric"):
        coverage_analyst.analyze_coverage_record(record, None, "")
