"""One Simulation Record outcome resolves its coverage reference once."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.flows.sim.acceptance import publish_simulation_record_projections


def test_one_outcome_resolves_its_coverage_reference_once_for_its_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The projection resolves the outcome's public Coverage Record reference once."""
    from booley.flows.sim import acceptance
    from tests.flows.sim.simulation_record_test_support import _run_coverage_simulation_record
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    outcome, _store = _run_coverage_simulation_record(tmp_path, NativeExecution())

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

    publish_simulation_record_projections(tmp_path, (outcome,))

    public = outcome.manifest_path.parents[1] / "coverage.json"
    assert "coverage_record" in json.loads((public.parent / "simulation.json").read_bytes())
    assert calls == {"resolve": 1, "encode": 1, "hash": 1, "owner": 1}
