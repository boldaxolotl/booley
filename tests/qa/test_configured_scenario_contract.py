"""Protect the complete reviewed Configured Scenario semantics."""

import hashlib
import json
from pathlib import Path

import yaml
from qa.validate import (
    load_scenarios,
    resolved_configured_checks,
    resolved_pre_run_requirements,
)

ROOT = Path(__file__).resolve().parents[2] / "qa"


def membership_digest(values):
    return hashlib.sha256(("\n".join(sorted(values)) + "\n").encode()).hexdigest()


def scenario_step(scenario, step_id):
    return next(step for step in scenario["steps"] if step["id"] == step_id)


def test_every_configured_scenario_preserves_reviewed_semantics():
    contract = json.loads(
        Path(__file__).with_name("configured-scenario-contract.json").read_text()
    )["configured_scenarios"]
    scenarios = load_scenarios(ROOT)
    configured_scenarios = {
        configured["id"]: (scenario, configured)
        for scenario in scenarios.values()
        for configured in scenario["configured_scenarios"]
    }
    assert configured_scenarios.keys() == contract.keys()
    for configured_id, (scenario, configured) in configured_scenarios.items():
        expected = contract[configured_id]
        prefix = scenario["scenario_id"] + "."
        checks = [
            prefix + check for check in resolved_configured_checks(scenario, configured, ROOT)
        ]
        exclusion_checks = [prefix + item["check"] for item in configured["exclusions"]]
        assert configured["required"] == expected["required"]
        assert membership_digest(checks) == expected["checks"], configured_id
        assert membership_digest(exclusion_checks) == expected["exclusions"]
        semantics = {
            "parameters": configured["parameters"],
            "pre_run_requirements": resolved_pre_run_requirements(scenario, configured),
            "exclusions": configured["exclusions"],
        }
        assert (
            hashlib.sha256(
                json.dumps(semantics, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            == expected["semantics"]
        )


def test_origin_push_checks_exercise_an_external_git_server():
    coverage = yaml.safe_load((ROOT / "coverage.yaml").read_text())
    capability = next(
        item for item in coverage["capabilities"] if item["id"] == "SECURITY-RUNTIME-ISOLATION"
    )
    assert "outside-runtime control push" in capability["contract"]
    assert "Session Runtime attempts the same authorized transport" in capability["contract"]
    assert "container-local and local-path remotes are out of scope" in capability["contract"]

    scenarios = load_scenarios(ROOT)
    for scenario_id in (
        "picorv32-published-demo-continuity",
        "taxi-10g-mac-port-evolution",
        "opentitan-uart-clean-room-greenfield",
    ):
        step = scenario_step(
            scenarios[scenario_id], "inventory.SECURITY-RUNTIME-ISOLATION.origin-push-deny"
        )
        check = step["checks"][0]
        assert "outside-runtime control client can push" in check["stimulus"]
        assert "same authorized network transport" in check["stimulus"]
        assert "outside-runtime control push succeeds" in check["expected"]
        assert "blocked by its network boundary" in check["expected"]
        assert "external probe ref remains unchanged" in check["expected"]
        assert "successful outside-runtime control push" in check["evidence"]
        assert "identifying the network-policy denial" in check["evidence"]
        assert "local-path remotes cannot satisfy this Check" in check["evidence"]


def test_picorv32_ticket_destinations_include_applicable_project_setup():
    scenario = load_scenarios(ROOT)["picorv32-published-demo-continuity"]
    setup = scenario_step(scenario, "project.separate-repository")
    expected = setup["checks"][0]["expected"]
    assert "paired destination refs exist before Ticket Create" in expected
    assert "flows.fpga.enabled = true" in expected
    assert "configurations that exclude FPGA preserve their declared opt-out" in expected
    assert "Both repositories are clean" in expected

    for step_id in (
        "create.dhrystone-self-checking-cycle-contract.payload",
        "create.rv32-zbb-pcpi.payload",
    ):
        assert "project.separate-repository" in scenario_step(scenario, step_id)["requires"]

    zbb = scenario_step(scenario, "create.rv32-zbb-pcpi.payload")["checks"][0]
    assert "already-prepared immutable project-data destination" in zbb["expected"]
    assert "Configurations that exclude FPGA" in zbb["expected"]
