"""Project-independent Host Bootstrap desired-state reconciliation."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from booley.config.host_config import HostConfigError, InteractiveHostPolicy, load_host_policy
from booley.harness import host_sidecars, nangate_pdk
from booley.harness.image_lifecycle import (
    HostImageScope,
    ImageLifecycleError,
    Intent,
    LifecycleResult,
)
from booley.harness.image_lifecycle import (
    Status as ImageStatus,
)
from booley.harness.image_lifecycle import (
    reconcile as reconcile_images,
)
from booley.harness.setup.skills import reconcile_host_skills
from booley.runtime.paths import skills_dir
from booley.runtime.skill_links import SkillLinkReport

MIN_GIT_VERSION = (2, 37, 2)
DEV_CONTAINERS_EXTENSION_ID = "ms-vscode-remote.remote-containers"
_GIT_VERSION_LINE = re.compile(
    r"^git version (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?P<suffix>[^\s]*)(?:\s.*)?$"
)
_GIT_PRERELEASE_SUFFIX = re.compile(r"(?:^|[.-])(?:alpha|beta|pre|rc)\d*", re.IGNORECASE)


class BootstrapState(StrEnum):
    """One Host Bootstrap resource's state."""

    CURRENT = "current"
    PENDING = "pending"
    CHANGED = "changed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BootstrapFinding:
    """One ordered, typed Host Bootstrap finding."""

    resource: str
    state: BootstrapState
    detail: str


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Host readiness without a coarse stamp or version Boolean."""

    intent: Intent
    findings: tuple[BootstrapFinding, ...]
    policy: InteractiveHostPolicy | None = None
    base_image: LifecycleResult | None = None

    @property
    def ready(self) -> bool:
        return all(
            finding.state in {BootstrapState.CURRENT, BootstrapState.CHANGED}
            for finding in self.findings
        )

    @property
    def exit_status(self) -> int:
        if any(finding.state is BootstrapState.ERROR for finding in self.findings):
            return 2
        if any(finding.state is BootstrapState.PENDING for finding in self.findings):
            return 1 if self.intent is Intent.CHECK else 2
        return 0


def reconcile_bootstrap(intent: Intent, *, verbose: bool = False) -> BootstrapResult:
    """Inspect or converge Host Bootstrap resources in their fixed order."""
    findings: list[BootstrapFinding] = []
    try:
        policy = load_host_policy()
    except HostConfigError as exc:
        return BootstrapResult(
            intent,
            (BootstrapFinding("host-config", BootstrapState.ERROR, str(exc)),),
        )
    findings.append(
        BootstrapFinding("host-config", BootstrapState.CURRENT, "host policy is valid")
    )

    prerequisites = _prerequisite_findings()
    findings.extend(prerequisites)
    if any(finding.state is BootstrapState.ERROR for finding in prerequisites):
        return BootstrapResult(intent, tuple(findings), policy)

    for reconcile in (
        _reconcile_vscode_dev_containers,
        _reconcile_skills,
        _reconcile_nangate,
    ):
        findings.append(reconcile(intent))
        if findings[-1].state is BootstrapState.ERROR:
            return BootstrapResult(intent, tuple(findings), policy)

    base_result, base_finding = _reconcile_base_image(intent, verbose=verbose)
    findings.append(base_finding)
    if base_finding.state is BootstrapState.ERROR:
        return BootstrapResult(intent, tuple(findings), policy)

    sidecars = host_sidecars.reconcile_sidecars(policy, intent)
    findings.extend(_sidecar_finding(finding) for finding in sidecars.findings)
    return BootstrapResult(intent, tuple(findings), policy, base_result)


def _prerequisite_findings() -> tuple[BootstrapFinding, ...]:
    git = _git_finding()
    docker = _tool_finding("docker", "--version")
    if docker.state is BootstrapState.CURRENT:
        daemon_error = _docker_daemon_error()
        if daemon_error:
            docker = BootstrapFinding("docker", BootstrapState.ERROR, daemon_error)
    return git, docker, _vscode_finding()


def _git_finding() -> BootstrapFinding:
    """Require a stable Git release that avoids the Windows temp-name limit."""
    minimum = ".".join(str(part) for part in MIN_GIT_VERSION)
    finding = _tool_finding("git", "--version")
    if finding.state is BootstrapState.ERROR:
        if finding.detail == "git is required but not on PATH":
            detail = f"Git {minimum} or newer is required but git is not on PATH"
        else:
            detail = finding.detail.replace("git", "Git", 1)
        return BootstrapFinding(
            "git",
            BootstrapState.ERROR,
            detail,
        )
    return _git_version_finding(finding.detail)


def _git_version_finding(line: str) -> BootstrapFinding:
    """Parse and enforce the supported stable Git version boundary."""
    minimum = ".".join(str(part) for part in MIN_GIT_VERSION)
    match = _GIT_VERSION_LINE.fullmatch(line)
    if match is None:
        return BootstrapFinding(
            "git",
            BootstrapState.ERROR,
            f"cannot determine a supported Git version; Git {minimum} or newer is required",
        )
    version = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    suffix = match.group("suffix")
    if _GIT_PRERELEASE_SUFFIX.search(suffix):
        return BootstrapFinding(
            "git",
            BootstrapState.ERROR,
            f"pre-release Git builds are not supported; install Git {minimum} or newer",
        )
    if version < MIN_GIT_VERSION:
        detected = ".".join(str(part) for part in version)
        return BootstrapFinding(
            "git",
            BootstrapState.ERROR,
            f"Git {detected} is too old; Git {minimum} or newer is required. "
            "Upgrade Git and rerun booley bootstrap.",
        )
    return BootstrapFinding("git", BootstrapState.CURRENT, line[:80])


def _tool_finding(name: str, version_arg: str) -> BootstrapFinding:
    executable = shutil.which(name)
    if executable is None:
        return BootstrapFinding(name, BootstrapState.ERROR, f"{name} is required but not on PATH")
    try:
        result = subprocess.run(
            [executable, version_arg], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return BootstrapFinding(name, BootstrapState.ERROR, f"cannot run {name}: {exc}")
    if result.returncode:
        return BootstrapFinding(name, BootstrapState.ERROR, f"{name} version probe failed")
    version = (result.stdout or result.stderr).strip().splitlines()
    detail = version[0][:80] if version else f"{name} available"
    return BootstrapFinding(name, BootstrapState.CURRENT, detail)


def _docker_daemon_error() -> str | None:
    executable = shutil.which("docker")
    assert executable is not None
    try:
        result = subprocess.run(
            [executable, "info"], capture_output=True, text=True, timeout=10, check=False
        )
    except subprocess.TimeoutExpired:
        return "Docker daemon did not respond within 10 seconds"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"cannot contact Docker daemon: {exc}"
    if result.returncode == 0:
        return None
    lines = (result.stderr or result.stdout).strip().splitlines()
    detail = f": {lines[0][:200]}" if lines else ""
    return f"Docker daemon is not running or accessible{detail}"


def _vscode_finding() -> BootstrapFinding:
    from booley.config.editor import resolve_editor_command, resolve_editor_install

    command = resolve_editor_command()
    if command:
        return BootstrapFinding(
            "vscode", BootstrapState.CURRENT, f"{Path(command).name} available"
        )
    application = resolve_editor_install()
    if application is not None:
        return BootstrapFinding(
            "vscode",
            BootstrapState.CURRENT,
            f"{application.name} application found; install its shell command for terminal use",
        )
    return BootstrapFinding(
        "vscode",
        BootstrapState.ERROR,
        "VS Code or a supported compatible editor is required for Interactive Mode",
    )


def _vscode_extension_ids(command: str) -> tuple[frozenset[str] | None, str | None]:
    """List local editor extensions, returning a user-facing failure when unavailable."""
    try:
        result = subprocess.run(
            [command, "--list-extensions"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "VS Code did not list extensions within 30 seconds"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"cannot inspect VS Code extensions: {exc}"
    if result.returncode:
        lines = (result.stderr or result.stdout).strip().splitlines()
        detail = f": {lines[0][:200]}" if lines else ""
        return None, f"VS Code extension probe failed{detail}"
    return frozenset(line.strip().lower() for line in result.stdout.splitlines()), None


def _install_vscode_dev_containers(command: str) -> str | None:
    """Install the desktop Dev Containers extension and return an error, if any."""
    try:
        result = subprocess.run(
            [command, "--install-extension", DEV_CONTAINERS_EXTENSION_ID, "--force"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Dev Containers extension installation did not finish within 120 seconds"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"cannot install the Dev Containers extension: {exc}"
    if result.returncode == 0:
        return None
    lines = (result.stderr or result.stdout).strip().splitlines()
    detail = f": {lines[0][:200]}" if lines else ""
    return f"Dev Containers extension installation failed{detail}"


def _vscode_dev_containers_finding(command: str) -> BootstrapFinding:
    """Inspect the Dev Containers extension in one resolved desktop editor."""
    installed, error = _vscode_extension_ids(command)
    if error:
        return BootstrapFinding("vscode-dev-containers", BootstrapState.ERROR, error)
    assert installed is not None
    if DEV_CONTAINERS_EXTENSION_ID in installed:
        return BootstrapFinding(
            "vscode-dev-containers",
            BootstrapState.CURRENT,
            f"{DEV_CONTAINERS_EXTENSION_ID} installed",
        )
    return BootstrapFinding(
        "vscode-dev-containers",
        BootstrapState.PENDING,
        f"{DEV_CONTAINERS_EXTENSION_ID} is not installed",
    )


def _reconcile_vscode_dev_containers(intent: Intent) -> BootstrapFinding:
    """Ensure the desktop editor can open Booley's Session Runtime."""
    from booley.config.editor import resolve_editor_management_command

    command = resolve_editor_management_command()
    if command is None:
        return BootstrapFinding(
            "vscode-dev-containers",
            BootstrapState.ERROR,
            "cannot find the installed editor's extension-management command",
        )
    finding = _vscode_dev_containers_finding(command)
    if finding.state is not BootstrapState.PENDING or intent is Intent.CHECK:
        return finding
    if error := _install_vscode_dev_containers(command):
        return BootstrapFinding("vscode-dev-containers", BootstrapState.ERROR, error)
    verified = _vscode_dev_containers_finding(command)
    if verified.state is BootstrapState.CURRENT:
        return BootstrapFinding(
            "vscode-dev-containers",
            BootstrapState.CHANGED,
            f"installed {DEV_CONTAINERS_EXTENSION_ID}",
        )
    if verified.state is BootstrapState.PENDING:
        return BootstrapFinding(
            "vscode-dev-containers",
            BootstrapState.ERROR,
            "VS Code did not report the extension after installation",
        )
    return verified


def _reconcile_skills(intent: Intent) -> BootstrapFinding:
    source = skills_dir()
    if not source.is_dir():
        return BootstrapFinding(
            "skills", BootstrapState.ERROR, f"packaged skills missing: {source}"
        )
    reconciliations = reconcile_host_skills(
        source,
        dry_run=intent is Intent.CHECK,
        allow_retarget=True,
    )
    failures = tuple((item.target, _skill_report_error(item.report)) for item in reconciliations)
    errors = tuple(f"{target}: {error}" for target, error in failures if error)
    if errors:
        return BootstrapFinding("skills", BootstrapState.ERROR, "; ".join(errors))
    changes = tuple(
        (item.target, sum(event.changed for event in item.report.events))
        for item in reconciliations
    )
    changed = sum(count for _target, count in changes)
    if intent is Intent.CHECK and changed:
        pending = "; ".join(
            f"{target}: {count} skill link change(s) pending" for target, count in changes if count
        )
        return BootstrapFinding("skills", BootstrapState.PENDING, pending)
    state = BootstrapState.CHANGED if changed else BootstrapState.CURRENT
    targets = ", ".join(str(item.target) for item in reconciliations)
    action = f"applied {changed} skill link change(s) across" if changed else "checked"
    return BootstrapFinding(
        "skills",
        state,
        f"{action} {len(reconciliations)} skill target(s): {targets}",
    )


def _skill_report_error(report: SkillLinkReport) -> str:
    details = [event.detail or event.name for event in report.events if event.failed]
    details.extend(report.diagnostics)
    if report.fatal:
        details.append(report.fatal)
    return "; ".join(details)


def _reconcile_nangate(intent: Intent) -> BootstrapFinding:
    root = nangate_pdk.cache_root()
    secured = False
    if intent is not Intent.CHECK:
        try:
            secured = nangate_pdk.secure_config_dir_for_cache(root)
        except nangate_pdk.NangatePdkError as exc:
            return BootstrapFinding("nangate45", BootstrapState.ERROR, str(exc))
    issues = nangate_pdk.validation_errors(root)
    if not issues:
        state = BootstrapState.CHANGED if secured else BootstrapState.CURRENT
        action = "secured config root and verified" if secured else "verified"
        return BootstrapFinding("nangate45", state, f"{action} cache at {root}")
    license_notice = (
        "Nangate45 is an optional upstream download for non-commercial use; "
        "comparison with other libraries is restricted. "
        f"Terms: {nangate_pdk.LICENSE_ID}."
    )
    if intent is Intent.CHECK:
        detail = f"{license_notice} " + "; ".join(issues)
        return BootstrapFinding("nangate45", BootstrapState.PENDING, detail)
    try:
        nangate_pdk.fetch(root)
    except nangate_pdk.NangatePdkError as exc:
        return BootstrapFinding("nangate45", BootstrapState.ERROR, f"{license_notice} {exc}")
    return BootstrapFinding(
        "nangate45", BootstrapState.CHANGED, f"{license_notice} Downloaded cache to {root}"
    )


def _reconcile_base_image(
    intent: Intent, *, verbose: bool
) -> tuple[LifecycleResult | None, BootstrapFinding]:
    try:
        result = reconcile_images(HostImageScope(), intent, verbose=verbose)
    except ImageLifecycleError as exc:
        return None, BootstrapFinding("base-image", BootstrapState.ERROR, str(exc))
    state = {
        ImageStatus.CURRENT: BootstrapState.CURRENT,
        ImageStatus.STALE: BootstrapState.PENDING,
        ImageStatus.CHANGED: BootstrapState.CHANGED,
        ImageStatus.EXTERNAL: BootstrapState.ERROR,
    }[result.status]
    detail = "; ".join(item.message for item in result.diagnostics)
    return result, BootstrapFinding(
        "base-image", state, detail or f"{result.selected_reference} {result.status}"
    )


def _sidecar_finding(finding: host_sidecars.SidecarFinding) -> BootstrapFinding:
    state = {
        host_sidecars.SidecarState.CURRENT: BootstrapState.CURRENT,
        host_sidecars.SidecarState.PENDING: BootstrapState.PENDING,
        host_sidecars.SidecarState.CHANGED: BootstrapState.CHANGED,
        host_sidecars.SidecarState.ERROR: BootstrapState.ERROR,
    }[finding.state]
    return BootstrapFinding(finding.resource, state, finding.detail)
