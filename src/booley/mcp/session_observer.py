"""Bounded best-effort presentation adapter for MCP request attribution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.requests import Request

from booley.config.goals import quiet_after
from booley.mcp.session_peer import observed_peer
from booley.mcp.session_registry import (
    Attribution,
    SessionRegistry,
    namespace,
    registered_worktree_presence,
    resolve_attribution,
)
from booley.runtime.project_dir import resolve_project_dir

logger = logging.getLogger(__name__)


def _consume_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()  # Retrieve errors even after the caller's advisory timeout.


MAX_PENDING = 128
MAX_HANDLES = 512


@dataclass
class _Observation:
    fallback: Attribution
    sequence: int
    call: tuple[str, str, float]
    root: Path | None
    client_name: str
    request: Request | None
    done: asyncio.Future[Attribution | None]
    resolved: Attribution | None = None
    superseded: bool = False

    @property
    def key(self) -> str:
        if self.resolved is not None:
            return self.resolved.key
        if self.fallback.kind == "thread" or self.request is None:
            return self.fallback.key
        return "peer:" + str(self.request.scope)


class SessionObserver:
    """One presence writer drains a bounded queue, coalescing the latest call per key.

    Responses wait only for the advisory budget. Colliding callers retain their
    own observation/outcome handles; reads and idle maintenance have independent
    bounded workers. No registry operation supplies routing or lifecycle authority.
    """

    def __init__(self, *, now: Callable[[], float] = time.time, budget: float = 0.05) -> None:
        self.now = now
        self.budget = budget
        self._pending: asyncio.Task[Any] | None = None
        self._maintenance_pending: asyncio.Task[Any] | None = None
        self._read_pending: asyncio.Task[Any] | None = None
        self._queue: OrderedDict[str, _Observation] = OrderedDict()
        self._handles: OrderedDict[int, _Observation] = OrderedDict()
        self._latest: OrderedDict[str, _Observation] = OrderedDict()
        self._sequence = 0
        self._closed = asyncio.Event()

    async def _bounded(
        self, operation: Callable[[], Any], *, maintenance: bool = False, reading: bool = False
    ) -> Any:
        pending = (
            self._maintenance_pending
            if maintenance
            else self._read_pending
            if reading
            else self._pending
        )
        if pending is not None and not pending.done():
            return None
        task = asyncio.create_task(asyncio.to_thread(operation))
        if maintenance:
            self._maintenance_pending = task
        elif reading:
            self._read_pending = task
        else:
            self._pending = task
        task.add_done_callback(_consume_failure)
        return await self._wait(task)

    async def _wait(self, pending: asyncio.Future[Any]) -> Any:
        try:
            return await asyncio.wait_for(asyncio.shield(pending), self.budget)
        except (TimeoutError, OSError, ValueError, RuntimeError):
            logger.debug("Session presentation unavailable", exc_info=True)
            return None

    def _remember(self, facts: Attribution, observation: _Observation) -> None:
        self._handles[id(facts)] = observation
        self._handles.move_to_end(id(facts))
        if len(self._handles) > MAX_HANDLES:
            self._handles.popitem(last=False)
            logger.warning("Session observation handle capacity exceeded")

    def _enqueue(self, observation: _Observation) -> None:
        if observation.superseded or self._closed.is_set():
            return
        key = observation.key
        previous = self._queue.get(key)
        if previous is not None and previous.sequence > observation.sequence:
            observation.superseded = True
            return
        if previous is not None and previous is not observation:
            previous.superseded = True
            if not previous.done.done():
                previous.done.set_result(None)
        if key not in self._queue and len(self._queue) >= MAX_PENDING:
            observation.superseded = True
            if not observation.done.done():
                observation.done.set_result(None)
            logger.warning("Session presence queue capacity exceeded; observation unavailable")
            return
        self._queue[key] = observation
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._drain())
            self._pending.add_done_callback(_consume_failure)

    async def _drain(self) -> None:
        while self._queue and not self._closed.is_set():
            _, observation = self._queue.popitem(last=False)
            if observation.superseded:
                continue
            try:
                facts = await asyncio.to_thread(self._resolve, observation)
                observation.resolved = facts
                self._remember(facts, observation)
                if self._latest_observation(observation):
                    call = observation.call
                    await asyncio.to_thread(self._publish, facts, call)
                    if observation.call != call:
                        self._enqueue(observation)
                if not observation.done.done():
                    observation.done.set_result(facts)
            except (OSError, ValueError, RuntimeError):
                logger.debug("Session presence unavailable", exc_info=True)
                if not observation.done.done():
                    observation.done.set_result(None)

    def _latest_observation(self, observation: _Observation) -> bool:
        key = observation.key
        previous = self._latest.get(key)
        if previous is not None and previous.sequence > observation.sequence:
            observation.superseded = True
            return False
        if previous is not None and previous is not observation:
            previous.superseded = True
        self._latest[key] = observation
        self._latest.move_to_end(key)
        if len(self._latest) > MAX_HANDLES:
            self._latest.popitem(last=False)
        return True

    async def attribution(
        self,
        arguments: Mapping[str, Any],
        metadata: Mapping[str, Any],
        client_name: str,
        *,
        request: Request | None = None,
        tool: str = "request",
    ) -> Attribution | None:
        """Resolve once; queued observations survive the bounded response deadline."""
        work_dir = arguments.get("work_dir")
        root = Path(work_dir) if isinstance(work_dir, str) and work_dir else None
        if root is not None and not root.is_dir():
            root = None
        fallback = resolve_attribution(root, metadata=metadata, client_name=client_name)
        # Retain only transport facts, not the SDK request body or client arguments.
        peer_request = (
            None
            if request is None
            else Request(
                {
                    "type": "http",
                    "server": request.scope.get("server"),
                    "headers": [
                        (k, v)
                        for k, v in request.scope.get("headers", [])
                        if k in {b"x-booley-peer-host", b"x-booley-peer-port"}
                    ],
                }
            )
        )
        self._sequence += 1
        observation = _Observation(
            fallback,
            self._sequence,
            (tool, "started", self.now()),
            root,
            client_name,
            peer_request,
            asyncio.get_running_loop().create_future(),
        )
        self._remember(fallback, observation)
        self._enqueue(observation)
        return await self._wait(observation.done) or observation.resolved or fallback

    def _resolve(self, observation: _Observation) -> Attribution:
        if observation.resolved is not None:
            return observation.resolved
        fallback = observation.fallback
        return self._registry().preserve_worktree(
            resolve_attribution(
                observation.root,
                metadata={"threadId": fallback.key.removeprefix("codex:")}
                if fallback.kind == "thread"
                else {},
                client_name=observation.client_name,
                peer=observed_peer(observation.request),
            )
        )

    def _publish(self, facts: Attribution, call: tuple[str, str, float]) -> None:
        tool, outcome, now = call
        self._registry().upsert(facts, tool, outcome, now=now)

    async def resolved(
        self, facts: Attribution | None, *, budget: float = 0.5
    ) -> Attribution | None:
        """Separate bounded admission wait for completed advisory identity, never authority."""
        observation = None if facts is None else self._handles.get(id(facts))
        if observation is None:
            return facts
        if observation.resolved is not None:
            return observation.resolved
        try:
            return (
                await asyncio.wait_for(asyncio.shield(observation.done), budget)
                or observation.resolved
                or facts
            )
        except (TimeoutError, OSError, ValueError, RuntimeError):
            return observation.resolved or facts

    async def shared(self, facts: Attribution | None) -> tuple[tuple[str, ...], str]:
        """Bounded shared-worktree warning; fallback explicitly cannot distinguish sessions."""
        observation = None if facts is None else self._handles.get(id(facts))
        if observation is not None:
            facts = observation.resolved or await self._wait(observation.done) or facts
        if facts is None or facts.kind == "worktree" or not facts.worktree_key:
            return (), "WARNING: shared-worktree session attribution is unavailable"
        snapshot = await self._bounded(
            lambda: self._registry().visible_snapshot(now=self.now(), scope=namespace()),
            reading=True,
        )
        if snapshot is None or snapshot.unavailable:
            if snapshot is not None and any(
                "registry partially unavailable" in item for item in snapshot.diagnostics
            ):
                return (), "WARNING: shared-worktree session registry partially unavailable"
            return (), "WARNING: shared-worktree session registry is unavailable"
        keys = tuple(
            row.attribution.key
            for row in snapshot.rows
            if row.attribution.worktree_key == facts.worktree_key
            and row.attribution.kind != "worktree"
        )
        warning = (
            "WARNING: another session shares this Goal worktree"
            if any(key != facts.key for key in keys)
            else ""
        )
        return keys, warning

    async def record(self, facts: Attribution | None, tool: str, outcome: str) -> None:
        """Carry the outcome on its request handle, never an unresolved fallback row."""
        if facts is None:
            return
        observation = self._handles.get(id(facts))
        if observation is not None:
            observation.call = tool, outcome, self.now()
            if observation.resolved is not None:
                self._enqueue(observation)
                if self._pending is not None:
                    await self._wait(self._pending)
            return
        if facts.kind != "worktree":
            await self._bounded(
                lambda: self._registry().upsert(facts, tool, outcome, now=self.now())
            )

    def _registry(self) -> SessionRegistry:
        project = resolve_project_dir()
        return SessionRegistry(project, quiet_after=quiet_after(project))

    async def maintain(self) -> None:
        """Prune during MCP silence; owned by the server lifespan, never Dashboard reads."""
        await self._bounded(
            lambda: self._registry().prune(
                now=self.now(),
                namespace=namespace(),
                worktree_exists=registered_worktree_presence(Path.cwd()),
            ),
            maintenance=True,
        )

    async def maintenance_loop(self) -> None:
        """One timed maintenance iteration per minute until lifespan cancellation."""
        while not self._closed.is_set():
            await self.maintain()
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=60)
            except TimeoutError:
                continue

    def close(self) -> None:
        """Signal the maintenance lifespan to end."""
        self._closed.set()


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
            self.observer.close()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
