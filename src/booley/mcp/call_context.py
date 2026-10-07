"""Per-call facts the MCP server resolves once for each tool call.

A tool call runs against one checkout, records its jobs under one root, and
reads one state file. Before this module the server answered each of those
questions with its own read of ``arguments`` or ``os.environ`` at the point of
use. :func:`resolve_call_context` is now the one place that decides them, so a
later resolver can retarget a whole call (jobs, state, logs, and the endpoint
subprocess environment) without touching every reader.

Outside Goal preview every value equals the scattered read it replaced: the explicit
``work_dir`` or the server's cwd, the container-wide jobs root, and the server
process's own ``BOOLEY_*`` variables. ``subprocess_env_overrides`` is empty.
With preview enabled, an explicit worktree selects its active Goal Record and
freezes one Run Binding. All evidence paths and subprocess overrides follow that
record; an omitted worktree is refused while any occupying record exists.
``ticket_file`` feeds :func:`~booley.mcp.flow_execution_selection.select_flow_execution`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from booley.goals.binding import GoalBindingError, GoalRunBinding, bind_run
from booley.goals.paths import record_paths
from booley.goals.preview import goal_mode_preview_enabled
from booley.goals.protected_inputs import ProtectedInputRoots
from booley.goals.session_key import session_key
from booley.goals.store import GoalRecordCorruptError, GoalStore
from booley.mcp.flow_execution_selection import configured_ticket_file
from booley.runtime.job_records import make_run_id
from booley.runtime.project_dir import resolve_project_dir
from booley.runtime.session_paths import session_jobs_dir
from booley.runtime.timefmt import compact_utc_now


@dataclass(frozen=True, slots=True)
class CallContext:
    """Facts one MCP tool call runs under, resolved when the call arrives.

    ``explicit_work_dir`` is the call's ``work_dir`` argument, or ``None`` when
    omitted; :attr:`work_dir` applies the cwd default only when read, so a call
    that never needs its checkout never touches the cwd. ``jobs_root``,
    ``state_path``, ``logs_dir``, and ``runtime_dir`` are ``None`` when the
    server environment leaves them unset, exactly as the reads they replaced
    returned nothing. ``runtime_dir`` is the explicit ``BOOLEY_RUNTIME_DIR``
    value, not the logs-derived fallback. ``ticket_file`` is the server's
    ``BOOLEY_TICKET_FILE``, or ``None`` when unset or empty.
    """

    explicit_work_dir: Path | None
    jobs_root: Path | None
    state_path: Path | None
    logs_dir: Path | None
    runtime_dir: Path | None
    subprocess_env_overrides: Mapping[str, str]
    ticket_file: Path | None
    binding: GoalRunBinding | None = None
    session_key: str | None = None

    @property
    def work_dir(self) -> Path:
        """Checkout the call targets: the explicit ``work_dir``, else the cwd now."""
        if self.explicit_work_dir is not None:
            return self.explicit_work_dir
        return Path.cwd()


def container_jobs_root() -> Path | None:
    """Container-wide job-record root configured in the server environment."""
    return session_jobs_dir()


def _explicit_work_dir(arguments: Mapping[str, Any]) -> Path | None:
    """The call's ``work_dir`` argument as a path, or ``None`` when omitted or empty."""
    work_dir = arguments.get("work_dir")
    return Path(str(work_dir)) if work_dir else None


def resolve_work_dir(arguments: Mapping[str, Any]) -> Path:
    """Checkout a call targets: its ``work_dir`` argument, else the server cwd.

    Validation stays with the caller (``_validate_work_dir``); this only
    applies the default.
    """
    explicit = _explicit_work_dir(arguments)
    return explicit if explicit is not None else Path.cwd()


def _env_path(name: str) -> Path | None:
    """Path held by environment variable *name*, or ``None`` when unset or empty."""
    value = os.environ.get(name)
    return Path(value) if value else None


def resolve_call_context(arguments: Mapping[str, Any]) -> CallContext:
    """Resolve the facts one tool call with *arguments* runs under."""
    explicit = _explicit_work_dir(arguments)
    if goal_mode_preview_enabled():
        goal = _goal_call_context(arguments, explicit)
        if goal is not None:
            return goal
    return CallContext(
        explicit_work_dir=explicit,
        jobs_root=container_jobs_root(),
        state_path=_env_path("BOOLEY_STATE_FILE"),
        logs_dir=_env_path("BOOLEY_LOGS_DIR"),
        runtime_dir=_env_path("BOOLEY_RUNTIME_DIR"),
        subprocess_env_overrides=MappingProxyType({}),
        ticket_file=configured_ticket_file(),
    )


def require_goal_work_dir(arguments: Mapping[str, Any], store: GoalStore) -> None:
    """Refuse default routing while an occupying or unreadable record exists."""
    if _explicit_work_dir(arguments) is not None:
        return
    scan = store.list_active()
    if scan.corrupt:
        raise GoalRecordCorruptError(scan.corrupt)
    if scan.records:
        paths = ", ".join(record.worktree_path for record in scan.records)
        raise GoalBindingError(
            f"Pass work_dir on every Booley call while Goal Mode is active: {paths}"
        )


def _goal_call_context(arguments: Mapping[str, Any], explicit: Path | None) -> CallContext | None:
    store = GoalStore(resolve_project_dir())
    require_goal_work_dir(arguments, store)
    if explicit is None:
        return None
    record = store.active_for_worktree(explicit)
    if record is None:
        return None
    invocation = make_run_id("goal", compact_utc_now(), 0)
    binding = bind_run(store, explicit, invocation)
    paths = record_paths(store.project_dir, binding.record_id)
    overrides = {
        "BOOLEY_GOAL_FILE": str(paths.record_file),
        "BOOLEY_GOAL_RUN_BINDING": json.dumps(binding.to_json()),
        "BOOLEY_STATE_FILE": str(paths.state_file),
        "BOOLEY_LOGS_DIR": str(paths.logs_dir),
        "BOOLEY_RUNTIME_DIR": str(paths.runtime_dir),
        "BOOLEY_CONTROL_PROJECT_ROOT": os.environ.get("BOOLEY_CONTROL_PROJECT_ROOT")
        or str(
            ProtectedInputRoots(binding.worktree_root, store.project_dir).main_checkout()
            or binding.worktree_root
        ),
        "BOOLEY_SLUG": binding.record_id,
    }
    return CallContext(
        explicit,
        paths.jobs_dir,
        paths.state_file,
        paths.logs_dir,
        paths.runtime_dir,
        MappingProxyType(overrides),
        None,
        binding,
        session_key(store, explicit),
    )


def binding_from_environment() -> GoalRunBinding | None:
    """Deserialize admission facts in an endpoint; never bind again at execution."""
    raw = os.environ.get("BOOLEY_GOAL_RUN_BINDING")
    if not goal_mode_preview_enabled() or not raw:
        return None
    try:
        return GoalRunBinding.from_json(json.loads(raw))
    except ValueError as exc:
        raise GoalBindingError(f"invalid Goal run binding environment: {exc}") from exc
