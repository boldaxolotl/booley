"""Operating-system concurrency contracts for shared Job slots."""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path
from typing import Any

from booley.runtime import job_slots


def _hold_single_slot(
    root: str,
    start: Any,
    active: Any,
    maximum: Any,
    counter_lock: Any,
) -> None:
    store = job_slots.SlotStore(
        Path(root),
        job_slots.SlotCaps(max_heavy=1),
    )
    start.wait(timeout=10)
    token = store.acquire(
        job_slots.CLASS_HEAVY,
        pid=os.getpid(),
        poll_interval=0.005,
    )
    try:
        with counter_lock:
            active.value += 1
            maximum.value = max(maximum.value, active.value)
        time.sleep(0.05)
    finally:
        with counter_lock:
            active.value -= 1
        store.release(token)


def test_single_slot_never_has_multiple_process_holders(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Barrier(4)
    active = ctx.Value("i", 0)
    maximum = ctx.Value("i", 0)
    counter_lock = ctx.Lock()
    workers = [
        ctx.Process(
            target=_hold_single_slot,
            args=(str(tmp_path), start, active, maximum, counter_lock),
        )
        for _ in range(4)
    ]

    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(timeout=20)
            assert not worker.is_alive(), "slot worker exceeded its bounded join"
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
        for worker in workers:
            worker.join(timeout=5)

    assert active.value == 0
    assert maximum.value == 1


def test_acquire_recovers_stale_promotion_gate(tmp_path: Path) -> None:
    class_dir = tmp_path / job_slots.CLASS_HEAVY
    class_dir.mkdir(parents=True)
    gate = class_dir / ".promotion.lock"
    gate.write_text("987654321\n", encoding="utf-8")
    old = time.time() - job_slots._UNREADABLE_REAP_AGE_SECONDS - 1
    os.utime(gate, (old, old))
    store = job_slots.SlotStore(
        tmp_path,
        job_slots.SlotCaps(max_heavy=1),
        is_pid_alive=lambda pid: pid == os.getpid(),
    )

    token = store.acquire(
        job_slots.CLASS_HEAVY,
        pid=os.getpid(),
        poll_interval=0.005,
    )
    try:
        assert token.is_holder
        assert not gate.exists()
    finally:
        store.release(token)
