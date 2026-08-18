"""Tests for ``_resolve_host_skills_sources`` — the [sandbox] mount_host_skills
host-side resolver that turns the user's global skill dirs into read-only binds.
"""

from __future__ import annotations

from pathlib import Path

from booley.harness import init_cmd
from tests.conftest import require_symlinks


def _write_toml(project_root: Path, *, enabled: bool) -> None:
    toml = project_root / ".booley_project" / "booley.toml"
    toml.parent.mkdir(parents=True, exist_ok=True)
    body = "[sandbox]\n"
    if enabled:
        body += "mount_host_skills = true\n"
    toml.write_text(body, encoding="utf-8")


def _make_skill(dir_: Path, name: str) -> Path:
    """Create a real skill dir (with SKILL.md) and return it."""
    skill = dir_ / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    return skill


def _setup(tmp_path, monkeypatch, *, enabled=True):
    """Wire a fake $HOME + packaged-skills dir; return (project_root, home)."""
    home = tmp_path / "home"
    home.mkdir()
    builtin = tmp_path / "pkg" / "skills"
    builtin.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(init_cmd, "skills_dir", lambda: builtin)
    project_root = tmp_path / "proj"
    project_root.mkdir()
    _write_toml(project_root, enabled=enabled)
    return project_root, home, builtin


def test_disabled_returns_empty(tmp_path, monkeypatch):
    project_root, home, _ = _setup(tmp_path, monkeypatch, enabled=False)
    _make_skill(home / ".claude" / "skills", "deslop")
    assert init_cmd._resolve_host_skills_sources(project_root) == []


def test_resolves_symlink_to_real_dir(tmp_path, monkeypatch):
    # ~/.claude/skills is typically all symlinks; the mount source must be the
    # resolved REAL dir, else it dangles in the container.
    require_symlinks(tmp_path)
    project_root, home, _ = _setup(tmp_path, monkeypatch)
    real = _make_skill(tmp_path / "real-skills", "deslop")
    claude = home / ".claude" / "skills"
    claude.mkdir(parents=True)
    (claude / "deslop").symlink_to(real)

    pairs = init_cmd._resolve_host_skills_sources(project_root)
    assert pairs == [("deslop", str(real.resolve()))]


def test_excludes_builtins_by_path_and_name(tmp_path, monkeypatch):
    require_symlinks(tmp_path)
    project_root, home, builtin = _setup(tmp_path, monkeypatch)
    # A host symlink INTO the packaged skills dir = a built-in already in the image.
    _make_skill(builtin, "booley-setup")
    claude = home / ".claude" / "skills"
    claude.mkdir(parents=True)
    (claude / "booley-setup").symlink_to(builtin / "booley-setup")
    # A genuine personal skill alongside it still comes through.
    _make_skill(claude, "keeper")

    pairs = dict(init_cmd._resolve_host_skills_sources(project_root))
    assert "booley-setup" not in pairs  # excluded: resolves under the packaged dir
    assert "keeper" in pairs


def test_dedupes_by_name_across_both_dirs(tmp_path, monkeypatch):
    # Same skill name in ~/.claude/skills and ~/.agents/skills -> Claude wins.
    require_symlinks(tmp_path)
    project_root, home, _ = _setup(tmp_path, monkeypatch)
    claude_real = _make_skill(tmp_path / "c", "grill-me")
    agents_real = _make_skill(tmp_path / "a", "grill-me")
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".claude" / "skills" / "grill-me").symlink_to(claude_real)
    (home / ".agents" / "skills" / "grill-me").symlink_to(agents_real)

    pairs = init_cmd._resolve_host_skills_sources(project_root)
    assert pairs == [("grill-me", str(claude_real.resolve()))]


def test_skips_dangling_and_non_skill_dirs(tmp_path, monkeypatch):
    require_symlinks(tmp_path)
    project_root, home, _ = _setup(tmp_path, monkeypatch)
    claude = home / ".claude" / "skills"
    claude.mkdir(parents=True)
    (claude / "dangling").symlink_to(tmp_path / "nowhere")  # dangling
    (claude / "not-a-skill").mkdir()  # real dir, no SKILL.md
    _make_skill(claude, "real")

    pairs = init_cmd._resolve_host_skills_sources(project_root)
    assert [n for n, _ in pairs] == ["real"]
