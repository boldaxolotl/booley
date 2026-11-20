"""Host health composition using existing Bootstrap and environment owners."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.audit import host_environment
from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.core.host_shell import quote_arg
from booley.core.private_store import ProjectStore, ProjectStoreEntry
from booley.harness import bootstrap
from booley.projects import inventory
from booley.runtime import (
    image_keepers,
    issuance_invalidation,
    runtime_context,
    session_admission,
    session_issuance,
)
from booley.runtime.host_install import (
    SHADOWED_INSTALL_FIX,
    HostInstallationError,
    current_host_installation,
    host_identity_differences,
    load_host_installation,
    shadowing_installs,
)
from booley.runtime.image_lifecycle import Intent
from booley.runtime.paths import skills_dir
from booley.runtime.platform_paths import run_captured_bounded, same_host_path

MIN_PY = (3, 11)


@dataclass(frozen=True)
class HostDiagnosticResult:
    """Complete host findings and the usable container executable, if any."""

    report: DiagnosticReport
    docker_exe: str | None


def inspect_host(project_root: Path) -> HostDiagnosticResult:
    """Probe health without installing or repairing host capabilities.

    *project_root* is the Project Doctor runs for: a Sandbox or Session stamp of that
    Project gates, while the same state of another Project is only a NOTE.
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
            _inspect_sandbox_egress(result.policy.egress, docker_ready, project_root, report)
        _add_environment(host_environment.probe_host_clock(), report)
        _inspect_session_stores(project_root, report)
    return HostDiagnosticResult(report.report(), docker_exe)


_STORE_REPAIR = (
    "apply the repair in the message, in order (a misnamed file: run any Docker "
    "commands it names, then remove the file and reissue with `booley init --seed`); "
    "when the message names no repair, remove the file it names; then rerun "
    "`booley doctor`; Doctor names one file per store, so rerun until it passes"
)


def _inspect_session_stores(project_root: Path, report: Findings) -> None:
    """Scan each per-Project Session store read-only for files that fail closed at use."""
    stamp_store = session_issuance.stamp_store()
    healthy = True
    count = 0
    stamps: tuple[ProjectStoreEntry, ...] = ()  # stays empty when the stamp scan fails
    try:
        stamps, bad_stamps = stamp_store.scan_each()
        count += len(stamps)
        healthy = _report_bad_stamps(stamp_store, bad_stamps, project_root, report)
    except stamp_store.private.error_type as exc:
        report.fail(f"Session store check: {exc}", _STORE_REPAIR)
        healthy = False
    # The claim and journal stores are scanned host-wide at use, so a bad file of any
    # Project fails this Project's commands too.
    for store in (session_admission.claim_store(), issuance_invalidation.journal_store()):
        try:
            count += len(store.scan())
        except store.private.error_type as exc:
            report.fail(f"Session store check: {exc}", _STORE_REPAIR)
            healthy = False
    if healthy:
        report.pass_(f"Session stores: {count} per-Project file(s) match their Project Roots")
    if stamps:  # a failing sibling store must not hide stamps that scanned cleanly
        _inspect_forgotten_stamps(stamps, report)


def _report_bad_stamps(
    store: ProjectStore,
    bad_stamps: tuple[tuple[Path, RuntimeError], ...],
    project_root: Path,
    report: Findings,
) -> bool:
    """FAIL for a bad stamp file named for this Project; NOTE for any other Project's."""
    own = {path.name for path in store.paths(str(project_root.resolve()))}
    for path, exc in bad_stamps:
        if path.name in own:
            report.fail(f"Session store check: {exc}", _STORE_REPAIR)
        else:
            report.note(
                f"another Project's Sandbox issuance stamp is unusable: {exc}; "
                "only that Project's Sandbox starts are refused",
                _STORE_REPAIR,
            )
    return not bad_stamps


def _inspect_forgotten_stamps(stamps: tuple[ProjectStoreEntry, ...], report: Findings) -> None:
    """NOTE once about stamps of deleted Projects that the Project Inventory no longer holds."""
    try:
        orphans = image_keepers.orphan_stamp_roots(stamps, inventory.known_roots())
    except inventory.ProjectInventoryError as exc:
        report.skip(f"forgotten-Project stamp check skipped: {exc}")
        return
    if orphans:
        command = " ".join(quote_arg(root) for root in orphans)
        report.note(
            f"{len(orphans)} Sandbox issuance stamp(s) belong to deleted Projects "
            "not in the Project Inventory",
            "if these folders are deleted (not on an unmounted drive), remove the stamps with "
            f"`booley projects prune-stamps {command}`, which keeps those Projects' images "
            "and state volumes; `booley projects forget --purge <root>` also removes those",
        )
        for root in orphans:
            report.detail(f"    {root}", verbose_only=False)


def _inspect_sandbox_egress(
    host_egress: str, docker_ready: bool, project_root: Path, report: Findings
) -> None:
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
        message = (
            f"Sandbox {item.name} (Project {item.project_root}) runs with egress "
            f"'{item.egress}' but host [sandbox] egress = '{host_egress}'"
        )
        fix = session_admission.recreate_remedy(item)
        if same_host_path(item.project_root, str(project_root)):
            report.warn(
                message, fix, check_id="host.sandbox-egress-mismatch", subject=item.project_root
            )
        else:  # another Project's Sandbox cannot make this Project's commands fail
            report.note(message, fix)
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
        message = (
            "current Booley process does not match the canonical host installation "
            f"({host_identity_differences(identity, actual)})"
        )
        listing = shadowing_installs(identity, actual)
        if listing:
            report.fail(f"{message}; {listing}", SHADOWED_INSTALL_FIX)
        else:
            report.fail(message, "booley bootstrap --update")
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
            report.fail(message, finding.doctor_fix or "booley bootstrap")
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
