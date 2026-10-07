"""Goal evidence in the Criterion evidence ledger (ADR 0067 Phase 3, B1, B5-B7, B9).

:class:`GoalEvidenceRecorder` is the
:class:`~booley.flows.execution_persistence.AcceptanceRecorder` of a Goal run.
It writes through :mod:`booley.criteria.evidence_ledger` under the
``goal_evidence`` purpose and the :class:`GoalIdentity` codec, into the bound
record's ``logs/`` directory, and only inside the publication gate
(:mod:`booley.goals.publication`), which it takes before the ledger's own
sequence lock. Before anything is written it:

- checks the gate for the Goals the write affects (record active, specs
  unchanged, protected inputs unchanged at start and now: D7, D15);
- stamps every change's detail with the ``target_surface`` digest (D6) and,
  for a simulation Goal, the suite it is judged against (B9), turning a pass
  that does not cover the complete resolved suite into a failure.

The recorder also supplies the run's state persistence
(:class:`~booley.goals.state_store.GoalStatePersistence`), so a Flow's state
saves through the same gate.

Identity and replay (B6)
========================

A Goal evidence identity names one *identity group*: the Goals one write
affects and the specification revision each had when the run was admitted::

    {"purpose": "goal_evidence", "record_id": <Goal id>,
     "goal_keys": [<sorted Goal keys>], "spec_revisions": {<key>: <int>}}

The invariant is **one transaction = one identity group**: a V1 append or a
V2 transaction is recorded under the group of exactly the Goals it changes,
the lookup key and every stored record carry that group, and replay and
projection consider only evidence whose group equals the requested one. A
group whose member changed is rejected whole: the gate refuses to publish it,
and the projection refuses to replay or validate evidence whose revisions are
not the record's current ones. Evidence for an unchanged Goal recorded in a
group with a changed one is therefore never selected; the Goal is re-run.

Groups with unchanged revisions may still overlap ({A, B} and later {A}).
Replay and validation of one group therefore use, per Criterion, the newest
observation across every valid group (:meth:`GoalProjection.current_observations`),
so recovering an older transaction never replays an old result over a newer
one.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from booley.core.boundary import BoundaryError, require_positive_int
from booley.criteria import evidence_ledger
from booley.criteria.evidence_ledger import (
    AcceptanceLedgerError,
    AcceptanceTransaction,
    EvidenceScope,
    canonical_json,
    plain_json_value,
)
from booley.criteria.state import CriterionChange, DevelopmentState, StatePersistence
from booley.flows.execution_persistence import AcceptanceRecordingError
from booley.goals.apply_barrier import require_no_apply
from booley.goals.binding import GoalRunBinding
from booley.goals.freshness import (
    DEFAULT_RESOLVERS,
    GoalFreshnessResolvers,
    resolve_target_surface,
    stamp_goal_detail,
)
from booley.goals.model import GoalFamily, GoalRecord, GoalSpec
from booley.goals.paths import GOAL_ID_PATTERN, record_paths
from booley.goals.proposals import ProposalError
from booley.goals.publication import EvidenceDiscarded, PublicationGate
from booley.goals.simulation import GOAL_SUITE_DETAIL_KEY, simulation_contract_violation
from booley.goals.state_store import GoalStateError, GoalStatePersistence, read_goal_state_file
from booley.goals.store import GoalStore, GoalStoreError

GOAL_EVIDENCE_PURPOSE = "goal_evidence"
_IDENTITY_FIELDS = frozenset({"purpose", "record_id", "goal_keys", "spec_revisions"})


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def goal_identity(record_id: str, revisions: Mapping[str, int]) -> dict[str, Any]:
    """The identity of the group of Goals *revisions* names (module docstring)."""
    keys = sorted(revisions)
    return {
        "purpose": GOAL_EVIDENCE_PURPOSE,
        "record_id": record_id,
        "goal_keys": keys,
        "spec_revisions": {key: revisions[key] for key in keys},
    }


def validate_goal_identity(value: object) -> dict[str, Any]:
    """Return *value* as a canonical Goal identity or raise :class:`AcceptanceLedgerError`."""
    if not isinstance(value, Mapping):
        raise AcceptanceLedgerError("Goal evidence identity must be an object")
    identity = cast("dict[str, Any]", plain_json_value(value))
    if frozenset(identity) != _IDENTITY_FIELDS:
        raise AcceptanceLedgerError("Goal evidence identity has unexpected fields")
    record_id = identity["record_id"]
    if identity["purpose"] != GOAL_EVIDENCE_PURPOSE:
        raise AcceptanceLedgerError("Goal evidence identity names another purpose")
    if not isinstance(record_id, str) or GOAL_ID_PATTERN.fullmatch(record_id) is None:
        raise AcceptanceLedgerError("Goal evidence identity names no Goal Record")
    raw_keys = identity["goal_keys"]
    revisions = identity["spec_revisions"]
    keys = cast("list[object]", raw_keys) if isinstance(raw_keys, list) else []
    if (
        not keys
        or not all(isinstance(key, str) and key for key in keys)
        or keys != sorted(set(cast("list[str]", keys)))
    ):
        raise AcceptanceLedgerError("Goal evidence identity needs sorted, distinct Goal keys")
    if not isinstance(revisions, dict) or set(cast("dict[str, Any]", revisions)) != set(keys):
        raise AcceptanceLedgerError("Goal evidence identity must revision every Goal it names")
    for key, revision in cast("dict[str, Any]", revisions).items():
        try:
            require_positive_int(revision, field=f"spec_revisions.{key}")
        except BoundaryError as exc:
            raise AcceptanceLedgerError(
                f"Goal specification revisions must be positive integers: {exc}"
            ) from exc
    canonical_json(identity)
    return identity


class GoalIdentity:
    """Identity codec for ``goal_evidence`` (module docstring).

    Envelopes carry ``goal: {record_id, identity}``; records carry
    ``goal_record`` (the Goal id) and ``goal_identity``. Its validator is its
    own: the Ticket codec's 32-hex generation rule does not apply, and
    neither codec weakens the other.
    """

    subject_field = "goal"
    subject_fields = frozenset({"record_id", "identity"})
    identity_field = "goal_identity"

    def lookup_identity(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        return {"goal_identity": validate_goal_identity(identity)}

    def envelope_subject(
        self, state: DevelopmentState, lookup_identity: Mapping[str, Any]
    ) -> dict[str, Any]:
        identity = lookup_identity["goal_identity"]
        if state.slug != identity["record_id"]:
            raise AcceptanceLedgerError("Goal state does not belong to the evidence's Goal Record")
        return {"record_id": identity["record_id"], "identity": identity}

    def validate_envelope_subject(self, subject: Mapping[str, Any]) -> None:
        identity = validate_goal_identity(subject.get("identity"))
        if subject.get("record_id") != identity["record_id"]:
            raise AcceptanceLedgerError("transaction Goal subject names two Goal Records")

    def subject_lookup_identity(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        return {"goal_identity": subject["identity"]}

    def record_subject(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        return {"goal_record": subject["record_id"], "goal_identity": subject["identity"]}

    def append_subject(
        self, state: DevelopmentState, identity: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        canonical = validate_goal_identity(identity)
        if state.slug != canonical["record_id"]:
            raise AcceptanceLedgerError("Goal state does not belong to the evidence's Goal Record")
        return {"goal_record": canonical["record_id"], "goal_identity": canonical}

    def validate_observation_identity(self, value: object, current: Mapping[str, Any]) -> None:
        stored = validate_goal_identity(value)
        if current and stored["record_id"] != current.get("record_id"):
            raise AcceptanceLedgerError("Criterion evidence names another Goal Record")


GOAL_SCOPE = EvidenceScope(GOAL_EVIDENCE_PURPOSE, GoalIdentity())


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalProjection:
    """Replay and projection fencing for Goal evidence: specs must be current (B6).

    Goal evidence has no report fencing; what it fences is the specification
    revision. Replaying evidence into state, and validating state against
    evidence, both refuse an identity group whose revisions are not the
    record's current ones.
    """

    store: GoalStore
    record_id: str

    def current_observations(
        self,
        log_dir: Path,
        state: DevelopmentState,
        identity: Mapping[str, Any],
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """*records* with each one replaced by a newer valid observation of its Goal.

        Identity groups overlap: a transaction for {A, B} that is recovered
        after a later run recorded A alone must not replay its older A over
        the newer one. Valid groups are those whose every revision is
        current; among their observations the highest ledger sequence per
        Criterion wins.

        *state* may predate another run's publication (a recovering instance
        loaded before a concurrent run selected its transaction), so the
        transactions the shared state file selects now, read under the
        record lock the caller holds, count as well as *state*'s own.
        """
        if not records:
            return records
        current = self._current_revisions()
        newest: dict[str, dict[str, Any]] = {}
        for payload in evidence_ledger.validated_evidence_records(
            GOAL_SCOPE, log_dir, self._with_disk_selection(state), identity
        ):
            group = payload[GoalIdentity.identity_field]
            if group["record_id"] != self.record_id or any(
                current.get(key) != revision for key, revision in group["spec_revisions"].items()
            ):
                continue
            seen = newest.get(payload["criterion"])
            if seen is None or payload["sequence"] > seen["sequence"]:
                newest[payload["criterion"]] = payload
        return [
            newer
            if (newer := newest.get(payload["criterion"], payload))["sequence"]
            > payload["sequence"]
            else payload
            for payload in records
        ]

    def _with_disk_selection(self, state: DevelopmentState) -> DevelopmentState:
        """A shallow copy of *state* selecting its own and the state file's transactions."""
        path = state.file_path
        if path is None:
            return state
        try:
            disk = read_goal_state_file(path)
        except GoalStateError as exc:
            raise AcceptanceLedgerError(f"Goal state cannot be read for replay: {exc}") from exc
        if disk is None:
            return state
        probe = copy.copy(state)
        probe.acceptance_transactions = sorted(
            {*state.acceptance_transactions, *disk.acceptance_transactions}
        )
        return probe

    def project_state(
        self, state: DevelopmentState, log_dir: Path, identity: Mapping[str, Any]
    ) -> None:
        self.require_current(identity)

    def effective_met(
        self,
        log_dir: Path,
        criterion: str,
        met: object,
        detail: object,
        identity: Mapping[str, Any],
    ) -> object:
        self.require_current(identity)
        return met

    def require_current(self, identity: Mapping[str, Any]) -> None:
        """Raise unless *identity*'s Goals all have their current specification revision."""
        canonical = validate_goal_identity(identity)
        if canonical["record_id"] != self.record_id:
            raise AcceptanceLedgerError("Goal evidence names another Goal Record")
        current = self._current_revisions()
        for key, revision in canonical["spec_revisions"].items():
            if current.get(key) != revision:
                raise AcceptanceLedgerError(
                    f"Goal evidence for {key} was recorded under specification revision "
                    f"{revision}; the Goal is now at {current.get(key, 'removed')}"
                )

    def _current_revisions(self) -> dict[str, int]:
        try:
            with self.store.record_lock(self.record_id):
                require_no_apply(record_paths(self.store.project_dir, self.record_id).root)
                record = self.store.load(self.record_id)
        except (GoalStoreError, ProposalError) as exc:
            raise AcceptanceLedgerError(f"Goal Record cannot be read: {exc}") from exc
        return {goal.spec.key: goal.spec_revision for goal in record.goals}


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------


class GoalEvidenceRecorder:
    """Record one bound run's Goal evidence (module docstring)."""

    def __init__(
        self,
        binding: GoalRunBinding,
        *,
        resolvers: GoalFreshnessResolvers = DEFAULT_RESOLVERS,
        publication_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._binding = binding
        self._gate = PublicationGate(binding)
        self._resolvers = resolvers
        self._checkpoint = publication_checkpoint
        self._persistence = GoalStatePersistence(binding, self._gate)

    @property
    def binding(self) -> GoalRunBinding:
        """The run this recorder publishes for."""
        return self._binding

    @property
    def log_dir(self) -> Path:
        """The bound record's ``logs/`` directory, root of its ledger."""
        return record_paths(self._binding.project_dir, self._binding.record_id).logs_dir

    def projection(self) -> GoalProjection:
        """The projection fencing replay of this record's evidence."""
        return GoalProjection(self._gate.store, self._binding.record_id)

    def state_persistence(self) -> StatePersistence:
        """The merging, gated persistence every state of this run saves through."""
        return self._persistence

    def acceptance_identity(self) -> dict[str, Any]:
        """The identity of every Goal the run was admitted with."""
        if not self._binding.spec_revisions:
            raise AcceptanceRecordingError("the Goal Mode declares no Goals")
        return goal_identity(self._binding.record_id, dict(self._binding.spec_revisions))

    def identity_for(self, keys: Sequence[str]) -> dict[str, Any]:
        """The identity group of the Goals *keys*, at their admitted revisions."""
        revisions: dict[str, int] = {}
        for key in keys:
            revision = self._binding.spec_revision(key)
            if revision is None:
                raise EvidenceDiscarded(f"{key} was not a Goal when the run started")
            revisions[key] = revision
        if not revisions:
            raise AcceptanceRecordingError("Goal evidence needs at least one Criterion change")
        return goal_identity(self._binding.record_id, revisions)

    def record_changes(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        invocation_id: str,
        producer: str,
        transaction_id: str | None = None,
    ) -> None:
        """V1: append each change as Goal evidence, then mirror the stamped detail into *state*.

        Records carry the binding's invocation id (the run id), not *invocation_id*.
        """
        if not changes:
            return
        with self._publication(state, changes) as (identity, stamped):
            try:
                evidence_ledger.record_changes(
                    self.log_dir,
                    state,
                    stamped,
                    scope=GOAL_SCOPE,
                    invocation_id=self._binding.invocation_id,
                    producer=producer,
                    execution_id="",
                    identity=identity,
                    transaction_id=transaction_id or "",
                )
            except AcceptanceLedgerError as exc:
                raise AcceptanceRecordingError(str(exc)) from exc
        _mirror_into_state(state, stamped)

    def record_or_verify_transaction(
        self,
        state: DevelopmentState,
        changes: list[CriterionChange],
        *,
        acceptance_facts: Mapping[str, Any],
        ticket_identity: Mapping[str, Any],
    ) -> AcceptanceTransaction:
        """V2: record or recover one Simulation Campaign transaction as Goal evidence.

        *ticket_identity* is ignored: a Goal transaction's identity is the
        group of the Goals its changes affect (module docstring).
        """
        with self._publication(state, changes) as (identity, stamped):
            try:
                return evidence_ledger.record_or_verify_transaction(
                    self.log_dir,
                    state,
                    stamped,
                    scope=GOAL_SCOPE,
                    projection=self.projection(),
                    acceptance_facts=acceptance_facts,
                    identity=identity,
                    publication_checkpoint=self._checkpoint,
                )
            except AcceptanceLedgerError as exc:
                raise AcceptanceRecordingError(str(exc)) from exc

    @contextmanager
    def _publication(
        self, state: DevelopmentState, changes: list[CriterionChange]
    ) -> Generator[tuple[dict[str, Any], list[CriterionChange]]]:
        """Hold the gate for *changes*; yield their identity group and stamped changes."""
        _require_goal_persistence(state)
        keys = sorted({change.key for change in changes})
        identity = self.identity_for(keys)
        with self._gate.publishing(keys) as record:
            yield identity, self._prepare(record, changes)

    def _require_unchanged_target(self, goal: GoalSpec) -> dict[str, Any]:
        """Refuse a result whose Target declaration changed after the run was admitted (D6).

        Returns the surface fingerprint that passed the comparison; the stamp
        must carry exactly this one, not a fresh read (an edit between two
        reads would stamp a declaration the run never tested).
        """
        captured = dict(self._binding.start_surfaces)
        if goal.target not in captured:
            raise EvidenceDiscarded(f"{_target_label(goal.target)} was not captured at admission")
        surface = resolve_target_surface(self._resolvers, self._binding.worktree_root, goal.target)
        digest = surface.get("digest")
        now = digest if isinstance(digest, str) else None
        if now != captured[goal.target]:
            raise EvidenceDiscarded(f"{_target_label(goal.target)} changed during the run")
        return surface

    def _prepare(
        self, record: GoalRecord, changes: list[CriterionChange]
    ) -> list[CriterionChange]:
        goals = {goal.spec.key: goal.spec for goal in record.goals}
        return [self._prepare_change(change, goals.get(change.key)) for change in changes]

    def _prepare_change(self, change: CriterionChange, goal: GoalSpec | None) -> CriterionChange:
        work_dir = self._binding.worktree_root
        surface = None if goal is None else self._require_unchanged_target(goal)
        detail = stamp_goal_detail(
            change.key,
            change.detail,
            work_dir=work_dir,
            goal=goal,
            resolvers=self._resolvers,
            target_surface=surface,
        )
        if goal is None or goal.family is not GoalFamily.SIM or goal.target is None:
            return replace(change, detail=detail)
        try:
            suite = self._resolvers.simulation_suite(work_dir, goal.target)
        except (OSError, ValueError) as exc:
            detail[GOAL_SUITE_DETAIL_KEY] = None
            return _unmet(change, detail, f"the simulation suite cannot be resolved: {exc}")
        detail[GOAL_SUITE_DETAIL_KEY] = list(suite)
        violation = simulation_contract_violation(detail, suite) if change.met else ""
        if violation:
            return _unmet(change, detail, violation)
        return replace(change, detail=detail)


def _target_label(target: str | None) -> str:
    return f"Target {target}" if target is not None else "the Project's Target declarations"


def _require_goal_persistence(state: DevelopmentState) -> None:
    """Refuse a file-backed state that would save around the publication gate."""
    if state.file_path is not None and not isinstance(state.persistence, GoalStatePersistence):
        raise AcceptanceRecordingError(
            "Goal evidence needs a state loaded through the Goal state persistence"
        )


def _unmet(change: CriterionChange, detail: dict[str, Any], reason: str) -> CriterionChange:
    """*change* as a failure: a pass that does not satisfy the Goal contract never marks met."""
    detail["goal_contract_violation"] = reason
    return replace(change, met=False, reason="goal-simulation-contract", detail=detail)


def _mirror_into_state(state: DevelopmentState, changes: Sequence[CriterionChange]) -> None:
    """Make *state*'s entries carry exactly what the ledger recorded for them."""
    for change in changes:
        entry = state.criteria.get(change.key)
        if entry is None:
            continue
        entry.detail = dict(change.detail)
        if entry.met and not change.met:
            entry.met = False
