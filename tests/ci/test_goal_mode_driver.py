"""Fail-closed CI Goal driver checks using a cheap fake MCP session."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / ".github/scripts"))
import goal_mode_driver as driver

_ID = "ci-demo-20261009T120000Z"
_LINT = {"family": "lint", "target": "lint_core"}
_ENTER = f"Goal Mode {_ID} entered in /fixture.\nGoals (all unmet):\n- lint_clean_lint_core"
_STATUS = f"{_ID} (active) · 1/1 met\nlint_clean_lint_core met clean"


def _reply(text, *, error=False):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], isError=error)


class FakeSession:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return next(self.replies)


def _exercise(replies, *, goals=(_LINT,), readiness=False):
    session = FakeSession(replies)
    result = asyncio.run(driver.exercise(session, Path("/fixture"), goals, readiness=readiness))
    return result, session.calls


def test_roundtrip_requires_enter_lint_fresh_status_and_finish() -> None:
    result, calls = _exercise(
        [
            _reply(_ENTER),
            _reply("EXIT_CODE: 0"),
            _reply(_STATUS),
            _reply(f"Goal Mode {_ID} finished."),
        ]
    )
    assert result["state"] == "finished"
    assert [name for name, _ in calls] == ["goal_enter", "lint", "goal_status", "goal_finish"]
    assert all(arguments["work_dir"] == "/fixture" for _, arguments in calls)
    finish = calls[-1][1]
    assert finish["record_id"] == _ID and finish["operation_id"] and finish["summary"]
    assert "abandon" not in finish and "instruction_quote" not in finish


def test_readiness_lists_full_contract_and_leaves_unmet_goals_active() -> None:
    goals = driver.load_contract(Path(".github/contracts/picorv32-demo.toml")).required_goals
    keys = [
        goal.key
        for goal in driver.translate_goals(
            tuple(driver.parse_goal_arg(goal) for goal in goals)
        ).goals
    ]
    status = f"{_ID} (active) · 0/{len(keys)} met\n" + "\n".join(f"{key} unmet" for key in keys)
    result, calls = _exercise([_reply(_ENTER), _reply(status)], goals=goals, readiness=True)
    assert result["state"] == "active" and set(result["goals"]) == set(keys)
    assert [name for name, _ in calls] == ["goal_enter", "goal_status"]
    assert calls[0][1]["goals"] == list(goals)
    assert any(goal["family"] == "mutation" for goal in goals)


@pytest.mark.parametrize(
    "reply",
    [
        _reply("", error=False),
        _reply("ERROR: refused", error=True),
        _reply("No record"),
        _reply(_ENTER.replace("/fixture", "/foreign")),
        _reply(_ENTER + "\nWarnings:\nTarget does not exist"),
    ],
)
def test_entry_missing_or_unresolved_proof_fails_closed(reply) -> None:
    with pytest.raises(driver.GoalDriverError, match="goal_enter"):
        _exercise([reply])


@pytest.mark.parametrize("text", ["EXIT_CODE: 1", "No completion proof"])
def test_failed_or_incomplete_lint_never_finishes(text) -> None:
    with pytest.raises(driver.GoalDriverError, match="lint"):
        _exercise([_reply(_ENTER), _reply(text)])


@pytest.mark.parametrize(
    "text",
    [
        "No active Goal Mode.",
        _STATUS.replace(_ID, "foreign"),
        _STATUS.replace("1/1", "0/1"),
        _STATUS.replace("1/1", "1/2"),
        _STATUS.replace("lint_clean_lint_core", "other"),
        _STATUS + "\nlint_clean_lint_core met",
        _STATUS.replace("1/1 met", "0/1 met").replace("met clean", "unmet"),
        _STATUS.replace("1/1 met", "0/1 met").replace("met clean", "stale"),
    ],
)
def test_missing_mismatched_unmet_or_stale_status_never_finishes(text) -> None:
    with pytest.raises(driver.GoalDriverError, match=r"goal_status|met and fresh"):
        _exercise([_reply(_ENTER), _reply("EXIT_CODE: 0"), _reply(text)])


@pytest.mark.parametrize(
    "reply",
    [
        _reply("not finished"),
        _reply(f"Goal Mode {_ID} finished but not really"),
        _reply("ERROR: unmet", error=True),
    ],
)
def test_finish_requires_successful_completion_proof(reply) -> None:
    with pytest.raises(driver.GoalDriverError, match="goal_finish"):
        _exercise([_reply(_ENTER), _reply("EXIT_CODE: 0"), _reply(_STATUS), reply])


def test_detached_flow_polls_exact_job_until_exit_zero() -> None:
    session = FakeSession([_reply("run_id=lint/1"), _reply("running"), _reply("EXIT_CODE: 0")])
    assert (
        asyncio.run(driver.run_flow(session, "lint", {"work_dir": "/fixture"})) == "EXIT_CODE: 0"
    )
    assert session.calls[1:] == [("booley_poll", {"run_id": "lint/1", "wait_seconds": 30})] * 2


def test_detached_flow_is_bounded_and_never_accepts_submission_as_success() -> None:
    session = FakeSession([_reply("run_id=lint/1"), _reply("running"), _reply("running")])
    with pytest.raises(driver.GoalDriverError, match="within 2 polls"):
        asyncio.run(driver.run_flow(session, "lint", {}, poll_limit=2))


def test_workspace_mutations_only_target_own_clone(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    state = source / ".booley_project"
    state.mkdir(parents=True)
    (state / "booley.toml").write_text("[project]\n")
    (state / "goals").mkdir()
    (state / "goals/saved").write_text("retained")
    owned = tmp_path / "owned"
    owned.mkdir()
    commands = []

    def run(command, cwd, env):
        commands.append((command, cwd))
        if command[1] == "clone":
            (Path(command[-1]) / ".git/info").mkdir(parents=True)
        if command[1] == "worktree":
            Path(command[-2]).mkdir()
        return ""

    monkeypatch.setattr(driver, "_run", run)
    worktree = driver._workspace(source, state, owned)
    assert all(cwd == owned or owned in cwd.parents for _, cwd in commands)
    assert (worktree / ".booley_project/booley.toml").is_file()
    assert not (worktree / ".booley_project/goals").exists()
    assert (state / "goals/saved").read_text() == "retained"
    assert "--no-hardlinks" in commands[0][0]


def test_final_allowed_poll_can_complete() -> None:
    session = FakeSession([_reply("run_id=lint/1"), _reply("running"), _reply("EXIT_CODE: 0")])
    assert asyncio.run(driver.run_flow(session, "lint", {}, poll_limit=2)) == "EXIT_CODE: 0"


@pytest.mark.parametrize("state", [driver.GoalState.ACTIVE, driver.GoalState.FINISHED])
def test_client_uses_modern_wire_preview_and_verifies_saved_state(
    tmp_path, monkeypatch, state
) -> None:
    captured = {}

    class Client:
        protocol_version = "2026-07-28"

        def __init__(self, server, *, mode):
            captured.update(server=server, mode=mode)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def exercise(*_args, **_kwargs):
        return {"record_id": _ID, "state": "finished"}

    monkeypatch.setattr(driver, "Client", Client)
    monkeypatch.setattr(driver, "exercise", exercise)
    monkeypatch.setattr(
        driver,
        "GoalStore",
        lambda _path: SimpleNamespace(load=lambda _id: SimpleNamespace(state=state)),
    )
    if state is driver.GoalState.FINISHED:
        result = asyncio.run(driver._client_run(tmp_path, [_LINT], Path(sys.executable), False))
        assert result["disposable"] is True
    else:
        with pytest.raises(driver.GoalDriverError, match="persisted Goal state"):
            asyncio.run(driver._client_run(tmp_path, [_LINT], Path(sys.executable), False))
    assert captured["mode"] == "2026-07-28"
    assert captured["server"].env["BOOLEY_GOAL_MODE_PREVIEW"] == "1"
    assert captured["server"].env["BOOLEY_MCP_MODE"] == "interactive"
    assert captured["server"].env["BOOLEY_PROJECT_DIR"] == str(tmp_path / ".booley_project")
