"""The reader of the hook's conversation records reads damaged storage as absent."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip

from booley.runtime import conversation_records as records
from booley.runtime.pid import capture_process_identity
from booley.runtime.session_activity import process_session_key

_STAMP = 11


def _write(common: Path, client: str, conversation: str, **changes: object) -> Path:
    worktree = common / "worktrees" / "s1"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_bytes(b"gitdir: elsewhere\n")
    os.utime(worktree / ".git", ns=(_STAMP, _STAMP))
    record: dict[str, object] = {
        "schema": 1,
        "client": client,
        "conversation_id": conversation,
        "cwd": str(worktree),
        "worktree": "s1",
        "handle": "S1",
        "process_key": None,
        "at": "2026-11-11T10:00:00Z",
        "git_stamp": _STAMP,
        **changes,
    }
    digest = hashlib.sha256(f"{client}:{conversation}".encode()).hexdigest()
    path = common / "worktrees" / ".conversations" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(record).encode())
    return path


def test_a_valid_record_is_found_and_live(tmp_path):
    _write(tmp_path, "claude", "c1")

    found = records.find(tmp_path, "claude", "c1")

    assert found is not None
    assert found.handle == "S1"
    assert records.is_live(tmp_path, found)
    assert records.live(tmp_path) == [found]


@pytest.mark.parametrize(
    "damage",
    ["schema", "no-cwd", "bad-stamp", "not-json", "not-object", "oversized", "other-id"],
)
def test_a_damaged_record_reads_as_absent(tmp_path, damage):
    # Bug caught: a half-written or foreign record crashing a resume or steering it.
    path = _write(tmp_path, "claude", "c1")
    data = json.loads(path.read_bytes())
    if damage == "schema":
        data["schema"] = 2
    elif damage == "no-cwd":
        del data["cwd"]
    elif damage == "bad-stamp":
        data["git_stamp"] = "x"
    elif damage == "other-id":
        data["conversation_id"] = "c2"
    payload = {
        "not-json": b"{broken",
        "not-object": b"[]",
        "oversized": b" " * (records.MAX_RECORD_BYTES + 1),
    }.get(damage, json.dumps(data).encode())
    path.write_bytes(payload)

    assert records.find(tmp_path, "claude", "c1") is None
    # A record under another name is still a valid record of the conversation it names.
    assert len(records.live(tmp_path)) == (damage == "other-id")


def test_a_missing_store_and_a_missing_worktree_are_not_live(tmp_path):
    assert records.find(tmp_path, "claude", "c1") is None
    assert records.live(tmp_path) == []
    path = _write(tmp_path, "claude", "c1")
    found = records.find(tmp_path, "claude", "c1")
    assert found is not None
    (path.parent.parent / "s1" / ".git").unlink()
    assert not records.is_live(tmp_path, found)


def test_a_redirected_store_is_refused(tmp_path):
    # Bug caught: following a symlinked record store out of the Booley Directory.
    path = _write(tmp_path, "claude", "c1")
    elsewhere = tmp_path / "elsewhere"
    path.parent.rename(elsewhere)
    symlink_or_skip(path.parent, elsewhere)

    assert records.find(tmp_path, "claude", "c1") is None
    assert records.live(tmp_path) == []


def test_a_symlinked_record_is_skipped(tmp_path):
    path = _write(tmp_path, "claude", "c1")
    real = path.with_suffix(".real")
    path.rename(real)
    symlink_or_skip(path, real)

    assert records.find(tmp_path, "claude", "c1") is None
    assert records.live(tmp_path) == []


@pytest.mark.skipif(sys.platform != "linux", reason="process keys are observed through procfs")
def test_only_a_running_process_occupies_its_worktree(tmp_path):
    # Bug caught: a finished session, or a record with no process, blocking a worktree.
    running = capture_process_identity(os.getpid())
    assert running is not None
    key = process_session_key(running)
    live = [
        records.ConversationRecord("claude", "a", "/w", "s1", None, "pid:fixture:990001:7", 1),
        records.ConversationRecord("claude", "b", "/w", "s2", None, None, 1),
        records.ConversationRecord("claude", "c", "/w", "s3", None, "codex:thread", 1),
        records.ConversationRecord("claude", "d", "/w", "s4", "S4", key, 1),
        records.ConversationRecord("codex", "e", "/w", "s5", None, key, 1),
        records.ConversationRecord("codex", "f", "/w", "s5", "S9", key, 1),
    ]

    assert records.occupants(live) == {"s4": "S4", "s5": key}
