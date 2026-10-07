"""The immutable binding of one run to one active Goal Mode (ADR 0067 Phase 3, B3).

A run is bound once, when it is admitted: :func:`bind_run` resolves the
worktree's occupying Goal Record, refuses anything but an ``active`` record
whose Goal Branch is checked out, and snapshots the protected inputs as the
run starts (D7). The resulting :class:`GoalRunBinding` is what the
publication gate (:mod:`booley.goals.publication`), the evidence recorder, and
the state persistence compare against when the run's evidence is published;
nothing is re-resolved at completion, so a run can never adopt a record that
was entered after it started.

The binding round-trips through a JSON object (:meth:`GoalRunBinding.to_json`,
:meth:`GoalRunBinding.from_json`) so a job record can carry it to whichever
process completes the run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from booley.core.boundary import (
    BoundaryError,
    require_bool_value,
    require_positive_int,
    require_str_value,
)
from booley.goals.apply_barrier import require_no_apply
from booley.goals.checkout import CheckoutError, GoalCheckout, branch_ref
from booley.goals.model import GoalRecord, GoalRecordFormatError, GoalState, WorktreeIdentity
from booley.goals.paths import GoalIdError, record_paths, validate_goal_id
from booley.goals.proposals import ProposalError
from booley.goals.protected_inputs import (
    ProtectedInputError,
    ProtectedInputRoots,
    ProtectedPath,
    ProtectedSnapshot,
    snapshot_protected_inputs,
)
from booley.goals.store import GoalStore, GoalStoreError
from booley.goals.target_surface import TargetSurfaceError, target_surface_fingerprint
from booley.targets.domain import FuseSocError

BINDING_SCHEMA = "booley.goal-run-binding/v1"
_JSON_FIELDS = frozenset(
    {
        "schema",
        "project_dir",
        "record_id",
        "record_revision",
        "worktree",
        "worktree_root",
        "goal_branch",
        "invocation_id",
        "spec_revisions",
        "protected_paths",
        "start_digest",
        "start_head_digest",
        "eligible",
        "ineligible_reason",
        "start_surfaces",
    }
)


class GoalBindingError(RuntimeError):
    """A run cannot be bound to a Goal Mode; the message says why."""


@dataclass(frozen=True)
class GoalRunBinding:
    """What one run was admitted under; compared again when it publishes.

    ``spec_revisions`` pairs every Goal key of the record with the
    ``spec_revision`` it had at admission, sorted by key. ``protected_paths``
    and the two start digests are the protected inputs as the run started
    (working view and HEAD view, :mod:`booley.goals.protected_inputs`).
    ``eligible`` is ``False`` when they already differed from the record's
    entry snapshot at admission: such a run may execute, but nothing it
    produces is published, whatever happens later (D7).
    """

    project_dir: Path
    record_id: str
    record_revision: int
    worktree: WorktreeIdentity
    worktree_root: Path
    goal_branch: str
    invocation_id: str
    spec_revisions: tuple[tuple[str, int], ...]
    protected_paths: tuple[str, ...]
    start_digest: str
    start_head_digest: str
    eligible: bool
    ineligible_reason: str = ""
    start_surfaces: tuple[tuple[str | None, str | None], ...] = ()

    @property
    def goal_keys(self) -> tuple[str, ...]:
        """Every Goal key of the record at admission, sorted."""
        return tuple(key for key, _revision in self.spec_revisions)

    def spec_revision(self, key: str) -> int | None:
        """The ``spec_revision`` Goal *key* had at admission, or ``None`` if it was no Goal."""
        return dict(self.spec_revisions).get(key)

    def start_surface(self, target: str | None) -> str | None:
        """The ``target_surface`` digest *target* had at admission; ``None`` if unresolvable."""
        return dict(self.start_surfaces).get(target)

    def start_snapshot(self) -> ProtectedSnapshot:
        """The protected inputs as the run started."""
        return ProtectedSnapshot(
            tuple(ProtectedPath.decode(path) for path in self.protected_paths),
            self.start_digest,
            self.start_head_digest,
        )

    def protected_roots(self) -> ProtectedInputRoots:
        """Where the protected inputs of this run are read from."""
        return ProtectedInputRoots(self.worktree_root, self.project_dir)

    def to_json(self) -> dict[str, Any]:
        """The JSON object a job record stores."""
        return {
            "schema": BINDING_SCHEMA,
            "project_dir": str(self.project_dir),
            "record_id": self.record_id,
            "record_revision": self.record_revision,
            "worktree": self.worktree.to_json(),
            "worktree_root": str(self.worktree_root),
            "goal_branch": self.goal_branch,
            "invocation_id": self.invocation_id,
            "spec_revisions": dict(self.spec_revisions),
            "protected_paths": list(self.protected_paths),
            "start_digest": self.start_digest,
            "start_head_digest": self.start_head_digest,
            "eligible": self.eligible,
            "ineligible_reason": self.ineligible_reason,
            "start_surfaces": [list(pair) for pair in self.start_surfaces],
        }

    @classmethod
    def from_json(cls, raw: object) -> GoalRunBinding:
        """Parse a stored binding, raising :class:`GoalBindingError` on any defect."""
        try:
            return _parse_binding(raw)
        except (BoundaryError, GoalRecordFormatError, GoalIdError, ValueError) as exc:
            raise GoalBindingError(f"invalid Goal run binding: {exc}") from exc


def _parse_binding(raw: object) -> GoalRunBinding:
    if not isinstance(raw, Mapping):
        raise ValueError("a binding is a JSON object")
    mapping = cast("Mapping[str, Any]", raw)
    if frozenset(mapping) != _JSON_FIELDS:
        raise ValueError(f"fields must be exactly {sorted(_JSON_FIELDS)}")
    if mapping["schema"] != BINDING_SCHEMA:
        raise ValueError(f"unsupported schema {mapping['schema']!r}")
    raw_revisions = mapping["spec_revisions"]
    if not isinstance(raw_revisions, Mapping):
        raise ValueError("spec_revisions must be an object")
    revisions = cast("Mapping[object, object]", raw_revisions)
    raw_paths = mapping["protected_paths"]
    if not isinstance(raw_paths, list):
        raise ValueError("protected_paths must be a list")
    paths = cast("list[object]", raw_paths)
    return GoalRunBinding(
        project_dir=Path(require_str_value(mapping["project_dir"], field="project_dir")),
        record_id=validate_goal_id(require_str_value(mapping["record_id"], field="record_id")),
        record_revision=_positive_int(mapping["record_revision"], "record_revision"),
        worktree=WorktreeIdentity.from_json(mapping["worktree"], where="worktree"),
        worktree_root=Path(require_str_value(mapping["worktree_root"], field="worktree_root")),
        goal_branch=require_str_value(mapping["goal_branch"], field="goal_branch"),
        invocation_id=require_str_value(mapping["invocation_id"], field="invocation_id"),
        spec_revisions=tuple(
            sorted(
                (
                    require_str_value(key, field="spec_revisions key"),
                    _positive_int(value, str(key)),
                )
                for key, value in revisions.items()
            )
        ),
        protected_paths=tuple(
            require_str_value(path, field="protected_paths entry") for path in paths
        ),
        start_digest=require_str_value(mapping["start_digest"], field="start_digest"),
        start_head_digest=require_str_value(
            mapping["start_head_digest"], field="start_head_digest"
        ),
        eligible=require_bool_value(mapping["eligible"], field="eligible"),
        ineligible_reason=require_str_value(
            mapping["ineligible_reason"], field="ineligible_reason", allow_empty=True
        ),
        start_surfaces=_surfaces(mapping["start_surfaces"]),
    )


def _surfaces(raw: object) -> tuple[tuple[str | None, str | None], ...]:
    if not isinstance(raw, list):
        raise ValueError("start_surfaces must be a list")
    pairs: list[tuple[str | None, str | None]] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, list) or len(cast("list[object]", item)) != 2:
            raise ValueError("start_surfaces entries are [target, digest] pairs")
        target, digest = cast("list[object]", item)
        if not all(value is None or isinstance(value, str) for value in (target, digest)):
            raise ValueError("start_surfaces entries hold strings or null")
        pairs.append((cast("str | None", target), cast("str | None", digest)))
    return tuple(pairs)


def _positive_int(value: object, label: str) -> int:
    return require_positive_int(value, field=label)


SurfaceResolver = Callable[[Path, str | None], dict[str, Any]]


def bind_run(
    store: GoalStore,
    work_dir: Path,
    invocation_id: str,
    *,
    surface: SurfaceResolver = target_surface_fingerprint,
) -> GoalRunBinding:
    """Bind run *invocation_id* in *work_dir* to the worktree's active Goal Mode.

    Refuses (:class:`GoalBindingError`) when the worktree hosts no Goal Mode,
    when its record is ``entering`` or ``finishing`` or cannot be read, when
    HEAD is not on the record's Goal Branch (a removed and re-created
    worktree inherits the record by name, not by branch), and when the
    protected inputs cannot be read. Protected inputs that already differ
    from the entry snapshot do not refuse the run; they make it ineligible to
    publish (:attr:`GoalRunBinding.eligible`). The ``target_surface`` of every
    bound Target is captured too, so publication can tell a Target declaration
    that changed while the run executed (*surface* resolves it).
    """
    if not invocation_id:
        raise GoalBindingError("a Goal run binding needs an invocation id")
    root = _worktree_root(work_dir)
    initial = _active_record(store, root)
    with store.record_lock(initial.id):
        record = _active_record(store, root)
        try:
            require_no_apply(record_paths(store.project_dir, record.id).root)
        except ProposalError as exc:
            raise GoalBindingError(str(exc)) from exc
        _require_goal_branch(root, record)
        try:
            snapshot = snapshot_protected_inputs(ProtectedInputRoots(root, store.project_dir))
        except ProtectedInputError as exc:
            raise GoalBindingError(f"cannot read the protected inputs of {root}: {exc}") from exc
        reason = protected_drift(record, snapshot)
        return GoalRunBinding(
            project_dir=store.project_dir,
            record_id=record.id,
            record_revision=record.revision,
            worktree=record.worktree,
            worktree_root=root,
            goal_branch=record.branch,
            invocation_id=invocation_id,
            spec_revisions=tuple(
                sorted((goal.spec.key, goal.spec_revision) for goal in record.goals)
            ),
            protected_paths=snapshot.encoded_paths,
            start_digest=snapshot.working_digest,
            start_head_digest=snapshot.head_digest,
            eligible=not reason,
            ineligible_reason=reason,
            start_surfaces=_start_surfaces(root, record, surface),
        )


def _start_surfaces(
    root: Path, record: GoalRecord, surface: SurfaceResolver
) -> tuple[tuple[str | None, str | None], ...]:
    targets = sorted(
        {goal.spec.target for goal in record.goals}, key=lambda t: (t is not None, t or "")
    )
    return tuple((target, surface_digest(root, target, surface)) for target in targets)


def surface_digest(root: Path, target: str | None, surface: SurfaceResolver) -> str | None:
    """*target*'s ``target_surface`` digest in *root*, or ``None`` when it cannot be resolved."""
    try:
        digest = surface(root, target).get("digest")
    except (TargetSurfaceError, FuseSocError, OSError, ValueError):
        return None
    return digest if isinstance(digest, str) else None


def checkout_drift(store: GoalStore, binding: GoalRunBinding) -> str:
    """Why the bound worktree no longer is what the run was admitted in; empty when it is."""
    try:
        identity = store.identify_worktree(binding.worktree_root)
        head = GoalCheckout(binding.worktree_root).head_ref()
    except (GoalStoreError, CheckoutError) as exc:
        return f"the Goal worktree cannot be inspected: {exc}"
    if identity != binding.worktree:
        return f"{binding.worktree_root} is no longer the worktree the run was admitted in"
    if head != branch_ref(binding.goal_branch):
        return f"the worktree is on {head or 'a detached HEAD'}, not Goal Branch {binding.goal_branch}"
    return ""


def protected_drift(record: GoalRecord, snapshot: ProtectedSnapshot) -> str:
    """Why *snapshot* differs from *record*'s entry snapshot; empty when it does not."""
    if snapshot.encoded_paths != record.protected_paths:
        return "protected inputs resolve to different files than at entry"
    if snapshot.working_digest != record.protected_digest:
        return "a protected input differs from its state at entry"
    if (
        record.protected_head_digest is not None
        and snapshot.head_digest != record.protected_head_digest
    ):
        return "a protected input differs from its state at entry in HEAD"
    return ""


def _worktree_root(work_dir: Path) -> Path:
    try:
        found = GoalCheckout(work_dir).containing_repository()
    except CheckoutError as exc:
        raise GoalBindingError(str(exc)) from exc
    if found is None:
        raise GoalBindingError(f"{work_dir} is not inside a Git worktree")
    toplevel, _prefix = found
    return toplevel


def _active_record(store: GoalStore, root: Path) -> GoalRecord:
    try:
        record = store.active_for_worktree(root)
    except GoalStoreError as exc:
        raise GoalBindingError(f"cannot resolve the Goal Mode of {root}: {exc}") from exc
    if record is None:
        raise GoalBindingError(f"worktree {root} hosts no Goal Mode")
    if record.state is not GoalState.ACTIVE:
        raise GoalBindingError(
            f"Goal Mode {record.id} is {record.state.value}; evidence binds only to an active one"
        )
    return record


def _require_goal_branch(root: Path, record: GoalRecord) -> None:
    try:
        head = GoalCheckout(root).head_ref()
    except CheckoutError as exc:
        raise GoalBindingError(str(exc)) from exc
    if head != branch_ref(record.branch):
        where = head or "a detached HEAD"
        raise GoalBindingError(
            f"worktree {root} is on {where}, not Goal Branch {record.branch} of Goal Mode "
            f"{record.id}"
        )
