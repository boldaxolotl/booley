"""Tests for host skill reconciliation during ``booley init``."""

from pathlib import Path

from booley.harness import init_skills
from booley.runtime.skill_links import SkillLinkReport


def test_host_reconciliation_returns_typed_target_reports(tmp_path: Path, monkeypatch):
    source = tmp_path / "packaged"
    target = tmp_path / "host" / "skills"
    report = SkillLinkReport()
    monkeypatch.setattr(init_skills, "_find_skill_targets", lambda: [target])
    monkeypatch.setattr(
        init_skills,
        "reconcile_skill_links",
        lambda *args, **kwargs: report,
    )

    results = init_skills.reconcile_host_skills(
        source,
        dry_run=True,
        allow_retarget=True,
    )

    assert results == (init_skills.HostSkillReconciliation(target, report),)
