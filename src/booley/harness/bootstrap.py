"""Project-independent Host Bootstrap desired-state reconciliation."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from booley.config.host_config import HostConfigError, SandboxHostPolicy, load_host_policy
from booley.harness import host_sidecars, nangate_pdk
from booley.harness.image_lifecycle import (
    HostImageScope,
    ImageLifecycleError,
    Intent,
    LifecycleResult,
    host_capacity_requests,
)
from booley.harness.image_lifecycle import (
    Status as ImageStatus,
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
from booley.runtime.host_install import current_host_installation, host_install_error
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


def reconcile_bootstrap(  # noqa: PLR0911 - fixed-order failures stop dependent mutations.
    intent: Intent,
    *,
    verbose: bool = False,
    include_images: bool = True,
    qa_opt_out: bool = False,
    allow_qa_adoption: bool = False,
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
        _append_qa_finding(findings, intent, True, False)

    prerequisites = _prerequisite_findings()
    findings.extend(prerequisites)
    if any(finding.state is BootstrapState.ERROR for finding in prerequisites):
        return BootstrapResult(intent, tuple(findings), policy)

    for reconcile in (_reconcile_vscode_dev_containers, _reconcile_skills):
        findings.append(reconcile(intent))
        if findings[-1].state is BootstrapState.ERROR:
            return BootstrapResult(intent, tuple(findings), policy)

    if not qa_opt_out:
        _append_qa_finding(findings, intent, False, allow_qa_adoption)

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
            requests = (*host_capacity_requests(intent), *host_sidecars.plan_image_builds(intent))
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


def _skill_names(source: Path) -> frozenset[str]:
    return frozenset(
        child.name
        for child in source.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    )


def _qa_warning(detail: str) -> BootstrapFinding:
    recovery = (
        "Restore the recorded clean primary main checkout, re-enable with "
        "`booley bootstrap --with-qa-skills`, or recover with "
        "`booley bootstrap --without-qa-skills`."
    )
    return BootstrapFinding("qa-skills", BootstrapState.WARNING, f"{detail} {recovery}")


def _reconcile_qa_skills(
    intent: Intent,
    *,
    opt_out: bool,
    allow_adoption: bool,
) -> BootstrapFinding | None:
    """Reconcile optional QA skills without blocking product Host Bootstrap."""
    try:
        resolved = _qa_source(opt_out)
    except (QaSkillSelectionError, OSError, ValueError) as exc:
        return _qa_warning(str(exc))
    if resolved is None:
        return None
    source, names = resolved
    reconciliations = reconcile_host_qa_skills(
        source,
        names=names,
        dry_run=intent is Intent.CHECK,
        allow_retarget=allow_adoption,
        allow_exact_adoption=allow_adoption,
    )
    return _skill_reconciliation_finding(intent, reconciliations, qa=True)


def _qa_source(opt_out: bool) -> tuple[Path, frozenset[str]] | None:
    if opt_out:
        return skills_dir(), frozenset()
    revision = current_host_installation(skills_dir()).revision
    source = active_qa_source_root(revision)
    if source is None:
        return None
    collisions = _skill_names(skills_dir()) & QA_SKILL_NAMES
    if collisions:
        raise QaSkillSelectionError(
            "QA skill names collide with packaged product skills: " + ", ".join(sorted(collisions))
        )
    return source, QA_SKILL_NAMES


def _append_qa_finding(
    findings: list[BootstrapFinding],
    intent: Intent,
    opt_out: bool,
    allow_adoption: bool,
) -> None:
    finding = _reconcile_qa_skills(
        intent,
        opt_out=opt_out,
        allow_adoption=allow_adoption,
    )
    if finding is not None:
        findings.append(finding)


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
    return BootstrapFinding(finding.resource, state, finding.detail)
