"""One Simulation Campaign outcome resolves its coverage reference once."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.acceptance import record_simulation_record_acceptance
from tests.criteria.ledger_support import FixtureRecorder
from tests.flows.sim.test_simulation_record_acceptance import _acceptance_endpoint, _sha


def test_one_outcome_resolves_its_coverage_reference_once_for_every_coverage_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two coverage Goals of one Target, and the projection, share one resolution."""
    from booley.flows import frozen_test_runs
    from booley.flows.sim import acceptance
    from booley.flows.sim.record.test_run_report import evidence_test_run_rows
    from booley.flows.test_runs import build_test_run
    from tests.flows.sim.simulation_record_test_support import _run_coverage_simulation_record
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    outcome, _store = _run_coverage_simulation_record(tmp_path, NativeExecution())
    invocation = outcome.manifest_path.parents[3]
    rows = evidence_test_run_rows(outcome)
    runs = tuple(
        build_test_run(row["test"], variant=row["variant"], seed=row["seed"]) for row in rows
    )
    selector = str(outcome.target["selector"])
    identity = f"{outcome.target['vlnv']}#{outcome.target['name']}"
    keys = ("coverage_first", "coverage_second")

    state = DevelopmentState.load(tmp_path / "state.json")
    state.slug = "ticket"
    state.ticket_type = "implementation"
    state.init_criteria(
        dict.fromkeys(keys, True),
        criterion_params={
            key: {"target": identity, "_target_selector": selector, "metrics": {"line": {}}}
            for key in keys
        },
        strict=True,
    )
    state.save()

    class GoalModeRecorder(FixtureRecorder):
        def frozen_test_runs(self):
            return frozen_test_runs.FrozenTestRuns(
                tuple(
                    frozen_test_runs.GoalTestRuns(key, "coverage", selector, runs) for key in keys
                )
            )

    calls: dict[str, int] = dict.fromkeys(("resolve", "encode", "hash", "owner"), 0)

    def counted(name: str, original):
        def spy(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        return spy

    for name, attribute in (
        ("resolve", "resolve_coverage_campaign_reference"),
        ("encode", "encode_coverage_campaign_reference"),
        ("hash", "content_digest"),
        ("owner", "authenticate_coverage_campaign_owner"),
    ):
        monkeypatch.setattr(acceptance, attribute, counted(name, getattr(acceptance, attribute)))

    recorder = GoalModeRecorder(
        log_dir=tmp_path / "logs", identity={"generation": "d" * 32, "authored_sha256": "e" * 64}
    )
    endpoint = _acceptance_endpoint(state, recorder, invocation)
    record_simulation_record_acceptance(endpoint, (outcome,))

    public = outcome.manifest_path.parents[1] / "coverage.json"
    raw = public.read_bytes()
    persisted = DevelopmentState.load(tmp_path / "state.json")
    for key in keys:
        reference = persisted.criteria[key].detail["coverage_campaign_reference"]
        assert (reference["bytes"], reference["sha256"]) == (len(raw), _sha(raw))
    assert "coverage_record" in json.loads((public.parent / "simulation.json").read_bytes())
    assert calls == {"resolve": 1, "encode": 1, "hash": 1, "owner": 1}
