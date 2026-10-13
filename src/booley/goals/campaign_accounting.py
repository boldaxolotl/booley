"""Account for Goal evidence that was durably observed but never published (ADR 0072).

A Goal that is judged by Test Runs must never read met while a matching run
failed at the current file contents. A failure is only sticky once it is in
the Goal's ledger, and two things can keep a durable observation out of it:

- a Simulation Campaign that was cancelled or killed after some of its work
  items finished: their authenticated results are on disk, but the campaign
  never returned an outcome to publish;
- an acceptance transaction whose records were published but that was never
  selected into the Goal state (its writer stopped in between): the ledger
  shows only selected transactions.

Every Simulation Campaign a bound run starts is therefore **registered**
before any of its work runs (:func:`register_campaign`): one document per
campaign manifest under the record's ``logs/acceptance/campaigns/``, holding
the run it started under and, per Goal of its Target, the freshness stamp of
the file contents it started at. :func:`account_for_campaigns` then, under
the record lock,

1. completes every unselected acceptance transaction with the ledger's own
   recovery (:func:`booley.criteria.evidence_ledger.complete_transaction`);
2. **adopts** the finished results of every registered campaign that is not
   settled: per Goal one observation with the campaign's matching Test Run
   rows, stamped with the contents the campaign started at, folded into the
   Goal's Test Run ledger like any other observation. Failures are always
   adopted. Passes are adopted only while the publication gate of the run
   that started the campaign still holds, as a normal publication of that
   run would require. Work items without a result stay missing.

It runs before a run registers a campaign, before a recorder publishes, when
status is read and before Finish. Reading a campaign's results is the
Simulation Flow's business: the composing entry point supplies the reader
(:data:`~booley.flows.campaign_results.CampaignResultsReader`), and without
one nothing is adopted, so the campaign stays unaccounted. Whatever it cannot settle stays **unaccounted**:
:func:`unaccounted_goals` names those Goals, and status reads them unmet
until the campaign is resumed (its own publication settles it) or a human
approves a retarget of the Goal, which starts its evidence again. There is
no other way to drop an unaccounted campaign: none an agent can take alone.

Crash safety. Adoption makes three durable writes, in this order:

1. the observation is appended to the ledger (one atomic rename). From here
   the failure is sticky: every later fold reads it;
2. the Goal state file is replaced with the observation mirrored into it. A
   stop before this leaves the state differing from its newest observation,
   which status reads as a conflict (never met);
3. the registration is replaced with the campaign settled. A stop before
   this makes the next call adopt again: it finds the observation by the
   campaign and result digests it carries, appends nothing, and repeats
   steps 2 and 3.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from booley.core.boundary import (
    BoundaryError,
    require_bool_value,
    require_dict,
    require_list,
    require_str_value,
)
from booley.criteria import evidence_ledger
from booley.criteria.evidence_ledger import AcceptanceLedgerError, canonical_json
from booley.criteria.state import DevelopmentState
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.campaign_results import (
    CampaignBusyError,
    CampaignResultsReader,
    DurableCampaignResults,
    StartedCampaign,
)
from booley.flows.execution_persistence import EvidenceDiscarded
from booley.goals.apply_barrier import pending_applies
from booley.goals.binding import GoalBindingError, GoalRunBinding
from booley.goals.model import GoalRecord, GoalState, RecordedGoal
from booley.goals.paths import GoalRecordPaths, record_paths
from booley.goals.publication import PublicationGate
from booley.goals.state_store import (
    declared_goal_state,
    read_goal_state_file,
    write_goal_state_file,
)
from booley.goals.store import GoalStore
from booley.goals.test_run_evidence import goal_test_runs, judged_change
from booley.goals.test_run_goals import names_test_runs
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.timefmt import parse_timestamp, utc_now_rfc3339

logger = logging.getLogger(__name__)

REGISTRATION_SCHEMA = "booley.goal-campaign/v1"
#: Detail key of an adopted observation: the campaign and results it was adopted from.
ADOPTED_DETAIL_KEY = "adopted_campaign"
REASON_UNACCOUNTED = "goal-campaign-unaccounted"

_FIELDS = frozenset(
    {
        "$schema",
        "campaign_id",
        "manifest_sha256",
        "manifest_path",
        "binding",
        "stamps",
        "registered_at",
        "accounted",
        "settled",
    }
)


class RegistrationFormatError(ValueError):
    """A stored campaign registration does not have the registration format."""


@dataclass(frozen=True)
class CampaignRegistration:
    """One Simulation Campaign a bound run started, and what of it is in the ledger.

    ``stamps`` holds, per Goal of the campaign's Target, the source
    fingerprint detail of the file contents the campaign started at.
    ``accounted`` names the work-item results (id, digest) whose outcomes the
    ledger holds; ``settled`` says nothing of the campaign is left to adopt
    until it runs again.
    """

    campaign_id: str
    manifest_sha256: str
    manifest_path: str
    binding: Mapping[str, Any]
    stamps: Mapping[str, Mapping[str, Any]]
    registered_at: str
    accounted: tuple[tuple[str, str], ...] = ()
    settled: bool = False

    def run_binding(self) -> GoalRunBinding:
        """The run the campaign started under. Raises :class:`GoalBindingError`."""
        return GoalRunBinding.from_json(self.binding)

    def to_json(self) -> dict[str, Any]:
        """The stored document."""
        return {
            "$schema": REGISTRATION_SCHEMA,
            "campaign_id": self.campaign_id,
            "manifest_sha256": self.manifest_sha256,
            "manifest_path": self.manifest_path,
            "binding": dict(self.binding),
            "stamps": {key: dict(stamp) for key, stamp in sorted(self.stamps.items())},
            "registered_at": self.registered_at,
            "accounted": [list(pair) for pair in self.accounted],
            "settled": self.settled,
        }

    @classmethod
    def from_json(cls, raw: object, *, where: str) -> CampaignRegistration:
        """Parse a stored registration, raising :class:`RegistrationFormatError`."""
        try:
            document = require_dict(raw, field=where)
            if frozenset(document) != _FIELDS or document["$schema"] != REGISTRATION_SCHEMA:
                raise RegistrationFormatError(f"{where} is not a campaign registration")
            stamps = require_dict(document["stamps"], field=f"{where}.stamps")
            accounted = require_list(document["accounted"], field=f"{where}.accounted")
            registration = cls(
                require_str_value(document["campaign_id"], field=f"{where}.campaign_id"),
                require_str_value(document["manifest_sha256"], field=f"{where}.manifest_sha256"),
                require_str_value(document["manifest_path"], field=f"{where}.manifest_path"),
                require_dict(document["binding"], field=f"{where}.binding"),
                {
                    str(key): require_dict(stamp, field=f"{where}.stamps[{key}]")
                    for key, stamp in stamps.items()
                },
                require_str_value(document["registered_at"], field=f"{where}.registered_at"),
                tuple(_result_pair(pair, f"{where}.accounted") for pair in accounted),
                require_bool_value(document["settled"], field=f"{where}.settled"),
            )
            parse_timestamp(registration.registered_at)
        except (BoundaryError, ValueError) as exc:
            if isinstance(exc, RegistrationFormatError):
                raise
            raise RegistrationFormatError(f"{where}: {exc}") from None
        return registration


def _result_pair(raw: object, where: str) -> tuple[str, str]:
    pair = require_list(raw, field=where)
    if len(pair) != 2 or not all(isinstance(item, str) and item for item in pair):
        raise RegistrationFormatError(f"{where} must hold [work item, result digest] pairs")
    return str(pair[0]), str(pair[1])


@dataclass(frozen=True)
class Unaccounted:
    """Durable evidence of Goals *goal_keys* that is not in their ledger, and why."""

    goal_keys: tuple[str, ...]
    what: str
    reason: str

    def text(self) -> str:
        """The reason a Goal it names reads unmet."""
        return (
            f"{REASON_UNACCOUNTED}: {self.what} {self.reason}; its results are not in the "
            "Goal's evidence. Resume it, or have a human approve a retarget of the Goal"
        )


# ---------------------------------------------------------------------------
# Stored registrations
# ---------------------------------------------------------------------------


def _directory(paths: GoalRecordPaths) -> Path:
    return paths.logs_dir / "acceptance" / "campaigns"


def _path(paths: GoalRecordPaths, manifest_sha256: str) -> Path:
    name = hashlib.sha256(manifest_sha256.encode("utf-8")).hexdigest()
    return _directory(paths) / f"{name}.json"


def _read(path: Path) -> CampaignRegistration:
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistrationFormatError(f"campaign registration {path.name}: {exc}") from None
    return CampaignRegistration.from_json(raw, where=f"campaign registration {path.name}")


def _write(paths: GoalRecordPaths, registration: CampaignRegistration) -> None:
    path = _path(paths, registration.manifest_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(path, canonical_json(registration.to_json()) + b"\n")


def _registrations(paths: GoalRecordPaths) -> list[CampaignRegistration]:
    """Every stored registration, oldest first. Raises :class:`RegistrationFormatError`."""
    directory = _directory(paths)
    if not directory.is_dir():
        return []
    found = [_read(path) for path in sorted(directory.glob("*.json"))]
    return sorted(found, key=lambda item: (item.registered_at, item.manifest_sha256))


def _relevant_keys(registration: CampaignRegistration, record: GoalRecord) -> tuple[str, ...]:
    """The Goals *registration* stamped that still have the revision its run was bound to.

    A Goal an approved change gave another revision started its evidence
    again: nothing a campaign observed before counts for it either way.
    """
    try:
        revisions = dict(registration.run_binding().spec_revisions)
    except GoalBindingError:
        revisions = {}
    current = {goal.spec.key: goal.spec_revision for goal in record.goals}
    return tuple(
        key
        for key in sorted(registration.stamps)
        if key in current and revisions.get(key, current[key]) == current[key]
    )


# ---------------------------------------------------------------------------
# Registering
# ---------------------------------------------------------------------------


def register_campaign(
    store: GoalStore,
    binding: GoalRunBinding,
    campaign: StartedCampaign,
    stamp: Callable[[RecordedGoal], Mapping[str, Any] | None],
) -> None:
    """Register *campaign* as started by *binding*'s run.

    The caller holds the record lock through the run's publication gate.
    *stamp* gives the source fingerprint detail of a Goal's dependent file
    contents now, or ``None`` when the run's evidence for that Goal would be
    discarded anyway. A baseline campaign, and one whose Target no Goal is
    judged by, is not registered. A campaign that is registered already (it
    is being resumed) keeps what was accounted and is open again.
    """
    if not campaign.candidate:
        return
    record = store.load(binding.record_id)
    stamps: dict[str, Mapping[str, Any]] = {}
    for goal in record.goals:
        admitted = binding.spec_revision(goal.spec.key) == goal.spec_revision
        named = goal.spec.target in campaign.target_names
        if not names_test_runs(goal.spec) or not named or not admitted:
            continue
        stamped = stamp(goal)
        if stamped is not None:
            stamps[goal.spec.key] = stamped
    if not stamps:
        return
    paths = record_paths(store.project_dir, binding.record_id)
    path = _path(paths, campaign.manifest_sha256)
    accounted = _read(path).accounted if path.exists() else ()
    registration = CampaignRegistration(
        campaign.campaign_id,
        campaign.manifest_sha256,
        campaign.manifest_path.resolve().as_posix(),
        binding.to_json(),
        stamps,
        utc_now_rfc3339(),
        accounted,
    )
    _write(paths, registration)


def mark_published(store: GoalStore, record_id: str, acceptance_facts: Mapping[str, Any]) -> None:
    """Settle the campaign whose acceptance transaction was just selected.

    The transaction consumed every result the campaign held when it returned
    its outcome. The caller holds the record lock.
    """
    paths = record_paths(store.project_dir, record_id)
    path = _path(paths, str(acceptance_facts.get("manifest_sha256")))
    if not path.exists():
        return
    _write(paths, replace(_read(path), accounted=_consumed(acceptance_facts), settled=True))


def _consumed(acceptance_facts: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """The (work item, result digest) pairs *acceptance_facts* consumed."""
    raw = acceptance_facts.get("consumed_results")
    facts = [_mapping(item) for item in cast("Sequence[object]", raw or ())]
    return tuple(
        (str(fact.get("work_item_id")), str(_mapping(fact.get("result")).get("sha256")))
        for fact in facts
    )


def _mapping(value: object) -> Mapping[str, object]:
    """*value* when it is a mapping, else an empty one."""
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------------------
# Reading what is unaccounted
# ---------------------------------------------------------------------------


def unaccounted_goals(
    store: GoalStore, record: GoalRecord, state: DevelopmentState
) -> dict[str, str]:
    """Why each Goal with durable evidence outside its ledger reads unmet, by Goal key.

    Read-only. Names the Goals of every registered campaign that is not
    settled and of every acceptance transaction the state does not select.
    A registration or transaction that cannot be read names every Goal that
    is judged by Test Runs: what it holds cannot be told.
    """
    paths = record_paths(store.project_dir, record.id)
    found: list[Unaccounted] = []
    everything = tuple(goal.spec.key for goal in record.goals if names_test_runs(goal.spec))
    try:
        for registration in _registrations(paths):
            if not registration.settled:
                keys = _relevant_keys(registration, record)
                found.append(_open_campaign(registration, keys, "ended without publishing"))
        found.extend(_unselected(record, paths, state))
    except (RegistrationFormatError, AcceptanceLedgerError) as exc:
        found.append(Unaccounted(everything, "recorded evidence", f"cannot be read ({exc})"))
    reasons: dict[str, str] = {}
    for item in found:
        for key in item.goal_keys:
            reasons.setdefault(key, item.text())
    return reasons


def _open_campaign(
    registration: CampaignRegistration, keys: tuple[str, ...], reason: str
) -> Unaccounted:
    what = f"Simulation Campaign {registration.campaign_id} ({registration.manifest_path})"
    return Unaccounted(keys, what, reason)


def _unselected(
    record: GoalRecord, paths: GoalRecordPaths, state: DevelopmentState
) -> list[Unaccounted]:
    """The live acceptance transactions *state* does not select."""
    from booley.goals.recorder import GOAL_SCOPE

    return [
        Unaccounted(
            tuple(pending.identity["goal_keys"]),
            f"acceptance transaction {pending.transaction_id[:12]}",
            "was recorded but never selected",
        )
        for pending in evidence_ledger.unselected_transactions(GOAL_SCOPE, paths.logs_dir, state)
        if _current_group(pending.identity, record)
    ]


def _current_group(identity: Mapping[str, Any], record: GoalRecord) -> bool:
    """Whether every Goal of an identity group still has the revision it was recorded under."""
    current = {goal.spec.key: goal.spec_revision for goal in record.goals}
    revisions = cast("Mapping[str, int]", identity.get("spec_revisions") or {})
    return identity.get("record_id") == record.id and all(
        current.get(key) == revision for key, revision in revisions.items()
    )


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


class _LockedStateFile:
    """State persistence of a writer that holds the record lock from read to write.

    Every writer of a Goal state file holds that lock, so the whole state as
    read and changed under it can be written back.
    """

    def __init__(self, record: GoalRecord) -> None:
        self._record = record

    def read(self, path: Path) -> DevelopmentState:
        state = read_goal_state_file(path)
        if state is None:
            return declared_goal_state(self._record, self._record.worktree_path)
        return state

    def loaded(self, state: DevelopmentState) -> None:
        """Nothing to capture: every save writes the whole state."""

    def save(self, state: DevelopmentState) -> None:
        if state.file_path is not None:
            write_goal_state_file(state.file_path, state)


@dataclass(frozen=True)
class _Accounting:
    """One accounting pass over a record, under its lock."""

    store: GoalStore
    record: GoalRecord
    paths: GoalRecordPaths
    reader: CampaignResultsReader | None

    def state(self) -> DevelopmentState:
        return DevelopmentState.load(self.paths.state_file, _LockedStateFile(self.record))


def account_for_campaigns(
    store: GoalStore,
    record_id: str,
    *,
    reader: CampaignResultsReader | None,
    own_run: str = "",
) -> None:
    """Bring every durable observation of Goal Record *record_id* into its ledger.

    Completes unselected acceptance transactions, then adopts the finished
    results of registered campaigns that are not settled (module docstring).
    *reader* reads a campaign's finished results; without one no campaign is
    adopted. Campaigns the run *own_run* registered are left to that run's
    own publication. Nothing happens for a record that is not active or whose
    apply is interrupted. What cannot be settled is left as it is, for
    :func:`unaccounted_goals` to name; nothing is raised for it.

    Raises :class:`~booley.goals.store.GoalStoreError` when the record cannot
    be locked or read, and :class:`AcceptanceLedgerError` when its ledger
    cannot be read.
    """
    from booley.goals.recorder import GOAL_SCOPE, GoalProjection

    with store.record_lock(record_id):
        record = store.load(record_id)
        paths = record_paths(store.project_dir, record_id)
        if record.state is not GoalState.ACTIVE or pending_applies(paths.root):
            return
        run = _Accounting(store, record, paths, reader)
        state = run.state()
        projection = GoalProjection(store, record_id)
        for pending in evidence_ledger.unselected_transactions(GOAL_SCOPE, paths.logs_dir, state):
            if not _current_group(pending.identity, record):
                continue
            try:
                evidence_ledger.complete_transaction(
                    GOAL_SCOPE, projection, paths.logs_dir, state, pending
                )
            except AcceptanceLedgerError as exc:
                logger.warning(
                    "acceptance transaction %s of Goal Mode %s cannot be completed: %s",
                    pending.transaction_id,
                    record_id,
                    exc,
                )
                continue
            if pending.acceptance_facts is not None:
                mark_published(store, record_id, pending.acceptance_facts)
        try:
            registrations = _registrations(paths)
        except RegistrationFormatError as exc:
            logger.warning("campaign registrations of Goal Mode %s: %s", record_id, exc)
            return
        for registration in registrations:
            if registration.settled or _run_id(registration) == own_run:
                continue
            problem = _settle(run, registration)
            if problem:
                logger.warning(
                    "Simulation Campaign %s of Goal Mode %s is unaccounted: %s",
                    registration.campaign_id,
                    record_id,
                    problem,
                )


def _run_id(registration: CampaignRegistration) -> str:
    return str(registration.binding.get("invocation_id", ""))


def _settle(run: _Accounting, registration: CampaignRegistration) -> str:
    """Adopt *registration*'s finished results and settle it; why not, when it cannot be."""
    if run.reader is None:
        return "this caller cannot read Simulation Campaigns"
    try:
        durable = run.reader(Path(registration.manifest_path), registration.manifest_sha256)
    except CampaignBusyError:
        return "it is still running"
    except ValueError as exc:  # the campaign's integrity error
        return f"its results cannot be authenticated ({exc})"
    if set(durable.results) - set(registration.accounted):
        keys = _relevant_keys(registration, run.record)
        trusted = bool(keys) and _publishable(registration, keys)
        state = run.state()
        goals = {goal.spec.key: goal for goal in run.record.goals}
        changed = [
            _adopt(run, state, registration, durable, goals[key], passes=trusted) for key in keys
        ]
        if any(changed):
            write_goal_state_file(run.paths.state_file, state)
    _write(run.paths, replace(registration, accounted=durable.results, settled=True))
    return ""


def _publishable(registration: CampaignRegistration, keys: Sequence[str]) -> bool:
    """Whether the run that started the campaign could still publish for *keys* now."""
    try:
        with PublicationGate(registration.run_binding()).publishing(keys):
            return True
    except (EvidenceDiscarded, GoalBindingError):
        return False


def _adopt(
    run: _Accounting,
    state: DevelopmentState,
    registration: CampaignRegistration,
    durable: DurableCampaignResults,
    goal: RecordedGoal,
    *,
    passes: bool,
) -> bool:
    """Record *goal*'s matching rows of *durable* once and mirror them into *state*.

    Returns whether *state* changed. Without *passes* only failures are
    adopted: the run that started the campaign could not publish them now.
    """
    from booley.goals.recorder import GOAL_SCOPE, goal_identity

    key = goal.spec.key
    frozen = goal_test_runs(goal, run.record.test_run_resolution(key))
    wanted = {"fail", "pass"} if passes else {"fail"}
    rows = [row for row in frozen.matching(durable.rows) if row["grade"] in wanted]
    if not rows or key not in state.criteria:
        return False
    marker = {
        "campaign_id": registration.campaign_id,
        "manifest_sha256": registration.manifest_sha256,
        "results_sha256": hashlib.sha256(
            canonical_json({"results": [list(pair) for pair in durable.results]})
        ).hexdigest(),
        "passes": passes,
    }
    history = evidence_ledger.validated_evidence_records(GOAL_SCOPE, run.paths.logs_dir, state, {})
    mine = [row for row in history if row["criterion"] == key and row["role"] == "candidate"]
    adopted = [row for row in mine if row["detail"].get(ADOPTED_DETAIL_KEY) == marker]
    if adopted:
        newest = adopted[-1] if adopted[-1]["sequence"] == mine[-1]["sequence"] else None
        if newest is None or _mirrors(state, key, newest["met"], newest["detail"]):
            return False
        state.set_criterion(key, bool(newest["met"]), detail=dict(newest["detail"]))
        return True
    detail = {
        ADOPTED_DETAIL_KEY: marker,
        **frozen.outcomes_detail(rows),
        SOURCE_FINGERPRINT_DETAIL_KEY: dict(registration.stamps[key]),
    }
    (change,) = deepcopy(state).set_criterion(key, False, detail=detail)
    change = judged_change(change, goal, run.record, history)
    evidence_ledger.record_changes(
        run.paths.logs_dir,
        state,
        [change],
        scope=GOAL_SCOPE,
        invocation_id=_run_id(registration),
        producer="sim",
        execution_id="",
        identity=goal_identity(run.record.id, {key: goal.spec_revision}),
    )
    state.set_criterion(key, change.met, detail=dict(change.detail))
    return True


def _mirrors(state: DevelopmentState, key: str, met: object, detail: object) -> bool:
    entry = state.criteria[key]
    return entry.met == met and entry.detail == detail
