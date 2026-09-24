"""Selected execution is the navigation authority for a Scenario Run."""

from pathlib import Path

from qa.validate import load_scenarios, resolved_configured_checks

from qa import run_suite


def binding(selected):
    return {
        "parameters": {},
        "expected_check_ids": sorted(selected),
    }


def test_selected_execution_suppresses_unselected_branch_and_mixed_checks():
    scenario = {
        "steps": [
            {"id": "setup", "requires": [], "checks": [{"id": "setup-check"}]},
            {
                "id": "selected",
                "requires": ["setup"],
                "checks": [{"id": "wanted"}, {"id": "paid-license"}],
            },
            {
                "id": "unselected-branch",
                "requires": [],
                "checks": [{"id": "paid-only"}],
            },
        ]
    }
    run = {"parameters": {}, "selected_check_ids": ["wanted"]}

    projection = run_suite.selected_execution(run, scenario, binding(["wanted"]), "run-1")

    assert projection == (
        {"step_id": "setup", "supporting": True, "selected_check_ids": ()},
        {"step_id": "selected", "supporting": False, "selected_check_ids": ("wanted",)},
    )


def test_picorv32_claude_cli_projection_excludes_paid_license_capture_points():
    root = Path(__file__).resolve().parents[2] / "qa"
    scenario = load_scenarios(root)["picorv32-published-demo-continuity"]
    configured = next(
        item
        for item in scenario["configured_scenarios"]
        if item["id"] == "picorv32-ubuntu-claude-cli"
    )
    selected = resolved_configured_checks(scenario, configured, root)
    run = {"parameters": configured["parameters"], "selected_check_ids": selected}

    projection = run_suite.selected_execution(
        run,
        scenario,
        {"parameters": configured["parameters"], "expected_check_ids": sorted(selected)},
        "picorv32-ubuntu-claude-cli",
    )

    projected_checks = {check_id for step in projection for check_id in step["selected_check_ids"]}
    assert projected_checks == set(selected)
    assert all("license" not in check_id for check_id in projected_checks)
