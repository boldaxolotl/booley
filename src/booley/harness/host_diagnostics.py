"""Host health composition using existing Bootstrap and environment owners."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

from booley.audit import host_environment
from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.harness import bootstrap
from booley.runtime import runtime_context, session_admission
from booley.runtime.host_install import (
    HostInstallationError,
    current_host_installation,
    host_identity_differences,
    load_host_installation,
)
from booley.runtime.image_lifecycle import Intent
from booley.runtime.paths import skills_dir

MIN_PY = (3, 11)


@dataclass(frozen=True)
class HostDiagnosticResult:
    """Complete host findings and the usable container executable, if any."""

    report: DiagnosticReport
    docker_exe: str | None


def inspect_host() -> HostDiagnosticResult:
    """Probe health without installing or repairing host capabilities.

    Inside a Sandbox, host preparation and clock probes are skipped.
    Environment probes retain their bounded process/network timeouts.
    """
    report = Findings()
    version = sys.version_info
    _add_environment(
        host_environment.audit_python_version((version.major, version.minor), MIN_PY), report
    )
    _inspect_package(report)
    if runtime_context.inside_session_runtime():
        report.skip("Host Bootstrap check skipped inside the Sandbox")
        runtime = host_environment.probe_container_runtime(
            "docker", inside_session_runtime=True, which=shutil.which
        )
        _add_environment(runtime.finding, report)
        docker_exe = runtime.executable
    else:
        _inspect_host_installation(report)
        result = bootstrap.reconcile_bootstrap(Intent.CHECK)
        _add_bootstrap(result, report)
        docker_ready = any(
            finding.resource == "docker" and finding.state is bootstrap.BootstrapState.CURRENT
            for finding in result.findings
        )
        docker_exe = shutil.which("docker") if docker_ready else None
        if result.policy is not None:
            _inspect_sandbox_egress(result.policy.egress, docker_ready, report)
        _add_environment(host_environment.probe_host_clock(), report)
    return HostDiagnosticResult(report.report(), docker_exe)


def _inspect_sandbox_egress(host_egress: str, docker_ready: bool, report: Findings) -> None:
    """Report the host egress mode and each running Sandbox issued for the other mode."""
    report.pass_(f"host Sandbox egress mode: {host_egress}")
    if not docker_ready:
        report.skip("running Sandbox egress check skipped - Docker is not ready")
        return
    try:
        sandboxes = session_admission.running_sandboxes()
    except session_admission.AdmissionError as exc:
        report.fail(
            f"cannot check running Sandboxes against host egress mode: {exc}",
            "resolve the reported Sandbox problem (Sandbox starts are refused for the same "
            "reason), then rerun booley doctor",
        )
        return
    mismatched = [item for item in sandboxes if item.egress != host_egress]
    for item in mismatched:
        report.warn(
            f"Sandbox {item.name} (Project {item.project_root}) runs with egress "
            f"'{item.egress}' but host [sandbox] egress = '{host_egress}'",
            session_admission.recreate_remedy(item),
            check_id="host.sandbox-egress-mismatch",
            subject=item.project_root,
        )
    if not mismatched:
        report.pass_(
            f"{len(sandboxes)} running Booley Sandbox(es) match host egress mode {host_egress}"
        )


def _inspect_host_installation(report: Findings) -> None:
    try:
        identity = load_host_installation()
        actual = current_host_installation(skills_dir())
    except HostInstallationError as exc:
        report.fail(str(exc), "booley bootstrap")
        return
    if actual != identity:
        report.fail(
            "current Booley process does not match the canonical host installation "
            f"({host_identity_differences(identity, actual)})",
            "booley bootstrap --update",
        )
        return
    report.pass_(
        "canonical Booley host installation "
        f"v{identity.version} revision={identity.revision or 'unknown'} "
        f"fingerprint={identity.payload_fingerprint[:12]} "
        f"executable={identity.executable} interpreter={identity.interpreter} "
        f"root={identity.distribution_root}"
    )


def _inspect_package(report: Findings) -> None:
    try:
        import booley

        report.pass_(f"booley package v{booley.__version__}")
        _add_environment(host_environment.audit_legacy_distribution(), report)
    except ImportError:
        report.fail("booley package not importable", "pip install booley-rtl")


def _add_environment(finding: host_environment.EnvironmentFinding, report: Findings) -> None:
    severity = finding.severity
    if severity is host_environment.EnvironmentSeverity.PASS:
        report.pass_(finding.message)
    elif severity is host_environment.EnvironmentSeverity.SKIP:
        report.skip(finding.message)
    elif severity is host_environment.EnvironmentSeverity.FAIL:
        report.fail(finding.message, finding.fix)
    else:
        assert finding.check_id is not None
        report.warn(finding.message, finding.fix, check_id=finding.check_id)


def _add_bootstrap(result: bootstrap.BootstrapResult, report: Findings) -> None:
    for finding in result.findings:
        message = f"Host Bootstrap {finding.resource}: {finding.detail}"
        if finding.state is bootstrap.BootstrapState.ERROR:
            report.fail(message, "booley bootstrap")
        elif finding.state is bootstrap.BootstrapState.PENDING:
            report.warn(
                f"{message} — run booley bootstrap",
                check_id="host.bootstrap-pending",
                subject=finding.resource,
            )
        elif finding.state is bootstrap.BootstrapState.WARNING:
            report.warn(
                message,
                check_id="host.bootstrap-warning",
                subject=finding.resource,
            )
        else:
            report.pass_(message)
