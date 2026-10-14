"""Goal evidence in the ledger: identity groups, fences, stamps, recovery, and the tree golden."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, replace
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
from tests.criteria.ledger_support import FixtureIdentity
from tests.goals.conftest import (
    LINT_KEY,
    SIM_KEY,
    bind,
    bump_spec,
    campaign_facts,
    enter_goals,
    passing_sim,
)
from tests.golden.conftest import assert_matches_golden, normalize_work_dir

FIXED_NOW = "2026-10-07T08:00:00Z"
PASSING_SIM = passing_sim()
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
        state, changes, acceptance_facts=campaign_facts(), identity={}
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

    with pytest.raises(
        EvidenceDiscarded, match=r"Goal SIM:top \(all selected tests pass\) changed"
    ):
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
        FixtureIdentity().lookup_identity(_VALID)
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

    with pytest.raises(
        EvidenceDiscarded, match=r"Goal SIM:top \(all selected tests pass\) changed"
    ):
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
    from tests.criteria import ledger_support as fixture_ledger

    state = DevelopmentState.load(tmp_path / "state.json")
    state.slug = "fix-uart"
    state.init_criteria({"lint_clean": True}, strict=True)
    changes = state.set_criterion("lint_clean", True, detail={})
    identity = {"generation": "d" * 32}
    with pytest.raises(_InterruptedError):
        fixture_ledger.record_or_verify_transaction(
            tmp_path,
            state,
            changes,
            acceptance_facts=campaign_facts(),
            identity=identity,
            publication_checkpoint=_interrupt("after:acceptance_intent"),
        )
    (intent_path,) = (tmp_path / "acceptance" / "intents").iterdir()
    intent = json.loads(intent_path.read_bytes())
    intent["envelope"]["ticket"]["identity"] = {"generation": "e" * 32}
    intent["envelope"]["ticket"]["generation"] = "e" * 32
    intent_path.write_bytes(canonical_json(intent) + b"\n")

    with pytest.raises(AcceptanceLedgerError, match="envelope does not match its lookup key"):
        fixture_ledger.record_or_verify_transaction(
            tmp_path, state, changes, acceptance_facts=campaign_facts(), identity=identity
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
        other, changes, acceptance_facts=campaign_facts(), identity={}
    )
    # Before it recorded anything, the concurrent run completed the crashed transaction
    # (ADR 0072: no new evidence while an earlier transaction is left unselected).
    first, last = _disk_state(goal_mode)["acceptance_transactions"]
    assert last == second.transaction_id

    state, recovered = _v2(goal_mode, recovering, stale)
    assert recovered.transaction_id == first

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


@pytest.mark.parametrize("schedule", ["lost_save", "reverse_save", "async_save"])
def test_canonical_evidence_survives_late_or_missing_save(goal_mode, schedule):
    import asyncio

    recorder = GoalEvidenceRecorder(bind(goal_mode))
    older = _load(goal_mode, recorder)
    newer = _load(goal_mode, recorder)
    report = _load(goal_mode, recorder)

    def publish(state, warnings):
        changes = state.set_criterion(LINT_KEY, True, detail={"warnings": warnings})
        recorder.record_changes(state, changes, invocation_id="race", producer="lint")
        if state is older:
            # A report copies back the published result without adopting its baseline.
            report.criteria[LINT_KEY] = deepcopy(state.criteria[LINT_KEY])

    async def interleave():
        published = asyncio.Event()
        saved = asyncio.Event()

        async def first():
            publish(older, 1)
            published.set()
            await saved.wait()
            report.save()

        async def second():
            await published.wait()
            publish(newer, 0)
            newer.save()
            saved.set()

        await asyncio.wait_for(asyncio.gather(first(), second()), 5)

    if schedule == "async_save":
        asyncio.run(interleave())
    else:
        publish(older, 1)
        if schedule == "reverse_save":
            publish(newer, 0)
            newer.save()
            report.save()
    disk = _load(goal_mode, recorder)
    row = evidence_ledger.validated_evidence_records(GOAL_SCOPE, recorder.log_dir, disk, {})[-1]
    persisted = _disk_state(goal_mode)["criteria"][LINT_KEY]
    assert persisted["detail"] == row["detail"]
    assert persisted["params"] == row["params"]
    assert disk.criteria[LINT_KEY].met == row["met"]
    assert disk.criteria[LINT_KEY].params == row["params"]
    assert disk.criteria[LINT_KEY].detail == row["detail"]


def test_async_producer_adapters_keep_final_observations_after_old_reports(layout):
    """External EDA/model results are stubbed at the actual EndpointState adapter."""
    import asyncio

    from booley.evidence.review_receipt import FILTERING_SEMANTICS_REVISION
    from booley.flows.endpoint_state import EndpointState
    from booley.goals.status import build_status
    from booley.goals.store import GoalStore

    class Producer(EndpointState):
        def _run(self):
            raise AssertionError("External execution is outside this persistence test")

    goals = [{"family": family, "target": "top"} for family in ("LINT", "ELAB", "SYNTH")]
    goals += [
        {"family": "REVIEW", "review": kind, "verdict": "clean", "paths": ["rtl.v"]}
        for kind in ("rtl_bugs", "rtl_protocol")
    ]
    layout = SimpleNamespace(**vars(layout), record=enter_goals(layout, goals))
    recorder = GoalEvidenceRecorder(bind(layout))
    snapshots = []

    def publish(goal, final=False):
        endpoint = Producer()
        endpoint.name = (
            "reviewer" if goal.spec.family.value == "review" else goal.spec.family.value
        )
        endpoint.configure_flow_execution(recorder)
        endpoint._args = SimpleNamespace(
            work_dir=str(layout.worktree),
            target=goal.spec.target,
            state_file=_paths(layout).state_file,
            diagnostic=False,
        )
        endpoint.read_state()
        report = _load(layout, recorder)
        endpoint.set_criterion(
            goal.spec.key,
            True,
            detail={
                "warnings": 0,
                "issues": 0,
                "pending": [],
                "final": final,
                "contract": {
                    "scope": ["rtl.v"],
                    "category": "rtl",
                    "filtering_semantics_revision": FILTERING_SEMANTICS_REVISION,
                },
            },
            source_target=goal.spec.target,
        )
        report.criteria[goal.spec.key] = deepcopy(endpoint.state.criteria[goal.spec.key])
        return report

    async def run():
        ready = [asyncio.Event() for _ in range(len(layout.record.goals) + 1)]
        ready[0].set()

        async def producer(index, goal):
            await ready[index].wait()
            (layout.worktree / "rtl.v").write_text(f"module top; // source {index}\nendmodule\n")
            snapshots.append(publish(goal))
            ready[index + 1].set()

        await asyncio.wait_for(
            asyncio.gather(*(producer(i, goal) for i, goal in enumerate(layout.record.goals))), 10
        )
        before_rerun = build_status(GoalStore(layout.control), layout.record)
        assert before_rerun.goals[0].status == "stale"
        # Every old report loaded before this final set of completed producer runs.
        for goal in layout.record.goals:
            publish(goal, final=True)
        for snapshot in reversed(snapshots):
            snapshot.save()

    asyncio.run(run())
    _assert_final_projection(layout, recorder)


def _assert_final_projection(layout, recorder):
    from booley.goals.status import build_status
    from booley.goals.store import GoalStore

    disk = _load(layout, recorder)
    history = evidence_ledger.validated_evidence_records(GOAL_SCOPE, recorder.log_dir, disk, {})
    assert len(history) == 2 * len(layout.record.goals)
    latest = {row["criterion"]: row for row in history}
    persisted = _disk_state(layout)["criteria"]
    for key, row in latest.items():
        assert persisted[key]["detail"] == row["detail"]
        assert persisted[key]["params"] == row["params"]
        assert disk.criteria[key].detail == row["detail"]
        assert disk.criteria[key].params == row["params"]
        assert row["detail"]["final"] is True
    status = build_status(GoalStore(layout.control), layout.record)
    assert not any(goal.state_conflict for goal in status.goals)
    assert all(goal.status == "met" for goal in status.goals), [
        (g.key, g.status, g.reason) for g in status.goals
    ]


def test_projection_recovers_canonical_nested_detail_without_remerging(goal_mode):
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    state = _load(goal_mode, recorder)
    _v1(goal_mode, recorder, LINT_KEY, True, {"warnings": 0, "old": "remove"})
    changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    changes = [
        replace(
            changes[0],
            params={"target": "top", "runtime": {"pins": [1, 2]}},
            detail={
                "warnings": 0,
                "nested": {"tuple": (1, {"items": [2, 3]})},
                "receipt_id": "producer-receipt",
            },
        )
    ]
    recorder.record_changes(state, changes, invocation_id="canonical", producer="lint")
    disk = _load(goal_mode, recorder)
    row = evidence_ledger.validated_evidence_records(GOAL_SCOPE, recorder.log_dir, disk, {})[-1]
    assert disk.criteria[LINT_KEY].params == row["params"]
    assert disk.criteria[LINT_KEY].detail == row["detail"]
    assert "old" not in disk.criteria[LINT_KEY].detail
    assert row["detail"]["nested"]["tuple"] == [1, {"items": [2, 3]}]
    assert disk.criteria[LINT_KEY].updated_at == row["recorded_at"]


def test_in_memory_publication_also_mirrors_canonical_stamped_fields(goal_mode):
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    state = DevelopmentState.from_json_object(_load(goal_mode, recorder).to_dict())
    changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    recorder.record_changes(state, changes, invocation_id="memory", producer="lint")
    row = evidence_ledger.validated_evidence_records(GOAL_SCOPE, recorder.log_dir, state, {})[-1]
    assert state.criteria[LINT_KEY].detail == row["detail"]


@pytest.mark.parametrize("family", ["LINT", "ELAB", "SYNTH", "FPGA", "MUTATION", "CYCLE_COUNT"])
def test_rerun_replaces_conflicting_cache_for_each_flow_family(layout, family):
    from booley.goals.status import build_status
    from booley.goals.store import GoalStore

    argument = {"family": family, "target": "top"}
    if family == "MUTATION":
        argument["scope"] = ["rtl.v"]
    if family == "CYCLE_COUNT":
        argument.update(test="smoke", thresholds={"cycle_count_max": 200})
    layout = SimpleNamespace(**vars(layout), record=enter_goals(layout, [argument]))
    key = layout.record.goals[0].spec.key
    recorder = GoalEvidenceRecorder(bind(layout))
    _v1(layout, recorder, key, False, {"warnings": 4})
    path = _paths(layout).state_file
    cached = json.loads(path.read_bytes())
    cached["criteria"][key].update(met=True, detail={"conflicting": "old report"})
    path.write_text(json.dumps(cached))
    detail = {
        **PASSING_SIM,
        "warnings": 0,
        "cycles": 100,
        "cycle_observation": "observed",
        "detected": 10,
        "total_valid": 10,
        "min_detected": 10,
    }
    state = _v1(layout, recorder, key, True, detail)
    row = evidence_ledger.validated_evidence_records(GOAL_SCOPE, recorder.log_dir, state, {})[-1]
    assert state.criteria[key].detail == row["detail"]
    assert state.criteria[key].params == row["params"]
    assert "conflicting" not in state.criteria[key].detail
    status = build_status(GoalStore(layout.control), layout.record).goals[0]
    assert status.status == "met", status.reason


def test_baseline_role_invalidation_does_not_resurrect_an_older_candidate(goal_mode):
    from booley.criteria.state import CriterionChange
    from booley.evidence.fields import SOURCE_INVALIDATED_REASON
    from booley.goals.status import build_status
    from booley.goals.store import GoalStore

    path = _paths(goal_mode).state_file
    raw = json.loads(path.read_bytes())
    raw["criteria"][LINT_KEY]["params"]["from_state"] = "fail"
    path.write_text(json.dumps(raw))
    recorder = GoalEvidenceRecorder(bind(goal_mode))
    state = _v1(goal_mode, recorder, LINT_KEY, True, {"warnings": 0})
    keys = state.reset_category("rtl")
    assert LINT_KEY in keys
    change = CriterionChange(
        LINT_KEY,
        False,
        SOURCE_INVALIDATED_REASON,
        dict(state.criteria[LINT_KEY].detail),
        True,
        dict(state.criteria[LINT_KEY].params),
    )
    recorder.record_changes(state, [change], invocation_id="reset", producer="coder")
    loaded = _load(goal_mode, recorder)
    assert loaded.criteria[LINT_KEY].stale is True
    assert loaded.criteria[LINT_KEY].met is False
    view = build_status(GoalStore(goal_mode.control), goal_mode.record)
    assert next(goal for goal in view.goals if goal.key == LINT_KEY).status == "unmet"
