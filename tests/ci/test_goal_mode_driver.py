"""Fail-closed CI Goal driver checks using a cheap fake MCP session."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.model import parse_goal_arg

sys.path.insert(0, str(Path(__file__).parents[2] / ".github/scripts"))
import goal_mode_driver as driver
from tests.smoke import test_goal_mode_image_smoke as smoke

_ID = "ci-demo-20261009T120000Z"
_WORKTREE = Path("/fixture")
_LINT = driver.load_contract(Path(".github/contracts/picorv32-demo.toml")).required_goals[0]
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


def _exercise(replies, *, goals=(_LINT,), readiness=False):
    session = FakeSession(replies)
    operation = driver.readiness if readiness else driver.roundtrip
    result = asyncio.run(operation(session, _WORKTREE, goals))
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
    assert set(result["steps"]) == {"goal_enter", "lint", "goal_status", "goal_finish"}
    assert all(step["status"] == "pass" and step["response"] for step in result["steps"].values())
    assert [name for name, _ in calls] == ["goal_enter", "lint", "goal_status", "goal_finish"]
    assert all(arguments["work_dir"] == str(_WORKTREE) for _, arguments in calls)
    finish = calls[-1][1]
    assert finish["record_id"] == _ID and finish["operation_id"] and finish["summary"]
    assert "abandon" not in finish and "instruction_quote" not in finish


def test_readiness_lists_full_contract_and_leaves_unmet_goals_active() -> None:
    goals = driver.load_contract(Path(".github/contracts/picorv32-demo.toml")).required_goals
    keys = [goal.key for goal in driver.translate_goals(goals).goals]
    status = f"{_ID} (active) · 0/{len(keys)} met\n" + "\n".join(
        f"{driver.goal_presentation(spec).text} unmet"
        for spec in driver.translate_goals(goals).goals
    )
    result, calls = _exercise([_reply(_ENTER), _reply(status)], goals=goals, readiness=True)
    assert result["state"] == "active" and set(result["goals"]) == set(keys)
    assert [name for name, _ in calls] == ["goal_enter", "goal_status"]
    assert calls[0][1]["goals"] == [driver.goal_arg_to_json(goal) for goal in goals]
    assert any(goal.family.value == "mutation" for goal in goals)


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
        _STATUS.replace("LINT:lint_core", "other"),
        _STATUS + "\nLINT:lint_core met",
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
        asyncio.run(driver.run_flow(session, "lint", {"work_dir": str(_WORKTREE)}))
        == "EXIT_CODE: 0"
    )
    assert session.calls[1:] == [("booley_poll", {"run_id": "lint/1", "wait_seconds": 30})] * 2


def test_detached_flow_is_bounded_and_never_accepts_submission_as_success() -> None:
    session = FakeSession([_reply("run_id=lint/1"), _reply("running"), _reply("running")])
    with pytest.raises(driver.GoalDriverError, match="within 2 polls"):
        asyncio.run(driver.run_flow(session, "lint", {}, poll_limit=2))


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

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(driver, "is_git_worktree_root", lambda _path: True)
    workspace = driver.create_workspace(source, state, owned)
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
    monkeypatch.setattr(driver, "roundtrip", exercise)
    monkeypatch.setattr(
        driver,
        "GoalStore",
        lambda path: (
            captured.update(store=path)
            or SimpleNamespace(load=lambda _id: SimpleNamespace(state=state))
        ),
    )
    if state is driver.GoalState.FINISHED:
        result = asyncio.run(
            driver._client_run(
                driver.Workspace(tmp_path / "primary", tmp_path / "worktree"),
                [_LINT],
                Path(sys.executable),
                False,
            )
        )
        assert result["disposable"] is True
    else:
        with pytest.raises(driver.GoalDriverError, match="persisted Goal state"):
            asyncio.run(
                driver._client_run(
                    driver.Workspace(tmp_path / "primary", tmp_path / "worktree"),
                    [_LINT],
                    Path(sys.executable),
                    False,
                )
            )
    assert captured["store"] == tmp_path / "primary/.booley"
    assert captured["mode"] == "2026-07-28"
    assert captured["server"].env["BOOLEY_MCP_MODE"] == "interactive"
    assert captured["server"].env["BOOLEY_DIR"] == str(tmp_path / "primary/.booley")


def test_entry_warnings_are_recorded_without_refusing_readiness() -> None:
    warning = "Target needs a generated input"
    result, calls = _exercise(
        [
            _reply(
                _ENTER + f"\nWarnings:\n- {warning}\n\nRules:\n- Keep working\nWARNING: footer"
            ),
            _reply(_STATUS),
        ],
        readiness=True,
    )
    assert result["steps"]["goal_enter"]["warnings"] == [warning]
    assert [name for name, _ in calls] == ["goal_enter", "goal_status"]


@pytest.fixture(scope="module")
def readiness_workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp("readiness")
    worktree = root / "worktree"
    (worktree / ".booley").mkdir(parents=True)
    (worktree / ".booley/booley.toml").write_text("[stealth]\nenabled=false\n")
    (worktree / "fixture.core").write_text(
        "CAPI=2:\nname: ci:demo:fixture:0\ntargets:\n"
        + "".join(
            f"  {name}: {{default_tool: verilator}}\n"
            for name in driver.load_contract(
                Path(".github/contracts/picorv32-demo.toml")
            ).required_targets
        )
    )
    return driver.Workspace(root, worktree)


def _readiness_client(workspace, goals, record, monkeypatch, warning=""):
    specs = driver.translate_goals(goals).goals
    session = FakeSession(
        [
            _reply(f"Goal Mode {_ID} entered in {workspace.worktree}.\n{warning}"),
            _reply(
                f"{_ID} (active) · 0/{len(specs)} met\n"
                + "\n".join(
                    f"{driver.goal_presentation(spec).text} unmet no evidence" for spec in specs
                )
            ),
        ]
    )

    @asynccontextmanager
    async def client(_workspace, _python):
        yield session

    monkeypatch.setattr(driver, "client_session", client)
    monkeypatch.setattr(
        driver, "GoalStore", lambda _path: SimpleNamespace(load=lambda _id: record)
    )
    return asyncio.run(driver._client_run(workspace, goals, Path(sys.executable), True))


@pytest.mark.parametrize("target", [None, "ghost"])
def test_readiness_rejects_unresolved_record_targets_without_warning_prose(
    readiness_workspace, monkeypatch, target
) -> None:
    goals = [parse_goal_arg({"family": "LINT", "target": target or "lint_core"})]
    key = driver.translate_goals(goals).goals[0].key
    record = SimpleNamespace(
        state=driver.GoalState.ACTIVE,
        goals=(SimpleNamespace(spec=SimpleNamespace(key=key, target=target)),),
    )
    with pytest.raises(driver.GoalDriverError, match="Target"):
        _readiness_client(readiness_workspace, goals, record, monkeypatch)


def test_full_contract_readiness_records_resolved_targets_and_preserves_harmless_warnings(
    readiness_workspace, monkeypatch
) -> None:
    goals = driver.load_contract(Path(".github/contracts/picorv32-demo.toml")).required_goals
    specs = driver.translate_goals(goals).goals
    record = SimpleNamespace(
        state=driver.GoalState.ACTIVE, goals=tuple(SimpleNamespace(spec=spec) for spec in specs)
    )
    result = _readiness_client(
        readiness_workspace,
        goals,
        record,
        monkeypatch,
        "Warnings:\n- Harmless advisory\n\nRules:\n- Rule\nWARNING: footer",
    )
    assert result["resolved_targets"] == {
        spec.key: f"ci:demo:fixture:0#{spec.target}" for spec in specs
    }
    assert set(result["goals"].values()) == {"unmet"}
    from release_validation.goal_readiness import validate as validate_readiness

    evidence_path = readiness_workspace.primary / "goal-readiness.json"
    driver.write_evidence(evidence_path, result)
    validate_readiness(Path(".github/contracts/picorv32-demo.toml"), evidence_path)
    assert result["steps"]["goal_enter"]["warnings"] == ["Harmless advisory"]


@pytest.mark.parametrize("flag", [None, False, True])
def test_unflagged_error_refusal_is_classified_once(flag) -> None:
    session = FakeSession(
        [_reply("ERROR: Goals must be met and fresh before finish: sim_fail", error=flag)]
    )
    with pytest.raises(driver.GoalToolRefusalError):
        asyncio.run(driver.finish(session, _WORKTREE, _ID))


def test_flow_polling_uses_one_deadline(monkeypatch) -> None:
    deadlines = []
    replies = iter(["run_id=lint/1", "running", "EXIT_CODE: 0"])

    async def text(_session, _name, _arguments, *, deadline):
        deadlines.append(deadline)
        return next(replies)

    monkeypatch.setattr(driver, "tool_text", text)
    assert asyncio.run(driver.run_flow(FakeSession([]), "lint", {})) == "EXIT_CODE: 0"
    assert len(deadlines) == 3 and len(set(deadlines)) == 1


def test_expired_deadline_refuses_without_waiting() -> None:
    session = FakeSession([_reply("not reached")])
    with pytest.raises(driver.GoalDriverError, match="timed out"):
        asyncio.run(driver.tool_text(session, "lint", {}, deadline=-1))
    assert not session.calls


def test_expected_failed_flow_still_requires_terminal_proof() -> None:
    session = FakeSession([_reply("run_id=sim/1"), _reply("EXIT_CODE: 1")])
    assert asyncio.run(driver.run_flow(session, "sim", {}, expected_exit_code=1)) == "EXIT_CODE: 1"


def test_atomic_evidence_failure_preserves_previous_complete_record(tmp_path, monkeypatch) -> None:
    path = tmp_path / "proof.json"
    driver.write_evidence(path, {"previous": "complete"})

    def fail(*_args, **_kwargs):
        raise OSError("interrupted replacement")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="interrupted"):
        driver.write_evidence(path, {"new": "proof"})
    assert path.read_text().strip() == '{\n  "previous": "complete"\n}'
    assert list(tmp_path.iterdir()) == [path]


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

    monkeypatch.setattr(smoke, "client_session", client)
    monkeypatch.setattr(smoke, "_enter", entered)
    monkeypatch.setattr(smoke, "_run_git", lambda *_args: SimpleNamespace(stdout="unchanged"))
    monkeypatch.setattr(
        smoke,
        "GoalStore",
        lambda _path: SimpleNamespace(
            load=lambda _id: SimpleNamespace(state=driver.GoalState.ACTIVE)
        ),
    )
    asyncio.run(smoke._failure(driver.Workspace(tmp_path, tmp_path / "worktree")))
    assert [name for name, _ in session.calls] == ["sim", "goal_status", "goal_finish"]


def test_status_parser_accepts_public_names_with_parenthesized_requirements():
    text = f"{_ID} (active) · 1/2 met\n SYNTH:synth_core (fmax ≥ 400 MHz) met fresh\n REVIEW:rtl_bugs (clean) unmet no evidence"
    assert driver.status_rows(text, _ID, ["SYNTH:synth_core", "REVIEW:rtl_bugs"]) == {
        "SYNTH:synth_core": "met",
        "REVIEW:rtl_bugs": "unmet",
    }
    with pytest.raises(driver.GoalDriverError):
        driver.status_rows(
            text + "\n SYNTH:synth_core (fmax ≥ 400 MHz) met fresh",
            _ID,
            ["SYNTH:synth_core", "REVIEW:rtl_bugs"],
        )


@pytest.mark.skipif(
    sys.platform == "win32", reason="the Goal driver runs only inside the Linux CI image"
)
def test_stop_baseline_jobs_cancels_detached_job_before_workspace_cleanup(tmp_path) -> None:
    """A detached baseline Job must not keep writing into a deleted workspace."""
    import subprocess
    import threading

    from booley.goals.sim_baseline_store import baseline_jobs_dir
    from booley.runtime import job_records
    from booley.runtime.process_group import new_group_kwargs
    from booley.runtime.timefmt import utc_now_rfc3339

    project = tmp_path / ".booley"
    jobs = baseline_jobs_dir(project)
    command = [sys.executable, "-c", "import time; time.sleep(120)"]
    process = subprocess.Popen(command, **new_group_kwargs())
    # The test is the parent, so reap the child to avoid a lingering zombie PID.
    threading.Thread(target=process.wait, daemon=True).start()
    record = job_records.JobRecord(
        "sim-baseline-x", "sim_baseline", utc_now_rfc3339(), 60, argv=command
    )
    record.pid = process.pid
    job_records.write_record(record, jobs)

    driver.stop_baseline_jobs(project, timeout_s=30)

    assert process.wait(timeout=5) != 0


def test_stop_baseline_jobs_covers_every_registered_family_root(tmp_path, monkeypatch) -> None:
    """A Job under a non-SIM family's root is cancelled too."""
    import dataclasses

    from booley.goals.sim_baseline_store import SIM_BASELINE, baseline_jobs_dir
    from booley.runtime import job_records

    other = dataclasses.replace(SIM_BASELINE, name="other")
    monkeypatch.setattr(driver, "BASELINE_FAMILIES", (SIM_BASELINE, other))
    project = tmp_path / ".booley"
    job_records.write_record(
        job_records.JobRecord("other-baseline-x", "other_baseline", "now", 60, pid=4242),
        baseline_jobs_dir(project, other),
    )
    stopped: list[int] = []
    monkeypatch.setattr(driver.job_records, "is_active", lambda *_a: not stopped)
    monkeypatch.setattr(
        driver, "request_group_termination", lambda group: stopped.append(group.id)
    )

    driver.stop_baseline_jobs(project, timeout_s=5)

    assert stopped == [4242]


def test_stop_baseline_jobs_without_jobs_returns_immediately(tmp_path) -> None:
    driver.stop_baseline_jobs(tmp_path / ".booley", timeout_s=0)


def test_stop_baseline_jobs_reports_jobs_that_outlive_the_bound(tmp_path, monkeypatch) -> None:
    from booley.runtime import job_records

    stuck = job_records.JobRecord("sim-baseline-stuck", "sim_baseline", "now", 60, pid=4242)
    signalled = []
    monkeypatch.setattr(driver.job_records, "list_records", lambda _root: [stuck])
    monkeypatch.setattr(driver.job_records, "is_active", lambda _record, _alive: True)
    monkeypatch.setattr(driver, "request_group_termination", signalled.append)

    with pytest.raises(driver.GoalDriverError, match="sim-baseline-stuck"):
        driver.stop_baseline_jobs(tmp_path, timeout_s=0)
    assert signalled == [driver.ProcessGroup(4242)]
