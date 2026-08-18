"""Worktree line metrics used by the ticket Console."""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley.harness.console_metrics import WorktreeLineCounter


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_snapshot_includes_tracked_and_untracked_text(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.sv").write_text("one\ntwo\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.sv")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "branch", "base")
    counter = WorktreeLineCounter(tmp_path, "base")

    (tmp_path / "tracked.sv").write_text("one changed\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "new.sv").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"not\x00text\n")
    (tmp_path / "link.sv").symlink_to("new.sv")

    assert counter.snapshot() == (4, 1)
    assert counter.snapshot_by_file() == {
        "new.sv": (2, 0),
        "tracked.sv": (2, 1),
    }


def test_normalize_agent_reported_paths(tmp_path):
    _git(tmp_path, "init", "-q")
    counter = WorktreeLineCounter(tmp_path, "HEAD")

    assert counter.normalize_path("rtl/top.sv") == "rtl/top.sv"
    assert counter.normalize_path(str(tmp_path / "rtl/top.sv")) == "rtl/top.sv"
    assert counter.normalize_path("/work/rtl/top.sv") == "rtl/top.sv"
    assert counter.normalize_path("../outside.sv") is None
    assert counter.normalize_path("/etc/outside.sv") is None


def test_snapshot_remains_absolute_after_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "rtl.sv").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "rtl.sv")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "branch", "base")
    counter = WorktreeLineCounter(tmp_path, "base")

    (tmp_path / "rtl.sv").write_text("one\ntwo\n", encoding="utf-8")
    _git(tmp_path, "commit", "-qam", "edit")
    (tmp_path / "rtl.sv").write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert counter.snapshot() == (2, 0)
