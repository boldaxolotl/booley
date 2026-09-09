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


@pytest.mark.parametrize("changed", [False, True])
def test_control_publication_requires_stable_evaluator(monkeypatch, tmp_path, changed):
    evaluator = Path(__file__).resolve().parents[2] / "qa/scenarios/uart/evaluator"
    monkeypatch.syspath_prepend(str(evaluator))
    controls = importlib.import_module("controls")
    identity = iter(["before", "after" if changed else "before"])
    monkeypatch.setattr(controls, "evaluator_identity", lambda: next(identity))
    monkeypatch.setattr(controls, "launch", lambda *args: 0)
    # Hardware verdict correctness is checked by the real simulator controls;
    # this regression isolates atomic publication at the controls entry point.
    monkeypatch.setattr(
        controls,
        "case_result",
        lambda case, build, directory, deadline: {
            "status": "fail" if directory.name.endswith("-corrupt") else "pass"
        },
    )
    destination = tmp_path / "controls"
    if changed:
        with pytest.raises(RuntimeError, match="Evaluator changed"):
            controls.controls(destination)
        assert not (destination / "controls.json").exists()
        assert (destination / "mmio-positive/transport.sv").is_file()
    else:
        controls.controls(destination)
        import json

        record = json.loads((destination / "controls.json").read_text())
        assert record["evaluator_sha256"] == "before"
        assert len(record["results"]) == 81
