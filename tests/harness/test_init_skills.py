"""Tests for system-level skill deployment performed by ``booley init``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from booley.harness import init_skills
from booley.harness.init_common import InitContext
from booley.runtime import paths as runtime_paths
from tests.conftest import require_symlinks


def _skill(root: Path, name: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
    return skill


def test_deploy_replaces_current_name_dangling_link(tmp_path: Path, monkeypatch):
    require_symlinks(tmp_path)
    old_skill = _skill(tmp_path / "old" / "skills", "booley-setup")
    packaged = tmp_path / "installed" / "skills"
    new_skill = _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(old_skill)
    old_skill.rename(tmp_path / "removed-skill")

    monkeypatch.setattr(runtime_paths, "skills_dir", lambda: packaged)
    monkeypatch.setattr(init_skills, "_find_skill_targets", lambda: [target])
    ctx = InitContext(project_root=tmp_path)

    init_skills._deploy_skills(ctx)

    assert link.resolve(strict=True) == new_skill.resolve()
    assert ctx.results[-1].status == "ok"


def test_deploy_records_link_creation_failure(tmp_path: Path, monkeypatch):
    packaged = tmp_path / "installed" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"

    monkeypatch.setattr(runtime_paths, "skills_dir", lambda: packaged)
    monkeypatch.setattr(init_skills, "_find_skill_targets", lambda: [target])
    monkeypatch.setattr(init_skills, "_make_junction_or_symlink", lambda _link, _target: False)
    ctx = InitContext(project_root=tmp_path)

    init_skills._deploy_skills(ctx)

    assert ctx.results[-1].status == "err"
    assert ctx.results[-1].detail == "1 link(s) failed"


def test_deploy_preserves_real_current_name_directory(tmp_path: Path, monkeypatch):
    packaged = tmp_path / "installed" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"
    existing = _skill(target, "booley-setup")

    monkeypatch.setattr(runtime_paths, "skills_dir", lambda: packaged)
    monkeypatch.setattr(init_skills, "_find_skill_targets", lambda: [target])
    ctx = InitContext(project_root=tmp_path)

    init_skills._deploy_skills(ctx)

    assert not existing.is_symlink()
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "# Test skill\n"
    assert ctx.results[-1].status == "err"


def test_deploy_preserves_healthy_link(tmp_path: Path, monkeypatch):
    require_symlinks(tmp_path)
    packaged = tmp_path / "installed" / "skills"
    skill = _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(skill)

    monkeypatch.setattr(runtime_paths, "skills_dir", lambda: packaged)
    monkeypatch.setattr(init_skills, "_find_skill_targets", lambda: [target])
    ctx = InitContext(project_root=tmp_path)

    init_skills._deploy_skills(ctx)

    assert link.resolve(strict=True) == skill.resolve()
    assert ctx.results[-1].status == "ok"


@pytest.mark.skipif(os.name != "nt", reason="requires NTFS junctions")
def test_prune_removes_current_name_dangling_windows_junction(tmp_path: Path):
    old_skill = _skill(tmp_path / "old" / "skills", "booley-setup")
    target = tmp_path / "host" / "skills"
    target.mkdir(parents=True)
    link = target / "booley-setup"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(old_skill)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    (old_skill / "SKILL.md").unlink()
    old_skill.rmdir()

    init_skills._prune_stale_skill_links(
        target,
        tmp_path / "installed" / "skills",
        {"booley-setup"},
    )

    assert not init_skills._exists_nofollow(link)
