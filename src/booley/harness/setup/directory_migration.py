"""Compose Goal/session inactivity policy with the runtime's filesystem migrator."""

from pathlib import Path

from booley.core.booley_layout import BooleyLayoutError
from booley.goals.store import GoalStore
from booley.harness.setup.directory_migration_jobs import observe_jobs
from booley.mcp.session_registry import DEAD_PROCESS_STATES, SessionRegistry
from booley.runtime.booley_dir_migration import Migration
from booley.runtime.booley_dir_migration import inspect_migration as inspect_filesystem
from booley.runtime.layout_consumers import require_quiescent
from booley.runtime.pid import observe_process


def observe_directory(directory: Path) -> None:
    """Refuse every occupying/corrupt record and live/unprovable session before writes."""
    scan = GoalStore(directory).list_active()
    if scan.corrupt:
        raise BooleyLayoutError(
            "cannot prove Goal inactivity: "
            + "; ".join(f"{entry.path}: {entry.reason}" for entry in scan.corrupt)
        )
    if scan.records:
        raise BooleyLayoutError(
            "migration refused; occupying Goal Records: "
            + "; ".join(
                f"{record.id} ({record.worktree_path}, {record.state.value})"
                for record in scan.records
            )
        )
    require_quiescent(directory.parent)
    observe_jobs(directory)
    if not (directory / "runtime" / "sessions").exists():
        return
    snapshot = SessionRegistry(directory).snapshot()
    if snapshot.unavailable:
        raise BooleyLayoutError(
            "cannot prove session inactivity: " + "; ".join(snapshot.diagnostics)
        )
    for row in snapshot.rows:
        process = row.attribution.process
        if process is None or observe_process(process).state not in DEAD_PROCESS_STATES:
            raise BooleyLayoutError(
                f"stop live/unknown Booley consumer {row.attribution.key} before migration"
            )


def inspect_migration(root: Path) -> Migration | None:
    """Observe domain occupancy before the migrator is allowed to plan mutations."""
    from booley.harness.setup.directory_migration_integrations import plan_generated_integrations

    return inspect_filesystem(
        root, observe=observe_directory, integrations=plan_generated_integrations
    )
