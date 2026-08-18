"""Developer Agent active-time and wall-clock budget tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from booley.harness.blocking import AgentTimeoutError
from booley.harness.config import DeveloperLimitsConfig
from booley.harness.developer_budget import DeveloperBudget, run_with_developer_budget


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _budget(tmp_path, clock: _Clock) -> DeveloperBudget:
    return DeveloperBudget(
        DeveloperLimitsConfig(active_timeout_seconds=30, wall_timeout_seconds=120),
        persist_path=tmp_path / "budget.json",
        clock=clock,
    )


def test_booley_wait_pauses_active_but_not_wall(tmp_path) -> None:
    clock = _Clock()
    budget = _budget(tmp_path, clock)
    budget.start(3)
    clock.advance(10)
    budget.pause("mcp:1", "waiting for simulate")
    clock.advance(80)

    paused = budget.snapshot()
    assert paused.active_elapsed_seconds == 10
    assert paused.wall_elapsed_seconds == 90
    assert paused.pause_reason == "waiting for simulate"

    budget.resume("mcp:1")
    clock.advance(5)
    final = budget.finish()
    assert final.active_elapsed_seconds == 15
    assert final.wall_elapsed_seconds == 95


def test_budget_persists_live_snapshot(tmp_path) -> None:
    clock = _Clock()
    budget = _budget(tmp_path, clock)
    budget.start(7)
    clock.advance(12)
    budget.pause("mcp:1", "waiting for lint")

    payload = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert payload["run_index"] == 7
    assert payload["active_elapsed_seconds"] == 12
    assert payload["wall_elapsed_seconds"] == 12
    assert payload["paused"] is True


@pytest.mark.asyncio
async def test_active_deadline_is_non_transient(tmp_path) -> None:
    budget = DeveloperBudget(
        DeveloperLimitsConfig(active_timeout_seconds=1, wall_timeout_seconds=10),
        persist_path=tmp_path / "budget.json",
    )
    budget.start(1)
    with pytest.raises(AgentTimeoutError, match="active-time limit reached"):
        await run_with_developer_budget(asyncio.sleep(5), budget)
    budget.finish()


@pytest.mark.asyncio
async def test_wall_deadline_still_expires_during_excluded_wait(tmp_path) -> None:
    clock = _Clock()
    budget = _budget(tmp_path, clock)
    budget.start(1)
    budget.pause("mcp:1", "waiting for simulate")
    deadline = asyncio.create_task(budget.wait_until_exhausted())
    await asyncio.sleep(0)
    clock.advance(121)
    budget.resume("mcp:1")

    assert await deadline == "wall"
    budget.finish()


@pytest.mark.asyncio
async def test_cancelling_wrapper_cleans_up_work_and_budget_tasks(tmp_path) -> None:
    budget = DeveloperBudget(
        DeveloperLimitsConfig(active_timeout_seconds=30, wall_timeout_seconds=120),
        persist_path=tmp_path / "budget.json",
    )
    budget.start(1)
    work_cancelled = asyncio.Event()
    monitor_cancelled = asyncio.Event()
    original_monitor = budget.wait_until_exhausted

    async def work() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            work_cancelled.set()

    async def monitor():
        try:
            return await original_monitor()
        finally:
            monitor_cancelled.set()

    budget.wait_until_exhausted = monitor  # type: ignore[method-assign]
    wrapper = asyncio.create_task(run_with_developer_budget(work(), budget))
    await asyncio.sleep(0)
    wrapper.cancel()

    with pytest.raises(asyncio.CancelledError):
        await wrapper
    assert work_cancelled.is_set()
    assert monitor_cancelled.is_set()
    budget.finish()


def test_stop_active_leaves_wall_budget_running(tmp_path) -> None:
    clock = _Clock()
    budget = _budget(tmp_path, clock)
    budget.start(1)
    clock.advance(8)
    budget.stop_active()
    clock.advance(20)

    snap = budget.snapshot()
    assert snap.active_elapsed_seconds == 8
    assert snap.wall_elapsed_seconds == 28
    budget.finish()
