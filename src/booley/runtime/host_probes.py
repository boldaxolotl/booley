"""Presentation-neutral, bounded observations of host capabilities."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ProbeState(StrEnum):
    """Neutral probe outcomes; callers decide whether each is required."""

    MISSING = "missing"
    HEALTHY = "healthy"
    FAILED = "failed"
    PERMISSION = "permission-denied"
    TIMEOUT = "timed-out"


@dataclass(frozen=True)
class HostProbe:
    """A bounded observation with no raw subprocess output."""

    executable: str | None
    state: ProbeState


def probe_docker(
    *,
    executable: str = "docker",
    probe_daemon: bool = True,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    cwd: Path | None = None,
) -> HostProbe:
    """Discover Docker and optionally ask its daemon for information."""
    found = which(executable)
    if not found:
        return HostProbe(None, ProbeState.MISSING)
    if not probe_daemon:
        return HostProbe(found, ProbeState.HEALTHY)
    try:
        result = run(
            [found, "info"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return HostProbe(found, ProbeState.TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return HostProbe(found, ProbeState.FAILED)
    if result.returncode == 0:
        return HostProbe(found, ProbeState.HEALTHY)
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    state = ProbeState.PERMISSION if "permission denied" in output else ProbeState.FAILED
    return HostProbe(found, state)


@dataclass(frozen=True)
class GithubProbe:
    """Independent GitHub CLI authentication and connectivity observations."""

    executable: str | None
    authentication: ProbeState
    connectivity: ProbeState


def probe_github(
    *,
    executable: str = "gh",
    probe_connectivity: bool = True,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    cwd: Path | None = None,
) -> GithubProbe:
    """Run bounded, read-only auth and repository probes independently."""
    found = which(executable)
    if not found:
        return GithubProbe(None, ProbeState.MISSING, ProbeState.MISSING)
    auth = _github_call(found, ("auth", "status"), run, cwd)
    connectivity = (
        _github_call(found, ("repo", "view", "--json", "nameWithOwner"), run, cwd)
        if probe_connectivity
        else ProbeState.MISSING
    )
    return GithubProbe(found, auth, connectivity)


def _github_call(
    executable: str,
    args: tuple[str, ...],
    run: Callable[..., subprocess.CompletedProcess[str]],
    cwd: Path | None,
) -> ProbeState:
    try:
        result = run(
            [executable, *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except subprocess.TimeoutExpired:
        return ProbeState.TIMEOUT
    except (OSError, subprocess.SubprocessError):
        return ProbeState.FAILED
    return ProbeState.HEALTHY if result.returncode == 0 else ProbeState.FAILED
