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
from booley.goals.apply_effects import FileEffect, read_bytes, require_destination
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
    finalize_rejection,
    list_proposals,
    save_decision,
)
from booley.goals.recorder import GOAL_SCOPE, goal_identity
from booley.goals.state_store import load_goal_state, review_categories
from booley.goals.store import GoalStore, RecordLock
from booley.runtime.atomic_files import atomic_write_once
from booley.runtime.timefmt import utc_now_rfc3339


class WaiverPort(Protocol):
    """Composition supplies concrete coverage/candidate policy, never Goal imports."""

    def bind(self, record: GoalRecord, goal_key: str, candidate_id: str) -> dict[str, Any]: ...
    def locking(self, binding: Mapping[str, Any]) -> AbstractContextManager[None]: ...
    def prepare(
        self, proposal: Proposal, decision: Decision, state: DevelopmentState
    ) -> tuple[tuple[FileEffect, ...], CoverageEvaluation]: ...
    def rejection(self, proposal: Proposal, decision: Decision) -> tuple[FileEffect, ...]: ...
    def evaluate(
        self, goal: GoalSpec, detail: Mapping[str, Any]
    ) -> tuple[bool, dict[str, Any]]: ...
    def validate_effect(self, binding: Mapping[str, Any], effect: FileEffect) -> None: ...


@dataclass(frozen=True)
class ChangeEnvironment:
    """The real preparation/coverage seams and deterministic failure checkpoints."""

    entry: EntryEnvironment
    waivers: WaiverPort | None = None
    on_boundary: Callable[[str], None] | None = None

    @property
    def store(self) -> GoalStore:
        """All lifecycle files stay in the resolved control Project directory."""
        return GoalStore(self.entry.project_dir)

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
        transaction = cls(
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
        if (
            transaction.decision.transaction_id != transaction.id
            or transaction.decision.payload_digest != transaction.payload_digest
        ):
            raise ProposalError("apply transaction decision binding is invalid")
        if len({effect.path for effect in transaction.effects}) != len(transaction.effects):
            raise ProposalError("apply transaction repeats a destination")
        if (transaction.entry is None) != (transaction.decision.decision == "reject"):
            raise ProposalError("a rejected decision cannot authorize Goal changes")
        return transaction


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
    with _waiver_lock(view.proposal, env):
        transaction = _prepare(lock, view.proposal, record, decision, env)
        _validate_contents(view.proposal, transaction)
        atomic_write_once(
            transaction_path(lock.record_dir, transaction.id), encode(transaction.to_json())
        )
        env.checkpoint("prepared")
        updated = save_decision(
            lock, view, decision, transaction_digest=digest(transaction.to_json())
        )
        env.checkpoint("approved" if decision.decision == "approve" else "rejected")
        return _drive(lock, updated, transaction, env)


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
    derivations = derive_changes(proposal, record, state, env.entry.project_dir, coverage=coverage)
    shadow = _state_delta(state, proposal)
    _apply_derivations(shadow, derivations)
    transaction_id = digest({"proposal": proposal.id, "decision": decision.to_json()})
    observations = _derived_observations(record.id, updated, derivations)
    paths = record_paths(env.entry.project_dir, record.id)
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
    updated = replace(record, revision=revision, goals=goals)
    return GoalRecord.from_json(updated.to_json())


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
    _validate_transaction(lock, view, transaction, env)
    existing = _existing_entry(lock, transaction.entry)
    for effect in transaction.effects:
        effect.require_expected()
    if transaction.entry is not None and existing == "absent":
        append_intent(lock, transaction.entry)
        env.checkpoint("intent")
    for index, effect in enumerate(transaction.effects):
        effect.apply()
        env.checkpoint(f"effect:{effect.label}:{index}")
    if transaction.entry is None:
        return finalize_rejection(lock, view)
    if _existing_entry(lock, transaction.entry) != "applied":
        append_applied(lock, transaction.entry.id)
        env.checkpoint("applied")
    result = finalize(lock, view)
    env.checkpoint("finalized")
    return result


def _validate_transaction(
    lock: RecordLock, view: ProposalView, transaction: ApplyTransaction, env: ChangeEnvironment
) -> None:
    if (
        transaction.proposal_id != view.proposal.id
        or transaction.payload_digest != view.proposal.payload_digest
        or transaction.decision != view.decision
    ):
        raise ProposalError("saved transaction does not bind this exact proposal decision")
    _validate_contents(view.proposal, transaction)
    if view.transaction_digest is None or view.transaction_digest != digest(transaction.to_json()):
        raise ProposalError(
            "recovery conflict: captured transaction differs from durable decision authority; restore the exact prepared transaction before retrying"
        )
    allowed = (
        {"candidate"}
        if transaction.decision.decision == "reject"
        else {"record", "state", "ledger", "waiver"}
    )
    for effect in transaction.effects:
        require_destination(effect.path, effect.label)
        if effect.label not in allowed:
            raise ProposalError("effect exceeds this decision's authority")
        if effect.label in {"waiver", "candidate"}:
            if view.proposal.waiver is None:
                raise ProposalError("external effect without waiver binding")
            _waiver_service(env).validate_effect(view.proposal.waiver, effect)
        else:
            _validate_record_effect(lock, view, effect)


def _validate_contents(proposal: Proposal, transaction: ApplyTransaction) -> None:
    """Require the complete decision-specific structure and exact public audit."""
    labels = [effect.label for effect in transaction.effects]
    if transaction.decision.decision == "reject":
        if transaction.entry is not None or labels != (["candidate"] if proposal.waiver else []):
            raise ProposalError("recovery conflict: effects exceed rejection decision's authority")
        return
    if transaction.entry != _change_entry(proposal, transaction.decision):
        raise ProposalError(
            "recovery conflict: ChangeEntry differs from exact approved proposal/decision"
        )
    if labels.count("record") != 1 or labels.count("state") != 1:
        raise ProposalError(
            "recovery conflict: prepared transaction lacks its complete record/state effects"
        )
    if (labels.count("waiver") > 0) != (proposal.waiver is not None) or "candidate" in labels:
        raise ProposalError("recovery conflict: effects exceed approval decision's authority")


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


def _validate_record_effect(lock: RecordLock, view: ProposalView, effect: FileEffect) -> None:
    require_destination(effect.path, effect.label)
    root = lock.record_dir
    if effect.label in {"record", "state"}:
        expected = root / ("record.json" if effect.label == "record" else "booley_state.json")
        if effect.path != expected or effect.after is None:
            raise ProposalError("invalid record/state effect destination")
        if effect.label == "record":
            record = GoalRecord.from_json(json.loads(effect.after))
            if record.id != lock.goal_id:
                raise ProposalError("record effect names another Goal Record")
        return
    if effect.label != "ledger" or effect.before is not None or effect.after is None:
        raise ProposalError("invalid ledger effect")
    payload = json.loads(effect.after)
    sequence = payload.get("sequence")
    if view.decision is None:
        raise ProposalError("ledger effect has no decision")
    transaction_id = digest({"proposal": view.proposal.id, "decision": view.decision.to_json()})
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ProposalError("invalid ledger sequence")
    expected = (
        root
        / "logs"
        / "acceptance"
        / "evidence"
        / f"{sequence:09d}.tx.{transaction_id}"
        / "record.json"
    )
    if effect.path != expected:
        raise ProposalError("ledger effect does not name its exact observation destination")
