"""Namespace-aware Goal presence warnings; observational and non-authoritative."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from booley.config.goals import quiet_after
from booley.goals.model import GoalRecord
from booley.goals.store import GoalStore
from booley.harness.doctor_waivers import DoctorWarning, warning
from booley.mcp.session_registry import SessionRegistry, SessionRow, namespace
from booley.runtime.pid import ProcessState, observe_process
from booley.runtime.timefmt import parse_timestamp
from booley.runtime.worktrees import WorktreeEntry, list_worktrees


@dataclass(frozen=True)
class PresenceDiagnostics:
    """Stable structured warnings and honest namespace/storage availability."""

    warnings: tuple[DoctorWarning, ...] = ()
    unavailable: str = ""


def worktree_presence(
    store: GoalStore, record: GoalRecord, entries: tuple[WorktreeEntry, ...]
) -> str:
    """Resolve repository/admin identity, accepting aliases and moved checkouts."""
    unreadable = False
    for entry in entries:
        try:
            if entry.path.exists() and store.identify_worktree(entry.path) == record.worktree:
                return "present"
        except (OSError, RuntimeError, ValueError):
            unreadable = True
    if unreadable:
        return "unreadable"
    return "missing"


def inspect_presence(
    root: Path,
    project_dir: Path,
    *,
    inside_sandbox: bool,
    now: float | None = None,
    proc_root: Path = Path("/proc"),
) -> PresenceDiagnostics:
    """Never start a Sandbox or inspect its PIDs/paths from the host namespace."""
    if not inside_sandbox:
        return PresenceDiagnostics(
            unavailable="Goal session checks unavailable on the host; run Doctor inside the existing Sandbox"
        )
    scope = namespace(proc_root)
    if not scope:
        return PresenceDiagnostics(
            unavailable="Goal session checks unavailable: PID namespace unreadable"
        )
    try:
        threshold = quiet_after(project_dir)
        entries = list_worktrees(root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return PresenceDiagnostics(unavailable=f"Goal worktree/session checks unavailable: {exc}")
    store = GoalStore(project_dir)
    sessions = SessionRegistry(project_dir, proc_root=proc_root).snapshot()
    warnings = tuple(
        finding
        for record in store.list_active().records
        for finding in _record_warnings(
            store, record, entries, sessions.rows, scope, threshold, now, proc_root
        )
    )
    return PresenceDiagnostics(warnings, "; ".join(sessions.diagnostics))


def _record_warnings(
    store: GoalStore,
    record: GoalRecord,
    entries: tuple[WorktreeEntry, ...],
    rows: tuple[SessionRow, ...],
    scope: str,
    threshold: float,
    now: float | None,
    proc_root: Path,
) -> list[DoctorWarning]:
    warnings = []
    presence = worktree_presence(store, record, entries)
    if presence != "present":
        warnings.append(
            warning(
                "goals.worktree-missing",
                f"{record.id}: Goal worktree {presence}",
                subject=record.id,
            )
        )
    matching = [
        row
        for row in rows
        if row.attribution.worktree_key == record.worktree.key
        and row.attribution.namespace == scope
    ]
    fresh = any(
        (now if now is not None else time.time()) - parse_timestamp(row.last_call_at).timestamp()
        < threshold
        and (
            row.attribution.kind != "process"
            or row.attribution.process is None
            or observe_process(row.attribution.process, proc_root=proc_root).state
            in {ProcessState.RUNNING, ProcessState.UNKNOWN}
        )
        for row in matching
    )
    if not fresh:
        warnings.append(
            warning(
                "goals.quiet-session",
                f"{record.id}: no recent attributable Booley call; client activity and liveness are unavailable, not evidence of abandonment",
                subject=record.id,
            )
        )
    return warnings
