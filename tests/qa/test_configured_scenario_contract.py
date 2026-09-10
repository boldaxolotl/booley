"""Protect the complete reviewed Configured Scenario semantics."""

import hashlib
import json
from pathlib import Path

from qa.validate import load_scenarios, resolved_configured_checks

ROOT = Path(__file__).resolve().parents[2] / "qa"


def membership_digest(values):
    return hashlib.sha256(("\n".join(sorted(values)) + "\n").encode()).hexdigest()


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
            "pre_run_requirements": configured["pre_run_requirements"],
            "exclusions": configured["exclusions"],
        }
        assert hashlib.sha256(
            json.dumps(semantics, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest() == expected["semantics"]
