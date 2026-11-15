"""Fail-closed Goal Mode driver checks using a cheap fake MCP session."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.goals.model import GoalState, parse_goal_arg


def _driver() -> Any:
    """Import lazily: the support module moved here from ``.github/scripts``."""
    from tests.goals import goal_mode_driver

    return goal_mode_driver


def _smoke() -> Any:
    from tests.smoke import test_goal_mode_image_smoke

    return test_goal_mode_image_smoke


_ID = "ci-demo-20261009T120000Z"
_WORKTREE = Path("/fixture")
_LINT = parse_goal_arg({"family": "LINT", "target": "lint_core"})
_ENTER = f"Goal Mode {_ID} entered in {_WORKTREE}.\nGoals (all unmet):\n- LINT:lint_core"
_STATUS = f"{_ID} (active) · 1/1 met\nLINT:lint_core met clean"


def _reply(text, *, error=False):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], isError=error)


class FakeSession:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return next(self.replies)


def _enter(reply):
    session = FakeSession([reply])
    result = asyncio.run(_driver().enter(session, _WORKTREE, [_LINT]))
    return result, session.calls


def test_entry_proves_the_record_and_the_workspace() -> None:
    result, calls = _enter(_reply(_ENTER))
    assert result["record_id"] == _ID and result["status"] == "pass"
    assert [name for name, _ in calls] == ["goal_enter"]
    assert calls[0][1]["work_dir"] == str(_WORKTREE)


@pytest.mark.parametrize(
    "reply",
    [
        _reply("", error=False),
        _reply("ERROR: refused", error=True),
        _reply("No record"),
        _reply(_ENTER.replace(str(_WORKTREE), str(Path("/foreign")))),
        _reply("ERROR: refusal without flag", error=None),
    ],
)
def test_entry_missing_or_unresolved_proof_fails_closed(reply) -> None:
    with pytest.raises(_driver().GoalDriverError, match="goal_enter"):
        _enter(reply)


@pytest.mark.parametrize(
    "text",
    [
        "No active Goal Mode.",
        _STATUS.replace(_ID, "foreign"),
        _STATUS.replace("1/1", "0/1"),
        _STATUS.replace("1/1", "1/2"),
        _STATUS.replace("LINT:lint_core", "other"),
        _STATUS + "\nLINT:lint_core met",
    ],
)
def test_missing_or_mismatched_status_rows_fail_closed(text) -> None:
    with pytest.raises(_driver().GoalDriverError, match="goal_status"):
        _driver().status_rows(text, _ID, ["LINT:lint_core"])


def test_finish_requires_successful_completion_proof() -> None:
    for reply in (
        _reply("not finished"),
        _reply(f"Goal Mode {_ID} finished but not really"),
        _reply("ERROR: unmet", error=True),
    ):
        with pytest.raises(_driver().GoalDriverError, match="goal_finish"):
            asyncio.run(_driver().finish(FakeSession([reply]), _WORKTREE, _ID))


def test_finish_accepts_the_finished_record() -> None:
    session = FakeSession([_reply(f"Goal Mode {_ID} finished.")])
    assert asyncio.run(_driver().finish(session, _WORKTREE, _ID)) == f"Goal Mode {_ID} finished."
    assert session.calls[0][0] == "goal_finish"


def test_detached_flow_polls_exact_job_until_exit_zero() -> None:
    session = FakeSession([_reply("run_id=lint/1"), _reply("running"), _reply("EXIT_CODE: 0")])
    assert (
        asyncio.run(_driver().run_flow(session, "lint", {"work_dir": str(_WORKTREE)}))
        == "EXIT_CODE: 0"
    )
    assert session.calls[1:] == [("booley_poll", {"run_id": "lint/1", "wait_seconds": 30})] * 2


def test_detached_flow_is_bounded_and_never_accepts_submission_as_success() -> None:
    session = FakeSession([_reply("run_id=lint/1"), _reply("running"), _reply("running")])
    with pytest.raises(_driver().GoalDriverError, match="within 2 polls"):
        asyncio.run(_driver().run_flow(session, "lint", {}, poll_limit=2))


def test_workspace_mutations_only_target_own_clone(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    state = source / ".booley"
    state.mkdir(parents=True)
    (state / "booley.toml").write_text("[project]\n")
    (state / "goals").mkdir()
    (state / "goals/saved").write_text("retained")
    owned = tmp_path / "owned"
    owned.mkdir()
    commands = []

    def run(command, cwd, env, *, stdin_payload=None):
        commands.append((command, cwd))
        if command[0] == "git" and command[1] == "clone":
            destination = Path(command[-1])
            (destination / ".git/info").mkdir(parents=True)
            if destination.name == ".booley":
                (destination / "booley.toml").write_text("[project]\n")
        elif command[1:5] == ["-m", "booley.harness.booley", "worktree", "new"]:
            destination = cwd / ".booley/worktrees/ci-demo/.booley"
            destination.mkdir(parents=True)
            (destination / ".git").write_text("paired pointer")
        return ""

    monkeypatch.setattr(_driver(), "_run", run)
    monkeypatch.setattr(_driver(), "is_git_worktree_root", lambda _path: True)
    workspace = _driver().create_workspace(source, state, owned)
    assert all(cwd == owned or owned in cwd.parents for _, cwd in commands)
    assert (workspace.worktree / ".booley/.git").is_file()
    assert workspace.booley_dir == owned / "primary/.booley"
    assert (state / "goals/saved").read_text() == "retained"
    assert "--no-hardlinks" in commands[0][0]
    assert any(
        command[1:] == ["-m", "booley.harness.booley", "worktree", "new", "ci-demo"]
        for command, _ in commands
    )


def test_final_allowed_poll_can_complete() -> None:
    session = FakeSession([_reply("run_id=lint/1"), _reply("running"), _reply("EXIT_CODE: 0")])
    assert asyncio.run(_driver().run_flow(session, "lint", {}, poll_limit=2)) == "EXIT_CODE: 0"


def test_entry_warnings_are_recorded_without_refusing_entry() -> None:
    warning = "Target needs a generated input"
    result, _calls = _enter(
        _reply(_ENTER + f"\nWarnings:\n- {warning}\n\nRules:\n- Keep working\nWARNING: footer")
    )
    assert result["warnings"] == [warning]


@pytest.mark.parametrize("flag", [None, False, True])
def test_unflagged_error_refusal_is_classified_once(flag) -> None:
    session = FakeSession(
        [_reply("ERROR: Goals must be met and fresh before finish: sim_fail", error=flag)]
    )
    with pytest.raises(_driver().GoalToolRefusalError):
        asyncio.run(_driver().finish(session, _WORKTREE, _ID))


def test_flow_polling_uses_one_deadline(monkeypatch) -> None:
    deadlines = []
    replies = iter(["run_id=lint/1", "running", "EXIT_CODE: 0"])

    async def text(_session, _name, _arguments, *, deadline):
        deadlines.append(deadline)
        return next(replies)

    monkeypatch.setattr(_driver(), "tool_text", text)
    assert asyncio.run(_driver().run_flow(FakeSession([]), "lint", {})) == "EXIT_CODE: 0"
    assert len(deadlines) == 3 and len(set(deadlines)) == 1


def test_expired_deadline_refuses_without_waiting() -> None:
    session = FakeSession([_reply("not reached")])
    with pytest.raises(_driver().GoalDriverError, match="timed out"):
        asyncio.run(_driver().tool_text(session, "lint", {}, deadline=-1))
    assert not session.calls


def test_expected_failed_flow_still_requires_terminal_proof() -> None:
    session = FakeSession([_reply("run_id=sim/1"), _reply("EXIT_CODE: 1")])
    assert (
        asyncio.run(_driver().run_flow(session, "sim", {}, expected_exit_code=1)) == "EXIT_CODE: 1"
    )


def test_image_failure_uses_shared_unflagged_refusal_classification(tmp_path, monkeypatch) -> None:
    session = FakeSession(
        [
            _reply("EXIT_CODE: 1\nintentional Goal Mode smoke failure"),
            _reply(f"{_ID} (active) · 0/1 met\nSIM:sim_fail unmet failure"),
            _reply("ERROR: Goals must be met and fresh before finish: SIM:sim_fail", error=None),
        ]
    )

    @asynccontextmanager
    async def client(_workspace):
        yield session

    async def entered(_client, _workspace, _goals):
        return _ID

    monkeypatch.setattr(_smoke(), "client_session", client)
    monkeypatch.setattr(_smoke(), "_enter", entered)
    monkeypatch.setattr(_smoke(), "_run_git", lambda *_args: SimpleNamespace(stdout="unchanged"))
    monkeypatch.setattr(
        _smoke(),
        "GoalStore",
        lambda _path: SimpleNamespace(load=lambda _id: SimpleNamespace(state=GoalState.ACTIVE)),
    )
    asyncio.run(_smoke()._failure(_driver().Workspace(tmp_path, tmp_path / "worktree")))
    assert [name for name, _ in session.calls] == ["sim", "goal_status", "goal_finish"]


def test_status_parser_accepts_public_names_with_parenthesized_requirements():
    text = f"{_ID} (active) · 1/2 met\n SYNTH:synth_core (fmax ≥ 400 MHz) met fresh\n REVIEW:rtl_bugs (clean) unmet no evidence"
    assert _driver().status_rows(text, _ID, ["SYNTH:synth_core", "REVIEW:rtl_bugs"]) == {
        "SYNTH:synth_core": "met",
        "REVIEW:rtl_bugs": "unmet",
    }
    with pytest.raises(_driver().GoalDriverError):
        _driver().status_rows(
            text + "\n SYNTH:synth_core (fmax ≥ 400 MHz) met fresh",
            _ID,
            ["SYNTH:synth_core", "REVIEW:rtl_bugs"],
        )
