"""Tests for host skill reconciliation during ``booley init``."""

from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.harness.setup import skills as init_skills
from booley.harness.setup.common import InitContext
from booley.runtime import paths as runtime_paths
from booley.runtime.skill_links import SkillLinkEvent, SkillLinkReport


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


def test_init_cmd_reexports_skill_deployment_compatibility_adapter() -> None:
    assert init_cmd._deploy_skills is init_skills._deploy_skills


def test_skill_deployment_adapter_records_reconciliation_failures(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "packaged"
    source.mkdir()
    target = tmp_path / "host" / "skills"
    event = SkillLinkEvent("booley-setup", "error", "packaged", target)
    report = SkillLinkReport(events=(event,), diagnostics=("manifest failed",))
    monkeypatch.setattr(runtime_paths, "skills_dir", lambda: source)
    monkeypatch.setattr(
        init_skills,
        "reconcile_host_skills",
        lambda *_args, **_kwargs: (init_skills.HostSkillReconciliation(target, report),),
    )
    ctx = InitContext(project_root=tmp_path, show_step_banners=False)

    init_skills._deploy_skills(ctx)

    assert ctx.results[-1].status == "err"
    assert ctx.results[-1].detail == "2 reconciliation issue(s)"


def test_skill_deployment_adapter_reports_missing_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runtime_paths, "skills_dir", lambda: tmp_path / "missing")
    ctx = InitContext(project_root=tmp_path, show_step_banners=False)

    init_skills._deploy_skills(ctx)

    assert ctx.results[-1].status == "warn"
    assert ctx.results[-1].detail == "skills dir missing"


@pytest.mark.parametrize(
    ("check_only", "status", "detail"),
    [
        (True, "warn", "checked 0 target(s)"),
        (False, "ok", "deployed to 0 target(s)"),
    ],
)
def test_skill_deployment_adapter_records_success_modes(
    tmp_path: Path,
    monkeypatch,
    check_only: bool,
    status: str,
    detail: str,
) -> None:
    source = tmp_path / "packaged"
    source.mkdir()
    monkeypatch.setattr(runtime_paths, "skills_dir", lambda: source)
    monkeypatch.setattr(init_skills, "reconcile_host_skills", lambda *_args, **_kwargs: ())
    ctx = InitContext(
        project_root=tmp_path,
        check_only=check_only,
        show_step_banners=False,
    )

    init_skills._deploy_skills(ctx)

    assert ctx.results[-1].status == status
    assert ctx.results[-1].detail == detail
