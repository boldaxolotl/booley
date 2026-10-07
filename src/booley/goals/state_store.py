"""Goal Mode's ``booley_state.json``: fail-closed loads and merging saves (ADR 0067 D15, B2, B10).

``booley_state.json`` has several whole-file writers, and two runs of one
Goal Mode may finish together, so a Goal save never writes its snapshot
blindly. :class:`GoalStatePersistence` is the
:class:`~booley.criteria.state.StatePersistence` strategy every Goal run's
state saves through:

- **Baseline.** Each :class:`~booley.criteria.state.DevelopmentState`
  instance carries its own baseline (``persistence_baseline``): the state as
  loaded, replaced by the merged result after each of its own saves. A
  ``deepcopy`` shadow copies it, and a writer that copies a saved shadow back
  calls :meth:`~booley.criteria.state.DevelopmentState.adopt`, which takes the
  shadow's baseline too.
- **Save.** Under the publication gate (record lock and binding checks,
  :mod:`booley.goals.publication`) a save re-reads the file and merges three
  ways: this instance's values against its baseline against the disk.
  Criteria and the top-level scalars and maps merge per key: a key this
  instance did not change takes the disk's value, a key it changed takes its
  own (the later save wins a same-key conflict, as serialised whole updates
  would). ``timeline`` entries (by canonical JSON) and
  ``acceptance_transactions`` (by id) merge by identity, never by length:
  disk entries are kept in disk order, entries new to this instance are
  appended in its order, and duplicates collapse. The merged state is written
  with one atomic replacement (flushed through a writable handle) and becomes
  both this instance's values and its baseline.
- **Refusals.** A save whose baseline holds a Criterion the instance lacks is
  refused: removing a Goal is not a Flow write. A failed gate, merge, or
  write leaves the instance and its baseline unchanged and propagates.
- **Fail-closed load.** A corrupt or malformed state file raises
  :class:`GoalStateError` instead of loading as empty. A missing file loads
  the record's declared Goals, every one unmet with no evidence (baseline
  pins frozen at entry are not recoverable from the record).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

from booley.core.boundary import (
    require_bool_value,
    require_dict,
    require_list,
    require_str_value,
)
from booley.criteria.categories import verification_fingerprint_categories
from booley.criteria.state import CriterionEntry, DevelopmentState
from booley.goals.binding import GoalRunBinding
from booley.goals.model import GoalRecord, GoalSpec
from booley.goals.paths import record_paths
from booley.goals.publication import PublicationGate
from booley.goals.store import GoalStore, GoalStoreError
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.domain import TARGET_IDENTITY_PARAM

# Top-level state fields merged as single values, and those merged per key.
_SCALAR_FIELDS = (
    "slug",
    "ticket_type",
    "strict_criteria",
    "work_dir",
    "authorized_zero_mandatory_basis_id",
)
_KEYED_FIELDS = ("criteria", "category_map", "flow_key_aliases")
_STATE_FILE_MODE = 0o644

Snapshot = dict[str, Any]


class GoalStateError(RuntimeError):
    """A Goal state file cannot be loaded or a save was refused."""


# ---------------------------------------------------------------------------
# Declared Goals
# ---------------------------------------------------------------------------


def goal_criterion_params(goals: Iterable[GoalSpec]) -> dict[str, dict[str, Any]]:
    """State params per Goal key: the Criterion params plus the bound Target."""
    params: dict[str, dict[str, Any]] = {}
    for goal in goals:
        entry = dict(goal.params)
        if goal.target is not None:
            entry.setdefault(TARGET_IDENTITY_PARAM, goal.target)
        params[goal.key] = entry
    return params


def review_category(key: str) -> str:
    """The one source category a review Goal judges (``criteria.categories``)."""
    (category,) = verification_fingerprint_categories(key)
    return category


def review_categories(keys: Iterable[str]) -> dict[str, str]:
    """The category override of every review Goal among *keys*."""
    return {key: review_category(key) for key in keys if key.startswith("review_")}


def declared_goal_state(record: GoalRecord, work_dir: Path | str) -> DevelopmentState:
    """An unbound state holding *record*'s Goals, every one mandatory, unmet, strict (D4)."""
    params = goal_criterion_params(goal.spec for goal in record.goals)
    state = DevelopmentState()
    state.slug = record.id
    state.work_dir = str(work_dir)
    state.init_criteria(
        dict.fromkeys(params, True),
        category_overrides=review_categories(params),
        criterion_params=params,
        strict=True,
    )
    return state


# ---------------------------------------------------------------------------
# Strict reading
# ---------------------------------------------------------------------------


def read_goal_state_file(path: Path) -> DevelopmentState | None:
    """Parse a Goal state file strictly; ``None`` when it does not exist.

    Raises :class:`GoalStateError` naming the file for unreadable, non-JSON,
    or malformed content.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GoalStateError(f"cannot read Goal state {path}: {exc}") from exc
    try:
        data: object = json.loads(raw.decode("utf-8-sig"))
        _validate_state_object(data)  # a dict of the right shapes: no AttributeError below
        return DevelopmentState.from_json_object(data)
    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise GoalStateError(f"corrupt Goal state {path}: {exc}") from exc


def _validate_state_object(data: object) -> None:
    if not isinstance(data, dict):
        raise TypeError("content is not a JSON object")
    mapping = cast("dict[str, Any]", data)
    for name in (*_SCALAR_FIELDS, "last_updated"):
        if name in mapping and not isinstance(
            mapping[name], bool if name == "strict_criteria" else str
        ):
            raise TypeError(f"{name} has the wrong type")
    for name in _KEYED_FIELDS:
        if not isinstance(mapping.get(name, {}), dict):
            raise TypeError(f"{name} must be an object")
    for key, entry in cast("dict[str, Any]", mapping.get("criteria", {})).items():
        if not isinstance(entry, dict):
            raise TypeError(f"criteria.{key} must be an object")
        _validate_criterion(f"criteria.{key}", cast("dict[str, Any]", entry))
    timeline = mapping.get("timeline", [])
    if not isinstance(timeline, list) or not all(
        isinstance(item, dict) for item in cast("list[Any]", timeline)
    ):
        raise TypeError("timeline must be a list of objects")


_CRITERION_BOOLS = ("met", "mandatory", "ever_met", "ever_failed", "locked", "stale")


def _validate_criterion(where: str, entry: dict[str, Any]) -> None:
    """Refuse a Criterion whose fields ``CriterionEntry.from_dict`` would copy unchecked."""
    for name in _CRITERION_BOOLS:
        if name in entry:
            require_bool_value(entry[name], field=f"{where}.{name}")
    if "updated_at" in entry:
        require_str_value(entry["updated_at"], field=f"{where}.updated_at", allow_empty=True)
    for name in ("detail", "params"):
        if name in entry:
            require_dict(entry[name], field=f"{where}.{name}")
    if "transition_evidence" in entry:
        for item in require_list(
            entry["transition_evidence"], field=f"{where}.transition_evidence"
        ):
            require_dict(item, field=f"{where}.transition_evidence entry")


def load_goal_state(store: GoalStore, record: GoalRecord) -> DevelopmentState:
    """Read *record*'s state for presentation: fail-closed, declared Goals when missing."""
    path = record_paths(store.project_dir, record.id).state_file
    state = read_goal_state_file(path)
    return declared_goal_state(record, record.worktree_path) if state is None else state


# ---------------------------------------------------------------------------
# The three-way merge
# ---------------------------------------------------------------------------


def state_snapshot(state: DevelopmentState) -> Snapshot:
    """The mergeable JSON form of *state* (no derived or save-stamped fields)."""
    snapshot: Snapshot = {name: getattr(state, name) for name in _SCALAR_FIELDS}
    snapshot["criteria"] = {key: entry.to_dict() for key, entry in state.criteria.items()}
    snapshot["category_map"] = state.category_map
    snapshot["flow_key_aliases"] = state.flow_key_aliases
    snapshot["timeline"] = state.timeline
    snapshot["acceptance_transactions"] = state.acceptance_transactions
    return cast("Snapshot", json.loads(json.dumps(snapshot)))


def merge_states(base: Snapshot, local: Snapshot, disk: Snapshot) -> Snapshot:
    """Merge *local* (an instance) and *disk*, both descended from *base* (module docstring)."""
    merged: Snapshot = {
        name: local[name] if local[name] != base[name] else disk[name] for name in _SCALAR_FIELDS
    }
    for name in _KEYED_FIELDS:
        merged[name] = _merge_keyed(base[name], local[name], disk[name])
    merged["timeline"] = _merge_by_identity(
        base["timeline"], local["timeline"], disk["timeline"], _canonical
    )
    merged["acceptance_transactions"] = _merge_by_identity(
        base["acceptance_transactions"],
        local["acceptance_transactions"],
        disk["acceptance_transactions"],
        lambda transaction: transaction,
    )
    return merged


def _merge_keyed(
    base: Mapping[str, Any], local: Mapping[str, Any], disk: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(disk)
    for key, value in local.items():
        if base.get(key, _ABSENT) != value:
            merged[key] = value
    for key in base.keys() - local.keys():
        if disk.get(key, _ABSENT) == base[key]:
            merged.pop(key, None)  # this instance removed a key nobody else changed
    return merged


def _merge_by_identity(
    base: Sequence[Any],
    local: Sequence[Any],
    disk: Sequence[Any],
    identity: Callable[[Any], Hashable],
) -> list[Any]:
    merged: list[Any] = []
    seen: set[Hashable] = set()
    for item in disk:
        key = identity(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    inherited = {identity(item) for item in base}
    for item in local:
        key = identity(item)
        if key not in seen and key not in inherited:
            seen.add(key)
            merged.append(item)
    return merged


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class _Absent:
    """Marker for a key one side of a merge does not have."""


_ABSENT = _Absent()


def _state_from_snapshot(snapshot: Snapshot, last_updated: str) -> DevelopmentState:
    return DevelopmentState.from_json_object(
        {**copy.deepcopy(snapshot), "last_updated": last_updated}
    )


def _apply_snapshot(state: DevelopmentState, merged: Snapshot, last_updated: str) -> None:
    """Make *state*'s values *merged* in place, keeping unchanged entry objects."""
    for name in _SCALAR_FIELDS:
        setattr(state, name, merged[name])
    criteria: dict[str, CriterionEntry] = {}
    for key, value in merged["criteria"].items():
        current = state.criteria.get(key)
        fresh = CriterionEntry.from_dict(copy.deepcopy(value))
        if current is None:
            criteria[key] = fresh
            continue
        if current.to_dict() != value:
            for item in fields(CriterionEntry):
                setattr(current, item.name, getattr(fresh, item.name))
        criteria[key] = current
    state.criteria.clear()
    state.criteria.update(criteria)
    for name in ("category_map", "flow_key_aliases"):
        target = cast("dict[str, Any]", getattr(state, name))
        target.clear()
        target.update(copy.deepcopy(merged[name]))
    state.timeline[:] = copy.deepcopy(merged["timeline"])
    state.acceptance_transactions[:] = list(merged["acceptance_transactions"])
    state.last_updated = last_updated


# ---------------------------------------------------------------------------
# The strategy
# ---------------------------------------------------------------------------


class GoalStatePersistence:
    """Load fail-closed and save by locked three-way merge, under one run's gate."""

    def __init__(self, binding: GoalRunBinding, gate: PublicationGate | None = None) -> None:
        self._binding = binding
        self._gate = gate if gate is not None else PublicationGate(binding)

    @property
    def state_file(self) -> Path:
        """The bound record's ``booley_state.json``."""
        return record_paths(self._binding.project_dir, self._binding.record_id).state_file

    def read(self, path: Path) -> DevelopmentState:
        """The strict content of *path*, or the record's declared Goals when it is missing."""
        self._require_bound_file(path)
        state = read_goal_state_file(path)
        if state is None:
            state = declared_goal_state(self._record(), self._binding.worktree_root)
        return state

    def _require_bound_file(self, path: Path) -> None:
        """Refuse a state file that is not the bound record's own."""
        if path.resolve() != self.state_file.resolve():
            raise GoalStateError(
                f"state file {path} does not belong to Goal Mode {self._binding.record_id}"
            )

    def loaded(self, state: DevelopmentState) -> None:
        """Capture *state* as loaded: the baseline its saves merge against."""
        state.persistence_baseline = state_snapshot(state)

    def _record(self) -> GoalRecord:
        try:
            return self._gate.store.load(self._binding.record_id)
        except GoalStoreError as exc:
            raise GoalStateError(f"cannot load the Goals of a missing state file: {exc}") from exc

    def save(self, state: DevelopmentState) -> None:
        """Merge *state* into the file under the publication gate (module docstring)."""
        path = state.file_path
        if path is None:
            return
        self._require_bound_file(path)
        base = state.persistence_baseline
        if base is None:
            raise GoalStateError("a Goal state must be loaded through its Goal persistence")
        local = state_snapshot(state)
        removed = sorted(base["criteria"].keys() - local["criteria"].keys())
        if removed:
            raise GoalStateError(f"a Flow write cannot remove Goals {removed}")
        affected = {
            key for key, value in local["criteria"].items() if base["criteria"].get(key) != value
        }
        with self._gate.publishing(affected):
            disk_state = read_goal_state_file(path)
            disk = base if disk_state is None else state_snapshot(disk_state)
            merged = merge_states(base, local, disk)
            stamp = utc_now_rfc3339()
            written = _state_from_snapshot(merged, stamp)
            encoded = json.dumps(written.to_dict(), indent=2).encode("utf-8")
            atomic_replace_bytes(path, encoded, mode=_STATE_FILE_MODE)
        _apply_snapshot(state, merged, stamp)
        state.persistence_baseline = state_snapshot(state)
