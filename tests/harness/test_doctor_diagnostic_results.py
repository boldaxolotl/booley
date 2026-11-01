"""Composition keeps report policy and effect ordering at Doctor's public entry."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.audit import host_environment
from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.config.host_config import SandboxHostPolicy
from booley.harness import doctor, host_diagnostics
from booley.harness.setup import readiness
from booley.runtime import doctor_stamp, session_admission
from tests.harness.test_doctor import _patch_environment, _write_project

# The exact host-wide Sandbox inventory listing owned by session_admission.
_INVENTORY_ARGV = [
    "docker",
    "ps",
    "--filter",
    "label=booley.role=interactive",
    "--format",
    "{{.ID}}\t{{.Names}}",
]


def _canonical_host_installation():
    return SimpleNamespace(
        version="0.2.15",
        revision="",
        payload_fingerprint="b" * 64,
        executable="/usr/local/bin/booley",
        interpreter="/usr/local/bin/python3",
        distribution_root="/opt/booley",
    )


def _use_real_host_checks(monkeypatch, root, booley_dir, bootstrap_result):
    """Run Doctor with the real host checks over a stubbed Bootstrap result."""
    inspect_host = host_diagnostics.inspect_host
    calls = _patch_environment(monkeypatch, root, booley_dir)
    monkeypatch.setattr(host_diagnostics, "inspect_host", inspect_host)
    monkeypatch.setattr(host_diagnostics, "load_host_installation", _canonical_host_installation)
    monkeypatch.setattr(
        host_diagnostics,
        "current_host_installation",
        lambda _source: _canonical_host_installation(),
    )
    monkeypatch.setattr(
        host_diagnostics.bootstrap, "reconcile_bootstrap", lambda _intent: bootstrap_result
    )
    monkeypatch.setattr(
        host_environment,
        "probe_host_clock",
        lambda: host_environment.EnvironmentFinding(
            host_environment.EnvironmentSeverity.SKIP, "offline"
        ),
    )
    return calls


def _sandbox_document(name: str, project_root: Path, *, editor: bool, egress: str | None) -> dict:
    """A running Booley Sandbox as `docker container inspect` reports it."""
    labels = {"booley.role": "interactive", "booley.project-id": f"id-{name}"}
    if editor:
        labels["devcontainer.local_folder"] = str(project_root)
    env = ["PATH=/usr/bin", "BOOLEY_CONTAINER=1"]
    if egress is not None:
        env.append(f"BOOLEY_EGRESS={egress}")
    return {
        "Id": f"id-{name}",
        "Name": f"/{name}",
        "Created": "2026-10-01T10:00:00Z",
        "State": {"Running": True, "StartedAt": "2026-10-01T10:00:01Z"},
        "Config": {"Labels": labels, "Env": env},
        "Mounts": [{"Type": "bind", "Source": str(project_root), "Destination": "/work"}],
    }


def _run_host_doctor(
    monkeypatch, root, *, egress: str, sandboxes: dict[str, dict], listing_fails: bool = False
):
    """Run the real Doctor reporter on the host with a fixture Sandbox inventory."""
    booley_dir = _write_project(root)
    bootstrap = host_diagnostics.bootstrap
    result = bootstrap.BootstrapResult(
        host_diagnostics.Intent.CHECK,
        (bootstrap.BootstrapFinding("docker", bootstrap.BootstrapState.CURRENT, "ok"),),
        policy=SandboxHostPolicy(egress=egress),
    )
    _use_real_host_checks(monkeypatch, root, booley_dir, result)
    fixture_run = subprocess.run
    inventory_calls = []

    def run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        if argv == _INVENTORY_ARGV:
            inventory_calls.append(argv)
            if listing_fails:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="daemon unavailable")
            listing = "".join(f"id-{name}\t{name}\n" for name in sandboxes)
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
        if argv[:3] == ["docker", "container", "inspect"] and argv[3].startswith("id-"):
            document = sandboxes[argv[3].removeprefix("id-")]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(document), stderr="")
        return fixture_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return doctor.run_doctor_result(argparse.Namespace(deep=False), root), inventory_calls


@pytest.mark.parametrize("egress", ["proxy", "unproxied"])
def test_host_doctor_reports_host_egress_mode(egress, tmp_path, monkeypatch):
    result, inventory_calls = _run_host_doctor(monkeypatch, tmp_path, egress=egress, sandboxes={})
    passed = [f.message for f in result.findings if f.severity == "pass"]
    assert f"host Sandbox egress mode: {egress}" in passed
    assert not [f for f in result.findings if f.check_id == "host.sandbox-egress-mismatch"]
    assert f"0 running Booley Sandbox(es) match host egress mode {egress}" in passed
    assert inventory_calls == [_INVENTORY_ARGV]


@pytest.mark.parametrize(
    ("host", "editor", "stale", "fix_commands"),
    [
        pytest.param(
            "unproxied",
            False,
            None,
            (
                ["booley", "session", "down", "--project"],
                ["booley", "init", "--seed", "--project"],
            ),
            id="host-unproxied-headless-proxy",
        ),
        pytest.param(
            "proxy",
            True,
            "unproxied",
            (["booley", "init", "--seed", "--project"],),
            id="host-proxy-editor-unproxied",
        ),
    ],
)
def test_host_doctor_warns_for_each_mismatched_running_sandbox(
    host, editor, stale, fix_commands, tmp_path, monkeypatch
):
    stale_root = tmp_path / "stale-project"
    current_root = tmp_path / "current-project"
    current_egress = "unproxied" if host == "unproxied" else None
    sandboxes = {
        "stale-sandbox": _sandbox_document(
            "stale-sandbox", stale_root, editor=editor, egress=stale
        ),
        "current-sandbox": _sandbox_document(
            "current-sandbox", current_root, editor=not editor, egress=current_egress
        ),
    }
    result, inventory_calls = _run_host_doctor(
        monkeypatch, tmp_path, egress=host, sandboxes=sandboxes
    )
    mismatches = [f for f in result.findings if f.check_id == "host.sandbox-egress-mismatch"]
    assert len(mismatches) == 1, result.findings
    finding = mismatches[0]
    sandbox_mode = "proxy" if stale is None else stale
    assert (finding.severity, finding.subject) == ("warn", str(stale_root.resolve()))
    assert finding.message == (
        f"Sandbox stale-sandbox (Project {stale_root.resolve()}) runs with egress "
        f"'{sandbox_mode}' but host [sandbox] egress = '{host}'"
    )
    # Each recreate command names the stale Project, quoted for the host shell
    # (POSIX shells or PowerShell on Windows).
    for command in fix_commands:
        assert session_admission._command([*command, str(stale_root.resolve())]) in finding.fix
    assert ("Rebuild Container" in finding.fix) is editor
    assert result.exit_code == 0
    assert inventory_calls == [_INVENTORY_ARGV]


def test_host_doctor_fails_when_sandbox_inventory_is_unreadable(tmp_path, monkeypatch):
    result, inventory_calls = _run_host_doctor(
        monkeypatch, tmp_path, egress="unproxied", sandboxes={}, listing_fails=True
    )
    failures = [
        f
        for f in result.findings
        if f.severity == "fail"
        and f.message.startswith("cannot check running Sandboxes against host egress mode")
    ]
    assert len(failures) == 1, result.findings
    assert "daemon unavailable" in failures[0].message
    assert result.exit_code != 0
    assert inventory_calls == [_INVENTORY_ARGV]


@pytest.mark.parametrize("waived", [False, True])
def test_pending_bootstrap_completes_with_waivable_warning_and_correct_health(
    waived, tmp_path, monkeypatch
):
    booley_dir = _write_project(tmp_path)
    bootstrap = host_diagnostics.bootstrap
    _use_real_host_checks(
        monkeypatch,
        tmp_path,
        booley_dir,
        bootstrap.BootstrapResult(
            host_diagnostics.Intent.CHECK,
            (
                bootstrap.BootstrapFinding("docker", bootstrap.BootstrapState.CURRENT, "ok"),
                bootstrap.BootstrapFinding(
                    "skills", bootstrap.BootstrapState.PENDING, "stale links"
                ),
            ),
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
    (booley_dir / "doctor-waivers.toml").write_text(waivers)
    result = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)
    finding = next(f for f in result.findings if f.check_id == "host.bootstrap-pending")
    assert (finding.severity, finding.subject) == ("waived" if waived else "warn", "skills")
    assert result.exit_code == 0
    assert result.clean is waived
    assert doctor_stamp.stamp_path(booley_dir).exists() is waived


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
        DiagnosticFinding(Severity.WARN, "first wording", "repair", "project.example", "target"),
    )


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_project_observations_and_repairs_keep_the_existing_order(
    tmp_path, monkeypatch, read_only, valid
):
    booley_dir = _write_project(tmp_path)
    (booley_dir / "AGENTS.md").write_text("# Project guidance\n")
    if not valid:
        (booley_dir / "booley.toml").unlink()
    _patch_environment(monkeypatch, tmp_path, booley_dir)
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
    ]
    if not valid:
        expected = ["config", "upgrade", "prune", "line-endings"]
    assert events == expected
    assert (tmp_path / "AGENTS.md").exists() is (valid and not read_only)
