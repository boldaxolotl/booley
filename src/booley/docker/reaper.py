"""Idle reaper + concurrency cap for Interactive Mode session containers.

ADR 0018 makes this mandatory: VS Code's stop-on-close is unreliable, and an
orphaned session container is an orphaned copy of the repo with forwarded git
credentials. This runs as the long-lived ``booley-reaper`` container (the only
container that mounts the docker socket; session containers keep none) and
periodically stops session containers (label ``booley.role=interactive``) that
are idle past a timeout or exceed the concurrency cap.

Self-contained (stdlib only) so it runs in a minimal ``docker:cli`` + python
image, mirroring ``proxy_entry.py``. It shells out to the ``docker`` CLI.

Idle signal: the in-container MCP server writes an epoch timestamp to
:data:`HEARTBEAT_PATH`; the reaper reads it via ``docker exec``. When no
heartbeat is available (older session, MCP not started) it falls back to the
container's start time, so the timeout degrades to a max-lifetime cap. The
heartbeat is also clamped to that start time: it persists across stop→start in
the writable layer, so a stale one must never make a just-booted container look
idle.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] reaper: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

INTERACTIVE_LABEL = "booley.role=interactive"
PROJECT_LABEL = "booley.project-id"
LICENSE_LABEL = "booley.license-profile"
RELAY_LABEL = "booley.role=license-relay"
_PROJECT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
# Where the in-container MCP server records its last-activity epoch (WS4).
HEARTBEAT_PATH = "/tmp/booley_mcp_heartbeat"

DEFAULT_IDLE_TIMEOUT_S = 7200
DEFAULT_MAX_SESSIONS = 4
DEFAULT_INTERVAL_S = 60


@dataclass(frozen=True)
class SessionContainer:
    """A running interactive session container and its activity timestamps."""

    id: str
    name: str
    started_at: float  # epoch seconds
    last_activity: float  # epoch seconds (heartbeat, else started_at)


@dataclass(frozen=True)
class LicenseOwnership:
    """Validated licensed-resource identity read before a session is stopped."""

    inspected: bool
    project_id: str | None


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------


def select_reap(
    containers: list[SessionContainer],
    *,
    now: float,
    idle_timeout: float,
    max_sessions: int,
) -> list[str]:
    """Return the IDs of containers to stop, given current policy.

    Two independent reasons, unioned:
      * idle — ``now - last_activity > idle_timeout``;
      * over-cap — among the non-idle survivors, the oldest beyond
        *max_sessions* (by ``started_at``, then ``id`` for determinism).
    """
    idle = {c.id for c in containers if now - c.last_activity > idle_timeout}
    survivors = sorted(
        (c for c in containers if c.id not in idle),
        key=lambda c: (c.started_at, c.id),
    )
    over_cap = survivors[: max(0, len(survivors) - max_sessions)]
    reap = idle | {c.id for c in over_cap}
    # Deterministic, stable order for callers/tests.
    return [c.id for c in sorted(containers, key=lambda c: (c.started_at, c.id)) if c.id in reap]


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


def parse_docker_time(value: str) -> float | None:
    """Parse a Docker RFC3339 timestamp (e.g. ``State.StartedAt``) to epoch seconds.

    Docker emits up to nanosecond precision and a trailing ``Z``; normalise both
    to what :func:`datetime.fromisoformat` accepts (microseconds, ``+00:00``).
    Returns ``None`` for empty/zero/unparseable values.
    """
    from datetime import datetime

    value = value.strip()
    if not value or value.startswith("0001-01-01"):  # docker's zero time
        return None
    iso = value.replace("Z", "+00:00")
    # Truncate fractional seconds to 6 digits (microseconds).
    if "." in iso:
        head, _, tail = iso.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[i:]
                break
        iso = f"{head}.{digits[:6]}{rest}"
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        logger.warning("could not parse docker time %r", value)
        return None


# ---------------------------------------------------------------------------
# Docker CLI layer (mockable via the injected *run*)
# ---------------------------------------------------------------------------


def _run(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def list_session_containers(
    run: Callable[..., subprocess.CompletedProcess] = _run,
) -> list[tuple[str, str]]:
    """Return ``(id, name)`` for running interactive session containers."""
    result = run(
        ["ps", "--filter", f"label={INTERACTIVE_LABEL}", "--format", "{{.ID}}\t{{.Names}}"]
    )
    if result.returncode != 0:
        logger.warning("docker ps failed: %s", result.stderr.strip())
        return []
    pairs = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cid, _, name = line.partition("\t")
        pairs.append((cid, name))
    return pairs


def _started_at(cid: str, run=_run) -> float | None:
    result = run(["inspect", cid, "--format", "{{.State.StartedAt}}"])
    if result.returncode != 0:
        return None
    return parse_docker_time(result.stdout)


def _heartbeat(cid: str, run=_run) -> float | None:
    """Read the MCP heartbeat epoch from inside *cid*, or None if unavailable."""
    result = run(["exec", cid, "cat", HEARTBEAT_PATH])
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return None


def collect(run: Callable[..., subprocess.CompletedProcess] = _run) -> list[SessionContainer]:
    """Gather session containers with their activity timestamps."""
    out: list[SessionContainer] = []
    for cid, name in list_session_containers(run):
        started = _started_at(cid, run)
        if started is None:
            logger.warning("skipping %s; no start time", name or cid)
            continue
        heartbeat = _heartbeat(cid, run)
        out.append(
            SessionContainer(
                id=cid,
                name=name,
                started_at=started,
                # A container cannot have been idle since before it booted.
                # The heartbeat file lives in the writable layer and survives
                # stop→start, but only the in-container MCP server rewrites it;
                # a container started without the devcontainer lifecycle hooks
                # (plain ``docker start``) therefore carries the *previous*
                # session's timestamp and would be reaped on the next tick.
                # Clamping to StartedAt gives every restart a full idle_timeout
                # of grace, and is a no-op for a live, heartbeating session.
                last_activity=max(heartbeat, started) if heartbeat is not None else started,
            )
        )
    return out


def stop_container(cid: str, run: Callable[..., subprocess.CompletedProcess] = _run) -> bool:
    result = run(["stop", cid], timeout=60)
    if result.returncode != 0:
        logger.warning("failed to stop %s: %s", cid, result.stderr.strip())
        return False
    return True


def license_ownership(  # noqa: PLR0911 - fail-closed external-label validation ladder
    cid: str, run: Callable[..., subprocess.CompletedProcess] = _run
) -> LicenseOwnership:
    """Resolve whether a session owns a licensed topology, failing closed on drift."""
    result = run(["inspect", cid, "--format", "{{json .Config.Labels}}"])
    if result.returncode != 0:
        logger.warning("cannot inspect license ownership for %s: %s", cid, result.stderr.strip())
        return LicenseOwnership(False, None)
    try:
        labels = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("cannot decode license ownership for %s", cid)
        return LicenseOwnership(False, None)
    if not isinstance(labels, dict):
        logger.warning("invalid license ownership labels for %s", cid)
        return LicenseOwnership(False, None)
    profile = labels.get(LICENSE_LABEL)
    if profile in (None, "none"):
        return LicenseOwnership(True, None)
    project_id = labels.get(PROJECT_LABEL)
    if not isinstance(profile, str) or not isinstance(project_id, str):
        logger.warning("incomplete licensed-resource labels for %s", cid)
        return LicenseOwnership(False, None)
    if _PROJECT_ID_RE.fullmatch(project_id) is None:
        logger.warning("invalid licensed-resource project identity for %s", cid)
        return LicenseOwnership(False, None)
    return LicenseOwnership(True, project_id)


def cleanup_licensed_session(
    cid: str,
    project_id: str,
    run: Callable[..., subprocess.CompletedProcess] = _run,
) -> tuple[str, ...]:
    """Remove one stopped licensed Session and its deterministic owned topology."""
    result = run(["rm", "-f", cid], timeout=60)
    missing = "no such" in result.stderr.lower() or "not found" in result.stderr.lower()
    residual = [] if result.returncode == 0 or missing else [f"container:{cid}"]
    residual.extend(cleanup_license_topology(project_id, run))
    if residual:
        logger.error(
            "licensed session %s was stopped but cleanup left: %s",
            cid,
            ", ".join(residual),
        )
    return tuple(residual)


def cleanup_license_topology(
    project_id: str,
    run: Callable[..., subprocess.CompletedProcess] = _run,
) -> tuple[str, ...]:
    """Remove deterministic relay objects for one validated Project identity."""
    session_id = project_id[:16]
    objects = (
        ("container", f"booley-license-relay-{session_id}"),
        ("network", f"booley-license-outbound-{session_id}"),
        ("network", f"booley-license-private-{session_id}"),
    )
    residual: list[str] = []
    for kind, name in objects:
        args = ["rm", "-f", name] if kind == "container" else ["network", "rm", name]
        result = run(args, timeout=60)
        missing = "no such" in result.stderr.lower() or "not found" in result.stderr.lower()
        if result.returncode != 0 and not missing:
            residual.append(f"{kind}:{name}")
    return tuple(residual)


def cleanup_orphaned_license_topologies(
    run: Callable[..., subprocess.CompletedProcess] = _run,
) -> tuple[str, ...]:
    """Remove relay topologies with no surviving interactive Project container."""
    result = run(["ps", "-aq", "--filter", f"label={RELAY_LABEL}"])
    if result.returncode != 0:
        logger.warning("cannot list orphaned license relays: %s", result.stderr.strip())
        return ()
    cleaned: list[str] = []
    projects: set[str] = set()
    for relay_id in (line.strip() for line in result.stdout.splitlines()):
        if not relay_id:
            continue
        ownership = license_ownership(relay_id, run)
        if ownership.inspected and ownership.project_id is not None:
            projects.add(ownership.project_id)
    for project_id in sorted(projects):
        sessions = run(
            [
                "ps",
                "-aq",
                "--filter",
                f"label={INTERACTIVE_LABEL}",
                "--filter",
                f"label={PROJECT_LABEL}={project_id}",
            ]
        )
        if sessions.returncode != 0 or sessions.stdout.strip():
            continue
        residual = cleanup_license_topology(project_id, run)
        if residual:
            logger.error("orphan license cleanup left: %s", ", ".join(residual))
        else:
            cleaned.append(project_id)
    return tuple(cleaned)


def reap_once(
    *,
    now: float,
    idle_timeout: float,
    max_sessions: int,
    run: Callable[..., subprocess.CompletedProcess] = _run,
) -> list[str]:
    """One reap pass: collect, select, stop. Returns the stopped IDs."""
    containers = collect(run)
    to_reap = select_reap(
        containers,
        now=now,
        idle_timeout=idle_timeout,
        max_sessions=max_sessions,
    )
    stopped = []
    for cid in to_reap:
        ownership = license_ownership(cid, run)
        if not ownership.inspected:
            logger.warning("not stopping %s until its license ownership can be inspected", cid)
            continue
        if not stop_container(cid, run):
            continue
        stopped.append(cid)
        if ownership.project_id is not None:
            cleanup_licensed_session(cid, ownership.project_id, run)
    if stopped:
        logger.info("reaped %d session container(s): %s", len(stopped), ", ".join(stopped))
    cleaned = cleanup_orphaned_license_topologies(run)
    if cleaned:
        logger.info("cleaned %d orphaned license topology(s)", len(cleaned))
    return stopped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        val = int(os.environ.get(name, default))
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default


def main() -> None:
    idle_timeout = _env_int("BOOLEY_IDLE_TIMEOUT_SECONDS", DEFAULT_IDLE_TIMEOUT_S)
    max_sessions = _env_int("BOOLEY_MAX_SESSIONS", DEFAULT_MAX_SESSIONS)
    interval = _env_int("BOOLEY_REAP_INTERVAL_SECONDS", DEFAULT_INTERVAL_S)
    logger.info(
        "starting: idle_timeout=%ds max_sessions=%d interval=%ds",
        idle_timeout,
        max_sessions,
        interval,
    )
    prev_wall = time.time()
    prev_mono = time.monotonic()
    while True:
        wall = time.time()
        mono = time.monotonic()
        # Clock-step guard (QA_REPORT C1b). The idle test compares wall-clock
        # `now` against each container's heartbeat, which is also wall-clock and
        # written by a *different* process — a shared wall clock is unavoidable
        # here (monotonic clocks are not comparable across containers). A forward
        # wall-clock step (classic WSL2 resync after the host sleeps/hibernates)
        # inflates `now - last_activity` for every container at once and would
        # reap live sessions whose next heartbeat simply hasn't landed across the
        # step. Detect the step by measuring the wall advance against true
        # elapsed (monotonic) time and skip one pass so heartbeats catch up.
        wall_advance = wall - prev_wall
        real_elapsed = mono - prev_mono
        prev_wall, prev_mono = wall, mono
        if wall_advance - real_elapsed > max(interval, 1.0):
            logger.warning(
                "wall clock jumped ~%.0fs in %.0fs real time — skipping this "
                "reap pass so heartbeats can catch up (avoids reaping live "
                "sessions on a clock step)",
                wall_advance,
                real_elapsed,
            )
        else:
            try:
                reap_once(now=wall, idle_timeout=idle_timeout, max_sessions=max_sessions)
            except (subprocess.SubprocessError, OSError) as exc:
                logger.warning("reap pass failed: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    main()
