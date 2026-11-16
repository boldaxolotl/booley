"""Per-Test-Run rows in ``simulation.json`` (ADR 0072)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.acceptance import _simulation_record_projection
from booley.flows.sim.record import AcceptanceFacts, decode_simulation_projection
from booley.flows.sim.record.codec import (
    SimulationRecordIntegrityError,
    encode_simulation_record_manifest,
)
from booley.flows.sim.record.projection import ProjectionTrust
from booley.flows.sim.record.test_run_report import evidence_test_run_rows
from booley.flows.test_runs import build_test_run, test_run_document
from tests.flows.sim.test_simulation_record_test_runs import (
    _FAULT,
    _SOAK,
    _hdl_plan,
    _run_simulation_record,
)

_RESET = build_test_run("reset", seed=1)
_COUNT = build_test_run("count", variant="soak", seed=42, args=("+iters=9",))


def _outcome(tmp_path: Path, *runs):
    return _run_simulation_record(tmp_path, _hdl_plan(tmp_path, tuple((run,) for run in runs)))


def _rows(*runs, classified=True) -> list[dict]:
    # The executor of these tests runs no simulator: every Test Run fails.
    return [
        {
            **test_run_document(run),
            "grade": "fail",
            **(
                {"functional_failure": False, "failure_execution": "blocked_by_build"}
                if classified
                else {}
            ),
        }
        for run in runs
    ]


def test_evidence_rows_are_the_recorded_test_runs_of_the_simulation_record(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET, _SOAK, _FAULT)
    assert evidence_test_run_rows(outcome) == _rows(_RESET, _SOAK, _FAULT)


def test_evidence_rows_refuse_a_manifest_the_facts_do_not_name(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET)
    (tmp_path / "other").mkdir()
    other = _hdl_plan(tmp_path / "other", ((_SOAK,),))
    outcome.manifest_path.write_bytes(encode_simulation_record_manifest(other.manifest))
    with pytest.raises(SimulationRecordIntegrityError, match="not the one its acceptance"):
        evidence_test_run_rows(outcome)
    outcome.manifest_path.unlink()
    with pytest.raises(SimulationRecordIntegrityError, match="cannot be read"):
        evidence_test_run_rows(outcome)


def test_projection_v3_carries_a_row_per_test_run_and_decodes(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET, _COUNT)
    projection = _simulation_record_projection(outcome)

    assert projection["$schema"] == "booley.simulation-projection/v4"
    assert projection["test_runs"] == _rows(_RESET, _COUNT, classified=False)
    assert [
        (row["name"], row["test_run"], row["variant"], row["seed"], row["fingerprint"])
        for row in projection["tests"]
    ] == [
        ("reset", "reset@1", None, 1, _RESET.fingerprint),
        ("count", "count+soak@42", "soak", 42, _COUNT.fingerprint),
    ]
    document = json.loads(json.dumps({**projection, "complete": True}))
    decoded = decode_simulation_projection(document, coverage_expected=False)
    assert decoded.trust is ProjectionTrust.AUTHENTICATED


def _v3_document(tmp_path: Path) -> dict:
    projection = _simulation_record_projection(_outcome(tmp_path, _RESET))
    return json.loads(json.dumps({**projection, "complete": True}))


@pytest.mark.parametrize(
    ("alter", "message"),
    [
        (lambda document: document.pop("test_runs"), "fields are not exact"),
        (lambda document: document.update(test_runs={}), "test_runs must be a list"),
        (lambda document: document["test_runs"][0].update(grade="maybe"), "no valid grade"),
        (lambda document: document["test_runs"][0].pop("grade"), "no valid grade"),
        (lambda document: document["test_runs"][0].update(seed=2), "disagrees with its own"),
        (lambda document: document["test_runs"][0].pop("args"), "must hold exactly"),
    ],
)
def test_projection_v3_refuses_an_inexact_test_run_row(tmp_path: Path, alter, message):
    document = _v3_document(tmp_path)
    alter(document)
    with pytest.raises(ValueError, match=message):
        decode_simulation_projection(document, coverage_expected=False)


def test_evidence_rows_come_from_the_authenticated_manifest(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET, _COUNT)
    outcome.manifest_path.write_bytes(b"{}\n")
    with pytest.raises(SimulationRecordIntegrityError):
        evidence_test_run_rows(outcome)


def test_evidence_rows_refuse_an_observation_whose_fingerprint_is_not_recorded(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET)
    document = dict(outcome.acceptance_facts.document)
    document["observations"] = [
        {**observation, "test_run_fingerprint": "sha256:" + "0" * 64}
        for observation in document["observations"]
    ]
    forged = replace(outcome, acceptance_facts=AcceptanceFacts(document))
    with pytest.raises(
        SimulationRecordIntegrityError, match="no recorded Test Run with its fingerprint"
    ):
        evidence_test_run_rows(forged)
