"""Readiness preserves useful findings when Project files cannot be inspected or repaired."""

import subprocess
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.fusesoc import core_projection
from booley.harness.setup import readiness
from tests.harness.test_doctor import _write_project


@pytest.mark.parametrize("filename", ["booley.toml", "configs.toml"])
def test_project_load_reports_malformed_toml_and_retains_resolved_directory(tmp_path, filename):
    project_dir = _write_project(tmp_path)
    (project_dir / filename).write_text("[broken")
    result = readiness.load_project(tmp_path)
    assert result.project is None
    assert result.project_dir == project_dir
    assert any(
        f.severity is Severity.FAIL and f"{filename} does not parse" in f.message
        for f in result.report.findings
    )


def test_project_load_reports_unreadable_configuration(tmp_path, monkeypatch):
    project_dir = _write_project(tmp_path)
    original = Path.open

    def open_file(path, *args, **kwargs):
        if path == project_dir / "booley.toml":
            raise PermissionError("unreadable project config")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    result = readiness.load_project(tmp_path)
    assert result.project is None
    assert any(
        f.severity is Severity.FAIL and "booley.toml unreadable" in f.message
        for f in result.report.findings
    )


def test_project_load_rejects_invalid_eda_before_constructing_continuation(tmp_path):
    project_dir = _write_project(tmp_path)
    with (project_dir / "booley.toml").open("a") as stream:
        stream.write("\n[eda.unknown]\n")
    result = readiness.load_project(tmp_path)
    assert result.project is None
    assert any(
        f.severity is Severity.FAIL and "unsupported EDA" in f.message
        for f in result.report.findings
    )


def test_project_load_keeps_legacy_target_fallback_when_no_core_is_available(tmp_path):
    _write_project(tmp_path)
    (tmp_path / "unit.core").unlink()
    result = readiness.load_project(tmp_path)
    assert result.project is not None
    assert result.project.first_target == "fast"


def test_guidance_reconciliation_is_idempotent_and_inspect_reports_current(tmp_path):
    project_dir = _write_project(tmp_path)
    (project_dir / "AGENTS.md").write_text("# Guidance\n")
    project = readiness.load_project(tmp_path).project
    assert project is not None
    first = readiness.check_guidance(project, mode=readiness.ReadinessMode.RECONCILE)
    paths = [tmp_path / name for name in ("AGENTS.md", "CLAUDE.md")]
    before = [(p.lstat().st_ino, p.lstat().st_mtime_ns, p.read_bytes()) for p in paths]
    second = readiness.check_guidance(project, mode=readiness.ReadinessMode.RECONCILE)
    assert first == second
    assert before == [(p.lstat().st_ino, p.lstat().st_mtime_ns, p.read_bytes()) for p in paths]
    inspected = readiness.check_guidance(project, mode=readiness.ReadinessMode.INSPECT)
    assert all(f.severity is Severity.PASS for f in inspected.findings)
    assert "root links current" in inspected.findings[0].message


def test_guidance_repair_preserves_conflicting_tracked_file(tmp_path):
    project_dir = _write_project(tmp_path)
    (project_dir / "AGENTS.md").write_text("canonical guidance\n")
    root_file = tmp_path / "AGENTS.md"
    root_file.write_text("tracked author guidance\n")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f", "AGENTS.md"], check=True, capture_output=True
    )
    project = readiness.load_project(tmp_path).project
    assert project is not None
    report = readiness.check_guidance(project, mode=readiness.ReadinessMode.RECONCILE)
    assert any(
        f.check_id == "guidance.links-unhealthy" and "could not create" in f.message
        for f in report.findings
    )
    assert root_file.read_text() == "tracked author guidance\n"


def test_guidance_read_error_retains_scope_warning_identity(tmp_path, monkeypatch):
    project_dir = _write_project(tmp_path)
    canon = project_dir / "AGENTS.md"
    canon.write_text("booley_status\n")
    project = readiness.load_project(tmp_path).project
    assert project is not None
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == canon:
            raise PermissionError("guidance denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    report = readiness.check_guidance(project, mode=readiness.ReadinessMode.INSPECT)
    scope = next(f for f in report.findings if f.check_id == "guidance.session-runtime-scope")
    assert scope.severity is Severity.WARN and scope.subject == str(canon)
    assert "could not read" in scope.message


def test_stealth_inspection_reports_stale_projection_then_reconcile_repairs_it(tmp_path):
    project_dir = _write_project(tmp_path)
    with (project_dir / "booley.toml").open("a") as stream:
        stream.write("\n[stealth]\nenabled = true\n")
    cores = project_dir / "cores"
    cores.mkdir()
    (cores / "hidden.core").write_text("CAPI=2:\nname: ::hidden:0\n")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    project = readiness.load_project(tmp_path).project
    assert project is not None
    first = readiness.check_stealth_cores(project, mode=readiness.ReadinessMode.INSPECT)
    failures = [f.message for f in first.findings if f.severity is Severity.FAIL]
    assert any("projections are not current" in message for message in failures)
    assert any("not excluded from host git status" in message for message in failures)
    projected = tmp_path / ".booley-projected-hidden.core"
    assert not projected.exists()
    repaired = readiness.check_stealth_cores(project, mode=readiness.ReadinessMode.RECONCILE)
    remaining = [f.message for f in repaired.findings if f.severity is Severity.FAIL]
    assert len(remaining) == 1 and "not excluded from host git status" in remaining[0]
    # Init owns the Git exclusion; Doctor repairs projections and reports this gap.
    (tmp_path / ".git/info/exclude").write_text(f"/{core_projection.PROJECTED_CORE_GLOB}\n")
    before = projected.read_bytes(), projected.stat().st_mtime_ns
    inspected = readiness.check_stealth_cores(project, mode=readiness.ReadinessMode.INSPECT)
    assert all(f.severity is Severity.PASS for f in inspected.findings)
    repeated = readiness.check_stealth_cores(project, mode=readiness.ReadinessMode.RECONCILE)
    assert repeated == inspected
    assert (projected.read_bytes(), projected.stat().st_mtime_ns) == before
