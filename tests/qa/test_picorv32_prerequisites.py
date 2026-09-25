"""Keep the PicoRV32 Ticket dependency chain aligned with blocked causes."""

from pathlib import Path

import yaml
from qa.triage import transitive_requirements

SCENARIO_PATH = Path(__file__).resolve().parents[2] / "qa/scenarios/picorv32/scenario.yaml"


def test_ticket1_failure_is_a_prerequisite_of_dependent_work():
    scenario = yaml.safe_load(SCENARIO_PATH.read_text())
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


def test_stealth_symlink_check_uses_scoped_verifier():
    scenario = yaml.safe_load(SCENARIO_PATH.read_text())
    step = next(item for item in scenario["steps"] if item["id"] == "stealth.native-no-symlink")
    check = step["checks"][0]

    assert "stealth-core-links" in step["action"]
    assert "projected and native `.core` entries" in check["expected"]
    assert "root guidance links are out of scope" in check["expected"]
    assert {asset["path"] for asset in step["assets"]} == {"fixture_validation.py"}
