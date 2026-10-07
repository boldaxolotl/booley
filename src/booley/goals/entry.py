"""Entering Goal Mode: the locked, re-drivable entry transaction (ADR 0067 D3, D7, D8, D10, D15).

Entry runs under the worktree lock from start to finish, so two entries on one
worktree never interleave and a later entry may treat any ``entering``
occupant it finds as abandoned: a live entry would still hold the lock.

Order (each numbered step is a durable boundary; :func:`rollback_entering`
undoes whatever the boundaries reached):

0. Outside any lock: validate the arguments, require ``work_dir`` to be the
   root of a linked worktree (never the main checkout), translate the Goals.
1. Take the worktree lock. Roll back an ``entering`` occupant; refuse an
   ``active`` or ``finishing`` one.
2. Under the lock: read HEAD and its branch, refuse a dirty tree, check the
   default Goalset decision, refuse an existing Goal Branch name, check
   Targets (unknown candidate Targets warn; baseline Targets and spec files
   must exist at HEAD).
3. Publish the record as ``entering`` (``branch_created`` false).
4. ``git branch <goal-branch> <base>``, then save ``branch_created`` true.
5. Re-check that HEAD is still the original ref at the base commit and the
   trees are clean, ``git checkout <goal-branch>``, and confirm HEAD is the
   Goal Branch at the base commit.
6. Snapshot the protected inputs into the record, before anything slow runs.
7. Pin baselines and freeze recipe fingerprints (no record lock held).
8. Write ``booley_state.json`` with every Goal unmet and strict Criteria,
   flushed to disk.
9. Revalidate: HEAD is the Goal Branch at the base commit, the worktree and
   any paired Project checkout are clean, and the protected inputs still
   match the step-6 snapshot. Then promote the record to ``active``.

A failure inside steps 4-9, including a failed revalidation, rolls back at once. A process that dies there
leaves an ``entering`` record; the next entry on the worktree rolls it back
(step 1). Rollback never discards work: it returns HEAD to the original ref
only when HEAD is still on the Goal Branch at the base commit with a clean
tree and the original ref still at the base commit, deletes the Goal Branch
only by compare-and-swap on the base commit and
only when entry recorded creating it, and names everything it leaves behind
in the ``failed`` record.

The record lock is taken only around each save, never across a Git command,
a Target resolution, or anything another thread or process must finish.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_bool_value, require_list, require_str_value
from booley.criteria.state import DevelopmentState
from booley.criteria.templates import BASELINE_TARGET_PARAM
from booley.criteria.thresholds import has_relative_threshold
from booley.evidence.acceptance import PairedProjectBaseline
from booley.flows.baseline_pins import (
    BaselinePinError,
    PinWording,
    SnapshotBuilder,
    freeze_recipe_family,
    pin_cycle_count_baselines,
)
from booley.goals.checkout import CheckoutError, GoalCheckout, branch_name, branch_ref
from booley.goals.goalsets import GOALSETS_DIR
from booley.goals.model import (
    GoalArg,
    GoalArgError,
    GoalFamily,
    GoalRecord,
    GoalSpec,
    GoalState,
    RecordedGoal,
    WorktreeIdentity,
    parse_goal_args,
)
from booley.goals.paths import GoalIdError, new_goal_id, record_paths, validate_slug
from booley.goals.protected_inputs import (
    ProtectedInputError,
    ProtectedInputRoots,
    ProtectedSnapshot,
    protected_input_violations,
    snapshot_protected_inputs,
)
from booley.goals.rules import goal_mode_rules
from booley.goals.state_store import goal_criterion_params, review_categories
from booley.goals.store import GoalStore, GoalStoreError
from booley.goals.translate import GoalTranslationError, Translation, translate_goals
from booley.runtime.atomic_files import fsync_directory
from booley.runtime.git import is_linked_worktree
from booley.runtime.project_repositories import (
    RepositoryCheckoutError,
    paired_project_repository,
)
from booley.runtime.timefmt import compact_utc_now, utc_now_rfc3339
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, UnknownTargetError

GOAL_BRANCH_PREFIX = "goal/"
DEFAULT_GOALSET = "default"
# How pinning errors name a Goal Mode (they name a Ticket on the Ticket path).
GOAL_PIN_WORDING = PinWording(work_item="the Goal Mode", entry_point="Goal entry")
# Per-Target Goal families whose thresholds may compare against a baseline Target.
_BASELINE_FAMILIES = frozenset({GoalFamily.SYNTH, GoalFamily.FPGA, GoalFamily.CYCLE_COUNT})


class GoalEntryError(RuntimeError):
    """Entry was refused or failed; the message says why and what was left behind."""


@dataclass(frozen=True)
class RecipeFamily:
    """One implementation Criterion family whose Target recipe entry freezes.

    The snapshot builder comes from the concrete Flow, which ``booley.goals``
    may not import; the MCP entry point composes it in.
    """

    prefix: str
    flow_label: str
    snapshot_builder: SnapshotBuilder


@dataclass(frozen=True)
class EntryRequest:
    """Validated ``goal_enter`` arguments."""

    work_dir: Path
    slug: str
    goals: tuple[GoalArg, ...]
    goalsets_used: tuple[str, ...] = ()
    default_skipped: bool = False
    skip_reason: str | None = None
    session_key: str | None = None


def _no_other_sessions(_identity: WorktreeIdentity) -> Sequence[str]:
    """The session registry arrives with the Dashboard; until then nobody is reported."""
    return ()


@dataclass(frozen=True)
class EntryEnvironment:
    """What entry needs from its caller besides the request.

    ``project_dir`` is the control Project directory, resolved by the caller
    for the session (never a worktree's copy). ``other_sessions`` names the
    other live sessions on a worktree, for the shared-worktree warning.
    ``on_boundary`` is called with each durable boundary's name after it is
    reached; tests use it to stop entry there.
    """

    project_dir: Path
    recipe_families: tuple[RecipeFamily, ...] = ()
    other_sessions: Callable[[WorktreeIdentity], Sequence[str]] = _no_other_sessions
    on_boundary: Callable[[str], None] | None = None
    lock_timeout_s: float = 30.0


@dataclass(frozen=True)
class EntryResult:
    """An entered Goal Mode, the warnings entry raised, and the rules text."""

    record: GoalRecord
    warnings: tuple[str, ...]
    rules: str
    recovered: GoalRecord | None = None

    def render(self) -> str:
        """The chat rendering returned by ``goal_enter``."""
        record = self.record
        lines = [
            f"Goal Mode {record.id} entered in {record.worktree_path}.",
            f"Goal Branch: {record.branch} (base {record.base_sha[:12]}, "
            f"entered from {_describe_ref(record.original_ref)}).",
            "",
            "Goals (all unmet):",
            *(f"- {_describe_goal(goal.spec)}" for goal in record.goals),
        ]
        if self.recovered is not None:
            lines += [
                "",
                f"Rolled back interrupted entry {self.recovered.id}: {self.recovered.failure}",
            ]
        if self.warnings:
            lines += ["", "Warnings:", *(f"- {warning}" for warning in self.warnings)]
        lines += ["", "Rules:", self.rules.rstrip("\n")]
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def parse_entry_request(arguments: Mapping[str, Any], *, session_key: str | None) -> EntryRequest:
    """Validate untrusted ``goal_enter`` arguments; raise :class:`GoalEntryError`."""
    try:
        raw_work_dir = arguments.get("work_dir")
        if raw_work_dir is None or raw_work_dir == "":
            raise GoalEntryError(
                "goal_enter needs work_dir: the root of the linked worktree to enter Goal "
                "Mode in (create one with `booley worktree new <name>`)"
            )
        work_dir = Path(require_str_value(raw_work_dir, field="work_dir"))
        slug = validate_slug(require_str_value(arguments.get("slug"), field="slug"))
        goals = parse_goal_args(arguments.get("goals"), where="goals")
        goalsets = _goalset_names(arguments.get("goalsets_used", []))
        skipped = require_bool_value(
            arguments.get("default_skipped", False), field="default_skipped"
        )
        reason = arguments.get("skip_reason")
        skip_reason = None if reason is None else require_str_value(reason, field="skip_reason")
    except (BoundaryError, GoalArgError, GoalIdError) as exc:
        raise GoalEntryError(str(exc)) from None
    if not goals:
        raise GoalEntryError("goal_enter needs at least one Goal")
    return EntryRequest(work_dir, slug, goals, goalsets, skipped, skip_reason, session_key)


def _goalset_names(raw: object) -> tuple[str, ...]:
    names = [
        require_str_value(item, field="goalsets_used[]")
        for item in require_list(raw, field="goalsets_used")
    ]
    for name in names:
        if not name or "/" in name or "\\" in name or name.endswith(".md"):
            raise GoalEntryError(
                f"goalsets_used entry {name!r} must be a Goalset name without '.md'"
            )
    return tuple(dict.fromkeys(names))


# ---------------------------------------------------------------------------
# The transaction
# ---------------------------------------------------------------------------


def enter_goal_mode(request: EntryRequest, env: EntryEnvironment) -> EntryResult:
    """Run the entry transaction (module docstring) and return the active record."""
    work_dir = _require_linked_worktree_root(request.work_dir)
    project_dir = env.project_dir.absolute()
    _refuse_worktree_copy(project_dir, work_dir)
    translation = _translate(request.goals)
    checkout = GoalCheckout(work_dir)
    store = GoalStore(project_dir, lock_timeout_s=env.lock_timeout_s)
    try:
        identity = store.identify_worktree(work_dir, create=True)
        assert identity is not None  # create=True always returns an identity
        with store.worktree_lock(identity) as lock:
            recovered = _recover_occupant(store, identity, checkout)
            plan = _plan_entry(request, checkout, identity, translation, project_dir)
            created = store.create(lock, plan.record)
            _boundary(env, "created")
            try:
                active = _drive_entry(store, created, plan, env, checkout)
            except Exception as exc:
                failed = rollback_entering(store, created.id, checkout, f"entry failed: {exc}")
                raise GoalEntryError(
                    f"entering Goal Mode failed: {exc}. Record {failed.id} is "
                    f"{failed.state.value}: {failed.failure}"
                ) from exc
    except (GoalStoreError, CheckoutError) as exc:
        raise GoalEntryError(str(exc)) from exc
    warnings = (*plan.warnings, *_session_warnings(env, identity))
    return EntryResult(active, warnings, goal_mode_rules(), recovered)


@dataclass(frozen=True)
class _EntryPlan:
    """Everything decided under the worktree lock before the record is published."""

    record: GoalRecord
    criterion_params: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    warnings: tuple[str, ...] = ()


def _plan_entry(
    request: EntryRequest,
    checkout: GoalCheckout,
    identity: WorktreeIdentity,
    translation: Translation,
    project_dir: Path,
) -> _EntryPlan:
    """Validate the checkout and the Goals under the lock; build the new record."""
    base_sha = checkout.head_sha()
    head_ref = checkout.head_ref()
    paired = _paired_project_checkout(checkout.root)
    _require_clean(checkout, "the worktree")
    if paired is not None:
        _require_clean(paired, "the paired Project checkout")
    _check_default_goalset(request, project_dir)
    timestamp = compact_utc_now()
    branch = f"{GOAL_BRANCH_PREFIX}{request.slug}-{timestamp[:8]}"
    checkout.require_valid_branch_name(branch)
    if checkout.branch_tip(branch) is not None:
        raise GoalEntryError(f"branch {branch} already exists; choose another slug")
    warnings = [*translation.warnings, *_check_targets(checkout.root, translation.goals)]
    record = GoalRecord(
        id=new_goal_id(request.slug, timestamp=timestamp),
        state=GoalState.ENTERING,
        worktree=identity,
        worktree_path=str(checkout.root),
        branch=branch,
        original_ref=head_ref or base_sha,
        base_sha=base_sha,
        entered_at=utc_now_rfc3339(),
        session_key=request.session_key,
        goals=tuple(RecordedGoal(goal, 1) for goal in translation.goals),
        goalsets_used=request.goalsets_used,
        default_skipped=request.default_skipped,
        skip_reason=request.skip_reason,
        paired_project_base_sha=None if paired is None else paired.head_sha(),
    )
    return _EntryPlan(record, goal_criterion_params(translation.goals), tuple(warnings))


def _drive_entry(
    store: GoalStore,
    record: GoalRecord,
    plan: _EntryPlan,
    env: EntryEnvironment,
    checkout: GoalCheckout,
) -> GoalRecord:
    """Steps 4-9: branch, checkout, protected inputs, pins, state, activation."""
    checkout.create_branch(record.branch, record.base_sha)
    record = _save(store, record, branch_created=True)
    _boundary(env, "branch_created")
    _checkout_goal_branch(checkout, record)
    _boundary(env, "checked_out")
    roots = ProtectedInputRoots(checkout.root, store.project_dir)
    snapshot = _snapshot(roots)
    record = _save(
        store,
        record,
        protected_paths=snapshot.encoded_paths,
        protected_digest=snapshot.working_digest,
        protected_head_digest=snapshot.head_digest,
    )
    _boundary(env, "protected_saved")
    params = {key: dict(value) for key, value in plan.criterion_params.items()}
    _pin_baselines(record, params, env, checkout)
    _boundary(env, "pinned")
    _write_initial_state(store, record, params, checkout.root)
    _boundary(env, "state_saved")
    _revalidate_before_activation(checkout, record, roots)
    record = _save(store, record, state=GoalState.ACTIVE)
    _boundary(env, "active")
    return record


def _save(store: GoalStore, record: GoalRecord, **changes: Any) -> GoalRecord:
    """Save *record* with *changes* under its record lock and return the saved revision."""
    with store.record_lock(record.id) as lock:
        return store.save(lock, replace(record, **changes))


def _boundary(env: EntryEnvironment, name: str) -> None:
    if env.on_boundary is not None:
        env.on_boundary(name)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _require_linked_worktree_root(work_dir: Path) -> Path:
    """The absolute worktree root *work_dir* names; the main checkout is refused (D3)."""
    if not work_dir.is_absolute():
        raise GoalEntryError(f"work_dir {str(work_dir)!r} must be an absolute path")
    root = work_dir.absolute()
    if not root.is_dir():
        raise GoalEntryError(
            f"work_dir {root} does not exist; create it with `booley worktree new <name>`"
        )
    if not is_linked_worktree(root):
        raise GoalEntryError(
            f"work_dir {root} is not the root of a linked worktree. Goal Mode never runs in "
            "the main checkout; create a worktree with `booley worktree new <name>`"
        )
    return root


def _refuse_worktree_copy(project_dir: Path, work_dir: Path) -> None:
    """Refuse a control Project directory inside the worktree: that is its copy (D9)."""
    try:
        Path(os.path.realpath(project_dir)).relative_to(os.path.realpath(work_dir))
    except ValueError:
        return
    raise GoalEntryError(
        f"the Project directory {project_dir} is the worktree's own copy; Goal Records "
        "must live in the Project directory of the main checkout"
    )


def _translate(goals: Sequence[GoalArg]) -> Translation:
    try:
        return translate_goals(goals)
    except GoalTranslationError as exc:
        raise GoalEntryError(str(exc)) from None


def _recover_occupant(
    store: GoalStore, identity: WorktreeIdentity, checkout: GoalCheckout
) -> GoalRecord | None:
    """Roll back an ``entering`` occupant; refuse an ``active`` or ``finishing`` one (A2)."""
    occupant = store.active_for_identity(identity)
    if occupant is None:
        return None
    if occupant.state is GoalState.ENTERING:
        # This entry holds the worktree lock, so no entry is still running it.
        return rollback_entering(store, occupant.id, checkout, "entry was interrupted")
    message = (
        f"worktree {checkout.root} already hosts Goal Mode {occupant.id} "
        f"({occupant.state.value}, Goal Branch {occupant.branch})"
    )
    if checkout.head_ref() != branch_ref(occupant.branch):
        message += (
            "; HEAD is not on its Goal Branch. If this worktree was removed and created "
            "again, end the old Goal Mode with `booley goal abandon` in this worktree"
        )
    raise GoalEntryError(message)


def _check_default_goalset(request: EntryRequest, project_dir: Path) -> None:
    """The default Goalset is applied or explicitly skipped with a reason (D10)."""
    exists = (project_dir / GOALSETS_DIR / f"{DEFAULT_GOALSET}.md").is_file()
    used = DEFAULT_GOALSET in request.goalsets_used
    reason = (request.skip_reason or "").strip()
    if request.default_skipped:
        if not exists:
            raise GoalEntryError("default_skipped is true but the Project has no default Goalset")
        if used:
            raise GoalEntryError("the default Goalset cannot be both used and skipped")
        if not reason:
            raise GoalEntryError("skipping the default Goalset needs the human's skip_reason")
        return
    if request.skip_reason is not None:
        raise GoalEntryError("skip_reason is only accepted with default_skipped true")
    if exists and not used:
        raise GoalEntryError(
            "the Project has a default Goalset: apply it (list 'default' in goalsets_used "
            "and include its Goals) or skip it with default_skipped and the human's skip_reason"
        )
    if used and not exists:
        raise GoalEntryError(
            "goalsets_used names 'default' but the Project has no default Goalset"
        )


def _check_targets(work_dir: Path, goals: Sequence[GoalSpec]) -> list[str]:
    """Warn on candidate Targets that do not exist; refuse missing baselines and specs."""
    try:
        catalog = TargetCatalog.build(work_dir)
    except FuseSocError as exc:
        raise GoalEntryError(f"cannot list the Targets of {work_dir}: {exc}") from exc
    warnings: list[str] = []
    for goal in goals:
        if goal.target is not None and not _target_exists(catalog, goal.target):
            warnings.append(
                f"Goal {goal.key} names Target {goal.target!r}, which does not exist yet; "
                "it stays unmet until the Target exists"
            )
        baseline = _baseline_target(goal)
        if baseline is not None and not _target_exists(catalog, baseline):
            raise GoalEntryError(
                f"Goal {goal.key} compares against baseline Target {baseline!r}, which does "
                "not exist at the base commit"
            )
        spec = goal.params.get("spec")
        if isinstance(spec, str) and not (work_dir / spec).is_file():
            raise GoalEntryError(f"Goal {goal.key} names spec file {spec!r}, which does not exist")
    return warnings


def _target_exists(catalog: TargetCatalog, name: str) -> bool:
    try:
        catalog.select(name)
    except UnknownTargetError:
        return False
    except FuseSocError as exc:  # ambiguous, incompatible, or unreadable: not a warning
        raise GoalEntryError(f"Target {name!r} cannot be selected: {exc}") from exc
    return True


def _baseline_target(goal: GoalSpec) -> str | None:
    """The baseline Target a relative Goal compares against, or ``None``."""
    if goal.family not in _BASELINE_FAMILIES or not has_relative_threshold(goal.params):
        return None
    baseline = goal.params.get(BASELINE_TARGET_PARAM, goal.target)
    return baseline if isinstance(baseline, str) else None


def _paired_project_checkout(work_dir: Path) -> GoalCheckout | None:
    """The worktree's paired Project repository checkout, if it has one (A3)."""
    try:
        repository = paired_project_repository(work_dir)
    except RepositoryCheckoutError as exc:
        raise GoalEntryError(str(exc)) from exc
    return None if repository is None else GoalCheckout(repository.worktree)


def _require_clean(checkout: GoalCheckout, what: str) -> None:
    """Refuse uncommitted changes: Targets would resolve from files the base lacks."""
    dirty = checkout.dirty_paths()
    if dirty:
        shown = ", ".join(dirty[:5]) + (" ..." if len(dirty) > 5 else "")
        raise GoalEntryError(f"{what} has uncommitted changes ({shown}); commit or stash them")


def _require_head(checkout: GoalCheckout, ref: str | None, sha: str, when: str) -> None:
    """Refuse unless HEAD is on *ref* (``None``: detached) at commit *sha*."""
    head_ref, head_sha = checkout.head_ref(), checkout.head_sha()
    if head_ref != ref or head_sha != sha:
        where = head_ref or f"detached {head_sha[:12]}"
        expected = ref or f"detached {sha[:12]}"
        raise GoalEntryError(f"HEAD is {where} {when}, not {expected} at {sha[:12]}")


def _checkout_goal_branch(checkout: GoalCheckout, record: GoalRecord) -> None:
    """Check out the Goal Branch, refusing if HEAD or the tree moved since validation."""
    original = record.original_ref if branch_name(record.original_ref) else None
    _require_head(checkout, original, record.base_sha, "before checking out the Goal Branch")
    _require_clean(checkout, "the worktree")
    checkout.checkout_branch(record.branch)
    _require_head(
        checkout, branch_ref(record.branch), record.base_sha, "after checking out the Goal Branch"
    )


def _snapshot(roots: ProtectedInputRoots) -> ProtectedSnapshot:
    """Step 6: the protected paths and both digests, as a run would read them now."""
    try:
        return snapshot_protected_inputs(roots)
    except ProtectedInputError as exc:
        raise GoalEntryError(str(exc)) from exc


def _revalidate_before_activation(
    checkout: GoalCheckout, record: GoalRecord, roots: ProtectedInputRoots
) -> None:
    """Step 9: nothing a run would read moved while entry pinned and froze (B7)."""
    _require_head(checkout, branch_ref(record.branch), record.base_sha, "before activation")
    _require_clean(checkout, "the worktree")
    paired = _paired_project_checkout(checkout.root)
    if record.paired_project_base_sha is not None:
        if paired is None or paired.head_sha() != record.paired_project_base_sha:
            raise GoalEntryError("the paired Project checkout moved while entering Goal Mode")
        _require_clean(paired, "the paired Project checkout")
    assert record.protected_digest is not None  # saved at step 6
    try:
        violations = protected_input_violations(
            record.protected_paths, record.protected_digest, record.protected_head_digest, roots
        )
    except ProtectedInputError as exc:
        raise GoalEntryError(str(exc)) from exc
    if violations:
        raise GoalEntryError(f"{'; '.join(violations)} while entering Goal Mode")


@dataclass(frozen=True)
class _GoalPinContext:
    """The :class:`~booley.flows.baseline_pins.PinContext` of one Goal Mode."""

    work_dir: Path
    base_sha: str
    recipe_freeze_root: Path


def _pin_baselines(
    record: GoalRecord,
    params: dict[str, dict[str, Any]],
    env: EntryEnvironment,
    checkout: GoalCheckout,
) -> None:
    """Pin ``_baseline_ref`` to the base commit and freeze recipes (Step 0 R6)."""
    paths = record_paths(env.project_dir, record.id)
    ctx = _GoalPinContext(checkout.root, record.base_sha, paths.runtime_dir / "recipe-freeze")
    paired = (
        None
        if record.paired_project_base_sha is None
        else PairedProjectBaseline.entry_pinned(record.paired_project_base_sha)
    )
    expanded = dict.fromkeys(params, True)
    try:
        pin_cycle_count_baselines(ctx, params, wording=GOAL_PIN_WORDING)
        for family in env.recipe_families:
            freeze_recipe_family(
                ctx,
                expanded,
                params,
                prefix=family.prefix,
                flow_label=family.flow_label,
                snapshot_builder=family.snapshot_builder,
                wording=GOAL_PIN_WORDING,
                paired_project=paired,
            )
    except BaselinePinError as exc:
        raise GoalEntryError(str(exc)) from exc


def _write_initial_state(
    store: GoalStore, record: GoalRecord, params: dict[str, dict[str, Any]], work_dir: Path
) -> None:
    """Write ``booley_state.json``: every Goal mandatory, unmet, strict (D4).

    ``DevelopmentState.save`` replaces the file without flushing it, so the
    file and its directory are flushed here: a record promoted to ``active``
    never points at a state file a crash could lose.
    """
    categories = review_categories(params)
    state_file = record_paths(store.project_dir, record.id).state_file
    with store.record_lock(record.id):
        state = DevelopmentState.load(state_file)
        state.slug = record.id
        state.work_dir = str(work_dir)
        state.init_criteria(
            dict.fromkeys(params, True),
            category_overrides=categories,
            criterion_params=params,
            strict=True,
        )
        state.save()
        _fsync_file(state_file)
        fsync_directory(state_file.parent)


def _fsync_file(path: Path) -> None:
    # Opened for update: Windows flushes only a handle with write access.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _session_warnings(env: EntryEnvironment, identity: WorktreeIdentity) -> list[str]:
    others = list(env.other_sessions(identity))
    if not others:
        return []
    return [
        f"other sessions are working in this worktree ({', '.join(others)}); they share this "
        "Goal Mode"
    ]


# ---------------------------------------------------------------------------
# Rollback (D15)
# ---------------------------------------------------------------------------


def rollback_entering(
    store: GoalStore, goal_id: str, checkout: GoalCheckout, reason: str
) -> GoalRecord:
    """Undo an ``entering`` record's Git effects and mark it ``failed``.

    The caller holds the worktree lock. Git is touched only where entry's own
    effect is still exactly in place; anything else is left and named in the
    record's ``failure``. A record that is no longer ``entering`` is returned
    unchanged.
    """
    record = store.load(goal_id)
    if record.state is not GoalState.ENTERING:
        return record
    notes = [reason, *_undo_goal_branch(checkout, record)]
    return _save(
        store, record, state=GoalState.FAILED, failure="; ".join(notes), ended_at=utc_now_rfc3339()
    )


def _undo_goal_branch(checkout: GoalCheckout, record: GoalRecord) -> list[str]:
    """Return HEAD to the original ref and delete the Goal Branch where that is safe."""
    branch, base = record.branch, record.base_sha
    tip = checkout.branch_tip(branch)
    if checkout.head_ref() == branch_ref(branch):
        if tip != base or checkout.dirty_paths():
            return [
                f"HEAD stays on Goal Branch {branch}: it has commits or uncommitted changes "
                "since entry; the branch is left in place"
            ]
        original = branch_name(record.original_ref)
        original_tip = base if original is None else checkout.branch_tip(original)
        if original_tip != base:
            moved = "was deleted" if original_tip is None else f"moved to {original_tip[:12]}"
            return [
                f"HEAD stays on Goal Branch {branch}: the original branch {original} {moved} "
                f"since entry (base {base[:12]}); both are left in place"
            ]
        try:
            checkout.checkout_ref(record.original_ref)
        except CheckoutError as exc:
            return [
                f"could not return to {_describe_ref(record.original_ref)} ({exc}); "
                f"HEAD stays on Goal Branch {branch}"
            ]
    return [] if tip is None else _delete_goal_branch(checkout, record, tip)


def _delete_goal_branch(checkout: GoalCheckout, record: GoalRecord, tip: str) -> list[str]:
    """Delete the Goal Branch only when entry created it and it still points at the base."""
    branch, base = record.branch, record.base_sha
    if not record.branch_created:
        ambiguous = (
            " (it points at the base commit, so entry may have created it)" if tip == base else ""
        )
        return [
            f"branch {branch} exists but entry did not record creating it{ambiguous}; "
            "left in place"
        ]
    if tip != base:
        return [f"Goal Branch {branch} has moved since entry; left in place"]
    if not checkout.delete_branch_if_at(branch, base):
        return [f"Goal Branch {branch} could not be deleted; left in place"]
    return []


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _describe_ref(ref: str) -> str:
    branch = branch_name(ref)
    return f"detached {ref[:12]}" if branch is None else f"branch {branch}"


def _describe_goal(goal: GoalSpec) -> str:
    subject = goal.target if goal.target is not None else "review"
    origins = ", ".join(goal.origins)
    return f"{goal.key}: {goal.family.value} {subject} (from {origins})"
