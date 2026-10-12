"""Session activity signals: event translation, storage bounds, freshness and pruning."""

from __future__ import annotations

import json
import os

import pytest

from booley.runtime import session_activity as activity
from booley.runtime.session_activity import ActivitySignal, ActivityStore, observed_activity

NOW = 1791454200.0
STAMP = "2026-10-08T10:10:00Z"


def test_store_round_trips_replaces_clears_and_isolates_keys(tmp_path):
    store = ActivityStore(tmp_path)
    assert store.read("codex:a") is None
    first = ActivitySignal("codex:a", activity.WAITING_TOOL, STAMP, "sim")
    store.write(first)
    assert store.read("codex:a") == first
    assert store.read("codex:b") is None
    store.write(ActivitySignal("codex:a", activity.WORKING, STAMP))
    assert store.read("codex:a").state == activity.WORKING
    assert [path.suffix for path in store.root.iterdir()] == [".json"]
    store.clear("codex:a")
    store.clear("codex:a")
    assert store.read("codex:a") is None


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        json.dumps({"schema": 2, "key": "codex:a", "state": "working", "at": STAMP}).encode(),
        json.dumps({"schema": 1, "key": "codex:other", "state": "working", "at": STAMP}).encode(),
        json.dumps({"schema": 1, "key": "codex:a", "state": "sleeping", "at": STAMP}).encode(),
        json.dumps({"schema": 1, "key": "codex:a", "state": "working", "at": "today"}).encode(),
        json.dumps(
            {"schema": 1, "key": "codex:a", "state": "working", "at": "2026-10-08T10:10:00.5Z"}
        ).encode(),
        b" " * (activity.MAX_SIGNAL_BYTES + 1),
    ],
)
def test_malformed_foreign_or_oversized_signals_read_as_absent(tmp_path, payload):
    store = ActivityStore(tmp_path)
    store.root.mkdir(parents=True)
    store.path("codex:a").write_bytes(payload)
    assert store.read("codex:a") is None


def test_store_refuses_a_symlinked_signal_and_reports_a_full_store(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path)
    store.write(ActivitySignal("codex:a", activity.WORKING, STAMP))
    target = tmp_path / "elsewhere.json"
    target.write_bytes(ActivitySignal("codex:b", activity.WORKING, STAMP).payload())
    try:
        store.path("codex:b").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    assert store.read("codex:b") is None
    store.path("codex:b").unlink()
    monkeypatch.setattr(activity, "MAX_SIGNALS", 1)
    with pytest.raises(OSError, match="full"):
        store.write(ActivitySignal("codex:c", activity.WORKING, STAMP))
    # A session that already has a signal can still update it.
    store.write(ActivitySignal("codex:a", activity.WAITING_USER, STAMP))
    assert store.read("codex:a").state == activity.WAITING_USER


def test_freshness_reports_unknown_for_missing_future_and_stale_busy_signals():
    assert observed_activity(None, now=NOW).state == activity.UNKNOWN
    working = ActivitySignal("k", activity.WORKING, STAMP, "")
    assert observed_activity(working, now=NOW + 60) == activity.Activity(
        activity.WORKING, STAMP, ""
    )
    assert observed_activity(working, now=NOW - 60).state == activity.UNKNOWN
    assert observed_activity(working, now=NOW - 2).state == activity.WORKING
    edge = NOW + activity.BUSY_STALE_AFTER
    assert observed_activity(working, now=edge - 1).state == activity.WORKING
    assert observed_activity(working, now=edge).state == activity.UNKNOWN
    tool = ActivitySignal("k", activity.WAITING_TOOL, STAMP, "sim")
    assert observed_activity(tool, now=edge).state == activity.UNKNOWN
    # Waiting for the user is not an activity that goes stale by itself.
    waiting = ActivitySignal("k", activity.WAITING_USER, STAMP)
    assert observed_activity(waiting, now=NOW + 10 * 86400).state == activity.WAITING_USER


def test_prune_keeps_registered_and_recent_signals_only(tmp_path):
    store = ActivityStore(tmp_path)
    assert store.prune(set(), now=NOW, quiet_after=100) == 0  # No store yet.
    for key in ("codex:held", "codex:recent", "codex:gone"):
        store.write(ActivitySignal(key, activity.WAITING_USER, STAMP))
    for key, age in (("codex:held", 5000), ("codex:recent", 50), ("codex:gone", 100)):
        os.utime(store.path(key), (NOW - age, NOW - age))
    assert store.prune({"codex:held"}, now=NOW, quiet_after=100) == 1
    assert store.read("codex:held") is not None
    assert store.read("codex:recent") is not None
    assert store.read("codex:gone") is None


def test_session_keys_match_the_registry_format():
    from booley.runtime.pid import ProcessIdentity

    identity = ProcessIdentity(pid=42, identity_scope="pid:[4026531836]", start_token=977)
    assert activity.process_session_key(identity) == "pid:pid:[4026531836]:42:977"
    assert activity.thread_session_key("01a1") == "codex:01a1"
    assert activity.valid_thread_id("01a1")
    assert not activity.valid_thread_id("")
    assert not activity.valid_thread_id("a\nb")
    assert not activity.valid_thread_id(7)
