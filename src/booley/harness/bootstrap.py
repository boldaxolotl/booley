"""Project-independent Host Bootstrap orchestration and CLI adapter."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from booley.config.host_config import HostConfigError, InteractiveHostPolicy, load_host_policy
from booley.harness import host_sidecars, nangate_pdk
from booley.harness.colors import accent, bold_chrome, green, red, yellow
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
from booley.harness.init_skills import _find_skill_targets
from booley.runtime.paths import skills_dir
from booley.runtime.skill_links import SkillLinkReport, reconcile_skill_links


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
    findings.append(BootstrapFinding("host-config", BootstrapState.CURRENT, "host policy is valid"))

    prerequisites = _prerequisite_findings()
    findings.extend(prerequisites)
    if any(finding.state is BootstrapState.ERROR for finding in prerequisites):
        return BootstrapResult(intent, tuple(findings), policy)

    findings.append(_reconcile_skills(intent))
    if findings[-1].state is BootstrapState.ERROR:
        return BootstrapResult(intent, tuple(findings), policy)
    findings.append(_reconcile_nangate(intent))
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
    git = _tool_finding("git", "--version")
    docker = _tool_finding("docker", "--version")
    if docker.state is BootstrapState.CURRENT:
        daemon_error = _docker_daemon_error()
        if daemon_error:
            docker = BootstrapFinding("docker", BootstrapState.ERROR, daemon_error)
    return git, docker, _vscode_finding()


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
    from booley.config.editor import resolve_editor_command

    command = resolve_editor_command()
    if command:
        return BootstrapFinding("vscode", BootstrapState.CURRENT, f"{Path(command).name} available")
    for candidate in _vscode_config_dirs():
        if candidate.is_dir():
            return BootstrapFinding(
                "vscode",
                BootstrapState.CURRENT,
                f"{candidate.name} GUI found; install its shell command for terminal use",
            )
    return BootstrapFinding(
        "vscode",
        BootstrapState.ERROR,
        "VS Code or a supported compatible editor is required for Interactive Mode",
    )


def _vscode_config_dirs() -> tuple[Path, ...]:
    import os
    import sys

    home = Path.home()
    names = ("Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf")
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = home / ".config"
    return tuple(base / name for name in names)


def _reconcile_skills(intent: Intent) -> BootstrapFinding:
    source = skills_dir()
    if not source.is_dir():
        return BootstrapFinding("skills", BootstrapState.ERROR, f"packaged skills missing: {source}")
    reports = tuple(
        reconcile_skill_links(
            target,
            source,
            dry_run=intent is Intent.CHECK,
            allow_retarget=True,
        )
        for target in _find_skill_targets()
    )
    failures = tuple(_skill_report_error(report) for report in reports)
    errors = tuple(error for error in failures if error)
    if errors:
        return BootstrapFinding("skills", BootstrapState.ERROR, "; ".join(errors))
    changed = sum(event.changed for report in reports for event in report.events)
    if intent is Intent.CHECK and changed:
        return BootstrapFinding("skills", BootstrapState.PENDING, f"{changed} skill link change(s) pending")
    state = BootstrapState.CHANGED if changed else BootstrapState.CURRENT
    return BootstrapFinding("skills", state, f"checked {len(reports)} skill target(s)")


def _skill_report_error(report: SkillLinkReport) -> str:
    details = [event.detail or event.name for event in report.events if event.failed]
    details.extend(report.diagnostics)
    if report.fatal:
        details.append(report.fatal)
    return "; ".join(details)


def _reconcile_nangate(intent: Intent) -> BootstrapFinding:
    root = nangate_pdk.cache_root()
    issues = nangate_pdk.validation_errors(root)
    if not issues:
        return BootstrapFinding("nangate45", BootstrapState.CURRENT, f"verified cache at {root}")
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
    return result, BootstrapFinding("base-image", state, detail or f"{result.selected_reference} {result.status}")


def _sidecar_finding(finding: host_sidecars.SidecarFinding) -> BootstrapFinding:
    state = {
        host_sidecars.SidecarState.CURRENT: BootstrapState.CURRENT,
        host_sidecars.SidecarState.PENDING: BootstrapState.PENDING,
        host_sidecars.SidecarState.CHANGED: BootstrapState.CHANGED,
        host_sidecars.SidecarState.ERROR: BootstrapState.ERROR,
    }[finding.state]
    return BootstrapFinding(finding.resource, state, finding.detail)


def run_bootstrap(args: object) -> int:
    """Run and render the public ``booley bootstrap`` command."""
    intent = (
        Intent.CHECK
        if getattr(args, "check_only", False)
        else Intent.REFRESH
        if getattr(args, "force", False)
        else Intent.ENSURE
    )
    result = reconcile_bootstrap(intent, verbose=getattr(args, "verbose", False))
    print(bold_chrome("Host Bootstrap"))
    glyphs = {
        BootstrapState.CURRENT: (accent, "[--]"),
        BootstrapState.PENDING: (yellow, "[!!]"),
        BootstrapState.CHANGED: (green, "[OK]"),
        BootstrapState.ERROR: (red, "[XX]"),
    }
    for finding in result.findings:
        color, glyph = glyphs[finding.state]
        print(f"  {color(glyph)} {finding.resource}: {finding.detail}")
    if result.exit_status == 0:
        print(green("Host Bootstrap is current."))
    elif result.exit_status == 1:
        print(yellow("Host Bootstrap has pending work; run `booley bootstrap`."))
    else:
        print(red("Host Bootstrap is incomplete; fix the errors above and retry."))
    return result.exit_status
