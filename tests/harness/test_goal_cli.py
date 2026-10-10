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


PREFIX = "No Goal Mode is active here. Active Goal Modes in this Project:\n"


def second_goal(mode, monkeypatch):
    from tests.goals.conftest import enter_goals, git, write_project_files

    second = mode.control / "worktrees" / "other"
    git(mode.main, "worktree", "add", "-q", "-b", "other", str(second))
    write_project_files(second / ".booley_project")
    other = SimpleNamespace(main=mode.main, control=mode.control, worktree=second)
    with monkeypatch.context() as context:
        context.setattr("booley.goals.entry.compact_utc_now", lambda: "20261007T120000Z")
        other.record = enter_goals(other)
    return other


@pytest.mark.parametrize("detail", [None, True, False])
@pytest.mark.parametrize("caller", ["occupant", "nested", "main", "outside", "abandoned"])
@pytest.mark.parametrize("multiple", [False, True])
def test_goal_status_complete_bytes(
    goal_mode,  # noqa: F811 — shared fixture
    tmp_path,
    monkeypatch,
    capsys,
    detail,
    caller,
    multiple,
):
    from booley.goals.model import GoalState
    from booley.goals.status import status_views
    from booley.goals.store import GoalStore
    from tests.goals.conftest import update_record
    from tests.goals.test_format import baseline_render_status

    if multiple or caller == "abandoned":
        second_goal(goal_mode, monkeypatch)
    if caller == "abandoned":
        update_record(goal_mode, state=GoalState.ABANDONED)
    location = goal_mode.worktree
    if caller == "nested":
        location = location / "nested"
        location.mkdir()
    elif caller == "main":
        location = goal_mode.main
    elif caller == "outside":
        location = tmp_path / "outside"
        location.mkdir()
    views = status_views(GoalStore(goal_mode.control), location)
    expected = baseline_render_status(views, short=detail)
    if caller in {"main", "outside", "abandoned"}:
        expected = PREFIX + expected
    monkeypatch.chdir(location)
    args = Namespace(goal_command="status", short=detail is True, long=detail is False)
    assert _cmd_goal(args, goal_mode.main) == 0
    captured = capsys.readouterr()
    assert captured.out == expected + "\n"
    assert captured.err == ""


@pytest.mark.parametrize("detail", [None, True, False])
@pytest.mark.parametrize("state", ["abandoned", "finished", "failed"])
@pytest.mark.parametrize("caller", ["occupant", "main", "outside"])
def test_no_occupying_goal_bytes(
    goal_mode,  # noqa: F811 — shared fixture
    tmp_path,
    monkeypatch,
    capsys,
    detail,
    state,
    caller,
):
    from booley.goals.model import GoalState
    from tests.goals.conftest import update_record

    if state == "finished":
        update_record(goal_mode, state=GoalState.FINISHING)
    update_record(goal_mode, state=GoalState(state))
    location = goal_mode.worktree if caller == "occupant" else goal_mode.main
    if caller == "outside":
        location = tmp_path / "outside"
        location.mkdir()
    monkeypatch.chdir(location)
    assert (
        _cmd_goal(
            Namespace(goal_command="status", short=detail is True, long=detail is False),
            goal_mode.main,
        )
        == 0
    )
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("No active Goal Mode.\n", "")


@pytest.mark.parametrize("error", ["corrupt", "identity", "unrelated"])
def test_goal_status_error_bytes(goal_mode, tmp_path, monkeypatch, capsys, error):  # noqa: F811 — shared fixture
    from booley.goals.paths import record_paths
    from booley.goals.status import status_views
    from booley.goals.store import REPOSITORY_ID_FILE, GoalStore, GoalStoreError
    from tests.goals.conftest import git

    location = goal_mode.worktree
    if error == "corrupt":
        record_paths(goal_mode.control, goal_mode.record.id).record_file.write_bytes(b"{bad")
    elif error == "identity":
        (goal_mode.main / ".git" / REPOSITORY_ID_FILE).unlink()
    else:
        location = tmp_path / "unrelated"
        location.mkdir()
        git(location, "init", "-q")
        location = location / "nested"
        location.mkdir()
    with pytest.raises(GoalStoreError) as failure:
        status_views(GoalStore(goal_mode.control), location)
    monkeypatch.chdir(location)
    assert (
        _cmd_goal(Namespace(goal_command="status", short=False, long=False), goal_mode.main) == 2
    )
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", f"ERROR: {failure.value}\n")
