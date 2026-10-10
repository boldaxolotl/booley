"""Goal evidence in the ledger: identity groups, fences, stamps, recovery, and the tree golden."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.criteria import evidence_ledger
from booley.criteria.evidence_ledger import AcceptanceLedgerError, canonical_json
from booley.criteria.state import DevelopmentState
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.execution_persistence import AcceptanceRecordingError
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.goals.freshness import GoalFreshnessResolvers
from booley.goals.paths import record_paths
from booley.goals.publication import EvidenceDiscarded
from booley.goals.recorder import (
    GOAL_SCOPE,
    GoalEvidenceRecorder,
    GoalIdentity,
    goal_identity,
    validate_goal_identity,
)
from booley.goals.simulation import GOAL_SUITE_DETAIL_KEY
from booley.goals.status import build_status
from booley.goals.store import GoalStore
from booley.ticket_board.acceptance_ledger import TicketIdentity
from tests.goals.conftest import (
    LINT_KEY,
    SIM_KEY,
    bind,
    bump_spec,
    campaign_facts,
    enter_goals,
)
from tests.golden.conftest import assert_matches_golden, normalize_work_dir

FIXED_NOW = "2026-10-07T08:00:00Z"
PASSING_SIM = {"required_tests": ["smoke"], "passed_tests": ["smoke"]}
BOUNDARIES = (
    "before:acceptance_intent",
    "after:acceptance_intent",
    "before:acceptance_record",
    "after:acceptance_record",
    "before:acceptance_commit",
    "after:acceptance_commit",
    "before:acceptance_state",
    "after:acceptance_state",
)


class _InterruptedError(Exception):
    """Simulated process death at one publication checkpoint."""


def _paths(layout: SimpleNamespace) -> Any:
    return record_paths(layout.control, layout.record.id)


def _load(layout: SimpleNamespace, recorder: GoalEvidenceRecorder) -> DevelopmentState:
    return DevelopmentState.load(_paths(layout).state_file, recorder.state_persistence())


def _disk_state(layout: SimpleNamespace) -> dict[str, Any]:
    return json.loads(_paths(layout).state_file.read_text(encoding="utf-8"))


def _v1(
    layout: SimpleNamespace,
    recorder: GoalEvidenceRecorder,
    key: str,
    met: bool,
    detail: dict[str, Any],
) -> DevelopmentState:
    state = _load(layout, recorder)
    changes = state.set_criterion(key, met, detail=detail)
    recorder.record_changes(
        state, changes, invocation_id="ignored", producer=key.split("_", maxsplit=1)[0]
    )
    state.save()
    return state


def _v2(
    layout: SimpleNamespace,
    recorder: GoalEvidenceRecorder,
    state: DevelopmentState | None = None,
) -> tuple[DevelopmentState, Any]:
    state = state or _load(layout, recorder)
    shadow = DevelopmentState.from_json_object(state.to_dict())
    changes = [
        *shadow.set_criterion(SIM_KEY, True, detail=dict(PASSING_SIM)),
        *shadow.set_criterion(LINT_KEY, True, detail={"warnings": 0}),
    ]
    transaction = recorder.record_or_verify_transaction(
        state, changes, acceptance_facts=campaign_facts(), ticket_identity={}
    )
    return state, transaction


# ---------------------------------------------------------------------------
# V1 and V2 through the gate
# ---------------------------------------------------------------------------


def test_v1_records_a_stamped_observation_under_its_identity_group(
    goal_mode: SimpleNamespace,
) -> None:
    recorder = GoalEvidenceRecorder(bind(goal_mode))

    state = _v1(goal_mode, recorder, LINT_KEY, True, {"warnings": 0})

    identity = goal_identity(goal_mode.record.id, {LINT_KEY: 1})
    (record,) = evidence_ledger.read_evidence_records(
        GOAL_SCOPE, recorder.log_dir, state, identity
    )
    assert record["purpose"] == "goal_evidence"
    assert record["goal_record"] == goal_mode.record.id
    assert record["invocation_id"] == "run-1"
    stamp = record["detail"][SOURCE_FINGERPRINT_DETAIL_KEY]
    assert "target_surface" in stamp["categories"]
    assert isinstance(stamp["fingerprint"]["target_surface"]["digest"], str)
    disk = _disk_state(goal_mode)["criteria"][LINT_KEY]
    assert disk["met"] is True
    assert disk["detail"] == record["detail"]


def test_v1_partial_simulation_pass_is_recorded_unmet(goal_mode: SimpleNamespace) -> None:
    recorder = GoalEvidenceRecorder(bind(goal_mode))

    state = _v1(
        goal_mode,
        recorder,
        SIM_KEY,
        True,
        {"required_tests": [], "passed_tests": [], "tests_passed": 1, "tests_total": 1},
    )

    entry = state.criteria[SIM_KEY]
    assert entry.met is False
    assert entry.detail[GOAL_SUITE_DETAIL_KEY] == ["smoke"]
    assert "complete resolved suite" in entry.detail["goal_contract_violation"]
    assert _disk_state(goal_mode)["criteria"][SIM_KEY]["met"] is False
    goal = next(
        g
        for g in build_status(GoalStore(goal_mode.control), goal_mode.record).goals
        if g.key == SIM_KEY
    )
    assert goal.status == "unmet"
    assert goal.evidence_summary == "1/1 tests"
    assert goal.reason == entry.detail["goal_contract_violation"]

    _v1(
        goal_mode,
        GoalEvidenceRecorder(bind(goal_mode, "run-2")),
        SIM_KEY,
        False,
        {**PASSING_SIM, "passed_tests": [], "tests_passed": 0, "tests_total": 1},
    )
    goal = next(
        g
        for g in build_status(GoalStore(goal_mode.control), goal_mode.record).goals
        if g.key == SIM_KEY
    )
    assert (goal.status, goal.evidence_summary, goal.reason) == ("unmet", "0/1 tests", "")


def test_v1_complete_simulation_pass_records_its_suite(goal_mode: SimpleNamespace) -> None:
    recorder = GoalEvidenceRecorder(bind(goal_mode))

    state = _v1(goal_mode, recorder, SIM_KEY, True, dict(PASSING_SIM))

    assert state.criteria[SIM_KEY].met is True
    assert state.criteria[SIM_KEY].detail[GOAL_SUITE_DETAIL_KEY] == ["smoke"]
    goal = next(
        g
        for g in build_status(GoalStore(goal_mode.control), goal_mode.record).goals
        if g.key == SIM_KEY
    )
    assert (goal.status, goal.reason) == ("met", "")


def test_v2_transaction_selects_its_identity_group(goal_mode: SimpleNamespace) -> None:
    recorder = GoalEvidenceRecorder(bind(goal_mode))

    state, transaction = _v2(goal_mode, recorder)

    disk = _disk_state(goal_mode)
    assert disk["acceptance_transactions"] == [transaction.transaction_id]
    assert disk["criteria"][LINT_KEY]["met"] is True
    assert disk["criteria"][SIM_KEY]["met"] is True
    assert state.acceptance_transactions == [transaction.transaction_id]
    group = goal_identity(goal_mode.record.id, {LINT_KEY: 1, SIM_KEY: 1})
    records = evidence_ledger.read_evidence_records(GOAL_SCOPE, recorder.log_dir, state, group)
    assert [record["criterion"] for record in records] == [SIM_KEY, LINT_KEY]
    _replayed_state, replayed = _v2(goal_mode, recorder, state)
    assert replayed == transaction


def test_v2_whole_transaction_is_rejected_when_one_member_changed(
    goal_mode: SimpleNamespace,
) -> None:
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    bump_spec(goal_mode, SIM_KEY)

    with pytest.raises(EvidenceDiscarded, match=f"Goal {SIM_KEY} changed"):
        _v2(goal_mode, recorder)

    assert not (recorder.log_dir / "acceptance").exists()


# ---------------------------------------------------------------------------
# The identity codec
# ---------------------------------------------------------------------------

_VALID = {
    "purpose": "goal_evidence",
    "record_id": "evidence-20261006T120000Z",
    "goal_keys": ["lint_clean_top", "sim_pass_top"],
    "spec_revisions": {"lint_clean_top": 1, "sim_pass_top": 2},
}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"purpose": "ticket_acceptance"}, "another purpose"),
        ({"record_id": "not-a-goal"}, "names no Goal Record"),
        ({"goal_keys": ["sim_pass_top", "lint_clean_top"]}, "sorted, distinct"),
        ({"goal_keys": ["lint_clean_top", "lint_clean_top"]}, "sorted, distinct"),
        ({"goal_keys": []}, "sorted, distinct"),
        ({"spec_revisions": {"lint_clean_top": 1}}, "every Goal it names"),
        ({"spec_revisions": {"lint_clean_top": True, "sim_pass_top": 1}}, "positive integers"),
        ({"spec_revisions": {"lint_clean_top": 0, "sim_pass_top": 1}}, "positive integers"),
        ({"generation": "d" * 32}, "unexpected fields"),
    ],
)
def test_goal_identity_rejects_malformed_identities(change: dict[str, Any], message: str) -> None:
    with pytest.raises(AcceptanceLedgerError, match=message):
        validate_goal_identity({**_VALID, **change})


def test_goal_and_ticket_codecs_reject_each_other() -> None:
    assert validate_goal_identity(_VALID) == _VALID
    with pytest.raises(AcceptanceLedgerError):
        GoalIdentity().lookup_identity({"generation": "d" * 32})
    with pytest.raises(AcceptanceLedgerError, match="ticket generation"):
        TicketIdentity().lookup_identity(_VALID)
    with pytest.raises(AcceptanceLedgerError, match="another Goal Record"):
        GoalIdentity().validate_observation_identity(
            _VALID, {**_VALID, "record_id": "other-20261006T120000Z"}
        )


# ---------------------------------------------------------------------------
# Stale specifications: replay, projection, and the intent cross-check
# ---------------------------------------------------------------------------


def test_replay_and_projection_under_a_stale_spec_are_rejected(
    goal_mode: SimpleNamespace,
) -> None:
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    state, _transaction = _v2(goal_mode, recorder)
    old_group = goal_identity(goal_mode.record.id, {LINT_KEY: 1, SIM_KEY: 1})
    bump_spec(goal_mode, SIM_KEY)
    projection = recorder.projection()

    with pytest.raises(EvidenceDiscarded, match=f"Goal {SIM_KEY} changed"):
        _v2(goal_mode, recorder, state)
    with pytest.raises(AcceptanceLedgerError, match="recorded under specification revision 1"):
        evidence_ledger.replay_projection(
            GOAL_SCOPE, projection, recorder.log_dir, state, old_group
        )
    with pytest.raises(AcceptanceLedgerError, match="recorded under specification revision 1"):
        evidence_ledger.validate_state_projection(
            GOAL_SCOPE, projection, recorder.log_dir, state, old_group
        )


def _interrupt(boundary: str) -> Callable[[str], None]:
    def checkpoint(seen: str) -> None:
        if seen == boundary:
            raise _InterruptedError(seen)

    return checkpoint


def test_intent_whose_envelope_names_an_older_spec_is_rejected(
    goal_mode: SimpleNamespace,
) -> None:
    first = GoalEvidenceRecorder(
        bind(goal_mode), publication_checkpoint=_interrupt("after:acceptance_intent")
    )
    with pytest.raises(_InterruptedError):
        _v2(goal_mode, first)
    (intent_path,) = (first.log_dir / "acceptance" / "intents").iterdir()
    intent = json.loads(intent_path.read_bytes())
    bump_spec(goal_mode, SIM_KEY)
    current = GoalEvidenceRecorder(bind(goal_mode, "run-2"))
    # Forge the intent a current retry looks up: current lookup key, old envelope.
    lookup = {**intent["lookup_key"], "goal_identity": current.identity_for([LINT_KEY, SIM_KEY])}
    forged = {**intent, "lookup_key": lookup}
    forged_path = intent_path.parent / f"{hashlib.sha256(canonical_json(lookup)).hexdigest()}.json"
    forged_path.write_bytes(canonical_json(forged) + b"\n")

    with pytest.raises(AcceptanceRecordingError, match="envelope does not match its lookup key"):
        _v2(goal_mode, current)


def test_ticket_intent_cross_check_rejects_a_mismatched_envelope(tmp_path: Path) -> None:
    from booley.ticket_board import acceptance_ledger as ticket_ledger

    state = DevelopmentState.load(tmp_path / "state.json")
    state.slug = "fix-uart"
    state.init_criteria({"lint_clean": True}, strict=True)
    changes = state.set_criterion("lint_clean", True, detail={})
    identity = {"generation": "d" * 32}
    with pytest.raises(_InterruptedError):
        ticket_ledger.record_or_verify_transaction(
            tmp_path,
            state,
            changes,
            acceptance_facts=campaign_facts(),
            ticket_identity=identity,
            publication_checkpoint=_interrupt("after:acceptance_intent"),
        )
    (intent_path,) = (tmp_path / "acceptance" / "intents").iterdir()
    intent = json.loads(intent_path.read_bytes())
    intent["envelope"]["ticket"]["identity"] = {"generation": "e" * 32}
    intent["envelope"]["ticket"]["generation"] = "e" * 32
    intent_path.write_bytes(canonical_json(intent) + b"\n")

    with pytest.raises(AcceptanceLedgerError, match="envelope does not match its lookup key"):
        ticket_ledger.record_or_verify_transaction(
            tmp_path, state, changes, acceptance_facts=campaign_facts(), ticket_identity=identity
        )


# ---------------------------------------------------------------------------
# Crash at every publication checkpoint, then recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_interrupted_goal_transaction_recovers(goal_mode: SimpleNamespace, boundary: str) -> None:
    binding = bind(goal_mode)
    interrupted = GoalEvidenceRecorder(binding, publication_checkpoint=_interrupt(boundary))
    with pytest.raises(_InterruptedError):
        _v2(goal_mode, interrupted)

    recovered_state, recovered = _v2(goal_mode, GoalEvidenceRecorder(binding))
    _replayed_state, replayed = _v2(goal_mode, GoalEvidenceRecorder(binding))

    assert replayed == recovered
    disk = _disk_state(goal_mode)
    assert disk["acceptance_transactions"] == [recovered.transaction_id]
    assert disk["criteria"][SIM_KEY]["met"] is True
    assert recovered_state.criteria[LINT_KEY].met is True


# ---------------------------------------------------------------------------
# The Goal evidence tree golden
# ---------------------------------------------------------------------------


def _render_tree(paths: Any) -> str:
    """Render the ledger beneath ``logs/`` byte for byte, then ``booley_state.json``."""
    lines: list[str] = []
    logs = paths.logs_dir
    for path in sorted(logs.rglob("*"), key=lambda item: item.relative_to(logs).as_posix()):
        relative = path.relative_to(logs).as_posix()
        if path.is_dir():
            lines.append(f"=== dir {relative}")
        elif path.name.endswith(".lock"):
            lines.append(f"=== lock {relative}")  # lock files hold no evidence
        else:
            content = path.read_bytes()
            lines.append(f"=== file {relative} ({len(content)} bytes)")
            lines.append(content.decode("utf-8"))
    # No byte count: the state names the worktree, whose path length varies.
    lines.append("=== file booley_state.json")
    lines.append(paths.state_file.read_bytes().decode("utf-8"))
    return "\n".join(lines)


def _normalize(text: str, root: Path) -> str:
    """Replace *root* in plain and JSON-escaped spellings (Windows paths hold backslashes).

    The state file stores ``work_dir`` natively, so on Windows the part after
    the placeholder still holds (JSON-escaped) backslashes; it is folded to
    POSIX so every platform renders the same bytes.
    """
    for spelling in sorted({str(root), str(root.resolve())}, key=len, reverse=True):
        text = text.replace(json.dumps(spelling)[1:-1], str(root))
    text = normalize_work_dir(text, root)
    return re.sub(
        r"<WORK>[^\"\s]*",
        lambda match: match.group(0).replace("\\\\", "/").replace("\\", "/"),
        text,
    )


def _path_free_source(work_dir: Path, *, target: str | None) -> dict[str, Any]:
    """The real source fingerprint without its absolute ``work_dir``, which hashes differ by."""
    fingerprint = compute_source_fingerprint(work_dir, target=target)
    fingerprint.pop("work_dir")
    return fingerprint


def test_goal_evidence_tree_matches_golden(
    layout: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for module in ("criteria.state", "criteria.evidence_ledger", "goals.state_store"):
        monkeypatch.setattr(f"booley.{module}.utc_now_rfc3339", lambda: FIXED_NOW)
    goal = SimpleNamespace(**vars(layout), record=enter_goals(layout))
    recorder = GoalEvidenceRecorder(
        bind(goal), resolvers=GoalFreshnessResolvers(source=_path_free_source)
    )

    lint = _v1(goal, recorder, LINT_KEY, False, {"warnings": 2})
    _state, transaction = _v2(goal, recorder)
    result = {
        "transaction": asdict(transaction),
        "lint_after_v1": lint.criteria[LINT_KEY].met,
    }

    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n" + _render_tree(_paths(goal))
    assert_matches_golden("goal_evidence/goal_evidence_tree.txt", _normalize(rendered, tmp_path))


def test_state_saving_around_the_gate_is_refused(goal_mode: SimpleNamespace) -> None:
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    state = DevelopmentState.load(_paths(goal_mode).state_file)  # default persistence
    changes = state.set_criterion(LINT_KEY, True, detail={})

    with pytest.raises(AcceptanceRecordingError, match="Goal state persistence"):
        recorder.record_changes(state, changes, invocation_id="x", producer="lint")


# ---------------------------------------------------------------------------
# Round-1 review regressions
# ---------------------------------------------------------------------------


def test_recovery_keeps_a_newer_observation_from_an_overlapping_group(
    goal_mode: SimpleNamespace,
) -> None:
    """{lint, sim} commits, crashes before selection; a later lint failure must survive."""
    binding = bind(goal_mode)
    crashed = GoalEvidenceRecorder(
        binding, publication_checkpoint=_interrupt("before:acceptance_state")
    )
    with pytest.raises(_InterruptedError):
        _v2(goal_mode, crashed)
    _v1(
        goal_mode, GoalEvidenceRecorder(bind(goal_mode, "run-2")), LINT_KEY, False, {"warnings": 5}
    )

    state, recovered = _v2(goal_mode, GoalEvidenceRecorder(binding))

    disk = _disk_state(goal_mode)
    assert disk["acceptance_transactions"] == [recovered.transaction_id]
    assert disk["criteria"][LINT_KEY]["met"] is False
    assert disk["criteria"][LINT_KEY]["detail"]["warnings"] == 5
    assert disk["criteria"][SIM_KEY]["met"] is True
    _replayed_state, replayed = _v2(goal_mode, GoalEvidenceRecorder(binding), state)
    assert replayed == recovered  # validation compares against the newest observation


def test_recovery_from_a_stale_instance_keeps_a_concurrently_selected_observation(
    goal_mode: SimpleNamespace,
) -> None:
    """Round 2: the recovering instance loaded its state before another run published.

    T1 {lint, sim} commits and crashes before selection. The recovery
    instance loads state. A concurrent run then publishes and selects T2, a
    failing lint alone. Recovery must see T2 although its own transaction
    list predates it, both in memory and in the first state it saves.
    """
    binding = bind(goal_mode)
    crashed = GoalEvidenceRecorder(
        binding, publication_checkpoint=_interrupt("before:acceptance_state")
    )
    with pytest.raises(_InterruptedError):
        _v2(goal_mode, crashed)
    recovering = GoalEvidenceRecorder(binding)
    stale = _load(goal_mode, recovering)  # loaded before T2 exists

    concurrent = GoalEvidenceRecorder(bind(goal_mode, "run-2"))
    other = _load(goal_mode, concurrent)
    shadow = DevelopmentState.from_json_object(other.to_dict())
    changes = shadow.set_criterion(LINT_KEY, False, detail={"warnings": 9})
    second = concurrent.record_or_verify_transaction(
        other, changes, acceptance_facts=campaign_facts(), ticket_identity={}
    )
    assert _disk_state(goal_mode)["acceptance_transactions"] == [second.transaction_id]

    state, recovered = _v2(goal_mode, recovering, stale)

    assert state.criteria[LINT_KEY].met is False
    assert state.criteria[LINT_KEY].detail["warnings"] == 9
    assert state.criteria[SIM_KEY].met is True
    disk = _disk_state(goal_mode)
    assert sorted(disk["acceptance_transactions"]) == sorted(
        [second.transaction_id, recovered.transaction_id]
    )
    assert disk["criteria"][LINT_KEY]["met"] is False
    assert disk["criteria"][LINT_KEY]["detail"]["warnings"] == 9
    assert disk["criteria"][SIM_KEY]["met"] is True


def test_target_changed_during_the_run_is_discarded(goal_mode: SimpleNamespace) -> None:
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    core = goal_mode.worktree / "top.core"
    core.write_bytes(core.read_bytes() + b"# a parameter changed mid-run\n")
    before = _paths(goal_mode).state_file.read_bytes()

    with pytest.raises(EvidenceDiscarded, match="Target top changed during the run"):
        _v1(goal_mode, recorder, LINT_KEY, True, {"warnings": 0})

    assert _paths(goal_mode).state_file.read_bytes() == before
    assert not (recorder.log_dir / "acceptance").exists()


def test_complete_coverage_campaign_detail_stays_met(goal_mode: SimpleNamespace) -> None:
    """The Coverage producer's ``sim_pass`` detail satisfies the Goal simulation contract.

    Coverage names no ``required_tests`` (its Ticket detail is unchanged); the
    contract reads the ``selected_tests`` it ran instead.
    """
    from booley.flows.sim.coverage_acceptance import (
        _simulation_detail,  # pyright: ignore[reportPrivateUsage]
    )

    plan = SimpleNamespace(
        handle=SimpleNamespace(identity="::top:0#top"),
        selected_tests=("smoke",),
        declared_tests=("smoke",),
    )
    campaign = SimpleNamespace(
        campaign_id="c" * 32, runs=[SimpleNamespace(test="smoke", simulation_verdict="pass")]
    )
    detail = _simulation_detail(plan, campaign, Path("unused"))  # type: ignore[arg-type]

    state = _v1(goal_mode, GoalEvidenceRecorder(bind(goal_mode)), SIM_KEY, True, detail)

    assert state.criteria[SIM_KEY].met is True
    assert "goal_contract_violation" not in state.criteria[SIM_KEY].detail


def test_v2_partial_simulation_reason_survives_projection(goal_mode):
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    state = _load(goal_mode, recorder)
    shadow = DevelopmentState.from_json_object(state.to_dict())
    changes = shadow.set_criterion(
        SIM_KEY,
        True,
        detail={
            "required_tests": [],
            "passed_tests": [],
            "tests_passed": 1,
            "tests_total": 1,
        },
    )
    recorder.record_or_verify_transaction(
        state, changes, acceptance_facts=campaign_facts(), ticket_identity={}
    )
    goal = next(
        g
        for g in build_status(GoalStore(goal_mode.control), goal_mode.record).goals
        if g.key == SIM_KEY
    )
    assert (goal.status, goal.evidence_summary) == ("unmet", "1/1 tests")
    assert (
        goal.reason
        == _disk_state(goal_mode)["criteria"][SIM_KEY]["detail"]["goal_contract_violation"]
    )


@pytest.mark.parametrize("met", [False, True])
def test_unresolvable_simulation_suite_exposes_reason(goal_mode, met):
    def unresolved(_root, _target):
        raise ValueError("suite unavailable")

    recorder = GoalEvidenceRecorder(
        bind(goal_mode), resolvers=GoalFreshnessResolvers(simulation_suite=unresolved)
    )
    state = _v1(goal_mode, recorder, SIM_KEY, met, dict(PASSING_SIM))
    goal = next(
        g
        for g in build_status(GoalStore(goal_mode.control), goal_mode.record).goals
        if g.key == SIM_KEY
    )
    assert goal.status == "unmet"
    assert goal.reason == state.criteria[SIM_KEY].detail["goal_contract_violation"]
    assert "suite unavailable" in goal.reason
