"""Composition keeps report policy and effect ordering at Doctor's public entry."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from booley.audit import host_environment
from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.harness import doctor, doctor_stamp, host_diagnostics
from booley.harness.setup import readiness
from tests.harness.test_doctor import _patch_environment, _write_project


@pytest.mark.parametrize("waived", [False, True])
def test_pending_bootstrap_completes_with_waivable_warning_and_correct_health(
    waived, tmp_path, monkeypatch
):
    project_dir = _write_project(tmp_path)
    inspect_host = host_diagnostics.inspect_host
    _patch_environment(monkeypatch, tmp_path, project_dir)
    monkeypatch.setattr(host_diagnostics, "inspect_host", inspect_host)
    monkeypatch.setattr(
        host_diagnostics,
        "load_host_installation",
        lambda: SimpleNamespace(
            version="0.2.15",
            revision="",
            payload_fingerprint="b" * 64,
            executable="/usr/local/bin/booley",
            interpreter="/usr/local/bin/python3",
            distribution_root="/opt/booley",
        ),
    )
    monkeypatch.setattr(
        host_diagnostics,
        "current_host_installation",
        lambda _source: SimpleNamespace(
            version="0.2.15",
            revision="",
            payload_fingerprint="b" * 64,
            executable="/usr/local/bin/booley",
            interpreter="/usr/local/bin/python3",
            distribution_root="/opt/booley",
        ),
    )
    bootstrap = host_diagnostics.bootstrap
    result = bootstrap.BootstrapResult(
        host_diagnostics.Intent.CHECK,
        (
            bootstrap.BootstrapFinding("docker", bootstrap.BootstrapState.CURRENT, "ok"),
            bootstrap.BootstrapFinding("skills", bootstrap.BootstrapState.PENDING, "stale links"),
        ),
    )
    monkeypatch.setattr(bootstrap, "reconcile_bootstrap", lambda _intent: result)
    monkeypatch.setattr(
        host_environment,
        "probe_host_clock",
        lambda: host_environment.EnvironmentFinding(
            host_environment.EnvironmentSeverity.SKIP, "offline"
        ),
    )
    waivers = "version = 1\n"
    for check in ("fusesoc.worktree-core-shadow", "project.core-file-untracked"):
        waivers += (
            f'[[waiver]]\ncheck = "{check}"\n'
            'reason = "Synthetic project fixture"\npermanent = true\n'
        )
    if waived:
        waivers += (
            '[[waiver]]\ncheck = "host.bootstrap-pending"\n'
            'subject = "skills"\nreason = "Reviewed temporary gap"\npermanent = true\n'
        )
    (project_dir / "doctor-waivers.toml").write_text(waivers)
    result = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)
    finding = next(f for f in result.findings if f.check_id == "host.bootstrap-pending")
    assert (finding.severity, finding.subject) == ("waived" if waived else "warn", "skills")
    assert result.exit_code == 0
    assert result.clean is waived
    assert doctor_stamp.stamp_path(project_dir).exists() is waived


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_project_observations_and_repairs_keep_the_existing_order(
    tmp_path, monkeypatch, read_only, valid
):
    project_dir = _write_project(tmp_path)
    (project_dir / "AGENTS.md").write_text("# Project guidance\n")
    if not valid:
        (project_dir / "booley.toml").unlink()
    _patch_environment(monkeypatch, tmp_path, project_dir)
    events = []

    def trace(module, name, label):
        original = getattr(module, name)

        def run(*args, **kwargs):
            events.append(label)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, run)

    trace(readiness, "load_project", "config")
    trace(doctor, "_check_upgrade_review", "upgrade")
    trace(readiness, "check_guidance", "guidance")
    trace(doctor, "_check_project_data_destination_branch", "project-data-branch")
    trace(doctor, "_check_worktree_prune_guard", "prune")
    trace(doctor, "_check_line_endings", "line-endings")
    trace(doctor, "_check_worktree_core_shadow_guard", "shadow")
    trace(readiness, "check_stealth_cores", "projections")
    trace(doctor, "_check_board_orphans", "orphans")
    doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path, read_only=read_only)
    expected = [
        "config",
        "upgrade",
        "guidance",
        "project-data-branch",
        "prune",
        "line-endings",
        "shadow",
        "projections",
        "orphans",
    ]
    if not valid:
        expected = ["config", "upgrade", "prune", "line-endings", "orphans"]
    assert events == expected
    assert (tmp_path / "AGENTS.md").exists() is (valid and not read_only)


def test_typed_report_preserves_warning_deduplication_and_machine_fields():
    reporter = doctor._Reporter.create()
    first = DiagnosticFinding(
        Severity.WARN, "first wording", "repair", "project.example", "target", "same"
    )
    second = DiagnosticFinding(
        Severity.WARN, "new wording", "repair", "project.example", "target", "same"
    )
    reporter.diagnostics(DiagnosticReport((first, second)))
    result = reporter.result(0)
    assert result.counts["warn"] == 1
    assert result.findings == (
        doctor.DoctorFinding("warn", "first wording", "repair", "project.example", "target"),
    )
