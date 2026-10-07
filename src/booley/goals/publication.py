"""The publication gate every Goal evidence write passes (ADR 0067 Phase 3, B1).

A run's evidence reaches ``booley_state.json`` through the Goal state
persistence and the ledger through the Goal evidence recorder. Both publish
only inside :meth:`PublicationGate.publishing`, which holds the record lock
and checks, against the run's :class:`~booley.goals.binding.GoalRunBinding`:

1. the run was eligible when it started: its protected inputs equalled the
   record's entry snapshot (D7, start sample);
2. the record still exists and is ``active``;
3. when the record's revision moved since admission, the protected-input
   baseline it holds is still the one the run started under; any other
   change (a Goal the run does not touch was added, relaxed, or retargeted)
   leaves the run's evidence valid;
4. every Goal the write affects still exists with the ``spec_revision`` it
   had at admission (D15 fence; a V2 transaction affecting several Goals is
   rejected whole when any of them changed);
5. the bound worktree is still the one admitted (same Worktree Identity)
   with HEAD on the Goal Branch: the record's branch and worktree cannot
   change through a store save, so what this guards is a real checkout
   switch after admission;
6. the protected inputs now equal the record's entry snapshot (D7, end
   sample).

The recorder adds one check per change: the bound Target's
``target_surface`` must equal the one captured at admission, so a Target
declaration edited while the run executed is never stamped as tested.

A failed check raises :class:`EvidenceDiscarded` before anything is written.
The D7 samples are taken at admission and at publication only, so a
protected input changed and restored while the run is in flight is not
caught; closing that needs an immutable snapshot, which v1 does not take.

Lock order is the record lock, then the ledger's ``.sequence.lock`` (taken
inside the ledger while the gate is held). The gate is held only around a
publication, never across Flow execution or a hand-off to another thread,
task, or process.
"""

from __future__ import annotations

from collections.abc import Collection, Generator
from contextlib import ExitStack, contextmanager

from booley.flows.execution_persistence import AcceptanceRecordingError, EvidenceDiscarded
from booley.goals.apply_barrier import require_no_apply
from booley.goals.binding import GoalRunBinding, checkout_drift, protected_drift
from booley.goals.model import GoalRecord, GoalState
from booley.goals.paths import record_paths
from booley.goals.proposals import ProposalError
from booley.goals.protected_inputs import ProtectedInputError, snapshot_protected_inputs
from booley.goals.store import GoalRecordNotFoundError, GoalStore, GoalStoreError

__all__ = ["EvidenceDiscarded", "PublicationGate"]


class PublicationGate:
    """Validate one bound run before each of its evidence writes (module docstring).

    The gate belongs to one invocation. Once a check has failed it stays
    closed: every later write of the invocation is refused with the same
    reason, even if the condition that failed has since gone away (say a
    protected input was restored), so a discarded invocation writes nothing.
    """

    def __init__(self, binding: GoalRunBinding) -> None:
        self.binding = binding
        self._discarded: str | None = None

    @property
    def store(self) -> GoalStore:
        """The store of the bound record's Control Project directory."""
        return GoalStore(self.binding.project_dir)

    @property
    def discarded(self) -> str | None:
        """Why this invocation's evidence was discarded, or ``None`` while it was not."""
        return self._discarded

    @contextmanager
    def publishing(self, affected: Collection[str]) -> Generator[GoalRecord]:
        """Hold the record lock, check the binding for *affected* Goals, yield the record.

        *affected* names the Goal keys the write changes; an empty collection
        (a timeline-only save) still checks eligibility, state, and the
        protected inputs. Raises :class:`EvidenceDiscarded` when a check fails
        or failed before.
        """
        if self._discarded is not None:
            raise EvidenceDiscarded(self._discarded)
        try:
            with self._checked(affected) as record:
                yield record
        except EvidenceDiscarded as exc:
            self._discarded = exc.reason
            raise

    @contextmanager
    def _checked(self, affected: Collection[str]) -> Generator[GoalRecord]:
        binding = self.binding
        if not binding.eligible:
            raise EvidenceDiscarded(
                f"protected input changed before the run: {binding.ineligible_reason}"
            )
        store = self.store
        with ExitStack() as stack:
            try:
                stack.enter_context(store.record_lock(binding.record_id))
            except GoalRecordNotFoundError as exc:
                raise self._gone() from exc
            except GoalStoreError as exc:
                raise AcceptanceRecordingError(
                    f"cannot lock Goal Mode {binding.record_id}: {exc}"
                ) from exc
            record = self._current_record(store)
            self._check_record(record, affected)
            drift = checkout_drift(store, binding)
            if drift:
                raise EvidenceDiscarded(f"checkout changed during the run: {drift}")
            self._check_protected_inputs_now(record)
            yield record

    def _gone(self) -> EvidenceDiscarded:
        return EvidenceDiscarded(f"Goal Mode {self.binding.record_id} no longer exists")

    def _current_record(self, store: GoalStore) -> GoalRecord:
        try:
            return store.load(self.binding.record_id)
        except GoalRecordNotFoundError as exc:
            raise self._gone() from exc
        except GoalStoreError as exc:
            raise EvidenceDiscarded(f"Goal Mode record cannot be read: {exc}") from exc

    def _check_record(self, record: GoalRecord, affected: Collection[str]) -> None:
        binding = self.binding
        try:
            require_no_apply(record_paths(binding.project_dir, record.id).root)
        except ProposalError as exc:
            raise EvidenceDiscarded(str(exc)) from exc
        if record.state is not GoalState.ACTIVE:
            raise EvidenceDiscarded(f"Goal Mode {record.state.value}")
        if record.revision != binding.record_revision and protected_drift(
            record, binding.start_snapshot()
        ):
            raise EvidenceDiscarded(
                "the Goal Mode's protected-input baseline changed during the run"
            )
        if binding.record_revision < record.publication_floor:
            raise EvidenceDiscarded("run predates the Goal lifecycle publication fence")
        current = {goal.spec.key: goal.spec_revision for goal in record.goals}
        for key in sorted(affected):
            admitted = binding.spec_revision(key)
            if admitted is None:
                raise EvidenceDiscarded(f"{key} was not a Goal when the run started")
            if key not in current:
                raise EvidenceDiscarded(f"Goal {key} was removed during the run")
            if current[key] != admitted:
                raise EvidenceDiscarded(f"Goal {key} changed during the run")

    def _check_protected_inputs_now(self, record: GoalRecord) -> None:
        try:
            snapshot = snapshot_protected_inputs(self.binding.protected_roots())
        except ProtectedInputError as exc:
            raise EvidenceDiscarded(f"protected inputs cannot be read: {exc}") from exc
        reason = protected_drift(record, snapshot)
        if reason:
            raise EvidenceDiscarded(f"protected input changed during the run: {reason}")
