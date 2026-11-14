"""Durable Goal evidence that was never published is accounted for (ADR 0072).

A cancelled or killed Simulation Campaign leaves authenticated results but
no outcome; a stopped publication leaves a transaction nothing selects.
Neither may let a matching failure be dodged, nor lose a seed that passed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.record import read_durable_results, started_simulation_record
from booley.flows.simulation_record_results import (
    DurableSimulationRecordResults,
    SimulationRecordBusyError,
    StartedSimulationRecord,
)
from booley.flows.test_runs import build_test_run
from booley.goals import simulation_record_accounting
from booley.goals.paths import record_paths
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.simulation_record_accounting import (
    ADOPTED_DETAIL_KEY,
    REASON_UNACCOUNTED,
    RegistrationFormatError,
    SimulationRecordRegistration,
    account_for_simulation_records,
)
from booley.goals.state_store import load_goal_state
from booley.goals.status import build_status, select_status
from booley.goals.store import GoalStore
from tests.goals.conftest import SIM_KEY, bind, enter_goals, run_row, simulation_record_facts
from tests.goals.test_test_run_evidence import (
    TWO_TESTS,
    _contributors,
    _edit_rtl,
    _interrupt,
    _InterruptedError,
    _outcomes,
    _record,
    _state_file,
    _status,
    _transaction,
)
from tests.sharing_error_support import hold_opens, no_retry_delay, sharing_error

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)

A, B = build_test_run("a", seed=1), build_test_run("b", seed=1)
MANIFEST_SHA = f"sha256:{'c' * 64}"
SIMULATION_RECORD = "22222222-2222-4222-8222-222222222222"
#: Booley 0.3.0's own Simulation records, captured as it wrote them.
FIXTURES_0_3_0 = Path(__file__).parents[1] / "flows" / "fixtures" / "booley_0_3_0"


@pytest.fixture
def two(layout: SimpleNamespace) -> SimpleNamespace:
    """An active Goal Mode whose simulation Target Goal is frozen to ``a@1`` and ``b@1``."""
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}], tests_toml=TWO_TESTS)
    return SimpleNamespace(**vars(layout), record=record)


class _SimulationRecord:
    """A registered campaign whose durable results the test controls."""

    def __init__(self, layout: SimpleNamespace) -> None:
        self.layout = layout
        self.rows: list[dict[str, Any]] = []
        self.error: Exception | None = None
        #: Every work item has a result: nothing more can come out of the campaign.
        self.complete = False
        self.manifest = layout.control / "reports" / "campaign" / "manifest.json"
        layout.simulation_record_reader = self.read

    def read(self, path: Path, manifest_sha256: str) -> DurableSimulationRecordResults:
        assert (path, manifest_sha256) == (self.manifest, MANIFEST_SHA)
        if self.error is not None:
            raise self.error
        results = tuple(
            (f"item:{index:04d}", f"sha256:{index:064x}") for index in range(len(self.rows))
        )
        return DurableSimulationRecordResults(
            MANIFEST_SHA, SIMULATION_RECORD, results, tuple(self.rows), complete=self.complete
        )

    def start(self, run_id: str, *, candidate: bool = True) -> GoalEvidenceRecorder:
        """Bind a run and register the campaign as that run starts it."""
        recorder = GoalEvidenceRecorder(
            bind(self.layout, run_id), simulation_record_reader=self.read
        )
        started = StartedSimulationRecord(
            self.manifest, SIMULATION_RECORD, MANIFEST_SHA, ("top", "::top:0#top"), candidate
        )
        recorder.register_simulation_record(started)
        return recorder

    def registrations(self) -> list[SimulationRecordRegistration]:
        """The readable registrations: those of pending campaigns (others may be unreadable)."""
        paths = record_paths(self.layout.control, self.layout.record.id)
        return simulation_record_accounting._stored(paths)[0]

    def registration(self) -> SimulationRecordRegistration:
        (only,) = self.registrations()
        return only


@pytest.fixture
def simulation_record(two: SimpleNamespace) -> _SimulationRecord:
    return _SimulationRecord(two)


def _account(layout: SimpleNamespace) -> None:
    reader = getattr(layout, "simulation_record_reader", None)
    account_for_simulation_records(GoalStore(layout.control), layout.record.id, reader=reader)


def _entry(layout: SimpleNamespace) -> dict[str, Any]:
    """The Goal's state entry as status projects it from the ledger."""
    store = GoalStore(layout.control)
    return load_goal_state(store, store.load(layout.record.id)).criteria[SIM_KEY].to_dict()


# --- A campaign that never published -----------------------------------------------


def test_failure_of_a_killed_simulation_record_is_sticky_for_the_next_simulation_record(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    """The agent sees ``a@1`` fail, kills the campaign, and passes in a fresh one."""
    simulation_record.start("run-1")
    simulation_record.rows = [run_row(A, "fail")]  # ``b`` never ran: the campaign was killed

    _record(two, "run-2", [run_row(A), run_row(B)])

    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
    status = _status(two)
    assert status.status == "unmet" and status.reason.startswith("goal-test-run-failed")
    # ``b`` never got a result: the campaign stays pending, holding only what it started with.
    (path,) = (
        record_paths(two.control, two.record.id).logs_dir / "acceptance/campaigns"
    ).iterdir()
    assert set(json.loads(path.read_bytes())) == {
        *("$schema", "campaign_id", "manifest_sha256", "manifest_path"),
        *("binding", "stamps", "registered_at"),
    }
    listed = simulation_record_accounting.unsettled_campaigns(GoalStore(two.control), two.record)
    assert listed == [
        {
            "state": "open",
            "campaign_id": SIMULATION_RECORD,
            "manifest_path": simulation_record.manifest.as_posix(),
        }
    ]


@pytest.mark.timeout(150)
def test_a_coverage_goal_is_outside_simulation_record_accounting(layout: SimpleNamespace) -> None:
    """No result a campaign left behind can change a Goal judged by coverage floors alone."""
    goals = [
        {"family": "SIM", "target": "top"},
        {"family": "COVERAGE", "target": "top", "tests": "all", "metrics": {"line": 80}},
    ]
    mode = SimpleNamespace(**vars(layout))
    mode.record = enter_goals(layout, goals, tests_toml=TWO_TESTS)
    (coverage_key,) = [key for key in mode.record.test_run_resolutions if key != SIM_KEY]
    simulation_record = _SimulationRecord(mode)
    _record(mode, "run-0", [run_row(A), run_row(B)], key=coverage_key)  # measured
    measured = _status(mode, coverage_key)
    assert measured.status != "unmet"

    simulation_record.start("run-1")
    assert set(simulation_record.registration().stamps) == {SIM_KEY}
    # Running, or unreadable: the simulation Goal waits for it, the coverage Goal does not.
    simulation_record.error = SimulationRecordBusyError("running")
    _record(mode, "run-2", [run_row(A), run_row(B)])
    assert _status(mode).reason.startswith(REASON_UNACCOUNTED)
    assert _status(mode, coverage_key) == measured
    paths = record_paths(mode.control, mode.record.id)
    broken = paths.logs_dir / "acceptance" / "campaigns" / ("f" * 64 + ".json")
    broken.write_bytes(b"{torn")
    assert _status(mode, coverage_key) == measured
    broken.unlink()
    # Killed with a failing frozen run: adopted for the simulation Goal only.
    simulation_record.error = None
    simulation_record.rows = [run_row(A, "fail")]
    _account(mode)
    assert _status(mode).reason.startswith("goal-test-run-failed")
    assert _status(mode, coverage_key) == measured


@pytest.mark.timeout(150)
def test_a_simulation_record_read_before_it_started_is_read_again_after_it_failed(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    """register -> status -> run -> fail -> SIGKILL before publication -> next run."""
    simulation_record.start("run-1")
    # A status read finds the campaign published but not started: no result yet.
    store = GoalStore(two.control)
    select_status(store, two.worktree, simulation_record_reader=simulation_record.read)
    assert len(simulation_record.registrations()) == 1

    # Now the scheduler takes the campaign: while it runs, the Goal cannot be met.
    simulation_record.error = SimulationRecordBusyError("running")
    _record(two, "run-2", [run_row(A), run_row(B)])
    status = _status(two)
    assert status.status == "unmet" and "is still running" in status.reason

    # ``a@1`` fails durably and the process is killed before any publication.
    simulation_record.error = None
    simulation_record.rows = [run_row(A, "fail")]
    _record(two, "run-3", [run_row(A), run_row(B)])
    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
    assert _status(two).reason.startswith("goal-test-run-failed")
    # Still open: it is read again, and nothing is adopted twice.
    _record(two, "run-4", [run_row(A), run_row(B)])
    assert len(_contributors(two)) == 3
    assert len(simulation_record.registrations()) == 1


def test_a_complete_simulation_record_is_settled_and_never_read_again(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    simulation_record.start("run-1")
    simulation_record.rows = [run_row(A), run_row(B, "fail")]
    simulation_record.complete = True
    _account(two)
    assert simulation_record.registrations() == []
    simulation_record.error = AssertionError("a settled campaign was read again")
    _account(two)
    assert _status(two).reason.startswith("goal-test-run-failed")


def test_seeds_that_passed_before_an_interruption_are_kept(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    simulation_record.start("run-1")
    simulation_record.rows = [run_row(A)]

    _account(two)

    entry = _entry(two)
    assert entry["met"] is False
    assert _outcomes(two) == {"a@1": "passed", "b@1": "missing"}
    assert entry["detail"][ADOPTED_DETAIL_KEY]["campaign_id"] == SIMULATION_RECORD
    # The other seed passes in a later campaign: the Goal is met across the two.
    _record(two, "run-2", [run_row(B)])
    assert _status(two).status == "met"


def test_adopted_results_carry_the_contents_the_simulation_record_started_at(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    """A failure seen at older contents is not a failure of the current ones, and comes back."""
    original = (two.worktree / "rtl.v").read_bytes()
    simulation_record.start("run-1")
    simulation_record.rows = [run_row(A, "fail")]
    _edit_rtl(two, "module top; wire fixed; endmodule\n")

    _record(two, "run-2", [run_row(A), run_row(B)])
    # Adopted, but stamped with the old contents: it does not fold with the new ones.
    assert _outcomes(two) == {"a@1": "passed", "b@1": "passed"}
    assert _status(two).status == "met"

    (two.worktree / "rtl.v").write_bytes(original)
    _record(two, "run-3", [run_row(A), run_row(B)])
    assert _outcomes(two)["a@1"] == "failed"


@pytest.mark.parametrize("stop", ["after:ledger_append", "after:registration_delete"])
def test_adoption_is_idempotent_and_survives_a_stop_after_each_write(
    two: SimpleNamespace,
    simulation_record: _SimulationRecord,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    simulation_record.start("run-1")
    simulation_record.rows = [run_row(A, "fail")]
    simulation_record.complete = True
    evidence = record_paths(two.control, two.record.id).logs_dir / "acceptance" / "evidence"
    owner, name = {
        "after:ledger_append": (simulation_record_accounting.evidence_ledger, "record_changes"),
        "after:registration_delete": (simulation_record_accounting, "_settle"),
    }[stop]
    write = getattr(owner, name)

    def stopped(*args: object, **kwargs: object) -> None:
        write(*args, **kwargs)
        raise _InterruptedError(stop)

    with monkeypatch.context() as patched:
        patched.setattr(owner, name, stopped)
        with pytest.raises(_InterruptedError):
            _account(two)
    adopted = sorted(path.name for path in evidence.iterdir() if path.name[0].isdigit())
    assert len(adopted) == 1
    assert len(simulation_record.registrations()) == (stop == "after:ledger_append")
    assert _status(two).reason.startswith("goal-test-run-failed")  # sticky from the append on

    _account(two)
    _account(two)  # a retry finds the adopted observation by its marker: nothing twice
    assert sorted(path.name for path in evidence.iterdir() if path.name[0].isdigit()) == adopted
    assert simulation_record.registrations() == []
    assert _entry(two)["detail"][ADOPTED_DETAIL_KEY]["campaign_id"] == SIMULATION_RECORD


def test_a_simulation_record_that_cannot_be_read_keeps_its_goals_unmet_until_it_is_settled(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    simulation_record.start("run-1")
    simulation_record.error = ValueError("result.json is truncated")
    _record(two, "run-2", [run_row(A), run_row(B)])  # every frozen run passes meanwhile

    status = _status(two)
    assert status.status == "unmet"
    assert status.reason.startswith(REASON_UNACCOUNTED)
    assert "cannot be read (result.json is truncated)" in status.reason
    # A campaign a scheduler still owns is not final either.
    simulation_record.error = SimulationRecordBusyError("running")
    _account(two)
    assert _status(two).reason.startswith(REASON_UNACCOUNTED)

    # Once it can be read (it was resumed, or ended), its failure counts.
    simulation_record.error = None
    simulation_record.rows = [run_row(A, "fail")]
    # A status read settles it.
    store = GoalStore(two.control)
    view = select_status(
        store, two.worktree, simulation_record_reader=simulation_record.read
    ).views[0]
    goal = next(goal for goal in view.goals if goal.key == SIM_KEY)
    assert goal.reason.startswith("goal-test-run-failed")


def test_an_approved_retarget_is_what_drops_an_unaccounted_simulation_record(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    from tests.goals.conftest import bump_spec

    simulation_record.start("run-1")
    simulation_record.error = ValueError("campaign directory disappeared")
    # No agent action clears it: more evidence, more accounting, another registration.
    _record(two, "run-2", [run_row(A), run_row(B)])
    _account(two)
    assert _status(two).reason.startswith(REASON_UNACCOUNTED)

    bump_spec(two, SIM_KEY)  # what a human-approved change does to the Goal
    two.record = GoalStore(two.control).load(two.record.id)
    assert not _status(two).reason.startswith(REASON_UNACCOUNTED)


def test_passes_are_not_adopted_once_the_starting_run_could_no_longer_publish(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    from tests.goals.conftest import edit_protected

    simulation_record.start("run-1")
    simulation_record.rows = [run_row(A), run_row(B, "fail")]
    edit_protected(two)  # a normal publication of run-1 would be discarded now

    _account(two)

    assert _outcomes(two) == {"a@1": "missing", "b@1": "failed"}
    assert (
        len(simulation_record.registrations()) == 1
    )  # pending: ``a@1`` may be adopted while it holds


def test_a_published_simulation_record_is_settled_and_a_diagnostic_target_registers_nothing(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    recorder = simulation_record.start("run-1")
    assert len(simulation_record.registrations()) == 1
    facts = {**simulation_record_facts(SIMULATION_RECORD), "manifest_sha256": MANIFEST_SHA}
    state = DevelopmentState.load(_state_file(two), recorder.state_persistence())
    shadow = DevelopmentState.from_json_object(state.to_dict())
    changes = shadow.set_criterion(SIM_KEY, True, detail={"test_runs": [run_row(A), run_row(B)]})
    recorder.record_or_verify_transaction(state, changes, acceptance_facts=facts, identity={})
    assert simulation_record.registrations() == []
    assert _status(two).status == "met"

    # A Cycle Count baseline campaign is evidence for no Goal: nothing is registered.
    simulation_record.start("run-2", candidate=False)
    assert simulation_record.registrations() == []


def test_without_a_reader_a_simulation_record_stays_unaccounted(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    simulation_record.start("run-1")
    simulation_record.rows = [run_row(A, "fail")]
    two.simulation_record_reader = None  # a caller that cannot read Simulation Campaigns
    _record(two, "run-2", [run_row(A), run_row(B)])
    _account(two)
    assert _status(two).reason.startswith(REASON_UNACCOUNTED)
    assert "cannot be read by this caller" in _status(two).reason
    assert len(simulation_record.registrations()) == 1


def test_observers_read_simulation_record_blockers_without_writing(
    two: SimpleNamespace, simulation_record: _SimulationRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure nobody adopted yet blocks status, the Dashboard and replay alike."""
    from booley.goals.flow_execution import GoalFlowExecution
    from booley.harness.dashboard.model import project_goal

    _record(two, "run-0", [run_row(A), run_row(B)])
    simulation_record.start("run-1")
    simulation_record.rows = [run_row(A, "fail")]  # finished durably; no owner has accounted since
    store = GoalStore(two.control)
    logs = record_paths(two.control, two.record.id).logs_dir
    before = sorted(path.read_bytes() for path in logs.rglob("*") if path.is_file())

    monkeypatch.setattr(
        "booley.flows.sim.unpublished_results.read_unpublished_results", simulation_record.read
    )
    shown = project_goal(store, two.record).status.goals[0]
    assert shown.status == "unmet" and "holds a failure that is not adopted yet" in shown.reason
    view = build_status(
        store, two.record, observational=True, simulation_record_reader=simulation_record.read
    )
    assert view.goals[0].reason == shown.reason
    replay = GoalFlowExecution(bind(two, "run-2"), simulation_record_reader=simulation_record.read)
    assert not replay.criterion_is_current(load_goal_state(store, two.record), SIM_KEY)
    assert sorted(path.read_bytes() for path in logs.rglob("*") if path.is_file()) == before


def test_replay_reads_a_pending_idle_simulation_record_only_through_its_reader(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    """An idle, failure-free pending campaign blocks a reader-less replay and no other."""
    from booley.goals.flow_execution import GoalFlowExecution

    _record(two, "run-0", [run_row(A), run_row(B)])
    simulation_record.start("run-1")
    state = load_goal_state(GoalStore(two.control), two.record)
    assert GoalFlowExecution(
        bind(two, "run-2"), simulation_record_reader=simulation_record.read
    ).criterion_is_current(state, SIM_KEY)
    assert not GoalFlowExecution(bind(two, "run-2")).criterion_is_current(state, SIM_KEY)


def test_registration_refuses_malformed_content() -> None:
    good = SimulationRecordRegistration(
        "c", MANIFEST_SHA, "/x/campaign/manifest.json", {}, {}, "2026-10-13T10:00:00Z"
    )
    assert SimulationRecordRegistration.from_json(good.to_json(), where="r") == good
    for broken in (
        {**good.to_json(), "$schema": "other"},
        {**good.to_json(), "settled": False},
        {**good.to_json(), "registered_at": "yesterday"},
        {key: value for key, value in good.to_json().items() if key != "stamps"},
    ):
        with pytest.raises(RegistrationFormatError):
            SimulationRecordRegistration.from_json(broken, where="r")


# --- A transaction that was never selected ------------------------------------------


@pytest.mark.parametrize("boundary", ["after:acceptance_commit", "before:acceptance_record"])
def test_failure_in_a_stopped_transaction_is_folded_by_the_next_simulation_record(
    two: SimpleNamespace, boundary: str
) -> None:
    """Interrupt a failing publication before it is selected, then pass elsewhere."""
    stopped = GoalEvidenceRecorder(bind(two, "run-a"), publication_checkpoint=_interrupt(boundary))
    with pytest.raises(_InterruptedError):
        _transaction(two, stopped, "11111111-1111-4111-8111-111111111111", [run_row(A, "fail")])
    assert _entry(two)["met"] is False

    _record(two, "run-b", [run_row(A), run_row(B)])

    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
    assert _status(two).reason.startswith("goal-test-run-failed")


@pytest.mark.parametrize("previous_observation", [False, True])
def test_an_unselected_transaction_keeps_its_goal_unmet_until_it_is_completed(
    two: SimpleNamespace,
    previous_observation: bool,
) -> None:
    if previous_observation:
        _record(two, "run-0", [run_row(A), run_row(B)])
        assert _status(two).status == "met"
    stopped = GoalEvidenceRecorder(
        bind(two, "run-a"), publication_checkpoint=_interrupt("after:acceptance_commit")
    )
    with pytest.raises(_InterruptedError):
        _transaction(two, stopped, "11111111-1111-4111-8111-111111111111", [run_row(A, "fail")])

    # Nothing has completed it yet: status does not read the Goal met from the older row.
    status = _status(two)
    assert status.status == "unmet" and "never selected" in status.reason
    _account(two)
    assert _status(two).reason.startswith("goal-test-run-failed")


def test_appended_records_that_were_never_selected_are_selected(two: SimpleNamespace) -> None:
    """The coverage publication appends records, then selects them: stop in between."""
    recorder = GoalEvidenceRecorder(bind(two, "run-a"))
    state = DevelopmentState.load(_state_file(two), recorder.state_persistence())
    changes = state.set_criterion(SIM_KEY, False, detail={"test_runs": [run_row(A, "fail")]})
    recorder.record_changes(
        state, changes, invocation_id="x", producer="sim", transaction_id="e" * 64
    )  # the writer stops here: no state save, no selection

    _record(two, "run-b", [run_row(A), run_row(B)])

    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
    selected = json.loads(_state_file(two).read_text(encoding="utf-8"))["acceptance_transactions"]
    assert "e" * 64 in selected


# --- Reading durable campaign results -------------------------------------------------


def test_durable_results_of_a_real_simulation_record_are_its_authenticated_rows(
    tmp_path: Path,
) -> None:
    from booley.flows.sim.record.codec import SimulationRecordIntegrityError
    from booley.flows.sim.record.planning import manifest_digest
    from booley.flows.sim.record.test_run_report import evidence_test_run_rows
    from tests.flows.sim.test_simulation_record_test_runs import _hdl_plan, _run_simulation_record

    plan = _hdl_plan(tmp_path, ((A,), (B,)))
    outcome = _run_simulation_record(tmp_path, plan)
    digest = manifest_digest(plan.manifest)

    durable = read_durable_results(outcome.manifest_path, digest)
    started = started_simulation_record(outcome.manifest_path)
    assert (started.simulation_record_id, started.manifest_sha256) == (
        durable.simulation_record_id,
        digest,
    )
    assert started.candidate is True and str(outcome.target["selector"]) in started.target_names

    assert list(durable.rows) == evidence_test_run_rows(outcome)
    assert [row["grade"] for row in durable.rows] == ["fail", "fail"]  # no simulator ran
    assert len(durable.results) == 2
    with pytest.raises(SimulationRecordIntegrityError, match="not the one that was registered"):
        read_durable_results(outcome.manifest_path, "sha256:" + "0" * 64)

    # A work item whose result is gone is simply not there; a torn one fails closed.
    results = sorted(outcome.manifest_path.parent.glob("work-items/*/result.json"))
    kept = results[1].read_bytes()
    results[1].unlink()
    partial = read_durable_results(outcome.manifest_path, digest)
    assert [row["name"] for row in partial.rows] == ["a@1"]
    results[1].write_bytes(kept[: len(kept) // 2])
    with pytest.raises(SimulationRecordIntegrityError):
        read_durable_results(outcome.manifest_path, digest)


# --- A registration nobody can read -------------------------------------------------


def test_an_unreadable_registration_blocks_until_an_approved_change_quarantines_it(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    from booley.goals.apply import ChangeEnvironment
    from booley.goals.change_service import create_proposal
    from tests.goals.conftest import entry_environment
    from tests.goals.test_proposals import approve

    simulation_record.start("run-1")
    simulation_record.complete = True
    paths = record_paths(two.control, two.record.id)
    directory = paths.logs_dir / "acceptance" / "campaigns"
    broken = directory / ("f" * 64 + ".json")
    broken.write_bytes(b"{torn")
    _record(two, "run-2", [run_row(A), run_row(B)])  # the readable one is still accounted for
    assert simulation_record.registrations() == []

    status = _status(two)
    assert status.status == "unmet" and status.reason.startswith(REASON_UNACCOUNTED)
    assert f"campaign registration {broken.name} cannot be read" in status.reason
    assert "moved aside when a human approves a Goal change" in status.reason
    # More evidence and more accounting do not clear it.
    _record(two, "run-3", [run_row(A), run_row(B)])
    _account(two)
    assert _status(two).reason.startswith(REASON_UNACCOUNTED)
    store = GoalStore(two.control)
    assert {
        "state": "unreadable",
        "file": broken.name,
    } in simulation_record_accounting.unsettled_campaigns(store, two.record)

    env = ChangeEnvironment(entry_environment(two))
    arguments = {
        "kind": "add",
        "after": {"family": "SIM", "target": "top", "test": "a@9"},
        "rationale": "the human looked at the unreadable registration",
    }
    view = create_proposal(env, two.record.id, arguments, session_key="test-session")
    assert approve(two, env, view.proposal.id).state == "applied"

    assert not broken.exists() and (directory / "quarantine" / broken.name).is_file()
    two.record = store.load(two.record.id)
    assert _status(two).status == "met"  # judged by its evidence again
    assert simulation_record_accounting.unsettled_campaigns(store, two.record) == [
        {"state": "quarantined", "file": broken.name}
    ]
    assert simulation_record_accounting.quarantine_unreadable(store, two.record.id) == ()


def test_the_review_package_lists_simulation_records_that_were_never_fully_accounted_for() -> None:
    from booley.goals.finish_presentation import _unsettled_simulation_record_lines

    assert _unsettled_simulation_record_lines({}) == []
    listed = [{"state": "quarantined", "file": "x.json"}]
    lines = _unsettled_simulation_record_lines({"unsettled_campaigns": listed})
    assert lines[1] == "Simulation Campaigns not fully accounted for"
    assert '"quarantined"' in lines[2]


@pytest.mark.timeout(150)
def test_finish_accounts_first_and_refuses_while_a_simulation_record_runs_or_cannot_be_read(
    two: SimpleNamespace, simulation_record: _SimulationRecord
) -> None:
    from dataclasses import replace

    from booley.goals.finish import finish_goal
    from booley.goals.lifecycle import LifecycleError
    from tests.goals.conftest import git
    from tests.goals.test_finish import environment, request

    git(two.main, "config", "user.name", "Goal Test")
    git(two.main, "config", "user.email", "goal@test.invalid")
    _record(two, "run-0", [run_row(A), run_row(B)])
    assert _status(two).status == "met"
    simulation_record.start("run-1")
    env = replace(environment(two), simulation_record_reader=simulation_record.read)

    def refused() -> str:
        with pytest.raises(LifecycleError, match="must be met and fresh before finish"):
            finish_goal(request(two), env)
        return _status(two).reason

    simulation_record.error = SimulationRecordBusyError("running")
    assert "is still running" in refused()
    simulation_record.error = ValueError("manifest is gone")
    assert "cannot be read (manifest is gone)" in refused()
    # Idle and readable: Finish adopts what it left, here a failure, and refuses for that.
    simulation_record.error = None
    simulation_record.rows = [run_row(A, "fail")]
    assert refused().startswith("goal-test-run-failed")


# --- Windows sharing errors (#2187) -------------------------------------------------


def _registered(two: SimpleNamespace, simulation_record: _SimulationRecord) -> tuple[Path, str]:
    simulation_record.start("run-1")
    (stored,) = (
        record_paths(two.control, two.record.id).logs_dir / "acceptance" / "campaigns"
    ).glob("*.json")
    return stored, stored.name


def test_registration_read_survives_transient_sharing_error(
    two, simulation_record, monkeypatch
) -> None:
    stored, _ = _registered(two, simulation_record)
    hold_opens(monkeypatch, {stored: 1})
    assert [found.simulation_record_id for found in simulation_record.registrations()] == [
        SIMULATION_RECORD
    ]


def test_lasting_registration_sharing_error_chains_cause(
    two, simulation_record, monkeypatch
) -> None:
    stored, _ = _registered(two, simulation_record)
    hold_opens(monkeypatch, {stored: 5})
    with pytest.raises(simulation_record_accounting.RegistrationFormatError) as caught:
        simulation_record_accounting._read(stored)
    assert getattr(caught.value.__cause__, "winerror", None) == 32


def test_quarantine_move_retries_transient_sharing_error(
    two, simulation_record, monkeypatch
) -> None:
    stored, name = _registered(two, simulation_record)
    stored.write_bytes(b"{torn")
    no_retry_delay(monkeypatch)
    real_replace = Path.replace
    held = [1]

    def replace_once_held(path, target):
        if held[0]:
            held[0] -= 1
            raise sharing_error()
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace_once_held)
    assert simulation_record_accounting.quarantine_unreadable(
        GoalStore(two.control), two.record.id
    ) == (name,)
    assert (stored.parent / "quarantine" / name).is_file()
