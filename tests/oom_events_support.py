"""The one fake of the container's OOM event counters (``job_resources.cgroup_memory_events``)."""

from __future__ import annotations

import itertools

import pytest

from booley.flows import job_resources


def rising_oom_events(monkeypatch: pytest.MonkeyPatch, *, limit_reached: bool) -> None:
    """The container's OOM-kill count rises on every read; the Sandbox's own limit count
    rises with it only when *limit_reached* (else the kill is the host's).
    """
    counter = itertools.count()

    def events() -> job_resources.MemoryEvents:
        kills = next(counter)
        return job_resources.MemoryEvents(kills, kills if limit_reached else 0)

    # The boundary: the one function that reads the cgroup's memory.events file.
    monkeypatch.setattr(job_resources, "cgroup_memory_events", events)
