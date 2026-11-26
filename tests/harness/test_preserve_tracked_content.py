"""Tracked-content-preserving Project Initialization, as `booley session refresh` runs it."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.runtime import auth_token
from tests.subprocess_budget import run_bounded_child


def _root(parent: Path, name: str = "proj") -> Path:
    project = parent / name
    (project / ".booley").mkdir(parents=True)
    run_bounded_child(["git", "init", str(project)], check=True)
    return project


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (
        "BOOLEY_DIR",
        "BOOLEY_AGENT_APP",
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_PRIMARY_AUTH",
    ):
        monkeypatch.delenv(key, raising=False)
    for key in auth_token.CREDENTIAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return _root(tmp_path)


def _refresh(root: Path) -> list[tuple[str, str, str]]:
    results = init_cmd.reinitialize_project(root, init_cmd.ProjectFacts(None))
    return [(result.name, result.status, result.detail) for result in results]


def _statuses(root: Path) -> list[tuple[str, str]]:
    return [(name, status) for name, status, _detail in _refresh(root)]


def test_refresh_reports_pending_rewrites_without_writing(root: Path) -> None:
    gitignore = root / ".booley" / ".gitignore"
    config = root / ".booley" / "booley.toml"
    gitignore.write_bytes(b"custom/\n")
    config.write_bytes(b"[project]\nname = 'x'\n")

    statuses = _statuses(root)

    assert ("booley_dir", "warn") in statuses
    assert ("agent_config", "warn") in statuses
    assert gitignore.read_bytes() == b"custom/\n"
    assert config.read_bytes() == b"[project]\nname = 'x'\n"


def test_refresh_creates_missing_files_once(root: Path) -> None:
    assert ("interactive_files", "ok") in _statuses(root)

    assert (root / ".booley" / ".gitignore").is_file()
    assert "/.devcontainer" in (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "interactive_files" not in {name for name, _status in _statuses(root)}


@pytest.mark.parametrize(
    ("problem", "status", "detail"),
    [
        ("unsafe-bind-source", "err", "generated bind source"),
        ("blocked-vscode-directory", "warn", "Dashboard task unavailable"),
    ],
)
def test_refresh_records_sandbox_file_problems(
    problem: str, status: str, detail: str, root: Path, tmp_path: Path
) -> None:
    if problem == "unsafe-bind-source":
        root = _root(tmp_path, "proj,unsafe")  # the bind-source validation refuses a comma
    else:
        (root / ".vscode").write_bytes(b"not a directory\n")

    results = [row for row in _refresh(root) if row[0] == "interactive_files"]

    assert [(name, found) for name, found, _ in results] == [("interactive_files", status)]
    assert detail in results[0][2]
