"""Session activity signals: event translation, storage bounds, freshness and pruning."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.conftest import symlink_or_skip, write_activity_signal

from booley.runtime import session_activity as activity
from booley.runtime.session_activity import ActivitySignal, ActivityStore, observed_activity

NOW = 1791454200.0
STAMP = "2026-10-08T10:10:00Z"


def test_store_reads_each_sessions_own_signal(tmp_path):
    store = ActivityStore(tmp_path)
    assert store.read("codex:a") is None
    write_activity_signal(tmp_path, "codex:a", activity.WAITING_TOOL, STAMP, "sim")
    assert store.read("codex:a") == ActivitySignal("codex:a", activity.WAITING_TOOL, STAMP, "sim")
    assert store.read("codex:b") is None
    write_activity_signal(tmp_path, "codex:a", activity.WORKING, STAMP)
    assert store.read("codex:a") == ActivitySignal("codex:a", activity.WORKING, STAMP)


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


def test_store_refuses_a_symlinked_signal(tmp_path):
    store = ActivityStore(tmp_path)
    target = write_activity_signal(tmp_path / "elsewhere", "codex:b", activity.WORKING, STAMP)
    store.root.mkdir(parents=True)
    symlink_or_skip(store.path("codex:b"), target)
    assert store.read("codex:b") is None


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
    for key, age in (("codex:held", 5000), ("codex:recent", 50), ("codex:gone", 100)):
        path = write_activity_signal(tmp_path, key, activity.WAITING_USER, STAMP)
        os.utime(path, (NOW - age, NOW - age))
    assert store.prune({"codex:held"}, now=NOW, quiet_after=100) == 1
    assert store.read("codex:held") is not None
    assert store.read("codex:recent") is not None
    assert store.read("codex:gone") is None


def test_store_reads_and_prunes_under_a_symlinked_booley_directory(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    booley_dir = tmp_path / "project" / ".booley"
    booley_dir.parent.mkdir()
    symlink_or_skip(booley_dir, store_dir, target_is_directory=True)
    store = ActivityStore(booley_dir)
    path = write_activity_signal(booley_dir, "codex:a", activity.WORKING, STAMP)
    os.utime(path, (NOW - 500, NOW - 500))

    assert store.read("codex:a") == ActivitySignal("codex:a", activity.WORKING, STAMP)
    assert store.prune(set(), now=NOW, quiet_after=100) == 1
    assert store.read("codex:a") is None

    # A link below the Booley Directory is still refused.
    write_activity_signal(store_dir, "codex:b", activity.WORKING, STAMP)
    moved = store_dir / "moved"
    (store_dir / "runtime/session_activity").rename(moved)
    symlink_or_skip(store_dir / "runtime/session_activity", moved, target_is_directory=True)
    assert store.read("codex:b") is None
    assert store.prune(set(), now=NOW + 10_000, quiet_after=100) == 0


def test_prune_expires_signals_when_directory_entries_have_no_inode(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path)
    path = write_activity_signal(tmp_path, "codex:gone", activity.WAITING_USER, STAMP)
    os.utime(path, (NOW - 100, NOW - 100))
    metadata = path.lstat()
    # Windows DirEntry.stat() reports zero for st_ino, unlike Path.lstat().
    listed = SimpleNamespace(
        st_ino=0, st_mtime=metadata.st_mtime, st_mtime_ns=metadata.st_mtime_ns
    )
    entry = SimpleNamespace(name=path.name, path=str(path), stat=lambda **_: listed)
    monkeypatch.setattr(store, "_entries", lambda: [entry])
    assert store.prune(set(), now=NOW, quiet_after=100) == 1
    assert store.read("codex:gone") is None


def test_prune_spares_a_signal_the_hook_replaced_after_it_was_listed(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path)
    path = write_activity_signal(tmp_path, "codex:back", activity.WAITING_USER, STAMP)
    os.utime(path, (NOW - 500, NOW - 500))
    listing = ActivityStore._entries

    def replaced_after_listing(self):
        entries = listing(self)
        for entry in entries:
            entry.stat(follow_symlinks=False)  # The pruner's view is the expired file.
        # The session's next event lands now, by atomic replacement, as the hook does it.
        fresh = path.with_name(".staged.tmp")
        fresh.write_bytes(path.read_bytes().replace(b"waiting for user input", b"working"))
        fresh.replace(path)
        return entries

    monkeypatch.setattr(ActivityStore, "_entries", replaced_after_listing)
    assert store.prune(set(), now=NOW, quiet_after=100) == 0
    assert store.read("codex:back").state == activity.WORKING


def test_prune_spares_a_replacement_between_identity_checks(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path)
    path = write_activity_signal(tmp_path, "codex:back", activity.WAITING_USER, STAMP)
    os.utime(path, (NOW - 500, NOW - 500))
    original_lstat = Path.lstat
    replaced = False

    def replace_after_first_stat(current):
        nonlocal replaced
        metadata = original_lstat(current)
        if current == path and not replaced:
            replaced = True
            fresh = path.with_name(".staged.tmp")
            fresh.write_bytes(path.read_bytes().replace(b"waiting for user input", b"working"))
            os.utime(fresh, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            fresh.replace(path)
        return metadata

    monkeypatch.setattr(Path, "lstat", replace_after_first_stat)
    assert store.prune(set(), now=NOW, quiet_after=100) == 0
    assert replaced
    assert store.read("codex:back").state == activity.WORKING


def test_session_keys_match_the_registry_format():
    from booley.runtime.pid import ProcessIdentity

    identity = ProcessIdentity(pid=42, identity_scope="pid:[4026531836]", start_token=977)
    assert activity.process_session_key(identity) == "pid:pid:[4026531836]:42:977"
    assert activity.thread_session_key("01a1") == "codex:01a1"
    assert activity.valid_thread_id("01a1")
    assert activity.valid_thread_id("t" * activity.MAX_THREAD_ID)
    assert not activity.valid_thread_id("t" * (activity.MAX_THREAD_ID + 1))
    assert not activity.valid_thread_id("")
    assert not activity.valid_thread_id("a\nb")
    assert not activity.valid_thread_id(7)


def test_activity_read_survives_transient_sharing_error(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    store = ActivityStore(tmp_path)
    write_activity_signal(tmp_path, "codex:a", activity.WORKING, STAMP)
    attempts = hold_opens(monkeypatch, {store.path("codex:a"): 1})
    assert store.read("codex:a") == ActivitySignal("codex:a", activity.WORKING, STAMP)
    assert attempts[store.path("codex:a")] == 2


def test_lasting_activity_sharing_error_reads_unknown_after_retrying(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    store = ActivityStore(tmp_path)
    write_activity_signal(tmp_path, "codex:a", activity.WORKING, STAMP)
    attempts = hold_opens(monkeypatch, {store.path("codex:a"): 99})
    assert store.read("codex:a") is None
    assert attempts[store.path("codex:a")] > 1


def test_end_marker_reader_accepts_only_the_hook_layout(tmp_path):
    assert getattr(activity, "ENDED", None) == "ended"
    now = 1791454205.0
    store = ActivityStore(tmp_path)
    write_activity_signal(tmp_path, "codex:t", "ended", "2026-10-08T10:10:00Z")
    assert store.ended_at("codex:t", now=now) == "2026-10-08T10:10:00Z"
    assert store.read("codex:t") is None
    assert store.ended_at("codex:absent", now=now) is None
    write_activity_signal(tmp_path, "codex:t", activity.WORKING, "2026-10-08T10:10:00Z")
    assert store.ended_at("codex:t", now=now) is None
    write_activity_signal(tmp_path, "codex:t", "ended", "2026-10-08T11:10:00Z")
    assert store.ended_at("codex:t", now=now) is None  # From the future: unusable.
    store.path("codex:t").write_text("{", encoding="utf-8")
    assert store.ended_at("codex:t", now=now) is None


def test_prune_removes_unretained_end_markers_at_once_and_spares_a_replacement(
    tmp_path, monkeypatch
):
    assert getattr(activity, "ENDED", None) == "ended"
    store = ActivityStore(tmp_path)
    gone = write_activity_signal(tmp_path, "codex:gone", "ended", STAMP)
    kept = write_activity_signal(tmp_path, "codex:kept", "ended", STAMP)
    busy = write_activity_signal(tmp_path, "codex:busy", activity.WORKING, STAMP)
    # Marker mtimes are fresh (not past quiet_after) and even ahead of the clock.
    for path in (gone, kept, busy):
        os.utime(path, (NOW + 60, NOW + 60))
    assert store.prune({"codex:kept"}, now=NOW, quiet_after=7200) == 1
    assert not gone.exists()
    assert kept.exists()  # Its session still has a row.
    assert busy.exists()  # Not a marker: the quiet rule still applies.

    # A marker the hook replaced between the two identity checks is spared.
    original_lstat = Path.lstat
    calls = 0

    def replaced_after_first_lstat(current):
        nonlocal calls
        metadata = original_lstat(current)
        if current == kept:
            calls += 1
            if calls == 2:
                return SimpleNamespace(
                    st_ino=metadata.st_ino + 1, st_mtime_ns=metadata.st_mtime_ns
                )
        return metadata

    monkeypatch.setattr(Path, "lstat", replaced_after_first_lstat)
    assert store.prune(set(), now=NOW, quiet_after=7200) == 0
    assert calls == 2
    assert kept.exists()


def test_prune_keeps_a_fresh_file_that_is_not_readable_as_an_end_marker(tmp_path):
    """A fresh file whose bytes are not JSON or not a marker is no marker: the quiet rule applies."""
    store = ActivityStore(tmp_path)
    store.root.mkdir(parents=True)
    garbage = store.path("codex:garbage")
    garbage.write_bytes(b"\xff not json")
    # A marker stored under another key's file name is not this file's marker.
    misplaced = store.path("codex:misplaced")
    misplaced.write_text(
        json.dumps({"schema": 1, "key": "codex:other", "state": "ended", "at": STAMP}),
        encoding="utf-8",
    )
    for path in (garbage, misplaced):
        os.utime(path, (NOW + 60, NOW + 60))
    assert store.prune(set(), now=NOW, quiet_after=7200) == 0
    assert garbage.exists()
    assert misplaced.exists()


def test_store_ignores_a_symlinked_end_marker(tmp_path):
    store = ActivityStore(tmp_path)
    target = write_activity_signal(tmp_path / "elsewhere", "codex:b", "ended", STAMP)
    store.root.mkdir(parents=True)
    symlink_or_skip(store.path("codex:b"), target)
    assert store.ended_at("codex:b", now=NOW) is None
