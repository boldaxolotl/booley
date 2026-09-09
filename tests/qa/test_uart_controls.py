"""Evaluator admission requires hardware qualification beyond transport."""

import importlib
from pathlib import Path

import pytest


def test_transport_only_evidence_cannot_admit_candidate(monkeypatch):
    evaluator = Path(__file__).resolve().parents[2] / "qa/scenarios/uart/evaluator"
    monkeypatch.syspath_prepend(str(evaluator))
    runner = importlib.import_module("run")
    results = {
        f"{family}-{variant}": {"status": status}
        for family in ["mmio", "serial"]
        for variant, status in [("positive", "pass"), ("corrupt", "fail"), ("restored", "pass")]
    }
    with pytest.raises(ValueError, match="Complete"):
        runner.verify_controls(
            {"evaluator_sha256": runner.evaluator_identity(), "results": results}
        )
