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
