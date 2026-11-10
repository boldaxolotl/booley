"""Fail the test that leaves a thread running (#3217).

A thread that outlives its test keeps running into later tests on the same
worker, where its failures and side effects are attributed to the wrong test
or surface only on a slow platform. The guard snapshots the live threads
before a test's fixtures set up. After every fixture finalizer has run it
waits a short grace period for new threads to finish on their own, then fails
the test that started the ones still alive, naming them.

Known leaks live in ``tests/thread_leak_allowlist.toml``; that list may only
shrink.
"""

from __future__ import annotations

import threading
import time
import tomllib
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest

from booley.core.boundary import require_positive_int, require_str_value

# One shared deadline per test. Threads that are not allow-listed finish well
# inside it on a healthy run; the margin absorbs slow Windows runners.
GRACE_S = 2.0
_ALLOWLIST_PATH = Path(__file__).with_name("thread_leak_allowlist.toml")
_BASELINE = pytest.StashKey[frozenset[threading.Thread]]()
_ALLOWLIST = pytest.StashKey[list["AllowEntry"]]()
_ENTRY_KEYS = frozenset({"thread", "test", "reason", "issue"})


@dataclass(frozen=True)
class AllowEntry:
    """One known leak: patterns that identify it and why it is tolerated."""

    thread: str | None
    test: str | None
    reason: str
    issue: int | None


def describe(thread: threading.Thread) -> str:
    """Return the ``module.qualname`` of what the thread runs.

    ``_target`` is private, but CPython keeps it while a ``Thread`` runs and
    nothing public names the callable. ``Timer`` and ``run`` overrides carry no
    target, so fall back to the timer's function, then to the thread's class.
    """
    target: Any = getattr(thread, "_target", None) or getattr(thread, "function", None)
    if target is None:
        kind = type(thread)
        return f"{kind.__module__}.{kind.__qualname__}"
    return f"{getattr(target, '__module__', '?')}.{getattr(target, '__qualname__', repr(target))}"


def _entry(raw: Any, position: int) -> AllowEntry:
    """Validate one ``[[leak]]`` table; raise ``ValueError`` naming it."""
    label = f"entry {position}"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a table")
    fields: dict[str, Any] = raw
    unknown = sorted(set(fields) - _ENTRY_KEYS)
    if unknown:
        raise ValueError(f"{label} has unknown key {unknown[0]!r}")
    patterns = {
        key: require_str_value(fields[key], field=f"{label} {key}")
        for key in ("thread", "test")
        if key in fields
    }
    if not patterns:
        # Without a pattern the entry would match every thread.
        raise ValueError(f"{label} needs a thread or test pattern")
    reason = require_str_value(fields.get("reason"), field=f"{label} reason")
    if not reason.strip():
        raise ValueError(f"{label} reason must not be blank")
    issue = (
        require_positive_int(fields["issue"], field=f"{label} issue")
        if "issue" in fields
        else None
    )
    return AllowEntry(
        thread=patterns.get("thread"), test=patterns.get("test"), reason=reason, issue=issue
    )


def load_allowlist(path: Path) -> list[AllowEntry]:
    """Read and validate the allow-list; ``pytest.UsageError`` on a bad entry."""
    document: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_entries: Any = document.get("leak", [])
    try:
        if not isinstance(raw_entries, list):
            raise ValueError("'leak' must be an array of tables")
        entries: list[Any] = raw_entries
        return [_entry(raw, position) for position, raw in enumerate(entries, start=1)]
    except ValueError as error:
        raise pytest.UsageError(f"{path.name}: {error}") from error


def _matches(entry: AllowEntry, nodeid: str, identity: str) -> bool:
    if entry.thread is not None and not fnmatchcase(identity, entry.thread):
        return False
    # Parameters (and pytest-repeat suffixes) are not part of the pattern, and
    # a '[' in a pattern must not read as a character class.
    return entry.test is None or fnmatchcase(nodeid.partition("[")[0], entry.test)


def unexpected_leaks(
    nodeid: str, threads: Iterable[threading.Thread], entries: Iterable[AllowEntry]
) -> list[threading.Thread]:
    """Return the threads that no allow-list entry covers for this test."""
    allowed = list(entries)
    return [
        thread
        for thread in threads
        if not any(_matches(entry, nodeid, describe(thread)) for entry in allowed)
    ]


def _failure_message(nodeid: str, leaked: list[threading.Thread]) -> str:
    lines = [f"{nodeid} left {len(leaked)} thread(s) running:"]
    lines += [
        f"  {thread.name!r} running {describe(thread)} (daemon={thread.daemon})"
        for thread in leaked
    ]
    lines.append(
        "Stop and join each thread before the test returns. "
        "tests/thread_leak_allowlist.toml lists known leaks and may only shrink; "
        "a new entry needs maintainer approval."
    )
    return "\n".join(lines)


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_ALLOWLIST] = load_allowlist(_ALLOWLIST_PATH)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, None, None]:
    """Snapshot the threads alive before any fixture of this test runs.

    Thread objects, not idents: idents are reused. Interpreter, xdist, and
    pytest-timeout threads are already alive here and are never reported.
    """
    item.stash[_BASELINE] = frozenset(threading.enumerate())
    return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, None, None]:
    """After all finalizers, fail the test if it left a non-allow-listed thread.

    A test whose finalizer raised already errors; the guard adds no second
    report, and a thread it leaked joins the next test's baseline.
    """
    baseline = item.stash.get(_BASELINE, None)
    if baseline is not None:
        del item.stash[_BASELINE]
    result = yield
    if baseline is None:
        return result
    entries = item.config.stash.get(_ALLOWLIST, [])
    candidates = unexpected_leaks(
        item.nodeid, [t for t in threading.enumerate() if t not in baseline], entries
    )
    deadline = time.monotonic() + GRACE_S
    for thread in candidates:
        thread.join(max(0.0, deadline - time.monotonic()))
    leaked = [thread for thread in candidates if thread.is_alive()]
    if leaked:
        pytest.fail(_failure_message(item.nodeid, leaked), pytrace=False)
    return result
