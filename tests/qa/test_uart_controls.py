"""Evaluator admission requires hardware qualification beyond transport."""

import asyncio
import importlib
import json
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
        assert len(record["results"]) == 129


def test_successful_system_loopback_observations_are_serializable(monkeypatch):
    evaluator = Path(__file__).resolve().parents[2] / "qa/scenarios/uart/evaluator"
    monkeypatch.syspath_prepend(str(evaluator))
    exercises = importlib.import_module("exercises")

    class Driver:
        def __init__(self):
            self.tx = [1]
            self.observations = []

        async def configure(self, *args, **kwargs):
            return None

        async def write(self, *args, **kwargs):
            return None

        async def wait(self, cycles):
            self.tx.extend([1] * cycles)

        async def read(self, *args, **kwargs):
            return 0

        def expect(self, observed, expected, reason):
            assert observed == expected
            self.observations.append(
                {"observed": observed, "expected": expected, "reason": reason}
            )

    async def no_recovery(*args, **kwargs):
        return None

    monkeypatch.setattr(exercises, "parity", no_recovery)
    driver = Driver()
    asyncio.run(exercises.loop(driver, {"mode": "system", "payload": [0x55], "nco": 0x4000}))
    json.dumps(driver.observations)


def test_corrupt_control_sweep_retains_every_subcase(monkeypatch):
    evaluator = Path(__file__).resolve().parents[2] / "qa/scenarios/uart/evaluator"
    monkeypatch.syspath_prepend(str(evaluator))
    exercises = importlib.import_module("exercises")
    calls = []

    class Driver:
        async def reset(self):
            calls.append("reset")

    async def fail(_driver, parameters):
        calls.append(parameters["name"])
        raise exercises.CircuitMismatchError(parameters["name"])

    steps = [exercises.ControlStep(name, fail, {"name": name}) for name in ["first", "second"]]
    with pytest.raises(exercises.CircuitMismatchError, match=r"first.*second"):
        asyncio.run(exercises.control_sweep(Driver(), steps))
    assert calls == ["first", "reset", "second", "reset"]
