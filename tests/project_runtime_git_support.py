"""Bounded isolated Git fixtures for Project runtime policy regressions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.harness.init_cmd import InitContext, _backfill_project_gitignore

NEW_RUNTIME_PATHS = (
    "reviewer-evidence/audit.json",
    "findings.jsonl",
    "findings.jsonl.tmp",
    "BOOLEY-FEEDBACK.md",
    "setup-evidence/attachments/log.txt",
    "PARITY-REPORT.md",
)


def git(root: Path, *args: str) -> str:
    """Run bounded Git against an isolated fixture repository."""
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, timeout=10
    ).stdout


def initialize_repository(root: Path, data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Commit inputs before any writer, without inherited Git ignore rules."""
    config = root.parent / f"{root.name}-git-config"
    config.mkdir(exist_ok=True)
    empty = config / "empty"
    empty.touch()
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("HOME", str(config))
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.excludesFile", str(empty))
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    _backfill_project_gitignore(data, InitContext(project_root=root))
    git(root, "add", ".")
    git(root, "commit", "-qm", "initialized inputs")
    assert_clean(root)


def assert_clean(root: Path) -> None:
    """Include every untracked descendant in the cleanliness assertion."""
    assert git(root, "status", "--short", "--untracked-files=all") == ""
