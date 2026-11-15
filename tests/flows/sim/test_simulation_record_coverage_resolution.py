"""One Simulation Record outcome resolves its coverage reference once."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.acceptance import record_simulation_record_acceptance
from tests.criteria.ledger_support import FixtureRecorder
from tests.flows.sim.test_simulation_record_acceptance import _acceptance_endpoint, _sha


def test_one_outcome_resolves_its_coverage_reference_once_for_every_coverage_criterion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two coverage Criteria of one Target, and the projection, share one resolution."""
    from booley.flows.sim import acceptance
    from tests.flows.sim.simulation_record_test_support import _run_coverage_simulation_record
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    outcome, _store = _run_coverage_simulation_record(tmp_path, NativeExecution())
    invocation = outcome.manifest_path.parents[3]
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

    calls: dict[str, int] = dict.fromkeys(("resolve", "encode", "hash", "owner"), 0)

    def counted(name: str, original):
        def spy(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        return spy

    for name, attribute in (
        ("resolve", "resolve_coverage_record_reference"),
        ("encode", "encode_coverage_record_reference"),
        ("hash", "content_digest"),
        ("owner", "authenticate_coverage_record_owner"),
    ):
        monkeypatch.setattr(acceptance, attribute, counted(name, getattr(acceptance, attribute)))

    recorder = FixtureRecorder(
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
