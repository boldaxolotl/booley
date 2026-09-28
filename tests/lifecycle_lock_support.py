"""Shared synchronization helpers for host lifecycle-lock regressions."""

from __future__ import annotations

import multiprocessing
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from booley.runtime import lifecycle_lock
from booley.runtime.file_lock import acquire_file_lock, release_file_lock


def _hold_lock(path_text: str, owner: str, ready: Any, release: Any) -> None:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        acquire_file_lock(handle)
        handle.write(f"{owner}\n")
        handle.flush()
        ready.set()
        release.wait(5)
        release_file_lock(handle)


@dataclass
class LifecycleLockHolder:
    """Control a child process holding one real lifecycle lock."""

    process: Any
    release_event: Any

    def release(self) -> None:
        self.release_event.set()


@contextmanager
def held_lifecycle_lock(
    path: Path,
    owner: str = "pid=41 operation=other lifecycle command",
) -> Iterator[LifecycleLockHolder]:
    """Hold *path* in a child process until the caller releases it."""
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_lock, args=(str(path), owner, ready, release))
    process.start()
    holder = LifecycleLockHolder(process, release)
    try:
        assert ready.wait(5)
        yield holder
    finally:
        holder.release()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)


def observe_lifecycle_contention(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Return an event set only after a real nonblocking acquire contends."""
    contended = threading.Event()
    real_wait = lifecycle_lock.wait_for_file_lock

    def observed_wait(handle, *, timeout_s, on_wait=None) -> None:
        def report_wait() -> None:
            contended.set()
            if on_wait is not None:
                on_wait()

        real_wait(handle, timeout_s=timeout_s, on_wait=report_wait)

    monkeypatch.setattr(lifecycle_lock, "wait_for_file_lock", observed_wait)
    return contended
