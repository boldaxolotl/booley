"""Shared immutable run and bound Scenario admission checks."""

from __future__ import annotations

from typing import Any


class RunSuiteError(ValueError):
    """A run contradicts its bound Scenario or Configured Scenario."""


def transitive_requirements(steps: dict[str, dict[str, Any]], step_id: str) -> set[str]:
    requirements = set()
    pending = list(steps[step_id].get("requires", []))
    while pending:
        requirement = pending.pop()
        if requirement in requirements:
            continue
        if requirement not in steps:
            raise RunSuiteError(f"{step_id}: unknown required Step {requirement}")
        requirements.add(requirement)
        pending.extend(steps[requirement].get("requires", []))
    return requirements


def validate_run_suite(
    run: dict[str, Any],
    results: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    scenario: dict[str, Any],
    configured: dict[str, Any],
    context: str,
) -> None:
    """Check the immutable run content against one frozen Scenario view."""
    if run["parameters"] != configured["parameters"]:
        raise RunSuiteError(f"{context}: parameters differ from Configured Scenario")
    selected = set(run["selected_check_ids"])
    expected = configured["expected_check_ids"]
    if expected is not None and selected != set(expected):
        raise RunSuiteError(f"{context}: selected Checks differ from Configured Scenario")
    steps = {item["id"]: item for item in scenario["steps"]}
    if not steps:
        raise RunSuiteError(f"{context}: bound Scenario has no Steps")
    check_steps = {
        check["id"]: step_id for step_id, step in steps.items() for check in step.get("checks", [])
    }
    if unknown := selected - check_steps.keys():
        raise RunSuiteError(
            f"{context}: selected Checks absent from bound Scenario: {sorted(unknown)}"
        )
    if unselected := {result["check_id"] for result in results} - selected:
        raise RunSuiteError(
            f"{context}: Check Results include unselected Checks: {sorted(unselected)}"
        )
    by_result = {result["check_result_id"]: result for result in results}
    for result in results:
        required_step = check_steps[result["check_id"]]
        if required_step != result["step_id"]:
            raise RunSuiteError(
                f"{context}: {result['check_result_id']}: Check {result['check_id']} "
                f"recorded Step {result['step_id']}; required Step {required_step}"
            )
        if result["status"] != "blocked":
            continue
        allowed = transitive_requirements(steps, result["step_id"])
        allowed.add(result["step_id"])
        for cause_id in result["caused_by_result_ids"]:
            if by_result[cause_id]["step_id"] not in allowed:
                raise RunSuiteError(
                    f"{context}: {result['check_result_id']}: blocked cause does not follow "
                    "the Scenario prerequisite direction"
                )
