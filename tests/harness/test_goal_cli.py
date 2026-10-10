"""Goal status CLI selects worktree context and honors long/short overrides."""

from argparse import Namespace
from types import SimpleNamespace

import pytest

from booley.harness.booley import _cmd_goal
from tests.goals.conftest import (  # noqa: F401 — fixtures shared with Goal integration tests
    goal_mode,
    layout,
)


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("inside", [False, True])
def test_goal_status_cli(
    goal_mode: SimpleNamespace,  # noqa: F811 — shared pytest fixture uses its required name
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short: bool,
    inside: bool,
) -> None:
    monkeypatch.chdir(goal_mode.worktree if inside else goal_mode.main)
    args = Namespace(goal_command="status", short=short, long=not short)
    assert _cmd_goal(args, goal_mode.main) == 0
    text = capsys.readouterr().out
    assert goal_mode.record.id in text
    assert ("lint_clean_top" in text) is not short


@pytest.mark.parametrize("short", [False, True])
def test_goal_status_cli_from_subdirectory_is_identical(
    goal_mode: SimpleNamespace,  # noqa: F811 — shared pytest fixture
    monkeypatch,
    capsys,
    short,
):
    from tests.goals.test_status import publish

    publish(goal_mode)
    nested = goal_mode.worktree / "firmware"
    nested.mkdir()
    args = Namespace(goal_command="status", short=short, long=not short)
    monkeypatch.chdir(goal_mode.worktree)
    assert _cmd_goal(args, goal_mode.main) == 0
    root = capsys.readouterr().out
    monkeypatch.chdir(nested)
    assert _cmd_goal(args, goal_mode.main) == 0
    assert capsys.readouterr().out == root


def test_goal_abandon_cli_selects_once_and_retries_with_explicit_ids(
    goal_mode,  # noqa: F811 — shared pytest fixture
    monkeypatch,
    capsys,
):
    from booley.goals.store import GoalStore

    monkeypatch.chdir(goal_mode.worktree)
    args = Namespace(
        goal_command="abandon",
        record_id=None,
        operation_id=None,
        instruction_quote="Stop this Goal",
    )
    assert _cmd_goal(args, goal_mode.main) == 0
    text = capsys.readouterr().out
    assert "Branch and files retained" in text and "Retry IDs:" in text
    assert text.splitlines()[0].startswith("Retry IDs:")
    operation = text.split("--operation-id ", 1)[1].splitlines()[0].strip()
    args.record_id = goal_mode.record.id
    args.operation_id = operation
    assert _cmd_goal(args, goal_mode.main) == 0
    assert capsys.readouterr().out == text
    assert (
        GoalStore(goal_mode.control).load(goal_mode.record.id).end_instruction_quote
        == "Stop this Goal"
    )


@pytest.mark.parametrize("met", [False, True])
def test_goal_status_simulation_contract_explanation(goal_mode, monkeypatch, capsys, met):  # noqa: F811 — shared pytest fixture
    from booley.goals.recorder import GoalEvidenceRecorder
    from tests.goals.conftest import SIM_KEY, bind
    from tests.goals.test_recorder import _v1

    state = _v1(
        goal_mode,
        GoalEvidenceRecorder(bind(goal_mode)),
        SIM_KEY,
        met,
        {
            "required_tests": [] if met else ["smoke"],
            "passed_tests": [],
            "tests_passed": 1 if met else 0,
            "tests_total": 1,
        },
    )
    monkeypatch.chdir(goal_mode.worktree)
    assert _cmd_goal(Namespace(goal_command="status", long=True, short=False), goal_mode.main) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert f"{int(met)}/1 tests" in text
    if met:
        assert state.criteria[SIM_KEY].detail["goal_contract_violation"] in text
    else:
        assert "complete resolved suite" not in text
        assert "—" not in text
