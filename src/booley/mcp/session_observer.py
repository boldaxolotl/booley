"""Bounded best-effort presentation adapter for MCP request attribution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from booley.config.goals import quiet_after
from booley.goals.preview import goal_mode_preview_enabled
from booley.mcp.session_peer import observed_peer
from booley.mcp.session_registry import (
    Attribution,
    SessionRegistry,
    namespace,
    registered_worktree_presence,
    resolve_attribution,
    valid_thread_id,
)
from booley.runtime.project_dir import resolve_project_dir

logger = logging.getLogger(__name__)


def _consume_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()  # Retrieve errors even after the caller's advisory timeout.


class SessionObserver:
    """One in-flight advisory operation per server; slow storage cannot queue requests.

    Timeout leaves at most one worker outstanding. Subsequent calls immediately
    degrade until it completes. No routing, result payload or lifecycle depends
    on any registry operation.
    """

    def __init__(self, *, now: Callable[[], float] = time.time, budget: float = 0.05) -> None:
        self.now = now
        self.budget = budget
        self._pending: asyncio.Task[Any] | None = None

    async def _bounded(self, operation: Callable[[], Any]) -> Any:
        if self._pending is not None and not self._pending.done():
            return None
        self._pending = asyncio.create_task(asyncio.to_thread(operation))
        self._pending.add_done_callback(_consume_failure)
        try:
            return await asyncio.wait_for(asyncio.shield(self._pending), self.budget)
        except Exception:
            logger.debug("Session presentation unavailable", exc_info=True)
            return None

    async def attribution(
        self, arguments: Mapping[str, Any], metadata: Mapping[str, Any], client_name: str
    ) -> Attribution | None:
        """Resolve advisory identity once, without changing routing inputs."""
        if not goal_mode_preview_enabled():
            return None
        work_dir = arguments.get("work_dir")
        root = Path(work_dir) if isinstance(work_dir, str) and work_dir else Path.cwd()
        resolved = await self._bounded(
            lambda: resolve_attribution(
                root,
                metadata=metadata,
                client_name=client_name,
                peer=observed_peer(),
            )
        )
        if resolved is not None:
            return resolved
        # Thread metadata remains usable even when advisory storage/procfs is slow.
        path = str(root.absolute())
        thread = metadata.get("threadId")
        valid_thread = valid_thread_id(thread)
        return Attribution(
            "codex:" + str(thread) if valid_thread else "worktree:path:" + path,
            "thread" if valid_thread else "worktree",
            path,
            "path:" + path,
        )

    async def shared(self, facts: Attribution | None) -> tuple[tuple[str, ...], str]:
        """Bounded shared-worktree warning; fallback explicitly cannot distinguish sessions."""
        if facts is None or facts.kind == "worktree":
            return (), "WARNING: shared-worktree session attribution is unavailable"
        snapshot = await self._bounded(
            lambda: self._registry().visible_snapshot(now=self.now(), scope=namespace())
        )
        if snapshot is None or snapshot.diagnostics:
            return (), "WARNING: shared-worktree session registry is unavailable"
        keys = tuple(
            row.attribution.key
            for row in snapshot.rows
            if row.attribution.worktree_key == facts.worktree_key
        )
        warning = (
            "WARNING: another session shares this Goal worktree"
            if any(key != facts.key for key in keys)
            else ""
        )
        return keys, warning

    async def record(self, facts: Attribution | None, tool: str, outcome: str) -> None:
        """Persist bounded metadata only; a failed registry never changes a tool reply."""
        if facts is None:
            return
        await self._bounded(lambda: self._registry().upsert(facts, tool, outcome, now=self.now()))

    def _registry(self) -> SessionRegistry:
        project = resolve_project_dir()
        return SessionRegistry(project, quiet_after=quiet_after(project))

    async def maintain(self) -> None:
        """Prune during MCP silence; owned by the server lifespan, never Dashboard reads."""
        if goal_mode_preview_enabled():
            await self._bounded(
                lambda: self._registry().prune(
                    now=self.now(),
                    namespace=namespace(),
                    worktree_exists=registered_worktree_presence(Path.cwd()),
                )
            )

    async def maintenance_loop(self) -> None:
        """One timed maintenance iteration per minute until lifespan cancellation."""
        while True:  # Lifespan cancellation bounds this service, no retries accumulate.
            await self.maintain()
            await asyncio.sleep(60)


class SessionMaintenanceApp:
    """Own a maintenance task for the HTTP ASGI lifespan, including idle periods."""

    def __init__(self, app: Any, observer: SessionObserver) -> None:
        self.app = app
        self.observer = observer

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return
        task = asyncio.create_task(self.observer.maintenance_loop())
        try:
            await self.app(scope, receive, send)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
