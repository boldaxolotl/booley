"""Host health composition using existing Bootstrap and environment owners."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.audit import host_environment
from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.core.private_store import ProjectStore, ProjectStoreEntry
from booley.harness import bootstrap
from booley.projects import inventory
from booley.runtime import (
    issuance_invalidation,
    runtime_context,
    session_admission,
    session_issuance,
)
from booley.runtime.host_install import (
    HostInstallationError,
    current_host_installation,
    host_identity_differences,
    load_host_installation,
)
from booley.runtime.image_lifecycle import Intent
from booley.runtime.paths import skills_dir
from booley.runtime.platform_paths import run_captured_bounded

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
            "docker",
            inside_session_runtime=True,
            which=shutil.which,
            run=run_captured_bounded,
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
        _inspect_session_stores(report)
    return HostDiagnosticResult(report.report(), docker_exe)


def _inspect_session_stores(report: Findings) -> None:
    """Scan each per-Project Session store read-only for files that fail closed at use."""
    stamp_store = session_issuance.stamp_store()
    stores: tuple[ProjectStore, ...] = (
        stamp_store,
        session_admission.claim_store(),
        issuance_invalidation.journal_store(),
    )
    count = 0
    healthy = True
    stamps: tuple[ProjectStoreEntry, ...] = ()  # stays empty when the stamp scan fails
    for store in stores:
        try:
            entries = store.scan()
            count += len(entries)
            if store is stamp_store:
                stamps = entries
        except store.private.error_type as exc:
            # The message carries its own repair (reissue, chmod, or the stray path).
            report.fail(
                f"Session store check: {exc}",
                "apply the repair in the message (a misnamed file: remove it and reissue with "
                "`booley init --seed`) or remove the file it names, then rerun `booley doctor`; "
                "Doctor names one file per store, so rerun until it passes",
            )
            healthy = False
    if healthy:
        report.pass_(f"Session stores: {count} per-Project file(s) match their Project Roots")
    if stamps:  # a failing sibling store must not hide stamps that scanned cleanly
        _inspect_forgotten_stamps(stamp_store, stamps, report)


def _inspect_forgotten_stamps(
    store: ProjectStore, stamps: tuple[ProjectStoreEntry, ...], report: Findings
) -> None:
    """WARN once about stamps of deleted Projects that the Project Inventory no longer holds.

    Roots that still exist stay quiet: ``booley init --seed`` worktrees and Projects
    initialized before the inventory existed hold live stamps without being remembered.
    """
    try:
        known = {store.filename(root) for root in inventory.known_roots()}
    except inventory.ProjectInventoryError as exc:
        report.skip(f"forgotten-Project stamp check skipped: {exc}")
        return
    orphans = sorted(
        entry.project_root
        for entry in stamps
        if store.filename(entry.project_root) not in known and _root_is_gone(entry.project_root)
    )
    if orphans:
        report.warn(
            f"{len(orphans)} Sandbox issuance stamp(s) belong to deleted Projects "
            "not in the Project Inventory",
            "; ".join(_forget_purge_command(root) for root in orphans),
            check_id="host.forgotten-project-stamps",
        )


def _root_is_gone(root: str) -> bool:
    """Return whether *root* is positively absent; an unreadable path is not proof it is gone."""
    try:
        Path(root).stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False  # EACCES, ESTALE, an unmounted drive: the Project may still exist
    return False


def _forget_purge_command(root: str) -> str:
    """Quote the remedy for the host shell so a root with spaces stays copyable."""
    argv = ["booley", "projects", "forget", "--purge", root]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


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
