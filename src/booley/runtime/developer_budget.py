"""Pausable Developer Agent active-time and wall-clock budgets."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from booley.config.settings import DeveloperLimitsConfig

from .agent_errors import AgentTimeoutError
from .timefmt import utc_now_rfc3339 as now_iso

BudgetKind = Literal["active", "wall"]
T = TypeVar("T")

_PERSIST_INTERVAL_SECONDS = 10.0
_TICK_SECONDS = 1.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeveloperBudgetSnapshot:
    """One live reading of both Developer Agent budgets."""

    wall_elapsed_seconds: float
    active_elapsed_seconds: float
    wall_limit_seconds: int
    active_limit_seconds: int
    paused: bool
    pause_reason: str


class DeveloperBudget:
    """Count wall time continuously and active time outside Booley MCP calls."""

    def __init__(
        self,
        limits: DeveloperLimitsConfig,
        *,
        persist_path: Path,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._persist_path = persist_path
        self._on_event = on_event
        self._clock = clock
        self._wall_started_at: float | None = None
        self._active_started_at: float | None = None
        self._active_elapsed = 0.0
        self._pauses: dict[str, str] = {}
        self._run_index = 0
        self._running = False
        self._active_finished = False
        self._last_persist_at = float("-inf")
        self._changed = asyncio.Event()

    def start(self, run_index: int) -> None:
        """Start both clocks for one ticket execution run."""
        if self._running:
            raise RuntimeError("Developer budget already started")
        now = self._clock()
        self._wall_started_at = now
        self._active_started_at = now
        self._run_index = run_index
        self._running = True
        self._active_finished = False
        self._publish(force_persist=True)

    def set_on_event(self, on_event: Callable[[dict[str, Any]], None] | None) -> None:
        """Attach the live display callback once the Console watcher exists."""
        self._on_event = on_event
        if self._running:
            self._publish(force_persist=True)

    def stop_active(self) -> DeveloperBudgetSnapshot:
        """Stop active accounting while leaving the wall-clock budget live."""
        if self._active_finished:
            return self.snapshot()
        self._accrue_active()
        self._active_started_at = None
        self._active_finished = True
        self._pauses.clear()
        self._changed.set()
        return self._publish(force_persist=True)

    def finish(self) -> DeveloperBudgetSnapshot:
        """Stop active accounting and persist the terminal snapshot."""
        self.stop_active()
        self._running = False
        self._changed.set()
        return self._publish(force_persist=True)

    def pause(self, key: str, reason: str) -> None:
        """Pause active time while one synchronous excluded wait is open."""
        if not self._running or self._active_finished or key in self._pauses:
            return
        if not self._pauses:
            self._accrue_active()
            self._active_started_at = None
        self._pauses[key] = reason
        self._changed.set()
        self._publish(force_persist=True)

    def resume(self, key: str) -> None:
        """Close one excluded wait and resume when no other waits remain."""
        if key not in self._pauses:
            return
        self._pauses.pop(key)
        if self._running and not self._active_finished and not self._pauses:
            self._active_started_at = self._clock()
        self._changed.set()
        self._publish(force_persist=True)

    def resume_prefix(self, prefix: str) -> None:
        """Close every excluded wait whose key belongs to one backend attempt."""
        keys = [key for key in self._pauses if key.startswith(prefix)]
        for key in keys:
            self.resume(key)

    def snapshot(self) -> DeveloperBudgetSnapshot:
        """Return a side-effect-free reading of both clocks."""
        now = self._clock()
        wall = 0.0 if self._wall_started_at is None else now - self._wall_started_at
        active = self._active_elapsed
        if self._active_started_at is not None:
            active += now - self._active_started_at
        reasons = sorted(set(self._pauses.values()))
        return DeveloperBudgetSnapshot(
            wall_elapsed_seconds=max(0.0, wall),
            active_elapsed_seconds=max(0.0, active),
            wall_limit_seconds=self.limits.wall_timeout_seconds,
            active_limit_seconds=self.limits.active_timeout_seconds,
            paused=bool(self._pauses),
            pause_reason=", ".join(reasons),
        )

    def remaining_wall_seconds(self) -> float:
        """Return the non-negative wall time remaining."""
        snap = self.snapshot()
        return max(0.0, snap.wall_limit_seconds - snap.wall_elapsed_seconds)

    def raise_if_exhausted(self) -> None:
        """Raise the terminal error when either currently applicable limit is spent."""
        kind = self._exhausted_kind(self.snapshot(), active_finished=self._active_finished)
        if kind is not None:
            raise self.timeout_error(kind)

    async def wait_until_exhausted(self) -> BudgetKind:
        """Publish live readings until one budget reaches its limit."""
        while self._running:
            snap = self._publish()
            kind = self._exhausted_kind(snap, active_finished=self._active_finished)
            if kind is not None:
                return kind
            delay = self._next_delay(snap, active_finished=self._active_finished)
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=delay)
        raise RuntimeError("Developer budget stopped before a deadline was reached")

    def timeout_error(self, kind: BudgetKind) -> AgentTimeoutError:
        """Build the terminal, non-transient error for an exhausted budget."""
        snap = self.snapshot()
        elapsed = snap.active_elapsed_seconds if kind == "active" else snap.wall_elapsed_seconds
        limit = snap.active_limit_seconds if kind == "active" else snap.wall_limit_seconds
        label = "active-time" if kind == "active" else "wall-clock"
        return AgentTimeoutError(
            f"Developer Agent {label} limit reached "
            f"({elapsed:.0f}s elapsed, {limit}s limit from [developer.limits])"
        )

    def _accrue_active(self) -> None:
        if self._active_started_at is None:
            return
        now = self._clock()
        self._active_elapsed += max(0.0, now - self._active_started_at)
        self._active_started_at = now

    @staticmethod
    def _exhausted_kind(
        snap: DeveloperBudgetSnapshot, *, active_finished: bool = False
    ) -> BudgetKind | None:
        if snap.wall_elapsed_seconds >= snap.wall_limit_seconds:
            return "wall"
        if (
            not active_finished
            and not snap.paused
            and snap.active_elapsed_seconds >= snap.active_limit_seconds
        ):
            return "active"
        return None

    @staticmethod
    def _next_delay(snap: DeveloperBudgetSnapshot, *, active_finished: bool = False) -> float:
        remaining = [snap.wall_limit_seconds - snap.wall_elapsed_seconds, _TICK_SECONDS]
        if not active_finished and not snap.paused:
            remaining.append(snap.active_limit_seconds - snap.active_elapsed_seconds)
        return max(0.001, min(remaining))

    def _publish(self, *, force_persist: bool = False) -> DeveloperBudgetSnapshot:
        snap = self.snapshot()
        if self._on_event is not None:
            try:
                self._on_event({"type": "developer_budget", **asdict(snap)})
            except Exception:
                logger.debug("Developer budget display callback failed", exc_info=True)
        self._persist(snap, force=force_persist)
        return snap

    def _persist(self, snap: DeveloperBudgetSnapshot, *, force: bool) -> None:
        now = self._clock()
        if not force and now - self._last_persist_at < _PERSIST_INTERVAL_SECONDS:
            return
        payload = {"run_index": self._run_index, "updated_at": now_iso(), **asdict(snap)}
        tmp = self._persist_path.with_suffix(".tmp")
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._persist_path)
        self._last_persist_at = now


async def run_with_developer_budget(
    work: Coroutine[Any, Any, T],
    budget: DeveloperBudget,
    *,
    on_exhausted: Callable[[BudgetKind], Awaitable[None] | None] | None = None,
) -> T:
    """Run one backend operation until it finishes or either budget expires."""
    work_task = asyncio.create_task(work)
    budget_task = asyncio.create_task(budget.wait_until_exhausted())
    try:
        done, _pending = await asyncio.wait(
            {work_task, budget_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if work_task in done:
            return await work_task

        kind = await budget_task
        if on_exhausted is not None:
            result = on_exhausted(kind)
            if inspect.isawaitable(result):
                await result
        raise budget.timeout_error(kind)
    finally:
        for task in (work_task, budget_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(work_task, budget_task, return_exceptions=True)
