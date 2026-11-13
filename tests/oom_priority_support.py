"""An in-memory ``/proc`` for tests of the Job OOM ranking (ADR 0087)."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Collection, Iterator
from typing import Any

import pytest

PROC_PATH = "booley.runtime.oom_priority.PROC"


class _LazyOomPriority:
    """``booley.runtime.oom_priority``, imported on first use inside a test body.

    Patch its attributes by dotted path (``"booley.runtime.oom_priority.PROC"``),
    not through this proxy.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module("booley.runtime.oom_priority"), name)


oom_priority = _LazyOomPriority()


class FakeProc:
    """Stands in for ``oom_priority.ProcOom``: ``oom_score_adj`` values and a process tree.

    A process that was never added reads as ``None``, so a fake without
    processes makes the ranking inert and writes nothing.
    """

    def __init__(self) -> None:
        self.values: dict[int, int] = {}
        self.children: dict[int, list[int]] = {}
        self.parents: dict[int, int] = {}
        self.reads: list[int] = []
        self.writes: list[tuple[int, int]] = []

    def add(self, pid: int, *, parent: int | None = None, value: int | None = None) -> int:
        """Start *pid* the way fork would: under *parent*, inheriting its value (else 0)."""
        if value is None:
            value = self.values.get(parent, 0) if parent is not None else 0
        self.values[pid] = value
        if parent is not None:
            self.children.setdefault(parent, []).append(pid)
            self.parents[pid] = parent
        return pid

    def read(self, pid: int) -> int | None:
        self.reads.append(pid)
        return self.values.get(pid)

    def write(self, pid: int, value: int) -> bool:
        self.writes.append((pid, value))
        if pid not in self.values:
            return False
        self.values[pid] = value
        return True

    def descendants(self, pid: int, exclude: Collection[int]) -> list[int]:
        found: list[int] = []
        frontier = [pid]
        while frontier:
            for child in self.children.get(frontier.pop(), []):
                if child not in exclude:
                    found.append(child)
                    frontier.append(child)
        return found

    def nested_in(self, pid: int, others: Collection[int]) -> bool:
        parent = self.parents.get(pid)
        while parent is not None:
            if parent in others:
                return True
            parent = self.parents.get(parent)
        return False


@pytest.fixture
def inert_oom_proc(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProc]:
    """Replace the real ``/proc`` accessor so a memory claim in a test ranks nothing.

    The ranking module is loaded by the admission modules a test imports; with
    it not loaded no ranking can run, so there is nothing to replace.
    """
    proc = FakeProc()
    module = sys.modules.get("booley.runtime.oom_priority")
    if module is not None:
        monkeypatch.setattr(module, "PROC", proc)
    yield proc
