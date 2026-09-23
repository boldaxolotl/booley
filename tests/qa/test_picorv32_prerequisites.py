"""Keep the PicoRV32 Ticket dependency chain aligned with blocked causes."""

from pathlib import Path

import pytest
import yaml
from qa.triage import transitive_requirements

from qa import validate

SCENARIO_PATH = Path(__file__).resolve().parents[2] / "qa/scenarios/picorv32/scenario.yaml"


def load_scenario():
    return yaml.safe_load(SCENARIO_PATH.read_text())


def test_ticket1_failure_is_a_prerequisite_of_dependent_work():
    scenario = load_scenario()
    steps = {step["id"]: step for step in scenario["steps"]}
    dependent_ids = {
        step_id
        for step_id in steps
        if step_id.startswith(("ticket1.", "guard.", "refresh.", "ticket2."))
    } | {"final.combined-regression", "coverage.criterion-families"}
    dependent_ids.remove("ticket1.scope")

    for step_id in dependent_ids:
        assert "ticket1.scope" in transitive_requirements(steps, step_id), step_id

    assert "ticket1.scope" not in transitive_requirements(
        steps, "inventory.SIMULATION-FLOW.sim-design-fail"
    )
    assert "guard.negative-result" not in transitive_requirements(steps, "guard.restore-rerun")
    assert "guard.restore-rerun" not in transitive_requirements(steps, "guard.discard")


def test_firmware_precedes_and_is_a_prerequisite_of_doctor():
    scenario = load_scenario()
    steps = {step["id"]: step for step in scenario["steps"]}

    assert steps["baseline.firmware"]["phase"] == "prepare"
    assert steps["baseline.firmware"]["requires"] == ["project.separate-repository"]
    assert steps["doctor.first-product-exercise"]["requires"] == ["baseline.firmware"]
    assert set(steps["doctor.plain"]["requires"]) == {
        "baseline.firmware",
        "doctor.first-product-exercise",
    }
    assert steps["doctor.deep"]["requires"] == ["doctor.plain"]
    assert steps["doctor.plain-recheck"]["requires"] == ["doctor.deep"]
    assert "baseline.firmware" in transitive_requirements(steps, "doctor.plain-recheck")

    core = next(item for item in scenario["check_sets"] if item["id"] == "picorv32-core")
    assert core["checks"].index("baseline.firmware") < core["checks"].index("doctor.plain")


def test_picorv32_contract_rejects_doctor_without_firmware_dependency():
    scenario = load_scenario()
    doctor = next(step for step in scenario["steps"] if step["id"] == "doctor.plain")
    doctor["requires"].remove("baseline.firmware")

    with pytest.raises(ValueError, match="PicoRV32 firmware/Doctor dependency"):
        validate.validate_picorv32_contract(scenario, SCENARIO_PATH)
