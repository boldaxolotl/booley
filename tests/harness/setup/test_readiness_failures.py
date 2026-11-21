"""Readiness preserves useful findings when Project files cannot be inspected or repaired."""

import subprocess
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.fusesoc import core_projection
from booley.harness.setup import readiness
from booley.harness.setup.guidance_links import ensure_guidance_links
from tests.harness.test_doctor import _write_project


@pytest.mark.parametrize("filename", ["booley.toml", "configs.toml"])
def test_project_load_reports_malformed_toml_and_retains_resolved_directory(tmp_path, filename):
    booley_dir = _write_project(tmp_path)
    (booley_dir / filename).write_text("[broken")
    result = readiness.load_project(tmp_path)
    assert result.project is None
    assert result.booley_dir == booley_dir
    assert any(
        f.severity is Severity.FAIL and f"{filename} does not parse" in f.message
        for f in result.report.findings
    )


def test_project_load_reports_unreadable_configuration(tmp_path, monkeypatch):
    booley_dir = _write_project(tmp_path)
    original = Path.open

    def open_file(path, *args, **kwargs):
        if path == booley_dir / "booley.toml":
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
    booley_dir = _write_project(tmp_path)
    with (booley_dir / "booley.toml").open("a") as stream:
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


def test_guidance_links_made_by_init_grade_as_current(tmp_path):
    booley_dir = _write_project(tmp_path)
    (booley_dir / "AGENTS.md").write_text("# Guidance\n")
    ensure_guidance_links(tmp_path, booley_dir)
    project = readiness.load_project(tmp_path).project
    assert project is not None
    inspected = readiness.check_guidance(project)
    assert all(f.severity is Severity.PASS for f in inspected.findings)
    assert "root links current" in inspected.findings[0].message


def test_guidance_read_error_retains_scope_warning_identity(tmp_path, monkeypatch):
    booley_dir = _write_project(tmp_path)
    canon = booley_dir / "AGENTS.md"
    canon.write_text("booley_status\n")
    project = readiness.load_project(tmp_path).project
    assert project is not None
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == canon:
            raise PermissionError("guidance denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    report = readiness.check_guidance(project)
    scope = next(f for f in report.findings if f.check_id == "guidance.session-runtime-scope")
    assert scope.severity is Severity.WARN and scope.subject == str(canon)
    assert "could not read" in scope.message


def test_stealth_inspection_reports_stale_projection_without_creating_it(tmp_path):
    booley_dir = _write_project(tmp_path)
    with (booley_dir / "booley.toml").open("a") as stream:
        stream.write("\n[stealth]\nenabled = true\n")
    cores = booley_dir / "cores"
    cores.mkdir()
    (cores / "hidden.core").write_text("CAPI=2:\nname: ::hidden:0\n")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    project = readiness.load_project(tmp_path).project
    assert project is not None
    first = readiness.check_stealth_cores(project)
    failures = [f.message for f in first.findings if f.severity is Severity.FAIL]
    assert [m for m in failures if "stealth core projections" in m] == [
        "stealth core projections are not excluded from host git status"
    ]
    assert [f.check_id for f in first.findings if f.severity is Severity.WARN] == [
        "stealth.projections-not-current"
    ]
    projected = tmp_path / ".booley-projected-hidden.core"
    assert not projected.exists()
    assert readiness.check_stealth_cores(project) == first
    assert not projected.exists()
    assert not readiness.stealth_projections_current(project)
    # Init owns both repairs: it reconciles the projections and adds the Git exclusion.
    core_projection.reconcile_projected_cores(tmp_path)
    (tmp_path / ".git/info/exclude").write_text(f"/{core_projection.PROJECTED_CORE_GLOB}\n")
    before = projected.read_bytes(), projected.stat().st_mtime_ns
    inspected = readiness.check_stealth_cores(project)
    assert all(f.severity is Severity.PASS for f in inspected.findings)
    assert readiness.stealth_projections_current(project)
    assert (projected.read_bytes(), projected.stat().st_mtime_ns) == before


def test_non_utf8_stealth_core_fails_as_unreadable(tmp_path):
    booley_dir = _write_project(tmp_path)
    with (booley_dir / "booley.toml").open("a") as stream:
        stream.write("\n[stealth]\nenabled = true\n")
    core = booley_dir / "cores" / "a.core"
    core.parent.mkdir()
    core.write_bytes(b"CAPI=2:\nname: ::a:\xff\n")
    project = readiness.load_project(tmp_path).project
    assert project is not None
    report = readiness.check_stealth_cores(project)
    unreadable = (
        f"could not inspect stealth core projections: could not read authoritative core {core}: "
    )
    assert [f.fix for f in report.findings if f.message.startswith(unreadable)] == [
        "fix the authored core, then run any Booley Flow to refresh the projections, or run booley init"
    ]


def test_uninspectable_projections_are_not_current(tmp_path, monkeypatch):
    _write_project(tmp_path)
    project = readiness.load_project(tmp_path).project
    assert project is not None
    assert readiness.stealth_projections_current(project)  # stealth is off: nothing to project

    def broken(_root):
        raise core_projection.CoreProjectionError("conflict")

    # Patched: no public entry point makes Booley's own projection inspection raise.
    monkeypatch.setattr(core_projection, "projection_enabled", lambda _root: True)
    monkeypatch.setattr(core_projection, "projection_issues", broken)
    assert not readiness.stealth_projections_current(project)


@pytest.mark.parametrize(
    "settings",
    [
        '[notifications]\nntfy_topic = "probe"\nevents = ["done"]',
        "[notifications]\nntfy_topic = 42\nevents = false",
    ],
)
def test_legacy_notifications_do_not_block_project_readiness(tmp_path, settings):
    booley_dir = _write_project(tmp_path)
    config = booley_dir / "booley.toml"
    baseline = readiness.load_project(tmp_path)
    config.write_text(config.read_text() + "\n" + settings)
    result = readiness.load_project(tmp_path)
    assert result.project is not None
    assert [f for f in result.report.findings if f.severity is Severity.FAIL] == [
        f for f in baseline.report.findings if f.severity is Severity.FAIL
    ]
    warning = next(f for f in result.report.findings if f.subject == "notifications")
    assert warning.severity is Severity.WARN
    assert "ignored" in warning.message
