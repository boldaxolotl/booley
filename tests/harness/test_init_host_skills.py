"""Tests for the ``[sandbox] mount_host_skills`` host-side skill resolver."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.runtime.platform_paths import docker_mount_path
from tests.conftest import require_symlinks


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True
) -> tuple[Path, Path, Path]:
    """Wire a fake $HOME + packaged-skills dir; return (project_root, home, builtin)."""
    home = tmp_path / "home"
    home.mkdir()
    builtin = tmp_path / "pkg" / "skills"
    builtin.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # The packaged skills dir is install layout, not an input a test can place.
    monkeypatch.setattr(init_cmd, "skills_dir", lambda: builtin)
    project_root = tmp_path / "proj"
    project_root.mkdir()
    toml = project_root / ".booley" / "booley.toml"
    toml.parent.mkdir()
    toml.write_bytes(b"[sandbox]\n" + (b"mount_host_skills = true\n" if enabled else b""))
    monkeypatch.setenv("BOOLEY_DIR", str(project_root / ".booley"))
    return project_root, home, builtin


def _make_skill(dir_: Path, name: str) -> Path:
    skill = dir_ / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"---\nname: x\n---\n")
    return skill


def _mounted(project_root: Path) -> list[tuple[str, str]]:
    # private: the only caller builds the Sandbox spec, which the public
    # current_interactive_spec previews through Docker.
    return init_cmd._resolve_host_skills_sources(project_root)


def test_host_skills_mount_each_real_personal_skill_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """~/.claude/skills is typically all symlinks: mount the resolved REAL dir, once."""
    require_symlinks(tmp_path)
    project_root, home, builtin = _setup(tmp_path, monkeypatch)
    claude = home / ".claude" / "skills"
    agents = home / ".agents" / "skills"
    claude.mkdir(parents=True)
    agents.mkdir(parents=True)
    linked = _make_skill(tmp_path / "real-skills", "deslop")
    personal = _make_skill(claude, "keeper")
    packaged = _make_skill(builtin, "booley-setup")
    claude_real = _make_skill(tmp_path / "c", "grill-me")
    foreign = _make_skill(tmp_path / "foreign", "booley-qa-triage")
    checkout = tmp_path / "Booley"
    (checkout / "src" / "booley").mkdir(parents=True)
    (checkout / "src" / "booley" / "__init__.py").write_bytes(b"")
    (checkout / "pyproject.toml").write_bytes(b"[tool.booley]\nsource_checkout = true\n")
    links = {
        "deslop": linked,
        # Links into the packaged skills dir are built-ins already in the image, whether
        # they carry the built-in's name or another one.
        "booley-setup": packaged,
        "renamed-builtin": packaged,
        "grill-me": claude_real,  # also in ~/.agents/skills: Claude's copy wins
        "dangling": tmp_path / "nowhere",
        # Booley's own QA skill is excluded; a foreign skill with a similar name is not.
        "booley-qa-run": _make_skill(checkout / "qa", "booley-qa-run"),
        "booley-qa-triage": foreign,
    }
    for name, target in links.items():
        (claude / name).symlink_to(target)
    (agents / "grill-me").symlink_to(_make_skill(tmp_path / "a", "grill-me"))
    (claude / "not-a-skill").mkdir()  # a real dir without SKILL.md

    expected = [
        ("booley-qa-triage", docker_mount_path(foreign.resolve())),
        ("deslop", docker_mount_path(linked.resolve())),
        ("grill-me", docker_mount_path(claude_real.resolve())),
        ("keeper", docker_mount_path(personal.resolve())),
    ]
    assert _mounted(project_root) == expected
    shutil.rmtree(agents)  # a host without ~/.agents/skills mounts the same skills
    assert _mounted(project_root) == expected


def test_host_skills_stay_unmounted_unless_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, home, _ = _setup(tmp_path, monkeypatch, enabled=False)
    _make_skill(home / ".claude" / "skills", "deslop")

    assert _mounted(project_root) == []
