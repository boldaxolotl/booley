"""Goal status CLI selects worktree context and honors long/short overrides."""

import re
from argparse import Namespace
from dataclasses import replace
from types import SimpleNamespace

import pytest

from booley.harness import colors
from booley.harness.booley import _cmd_goal
from tests.goals.conftest import (  # noqa: F401 — fixtures shared with Goal integration tests
    goal_mode,
    layout,
)


@pytest.fixture(autouse=True)
def _colors_off(monkeypatch):
    """Plain output unless a test turns color on (``FORCE_COLOR`` must not leak in)."""
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)


@pytest.fixture(autouse=True)
def _frozen_baseline_jobs(monkeypatch):
    monkeypatch.setattr(
        "booley.mcp.sim_baseline_jobs.schedule_goal_baselines", lambda *_args: None
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
    assert "LINT:top" in text


@pytest.mark.parametrize(
    "short",
    [
        pytest.param(False, id="False"),
        pytest.param(True, marks=pytest.mark.timeout(120), id="True"),
    ],
)
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
    from tests.goals.conftest import SIM_KEY, bind, run_row
    from tests.goals.test_recorder import _v1

    state = _v1(
        goal_mode,
        GoalEvidenceRecorder(bind(goal_mode)),
        SIM_KEY,
        met,
        {
            # A claimed pass that names no frozen Test Run, or a failing frozen one.
            "test_runs": [] if met else [run_row(grade="fail")],
            "tests_passed": 1 if met else 0,
            "tests_total": 1,
        },
    )
    monkeypatch.chdir(goal_mode.worktree)
    assert _cmd_goal(Namespace(goal_command="status", long=True, short=False), goal_mode.main) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert f"{int(met)}/1 tests" in text
    assert state.criteria[SIM_KEY].met is False
    reason = "goal-test-run-incomplete" if met else "goal-test-run-failed"
    assert f"{reason}: {state.criteria[SIM_KEY].detail['goal_contract_violation']}" in text


PREFIX = "No Goal Mode is active here. Active Goal Modes in this Project:\n"
# The ended record the fixture's helpers leave: no end time, Summary, or Review Package.
LAST_ENDED = "\n\nLast ended Goal Mode in this worktree: {} ({})"


def second_goal(mode, monkeypatch):
    from tests.goals.conftest import enter_goals, git, write_project_files

    second = mode.control / "worktrees" / "other"
    git(mode.main, "worktree", "add", "-q", "-b", "other", str(second))
    write_project_files(second / ".booley")
    other = SimpleNamespace(main=mode.main, control=mode.control, worktree=second)
    with monkeypatch.context() as context:
        context.setattr("booley.goals.entry.compact_utc_now", lambda: "20261007T120000Z")
        other.record = enter_goals(other)
    return other


# The Goals `_status_view` shows: (name, status, evidence cell).
ROWS = [
    ("LINT:top", "met", "clean"),
    ("SIM:top_long_name", "unmet", "0/1 tests — goal-test-run-failed: the run failed"),
    ("SYNTH:top", "stale", "3 cells"),
]


def _status_view(mode, *, warning=""):
    """One view with met, unmet and stale Goals plus every recoverable-proposal line."""
    from booley.goals.changes import Approval, ChangeKind
    from booley.goals.presentation import GoalPresentation
    from booley.goals.proposals import Decision, ProposalView, new_proposal
    from booley.goals.status import GoalStatus, GoalStatusView

    goals = (
        GoalStatus("lint", "met", "clean", presentation=GoalPresentation("LINT:top")),
        GoalStatus(
            "sim",
            "unmet",
            "0/1 tests",
            reason="goal-test-run-failed: the run failed",
            presentation=GoalPresentation("SIM:top_long_name"),
            sim_transitions=({"transition": "pass -> fail", "name": "smoke"},),
        ),
        GoalStatus(
            "synth",
            "stale",
            "3 cells",
            presentation=GoalPresentation("SYNTH:top"),
            synth_baseline={"status": "pending", "reason": ""},
        ),
    )
    spec = mode.record.goals[0].spec
    proposal = new_proposal(
        mode.record,
        kind=ChangeKind.ADD,
        goal_key=spec.key,
        before=None,
        after=spec,
        after_arg=None,
        runtime_params={},
        rationale="add this Goal",
        session_key=None,
    )
    proposal = replace(proposal, id="p-1")  # a fixed id keeps colored and plain runs comparable
    decision = Decision(
        "approve",
        "reviewed",
        Approval.AGENT_RECORDED,
        "approve",
        "2026-10-19T00:00:00Z",
        proposal.payload_digest,
        None,
    )
    return GoalStatusView(
        mode.record,
        goals,
        warning,
        1,
        (ProposalView(proposal, decision=decision),),
        (("p-1", "stale revision"),),
        ("op-1",),
    )


def _cli_status(mode, monkeypatch, capsys, *, short, color=False):
    """Stdout of `booley goal status` for the `_status_view` view."""
    from booley.goals.status import GoalStatusSelection

    view = _status_view(mode, warning="evidence is old")
    monkeypatch.setattr(
        "booley.goals.status.select_status",
        lambda *_args, **_kwargs: GoalStatusSelection((view,), project_wide=False),
    )
    monkeypatch.setattr(colors, "COLORS_ENABLED", color)
    monkeypatch.chdir(mode.worktree)
    args = Namespace(goal_command="status", short=short, long=not short)
    assert _cmd_goal(args, mode.main) == 0
    return capsys.readouterr().out


def _table_lines(text):
    """The lines of *text* and the index of its one table header line."""
    lines = text.splitlines()
    headers = [
        i for i, line in enumerate(lines) if re.fullmatch(r"  Goal\s+Status\s+Evidence", line)
    ]
    assert len(headers) == 1, f"expected one table header in:\n{text}"
    header = headers[0]
    return lines, header


SHORT_LONG = pytest.mark.parametrize("short", [True, False], ids=["short", "long"])


@SHORT_LONG
def test_goal_status_table_is_aligned(goal_mode, monkeypatch, capsys, short):  # noqa: F811 — shared fixture
    lines, header = _table_lines(_cli_status(goal_mode, monkeypatch, capsys, short=short))
    assert set(lines[header + 1].replace(" ", "")) == {"─"}
    status_at, evidence_at = lines[header].index("Status"), lines[header].index("Evidence")
    for name, status, evidence in ROWS:
        row = next(line for line in lines[header + 2 :] if line.lstrip().startswith(name + " "))
        assert row[status_at:].startswith(status + " ")
        assert row[evidence_at:] == evidence
    if short:
        return
    facts = lines[:header]
    keys = [line for line in facts if re.match(r"  (Worktree|Branch|Pending proposals) {3}", line)]
    assert len(keys) == 3
    assert (
        len({re.match(r"  (Worktree|Branch|Pending proposals) +", line).end() for line in keys})
        == 1
    )
    trailing = "\n".join(lines[header + 2 + len(ROWS) :])
    for expected in (
        "Proposal ",
        "  Conflict: stale revision",
        "  agent-recorded: reviewed",
        "Interrupted applies: op-1",
    ):
        assert expected in trailing


@SHORT_LONG
def test_goal_status_colors_status_only_on_tty(goal_mode, monkeypatch, capsys, short):  # noqa: F811 — shared fixture
    plain = _cli_status(goal_mode, monkeypatch, capsys, short=short)
    colored = _cli_status(goal_mode, monkeypatch, capsys, short=short, color=True)
    assert colors.strip_ansi(plain) == plain
    assert colors.strip_ansi(colored) == plain
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    for status, paint in (("met", colors.green), ("unmet", colors.red), ("stale", colors.amber)):
        assert paint(status) in colored
    assert all(
        line.startswith("  " + name)
        for name, _, _ in ROWS
        for line in colored.splitlines()
        if line.lstrip().startswith(name + " ")
    )
    if not short:
        dim_start = colors.dim("x").split("x")[0]
        assert next(line for line in colored.splitlines() if "Worktree" in line).startswith(
            "  " + dim_start
        )


@SHORT_LONG
def test_goal_status_plain_rows_begin_name_status(goal_mode, monkeypatch, capsys, short):  # noqa: F811 — shared fixture
    lines, header = _table_lines(_cli_status(goal_mode, monkeypatch, capsys, short=short))
    for name, status, _ in ROWS:
        rows = [
            line
            for line in lines[header + 2 :]
            if re.match(rf"\s*{re.escape(name)}\s+{status}(\s|$)", line)
        ]
        assert len(rows) == 1
        assert not any(f"{name}: {status}" in line for line in lines)


def _cli_baseline(views, short):
    """Frozen expected CLI text: pads its own columns, independent of the shared renderer."""
    from booley.goals.sim_transitions import transition_summary

    out = []
    compact = len(views) != 1 if short is None else short
    for view in views:
        out.append(
            f"{view.record.id} ({view.record.state.value}) · {view.met}/{len(view.goals)} met"
        )
        if view.warning:
            out.append(view.warning)
        if not compact:
            facts = [
                ("Worktree", view.record.worktree_path),
                ("Branch", view.record.branch),
                ("Pending proposals", str(view.pending_proposals)),
            ]
            out += [f"  {key:<17}   {value}" for key, value in facts]
        cells = [("Goal", "Status", "Evidence")] + [
            (
                g.presentation.text,
                g.status,
                g.evidence_summary
                + (f" — {g.reason}" if g.status == "unmet" and g.reason else ""),
            )
            for g in view.goals
        ]
        wide = [max(len(cell[i]) for cell in cells) for i in (0, 1, 2)]
        out.append("  " + "   ".join(("Goal".ljust(wide[0]), "Status".ljust(wide[1]), "Evidence")))
        out.append("  " + "   ".join("─" * w for w in wide))
        for goal, (name, status, evidence) in zip(view.goals, cells[1:], strict=True):
            out.append(f"  {name.ljust(wide[0])}   {status.ljust(wide[1])}   {evidence}".rstrip())
            if compact and goal.sim_transitions is not None:
                summary = transition_summary(goal.sim_transitions)
                out.append(f"  {name}: baseline -> current: {summary}")
        if not compact:
            from booley.goals.format import test_run_lines

            runs = [line for goal in view.goals for line in test_run_lines(goal)]
            out += (["Test Runs"] if runs else []) + [f"  {line}" for line in runs]
    return "\n".join(out)


# Windows CI observed 30.2 s; retain the required 50% timeout headroom.
@pytest.mark.timeout(120)
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

    other = second_goal(goal_mode, monkeypatch) if multiple or caller == "abandoned" else None
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
    expected_ids = [goal_mode.record.id]
    if caller == "abandoned":
        assert other is not None
        expected_ids = [other.record.id]
    elif caller in {"main", "outside"} and other is not None:
        expected_ids.append(other.record.id)
    assert {view.record.id for view in views} == set(expected_ids)
    assert len(views) == len(expected_ids)
    expected = _cli_baseline(views, short=detail)
    if caller in {"main", "outside", "abandoned"}:
        expected = PREFIX + expected
    if caller == "abandoned":
        # The worktree's own ended record follows the Project's active Goal Modes.
        expected += LAST_ENDED.format(goal_mode.record.id, "abandoned")
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
    expected = "No active Goal Mode."
    if caller == "occupant":
        expected += LAST_ENDED.format(goal_mode.record.id, state)
    assert (captured.out, captured.err) == (expected + "\n", "")


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
    message = (
        f"Goal unavailable (unsupported record); inspect: {record_paths(goal_mode.control, goal_mode.record.id).record_file}"
        if error == "corrupt"
        else str(failure.value)
    )
    assert (captured.out, captured.err) == ("", f"ERROR: {message}\n")


@pytest.fixture
def selection_cli(monkeypatch):
    from booley.harness import booley as cli

    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", lambda: False)

    def invoke(*arguments):
        monkeypatch.setattr("sys.argv", ["booley", *map(str, arguments)])
        return cli.main()

    return invoke


@pytest.mark.parametrize("caller", ["main", "worktree"])
@pytest.mark.parametrize("spelling", ["prefix", "alias", "suffix", "nested-relative"])
def test_goal_status_project_selection(
    goal_mode,  # noqa: F811 — shared fixture
    monkeypatch,
    capsys,
    selection_cli,
    caller,
    spelling,
):
    import os

    other = second_goal(goal_mode, monkeypatch)
    monkeypatch.chdir(getattr(goal_mode, caller))
    selected = other.worktree
    if spelling == "nested-relative":
        selected = selected / "nested"
        selected.mkdir()
        selected = os.path.relpath(selected)
    arguments = ["-C", selected, "goal", "status", "--long"]
    if spelling == "alias":
        arguments[0] = "--project"
    elif spelling == "suffix":
        arguments = ["goal", "status", "--long", "-C", selected]
    assert selection_cli(*arguments) == 0
    captured = capsys.readouterr()
    assert other.record.id in captured.out
    assert goal_mode.record.id not in captured.out
    assert PREFIX not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("caller", ["main", "worktree"])
def test_goal_abandon_project_selection_and_retry(
    goal_mode,  # noqa: F811 — shared fixture
    monkeypatch,
    capsys,
    selection_cli,
    caller,
):
    from booley.goals.model import GoalState
    from booley.goals.store import GoalStore
    from tests.goals.conftest import git

    other = second_goal(goal_mode, monkeypatch)
    store = GoalStore(goal_mode.control)
    before = store.load(goal_mode.record.id)
    monkeypatch.chdir(getattr(goal_mode, caller))
    quote = "Stop selected Goal"
    arguments = ["-C", other.worktree, "goal", "abandon", "--instruction-quote", quote]
    result = selection_cli(*arguments)
    captured = capsys.readouterr()
    # Check persisted selection even when the unfixed command reports success for A.
    assert store.load(goal_mode.record.id) == before
    assert result == 0, captured.err
    ended = store.load(other.record.id)
    assert ended.state == GoalState.ABANDONED
    assert ended.end_instruction_quote == quote
    assert "Branch and files retained" in captured.out
    assert captured.err == ""
    operation = captured.out.split("--operation-id ", 1)[1].splitlines()[0].strip()
    for retry_caller in (goal_mode.main, goal_mode.worktree):
        monkeypatch.chdir(retry_caller)
        assert (
            selection_cli(*arguments, "--record-id", other.record.id, "--operation-id", operation)
            == 0
        )
        assert capsys.readouterr().out == captured.out
        assert store.load(goal_mode.record.id) == before
        assert store.load(other.record.id) == ended
    for mode in (goal_mode, other):
        assert (mode.worktree / "rtl.v").is_file()
        assert git(mode.main, "show-ref", "--verify", f"refs/heads/{mode.record.branch}")


@pytest.mark.parametrize("command", ["status", "abandon"])
def test_goal_project_selection_main_without_occupant(
    goal_mode,  # noqa: F811 — shared fixture
    monkeypatch,
    capsys,
    selection_cli,
    command,
):
    from booley.goals.store import GoalStore

    other = second_goal(goal_mode, monkeypatch)
    store = GoalStore(goal_mode.control)
    before = [store.load(mode.record.id) for mode in (goal_mode, other)]
    monkeypatch.chdir(goal_mode.worktree)
    assert selection_cli("-C", goal_mode.main, "goal", command) == (
        0 if command == "status" else 2
    )
    captured = capsys.readouterr()
    if command == "status":
        assert captured.out.startswith(PREFIX)
        assert all(record.id in captured.out for record in before)
        assert captured.err == ""
    else:
        assert captured.err == (
            "ERROR: worktree hosts no Goal Mode; supply exact retry IDs for a closed operation\n"
        )
    assert [store.load(mode.record.id) for mode in (goal_mode, other)] == before


@pytest.mark.timeout(120)  # Windows CI: 33.9 s including fixtures; retain 3x headroom.
def test_goal_status_project_selection_no_option(goal_mode, monkeypatch, capsys, selection_cli):  # noqa: F811 — shared fixture
    other = second_goal(goal_mode, monkeypatch)
    nested = goal_mode.worktree / "nested"
    nested.mkdir()
    occupant_output = None
    for location in (goal_mode.main, goal_mode.worktree, nested):
        monkeypatch.chdir(location)
        assert selection_cli("goal", "status", "--long") == 0
        captured = capsys.readouterr()
        assert goal_mode.record.id in captured.out
        assert captured.err == ""
        if location == goal_mode.main:
            assert captured.out.startswith(PREFIX)
            assert other.record.id in captured.out
        else:
            assert PREFIX not in captured.out
            assert other.record.id not in captured.out
            if occupant_output is None:
                occupant_output = captured.out
            assert captured.out == occupant_output


def test_goal_abandon_project_selection_no_option(goal_mode, monkeypatch, capsys, selection_cli):  # noqa: F811 — shared fixture
    from booley.goals.model import GoalState
    from booley.goals.store import GoalStore

    other = second_goal(goal_mode, monkeypatch)
    store = GoalStore(goal_mode.control)
    before = store.load(other.record.id)
    monkeypatch.chdir(goal_mode.worktree)
    assert selection_cli("goal", "abandon") == 0
    assert capsys.readouterr().err == ""
    assert store.load(goal_mode.record.id).state == GoalState.ABANDONED
    assert store.load(other.record.id) == before
