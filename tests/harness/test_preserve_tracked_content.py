"""Tracked-content-preserving Project Initialization and the Interactive files step."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import dashboard_tasks
from booley.runtime.session_issuance import RuntimeSpecError
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "proj"
    (project / ".booley").mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(project)],
        capture_output=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    return project


def _statuses(ctx: InitContext) -> list[tuple[str, str]]:
    return [(result.name, result.status) for result in ctx.results]


def test_preserve_mode_reports_gitignore_backfill_without_appending(root: Path) -> None:
    gitignore = root / ".booley" / ".gitignore"
    gitignore.write_bytes(b"custom/\n")
    ctx = InitContext(project_root=root, preserve_tracked_content=True)

    init_cmd._backfill_project_gitignore(root / ".booley", ctx)

    assert gitignore.read_bytes() == b"custom/\n"
    assert _statuses(ctx) == [("booley_dir", "warn")]


def test_preserve_mode_still_creates_a_missing_gitignore(root: Path) -> None:
    ctx = InitContext(project_root=root, preserve_tracked_content=True)

    init_cmd._backfill_project_gitignore(root / ".booley", ctx)

    assert (root / ".booley" / ".gitignore").is_file()


def test_preserve_mode_reports_agent_insertion_into_existing_config(root: Path) -> None:
    config = root / ".booley" / "booley.toml"
    config.write_bytes(b"[project]\nname = 'x'\n")
    ctx = InitContext(project_root=root, preserve_tracked_content=True)

    assert init_cmd._step_agent_config(
        ctx, init_cmd.AgentSelection("claude", "auto", True, True), config
    )

    assert config.read_bytes() == b"[project]\nname = 'x'\n"
    assert _statuses(ctx) == [("agent_config", "warn")]


def test_preserve_mode_still_creates_a_missing_config(root: Path) -> None:
    config = root / ".booley" / "booley.toml"
    ctx = InitContext(project_root=root, preserve_tracked_content=True)

    assert init_cmd._step_agent_config(
        ctx, init_cmd.AgentSelection("claude", "auto", True, True), config
    )

    assert "[agent]" in config.read_text(encoding="utf-8")
    assert _statuses(ctx) == [("agent_config", "ok")]


def test_interactive_files_step_adds_excludes_once(root: Path) -> None:
    ctx = InitContext(project_root=root)

    init_cmd._step_interactive_files(ctx)
    init_cmd._step_interactive_files(ctx)

    exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/.devcontainer" in exclude
    assert _statuses(ctx) == [("interactive_files", "ok")]  # the rerun recorded nothing


def test_interactive_files_step_does_nothing_in_check_only(root: Path) -> None:
    ctx = InitContext(project_root=root, check_only=True)

    init_cmd._step_interactive_files(ctx)

    assert ctx.results == []
    assert "/.devcontainer" not in (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")


def test_interactive_files_step_records_authorization_failure(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_root: Path) -> Path:
        raise RuntimeSpecError("not authorized")

    monkeypatch.setattr(init_cmd, "authorized_project_data_source", refuse)
    ctx = InitContext(project_root=root)

    init_cmd._step_interactive_files(ctx)

    assert [(r.name, r.status, r.detail) for r in ctx.results] == [
        ("interactive_files", "err", "not authorized")
    ]


def test_interactive_files_step_warns_on_dashboard_diagnostics(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = dashboard_tasks.TaskPlan(diagnostics=("Dashboard task unavailable: busy",))
    monkeypatch.setattr(
        dashboard_tasks, "reconcile", lambda *_args: dashboard_tasks.TaskTransaction(plan)
    )
    ctx = InitContext(project_root=root)

    init_cmd._step_interactive_files(ctx)

    assert [(r.status, r.detail) for r in ctx.results if r.name == "interactive_files"] == [
        ("warn", "Dashboard task unavailable: busy")
    ]
