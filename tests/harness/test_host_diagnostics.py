"""Host diagnostics use Bootstrap observations without initiating preparation."""

import subprocess
from types import SimpleNamespace

import pytest

from booley.audit import host_environment
from booley.audit.diagnostic_results import Severity
from booley.harness import host_diagnostics
from booley.runtime import runtime_context


@pytest.mark.parametrize(
    "state, severity",
    [("CURRENT", Severity.PASS), ("PENDING", Severity.WARN), ("ERROR", Severity.FAIL)],
)
def test_host_bootstrap_states_are_complete_typed_findings(monkeypatch, state, severity):
    bootstrap = host_diagnostics.bootstrap
    calls = []

    def reconcile(intent):
        calls.append(intent)
        finding = bootstrap.BootstrapFinding("docker", bootstrap.BootstrapState[state], "observed")
        return bootstrap.BootstrapResult(intent, (finding,))

    monkeypatch.setattr(bootstrap, "reconcile_bootstrap", reconcile)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    identity = SimpleNamespace(
        version="1.2.3",
        revision="abc123",
        payload_fingerprint="f" * 64,
        executable="/usr/local/bin/booley",
        interpreter="/usr/local/bin/python3",
        distribution_root="/opt/booley",
    )
    monkeypatch.setattr(host_diagnostics, "load_host_installation", lambda: identity)
    monkeypatch.setattr(host_diagnostics, "current_host_installation", lambda _source: identity)
    monkeypatch.setattr(host_diagnostics.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        host_environment,
        "probe_host_clock",
        lambda: host_environment.EnvironmentFinding(
            host_environment.EnvironmentSeverity.SKIP, "offline"
        ),
    )
    result = host_diagnostics.inspect_host()
    finding = next(f for f in result.report.findings if "Host Bootstrap docker" in f.message)
    assert finding.severity is severity
    assert calls == [host_diagnostics.Intent.CHECK]
    assert result.docker_exe == ("/usr/bin/docker" if state == "CURRENT" else None)
    if state == "PENDING":
        assert (finding.check_id, finding.subject) == ("host.bootstrap-pending", "docker")


def test_in_runtime_host_diagnosis_does_not_prepare_or_probe_host(monkeypatch):
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(
        host_diagnostics.bootstrap,
        "reconcile_bootstrap",
        lambda *_a: pytest.fail("host preparation"),
    )
    monkeypatch.setattr(
        host_environment, "probe_host_clock", lambda: pytest.fail("host clock probe")
    )
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("external command"))
    result = host_diagnostics.inspect_host()
    assert result.docker_exe is None
    assert any(
        f.severity is Severity.SKIP and "inside" in f.message for f in result.report.findings
    )


def test_host_diagnosis_reports_canonical_installation(monkeypatch):
    report = host_diagnostics.Findings()
    identity = SimpleNamespace(
        version="1.2.3",
        revision="abc123",
        payload_fingerprint="abcdef0123456789",
        executable="/usr/local/bin/booley",
        interpreter="/usr/local/bin/python3",
        distribution_root="/opt/booley",
    )
    monkeypatch.setattr(
        host_diagnostics,
        "load_host_installation",
        lambda: identity,
    )
    monkeypatch.setattr(host_diagnostics, "current_host_installation", lambda _source: identity)

    host_diagnostics._inspect_host_installation(report)

    finding = report.report().findings[0]
    assert "v1.2.3 revision=abc123 fingerprint=abcdef012345" in finding.message
    assert "/opt/booley" in finding.message
    assert "revision=abc123" in finding.message
    assert "executable=/usr/local/bin/booley" in finding.message


def test_host_diagnosis_rejects_mismatched_current_installation(monkeypatch):
    identity = SimpleNamespace(
        version="1.2.3",
        revision="abc123",
        payload_fingerprint="f" * 64,
        executable="/usr/local/bin/booley",
        interpreter="/usr/local/bin/python3",
        distribution_root="/opt/booley",
    )
    actual = SimpleNamespace(**{**identity.__dict__, "distribution_root": "/tmp/booley"})
    report = host_diagnostics.Findings()
    monkeypatch.setattr(host_diagnostics, "load_host_installation", lambda: identity)
    monkeypatch.setattr(host_diagnostics, "current_host_installation", lambda _source: actual)

    host_diagnostics._inspect_host_installation(report)

    finding = report.report().findings[0]
    assert finding.severity is Severity.FAIL
    assert "--upgrade-installation" in finding.fix


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize(
    "directory, executable", [(False, None), (True, None), (False, "/bin/agent")]
)
def test_agent_detection_uses_config_or_executable(
    tmp_path, monkeypatch, provider, directory, executable
):
    monkeypatch.setattr(host_environment.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(host_environment.shutil, "which", lambda _name: executable)
    if directory:
        (tmp_path / f".{provider}").mkdir()
    result = host_environment.inspect_agent_installation(provider)
    assert result.installed is (directory or executable is not None)
    assert result.config_present is directory
    assert result.executable == executable


@pytest.mark.parametrize(
    "severity",
    [host_environment.EnvironmentSeverity.FAIL, host_environment.EnvironmentSeverity.WARN],
)
def test_host_diagnostics_preserves_environment_failures_and_warning_identity(
    monkeypatch, severity
):
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    observation = host_environment.EnvironmentFinding(
        severity,
        "legacy package observation",
        "remove legacy package",
        "host.legacy-package" if severity is host_environment.EnvironmentSeverity.WARN else None,
    )
    monkeypatch.setattr(host_environment, "audit_legacy_distribution", lambda: observation)
    report = host_diagnostics.inspect_host().report
    actual = next(f for f in report.findings if f.message == observation.message)
    assert actual.severity == severity.value
    assert (actual.fix, actual.check_id) == (observation.fix, observation.check_id)
