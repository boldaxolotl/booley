"""Project-independent Host Bootstrap desired-state reconciliation."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from booley.config.host_config import (
    HostConfigError,
    SandboxHostPolicy,
    host_config_path,
    load_host_policy,
)
from booley.harness import host_sidecars, nangate_pdk
from booley.harness.image_lifecycle import (
    host_capacity_requests,
)
from booley.harness.image_lifecycle import (
    reconcile as reconcile_images,
)
from booley.harness.setup.skills import (
    HostSkillReconciliation,
    reconcile_host_qa_skills,
    reconcile_host_skills,
)
from booley.runtime.docker_capacity import DockerBuildPlan, ensure_docker_build_capacity
from booley.runtime.host_install import (
    host_install_error,
    inspect_host_installation,
)
from booley.runtime.image_lifecycle import (
    HostImageScope,
    ImageLifecycleError,
    Intent,
    LifecycleResult,
)
from booley.runtime.image_lifecycle import (
    Status as ImageStatus,
)
from booley.runtime.paths import skills_dir
from booley.runtime.qa_skill_selection import (
    QA_SKILL_NAMES,
    QaSkillSelectionError,
    active_qa_source_root,
)
from booley.runtime.skill_links import SkillLinkReport

MIN_GIT_VERSION = (2, 37, 2)
RELATIVE_WORKTREE_MIN_GIT_VERSION = (2, 48, 0)
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
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BootstrapFinding:
    """One ordered, typed Host Bootstrap finding."""

    resource: str
    state: BootstrapState
    detail: str
    remedy: str | None = None


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Host readiness without a coarse stamp or version Boolean."""

    intent: Intent
    findings: tuple[BootstrapFinding, ...]
    policy: SandboxHostPolicy | None = None
    base_image: LifecycleResult | None = None

    @property
    def ready(self) -> bool:
        return all(
            finding.state
            in {BootstrapState.CURRENT, BootstrapState.CHANGED, BootstrapState.WARNING}
            for finding in self.findings
        )

    @property
    def exit_status(self) -> int:
        if any(finding.state is BootstrapState.ERROR for finding in self.findings):
            return 2
        if any(finding.state is BootstrapState.PENDING for finding in self.findings):
            return 1 if self.intent is Intent.CHECK else 2
        return 0


def inspect_bootstrap(*, verbose: bool = False, include_images: bool = True) -> BootstrapResult:
    """Collect complete read-only readiness for the Bootstrap CLI."""
    findings: list[BootstrapFinding] = []
    policy = _inspect_bootstrap_policy(findings)
    installation = _inspect_installation()
    findings.append(installation)
    prerequisites = _prerequisite_findings()
    findings.extend(prerequisites)
    findings.append(_reconcile_vscode_dev_containers(Intent.CHECK))
    findings.append(
        BootstrapFinding(
            "skills",
            BootstrapState.ERROR,
            f"cannot inspect canonical skill links: {installation.detail}",
            remedy="",
        )
        if installation.state is BootstrapState.ERROR
        else _inspect_skills()
    )
    if qa := _reconcile_qa_skills(Intent.CHECK, opt_out=False):
        findings.append(qa)
    findings.append(_inspect_nangate())
    if not include_images:
        return BootstrapResult(Intent.CHECK, tuple(findings), policy)
    docker = next(item for item in prerequisites if item.resource == "docker")
    return _inspect_bootstrap_images(findings, policy, docker, verbose=verbose)


def _inspect_bootstrap_policy(findings: list[BootstrapFinding]) -> SandboxHostPolicy | None:
    try:
        policy = _load_bootstrap_policy(findings)
    except (HostConfigError, OSError, UnicodeError) as exc:
        policy = None
        findings.append(
            BootstrapFinding(
                "host-config",
                BootstrapState.ERROR,
                f"cannot inspect host config {host_config_path()} as UTF-8 TOML: {exc}",
            )
        )
    else:
        findings.append(
            BootstrapFinding("host-config", BootstrapState.CURRENT, "host policy is valid")
        )
    return policy


def _inspect_installation() -> BootstrapFinding:
    installation = inspect_host_installation(skills_dir())
    state = (
        BootstrapState.ERROR
        if installation.error
        else BootstrapState.PENDING
        if installation.pending
        else BootstrapState.CURRENT
    )
    return BootstrapFinding(
        "host-install",
        state,
        installation.error
        or (
            "canonical host installation is not recorded"
            if installation.pending
            else "canonical installation is current"
        ),
        remedy="",
    )


def _inspect_skills() -> BootstrapFinding:
    source = skills_dir()
    try:
        if not source.is_dir():
            return BootstrapFinding(
                "skills", BootstrapState.ERROR, f"packaged skills missing: {source}"
            )
        reconciliations = reconcile_host_skills(source, dry_run=True, allow_retarget=True)
    except OSError as exc:
        return BootstrapFinding(
            "skills", BootstrapState.ERROR, f"cannot read skills at {source}: {exc}"
        )
    return _skill_reconciliation_finding(Intent.CHECK, reconciliations, qa=False)


def _inspect_nangate() -> BootstrapFinding:
    root = nangate_pdk.cache_root()
    inspection = nangate_pdk.inspect_cache(root)
    if inspection.errors:
        return BootstrapFinding(
            "nangate45", BootstrapState.ERROR, "; ".join((*inspection.errors, *inspection.pending))
        )
    if inspection.pending:
        return BootstrapFinding(
            "nangate45",
            BootstrapState.PENDING,
            _nangate_license_notice() + " " + "; ".join(inspection.pending),
        )
    return BootstrapFinding("nangate45", BootstrapState.CURRENT, f"verified cache at {root}")


def _inspect_bootstrap_images(
    findings: list[BootstrapFinding],
    policy: SandboxHostPolicy | None,
    docker: BootstrapFinding,
    *,
    verbose: bool,
) -> BootstrapResult:
    if docker.state is BootstrapState.ERROR:
        findings.extend(
            BootstrapFinding(
                resource,
                BootstrapState.ERROR,
                f"inspection blocked by Docker: {docker.detail}",
                remedy="restore Docker daemon access",
            )
            for resource in (
                "base-image",
                "proxy-image",
                "reaper-image",
                "network",
                "proxy",
                "reaper",
            )
        )
        return BootstrapResult(Intent.CHECK, tuple(findings), policy)
    base, finding = _reconcile_base_image(Intent.CHECK, verbose=verbose)
    findings.append(finding)
    sidecars = host_sidecars.inspect_sidecars(policy)
    findings.extend(_sidecar_finding(item) for item in sidecars.findings)
    return BootstrapResult(Intent.CHECK, tuple(findings), policy, base)


def reconcile_bootstrap(  # noqa: PLR0911 - fixed-order failures stop dependent mutations.
    intent: Intent,
    *,
    verbose: bool = False,
    include_images: bool = True,
    qa_opt_out: bool = False,
) -> BootstrapResult:
    """Inspect or converge Host Bootstrap resources in their fixed order."""
    findings: list[BootstrapFinding] = []

    try:
        policy = _load_bootstrap_policy(findings)
    except HostConfigError as exc:
        return BootstrapResult(
            intent,
            (BootstrapFinding("host-config", BootstrapState.ERROR, str(exc)),),
        )
    findings.append(
        BootstrapFinding("host-config", BootstrapState.CURRENT, "host policy is valid")
    )

    if error := host_install_error(skills_dir()):
        findings.append(BootstrapFinding("host-install", BootstrapState.ERROR, error))
        return BootstrapResult(intent, tuple(findings), policy)

    if qa_opt_out:
        findings.append(_reconcile_qa_skills(intent, opt_out=True))

    prerequisites = _prerequisite_findings()
    findings.extend(prerequisites)
    if any(finding.state is BootstrapState.ERROR for finding in prerequisites):
        return BootstrapResult(intent, tuple(findings), policy)

    for reconcile in (_reconcile_vscode_dev_containers, _reconcile_skills):
        findings.append(reconcile(intent))
        if findings[-1].state is BootstrapState.ERROR:
            return BootstrapResult(intent, tuple(findings), policy)

    if not qa_opt_out and (qa := _reconcile_qa_skills(intent, opt_out=False)):
        findings.append(qa)

    findings.append(_reconcile_nangate(intent))
    if findings[-1].state is BootstrapState.ERROR:
        return BootstrapResult(intent, tuple(findings), policy)

    if not include_images:
        return BootstrapResult(intent, tuple(findings), policy)

    return _reconcile_bootstrap_images(intent, findings, policy, verbose=verbose)


def _load_bootstrap_policy(findings: list[BootstrapFinding]) -> SandboxHostPolicy:
    def record_deprecation(message: str) -> None:
        findings.append(BootstrapFinding("host-config", BootstrapState.WARNING, message))

    return load_host_policy(on_deprecation=record_deprecation)


def _reconcile_bootstrap_images(
    intent: Intent,
    findings: list[BootstrapFinding],
    policy: SandboxHostPolicy,
    *,
    verbose: bool,
) -> BootstrapResult:
    """Reconcile image-bearing Host Bootstrap resources after prerequisites."""
    plan = None
    if intent is not Intent.CHECK:
        try:
            requests = (
                *host_capacity_requests(intent),
                *host_sidecars.plan_image_builds(intent, egress=policy.egress),
            )
            if requests:
                plan = DockerBuildPlan(requests)
                ensure_docker_build_capacity(
                    ("docker", "build"),
                    image=plan.current.output_tag,
                    current_request=plan.current,
                    remaining_plan=plan,
                )
        except (
            ImageLifecycleError,
            host_sidecars.SidecarError,
            OSError,
            ValueError,
        ) as exc:
            findings.append(BootstrapFinding("image-capacity", BootstrapState.ERROR, str(exc)))
            return BootstrapResult(intent, tuple(findings), policy)

    base_result, base_finding = _reconcile_base_image(
        intent,
        verbose=verbose,
        capacity_plan=plan,
    )
    findings.append(base_finding)
    if base_finding.state is BootstrapState.ERROR:
        return BootstrapResult(intent, tuple(findings), policy)

    sidecars = host_sidecars.reconcile_sidecars(policy, intent, capacity_plan=plan)
    findings.extend(_sidecar_finding(finding) for finding in sidecars.findings)
    return BootstrapResult(intent, tuple(findings), policy, base_result)


def _prerequisite_findings() -> tuple[BootstrapFinding, ...]:
    git = _git_finding()
    docker = _tool_finding("docker", "--version")
    if docker.state is BootstrapState.CURRENT:
        daemon_error = _docker_daemon_error()
        if daemon_error:
            docker = BootstrapFinding("docker", BootstrapState.ERROR, daemon_error)
    return git, docker, _vscode_finding(), _host_agent_cli_finding()


def _host_agent_cli_finding() -> BootstrapFinding:
    """Project Setup requires an agent CLI on the host PATH."""
    installed = tuple(name for name in ("claude", "codex") if shutil.which(name))
    if installed:
        return BootstrapFinding(
            "host-agent-cli", BootstrapState.CURRENT, "on PATH: " + ", ".join(installed)
        )
    return BootstrapFinding(
        "host-agent-cli",
        BootstrapState.WARNING,
        "Project Setup requires Claude Code (claude) or Codex (codex) on the host PATH. "
        "Install Claude Code: https://code.claude.com/docs/en/setup or "
        "Codex: https://developers.openai.com/codex/cli, then rerun `booley bootstrap`.",
    )


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
    if _GIT_PRERELEASE_SUFFIX.search(match.group("suffix")):
        return BootstrapFinding(
            "git",
            BootstrapState.ERROR,
            f"pre-release Git builds are not supported; install Git {minimum} or newer",
        )
    version = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    if version < MIN_GIT_VERSION:
        detected = ".".join(str(part) for part in version)
        return BootstrapFinding(
            "git",
            BootstrapState.ERROR,
            f"Git {detected} is too old; Git {minimum} or newer is required. "
            "Upgrade Git and rerun booley bootstrap.",
        )
    return BootstrapFinding("git", BootstrapState.CURRENT, line[:80])


def parse_git_version(line: str) -> tuple[int, int, int] | None:
    """Return a stable Git version, accepting ordinary vendor suffixes."""
    match = _GIT_VERSION_LINE.fullmatch(line.strip())
    if match is None or _GIT_PRERELEASE_SUFFIX.search(match.group("suffix")):
        return None
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))


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
    from booley.config.editor import resolve_editor_install, resolve_editor_management_command

    command = resolve_editor_management_command()
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
    """Ensure the desktop editor can open Booley's Sandbox."""
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
    if error := host_install_error(source):
        return BootstrapFinding("skills", BootstrapState.ERROR, error)
    reconciliations = reconcile_host_skills(
        source,
        dry_run=intent is Intent.CHECK,
        allow_retarget=True,
    )
    return _skill_reconciliation_finding(intent, reconciliations, qa=False)


def _skill_reconciliation_finding(
    intent: Intent,
    reconciliations: tuple[HostSkillReconciliation, ...],
    *,
    qa: bool,
) -> BootstrapFinding:
    resource = "qa-skills" if qa else "skills"
    label = "QA skill" if qa else "skill"
    errors = tuple(
        f"{item.target}: {error}"
        for item in reconciliations
        if (error := _skill_report_error(item.report))
    )
    if errors:
        detail = "; ".join(errors)
        return (
            _qa_warning(detail) if qa else BootstrapFinding(resource, BootstrapState.ERROR, detail)
        )
    changes = tuple(
        (item.target, sum(event.changed for event in item.report.events))
        for item in reconciliations
    )
    changed = sum(count for _target, count in changes)
    if intent is Intent.CHECK and changed:
        detail = "; ".join(
            f"{target}: {count} {label} link change(s) pending"
            for target, count in changes
            if count
        )
        state = BootstrapState.WARNING if qa else BootstrapState.PENDING
        suffix = "; run `booley bootstrap`" if qa else ""
        return BootstrapFinding(resource, state, detail + suffix)
    state = BootstrapState.CHANGED if changed else BootstrapState.CURRENT
    targets = ", ".join(str(item.target) for item in reconciliations)
    action = f"applied {changed} {label} link change(s) across" if changed else "checked"
    return BootstrapFinding(
        resource,
        state,
        f"{action} {len(reconciliations)} skill target(s): {targets}",
    )


def _skill_report_error(report: SkillLinkReport) -> str:
    details = [event.detail or event.name for event in report.events if event.failed]
    details.extend(report.diagnostics)
    if report.fatal:
        details.append(report.fatal)
    return "; ".join(details)


def _qa_warning(detail: str) -> BootstrapFinding:
    recovery = (
        "Restore the recorded source checkout, re-enable from its new location with "
        "`booley bootstrap --with-qa-skills --force`, or recover with "
        "`booley bootstrap --without-qa-skills`."
    )
    return BootstrapFinding("qa-skills", BootstrapState.WARNING, f"{detail} {recovery}")


def _reconcile_qa_skills(intent: Intent, *, opt_out: bool) -> BootstrapFinding | None:
    """Keep QA warnings nonblocking and opt-out independent of selection parsing."""
    try:
        source = skills_dir() if opt_out else active_qa_source_root()
    except (QaSkillSelectionError, OSError, ValueError) as exc:
        return _qa_warning(str(exc))
    if source is None:
        return None
    reconciliations = reconcile_host_qa_skills(
        source,
        names=frozenset() if opt_out else QA_SKILL_NAMES,
        dry_run=intent is Intent.CHECK,
        allow_retarget=intent is Intent.REFRESH,
    )
    return _skill_reconciliation_finding(intent, reconciliations, qa=True)


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
    license_notice = _nangate_license_notice()
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


def _nangate_license_notice() -> str:
    return (
        "Nangate45 is an optional upstream download for non-commercial use; "
        "comparison with other libraries is restricted. "
        f"Terms: {nangate_pdk.LICENSE_ID}."
    )


def _reconcile_base_image(
    intent: Intent,
    *,
    verbose: bool,
    capacity_plan: DockerBuildPlan | None = None,
) -> tuple[LifecycleResult | None, BootstrapFinding]:
    try:
        result = reconcile_images(
            HostImageScope(),
            intent,
            verbose=verbose,
            capacity_plan=capacity_plan,
        )
    except ImageLifecycleError as exc:
        return None, BootstrapFinding("base-image", BootstrapState.ERROR, str(exc))
    state = {
        ImageStatus.CURRENT: BootstrapState.CURRENT,
        ImageStatus.STALE: BootstrapState.PENDING,
        ImageStatus.CHANGED: BootstrapState.CHANGED,
        ImageStatus.EXTERNAL: BootstrapState.ERROR,
    }[result.status]
    if result.cleanup.pending and state is BootstrapState.CURRENT:
        state = BootstrapState.PENDING
    details = [item.message for item in result.diagnostics]
    if result.cleanup.pending:
        noun = "tag" if len(result.cleanup.pending) == 1 else "tags"
        details.append(
            f"obsolete Docker image {noun} can be removed: " + ", ".join(result.cleanup.pending)
        )
    if result.cleanup.removed:
        details.append("removed obsolete Docker image tags: " + ", ".join(result.cleanup.removed))
    if result.cleanup.retained_required:
        details.append(
            "retained Docker image tags still required by containers: "
            + ", ".join(result.cleanup.retained_required)
        )
    detail = "; ".join(details)
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
    return BootstrapFinding(finding.resource, state, finding.detail, finding.remedy)
