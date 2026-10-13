"""Account for Goal evidence that was durably observed but never published (ADR 0072).

A Goal that is judged by the outcomes of its Test Runs must never read met
while a matching run failed at the current file contents. A failure is only
sticky once it is in the Goal's ledger, and two things can keep a durable
observation out of it:

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
(:data:`~booley.flows.campaign_results.CampaignResultsReader`).

A registration is **open** until nothing more can come out of its campaign:
its acceptance was published, or every one of its work items has a durable
result that was adopted. Only then it is **settled** and never read again.
An open registration is read again on every pass, so results that appear
later (the campaign had not started, was still running, or was resumed) are
adopted then. While it is open it makes its Goals unmet
(:func:`unaccounted_goals`, ``goal-campaign-unaccounted``) only as long as
its results may still change or cannot be told: it has not been read since
it was registered, a scheduler holds its lock, or the read fails. An idle,
readable campaign that ran only in part has its results adopted; its other
runs simply stay missing, and it blocks nothing.

A coverage Goal is judged by its coverage floors alone
(:func:`~booley.goals.test_run_evidence.outcomes_decide`): no result a
campaign left behind can change it, so campaigns are neither registered nor
adopted for it, and no registration makes it unmet.

A registration that cannot be decoded names every Goal that is judged by its
Test Runs' outcomes, because what it holds cannot be told. The way out needs a human:
when an approved Goal change is applied, unreadable registrations are moved
to ``quarantine/`` beside the others (:func:`quarantine_unreadable`). The
review package lists every registration that is open or quarantined
(:func:`unsettled_campaigns`).

Residual risk. A campaign's work-item files are as writable as the worktree:
a result deleted before it is adopted is not an integrity error, its run
only reads as started without a result. Protecting those files belongs to
protected inputs (issue #1111), not to this module.

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
from booley.criteria.state import CriterionChange, DevelopmentState
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
from booley.goals.test_run_evidence import goal_test_runs, judged_change, outcomes_decide
from booley.goals.test_run_goals import names_test_runs
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.timefmt import parse_timestamp, utc_now_rfc3339

logger = logging.getLogger(__name__)

REGISTRATION_SCHEMA = "booley.goal-campaign/v1"
#: Detail key of an adopted observation: the campaign and results it was adopted from.
ADOPTED_DETAIL_KEY = "adopted_campaign"
REASON_UNACCOUNTED = "goal-campaign-unaccounted"
_QUARANTINE = "quarantine"

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
        "unfinished",
        "blocked",
        "settled",
    }
)

# Why a registration that was never read keeps its Goals unmet.
_NOT_READ = "has not been read since it was registered"


class RegistrationFormatError(ValueError):
    """A stored campaign registration does not have the registration format."""


@dataclass(frozen=True)
class CampaignRegistration:
    """One Simulation Campaign a bound run started, and what of it is in the ledger.

    ``stamps`` holds, per Goal of the campaign's Target, the source
    fingerprint detail of the file contents the campaign started at.
    ``accounted`` names the work-item results (id, digest) whose outcomes the
    ledger holds, ``unfinished`` the Test Runs (name, fingerprint) that were
    started and had no result when the campaign was last read. ``blocked``
    says why the campaign's results may still change or cannot be told
    (empty once it was read idle); ``settled`` says nothing more can come
    out of the campaign.
    """

    campaign_id: str
    manifest_sha256: str
    manifest_path: str
    binding: Mapping[str, Any]
    stamps: Mapping[str, Mapping[str, Any]]
    registered_at: str
    accounted: tuple[tuple[str, str], ...] = ()
    unfinished: tuple[tuple[str, str], ...] = ()
    blocked: str = _NOT_READ
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
            "unfinished": [list(pair) for pair in self.unfinished],
            "blocked": self.blocked,
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
            unfinished = require_list(document["unfinished"], field=f"{where}.unfinished")
            blocked = document["blocked"]
            if not isinstance(blocked, str):
                raise RegistrationFormatError(f"{where}.blocked must be a string")
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
                tuple(_result_pair(pair, f"{where}.unfinished") for pair in unfinished),
                blocked,
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
        raise RegistrationFormatError(f"{where} must hold pairs of two names")
    return str(pair[0]), str(pair[1])


@dataclass(frozen=True)
class Unaccounted:
    """Durable evidence of Goals *goal_keys* that is not in their ledger, and why."""

    goal_keys: tuple[str, ...]
    what: str
    reason: str
    remedy: str = "Read status again once it has ended, or resume it"

    def text(self) -> str:
        """The reason a Goal it names reads unmet."""
        return (
            f"{REASON_UNACCOUNTED}: {self.what} {self.reason}; what it observed is not in "
            f"the Goal's evidence. {self.remedy}"
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


def _stored(paths: GoalRecordPaths) -> tuple[list[CampaignRegistration], list[tuple[Path, str]]]:
    """Every stored registration, oldest first, and every file that cannot be decoded.

    Each document is decoded on its own: one unreadable file does not hide
    the others.
    """
    directory = _directory(paths)
    found: list[CampaignRegistration] = []
    unreadable: list[tuple[Path, str]] = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else ():
        try:
            found.append(_read(path))
        except RegistrationFormatError as exc:
            unreadable.append((path, str(exc)))
    found.sort(key=lambda item: (item.registered_at, item.manifest_sha256))
    return found, unreadable


def quarantine_unreadable(store: GoalStore, record_id: str) -> tuple[str, ...]:
    """Move every registration that cannot be decoded to ``quarantine/``; return their names.

    Called when an approved Goal change is applied, under the record lock:
    the human's approval is what lets an unreadable registration stop
    blocking the Goals. Each move is one atomic rename, and a file that was
    moved already is not there to move again.
    """
    paths = record_paths(store.project_dir, record_id)
    _, unreadable = _stored(paths)
    moved: list[str] = []
    for path, _reason in unreadable:
        target = _directory(paths) / _QUARANTINE / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        moved.append(path.name)
    return tuple(moved)


def unsettled_campaigns(store: GoalStore, record: GoalRecord) -> list[dict[str, Any]]:
    """What the review package lists: registrations that are open, unreadable or quarantined."""
    paths = record_paths(store.project_dir, record.id)
    found, unreadable = _stored(paths)
    listed: list[dict[str, Any]] = [
        {
            "state": "open",
            "campaign_id": item.campaign_id,
            "manifest_path": item.manifest_path,
            "blocked": item.blocked,
            "started_without_result": [name for name, _ in item.unfinished],
        }
        for item in found
        if not item.settled
    ]
    listed += [{"state": "unreadable", "file": path.name} for path, _ in unreadable]
    quarantine = _directory(paths) / _QUARANTINE
    if quarantine.is_dir():
        names = sorted(path.name for path in quarantine.glob("*.json"))
        listed += [{"state": "quarantined", "file": name} for name in names]
    return listed


def _relevant_keys(registration: CampaignRegistration, record: GoalRecord) -> tuple[str, ...]:
    """The Goals *registration* stamped that still have the revision its run was bound to.

    A Goal an approved change gave another revision started its evidence
    again: nothing a campaign observed before counts for it either way. Nor
    does it for a Goal its Test Runs' outcomes do not decide.
    """
    try:
        revisions = dict(registration.run_binding().spec_revisions)
    except GoalBindingError:
        revisions = {}
    current = {goal.spec.key: goal.spec_revision for goal in _outcome_goals(record)}
    return tuple(
        key
        for key in sorted(registration.stamps)
        if key in current and revisions.get(key, current[key]) == current[key]
    )


def _outcome_goals(record: GoalRecord) -> tuple[RecordedGoal, ...]:
    """The Goals of *record* that a Test Run outcome outside their ledger could change."""
    return tuple(
        goal for goal in record.goals if names_test_runs(goal.spec) and outcomes_decide(goal)
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
    judged by through its Test Runs' outcomes, is not registered. A campaign that is registered already (it
    is being resumed) keeps what was accounted and is open again.
    """
    if not campaign.candidate:
        return
    record = store.load(binding.record_id)
    stamps: dict[str, Mapping[str, Any]] = {}
    for goal in _outcome_goals(record):
        admitted = binding.spec_revision(goal.spec.key) == goal.spec_revision
        named = goal.spec.target in campaign.target_names
        if not named or not admitted:
            continue
        stamped = stamp(goal)
        if stamped is not None:
            stamps[goal.spec.key] = stamped
    if not stamps:
        return
    paths = record_paths(store.project_dir, binding.record_id)
    path = _path(paths, campaign.manifest_sha256)
    accounted = _accounted_so_far(path)
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


def _accounted_so_far(path: Path) -> tuple[tuple[str, str], ...]:
    """What an earlier registration of the same campaign already brought into the ledger."""
    try:
        return _read(path).accounted if path.exists() else ()
    except RegistrationFormatError:
        return ()  # unreadable: adopting its results again is harmless


def mark_published(store: GoalStore, record_id: str, acceptance_facts: Mapping[str, Any]) -> None:
    """Settle the campaign whose acceptance transaction was just selected.

    The transaction consumed every result the campaign held when it returned
    its outcome. The caller holds the record lock.
    """
    paths = record_paths(store.project_dir, record_id)
    path = _path(paths, str(acceptance_facts.get("manifest_sha256")))
    if not path.exists():
        return
    published = replace(
        _read(path), accounted=_consumed(acceptance_facts), unfinished=(), blocked="", settled=True
    )
    _write(paths, published)


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

    Read-only. Names the Goals of every open registration whose results may
    still change or cannot be told (module docstring), of every acceptance
    transaction the state does not select, and, for a registration that
    cannot be decoded, every Goal that is judged by its Test Runs' outcomes.
    """
    paths = record_paths(store.project_dir, record.id)
    everything = tuple(goal.spec.key for goal in record.goals if names_test_runs(goal.spec))
    decided = tuple(goal.spec.key for goal in _outcome_goals(record))
    registrations, unreadable = _stored(paths)
    found = [
        _open_campaign(item, _relevant_keys(item, record), item.blocked)
        for item in registrations
        if not item.settled and item.blocked
    ]
    found += [
        Unaccounted(
            decided,
            f"campaign registration {path.name}",
            f"cannot be read ({reason})",
            "It is moved aside when a human approves a Goal change; nothing else clears it",
        )
        for path, reason in unreadable
    ]
    try:
        found.extend(_unselected(record, paths, state))
    except AcceptanceLedgerError as exc:
        found.append(Unaccounted(everything, "recorded evidence", f"cannot be read ({exc})"))
    reasons: dict[str, str] = {}
    for item in found:
        for key in item.goal_keys:
            reasons.setdefault(key, item.text())
    return reasons


def started_without_result(store: GoalStore, record: GoalRecord) -> dict[str, str]:
    """A note per Goal whose frozen Test Run a campaign started and left without a result.

    Such a run reads ``missing`` in the Goal's ledger; the note says that it
    was not simply never run. Read-only, from the open registrations.
    """
    paths = record_paths(store.project_dir, record.id)
    notes: dict[str, str] = {}
    for item in _stored(paths)[0]:
        left = set(item.unfinished)
        for key in _relevant_keys(item, record) if left and not item.settled else ():
            resolution = record.test_run_resolution(key)
            runs = () if resolution is None else resolution.test_runs
            names = [run.name for run in runs if (run.name, run.fingerprint) in left]
            if names:
                notes.setdefault(
                    key,
                    f"{', '.join(names)} started in Simulation Campaign {item.campaign_id} "
                    f"({item.manifest_path}) and left no result",
                )
    return notes


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
            "A status read or the next recorded run completes it",
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
    with store.record_lock(record_id):
        record = store.load(record_id)
        paths = record_paths(store.project_dir, record_id)
        if record.state is not GoalState.ACTIVE or pending_applies(paths.root):
            return
        run = _Accounting(store, record, paths, reader)
        _complete_transactions(run)
        registrations, unreadable = _stored(paths)
        for path, reason in unreadable:
            logger.warning("campaign registration %s of Goal Mode %s: %s", path, record_id, reason)
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


def _complete_transactions(run: _Accounting) -> None:
    """Complete every live transaction the Goal state does not select; log what cannot be."""
    from booley.goals.recorder import GOAL_SCOPE, GoalProjection

    state = run.state()
    logs_dir = run.paths.logs_dir
    projection = GoalProjection(run.store, run.record.id)
    for pending in evidence_ledger.unselected_transactions(GOAL_SCOPE, logs_dir, state):
        if not _current_group(pending.identity, run.record):
            continue
        try:
            evidence_ledger.complete_transaction(GOAL_SCOPE, projection, logs_dir, state, pending)
        except AcceptanceLedgerError as exc:
            logger.warning(
                "acceptance transaction %s of Goal Mode %s cannot be completed: %s",
                pending.transaction_id,
                run.record.id,
                exc,
            )
            continue
        if pending.acceptance_facts is not None:
            mark_published(run.store, run.record.id, pending.acceptance_facts)


def _run_id(registration: CampaignRegistration) -> str:
    return str(registration.binding.get("invocation_id", ""))


def _settle(run: _Accounting, registration: CampaignRegistration) -> str:
    """Read an open *registration* again, adopt what is new, and say why it stays blocked.

    The registration is settled only when its campaign is complete: every
    work item has a result, and all of them are adopted. A campaign that is
    idle and readable but ran only in part stays open and unblocked; one that
    is running or cannot be read stays open and blocked (the returned text).
    """
    if run.reader is None:
        return "this caller cannot read Simulation Campaigns"
    try:
        durable = run.reader(Path(registration.manifest_path), registration.manifest_sha256)
    except CampaignBusyError:
        return _blocked(run, registration, "is still running")
    except ValueError as exc:  # the campaign's integrity error
        return _blocked(run, registration, f"cannot be read ({exc})")
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
    read = replace(
        registration,
        accounted=durable.results,
        unfinished=durable.unfinished,
        blocked="",
        settled=durable.complete,
    )
    if read != registration:
        _write(run.paths, read)
    return ""


def _blocked(run: _Accounting, registration: CampaignRegistration, reason: str) -> str:
    """Record that *registration*'s results may still change or cannot be told."""
    if registration.blocked != reason:
        _write(run.paths, replace(registration, blocked=reason))
    return reason


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
    from booley.goals.recorder import GOAL_SCOPE

    key = goal.spec.key
    frozen = goal_test_runs(goal, run.record.test_run_resolution(key))
    wanted = {"fail", "pass"} if passes else {"fail"}
    rows = [row for row in frozen.matching(durable.rows) if row["grade"] in wanted]
    if not rows or key not in state.criteria:
        return False
    marker = _marker(registration, durable, passes=passes)
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
    change = _append(run, state, registration, goal, detail, history)
    state.set_criterion(key, change.met, detail=dict(change.detail))
    return True


def _marker(
    registration: CampaignRegistration, durable: DurableCampaignResults, *, passes: bool
) -> dict[str, Any]:
    """What identifies one adoption: the campaign, the results it held, what was taken."""
    results = {"results": [list(pair) for pair in durable.results]}
    return {
        "campaign_id": registration.campaign_id,
        "manifest_sha256": registration.manifest_sha256,
        "results_sha256": hashlib.sha256(canonical_json(results)).hexdigest(),
        "passes": passes,
    }


def _append(
    run: _Accounting,
    state: DevelopmentState,
    registration: CampaignRegistration,
    goal: RecordedGoal,
    detail: dict[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> CriterionChange:
    """Judge an adopted observation by the Goal's ledger and append it to the evidence."""
    from booley.goals.recorder import GOAL_SCOPE, goal_identity

    key = goal.spec.key
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
    return change


def _mirrors(state: DevelopmentState, key: str, met: object, detail: object) -> bool:
    entry = state.criteria[key]
    return entry.met == met and entry.detail == detail
