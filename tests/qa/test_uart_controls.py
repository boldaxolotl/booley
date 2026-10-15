"""Evaluator admission requires hardware qualification beyond transport."""

import asyncio
import importlib
import json
from pathlib import Path

import pytest


def test_transport_only_evidence_cannot_admit_candidate(monkeypatch):
    evaluator = Path(__file__).resolve().parents[2] / "qa/missions/uart/evaluator"
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
    evaluator = Path(__file__).resolve().parents[2] / "qa/missions/uart/evaluator"
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
        record = json.loads((destination / "controls.json").read_text())
        assert record["evaluator_sha256"] == "before"
        # One simulator run per control per variant (positive/corrupt/restored).
        expected_runs = len(controls.control_cases()) * len(controls.VARIANTS)
        assert len(record["results"]) == expected_runs


def test_successful_system_loopback_observations_are_serializable(monkeypatch):
    evaluator = Path(__file__).resolve().parents[2] / "qa/missions/uart/evaluator"
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


def test_nco_zero_observations_are_serializable(monkeypatch):
    """The NCO=0 stall oracle once stored a set, so observe.py could never publish it."""
    evaluator = Path(__file__).resolve().parents[2] / "qa/missions/uart/evaluator"
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

        def expect(self, observed, expected, reason):
            assert observed == expected
            self.observations.append(
                {"observed": observed, "expected": expected, "reason": reason}
            )

    driver = Driver()
    asyncio.run(exercises.baud(driver, {"nco": 0, "payload": [0x55, 0xAA]}))
    assert json.loads(json.dumps(driver.observations))[0]["observed"] == [1]


def test_nco_zero_control_is_admitted(monkeypatch):
    evaluator = Path(__file__).resolve().parents[2] / "qa/missions/uart/evaluator"
    monkeypatch.syspath_prepend(str(evaluator))
    control_cases = importlib.import_module("control_cases")
    nco_zero = [item for item in control_cases.control_cases() if item[0] == "nco-zero"]
    assert len(nco_zero) == 1
    _, source, mutation, stimulus = nco_zero[0]
    assert stimulus["operation"] == "baud" and stimulus["parameters"]["nco"] == 0
    fixture = (evaluator / "controls" / f"{source}.sv").read_text()
    assert f"CORRUPT_{mutation.upper()} = 0;" in fixture
    assert {f"nco-zero-{variant}" for variant in control_cases.VARIANTS} <= set(
        control_cases.expected_controls()
    )


def test_corrupt_control_sweep_retains_every_subcase(monkeypatch):
    evaluator = Path(__file__).resolve().parents[2] / "qa/missions/uart/evaluator"
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


def test_override_with_tx_enabled_control_is_admitted(monkeypatch):
    """A design honoring OVRD only with CTRL.TX clear must fail a dedicated control."""
    evaluator = Path(__file__).resolve().parents[2] / "qa/missions/uart/evaluator"
    monkeypatch.syspath_prepend(str(evaluator))
    control_cases = importlib.import_module("control_cases")
    matches = [item for item in control_cases.control_cases() if item[0] == "override-tx-enabled"]
    assert len(matches) == 1
    _, source, mutation, stimulus = matches[0]
    assert stimulus["operation"] == "override" and stimulus["parameters"]["mode"] == "release"
    fixture = (evaluator / "controls" / f"{source}.sv").read_text()
    assert f"CORRUPT_{mutation.upper()} = 0;" in fixture


@pytest.mark.parametrize("mode", ["low", "high", "release"])
def test_only_release_overrides_with_tx_enabled(monkeypatch, mode):
    """Release mode drives OVRD low with TX enabled before the TX-disabled payload phase."""
    evaluator = Path(__file__).resolve().parents[2] / "qa/missions/uart/evaluator"
    monkeypatch.syspath_prepend(str(evaluator))
    exercises = importlib.import_module("exercises")
    writes = []

    class Driver:
        def __init__(self):
            self.tx = [1]
            self.control = 0

        async def configure(self, control=3, **kwargs):
            self.control = control

        async def write(self, address, value):
            writes.append((address, value, self.control))

        async def wait(self, cycles):
            ovrd = [value for address, value, _ in writes if address == 0x28][-1]
            self.tx.extend([(ovrd >> 1) & 1 if ovrd & 1 else 1] * cycles)

        async def read(self, *args, **kwargs):
            return 0

        def expect(self, observed, expected, reason):
            assert observed == expected, reason

    async def no_drain(*args, **kwargs):
        return None

    monkeypatch.setattr(exercises, "drain", no_drain)
    asyncio.run(exercises.override(Driver(), {"mode": mode, "payload": [0x55]}))
    enabled_overrides = [w for w in writes if w[0] == 0x28 and w[1] & 1 and w[2] & 1]
    assert enabled_overrides == ([(0x28, 1, 3)] if mode == "release" else [])
