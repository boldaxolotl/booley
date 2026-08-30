"""Tests for host skill reconciliation performed by ``booley init``."""

from __future__ import annotations

from pathlib import Path

from booley.harness import init_skills
from booley.harness.init_common import InitContext
from booley.runtime import paths as runtime_paths
from booley.runtime.skill_links import SkillLinkEvent, SkillLinkReport
from tests.conftest import require_symlinks


def _skill(root: Path, name: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
    return skill


def _deploy_from(
    tmp_path: Path,
    monkeypatch,
    packaged: Path,
    target: Path,
    *,
    check_only: bool = False,
) -> InitContext:
    monkeypatch.setattr(runtime_paths, "skills_dir", lambda: packaged)
    monkeypatch.setattr(init_skills, "_find_skill_targets", lambda: [target])
    ctx = InitContext(project_root=tmp_path, check_only=check_only)
    init_skills._deploy_skills(ctx)
    return ctx


def test_deploy_preserves_unrecorded_legacy_dangling_link(tmp_path: Path, monkeypatch):
    require_symlinks(tmp_path)
    old_skill = _skill(tmp_path / "old" / "booley" / "data" / "skills", "booley-setup")
    packaged = tmp_path / "installed" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(old_skill)
    old_skill.rename(tmp_path / "removed-skill")
    original_target = link.readlink()

    ctx = _deploy_from(tmp_path, monkeypatch, packaged, target)

    assert link.readlink() == original_target
    assert ctx.results[-1].status == "err"


def test_deploy_preserves_unrelated_current_name_dangling_link(tmp_path: Path, monkeypatch):
    require_symlinks(tmp_path)
    packaged = tmp_path / "installed" / "booley" / "data" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"
    target.mkdir(parents=True)
    user_skill = tmp_path / "temporarily-unmounted-team-skills" / "booley-setup"
    link = target / "booley-setup"
    link.symlink_to(user_skill)
    original_target = link.readlink()

    ctx = _deploy_from(tmp_path, monkeypatch, packaged, target)

    assert link.readlink() == original_target
    assert ctx.results[-1].status == "err"


def test_deploy_records_reconciliation_error(tmp_path: Path, monkeypatch):
    packaged = tmp_path / "installed" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"
    event = SkillLinkEvent(
        "booley-setup",
        "error",
        "packaged",
        target / "booley-setup",
        desired_target=str(packaged / "booley-setup"),
        detail="junction failed",
    )
    monkeypatch.setattr(
        init_skills,
        "reconcile_skill_links",
        lambda *_args, **_kwargs: SkillLinkReport(events=(event,)),
    )

    ctx = _deploy_from(tmp_path, monkeypatch, packaged, target)

    assert ctx.results[-1].status == "err"
    assert ctx.results[-1].detail == "1 reconciliation issue(s)"


def test_deploy_records_report_diagnostic(tmp_path: Path, monkeypatch):
    packaged = tmp_path / "installed" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"
    monkeypatch.setattr(
        init_skills,
        "reconcile_skill_links",
        lambda *_args, **_kwargs: SkillLinkReport(diagnostics=("manifest write failed",)),
    )

    ctx = _deploy_from(tmp_path, monkeypatch, packaged, target)

    assert ctx.results[-1].status == "err"


def test_deploy_preserves_real_current_name_directory(tmp_path: Path, monkeypatch):
    packaged = tmp_path / "installed" / "skills"
    _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"
    existing = _skill(target, "booley-setup")

    ctx = _deploy_from(tmp_path, monkeypatch, packaged, target)

    assert not existing.is_symlink()
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "# Test skill\n"
    assert ctx.results[-1].status == "err"


def test_deploy_adopts_healthy_link(tmp_path: Path, monkeypatch):
    require_symlinks(tmp_path)
    packaged = tmp_path / "installed" / "skills"
    skill = _skill(packaged, "booley-setup")
    target = tmp_path / "host" / "skills"
    target.mkdir(parents=True)
    link = target / "booley-setup"
    link.symlink_to(skill)

    ctx = _deploy_from(tmp_path, monkeypatch, packaged, target)

    assert link.resolve(strict=True) == skill.resolve()
    assert ctx.results[-1].status == "ok"


def test_check_only_does_not_create_default_agent_directory(tmp_path: Path, monkeypatch):
    packaged = tmp_path / "installed" / "skills"
    _skill(packaged, "booley-setup")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(runtime_paths, "skills_dir", lambda: packaged)
    ctx = InitContext(project_root=tmp_path, check_only=True)

    init_skills._deploy_skills(ctx)

    assert not (tmp_path / ".agents").exists()
    assert ctx.results[-1].status == "warn"
