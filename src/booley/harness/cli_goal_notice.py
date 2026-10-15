"""Read-only Goal recording advisory for normalized human CLI selections."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from subprocess import TimeoutExpired

from booley.core.booley_layout import BooleyLayoutError, git_common_directory
from booley.core.checkout_role import SourceCheckoutProjectError
from booley.goals.model import GoalRecord
from booley.goals.store import (
    GoalRecordCorruptError,
    GoalStore,
    GoalStoreError,
    resolve_worktree_identity,
)
from booley.runtime.booley_dir import resolve_authoritative_booley_dir, resolve_booley_dir
from booley.runtime.directory_migration_git import primary_checkout


def warn_cli_goal_recording(work_dir: Path) -> None:
    """Disclose an occupying Goal without binding, locking, or creating identity."""
    try:
        # Ordinary non-Git Projects have no Worktree Identity or Goal authority.
        root = next((p for p in (work_dir, *work_dir.parents) if (p / ".git").exists()), None)
        if root is None or git_common_directory(root) is None:
            return
        stores = {resolve_authoritative_booley_dir(primary_checkout(root)).resolve()}
        with contextlib.suppress(FileNotFoundError, SourceCheckoutProjectError):
            stores.add(resolve_booley_dir(cache_result=False).resolve())
        records = []
        failures = []
        for directory in sorted(stores):
            store = GoalStore(directory)
            try:
                record = _selected_record(store, root)
            except (BooleyLayoutError, GoalStoreError, OSError) as exc:
                failures.append(f"{directory}: {exc}")
                continue
            if record is not None:
                records.append(record)
        if len(records) > 1:
            raise GoalStoreError("several Booley Directories claim the selected worktree")
        if failures and not records:
            raise GoalStoreError("; ".join(failures))
        if records:
            uncertainty = (
                f" Other Goal store advisory unavailable: {'; '.join(failures)}"
                if failures
                else ""
            )
            print(
                f"WARN: not recorded for active Goal {records[0].id}: Goal evidence comes "
                f"only from the Developer Agent's MCP calls with work_dir.{uncertainty}",
                file=sys.stderr,
            )
    except (
        BooleyLayoutError,
        GoalStoreError,
        OSError,
        SourceCheckoutProjectError,
        TimeoutExpired,
    ) as exc:
        print(f"WARN: Goal recording advisory unavailable for {work_dir}: {exc}", file=sys.stderr)


def _selected_record(store: GoalStore, root: Path) -> GoalRecord | None:
    """Keep missing-identity diagnostics scoped to the occupying checkout."""
    identity = resolve_worktree_identity(root, create=False)
    if identity is not None:
        return store.active_for_identity(identity)
    scan = store.list_active()
    if scan.corrupt:
        raise GoalRecordCorruptError(scan.corrupt)
    if any(
        root.resolve().is_relative_to(Path(item.worktree_path).resolve()) for item in scan.records
    ):
        return store.active_for_worktree(root)
    return None
