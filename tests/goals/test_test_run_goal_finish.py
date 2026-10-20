"""Finish judges Goals that name Test Runs by every frozen Test Run (ADR 0072)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.test_runs import build_test_run
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from tests.goals.conftest import SIM_KEY, SIM_NAME, enter_goals, git, run_row, write_tests_toml
from tests.goals.test_finish import environment, request
from tests.goals.test_test_run_evidence import TWO_TESTS, A, B, _outcomes, _record

# Finish stages and verifies real Git objects, as tests/goals/test_finish.py does.
pytestmark = pytest.mark.timeout(120)


def _mode(layout: SimpleNamespace, goals: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
    layout.record = enter_goals(layout, goals, **kwargs)
    git(layout.main, "config", "user.name", "Goal Test")
    git(layout.main, "config", "user.email", "goal@test.invalid")
    return layout


def _refused(layout: SimpleNamespace, name: str) -> None:
    with pytest.raises(
        LifecycleError, match="must be met and fresh before finish: " + re.escape(name)
    ):
        finish_goal(request(layout), environment(layout))


@pytest.mark.timeout(150)
def test_finish_requires_every_frozen_test_run_passed(layout: SimpleNamespace) -> None:
    mode = _mode(layout, [{"family": "SIM", "target": "top"}], tests_toml=TWO_TESTS)
    _refused(mode, SIM_NAME)  # nothing recorded
    _record(mode, "run-1", [run_row(A)])
    _refused(mode, SIM_NAME)  # b@1 has not passed
    _record(mode, "run-2", [run_row(B)])

    result = finish_goal(request(mode), environment(mode))

    assert result["status"] == "finished"
    package = json.loads(Path(result["package"]).read_bytes())
    (row,) = package["goals"]
    assert _outcomes(mode) == {"a@1": "passed", "b@1": "passed"}
    # The campaign that passed a@1 contributed and is disclosed with the package.
    assert [item["sequence"] for item in row["original_observations"]] == [1]
    assert "Test Runs: a@1 passed, b@1 passed" in Path(result["summary"]).read_text()


def test_finish_refuses_a_sticky_failure(layout: SimpleNamespace) -> None:
    mode = _mode(layout, [{"family": "SIM", "target": "top"}], tests_toml=TWO_TESTS)
    _record(mode, "run-1", [run_row(A, "fail"), run_row(B)], met=False)
    _record(mode, "run-2", [run_row(A), run_row(B)])
    _refused(mode, SIM_NAME)


def test_finish_refuses_pending_and_empty_goals(layout: SimpleNamespace) -> None:
    pending = "sim_pass_top_run_6c61746572_d"
    goals = [
        {"family": "SIM", "target": "top"},
        {"family": "SIM", "target": "top", "test": "later"},
    ]
    mode = _mode(layout, goals, tests_toml=b'[top]\ntests = ["a"]\nskip = ["a"]\n')
    # A pass cannot meet a Goal that requires no Test Run, or whose Test does not exist yet.
    _record(mode, "run-1", [run_row(A)])
    _record(mode, "run-2", [run_row(build_test_run("later", seed=1))], key=pending)
    _refused(mode, f"{SIM_NAME}, SIM:top (all selected tests pass, test later)")


def test_finish_refuses_a_goal_whose_frozen_run_changed(layout: SimpleNamespace) -> None:
    variants = b'[top]\ntests = ["a"]\n[top.variants.a]\ndefault = "v"\nv = ["+n=1"]\n'
    mode = _mode(layout, [{"family": "SIM", "target": "top"}], tests_toml=variants)
    (frozen,) = mode.record.test_run_resolutions[SIM_KEY].test_runs
    _record(mode, "run-1", [run_row(frozen)])
    write_tests_toml(mode, variants.replace(b"+n=1", b"+n=2"))
    _refused(mode, SIM_NAME)
