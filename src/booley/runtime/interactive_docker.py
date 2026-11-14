"""Host-side Docker probes and inventory for Interactive Mode (ADR 0018).

This module shells out to ``docker`` to inspect networks, containers, images,
issued-image keepers, and per-project state volumes, and creates the unproxied
network. The egress proxy and idle reaper are owned by ``harness/host_sidecars``,
which reads the shared object names defined here. All commands go through
:func:`_run_docker` so tests can patch a single seam.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from booley.core.boundary import BoundaryError
from booley.runtime.devcontainer import EGRESS_NETWORK, UNPROXIED_NETWORK
from booley.runtime.platform_paths import run_captured_bounded

if TYPE_CHECKING:
    from booley.runtime.image_identity import Comparison, ImageMetadata

logger = logging.getLogger(__name__)

# --- Object names / labels (the network name is shared with devcontainer.py) ---
PROXY_CONTAINER = "booley-proxy"
PROXY_IMAGE = "booley-egress-proxy"
REAPER_CONTAINER = "booley-reaper"
REAPER_IMAGE = "booley-reaper"
# Refresh and rebuild park the replaced Session as ``<name>`` + this suffix.
REFRESH_BACKUP_SUFFIX = "-pre-refresh"

DOCKER_SOCK = "/var/run/docker.sock"

_DOCKER_TIMEOUT = 30
_METADATA_TIMEOUT = 15

# ``--internal`` removes external routing but, by itself, Docker still assigns
# the host a bridge-gateway address reachable from attached containers.  The
# isolated gateway mode removes that host-side address as well.
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
GATEWAY_MODE_ISOLATED = "isolated"


# ---------------------------------------------------------------------------
# Low-level docker seam
# ---------------------------------------------------------------------------


def _run_docker(
    args: list[str],
    *,
    timeout: int = _DOCKER_TIMEOUT,
    executable: str = "docker",
) -> subprocess.CompletedProcess:
    """Run ``docker <args>`` capturing text output. Never raises on non-zero exit.

    Honors *timeout* even when a descendant of the Docker CLI keeps the output
    pipe open: the CLI runs in its own process group, which is killed as a
    whole on timeout (``subprocess.TimeoutExpired``). Because of the new
    session the CLI has no controlling terminal. Raises ``FileNotFoundError``
    when *executable* is absent.

    Tests patch this single function to simulate the Docker CLI.
    """
    return run_captured_bounded([executable, *args], timeout=timeout)


def relay_docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Bounded ``docker <args>`` in the shape the FlexNet relay's ``Runner`` expects.

    ``booley.eda`` cannot import this package, so runtime composers inject this
    function as the relay's runner. It shares :func:`_run_docker`, so a hung Docker
    CLI is killed as a whole process tree within *timeout* plus the bounded margin.
    """
    return _run_docker(args, timeout=timeout)


# ---------------------------------------------------------------------------
# Existence / status probes
# ---------------------------------------------------------------------------


def network_exists(name: str = EGRESS_NETWORK) -> bool:
    try:
        return _run_docker(["network", "inspect", name], timeout=15).returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def network_is_internal(name: str = EGRESS_NETWORK) -> bool:
    """True if the named network exists and is ``--internal`` (no external route)."""
    try:
        result = _run_docker(
            ["network", "inspect", name, "--format", "{{.Internal}}"],
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def container_exists(name: str) -> bool:
    try:
        return _run_docker(["container", "inspect", name], timeout=15).returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def container_running(name: str) -> bool:
    try:
        result = _run_docker(
            ["container", "inspect", name, "--format", "{{.State.Running}}"],
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def image_exists(name: str, *, executable: str = "docker", timeout: int = 15) -> bool:
    """Observe local image availability without pulling or starting an image."""
    try:
        return (
            _run_docker(
                ["image", "inspect", name], timeout=timeout, executable=executable
            ).returncode
            == 0
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


class ImageInspectError(RuntimeError):
    """Docker could not say whether an image exists (as opposed to it being absent)."""


def _absent_or_inspect_error(
    reference: str, result: subprocess.CompletedProcess
) -> ImageInspectError | None:
    """Classify a non-zero ``docker image inspect``: ``None`` means no such image."""
    detail = result.stderr.strip() or result.stdout.strip()
    if re.search(r"\bno such (?:image|object)\b", detail, re.IGNORECASE):
        return None
    return ImageInspectError(
        f"cannot inspect Docker image {reference!r}: "
        + (detail or f"Docker exited {result.returncode}")
    )


def _inspect_image(
    reference: str, extra: list[str], *, timeout: int, executable: str = "docker"
) -> subprocess.CompletedProcess | None:
    """Run ``docker image inspect``; ``None`` when the image does not exist.

    Only Docker's own "no such image" answer means absent; a missing or
    non-executable CLI, an unreachable daemon, a timeout, and any other
    non-zero exit raise :class:`ImageInspectError` naming *reference*.
    """
    try:
        result = _run_docker(
            ["image", "inspect", reference, *extra],
            timeout=timeout,
            executable=executable,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageInspectError(f"cannot inspect Docker image {reference!r}: {exc}") from exc
    if result.returncode == 0:
        return result
    error = _absent_or_inspect_error(reference, result)
    if error is not None:
        raise error
    return None


def image_id(reference: str) -> str | None:
    """Return an image's immutable Docker ID, or ``None`` when the image does not exist.

    The one image-ID lookup for Booley's runtime and harness. Only Docker's own
    "no such image" answer means absent; a missing or non-executable CLI, an
    unreachable daemon, a timeout, any other non-zero exit, and an empty answer
    raise :class:`ImageInspectError` naming *reference* and the cause.
    """
    result = _inspect_image(reference, ["--format", "{{.Id}}"], timeout=_DOCKER_TIMEOUT)
    if result is None:
        return None
    value = result.stdout.strip()
    if value:
        return value
    raise ImageInspectError(
        f"cannot inspect Docker image {reference!r}: Docker returned no image ID"
    )


def inspect_image_metadata(
    reference: str,
    *,
    executable: str = "docker",
) -> ImageMetadata | None:
    """Return the full metadata of exactly *reference*, or ``None`` when it does not exist.

    Same contract as :func:`image_id`: only "no such image" is absent. Failures
    to run Docker and malformed output raise :class:`ImageInspectError`.
    """
    from booley.runtime.image_identity import decode_image_metadata

    result = _inspect_image(reference, [], timeout=_METADATA_TIMEOUT, executable=executable)
    if result is None:
        return None
    try:
        return decode_image_metadata(reference, json.loads(result.stdout))
    except (json.JSONDecodeError, BoundaryError) as exc:
        raise ImageInspectError(
            f"cannot inspect Docker image {reference!r}: invalid metadata: {exc}"
        ) from exc


def compare_issued_build(image: str, *, executable: str = "docker") -> Comparison:
    """Compare one exact issued image with the canonical host installation."""
    from booley.runtime.image_identity import (
        Comparison,
        Status,
        build_identity,
        compare_build_identity,
        current_host_build_identity,
    )

    expected = current_host_build_identity()
    try:
        observed = inspect_image_metadata(image, executable=executable)
    except ImageInspectError as exc:
        return Comparison(Status.UNKNOWN, str(exc))
    if expected is None or observed is None:
        return Comparison(Status.UNKNOWN)
    return compare_build_identity(expected, build_identity(observed))


def compare_issued_image(
    spec_image: str, configured: str, *, project_root: Path, executable: str = "docker"
) -> Comparison:
    """Compare issued selection using managed provenance or external exact IDs."""
    from booley.runtime import project_image
    from booley.runtime.image_identity import Comparison, Status
    from booley.runtime.image_provenance import is_local_image_id

    if not is_local_image_id(spec_image):
        return Comparison(Status.MISMATCH)
    if project_image.is_managed_sandbox_image(project_root, configured):
        return compare_issued_selection(
            spec_image, configured, project_root=project_root, executable=executable
        )
    return compare_issued_reference(spec_image, configured, executable=executable)


def compare_issued_selection(
    issued_reference: str,
    configured_reference: str,
    *,
    executable: str = "docker",
    project_root: Path | None = None,
) -> Comparison:
    """Inspect and compare issued/configured selection without mutation."""
    from booley.runtime.image_identity import Comparison, Status, compare_logical_selection

    def inspect(reference: str) -> ImageMetadata | None:
        return inspect_image_metadata(reference, executable=executable)

    try:
        return compare_logical_selection(
            issued_reference,
            configured_reference,
            inspect,
            project_root=project_root,
            executable=executable,
        )
    except ImageInspectError as exc:
        return Comparison(Status.UNKNOWN, str(exc))


def compare_issued_reference(
    issued_reference: str,
    configured_reference: str,
    *,
    executable: str = "docker",
) -> Comparison:
    """Compare exact IDs when no trusted logical provenance contract exists."""
    from booley.runtime.image_identity import Comparison, Status

    try:
        issued = inspect_image_metadata(issued_reference, executable=executable)
        configured = inspect_image_metadata(configured_reference, executable=executable)
    except ImageInspectError as exc:
        return Comparison(Status.UNKNOWN, str(exc))
    if issued is None or configured is None:
        return Comparison(Status.UNKNOWN)
    status = Status.MATCH if issued.image_id == configured.image_id else Status.MISMATCH
    return Comparison(status)


# Per-project persistent home-state volumes (see devcontainer.state_volume_name):
# booley-<app>-state-<canonical-project-hash>.
_STATE_VOLUME_RE = re.compile(r"^booley-(?:claude|codex)-state-.+")
_ISSUED_IMAGE_RE = re.compile(r"^booley-issued-[0-9a-f]{64}:session$")


def tag_image(source: str, target: str) -> None:
    """Create or move *target* to exact image *source*, raising on failure."""
    try:
        result = _run_docker(["image", "tag", source, target], timeout=30)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise RuntimeError(f"cannot retain issued Sandbox Image: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "docker image tag failed"
        raise RuntimeError(f"cannot retain issued Sandbox Image: {detail}")


def remove_image_tag(target: str) -> None:
    """Release one tag without force; Docker may reclaim otherwise unreferenced bytes."""
    try:
        result = _run_docker(["image", "rm", "--no-prune", target], timeout=30)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise RuntimeError(f"cannot remove issued Sandbox Image keeper: {exc}") from exc
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip()
    if re.search(r"\bno such (?:image|object)\b", detail, re.IGNORECASE):
        return
    raise RuntimeError(
        "cannot remove issued Sandbox Image keeper: " + (detail or "docker image rm failed")
    )


def strict_docker(
    args: list[str],
    *,
    context: str = "cannot observe issued Sandbox Image keepers",
    timeout: int = 30,
) -> str:
    """Run one Docker command, raising ``RuntimeError`` with *context* on any failure."""
    try:
        result = _run_docker(args, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"{context}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"{context}: "
            + (result.stderr.strip() or result.stdout.strip() or "Docker command failed")
        )
    return result.stdout


def validated_image_id(value: str) -> str:
    """Validate an immutable Docker image ID at the cleanup boundary."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise RuntimeError("invalid immutable Docker image ID during keeper inspection")
    return value


def issued_image_tags() -> tuple[str, ...]:
    """List exact keeper tags, raising when the daemon observation is incomplete."""
    output = strict_docker(["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
    tags = set()
    for line in output.splitlines():
        if not line or line != line.strip() or len(line.split(":")) < 2:
            raise RuntimeError("malformed Docker image listing during keeper inspection")
        if _ISSUED_IMAGE_RE.fullmatch(line):
            tags.add(line)
    return tuple(sorted(tags))


def container_image_ids_strict() -> frozenset[str]:
    """Observe immutable images of ALL containers, including stopped and foreign ones."""
    output = strict_docker(["container", "ls", "--all", "--quiet", "--no-trunc"])
    ids = output.splitlines()
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in ids):
        raise RuntimeError("malformed Docker container listing during keeper inspection")
    images = set()
    for container in ids:
        image = strict_docker(["container", "inspect", container, "--format", "{{.Image}}"])
        images.add(validated_image_id(image.strip()))
    return frozenset(images)


def state_volumes() -> list[str]:
    """Return names of Interactive Mode persistent home-state volumes.

    These survive container rebuilds by design (they hold the agent's plans,
    session transcripts, and todos); the idle reaper deliberately leaves them
    alone. ``booley doctor`` uses this to surface orphans from removed projects.
    Empty on any error.
    """
    try:
        result = _run_docker(["volume", "ls", "--format", "{{.Name}}"], timeout=15)
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    return [
        name
        for line in result.stdout.splitlines()
        if (name := line.strip()) and _STATE_VOLUME_RE.match(name)
    ]


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def ensure_unproxied_network() -> bool:
    """Create the routed bridge every unproxied Sandbox joins, if missing.

    Returns True if the network was created, False if it already existed.
    Callers hold the host lifecycle lock, so no create race is handled here.
    Raises :class:`RuntimeError` if creation fails or a network of that name
    is ``--internal`` and so cannot route outside Docker.
    """
    if network_exists(UNPROXIED_NETWORK):
        if not network_is_internal(UNPROXIED_NETWORK):
            return False
        raise RuntimeError(
            f"network {UNPROXIED_NETWORK} cannot route outside Docker; remove it when no "
            f"container uses it (`docker network rm {UNPROXIED_NETWORK}`) and start again"
        )
    result = _run_docker(["network", "create", "--driver", "bridge", UNPROXIED_NETWORK])
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to create network {UNPROXIED_NETWORK}: {result.stderr.strip()}"
        )
    return True
