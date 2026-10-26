"""One locked, durable Goal apply and its shared restart recovery entry point.

Intent is the commit point. All fallible preparation precedes it; recovery
publishes the captured bytes despite later source drift, and refuses external
destination edits. Ordinary bindings have no authority to cross its barrier.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from booley.core.boundary import require_dict, require_list, require_uuid4
from booley.criteria.evidence_ledger import prepare_observations
from booley.criteria.state import CriterionChange, CriterionEntry, DevelopmentState
from booley.goals.apply_barrier import require_no_apply
from booley.goals.apply_effects import FileEffect, read_bytes
from booley.goals.campaign_accounting import quarantine_unreadable
from booley.goals.change_policy import conflict, validate_kind
from booley.goals.changes import (
    ChangeEntry,
    ChangeKind,
    append_applied,
    append_intent,
    read_change_log,
)
from booley.goals.derivation import CoverageEvaluation, Derivation, derive_changes
from booley.goals.entry import EntryEnvironment
from booley.goals.input_identity import main_booley_dir_entry
from booley.goals.model import GoalRecord, GoalSpec, GoalState, RecordedGoal
from booley.goals.paths import record_paths
from booley.goals.proposals import (
    Decision,
    Proposal,
    ProposalError,
    ProposalView,
    digest,
    encode,
    finalize,
    list_proposals,
    save_decision,
)
from booley.goals.recorder import GOAL_SCOPE, goal_identity
from booley.goals.state_store import load_goal_state, review_categories
from booley.goals.store import GoalStore, RecordLock, frozen_run_violation
from booley.goals.test_run_freezing import (
    GoalResolutionError,
    ResolutionRequest,
    SeedDraw,
    TargetPlans,
    refreeze_changed_goal,
)
from booley.goals.test_run_goals import names_test_runs
from booley.goals.test_run_resolution import TestRunResolution
from booley.goals.waiver_binding import WaiverBinding
from booley.runtime.atomic_files import atomic_write_once
from booley.runtime.timefmt import utc_now_rfc3339


class WaiverPort(Protocol):
    """Composition supplies concrete coverage/candidate policy, never Goal imports."""

    def bind(self, record: GoalRecord, goal_key: str, candidate_id: str) -> WaiverBinding: ...
    def locking(self, binding: WaiverBinding) -> AbstractContextManager[None]: ...
    def prepare(
        self, proposal: Proposal, decision: Decision, state: DevelopmentState
    ) -> tuple[tuple[FileEffect, ...], CoverageEvaluation]: ...
    def rejection(self, proposal: Proposal, decision: Decision) -> tuple[FileEffect, ...]: ...
    def validate_effect(self, effect: FileEffect) -> None: ...
    def evaluate(
        self, goal: GoalSpec, detail: Mapping[str, Any]
    ) -> tuple[bool, dict[str, Any]]: ...


@dataclass(frozen=True)
class ChangeEnvironment:
    """The real preparation/coverage seams and deterministic failure checkpoints."""

    entry: EntryEnvironment
    waivers: WaiverPort | None = None
    on_boundary: Callable[[str], None] | None = None

    @property
    def draw_seeds(self) -> SeedDraw:
        """Where the seeds of a multi-seed Goal an approved change freezes come from."""
        return self.entry.draw_seeds

    @property
    def store(self) -> GoalStore:
        """All lifecycle files stay in the resolved Main Booley Directory."""
        return GoalStore(self.entry.booley_dir)

    def checkpoint(self, name: str) -> None:
        """Expose durable boundaries for crash/recovery tests."""
        if self.on_boundary is not None:
            self.on_boundary(name)


@dataclass(frozen=True)
class ApplyTransaction:
    """Immutable effect bytes, exact payload/decision binding, and compatible log entry."""

    id: str
    proposal_id: str
    payload_digest: str
    decision: Decision
    entry: ChangeEntry | None
    effects: tuple[FileEffect, ...]

    def to_json(self) -> dict[str, Any]:
        """The complete recovery input, with no mutable source references."""
        return {
            "schema": "booley.goal-apply/v1",
            "id": self.id,
            "proposal_id": self.proposal_id,
            "payload_digest": self.payload_digest,
            "decision": self.decision.to_json(),
            "entry": None if self.entry is None else self.entry.to_json(),
            "effects": [effect.to_json() for effect in self.effects],
        }

    @classmethod
    def from_json(cls, value: object) -> ApplyTransaction:
        """Validate stored transactions before considering any destination write."""
        raw = require_dict(value, field="apply transaction")
        if (
            set(raw)
            != {"schema", "id", "proposal_id", "payload_digest", "decision", "entry", "effects"}
            or raw["schema"] != "booley.goal-apply/v1"
        ):
            raise ProposalError("invalid Goal apply transaction schema")
        return cls(
            require_uuid4(raw["id"], field="transaction.id"),
            require_uuid4(raw["proposal_id"], field="proposal_id"),
            str(raw["payload_digest"]),
            Decision.from_json(raw["decision"]),
            None if raw["entry"] is None else ChangeEntry.from_json(raw["entry"]),
            tuple(
                FileEffect.from_json(effect)
                for effect in require_list(raw["effects"], field="effects")
            ),
        )


def transaction_path(root: Path, transaction_id: str) -> Path:
    """A transaction id names an immutable recovery file."""
    return root / "applies" / f"{require_uuid4(transaction_id, field='transaction_id')}.json"


def decide(
    lock: RecordLock, view: ProposalView, decision: Decision, env: ChangeEnvironment
) -> ProposalView:
    """Prepare everything, persist exact decision, then re-drive its effects under locks."""
    lock.require_owned()
    record = env.store.load(lock.goal_id)
    _require_pending(view, record)
    require_no_apply(lock.record_dir)
    decision = replace(decision, transaction_id=str(uuid4()))
    _remove_orphan_transactions(lock.record_dir)
    with _waiver_lock(view.proposal, env):
        transaction = _prepare(lock, view.proposal, record, decision, env)
        atomic_write_once(
            transaction_path(lock.record_dir, transaction.id), encode(transaction.to_json())
        )
        env.checkpoint("prepared")
        updated = save_decision(
            lock, view, decision, transaction_digest=digest(transaction.to_json())
        )
        env.checkpoint("approved" if decision.decision == "approve" else "rejected")
        return _drive(lock, updated, transaction, env)


def _remove_orphan_transactions(root: Path) -> None:
    """Delete prepared transactions no decision names.

    A stop after a transaction was written and before its decision was saved
    leaves the file behind; that decision was never made, and the next one
    prepares its own transaction. The caller holds the record lock, so no
    other decision is between those two writes now.
    """
    named = {
        view.decision.transaction_id for view in list_proposals(root) if view.decision is not None
    }
    for path in sorted((root / "applies").glob("*.json")):
        if path.stem not in named:
            path.unlink()


def recover(store: GoalStore, record_id: str, env: ChangeEnvironment) -> GoalRecord:
    """Shared mutating-call recovery for proposal calls and Phase 5 finish."""
    with store.record_lock(record_id) as lock:
        recover_locked(lock, env)
        return store.load(record_id)


def recover_locked(lock: RecordLock, env: ChangeEnvironment, *, abandoning: bool = False) -> None:
    """Shared authority under an already-owned lifecycle lock. Abandon only committed intents."""
    lock.require_owned()
    views = list_proposals(lock.record_dir)
    log = read_change_log(lock.record_dir / "changes.jsonl")
    intents = {entry.id for entry in (*log.interrupted, *log.applied)}
    approved = [
        view
        for view in views
        if view.state == "approved"
        and not view.closed_by_abandonment
        and (not abandoning or view.proposal.id in intents)
    ]
    if len(approved) > 1:
        raise ProposalError("more than one approved Goal apply needs recovery")
    for view in views:
        if view.closed_by_abandonment:
            continue
        if abandoning and (
            view.state == "pending"
            or (view.state == "approved" and view.proposal.id not in intents)
        ):
            continue
        if (
            view.state not in {"approved", "rejected"}
            or view.decision is None
            or view.decision.transaction_id is None
            or view.resolved_at is not None
        ):
            continue
        path = transaction_path(lock.record_dir, view.decision.transaction_id)
        transaction = ApplyTransaction.from_json(json.loads(path.read_bytes()))
        with _waiver_lock(view.proposal, env):
            _drive(lock, view, transaction, env)
    unresolved = read_change_log(lock.record_dir / "changes.jsonl").interrupted
    if unresolved:
        raise ProposalError(
            "interrupted legacy Goal apply has no captured transaction; restore/reconcile it explicitly"
        )


@contextmanager
def _waiver_lock(proposal: Proposal, env: ChangeEnvironment) -> Generator[None]:
    if proposal.waiver is None:
        yield
        return
    if env.waivers is None:
        raise ProposalError("Goal waiver service is unavailable for recovery")
    with env.waivers.locking(proposal.waiver):
        yield


def _require_pending(view: ProposalView, record: GoalRecord) -> None:
    if view.closed_by_abandonment or view.state != "pending":
        raise ProposalError("proposal already decided; terminal decisions cannot be replayed")
    if record.state is not GoalState.ACTIVE:
        raise ProposalError(f"Goal Mode is {record.state.value}")
    # Rejection remains available even when its old destination is conflicting.


def _prepare(
    lock: RecordLock,
    proposal: Proposal,
    record: GoalRecord,
    decision: Decision,
    env: ChangeEnvironment,
) -> ApplyTransaction:
    assert decision.transaction_id is not None
    if decision.decision == "reject":
        effects = (
            () if proposal.waiver is None else _waiver_service(env).rejection(proposal, decision)
        )
        return ApplyTransaction(
            decision.transaction_id, proposal.id, proposal.payload_digest, decision, None, effects
        )
    reason = conflict(proposal, record)
    if reason:
        raise ProposalError(reason)
    validate_kind(proposal.kind, proposal.before, proposal.after)
    state = load_goal_state(env.store, record)
    if state.slug != record.id:
        raise ProposalError("Goal state belongs to another record")
    waiver_effects: tuple[FileEffect, ...] = ()
    coverage = None if env.waivers is None else env.waivers.evaluate
    if proposal.waiver is not None:
        waiver_effects, coverage = _waiver_service(env).prepare(proposal, decision, state)
    return _prepare_approved(
        lock, proposal, record, state, decision, waiver_effects, coverage, env
    )


def _waiver_service(env: ChangeEnvironment) -> WaiverPort:
    if env.waivers is None:
        raise ProposalError("Goal waiver service is unavailable")
    return env.waivers


def _prepare_approved(
    lock: RecordLock,
    proposal: Proposal,
    record: GoalRecord,
    state: DevelopmentState,
    decision: Decision,
    waiver_effects: tuple[FileEffect, ...],
    coverage: CoverageEvaluation | None,
    env: ChangeEnvironment,
) -> ApplyTransaction:
    assert decision.transaction_id is not None
    updated = _next_record(record, proposal)
    updated = _with_frozen_test_runs(record, updated, proposal, env)
    derivations = derive_changes(proposal, record, state, env.entry.booley_dir, coverage=coverage)
    shadow = _state_delta(state, proposal)
    _apply_derivations(shadow, derivations)
    transaction_id = digest({"proposal": proposal.id, "decision": decision.to_json()})
    observations = _derived_observations(record.id, updated, derivations)
    paths = record_paths(env.entry.booley_dir, record.id)
    ledger = _prepare_ledger(paths.logs_dir, shadow, observations, transaction_id, proposal.id)
    if ledger:
        shadow.acceptance_transactions.append(transaction_id)
    effects = (
        FileEffect(
            paths.record_file, read_bytes(paths.record_file), encode(updated.to_json()), "record"
        ),
        *waiver_effects,
        *ledger,
        FileEffect(
            paths.state_file, read_bytes(paths.state_file), encode(shadow.to_dict()), "state"
        ),
    )
    return ApplyTransaction(
        decision.transaction_id,
        proposal.id,
        proposal.payload_digest,
        decision,
        _change_entry(proposal, decision),
        effects,
    )


def _prepare_ledger(
    logs_dir: Path,
    state: DevelopmentState,
    observations: list[tuple[CriterionChange, str, Mapping[str, Any]]],
    transaction_id: str,
    proposal_id: str,
) -> tuple[FileEffect, ...]:
    prepared = prepare_observations(
        logs_dir,
        state,
        scope=GOAL_SCOPE,
        observations=observations,
        transaction_id=transaction_id,
        invocation_id=proposal_id,
        producer="approved_goal_change",
    )
    return tuple(
        FileEffect(logs_dir / row.relative_path, None, row.content, "ledger") for row in prepared
    )


def _change_entry(proposal: Proposal, decision: Decision) -> ChangeEntry:
    return ChangeEntry(
        proposal.id,
        decision.at,
        proposal.kind,
        proposal.goal_key,
        proposal.before,
        proposal.after,
        decision.reason,
        decision.source,
        decision.quote,
        decision.session_key,
        peer_process=(
            None
            if decision.peer_process is None
            else json.dumps(decision.peer_process.to_payload(), sort_keys=True)
        ),
        transaction_ref=decision.transaction_id,
    )


def _apply_derivations(state: DevelopmentState, derivations: tuple[Derivation, ...]) -> None:
    for derived in derivations:
        change = derived.change
        prior = state.criteria[change.key]
        state.criteria[change.key] = replace(
            prior,
            met=change.met,
            params=change.params,
            detail=change.detail,
            stale=False,
            ever_met=prior.ever_met or change.met,
        )


def _derived_observations(
    record_id: str, updated: GoalRecord, derivations: tuple[Derivation, ...]
) -> list[tuple[CriterionChange, str, Mapping[str, Any]]]:
    revisions = {goal.spec.key: goal.spec_revision for goal in updated.goals}
    return [
        (
            derived.change,
            derived.recorded_at,
            goal_identity(record_id, {derived.change.key: revisions[derived.change.key]}),
        )
        for derived in derivations
    ]


def _next_record(record: GoalRecord, proposal: Proposal) -> GoalRecord:
    revision = record.revision + 1
    if any(goal.spec_revision >= revision for goal in record.goals):
        raise ProposalError("Goal specification sequence is ahead of its record revision")
    replacement = RecordedGoal(proposal.after, revision)
    goals = tuple(
        replacement if goal.spec.key == proposal.goal_key else goal for goal in record.goals
    )
    if proposal.kind is ChangeKind.ADD:
        goals = (*record.goals, replacement)
    resolutions = {
        key: value
        for key, value in record.test_run_resolutions.items()
        if any(goal.spec.key == key for goal in goals)
    }
    updated = replace(record, revision=revision, goals=goals, test_run_resolutions=resolutions)
    return GoalRecord.from_json(updated.to_json())


def _with_frozen_test_runs(
    record: GoalRecord, updated: GoalRecord, proposal: Proposal, env: ChangeEnvironment
) -> GoalRecord:
    """*updated* with the changed Goal's Test Runs frozen (ADR 0072).

    The prepared record carries the exact runs and drawn seeds, so a
    recovered apply writes the same ones. What the change leaves alone stays
    frozen (:func:`refreeze_changed_goal`); a retarget that leaves the Goal
    as written freezes it again from the Target as it is now. No other Goal's
    frozen Test Runs may change: the record effect is written without
    :meth:`GoalStore.save`, so its rule is applied here.
    """
    after = next(goal for goal in updated.goals if goal.spec.key == proposal.after.key)
    changed = {proposal.goal_key, after.spec.key}
    siblings = {
        goal.spec.key: (goal, held)
        for goal in updated.goals
        if goal.spec.key not in changed
        and (held := record.test_run_resolution(goal.spec.key)) is not None
    }
    resolutions: dict[str, TestRunResolution] = {}
    if names_test_runs(after.spec):
        held = record.test_run_resolution(proposal.goal_key)
        before = None if proposal.before is None or held is None else (proposal.before, held)
        try:
            resolutions[after.spec.key] = refreeze_changed_goal(
                after,
                before,
                TargetPlans(Path(record.worktree_path)),
                ResolutionRequest("change", utc_now_rfc3339(), env.draw_seeds),
                refreeze=proposal.kind is ChangeKind.RETARGET
                and proposal.before == proposal.after,
                siblings=siblings,
            )
        except GoalResolutionError as exc:
            raise ProposalError(str(exc)) from exc
    resolutions |= {key: held for key, (_, held) in siblings.items()}
    frozen = GoalRecord.from_json(replace(updated, test_run_resolutions=resolutions).to_json())
    violation = frozen_run_violation(record, frozen)
    if violation:
        raise ProposalError(f"the approved change cannot be applied: {violation}")
    return frozen


def _state_delta(state: DevelopmentState, proposal: Proposal) -> DevelopmentState:
    shadow = copy.deepcopy(state)
    old = shadow.criteria.pop(proposal.goal_key, None)
    if proposal.kind is ChangeKind.ADD and old is not None:
        raise ProposalError("state destination is occupied")
    shadow.category_map.pop(proposal.goal_key, None)
    for alias, keys in tuple(shadow.flow_key_aliases.items()):
        remaining = [key for key in keys if key != proposal.goal_key]
        if remaining:
            shadow.flow_key_aliases[alias] = remaining
        else:
            shadow.flow_key_aliases.pop(alias)
    shadow.category_map.update(review_categories((proposal.after.key,)))
    shadow.criteria[proposal.after.key] = CriterionEntry(
        params=copy.deepcopy(proposal.runtime_params), updated_at=utc_now_rfc3339()
    )
    return shadow


def _drive(
    lock: RecordLock, view: ProposalView, transaction: ApplyTransaction, env: ChangeEnvironment
) -> ProposalView:
    _validate_transaction(view, transaction, env)
    entry = main_booley_dir_entry(Path(env.store.load(lock.goal_id).worktree_path))
    existing = _existing_entry(lock, transaction.entry)
    for effect in transaction.effects:
        effect.require_expected(booley_dir=entry)
    if transaction.entry is not None and existing == "absent":
        append_intent(lock, transaction.entry)
        env.checkpoint("intent")
    for index, effect in enumerate(transaction.effects):
        effect.apply(booley_dir=entry)
        env.checkpoint(f"effect:{effect.label}:{index}")
    if transaction.entry is None:
        return finalize(lock, view, rejected=True)
    if _existing_entry(lock, transaction.entry) != "applied":
        append_applied(lock, transaction.entry.id)
        env.checkpoint("applied")
    result = finalize(lock, view)
    env.checkpoint("finalized")
    # The human approved a Goal change: registrations nobody can read stop blocking the
    # Goals from here on (ADR 0072). A stop before this leaves them where they block.
    quarantine_unreadable(env.store, lock.goal_id)
    return result


def _validate_transaction(
    view: ProposalView, transaction: ApplyTransaction, env: ChangeEnvironment
) -> None:
    if (
        transaction.proposal_id != view.proposal.id
        or transaction.payload_digest != view.proposal.payload_digest
        or transaction.decision != view.decision
    ):
        raise ProposalError("saved transaction does not bind this exact proposal decision")
    if view.transaction_digest is None or view.transaction_digest != digest(transaction.to_json()):
        raise ProposalError(
            "recovery conflict: captured transaction differs from durable decision authority; restore the exact prepared transaction before retrying"
        )
    for effect in transaction.effects:
        if effect.label == "waiver":
            _waiver_service(env).validate_effect(effect)


def _existing_entry(
    lock: RecordLock, entry: ChangeEntry | None
) -> Literal["absent", "intent", "applied"]:
    """A persisted intent or applied entry must equal the entire captured audit."""
    if entry is None:
        return "absent"
    log = read_change_log(lock.record_dir / "changes.jsonl")
    found = next((item for item in (*log.applied, *log.interrupted) if item.id == entry.id), None)
    if found is not None and found != entry:
        raise ProposalError(
            "recovery conflict: persisted Change Log entry differs from exact approved transaction"
        )
    if found is None:
        return "absent"
    return "applied" if any(item.id == entry.id for item in log.applied) else "intent"
