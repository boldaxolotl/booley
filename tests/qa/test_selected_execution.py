"""The selected-execution command validates and renders frozen run bindings."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from tests.qa.test_triage import write_json, write_run, write_suite

from qa import selected_execution, triage


def selected_run(tmp_path: Path) -> tuple[Path, Path]:
    suite = write_suite(tmp_path, [("required", True)])
    scenario_path = suite / "scenarios/sample/scenario.yaml"
    scenario = yaml.safe_load(scenario_path.read_text())
    scenario["steps"] = [
        {
            "id": "setup",
            "action": "Prepare the environment.",
            "requires": [],
            "checks": [{"id": "setup-check"}],
        },
        {
            "id": "mixed",
            "action": "Run the selected behavior.\nKeep its evidence.",
            "requires": ["setup"],
            "checks": [{"id": "wanted"}, {"id": "unselected"}],
        },
    ]
    scenario_path.write_text(yaml.safe_dump(scenario))
    root = write_run(tmp_path, "run-1", "required", [], seal=False)
    run = triage.read_json(root / "run.json")
    run["selected_check_ids"] = ["wanted"]
    write_json(root / "run.json", run)
    return root, suite


def test_projection_for_run_renders_deterministically_without_unselected_checks(tmp_path):
    run_root, suite = selected_run(tmp_path)

    projection = selected_execution.projection_for_run(run_root, suite)

    rendered = selected_execution.render_projection(projection)
    assert rendered == (
        "- setup [supporting Step]\n"
        "  - action: Prepare the environment.\n"
        "- mixed [selected Step]\n"
        "  - action: Run the selected behavior.\n"
        "    Keep its evidence.\n"
        "  - capture Check: wanted\n"
    )
    assert "unselected" not in rendered


def test_selected_execution_cli_matches_api(tmp_path):
    run_root, suite = selected_run(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "qa/selected_execution.py",
            str(run_root),
            "--suite-root",
            str(suite),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == selected_execution.render_projection(
        selected_execution.projection_for_run(run_root, suite)
    )


def test_projection_for_run_rejects_invalid_run_and_frozen_suite_bindings(tmp_path):
    run_root, suite = selected_run(tmp_path)
    run = triage.read_json(run_root / "run.json")
    run["selected_check_ids"] = ["missing"]
    write_json(run_root / "run.json", run)

    with pytest.raises(triage.TriageError, match="selected Checks absent"):
        selected_execution.projection_for_run(run_root, suite)

    run.pop("deadline_at")
    write_json(run_root / "run.json", run)
    with pytest.raises(triage.TriageError, match="deadline_at"):
        selected_execution.projection_for_run(run_root, suite)
