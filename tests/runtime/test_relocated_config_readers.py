"""Configuration readers follow a relocated Booley Directory (#2918).

A checkout's root ``booley.toml`` may set a relative ``[project].dir``. Every reader
then takes its configuration from that directory, in the main checkout and in a
worktree made by ``booley worktree new``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import pytest
from tests.subprocess_budget import run_bounded_child

from booley.commit_policy import policy
from booley.core.booley_dir import own_booley_dir
from booley.dev_support import validate_commit_msg
from booley.runtime.project_discovery import _is_configured_booley_dir

_posix_git = pytest.mark.skipif(
    shutil.which("git") is None or os.name == "nt",
    reason="needs git and the POSIX worktree script",
)

_STATE_TOML = """\
[project]
name = "{name}"

[submodules]
paths = []

[flows.lint]
enabled = {lint}

[stealth]
enabled = {stealth}

[agent]
clis = {clis}
"""


def _git(cwd: Path, *args: str) -> None:
    result = run_bounded_child(["git", *args], cwd=cwd)
    assert result.returncode == 0, result.stderr


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture
def relocated_checkouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A main checkout and a ``booley worktree new`` worktree, both with ``dir = "state"``."""
    from booley.harness import worktree_cmd

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init",),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.com"),
    ):
        _git(root, *args)
    _write(root / "README.md", "hello\n")
    _write(root / "booley.toml", '[project]\ndir = "state"\n')
    _git(root, "add", "README.md", "booley.toml")
    _git(root, "commit", "-m", "init")
    state = root / "state"
    _write(
        state / "booley.toml",
        _STATE_TOML.format(
            name="main-name", lint="false", stealth="false", clis='["claude", "codex"]'
        ),
    )
    _write(state / "tests.toml", "[tests]\n")
    _write(state / "rtl_style_guide.md", "relocated style\n")
    _write(state / "AGENTS.md", "guidance\n")
    assert worktree_cmd.run(argparse.Namespace(worktree_command="new", name="demo"), root) == 0
    worktree = state / "worktrees" / "demo"
    _write(
        worktree / "state" / "booley.toml",
        _STATE_TOML.format(name="worktree-name", lint="true", stealth="true", clis='["codex"]'),
    )
    return {"main": root, "worktree": worktree}


@_posix_git
@pytest.mark.parametrize("checkout", ["main", "worktree"])
def test_configuration_readers_use_relocated_booley_dir(
    relocated_checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch, checkout: str
) -> None:
    from booley.config import agent, settings
    from booley.config.flow_enablement import flow_enabled
    from booley.core.config_paths import resolve_booley_toml
    from booley.flows.sim.record import flow_planning
    from booley.harness.booley import _discover_project_mcp_tools
    from booley.runtime import shared_infra

    root = relocated_checkouts[checkout]
    name = f"{checkout}-name"
    state = root / "state"

    assert resolve_booley_toml(root) == state / "booley.toml"
    assert settings._load_booley_toml(root)["project"]["name"] == name
    is_main = checkout == "main"
    assert flow_enabled("lint", root) is not is_main
    rtl_config = shared_infra._load_rtl_config(root)
    assert rtl_config is not None and rtl_config["project"]["name"] == name
    monkeypatch.setattr(agent, "_agent_settings", None)
    assert agent.load_agent_settings(root).clis == (("claude", "codex") if is_main else ("codex",))
    suite = flow_planning._required_suite(root, ["t"])
    assert suite["source_path"] == "state/tests.toml"
    assert policy.stealth_enabled(root) is not is_main
    assert validate_commit_msg._has_project_config(root)
    assert validate_commit_msg._has_project_config(state)

    discovered: list[Path | None] = []

    def fake_discover(*, project_mcp_tools_dir: Path | None = None, **_: object) -> list[object]:
        discovered.append(project_mcp_tools_dir)
        return []

    monkeypatch.setattr("booley.mcp.registry.discover_mcp_tools", fake_discover)
    assert _discover_project_mcp_tools(root) == []
    assert discovered == [state / "mcp_tools"]


@pytest.mark.parametrize(
    ("toml", "expected"),
    [
        (None, ".booley"),
        ("[project]\nname = 'x'\n", ".booley"),
        ("[project]\ndir = 'state'\n", "state"),
        ("[project]\ndir = 'a/../b'\n", "b"),
        ("[project]\ndir = '.'\n", "."),
        ("[project]\ndir = '/elsewhere'\n", "/elsewhere"),
        ("[project\n", ".booley"),
        ("[project]\ndir = 3\n", ".booley"),
        ("[project]\ndir = ''\n", ".booley"),
    ],
)
def test_stdlib_booley_dir_matches_own_booley_dir(
    tmp_path: Path, toml: str | None, expected: str
) -> None:
    if toml is not None:
        _write(tmp_path / "booley.toml", toml)

    assert policy.checkout_booley_dir(tmp_path) == own_booley_dir(tmp_path)
    assert policy.checkout_booley_dir(tmp_path) == Path(os.path.normpath(tmp_path / expected))


def test_stdlib_booley_dir_tolerates_undecodable_configuration(tmp_path: Path) -> None:
    (tmp_path / "booley.toml").write_bytes(b"\xff\xfe[project]\n")

    assert policy.checkout_booley_dir(tmp_path) == tmp_path / ".booley"
    assert own_booley_dir(tmp_path) == tmp_path / ".booley"


def test_stdlib_booley_dir_without_tomllib_is_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "booley.toml", '[project]\ndir = "state"\n')
    monkeypatch.setitem(sys.modules, "tomllib", None)

    assert policy.checkout_booley_dir(tmp_path) == tmp_path / ".booley"


def test_booley_dir_repository_matches_project_discovery(tmp_path: Path) -> None:
    _write(tmp_path / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(tmp_path / "booley.toml", '[project]\ndir = "cfg/state"\n')
    _write(tmp_path / "cfg" / "booley.toml", "[project]\n")
    selected = tmp_path / "cfg" / "state"
    sibling = tmp_path / "cfg"
    selected.mkdir(parents=True)

    assert policy.is_booley_dir_repository(selected) is True
    assert policy.is_booley_dir_repository(sibling) is False
    assert _is_configured_booley_dir(selected) is True
    assert _is_configured_booley_dir(sibling) is False
    assert policy.is_booley_dir_repository(tmp_path / ".booley") is True


def test_git_marker_accepts_a_gitdir_file_and_rejects_empty_sentinels(tmp_path: Path) -> None:
    linked, empty, unreadable = tmp_path / "linked", tmp_path / "empty", tmp_path / "bare"
    _write(linked / ".git", "gitdir: /elsewhere\n")
    (empty / ".git").mkdir(parents=True)
    unreadable.mkdir()

    assert policy._has_git_marker(linked) is True
    assert policy._has_git_marker(empty) is False
    assert policy._has_git_marker(unreadable) is False


def test_relocated_versioned_booley_dir_loads_policy_from_its_own_repository(
    tmp_path: Path,
) -> None:
    _write(tmp_path / ".git", "gitdir: /elsewhere\n")
    _write(tmp_path / "booley.toml", '[project]\ndir = "state"\n')
    _write(tmp_path / "state" / "booley.toml", "[stealth]\nenabled = false\n")

    assert policy.stealth_enabled(tmp_path / "state") is False


@_posix_git
def test_project_inventory_discovers_relocated_project(
    relocated_checkouts: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.projects import inventory

    root = relocated_checkouts["main"]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert root in inventory.discover_projects((tmp_path,))
    assert inventory._status(root) is inventory.ProjectStatus.PRESENT
    assert inventory.remember_project(root) == root.resolve()


def test_inventory_ignores_booley_dir_outside_the_checkout(tmp_path: Path) -> None:
    from booley.projects import inventory

    for index, value in enumerate((".", (tmp_path / "elsewhere").as_posix())):
        project = tmp_path / f"p{index}"
        _write(project / "booley.toml", f'[project]\ndir = "{value}"\n')
        (project / ".git").mkdir()

        assert inventory._has_booley_dir(project) is False
        assert inventory._is_initialized_checkout(project) is False


def test_required_suite_reports_booley_dir_outside_the_checkout(tmp_path: Path) -> None:
    from booley.flows.sim.record import flow_planning
    from booley.flows.sim.record.codec import SimulationRecordIntegrityError

    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "tests.toml", "[tests]\n")
    project = tmp_path / "repo"
    _write(project / "booley.toml", f'[project]\ndir = "{elsewhere.as_posix()}"\n')

    with pytest.raises(SimulationRecordIntegrityError, match=r"no readable tests\.toml"):
        flow_planning._required_suite(project, ["t"])
