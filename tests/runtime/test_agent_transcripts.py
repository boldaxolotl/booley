"""Conversation recency read from the Agent CLIs' own storage."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from booley.runtime import agent_transcripts
from booley.runtime.agent_transcripts import (
    RecencyUnknownError,
    last_active,
    latest_in,
    store_root,
)

_EPOCH = 1_800_000_000


@pytest.fixture(autouse=True)
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return tmp_path


def _claude(tmp_path: Path, cwd: str, name: str, mtime: float) -> None:
    slug = "".join(c if c.isalnum() else "-" for c in cwd)
    path = tmp_path / "claude" / "projects" / slug / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}\n")
    os.utime(path, (mtime, mtime))


def _rollout(
    tmp_path: Path,
    name: str,
    cwd: str,
    mtime: float,
    *,
    source: str = "cli",
    first: bytes | None = None,
) -> None:
    path = (
        tmp_path
        / "codex"
        / "sessions"
        / "2026"
        / "11"
        / "10"
        / f"rollout-2026-11-10T00-00-00-{name}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"cwd": cwd, "source": source}}
    path.write_bytes((first if first is not None else json.dumps(meta).encode()) + b"\n")
    os.utime(path, (mtime, mtime))


def test_the_store_defaults_to_the_home_directory(monkeypatch, tmp_path):
    # Bug caught: an unset override reading a relative or empty path.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    assert store_root("claude") == tmp_path / "home" / ".claude"
    assert store_root("codex") == tmp_path / "home" / ".codex"


def test_claude_conversations_are_found_by_directory_slug(homes):
    # Bug caught: the slug differing from the CLI's, or an id escaping the project directory.
    _claude(homes, "/work/my.repo", "a", _EPOCH)
    _claude(homes, "/work/my.repo", "b", _EPOCH + 5)

    assert last_active("claude", "/work/my.repo", "a") == _EPOCH
    assert last_active("claude", "/work/my.repo", "missing") is None
    assert last_active("claude", "/work/my.repo", "../claude/projects") is None
    assert last_active("claude", "/work/my.repo", "") is None
    assert latest_in("claude", Path("/work/my.repo"), None) == _EPOCH + 5
    assert latest_in("claude", Path("/work/other"), None) is None


def test_codex_conversation_recency_is_the_update_time_of_its_rollout(homes):
    # Bug caught: the id read as a glob pattern, or a rollout of another conversation counted.
    _rollout(homes, "a-1", "/w", _EPOCH)
    _rollout(homes, "b-1", "/w", _EPOCH + 9)

    assert last_active("codex", "/w", "a-1") == _EPOCH
    assert last_active("codex", "/w", "*") is None
    assert last_active("codex", "/w", "nope") is None


def test_codex_latest_skips_other_directories_and_exec_runs(homes):
    # Bug caught: `--last` counting a headless `codex exec` run or another directory's
    # conversation as main's newest.
    _rollout(homes, "other", "/elsewhere", _EPOCH + 30)
    _rollout(homes, "headless", "/w", _EPOCH + 20, source="exec")
    _rollout(homes, "mine", "/w", _EPOCH + 10)
    _rollout(homes, "older", "/w", _EPOCH)

    assert latest_in("codex", Path("/w"), None) == _EPOCH + 10
    assert latest_in("codex", Path("/w"), _EPOCH + 11) is None
    assert latest_in("codex", Path("/nowhere"), None) is None


def test_a_missing_store_is_no_conversation(homes):
    assert latest_in("codex", Path("/w"), None) is None
    assert latest_in("claude", Path("/w"), None) is None


@pytest.mark.parametrize("fault", ["too-many", "oversized", "not-json", "not-meta", "not-object"])
def test_codex_latest_is_unknown_when_a_newer_rollout_cannot_be_read(homes, monkeypatch, fault):
    # Bug caught: guessing "no conversation" for a rollout that may well be main's newest.
    _rollout(homes, "fine", "/w", _EPOCH)
    broken = {
        "oversized": b'{"type":"session_meta","payload":{"cwd":"' + b"a" * 64 + b'"}}',
        "not-json": b"{broken",
        "not-meta": b'{"type":"event_msg","payload":{}}',
        "not-object": b"[1]",
    }.get(fault, b'{"type":"session_meta","payload":{"cwd":"/w"}}')
    _rollout(homes, "newer", "/w", _EPOCH + 10, first=broken)
    monkeypatch.setattr(agent_transcripts, "MAX_FIRST_LINE", 32 if fault == "oversized" else 4096)
    monkeypatch.setattr(agent_transcripts, "MAX_ROLLOUTS", 1 if fault == "too-many" else 100)

    with pytest.raises(RecencyUnknownError):
        latest_in("codex", Path("/w"), _EPOCH)


def test_codex_latest_ignores_unreadable_rollouts_that_are_not_newer(homes):
    # Bug caught: opening every rollout, so one old corrupt file makes recency unknown.
    _rollout(homes, "old", "/w", _EPOCH - 100, first=b"{broken")
    _rollout(homes, "mine", "/w", _EPOCH)

    assert latest_in("codex", Path("/w"), _EPOCH - 50) == _EPOCH
