"""Deferred resolution: freeze a Goal's Test Runs once they can be resolved (ADR 0072).

A Goal may name a Target or a Test that does not exist yet (ADR 0067), a
Test may be added later to the Target of a Target Goal, and a Goal Record an
older Booley created has no frozen Test Runs at all. Each of those slots is
pending until :func:`ensure_test_run_resolutions` fills it, when a run is
bound and when Goal status is read.

A resolution is **one atomic save** of ``record.json`` under the record lock.
The saved record already holds the exact resolved Test Runs and drawn seeds,
so a crash leaves either the old record (nothing was observable; the next
call draws again) or the new one. The Change Log is not involved: its entries
are approved Goal changes, which recovery and the apply barrier act on.
While an apply is interrupted nothing is resolved, so a resolution never
saves over one. A resolver whose record another writer replaced re-reads it
and tries again; what the other resolver froze stays frozen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from booley.flows.test_runs import draw_seeds
from booley.goals.apply_barrier import pending_applies
from booley.goals.model import GoalRecord, GoalState
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore, StaleRevisionError
from booley.goals.test_run_freezing import ResolutionRequest, SeedDraw, freeze_goals
from booley.goals.test_run_plans import TargetPlans
from booley.goals.test_run_resolution import DEFERRED
from booley.runtime.timefmt import utc_now_rfc3339

# A resolver loses a save only to another writer of the same record; each
# retry re-reads what that writer saved, so a few attempts always suffice.
_SAVE_ATTEMPTS = 3


def ensure_test_run_resolutions(
    store: GoalStore,
    record_id: str,
    work_dir: Path,
    *,
    draw: SeedDraw | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> GoalRecord:
    """Fill every pending Test Run slot of Goal Record *record_id* that can be filled now.

    *work_dir* is the Goal worktree whose ``tests.toml`` and Targets the
    slots resolve against. Returns the record as it is afterwards; a record
    that is not ``active``, or whose apply is interrupted, is returned
    unchanged. A Goal its Target refuses stays pending. *checkpoint* is
    called with ``"before:save"`` and ``"after:save"`` (tests interrupt
    there).

    Raises :class:`~booley.goals.store.GoalStoreError` when the record cannot
    be read or saved.
    """
    for _attempt in range(_SAVE_ATTEMPTS):
        with store.record_lock(record_id) as lock:
            record = store.load(record_id)
            resolved = _resolved(store, record, work_dir, draw)
            if resolved is None:
                return record
            if checkpoint is not None:
                checkpoint("before:save")
            try:
                saved = store.save(lock, resolved)
            except StaleRevisionError:
                continue
            if checkpoint is not None:
                checkpoint("after:save")
            return saved
    raise StaleRevisionError(
        f"Goal Record {record_id} kept changing while its Test Runs were resolved"
    )


def _resolved(
    store: GoalStore, record: GoalRecord, work_dir: Path, draw: SeedDraw | None
) -> GoalRecord | None:
    """*record* with its pending slots filled, or ``None`` when nothing may or need change."""
    if record.state is not GoalState.ACTIVE:
        return None
    if pending_applies(record_paths(store.project_dir, record.id).root):
        return None
    # Looked up here, not bound as a default: a caller without its own source
    # (a run being bound, a status read) draws from this module's.
    request = ResolutionRequest(DEFERRED, utc_now_rfc3339(), draw or draw_seeds)
    frozen = freeze_goals(
        record.goals, record.test_run_resolutions, TargetPlans(work_dir), request, strict=False
    )
    if frozen == dict(record.test_run_resolutions):
        return None
    return replace(record, test_run_resolutions=frozen)
