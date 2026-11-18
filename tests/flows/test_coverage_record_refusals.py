"""Refusals that name the Coverage Record keep saying so."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.sim.record import coverage_execution
from booley.mcp import coverage_evidence
from booley.mcp.coverage_evidence import CoverageEvidenceError
from booley.specialists import coverage_analysis, coverage_analyst
from booley.specialists.coverage_analysis import CoverageAnalysisError


def _any(**fields: object) -> Any:
    return SimpleNamespace(**fields)


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
        # Private seam: it runs only inside a Coverage Record execution's publication.
        coverage_execution._coverage_tests(_any(detail=detail))


def test_coverage_execution_refuses_an_invalid_test_item() -> None:
    with pytest.raises(
        coverage_execution.SimulationRecordIntegrityError,
        match="Coverage Record test evidence is invalid",
    ):
        # Private seam: it runs only inside a Coverage Record execution's publication.
        coverage_execution._coverage_test_outcome({"name": "t", "verdict": "bogus"})


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
        # Private seam: it is reachable publicly only behind query_active_coverage_evidence's
        # session cache and audit files.
        coverage_evidence._load_session("record", str(tmp_path), str(snapshot))


def test_coverage_analysis_refuses_an_unusable_normalization() -> None:
    record = _any(
        collector=_any(native_format={"compatibility": "compatible"}),
        collection={"status": "complete"},
        normalization={"status": "failed"},
    )
    with pytest.raises(CoverageAnalysisError, match="Coverage Record normalization is not usable"):
        # Private seam: analyze_coverage_record first encodes the whole Coverage Record
        # (_analysis_envelope) before it judges eligibility.
        coverage_analysis._eligibility(record)


def test_coverage_analyst_has_no_prompt_of_its_own() -> None:
    specialist = object.__new__(coverage_analyst.CoverageAnalystSpecialist)
    with pytest.raises(NotImplementedError, match="validated Coverage Record seam"):
        # Private seam: the abstract-method override no public path calls; _run replaces it.
        specialist._build_prompt()


def test_coverage_analyst_refuses_a_non_numeric_invocation_id() -> None:
    record = _any(invocation={"id": "abc"})
    with pytest.raises(CoverageAnalysisError, match="invocation id must be numeric"):
        coverage_analyst.analyze_coverage_record(record, None, "")
