"""T3 Code thread titles for the web Dashboard's Job owners."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Iterable
from time import monotonic

from booley.core.boundary import as_dict
from booley.runtime.platform_paths import run_captured_bounded

logger = logging.getLogger(__name__)

# Time one snapshot may spend on lookups, shared by all its threads. A lookup starts only
# with half of it left, so a slow but working T3 Code is not mistaken for a stalled one:
# the threads left over are tried on the next snapshot, after the cached ones cost nothing.
_LOOKUP_BUDGET_S = 2.0
# How long a found title, an unknown thread, or a missing or stalled T3 Code is remembered.
_CACHE_S = 60.0


class ThreadTitles:
    """Titles of T3 Code threads, asked of the ``t3`` command and cached.

    The cache keeps a page refresh from running ``t3`` each time; its expiry lets a title
    T3 Code generates after the first message replace the placeholder.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._unavailable_until = 0.0

    def resolve(self, threads: Iterable[str]) -> dict[str, str]:
        """The titles known for *threads*; the others are left out and tried on a later call."""
        deadline = monotonic() + _LOOKUP_BUDGET_S
        titles: dict[str, str] = {}
        for thread in dict.fromkeys(threads):
            now = monotonic()
            entry = self._cache.get(thread)
            if entry is None or entry[0] <= now:
                if deadline - now < _LOOKUP_BUDGET_S / 2 or now < self._unavailable_until:
                    continue
                try:
                    title = _lookup(thread, deadline - now)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    logger.debug("T3 Code did not answer for thread %s: %s", thread, exc)
                    self._unavailable_until = now + _CACHE_S
                    continue
                entry = self._cache[thread] = (now + _CACHE_S, title)
            if entry[1] is not None:
                titles[thread] = entry[1]
        return titles


def _lookup(thread: str, timeout: float) -> str | None:
    """The title ``t3 thread show`` reports, or ``None`` for an unknown thread or unusable output."""
    cli = os.environ.get("T3CODE_CLI_PATH") or "t3"
    done = run_captured_bounded(
        [cli, "thread", "show", thread, "--json", "--messages", "0"],
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        return None
    try:
        payload: object = json.loads(done.stdout)
    except ValueError:
        return None
    thread_info = as_dict((as_dict(payload) or {}).get("thread")) or {}
    title = thread_info.get("title")
    return title if isinstance(title, str) and title else None
