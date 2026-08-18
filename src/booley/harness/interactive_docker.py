"""Long-lived Docker objects for Interactive Mode (ADR 0018, WS1/WS2).

``booley init`` creates a persistent, Docker-supervised egress sidecar and idle
reaper; "Reopen in Container" later attaches to what already exists. There is no
host daemon — every object runs with ``--restart unless-stopped`` so the Docker
engine supervises it (the lineage rejected a ``booley up`` daemon).

This module is the host-side lifecycle layer: it shells out to ``docker`` to
create/inspect the network, proxy, and reaper, idempotently. All commands go
through :func:`_run_docker` so tests can patch a single seam.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from booley.config.settings import InteractiveConfig

from .devcontainer import EGRESS_NETWORK, PROXY_PORT

logger = logging.getLogger(__name__)

# --- Object names / labels (the network name is shared with devcontainer.py) ---
PROXY_CONTAINER = "booley-proxy"
PROXY_IMAGE = "booley-egress-proxy"
REAPER_CONTAINER = "booley-reaper"
REAPER_IMAGE = "booley-reaper"

PROXY_ROLE_LABEL = "booley.role=egress-proxy"
REAPER_ROLE_LABEL = "booley.role=reaper"

DOCKER_SOCK = "/var/run/docker.sock"

_DOCKER_TIMEOUT = 30

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
) -> subprocess.CompletedProcess:
    """Run ``docker <args>`` capturing output. Never raises on non-zero exit.

    Tests patch this single function to simulate the Docker CLI.
    """
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


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


def network_is_host_isolated(name: str = EGRESS_NETWORK) -> bool:
    """True only when *name* cannot route to the Docker host bridge gateway."""
    try:
        result = _run_docker(
            ["network", "inspect", name, "--format", "{{json .Options}}"],
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    if result.returncode != 0:
        return False
    try:
        options = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return False
    return isinstance(options, dict) and options == {GATEWAY_MODE_OPTION: GATEWAY_MODE_ISOLATED}


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


def image_exists(name: str) -> bool:
    try:
        return _run_docker(["image", "inspect", name], timeout=15).returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def image_id(name: str) -> str | None:
    """Return an image's immutable Docker ID, or ``None`` on inspect failure."""
    try:
        result = _run_docker(["image", "inspect", name, "--format", "{{.Id}}"], timeout=15)
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _container_image_matches(container: str, image: str) -> bool | None:
    """Whether *container* was created from *image*'s current resolved ID.

    Rebuilding a tag does not update containers already created from it. Return
    ``None`` when either inspect is unavailable so a transient Docker failure
    never causes us to remove a healthy sidecar on uncertain evidence.
    """
    try:
        container_result = _run_docker(
            ["container", "inspect", container, "--format", "{{.Image}}"],
            timeout=15,
        )
        image_result = _run_docker(
            ["image", "inspect", image, "--format", "{{.Id}}"],
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    container_id = container_result.stdout.strip()
    image_id = image_result.stdout.strip()
    if container_result.returncode != 0 or image_result.returncode != 0:
        return None
    if not container_id or not image_id:
        return None
    return container_id == image_id


# Per-project persistent home-state volumes (see devcontainer.state_volume_name):
# booley-<app>-state-<canonical-project-hash>.
_STATE_VOLUME_RE = re.compile(r"^booley-(?:claude|codex)-state-.+")
_ISSUED_IMAGE_RE = re.compile(r"^booley-issued-[0-9a-f]{64}:session$")


def tag_image(source: str, target: str) -> None:
    """Create or move *target* to exact image *source*, raising on failure."""
    try:
        result = _run_docker(["image", "tag", source, target], timeout=30)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise RuntimeError(f"cannot retain issued Session Runtime image: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "docker image tag failed"
        raise RuntimeError(f"cannot retain issued Session Runtime image: {detail}")


def issued_image_tags() -> list[str]:
    """Return Booley issuance-keeper tags visible to the local Docker daemon."""
    try:
        result = _run_docker(["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30)
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    return sorted(
        {
            line.strip()
            for line in result.stdout.splitlines()
            if _ISSUED_IMAGE_RE.fullmatch(line.strip())
        }
    )


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


def container_networks(name: str) -> list[str]:
    """Return the names of the networks *name* is attached to (empty on error)."""
    try:
        result = _run_docker(
            ["container", "inspect", name, "--format", "{{json .NetworkSettings.Networks}}"],
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        nets = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return []
    return list(nets) if isinstance(nets, dict) else []


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def ensure_egress_network(name: str = EGRESS_NETWORK) -> bool:
    """Create the host-isolated ``--internal`` egress network if missing.

    Returns True if a network was created, False if it already existed. Raises
    :class:`RuntimeError` if creation fails or an existing network has the
    legacy host-reachable bridge policy.  Docker cannot mutate network driver
    options in place, so the diagnostic deliberately requires an explicit
    Session shutdown and ``booley init --force`` migration.
    """
    if network_exists(name):
        if network_is_internal(name) and network_is_host_isolated(name):
            return False
        raise RuntimeError(
            f"network {name} has an unsafe or stale routing policy; stop active "
            "Sessions, remove the network and booley-proxy, then run booley init --force"
        )
    result = _run_docker(
        [
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--opt",
            f"{GATEWAY_MODE_OPTION}={GATEWAY_MODE_ISOLATED}",
            name,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to create network {name}: {result.stderr.strip()}")
    return True


# ---------------------------------------------------------------------------
# Egress proxy image + container
# ---------------------------------------------------------------------------


def _docker_src_dir(booley_root: Path) -> Path:
    """Build context for the sidecar/reaper images.

    Their sources live in ``src/booley/docker/``; we use that as the build
    context (not the repo root) because the repo-root ``.dockerignore`` is
    whitelist-style and strips ``src/`` — it only ships the wheel for the
    sandbox image. The Dockerfiles therefore COPY by bare filename.
    """
    return booley_root / "src" / "booley" / "docker"


def _build_failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    """Compact actionable tail from a failed Docker image build."""
    output = result.stderr.strip() or result.stdout.strip()
    if not output:
        return f"docker exited {result.returncode} without diagnostic output"
    return "\n".join(output.splitlines()[-20:])


def build_egress_proxy_image(booley_root: Path) -> bool:
    """Build the egress-proxy image from the in-repo Dockerfile. Returns success."""
    docker_data = booley_root / "src" / "booley" / "data" / "docker"
    dockerfile = docker_data / "Dockerfile.egress-proxy"
    if not dockerfile.is_file():
        raise RuntimeError(f"egress-proxy Dockerfile not found at {dockerfile}")
    result = _run_docker(
        ["build", "-t", PROXY_IMAGE, "-f", str(dockerfile), str(_docker_src_dir(booley_root))],
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"egress-proxy image build failed:\n{_build_failure_detail(result)}")
    return True


def ensure_egress_proxy_image(booley_root: Path, *, force: bool = False) -> bool:
    """Ensure the egress-proxy image exists, building it if missing. Returns success."""
    if image_exists(PROXY_IMAGE) and not force:
        return True
    return build_egress_proxy_image(booley_root)


def _proxy_run_args(allowlist: tuple[str, ...] | None) -> list[str]:
    """Build the ``docker run`` argv for the proxy (attached to default bridge)."""
    args = [
        "run",
        "-d",
        "--name",
        PROXY_CONTAINER,
        "--restart",
        "unless-stopped",
        "--label",
        PROXY_ROLE_LABEL,
        "-e",
        f"PROXY_PORT={PROXY_PORT}",
    ]
    if allowlist:
        # proxy_entry.py reads PROXY_ALLOWLIST as a JSON array (extends defaults).
        args += ["-e", f"PROXY_ALLOWLIST={json.dumps(list(allowlist))}"]
    args.append(PROXY_IMAGE)
    return args


def ensure_egress_proxy(*, allowlist: tuple[str, ...] | None = None) -> str:
    """Ensure the dual-homed ``booley-proxy`` container is up and reachable.

    The proxy is dual-homed: it runs on the default bridge (its external route)
    and is also connected to the ``--internal`` egress network, where session
    containers reach it by name. Idempotent.

    Returns one of ``"created"``, ``"started"``, ``"running"``. Raises
    :class:`RuntimeError` on failure.
    """
    if not container_exists(PROXY_CONTAINER):
        result = _run_docker(_proxy_run_args(allowlist))
        if result.returncode != 0:
            raise RuntimeError(f"failed to start {PROXY_CONTAINER}: {result.stderr.strip()}")
        status = "created"
    elif not container_running(PROXY_CONTAINER):
        result = _run_docker(["start", PROXY_CONTAINER])
        if result.returncode != 0:
            raise RuntimeError(f"failed to start {PROXY_CONTAINER}: {result.stderr.strip()}")
        status = "started"
    else:
        status = "running"

    _connect_to_egress(PROXY_CONTAINER)
    return status


def _connect_to_egress(container: str, network: str = EGRESS_NETWORK) -> None:
    """Attach *container* to the egress network if not already attached.

    ``docker network connect`` errors when already connected; treat that as
    success so the call stays idempotent.
    """
    if network in container_networks(container):
        return
    result = _run_docker(["network", "connect", network, container])
    if result.returncode != 0 and "already exists" not in result.stderr.lower():
        raise RuntimeError(f"failed to connect {container} to {network}: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Idle reaper + concurrency cap (WS2)
# ---------------------------------------------------------------------------


def build_reaper_image(booley_root: Path) -> bool:
    """Build the reaper image from the in-repo Dockerfile. Returns success."""
    dockerfile = booley_root / "src" / "booley" / "data" / "docker" / "Dockerfile.reaper"
    if not dockerfile.is_file():
        raise RuntimeError(f"reaper Dockerfile not found at {dockerfile}")
    result = _run_docker(
        ["build", "-t", REAPER_IMAGE, "-f", str(dockerfile), str(_docker_src_dir(booley_root))],
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"reaper image build failed:\n{_build_failure_detail(result)}")
    return True


def ensure_reaper_image(booley_root: Path, *, force: bool = False) -> bool:
    """Ensure the reaper image exists, building it if missing. Returns success."""
    if image_exists(REAPER_IMAGE) and not force:
        return True
    return build_reaper_image(booley_root)


def _reaper_run_args(cfg: InteractiveConfig) -> list[str]:
    """Build the ``docker run`` argv for the reaper container."""
    return [
        "run",
        "-d",
        "--name",
        REAPER_CONTAINER,
        "--restart",
        "unless-stopped",
        "--label",
        REAPER_ROLE_LABEL,
        # The reaper is the ONLY container with the socket; sessions keep none.
        "-v",
        f"{DOCKER_SOCK}:{DOCKER_SOCK}",
        "-e",
        f"BOOLEY_IDLE_TIMEOUT_SECONDS={cfg.idle_timeout_seconds}",
        "-e",
        f"BOOLEY_MAX_SESSIONS={cfg.max_sessions}",
        REAPER_IMAGE,
    ]


def ensure_reaper(cfg: InteractiveConfig, *, force: bool = False) -> str:
    """Ensure the ``booley-reaper`` supervisor container is up. Idempotent.

    Returns one of ``"created"``, ``"recreated"``, ``"started"``,
    ``"running"``. Raises :class:`RuntimeError` on failure. A rebuilt image
    moves the tag without updating an existing container, so a container pinned
    to the superseded image is replaced. Policy env (idle timeout / max
    sessions) is baked at create time, so *force* also replaces the container
    even when its image ID still matches.
    """
    if not container_exists(REAPER_CONTAINER):
        result = _run_docker(_reaper_run_args(cfg))
        if result.returncode != 0:
            raise RuntimeError(f"failed to start {REAPER_CONTAINER}: {result.stderr.strip()}")
        return "created"
    if force or _container_image_matches(REAPER_CONTAINER, REAPER_IMAGE) is False:
        result = _run_docker(["rm", "-f", REAPER_CONTAINER])
        if result.returncode != 0:
            raise RuntimeError(f"failed to replace {REAPER_CONTAINER}: {result.stderr.strip()}")
        result = _run_docker(_reaper_run_args(cfg))
        if result.returncode != 0:
            raise RuntimeError(f"failed to start {REAPER_CONTAINER}: {result.stderr.strip()}")
        return "recreated"
    if not container_running(REAPER_CONTAINER):
        result = _run_docker(["start", REAPER_CONTAINER])
        if result.returncode != 0:
            raise RuntimeError(f"failed to start {REAPER_CONTAINER}: {result.stderr.strip()}")
        return "started"
    return "running"
