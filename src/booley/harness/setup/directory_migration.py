"""Compose the Job inactivity policy with the runtime's filesystem migrator."""

from pathlib import Path

from booley.harness.setup.directory_migration_jobs import observe_jobs
from booley.runtime.booley_dir_migration import Migration
from booley.runtime.booley_dir_migration import inspect_migration as inspect_filesystem
from booley.runtime.layout_consumers import require_quiescent


def observe_directory(directory: Path) -> None:
    """Refuse every live or unprovable Job before writes."""
    require_quiescent(directory.parent)
    observe_jobs(directory)


def inspect_migration(root: Path) -> Migration | None:
    """Observe domain occupancy before the migrator is allowed to plan mutations."""
    from booley.harness.setup.directory_migration_integrations import plan_generated_integrations

    return inspect_filesystem(
        root, observe=observe_directory, integrations=plan_generated_integrations
    )
