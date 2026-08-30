"""Typed host-environment probes for Doctor orchestration."""

from __future__ import annotations

import email.utils
import getpass
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from booley.audit.contracts import CommandRunner

CLOCK_SKEW_WARN_SECONDS = 120
CLOCK_REFERENCE_URLS = ("https://www.google.com", "https://one.one.one.one")


class EnvironmentSeverity(StrEnum):
    """Presentation-independent host finding severity."""

    PASS = "pass"
    WARN = "warn"
    SKIP = "skip"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class EnvironmentFinding:
    """One host-environment finding and its stable warning identity."""

    severity: EnvironmentSeverity
    message: str
    fix: str = ""
    check_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContainerRuntimeAudit:
    """Container executable when healthy, plus its single probe finding."""

    executable: str | None
    finding: EnvironmentFinding


def audit_python_version(
    current: tuple[int, int],
    minimum: tuple[int, int],
) -> EnvironmentFinding:
    """Assess the interpreter version against Booley's supported minimum."""
    if current >= minimum:
        return EnvironmentFinding(EnvironmentSeverity.PASS, f"Python {current[0]}.{current[1]}")
    return EnvironmentFinding(
        EnvironmentSeverity.FAIL,
        f"Python {current[0]}.{current[1]} (need >= {minimum[0]}.{minimum[1]})",
        f"install python{minimum[0]}.{minimum[1]}+",
    )


def audit_legacy_distribution() -> EnvironmentFinding:
    """Detect the pre-rename distribution that can shadow ``booley-rtl``."""
    from importlib.metadata import PackageNotFoundError, distribution

    import booley

    try:
        legacy = distribution("booley")
    except PackageNotFoundError:
        return EnvironmentFinding(
            EnvironmentSeverity.PASS,
            "no legacy `booley` distribution shadowing `booley-rtl`",
        )

    resolved = Path(booley.__file__ or "?").resolve().parent
    legacy_version = legacy.metadata["Version"] or "?"
    try:
        distribution("booley-rtl")
        current_installed = True
    except PackageNotFoundError:
        current_installed = False
    if booley.__dist_name__ == "booley" and not current_installed:
        return _legacy_only_finding(legacy_version, resolved)
    if booley.version_attribution.source_root is not None:
        return EnvironmentFinding(
            EnvironmentSeverity.FAIL,
            f"legacy `booley` distribution ({legacy_version}) is installed, but the "
            f"active import resolves to the source checkout at {resolved}; stale "
            "metadata can shadow a checkout on sys.path",
            "pip uninstall -y booley  # then re-run doctor to confirm the stale metadata is gone",
        )
    return EnvironmentFinding(
        EnvironmentSeverity.FAIL,
        f"both `booley` ({legacy_version}) and `booley-rtl` are installed; the "
        f"legacy distribution can shadow the newer one on sys.path "
        f"(`import booley` currently resolves to {resolved})",
        "pip uninstall -y booley  # then re-run doctor to confirm the path moved",
    )


def _legacy_only_finding(version: str, resolved: Path) -> EnvironmentFinding:
    return EnvironmentFinding(
        EnvironmentSeverity.FAIL,
        f"the pre-rename `booley` distribution ({version}) is installed "
        f"and is the one supplying `import booley` (from {resolved}); "
        "`booley-rtl` is not installed at all",
        "pip uninstall -y booley && pip install -e '.[dev]'  # or: pip install booley-rtl",
    )


def probe_host_clock(
    *,
    now: Callable[[], datetime] | None = None,
) -> EnvironmentFinding:
    """Compare the host clock with reachable HTTP Date references."""
    current_time = now or (lambda: datetime.now(UTC))
    for url in CLOCK_REFERENCE_URLS:
        remote = _remote_http_time(url)
        if remote is None:
            continue
        skew = (current_time() - remote).total_seconds()
        if abs(skew) <= CLOCK_SKEW_WARN_SECONDS:
            return EnvironmentFinding(
                EnvironmentSeverity.PASS,
                f"host clock agrees with true UTC (skew {skew:+.0f}s)",
            )
        return _clock_skew_finding(url, skew)
    return EnvironmentFinding(
        EnvironmentSeverity.SKIP,
        "host clock check skipped - no reference time reachable (offline?)",
    )


def _remote_http_time(url: str) -> datetime | None:
    try:
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as response:
            date_header = response.headers.get("Date", "")
        remote = email.utils.parsedate_to_datetime(date_header)
    except (urllib.error.URLError, OSError, TypeError, ValueError):
        return None
    return remote if remote.tzinfo is not None else None


def _clock_skew_finding(url: str, skew: float) -> EnvironmentFinding:
    direction = "behind" if skew < 0 else "ahead of"
    hours = abs(skew) / 3600
    return EnvironmentFinding(
        EnvironmentSeverity.WARN,
        f"host clock is {hours:.1f}h {direction} true UTC (vs HTTP Date "
        f"from {url}) — sandbox image builds and TLS can fail on this; "
        "dual-boot machines often read the RTC as local time. Fix: "
        "elevated `w32tm /resync` (Windows) or enable NTP time sync",
        check_id="host.clock-skew",
    )


def probe_container_runtime(
    container_cli: str,
    *,
    inside_session_runtime: bool,
    which: Callable[[str], str | None] = shutil.which,
    run: CommandRunner = subprocess.run,
) -> ContainerRuntimeAudit:
    """Discover and probe the host container runtime."""
    if inside_session_runtime:
        finding = EnvironmentFinding(
            EnvironmentSeverity.SKIP,
            "container runtime check skipped (inside Session Runtime; Booley Flows run here)",
        )
        return ContainerRuntimeAudit(None, finding)
    executable = which(container_cli)
    if not executable:
        finding = EnvironmentFinding(
            EnvironmentSeverity.FAIL,
            "container runtime not on PATH",
            "install a supported container runtime",
        )
        return ContainerRuntimeAudit(None, finding)
    return _run_container_probe(executable, run)


def _run_container_probe(executable: str, run: CommandRunner) -> ContainerRuntimeAudit:
    try:
        result = run(
            [executable, "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        finding = EnvironmentFinding(
            EnvironmentSeverity.FAIL,
            f"container runtime probe failed: {exc}",
            "start the container runtime service",
        )
        return ContainerRuntimeAudit(None, finding)
    if result.returncode == 0:
        finding = EnvironmentFinding(EnvironmentSeverity.PASS, "container runtime running")
        return ContainerRuntimeAudit(executable, finding)
    return ContainerRuntimeAudit(None, _container_failure(result))


def _container_failure(result: subprocess.CompletedProcess[str]) -> EnvironmentFinding:
    combined = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    if "permission denied" in combined:
        return EnvironmentFinding(
            EnvironmentSeverity.FAIL,
            "container runtime permission denied",
            docker_permission_denied_fix(),
        )
    return EnvironmentFinding(
        EnvironmentSeverity.FAIL,
        "container runtime not running",
        "start the container runtime service",
    )


def docker_permission_denied_fix() -> str:
    """Tailor the runtime permission remedy to the user's group state."""
    try:
        import grp

        group = grp.getgrnam("docker")
    except (ImportError, KeyError):
        return "add your user to the 'docker' group (sudo usermod -aG docker $USER) and log out/in"
    in_group_static = getpass.getuser() in group.gr_mem
    try:
        in_group_live = group.gr_gid in os.getgroups()
    except (AttributeError, OSError):
        in_group_live = False
    if in_group_static and not in_group_live:
        return (
            "you are in the 'docker' group but this shell predates it; "
            "log out/in, or run via: sg docker -c '<command>'"
        )
    if not in_group_static:
        return "add your user to the 'docker' group (sudo usermod -aG docker $USER) and log out/in"
    return "ensure the docker daemon is running and the socket is accessible"
