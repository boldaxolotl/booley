"""Goal recording keeps simulation truth independent of coverage evaluation."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows import target_test_plan_loading
from booley.flows.frozen_test_runs import OUTCOMES_ONLY_DETAIL_KEY
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.goals.apply import ChangeEnvironment
from booley.goals.entry import EntryRequest, enter_goal_mode
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.model import parse_goal_args
from booley.goals.paths import record_paths
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.state_store import load_goal_state
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from booley.harness.dashboard.model import project_goal
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.goals import conftest
from tests.goals.conftest import bind, campaign_facts, entry_environment, git, run_row
from tests.goals.test_test_run_evidence import _replayed, _state_file, _status
from tests.goals.test_test_run_goal_changes import _change

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)


@pytest.mark.parametrize("simulation", ["pass", "fail"])
@pytest.mark.parametrize("coverage", ["pass", "fail"])
def test_bound_flow_records_both_verdicts(layout, monkeypatch, simulation, coverage):
    root, record = _enter(
        layout,
        monkeypatch,
        [
            {"family": "SIM", "target": "top"},
            {"family": "COVERAGE", "target": "top", "tests": "all", "metrics": {"line": 100}},
        ],
    )
    adapter = GoalFlowExecution(bind(layout))
    result = SimulateFlow(
        coverage_execution=lambda *_args: NativeExecution(
            hits=1 if coverage == "pass" else 0,
            verdict=simulation,
        )
    ).execute(
        SimRequest(target="top", work_dir=root, coverage=True, report_dir=root / "reports"),
        adapter=adapter,
    )
    saved = DevelopmentState.load(adapter.state_file)
    assert saved.criteria["sim_pass_top"].met is (simulation == "pass")
    # A coverage Goal is judged by its coverage floors alone: a Test Run that failed
    # its checks is the simulation Goal's concern, not the coverage Goal's.
    entry = saved.criteria["coverage_top"]
    assert entry.met is (coverage == "pass")
    assert "criterion_metric" not in entry.detail
    violation = entry.detail.get("goal_contract_violation", "")
    assert "Test Runs failed" not in violation and "have not passed" not in violation
    # The failing runs stay visible in what the Goal shows of its Test Runs.
    view = build_status(GoalStore(layout.control), record, work_dir=root)
    status = next(goal for goal in view.goals if goal.key == "coverage_top")
    assert {run.outcome for run in status.test_runs.runs} == {
        "passed" if simulation == "pass" else "failed"
    }
    # (This substituted campaign records no waiver policy, so met evidence reads stale.)
    assert (status.status == "unmet") is (coverage == "fail"), status
    assert "goal-test-run-" not in status.reason
    assert result.exit_code == (0 if simulation == "pass" else 1), result.outcome
    assert len(saved.acceptance_transactions) == 1
    # Only a measured coverage miss is a failure the Goal observed (AC5).
    assert entry.ever_failed is (coverage == "fail")
    mode = SimpleNamespace(**vars(layout), record=record)
    replayed = _replayed(mode, "coverage_top")
    assert replayed.ever_failed is (coverage == "fail")


def _enter(layout, monkeypatch, goals, *, tools=("verilator",), tests_toml=None):
    """A coverage-capable ``top`` Target with Tests ``reset`` and ``wrap``, in Goal Mode.

    Further *tools* add Targets ``sim_1``... with the same Tests.
    """
    root: Path = layout.worktree
    project(root, tools)
    core = root / "counter.core"
    core.write_text(core.read_text().replace("sim_0", "top"))
    others = "".join(f'[sim_{i}]\ntests = ["reset", "wrap"]\n' for i in range(1, len(tools)))
    default = '[top]\ntests = ["reset", "wrap"]\n'
    (root / ".booley/tests.toml").write_text((tests_toml or default) + others)
    git(root, "add", "counter.core", "rtl/counter.sv")
    git(root, "commit", "-m", "simulation recipe")
    record = enter_goal_mode(
        EntryRequest(work_dir=root, slug="seeds", goals=parse_goal_args(goals)),
        entry_environment(layout),
    ).record
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    return root, record


_BYSTANDER = {"family": "COVERAGE", "target": "sim_1", "tests": "all", "metrics": {"line": 100}}
_CORE = "acme:demo:counter:1#"


class _MissedBranchExecution(NativeExecution):
    """Every line point is hit; one branch point never is."""

    def payload(self, hits):
        missed = "C '\x01f\x02rtl/counter.sv\x01l\x023\x01n\x021\x01h\x02TOP.counter"
        return super().payload(hits) + missed + "\x01t\x02branch\x01o\x02true' 0\n"


def _know_targets_by_identity(monkeypatch):
    """Let the Goal Mode fakes know ``sim_1`` and every Target by its core identity."""
    monkeypatch.setattr(conftest, "KNOWN_TARGETS", conftest.KNOWN_TARGETS | {"sim_1"})
    monkeypatch.setattr(
        target_test_plan_loading,
        "checkout_target_identity",
        lambda root, target: conftest.fake_target_identity(root, target.removeprefix(_CORE)),
    )
    monkeypatch.setattr(
        target_test_plan_loading,
        "load_checkout_target_test_plan",
        lambda root, target: conftest.fake_test_plan(root, target.removeprefix(_CORE)),
    )


# One row per case: the Goal's metrics, a factory of what the campaign measured (each run
# needs a fresh one), the Tests it ran, and the Goal's verdict, evaluation status and
# suite status.
def _hit_once():
    return NativeExecution(hits=1)


_COVERAGE_CASES = [
    pytest.param({"line": 100}, _hit_once, None, True, "pass", "match", id="single_metric"),
    pytest.param(
        {"line": 100, "branch": 100},
        _MissedBranchExecution,
        None,
        False,
        "fail",
        "match",
        id="multi_metric",
    ),
    pytest.param({"branch": 100}, _hit_once, None, False, "blocked", "match", id="blocked"),
    pytest.param(
        {"line": 100},
        _hit_once,
        '[top]\ntests = ["reset", "wrap"]\nskip = ["wrap"]\n',
        False,
        "blocked",
        "mismatch",
        id="suite_mismatch",
    ),
]


def _judge_coverage_goal(layout, monkeypatch, target, case):
    """Run one coverage Goal on *target* through the bound flow; return its saved entry.

    Also checks that judging it leaves a coverage Goal of another Target untouched.
    """
    metrics, execution, tests_toml = case
    goals = [
        {"family": "COVERAGE", "target": target, "tests": "all", "metrics": metrics},
        _BYSTANDER,
    ]
    _know_targets_by_identity(monkeypatch)
    root, record = _enter(
        layout, monkeypatch, goals, tools=("verilator", "verilator"), tests_toml=tests_toml
    )
    (key,) = [name for name in record.test_run_resolutions if "top" in name]
    bystander = next(name for name in record.test_run_resolutions if "sim_1" in name)
    store = GoalStore(layout.control)
    before = load_goal_state(store, record).criteria[bystander].to_dict()

    adapter = GoalFlowExecution(bind(layout))
    SimulateFlow(coverage_execution=lambda *_args: execution()).execute(
        SimRequest(target="top", work_dir=root, coverage=True, report_dir=root / "reports"),
        adapter=adapter,
    )
    saved = DevelopmentState.load(adapter.state_file)
    assert len(saved.acceptance_transactions) == 1
    assert load_goal_state(store, record).criteria[bystander].to_dict() == before
    return key, saved.criteria[key]


@pytest.mark.parametrize(
    ("metrics", "execution", "tests_toml", "met", "status", "suite"), _COVERAGE_CASES
)
def test_a_coverage_goal_is_judged_by_its_whole_evaluation_however_its_target_is_written(
    layout, monkeypatch, metrics, execution, tests_toml, met, status, suite
):
    """The Goal's verdict is its evaluation's, whether it names ``top`` or its identity.

    Written by identity the Goal's key is not ``coverage_top``; it is still the one
    Coverage Criterion of its Target, never an atomic metric projection.
    """
    key, entry = _judge_coverage_goal(
        layout, monkeypatch, f"{_CORE}top", (metrics, execution, tests_toml)
    )
    assert key != "coverage_top"
    assert entry.met is met
    assert entry.detail["evaluation"]["status"] == status
    assert entry.detail["evaluation"]["suite"]["status"] == suite
    assert "criterion_metric" not in entry.detail


@pytest.mark.parametrize(
    ("metrics", "execution", "tests_toml", "met", "status", "suite"), _COVERAGE_CASES
)
def test_a_coverage_goal_written_by_name_has_the_same_verdict_and_detail(
    layout, monkeypatch, metrics, execution, tests_toml, met, status, suite
):
    """The plain spelling ``top`` gives the verdict and evaluation of the identity spelling."""
    key, entry = _judge_coverage_goal(layout, monkeypatch, "top", (metrics, execution, tests_toml))
    assert key == "coverage_top"
    assert entry.met is met
    assert entry.detail["evaluation"]["status"] == status
    assert entry.detail["evaluation"]["suite"]["status"] == suite
    assert "criterion_metric" not in entry.detail


def _coverage_run(layout, root, run_id, *tests):
    adapter = GoalFlowExecution(bind(layout, run_id))
    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution(hits=1)).execute(
        SimRequest(
            target="top",
            work_dir=root,
            coverage=True,
            report_dir=root / "reports" / run_id,
            test=tests or None,
        ),
        adapter=adapter,
    )
    return result, DevelopmentState.load(adapter.state_file)


@pytest.mark.timeout(120)  # Windows CI needs 3x the observed 30-33s runtime.
def test_multi_seed_coverage_goal_is_met_only_by_a_campaign_of_exactly_its_frozen_seeds(
    layout, monkeypatch
):
    goals = [
        {"family": "COVERAGE", "target": "top", "tests": ["reset@x2"], "metrics": {"line": 100}}
    ]
    root, record = _enter(layout, monkeypatch, goals)
    frozen = [run.name for run in record.test_run_resolutions["coverage_top"].test_runs]
    assert len(set(frozen)) == 2 and all(name.startswith("reset@") for name in frozen)

    # The default seed of the same Test is not one of the frozen seeds: diagnostic.
    result, state = _coverage_run(layout, root, "default-seed", "reset")
    assert state.criteria["coverage_top"].met is False
    assert state.criteria["coverage_top"].detail == {}
    assert "match no Goal" in result.outcome.report_text
    # One frozen seed alone is not the Goal's campaign either: coverage is not merged
    # across campaigns. Its outcome is recorded for the Goal, its coverage is not.
    _result, state = _coverage_run(layout, root, "one-seed", frozen[0])
    entry = state.criteria["coverage_top"]
    assert entry.met is False and "evaluation" not in entry.detail
    assert [row["name"] for row in entry.detail["test_runs"]] == [frozen[0]]
    assert "did not measure the Goal" in entry.detail["goal_contract_violation"]

    # ``reset@x2`` names the Goal's Target, Test Variant and N: it runs the frozen seeds.
    result, state = _coverage_run(layout, root, "frozen-seeds", "reset@x2")
    assert result.exit_code == 0, result.outcome
    entry = state.criteria["coverage_top"]
    assert entry.met is True
    assert [row["name"] for row in entry.detail["test_runs"]] == frozen
    view = build_status(GoalStore(layout.control), record, work_dir=root)
    status = next(goal for goal in view.goals if goal.key == "coverage_top")
    outcomes = [run.outcome for run in status.test_runs.runs]
    assert outcomes == ["passed", "passed"]
    assert "match no Goal" not in result.outcome.report_text


@pytest.mark.parametrize("grade", ["pass", "fail"])
def test_an_ordinary_run_never_changes_a_measured_coverage_verdict(layout, monkeypatch, grade):
    """After the campaign measured the Goal, a frozen run's outcome elsewhere decides nothing."""
    from tests.goals.test_test_run_evidence import _record, _shown

    goals = [{"family": "COVERAGE", "target": "top", "tests": "all", "metrics": {"line": 100}}]
    root, record = _enter(layout, monkeypatch, goals)
    mode = SimpleNamespace(**vars(layout), record=record)
    frozen = record.test_run_resolutions["coverage_top"].test_runs
    result, state = _coverage_run(layout, root, "campaign")
    assert result.exit_code == 0, result.outcome
    measured = state.criteria["coverage_top"].detail
    before = _status(mode, "coverage_top")
    # (This substituted campaign records no waiver policy, so met evidence reads stale.)
    assert before.status != "unmet" and "goal-test-run-" not in before.reason

    # An ordinary simulation at the same file contents runs one frozen Test Run.
    ordinary = _record(
        mode,
        "ordinary",
        [run_row(frozen[0], grade)],
        key="coverage_top",
        met=False,
        extra={OUTCOMES_ONLY_DETAIL_KEY: True},
    )
    assert ordinary["evaluation"] == measured["evaluation"]  # the measurement, restated
    after = _status(mode, "coverage_top")
    assert (after.status, after.reason) == (before.status, before.reason)
    assert DevelopmentState.load(_state_file(mode)).criteria["coverage_top"].met is True
    assert set(_shown(after).values()) == {"passed"}  # the measuring campaign's outcomes


def test_a_registration_that_is_not_a_simulation_campaign_manifest_cannot_be_read(tmp_path):
    """Only a Simulation Campaign manifest is a readable registration."""
    from booley.flows.sim.unpublished_results import read_unpublished_results

    with pytest.raises(ValueError, match=r"campaign/manifest\.json"):
        read_unpublished_results(tmp_path / "targets/top/coverage.json", "sha256:" + "0" * 64)


def _plain_run(layout, record, route, grade):
    """What a plain sim run (no coverage) records for a SIM and a COVERAGE Goal on ``top``.

    It grades the frozen Test Runs of both Goals. The SIM Goal's verdict is judged from
    the runs; the coverage Goal gets the outcomes-only change a non-coverage run produces.
    """
    sim_rows = [
        run_row(run, grade) for run in record.test_run_resolutions["sim_pass_top"].test_runs
    ]
    cov_rows = [
        run_row(run, grade) for run in record.test_run_resolutions["coverage_top"].test_runs
    ]
    recorder = GoalEvidenceRecorder(bind(layout, f"plain-{route}-{grade}"))
    state = DevelopmentState.load(
        record_paths(layout.control, record.id).state_file, recorder.state_persistence()
    )
    sim_detail = {"test_runs": sim_rows}
    cov_detail = {"test_runs": cov_rows, OUTCOMES_ONLY_DETAIL_KEY: True}
    if route == "v1":
        changes = [
            *state.set_criterion("sim_pass_top", grade == "pass", detail=sim_detail),
            *state.set_criterion("coverage_top", False, detail=cov_detail),
        ]
        recorder.record_changes(state, changes, invocation_id="ignored", producer="sim")
        state.save()
    else:
        shadow = DevelopmentState.from_json_object(state.to_dict())
        changes = [
            *shadow.set_criterion("sim_pass_top", grade == "pass", detail=sim_detail),
            *shadow.set_criterion("coverage_top", False, detail=cov_detail),
        ]
        recorder.record_or_verify_transaction(
            state, changes, acceptance_facts=campaign_facts(), identity={}
        )


def _label(layout, record, key):
    """The dashboard's status label for Goal *key*."""
    view = project_goal(GoalStore(layout.control), record)
    return next(goal.state for goal in view.goals if goal.key == key)


@pytest.mark.parametrize("grade", ["pass", "fail"])
@pytest.mark.parametrize("route", ["v1", "v2"])
def test_a_plain_run_that_grades_a_coverage_goals_frozen_run_never_latches_ever_failed(
    layout, monkeypatch, route, grade
):
    """A plain run measured no coverage, so the coverage Goal has not failed (AC1, AC4)."""
    goals = [
        {"family": "SIM", "target": "top"},
        {"family": "COVERAGE", "target": "top", "tests": "all", "metrics": {"line": 100}},
    ]
    root, record = _enter(layout, monkeypatch, goals)
    mode = SimpleNamespace(**vars(layout), record=record)
    _plain_run(layout, record, route, grade)

    live = DevelopmentState.load(_state_file(mode)).criteria
    assert live["coverage_top"].met is False
    assert live["coverage_top"].ever_failed is False
    # A SIM Goal on the same Target does latch from a failed frozen run.
    assert live["sim_pass_top"].ever_failed is (grade == "fail")
    assert "did not measure the Goal" in live["coverage_top"].detail["goal_contract_violation"]
    assert _label(layout, record, "coverage_top") == "not yet run"
    assert _replayed(mode, "coverage_top").ever_failed is False

    # The measuring run that meets the floors leaves the Criterion met, never failed.
    _result, state = _coverage_run(layout, root, "measuring")
    entry = state.criteria["coverage_top"]
    assert entry.met is True and entry.ever_failed is False
    assert _label(layout, record, "coverage_top") != "failing"
    assert _replayed(mode, "coverage_top").ever_failed is False


def test_an_approved_change_never_turns_a_pending_goal_into_a_failure(layout, monkeypatch):
    """Re-deriving a sibling's unjudged verdict keeps it unjudged (AC6)."""
    goals = [
        {"family": "SIM", "target": "top", "test": "reset@x3"},
        {"family": "SIM", "target": "top", "test": "later"},
    ]
    _root, record = _enter(layout, monkeypatch, goals)
    layout.record = record
    sim_key = next(key for key in record.test_run_resolutions if key.endswith("_x"))
    later = "sim_pass_top_run_6c61746572_d"
    recorder = GoalEvidenceRecorder(bind(layout, "pending-sibling"))
    state = DevelopmentState.load(
        record_paths(layout.control, record.id).state_file, recorder.state_persistence()
    )
    shadow = DevelopmentState.from_json_object(state.to_dict())
    rows = [run_row(run, "pass") for run in record.test_run_resolutions[sim_key].test_runs]
    changes = [
        *shadow.set_criterion(sim_key, True, detail={"test_runs": rows}),
        *shadow.set_criterion(later, False, detail={"test_runs": rows[:1]}),
    ]
    recorder.record_or_verify_transaction(
        state, changes, acceptance_facts=campaign_facts(), identity={}
    )
    assert _replayed(layout, later).ever_failed is False

    env = ChangeEnvironment(entry_environment(layout))
    _change(layout, env, "relax", sim_key, {"family": "SIM", "target": "top", "test": "reset@x2"})
    assert _replayed(layout, later).ever_failed is False
