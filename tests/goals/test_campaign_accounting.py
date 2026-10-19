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
from booley.flows.campaign_results import (
    CampaignBusyError,
    DurableCampaignResults,
    StartedCampaign,
)
from booley.flows.sim.campaign import read_durable_results, started_campaign
from booley.flows.test_runs import build_test_run
from booley.goals import campaign_accounting
from booley.goals.campaign_accounting import (
    ADOPTED_DETAIL_KEY,
    REASON_UNACCOUNTED,
    CampaignRegistration,
    RegistrationFormatError,
    account_for_campaigns,
)
from booley.goals.paths import record_paths
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.status import select_status
from booley.goals.store import GoalStore
from tests.goals.conftest import SIM_KEY, bind, campaign_facts, enter_goals, run_row
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

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)

A, B = build_test_run("a", seed=1), build_test_run("b", seed=1)
MANIFEST_SHA = f"sha256:{'c' * 64}"
CAMPAIGN = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def two(layout: SimpleNamespace) -> SimpleNamespace:
    """An active Goal Mode whose simulation Target Goal is frozen to ``a@1`` and ``b@1``."""
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}], tests_toml=TWO_TESTS)
    return SimpleNamespace(**vars(layout), record=record)


class _Campaign:
    """A registered campaign whose durable results the test controls."""

    def __init__(self, layout: SimpleNamespace) -> None:
        self.layout = layout
        self.rows: list[dict[str, Any]] = []
        self.error: Exception | None = None
        #: Every work item has a result: nothing more can come out of the campaign.
        self.complete = False
        self.unfinished: tuple[tuple[str, str], ...] = ()
        self.manifest = layout.control / "reports" / "campaign" / "manifest.json"
        layout.campaign_reader = self.read

    def read(self, path: Path, manifest_sha256: str) -> DurableCampaignResults:
        assert (path, manifest_sha256) == (self.manifest, MANIFEST_SHA)
        if self.error is not None:
            raise self.error
        results = tuple(
            (f"item:{index:04d}", f"sha256:{index:064x}") for index in range(len(self.rows))
        )
        return DurableCampaignResults(
            MANIFEST_SHA,
            CAMPAIGN,
            results,
            tuple(self.rows),
            complete=self.complete,
            unfinished=self.unfinished,
        )

    def start(self, run_id: str, *, candidate: bool = True) -> GoalEvidenceRecorder:
        """Bind a run and register the campaign as that run starts it."""
        recorder = GoalEvidenceRecorder(bind(self.layout, run_id), campaign_reader=self.read)
        started = StartedCampaign(
            self.manifest, CAMPAIGN, MANIFEST_SHA, ("top", "::top:0#top"), candidate
        )
        recorder.register_campaign(started)
        return recorder

    def registrations(self) -> list[CampaignRegistration]:
        paths = record_paths(self.layout.control, self.layout.record.id)
        found = sorted((paths.logs_dir / "acceptance" / "campaigns").glob("*.json"))
        return [
            CampaignRegistration.from_json(json.loads(path.read_bytes()), where="test")
            for path in found
        ]

    def registration(self) -> CampaignRegistration:
        (only,) = self.registrations()
        return only

    def registration_settled(self) -> bool:
        """Whether this campaign's own registration is settled (others may be unreadable)."""
        paths = record_paths(self.layout.control, self.layout.record.id)
        found, _unreadable = campaign_accounting._stored(paths)
        (only,) = found
        return only.settled


@pytest.fixture
def campaign(two: SimpleNamespace) -> _Campaign:
    return _Campaign(two)


def _account(layout: SimpleNamespace) -> None:
    reader = getattr(layout, "campaign_reader", None)
    account_for_campaigns(GoalStore(layout.control), layout.record.id, reader=reader)


def _entry(layout: SimpleNamespace) -> dict[str, Any]:
    return json.loads(_state_file(layout).read_text(encoding="utf-8"))["criteria"][SIM_KEY]


# --- A campaign that never published -----------------------------------------------


def test_failure_of_a_killed_campaign_is_sticky_for_the_next_campaign(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    """The agent sees ``a@1`` fail, kills the campaign, and passes in a fresh one."""
    campaign.start("run-1")
    campaign.rows = [run_row(A, "fail")]  # ``b`` never ran: the campaign was killed

    _record(two, "run-2", [run_row(A), run_row(B)])

    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
    status = _status(two)
    assert status.status == "unmet" and status.reason.startswith("goal-test-run-failed")
    # ``b`` never got a result: the campaign is not settled, and it blocks nothing.
    registration = campaign.registration()
    assert (registration.settled, registration.blocked) == (False, "")


@pytest.mark.timeout(150)
def test_a_coverage_goal_is_outside_campaign_accounting(layout: SimpleNamespace) -> None:
    """No result a campaign left behind can change a Goal judged by coverage floors alone."""
    goals = [
        {"family": "SIM", "target": "top"},
        {"family": "COVERAGE", "target": "top", "tests": "all", "metrics": {"line": 80}},
    ]
    mode = SimpleNamespace(**vars(layout))
    mode.record = enter_goals(layout, goals, tests_toml=TWO_TESTS)
    (coverage_key,) = [key for key in mode.record.test_run_resolutions if key != SIM_KEY]
    campaign = _Campaign(mode)
    _record(mode, "run-0", [run_row(A), run_row(B)], key=coverage_key)  # measured
    measured = _status(mode, coverage_key)
    assert measured.status != "unmet"

    campaign.start("run-1")
    assert set(campaign.registration().stamps) == {SIM_KEY}
    # Running, or unreadable: the simulation Goal waits for it, the coverage Goal does not.
    campaign.error = CampaignBusyError("running")
    _record(mode, "run-2", [run_row(A), run_row(B)])
    assert _status(mode).reason.startswith(REASON_UNACCOUNTED)
    assert _status(mode, coverage_key) == measured
    paths = record_paths(mode.control, mode.record.id)
    broken = paths.logs_dir / "acceptance" / "campaigns" / ("f" * 64 + ".json")
    broken.write_bytes(b"{torn")
    assert _status(mode, coverage_key) == measured
    broken.unlink()
    # Killed with a failing frozen run: adopted for the simulation Goal only.
    campaign.error = None
    campaign.rows = [run_row(A, "fail")]
    _account(mode)
    assert _status(mode).reason.startswith("goal-test-run-failed")
    assert _status(mode, coverage_key) == measured


@pytest.mark.timeout(150)
def test_a_campaign_read_before_it_started_is_read_again_after_it_failed(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    """register -> status -> run -> fail -> SIGKILL before publication -> next run."""
    campaign.start("run-1")
    assert campaign.registration().blocked  # nothing has read it yet: its Goals are unmet
    # A status read finds the campaign published but not started: no result yet.
    store = GoalStore(two.control)
    select_status(store, two.worktree, campaign_reader=campaign.read)
    registration = campaign.registration()
    assert (registration.settled, registration.blocked, registration.accounted) == (False, "", ())

    # Now the scheduler takes the campaign: while it runs, the Goal cannot be met.
    campaign.error = CampaignBusyError("running")
    _record(two, "run-2", [run_row(A), run_row(B)])
    status = _status(two)
    assert status.status == "unmet" and "is still running" in status.reason

    # ``a@1`` fails durably and the process is killed before any publication.
    campaign.error = None
    campaign.rows = [run_row(A, "fail")]
    campaign.unfinished = ((B.name, B.fingerprint),)
    _record(two, "run-3", [run_row(A), run_row(B)])
    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
    assert _status(two).reason.startswith("goal-test-run-failed")
    # Still open: it is read again, and nothing is adopted twice.
    _record(two, "run-4", [run_row(A), run_row(B)])
    assert len(_contributors(two)) == 3
    assert campaign.registration().settled is False


def test_a_complete_campaign_is_settled_and_never_read_again(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    campaign.start("run-1")
    campaign.rows = [run_row(A), run_row(B, "fail")]
    campaign.complete = True
    _account(two)
    assert campaign.registration().settled is True
    campaign.error = AssertionError("a settled campaign was read again")
    _account(two)
    assert _status(two).reason.startswith("goal-test-run-failed")


def test_a_frozen_run_that_was_started_and_left_no_result_is_named(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    campaign.start("run-1")
    campaign.rows = [run_row(A)]
    campaign.unfinished = ((B.name, B.fingerprint),)
    _account(two)
    status = _status(two)
    assert status.status == "unmet"
    assert f"b@1 started in Simulation Campaign {CAMPAIGN}" in status.reason
    assert "left no result" in status.reason
    listed = campaign_accounting.unsettled_campaigns(GoalStore(two.control), two.record)
    assert listed == [
        {
            "state": "open",
            "campaign_id": CAMPAIGN,
            "manifest_path": campaign.manifest.as_posix(),
            "blocked": "",
            "started_without_result": ["b@1"],
        }
    ]


def test_seeds_that_passed_before_an_interruption_are_kept(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    campaign.start("run-1")
    campaign.rows = [run_row(A)]

    _account(two)

    entry = _entry(two)
    assert entry["met"] is False
    assert _outcomes(two) == {"a@1": "passed", "b@1": "missing"}
    assert entry["detail"][ADOPTED_DETAIL_KEY]["campaign_id"] == CAMPAIGN
    # The other seed passes in a later campaign: the Goal is met across the two.
    _record(two, "run-2", [run_row(B)])
    assert _status(two).status == "met"


def test_adopted_results_carry_the_contents_the_campaign_started_at(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    """A failure seen at older contents is not a failure of the current ones, and comes back."""
    original = (two.worktree / "rtl.v").read_bytes()
    campaign.start("run-1")
    campaign.rows = [run_row(A, "fail")]
    _edit_rtl(two, "module top; wire fixed; endmodule\n")

    _record(two, "run-2", [run_row(A), run_row(B)])
    # Adopted, but stamped with the old contents: it does not fold with the new ones.
    assert _outcomes(two) == {"a@1": "passed", "b@1": "passed"}
    assert _status(two).status == "met"

    (two.worktree / "rtl.v").write_bytes(original)
    _record(two, "run-3", [run_row(A), run_row(B)])
    assert _outcomes(two)["a@1"] == "failed"


def test_adoption_is_idempotent_and_survives_a_stop_after_each_write(
    two: SimpleNamespace, campaign: _Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign.start("run-1")
    campaign.rows = [run_row(A, "fail")]
    evidence = record_paths(two.control, two.record.id).logs_dir / "acceptance" / "evidence"

    # Stop after the ledger append, before the state file and the registration.
    def stop(*_args: object, **_kwargs: object) -> None:
        raise _InterruptedError("after:ledger")

    with monkeypatch.context() as patched:
        patched.setattr(campaign_accounting, "write_goal_state_file", stop)
        with pytest.raises(_InterruptedError):
            _account(two)
    assert len(list(evidence.iterdir() if evidence.exists() else [])) >= 1
    rows_after_stop = sorted(path.name for path in evidence.iterdir() if path.name[0].isdigit())
    assert campaign.registration().settled is False
    # A late old report must not be merged into the adopted immutable observation.
    path = record_paths(two.control, two.record.id).state_file
    cached = json.loads(path.read_bytes())
    cached["criteria"][SIM_KEY]["detail"] = {"obsolete_report_field": "old"}
    path.write_text(json.dumps(cached))
    # The state differs from its newest observation: never met, and named unaccounted.
    assert _status(two).status == "unmet"

    # Stop after the state file, before the registration.
    with monkeypatch.context() as patched:
        patched.setattr(campaign_accounting, "_write", stop)
        with pytest.raises(_InterruptedError):
            _account(two)
    assert _outcomes(two)["a@1"] == "failed"
    from booley.criteria.evidence_ledger import validated_evidence_records
    from booley.goals.recorder import GOAL_SCOPE
    from booley.goals.state_store import load_goal_state

    store = GoalStore(two.control)
    state = load_goal_state(store, store.load(two.record.id))
    selected = validated_evidence_records(GOAL_SCOPE, path.parent / "logs", state, {})[-1]
    assert _entry(two)["detail"] == selected["detail"]
    assert "obsolete_report_field" not in selected["detail"]

    _account(two)
    _account(two)  # an open registration is read on every pass: nothing is adopted twice
    assert sorted(path.name for path in evidence.iterdir() if path.name[0].isdigit()) == (
        rows_after_stop
    )
    assert (campaign.registration().settled, campaign.registration().blocked) == (False, "")
    assert _status(two).reason.startswith("goal-test-run-failed")


def test_a_campaign_that_cannot_be_read_keeps_its_goals_unmet_until_it_is_settled(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    campaign.start("run-1")
    campaign.error = ValueError("result.json is truncated")
    _record(two, "run-2", [run_row(A), run_row(B)])  # every frozen run passes meanwhile

    status = _status(two)
    assert status.status == "unmet"
    assert status.reason.startswith(REASON_UNACCOUNTED)
    assert "cannot be read (result.json is truncated)" in status.reason
    # A campaign a scheduler still owns is not final either.
    campaign.error = CampaignBusyError("running")
    _account(two)
    assert _status(two).reason.startswith(REASON_UNACCOUNTED)

    # Once it can be read (it was resumed, or ended), its failure counts.
    campaign.error = None
    campaign.rows = [run_row(A, "fail")]
    # A status read settles it.
    store = GoalStore(two.control)
    view = select_status(store, two.worktree, campaign_reader=campaign.read).views[0]
    goal = next(goal for goal in view.goals if goal.key == SIM_KEY)
    assert goal.reason.startswith("goal-test-run-failed")


def test_an_approved_retarget_is_what_drops_an_unaccounted_campaign(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    from tests.goals.conftest import bump_spec

    campaign.start("run-1")
    campaign.error = ValueError("campaign directory disappeared")
    # No agent action clears it: more evidence, more accounting, another registration.
    _record(two, "run-2", [run_row(A), run_row(B)])
    _account(two)
    assert _status(two).reason.startswith(REASON_UNACCOUNTED)

    bump_spec(two, SIM_KEY)  # what a human-approved change does to the Goal
    two.record = GoalStore(two.control).load(two.record.id)
    assert not _status(two).reason.startswith(REASON_UNACCOUNTED)


def test_passes_are_not_adopted_once_the_starting_run_could_no_longer_publish(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    from tests.goals.conftest import edit_protected

    campaign.start("run-1")
    campaign.rows = [run_row(A), run_row(B, "fail")]
    edit_protected(two)  # a normal publication of run-1 would be discarded now

    _account(two)

    assert _outcomes(two) == {"a@1": "missing", "b@1": "failed"}
    assert campaign.registration().blocked == ""


def test_a_published_campaign_is_settled_and_a_diagnostic_target_registers_nothing(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    recorder = campaign.start("run-1")
    assert campaign.registration().settled is False
    facts = {**campaign_facts(CAMPAIGN), "manifest_sha256": MANIFEST_SHA}
    state = DevelopmentState.load(_state_file(two), recorder.state_persistence())
    shadow = DevelopmentState.from_json_object(state.to_dict())
    changes = shadow.set_criterion(SIM_KEY, True, detail={"test_runs": [run_row(A), run_row(B)]})
    recorder.record_or_verify_transaction(state, changes, acceptance_facts=facts, identity={})
    assert campaign.registration().settled is True
    assert _status(two).status == "met"

    # A Cycle Count baseline campaign is evidence for no Goal: nothing is registered.
    campaign.start("run-2", candidate=False)
    assert len(campaign.registrations()) == 1


def test_without_a_reader_a_campaign_stays_unaccounted(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    campaign.start("run-1")
    campaign.rows = [run_row(A, "fail")]
    two.campaign_reader = None  # a caller that cannot read Simulation Campaigns
    _record(two, "run-2", [run_row(A), run_row(B)])
    _account(two)
    assert _status(two).reason.startswith(REASON_UNACCOUNTED)
    assert campaign.registration().settled is False


def test_registration_refuses_malformed_content() -> None:
    good = CampaignRegistration(
        "c", MANIFEST_SHA, "/x/campaign/manifest.json", {}, {}, "2026-10-13T10:00:00Z"
    )
    assert CampaignRegistration.from_json(good.to_json(), where="r") == good
    for broken in (
        {**good.to_json(), "$schema": "other"},
        {**good.to_json(), "settled": "yes"},
        {**good.to_json(), "accounted": [["only-one"]]},
        {**good.to_json(), "registered_at": "yesterday"},
        {key: value for key, value in good.to_json().items() if key != "stamps"},
    ):
        with pytest.raises(RegistrationFormatError):
            CampaignRegistration.from_json(broken, where="r")


# --- A transaction that was never selected ------------------------------------------


@pytest.mark.parametrize("boundary", ["after:acceptance_commit", "before:acceptance_record"])
def test_failure_in_a_stopped_transaction_is_folded_by_the_next_campaign(
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


def test_durable_results_of_a_real_campaign_are_its_authenticated_rows(tmp_path: Path) -> None:
    from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
    from booley.flows.sim.campaign.planning import manifest_digest
    from booley.flows.sim.campaign.test_run_report import evidence_test_run_rows
    from tests.flows.sim.test_campaign_test_runs import _hdl_plan, _run_campaign

    plan = _hdl_plan(tmp_path, ((A,), (B,)))
    outcome = _run_campaign(tmp_path, plan)
    digest = manifest_digest(plan.manifest)

    durable = read_durable_results(outcome.manifest_path, digest)
    started = started_campaign(outcome.manifest_path)
    assert (started.campaign_id, started.manifest_sha256) == (durable.campaign_id, digest)
    assert started.candidate is True and str(outcome.target["selector"]) in started.target_names

    assert list(durable.rows) == evidence_test_run_rows(outcome)
    assert [row["grade"] for row in durable.rows] == ["fail", "fail"]  # no simulator ran
    assert len(durable.results) == 2
    with pytest.raises(SimulationCampaignIntegrityError, match="not the one that was registered"):
        read_durable_results(outcome.manifest_path, "sha256:" + "0" * 64)

    # A work item whose result is gone is simply not there; a torn one fails closed.
    results = sorted(outcome.manifest_path.parent.glob("work-items/*/result.json"))
    kept = results[1].read_bytes()
    results[1].unlink()
    partial = read_durable_results(outcome.manifest_path, digest)
    assert [row["name"] for row in partial.rows] == ["a@1"]
    results[1].write_bytes(kept[: len(kept) // 2])
    with pytest.raises(SimulationCampaignIntegrityError):
        read_durable_results(outcome.manifest_path, digest)


# --- A registration nobody can read -------------------------------------------------


def test_an_unreadable_registration_blocks_until_an_approved_change_quarantines_it(
    two: SimpleNamespace, campaign: _Campaign
) -> None:
    from booley.goals.apply import ChangeEnvironment
    from booley.goals.change_service import create_proposal
    from tests.goals.conftest import entry_environment
    from tests.goals.test_proposals import approve

    campaign.start("run-1")
    campaign.complete = True
    paths = record_paths(two.control, two.record.id)
    directory = paths.logs_dir / "acceptance" / "campaigns"
    broken = directory / ("f" * 64 + ".json")
    broken.write_bytes(b"{torn")
    _record(two, "run-2", [run_row(A), run_row(B)])  # the readable one is still accounted for
    assert campaign.registration_settled()

    status = _status(two)
    assert status.status == "unmet" and status.reason.startswith(REASON_UNACCOUNTED)
    assert f"campaign registration {broken.name} cannot be read" in status.reason
    assert "moved aside when a human approves a Goal change" in status.reason
    # More evidence and more accounting do not clear it.
    _record(two, "run-3", [run_row(A), run_row(B)])
    _account(two)
    assert _status(two).reason.startswith(REASON_UNACCOUNTED)
    store = GoalStore(two.control)
    assert {"state": "unreadable", "file": broken.name} in campaign_accounting.unsettled_campaigns(
        store, two.record
    )

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
    assert campaign_accounting.unsettled_campaigns(store, two.record) == [
        {"state": "quarantined", "file": broken.name}
    ]
    assert campaign_accounting.quarantine_unreadable(store, two.record.id) == ()


def test_the_review_package_lists_campaigns_that_were_never_fully_accounted_for() -> None:
    from booley.goals.finish_presentation import _unsettled_campaign_lines

    assert _unsettled_campaign_lines({}) == []
    listed = [{"state": "quarantined", "file": "x.json"}]
    lines = _unsettled_campaign_lines({"unsettled_campaigns": listed})
    assert lines[1] == "Simulation Campaigns not fully accounted for"
    assert '"quarantined"' in lines[2]


@pytest.mark.timeout(150)
def test_finish_accounts_first_and_refuses_while_a_campaign_runs_or_cannot_be_read(
    two: SimpleNamespace, campaign: _Campaign
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
    campaign.start("run-1")
    env = replace(environment(two), campaign_reader=campaign.read)

    def refused() -> str:
        with pytest.raises(LifecycleError, match="must be met and fresh before finish"):
            finish_goal(request(two), env)
        return _status(two).reason

    campaign.error = CampaignBusyError("running")
    assert "is still running" in refused()
    campaign.error = ValueError("manifest is gone")
    assert "cannot be read (manifest is gone)" in refused()
    # Idle and readable: Finish adopts what it left, here a failure, and refuses for that.
    campaign.error = None
    campaign.rows = [run_row(A, "fail")]
    assert refused().startswith("goal-test-run-failed")
