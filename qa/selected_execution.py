#!/usr/bin/env python3
"""Render the read-only selected execution plan for a frozen Scenario Run."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    from . import triage
    from .run_suite import selected_execution
except ImportError:
    import triage
    from run_suite import selected_execution


def projection_for_run(run_root: Path, suite_root: Path) -> tuple[dict[str, Any], ...]:
    """Validate frozen bindings before projecting selected execution."""
    run_path = run_root / "run.json"
    run = triage.read_json(run_path)
    triage.validate_definition(run, "run-record.schema.json", "run", str(run_path))
    configured, scenario_files = triage.load_suite(suite_root)
    snapshot = {"configured_scenarios": configured, "scenario_files": scenario_files}
    scenario = triage.bound_scenario(snapshot, run["scenario_id"])
    matches = [
        item
        for item in configured
        if item["scenario_id"] == run["scenario_id"]
        and item["configured_scenario_id"] == run["configured_scenario_id"]
    ]
    if len(matches) != 1:
        raise triage.TriageError(f"{run_root}: Configured Scenario is absent from bound Scenario")
    return selected_execution(run, scenario, matches[0], str(run_root))


def render_projection(projection: tuple[dict[str, Any], ...]) -> str:
    """Render deterministic Scenario Operator navigation without unselected Checks."""
    lines = []
    for item in projection:
        role = "supporting Step" if item["supporting"] else "selected Step"
        lines.append(f"- {item['step_id']} [{role}]")
        action_lines = str(item["action"]).splitlines()
        lines.append(f"  - action: {action_lines[0]}")
        lines.extend(f"    {line}" for line in action_lines[1:])
        lines.extend(f"  - capture Check: {check}" for check in item["selected_check_ids"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--suite-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(render_projection(projection_for_run(args.run_root, args.suite_root)), end="")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
