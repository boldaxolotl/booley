"""Project Initialization runs anywhere: idempotent, with no Docker or host-private state."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from booley.fusesoc.core_projection import PROJECTED_CORE_GLOB
from booley.harness import init_cmd
from booley.harness.setup.common import InitContext, StepResult
from booley.runtime.booley_dir import init_booley_dir_scope, reset_cache
from tests.docker_cli_guard import assert_docker_cli_unused, forbid_docker_cli
from tests.subprocess_budget import run_bounded_child

CORE_TEXT = "CAPI=2:\nname: example:lib:cpu:1.0\n"
HOST_PRIVATE_ENVIRONMENT = (
    "HOME",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A git repo with a Booley guide and a Stealth core, host-private state absent."""
    root = tmp_path / "proj"
    root.mkdir()
    run_bounded_child(["git", "init", str(root)], check=True)
    (root / ".booley" / "cores").mkdir(parents=True)
    (root / ".booley" / "AGENTS.md").write_text("# Project guide\n", encoding="utf-8")
    (root / ".booley" / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
    (root / ".booley" / "cores" / "cpu.core").write_text(CORE_TEXT, encoding="utf-8")
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    # No docker CLI, and every host-private location points at a path that does not exist.
    forbid_docker_cli(monkeypatch)
    monkeypatch.setattr(init_cmd.shutil, "which", lambda _name: None)
    for name in HOST_PRIVATE_ENVIRONMENT:
        monkeypatch.setenv(name, str(tmp_path / "absent" / name))
    reset_cache()
    yield root
    assert_docker_cli_unused()


def _initialize(root: Path) -> list[StepResult]:
    """One Project Initialization run with Sandbox Git facts supplied by the caller."""
    ctx = InitContext(project_root=root, interactive=False, show_step_banners=False)
    config_path = root / ".booley" / "booley.toml"
    selection = init_cmd._resolve_agent_selection(ctx, init_cmd.argparse.Namespace(), config_path)
    assert selection is not None
    request = init_cmd.ProjectInitialization(
        selection, config_path, lambda: init_cmd.ProjectFacts((2, 50, 0))
    )
    with init_booley_dir_scope(root):
        assert init_cmd.initialize_project(ctx, request)
    return ctx.results


def _repository_state(root: Path) -> dict[str, object]:
    """Files with modes and local git config of both repositories; indexes excluded."""
    files = {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mode)
        for path in root.rglob("*")
        if path.is_file() and not (path.name == "index" and path.parent.name == ".git")
    }
    configs = {
        repository: run_bounded_child(
            ["git", "-C", str(root / repository), "config", "--local", "--list"],
            check=True,
        ).stdout
        for repository in (".", ".booley")
        if (root / repository / ".git").exists()
    }
    return {"files": files, "git_config": configs}


def test_second_project_initialization_changes_nothing(project: Path) -> None:
    first = _initialize(project)
    after_first = _repository_state(project)
    second = _initialize(project)

    assert any(result.status == "ok" for result in first)  # the first run really did work
    assert _repository_state(project) == after_first
    # Steady-state reports: these steps record `ok` even when they find nothing to do.
    assert [(result.name, result.status) for result in second if result.status != "current"] == [
        ("agent_config", "skip"),
        ("core_projections", "ok"),
        ("git_hooks", "skip"),
        ("line_endings", "ok"),
        ("guidance_links", "ok"),
    ]
    assert [result.detail for result in second if result.name == "core_projections"] == [
        "0 change(s)"
    ]


def test_project_initialization_runs_without_docker_or_host_private_state(
    project: Path, tmp_path: Path
) -> None:
    results = _initialize(project)

    assert [result for result in results if result.status == "err"] == []
    assert len(list(project.glob(PROJECTED_CORE_GLOB))) == 1  # the Stealth core was projected
    assert (project / "AGENTS.md").exists()  # the guidance link was created
    for name in HOST_PRIVATE_ENVIRONMENT:
        assert not (tmp_path / "absent" / name).exists()
