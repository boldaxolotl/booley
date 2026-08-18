"""Hardened Docker topology and lifecycle for one Session FlexNet relay."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from booley.paths import package_data_dir

from .flexnet_relay import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_CONNECTIONS,
    ENV_CONNECT_TIMEOUT,
    ENV_IDLE_TIMEOUT,
    ENV_LMGRD_PORT,
    ENV_MAX_CONNECTIONS,
    ENV_SERVER_HOSTID,
    ENV_UPSTREAM_IPV4,
    ENV_VENDOR_PORT,
    RelayConfigError,
    validate_server_hostid,
    validate_upstream_ipv4,
)

RELAY_IMAGE = "booley-flexnet-relay:1"
RELAY_ALIAS = "booley-license-xilinx"
ROLE_LABEL = "booley.role=license-relay"
SESSION_LABEL = "booley.session-id"
PRIVATE_NETWORK_LABEL = "booley.role=license-private-network"
OUTBOUND_NETWORK_LABEL = "booley.role=license-outbound-network"
DOCKER_TIMEOUT = 30
BUILD_TIMEOUT = 600
DEFAULT_HEALTH_ATTEMPTS = 60
DEFAULT_HEALTH_POLL_INTERVAL = 0.2
GATEWAY_MODE_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
GATEWAY_MODE_ISOLATED = "isolated"

Runner = Callable[[list[str], int], subprocess.CompletedProcess[str]]
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELAY_ENV_NAMES = frozenset(
    {
        ENV_UPSTREAM_IPV4,
        ENV_SERVER_HOSTID,
        ENV_LMGRD_PORT,
        ENV_VENDOR_PORT,
        ENV_CONNECT_TIMEOUT,
        ENV_IDLE_TIMEOUT,
        ENV_MAX_CONNECTIONS,
    }
)


class RelayDockerError(RuntimeError):
    """Docker could not create, validate, or clean up the relay topology."""


@dataclass(frozen=True)
class RelayProfile:
    """Host-authorized fixed FlexNet destination passed to Docker."""

    server_ipv4: str
    server_hostid: str
    lmgrd_port: int
    vendor_port: int

    def __post_init__(self) -> None:
        canonical = str(validate_upstream_ipv4(self.server_ipv4))
        object.__setattr__(self, "server_ipv4", canonical)
        validate_server_hostid(self.server_hostid)
        if not _valid_port(self.lmgrd_port) or not _valid_port(self.vendor_port):
            raise RelayConfigError("FlexNet ports must be integers from 1 through 65535")
        if self.lmgrd_port == self.vendor_port:
            raise RelayConfigError("lmgrd and vendor ports must be distinct")


@dataclass(frozen=True)
class RelayResources:
    """Deterministic Docker object names for one Session relay."""

    session_id: str
    private_network: str
    outbound_network: str
    relay_container: str


def resources_for_session(session_identity: str) -> RelayResources:
    """Derive non-sensitive deterministic object names from exact Session identity."""
    if not isinstance(session_identity, str) or not session_identity:
        raise RelayDockerError("Session identity must be a non-empty string")
    digest = hashlib.sha256(session_identity.encode("utf-8")).hexdigest()[:16]
    return RelayResources(
        digest,
        f"booley-license-private-{digest}",
        f"booley-license-outbound-{digest}",
        f"booley-license-relay-{digest}",
    )


def relay_image_build_argv(*, image: str = RELAY_IMAGE) -> list[str]:
    """Build the packaged relay image from only the standalone relay module."""
    if not isinstance(image, str) or not image.strip():
        raise RelayDockerError("relay image reference must be a non-empty string")
    dockerfile = package_data_dir() / "docker" / "Dockerfile.flexnet-relay"
    context = Path(__file__).resolve().parent
    if not dockerfile.is_file() or not (context / "flexnet_relay.py").is_file():
        raise RelayDockerError("packaged FlexNet relay image sources are missing")
    return ["image", "build", "--tag", image, "--file", str(dockerfile), str(context)]


def build_relay_image(*, image: str = RELAY_IMAGE, runner: Runner | None = None) -> None:
    """Build the pinned minimal relay image or raise with Docker's diagnostic."""
    run = runner or _run_docker
    result = run(relay_image_build_argv(image=image), BUILD_TIMEOUT)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Docker returned no detail"
        raise RelayDockerError(f"could not build FlexNet relay image: {detail}")


def relay_image_exists(*, image: str = RELAY_IMAGE, runner: Runner | None = None) -> bool:
    """Return whether Docker can resolve the exact production relay image."""
    run = runner or _run_docker
    try:
        return run(["image", "inspect", image], DOCKER_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_relay_image(
    *, image: str = RELAY_IMAGE, force: bool = False, runner: Runner | None = None
) -> bool:
    """Build the relay image when absent or forced; return whether it was built."""
    if not force and relay_image_exists(image=image, runner=runner):
        return False
    build_relay_image(image=image, runner=runner)
    return True


def resolve_relay_image_id(*, image: str = RELAY_IMAGE, runner: Runner | None = None) -> str:
    """Resolve one relay tag/digest to the immutable local Docker image ID."""
    run = runner or _run_docker
    image_id = _inspect_value(run, "image", image, "{{.Id}}")
    if _IMAGE_ID_RE.fullmatch(image_id) is None:
        raise RelayDockerError(f"Docker returned an invalid relay image ID for {image!r}")
    return image_id


def private_network_create_argv(
    resources: RelayResources, *, issuance_labels: tuple[str, ...] = ()
) -> list[str]:
    """Build the isolated Session/client network creation command."""
    return _append_labels(
        [
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--opt",
            f"{GATEWAY_MODE_OPTION}={GATEWAY_MODE_ISOLATED}",
            "--label",
            PRIVATE_NETWORK_LABEL,
            "--label",
            f"{SESSION_LABEL}={resources.session_id}",
        ],
        issuance_labels,
        resources.private_network,
    )


def outbound_network_create_argv(
    resources: RelayResources, *, issuance_labels: tuple[str, ...] = ()
) -> list[str]:
    """Build the relay-only routed network creation command."""
    return _append_labels(
        [
            "network",
            "create",
            "--driver",
            "bridge",
            "--label",
            OUTBOUND_NETWORK_LABEL,
            "--label",
            f"{SESSION_LABEL}={resources.session_id}",
        ],
        issuance_labels,
        resources.outbound_network,
    )


def relay_run_argv(
    resources: RelayResources,
    profile: RelayProfile,
    *,
    image: str = RELAY_IMAGE,
    issuance_labels: tuple[str, ...] = (),
) -> list[str]:
    """Build a hardened relay container command with no host authority surfaces."""
    if not isinstance(image, str) or not image.strip():
        raise RelayDockerError("relay image reference must be a non-empty string")
    argv = [
        "container",
        "run",
        "-d",
        "--name",
        resources.relay_container,
        "--network",
        resources.outbound_network,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=1m",
        "--user",
        "65532:65532",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "64m",
        "--cpus",
        "0.50",
        "--ulimit",
        "nofile=256:256",
        "--restart",
        "no",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=2",
        "--label",
        ROLE_LABEL,
        "--label",
        f"{SESSION_LABEL}={resources.session_id}",
    ]
    for label in issuance_labels:
        argv += ["--label", label]
    argv += [
        "--env",
        f"{ENV_UPSTREAM_IPV4}={profile.server_ipv4}",
        "--env",
        f"{ENV_SERVER_HOSTID}={profile.server_hostid}",
        "--env",
        f"{ENV_LMGRD_PORT}={profile.lmgrd_port}",
        "--env",
        f"{ENV_VENDOR_PORT}={profile.vendor_port}",
        "--env",
        f"{ENV_CONNECT_TIMEOUT}={DEFAULT_CONNECT_TIMEOUT:g}",
        "--env",
        f"{ENV_IDLE_TIMEOUT}={DEFAULT_IDLE_TIMEOUT:g}",
        "--env",
        f"{ENV_MAX_CONNECTIONS}={DEFAULT_MAX_CONNECTIONS}",
        image,
    ]
    return argv


def relay_private_connect_argv(resources: RelayResources, profile: RelayProfile) -> list[str]:
    """Attach only the relay to the private network under both required aliases."""
    return [
        "network",
        "connect",
        "--alias",
        RELAY_ALIAS,
        "--alias",
        profile.server_hostid,
        resources.private_network,
        resources.relay_container,
    ]


def session_private_connect_argv(resources: RelayResources, session_container: str) -> list[str]:
    """Attach a Session Runtime only to its private license network."""
    if not isinstance(session_container, str) or not session_container:
        raise RelayDockerError("Session container name must be non-empty")
    return ["network", "connect", resources.private_network, session_container]


def provision_relay(
    profile: RelayProfile,
    session_identity: str,
    *,
    image: str = RELAY_IMAGE,
    runner: Runner | None = None,
    health_attempts: int = DEFAULT_HEALTH_ATTEMPTS,
    poll_interval: float = DEFAULT_HEALTH_POLL_INTERVAL,
    issuance_labels: tuple[str, ...] = (),
) -> RelayResources:
    """Create, connect, and health-gate one topology with reverse-order rollback."""
    if health_attempts < 1 or poll_interval < 0:
        raise RelayDockerError("health polling bounds are invalid")
    run = runner or _run_docker
    resources = resources_for_session(session_identity)
    image_id = resolve_relay_image_id(image=image, runner=run)
    created: list[tuple[str, str]] = []
    try:
        _checked(
            run,
            private_network_create_argv(resources, issuance_labels=issuance_labels),
            "create private license network",
        )
        created.append(("network", resources.private_network))
        _checked(
            run,
            outbound_network_create_argv(resources, issuance_labels=issuance_labels),
            "create outbound license network",
        )
        created.append(("network", resources.outbound_network))
        _checked(
            run,
            relay_run_argv(
                resources,
                profile,
                image=image_id,
                issuance_labels=issuance_labels,
            ),
            "create license relay",
        )
        created.append(("container", resources.relay_container))
        _checked(run, relay_private_connect_argv(resources, profile), "connect license relay")
        _wait_healthy(run, resources.relay_container, health_attempts, poll_interval)
        return resources
    except (OSError, subprocess.SubprocessError, RelayDockerError) as exc:
        residual = _rollback(run, created)
        suffix = f"; rollback left: {', '.join(residual)}" if residual else ""
        raise RelayDockerError(f"license relay startup failed: {exc}{suffix}") from exc


def remove_relay(resources: RelayResources, *, runner: Runner | None = None) -> None:
    """Remove one Session relay and both networks, reporting every residual."""
    run = runner or _run_docker
    objects = [
        ("network", resources.private_network),
        ("network", resources.outbound_network),
        ("container", resources.relay_container),
    ]
    residual = _rollback(run, objects)
    if residual:
        raise RelayDockerError(f"failed to remove license relay objects: {', '.join(residual)}")


def recreate_relay(
    profile: RelayProfile,
    session_identity: str,
    *,
    image: str = RELAY_IMAGE,
    runner: Runner | None = None,
    health_attempts: int = DEFAULT_HEALTH_ATTEMPTS,
    poll_interval: float = DEFAULT_HEALTH_POLL_INTERVAL,
    issuance_labels: tuple[str, ...] = (),
) -> RelayResources:
    """Remove exact prior objects, then provision a fresh healthy topology."""
    resources = resources_for_session(session_identity)
    remove_relay(resources, runner=runner)
    return provision_relay(
        profile,
        session_identity,
        image=image,
        runner=runner,
        health_attempts=health_attempts,
        poll_interval=poll_interval,
        issuance_labels=issuance_labels,
    )


def connect_session(
    resources: RelayResources,
    session_container: str,
    *,
    runner: Runner | None = None,
) -> None:
    """Attach the Session only to the private license network."""
    run = runner or _run_docker
    _checked(
        run,
        session_private_connect_argv(resources, session_container),
        "connect Session Runtime to private license network",
    )


def validate_relay(
    resources: RelayResources,
    session_container: str | None,
    profile: RelayProfile,
    *,
    issuance_labels: tuple[str, ...],
    image: str = RELAY_IMAGE,
    runner: Runner | None = None,
) -> None:
    """Fail closed unless the complete relay security contract matches issuance."""
    run = runner or _run_docker
    image_id = resolve_relay_image_id(image=image, runner=run)
    expected = {
        ROLE_LABEL,
        f"{SESSION_LABEL}={resources.session_id}",
        *issuance_labels,
    }
    actual = _inspect_labels(run, "container", resources.relay_container)
    if actual != expected:
        raise RelayDockerError("license relay labels differ from host issuance")
    if (
        _inspect_value(run, "container", resources.relay_container, "{{.State.Health.Status}}")
        != "healthy"
    ):
        raise RelayDockerError("license relay is not healthy")
    expected_env = {
        f"{ENV_UPSTREAM_IPV4}={profile.server_ipv4}",
        f"{ENV_SERVER_HOSTID}={profile.server_hostid}",
        f"{ENV_LMGRD_PORT}={profile.lmgrd_port}",
        f"{ENV_VENDOR_PORT}={profile.vendor_port}",
        f"{ENV_CONNECT_TIMEOUT}={DEFAULT_CONNECT_TIMEOUT:g}",
        f"{ENV_IDLE_TIMEOUT}={DEFAULT_IDLE_TIMEOUT:g}",
        f"{ENV_MAX_CONNECTIONS}={DEFAULT_MAX_CONNECTIONS}",
    }
    _validate_container_contract(
        run,
        resources.relay_container,
        image_id=image_id,
        expected_env=expected_env,
    )
    relay_networks = _inspect_networks(run, resources.relay_container)
    if relay_networks != {resources.private_network, resources.outbound_network}:
        raise RelayDockerError("license relay network topology has drifted")
    if session_container is not None:
        session_networks = _inspect_networks(run, session_container)
        if resources.private_network not in session_networks:
            raise RelayDockerError(
                "Session Runtime is not attached to its private license network"
            )
        if resources.outbound_network in session_networks:
            raise RelayDockerError("Session Runtime is attached to the relay outbound network")
    _validate_network(
        run,
        resources.private_network,
        True,
        PRIVATE_NETWORK_LABEL,
        resources.session_id,
        issuance_labels,
    )
    _validate_network_endpoints(
        run,
        resources,
        profile,
        session_container,
        issuance_labels,
    )
    _validate_network(
        run,
        resources.outbound_network,
        False,
        OUTBOUND_NETWORK_LABEL,
        resources.session_id,
        issuance_labels,
    )


def cleanup_project_resources(
    project_root: Path, *, runner: Runner | None = None
) -> tuple[str, ...]:
    """Remove exact Project-labeled containers before networks and report residue."""
    run = runner or _run_docker
    project_id = hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()
    label = f"booley.project-id={project_id}"
    residual: list[str] = []
    for kind, list_args in (
        ("container", ["container", "ls", "-aq", "--filter", f"label={label}"]),
        ("network", ["network", "ls", "-q", "--filter", f"label={label}"]),
    ):
        try:
            listed = run(list_args, DOCKER_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            residual.append(f"<{kind}-list-failed>")
            continue
        if listed.returncode != 0:
            residual.append(f"<{kind}-list-failed>")
            continue
        for name in (line.strip() for line in listed.stdout.splitlines()):
            if not name:
                continue
            args = (
                ["container", "rm", "-f", name] if kind == "container" else ["network", "rm", name]
            )
            try:
                removed = run(args, DOCKER_TIMEOUT)
            except (OSError, subprocess.SubprocessError):
                residual.append(f"{kind}:{name}")
                continue
            if removed.returncode != 0 and not _is_missing(removed.stderr):
                residual.append(f"{kind}:{name}")
    return tuple(residual)


def _run_docker(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _checked(run: Runner, args: list[str], action: str) -> None:
    result = run(args, DOCKER_TIMEOUT)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Docker returned no detail"
        raise RelayDockerError(f"could not {action}: {detail}")


def _wait_healthy(run: Runner, container: str, attempts: int, interval: float) -> None:
    command = ["container", "inspect", container, "--format", "{{.State.Health.Status}}"]
    last = "unknown"
    for attempt in range(attempts):
        result = run(command, DOCKER_TIMEOUT)
        last = result.stdout.strip().lower() if result.returncode == 0 else "inspect-error"
        if last == "healthy":
            return
        if attempt + 1 < attempts and interval:
            time.sleep(interval)
    raise RelayDockerError(f"license relay did not become healthy (last status: {last})")


def _validate_container_contract(
    run: Runner,
    name: str,
    *,
    image_id: str,
    expected_env: set[str],
) -> None:
    state = _inspect_object(run, "container", name)
    config = state.get("Config")
    host = state.get("HostConfig")
    mounts = state.get("Mounts")
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise RelayDockerError("Docker returned invalid relay container state")
    _validate_container_identity(state, config, host, mounts, image_id)
    if not _security_options_match(host.get("SecurityOpt")):
        raise RelayDockerError("license relay security options have drifted")
    if host.get("Tmpfs") != {"/tmp": "rw,noexec,nosuid,nodev,size=1m"}:
        raise RelayDockerError("license relay tmpfs policy has drifted")
    if not _limits_match(host):
        raise RelayDockerError("license relay resource limits have drifted")
    if host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}:
        raise RelayDockerError("license relay restart policy has drifted")
    if host.get("LogConfig") != {
        "Type": "json-file",
        "Config": {"max-file": "2", "max-size": "1m"},
    }:
        raise RelayDockerError("license relay logging policy has drifted")
    _validate_relay_env(config.get("Env"), expected_env)


def _validate_container_identity(
    state: dict, config: dict, host: dict, mounts: object, image_id: str
) -> None:
    runtime_state = state.get("State")
    if not isinstance(runtime_state, dict) or runtime_state.get("Running") is not True:
        raise RelayDockerError("license relay is not running")
    if state.get("Image") != image_id or config.get("Image") != image_id:
        raise RelayDockerError("license relay image identity has drifted")
    if config.get("User") != "65532:65532":
        raise RelayDockerError("license relay user has drifted")
    if mounts != [] or host.get("Binds") not in (None, []):
        raise RelayDockerError("license relay mounts have drifted")
    if not _ports_are_private(config, host):
        raise RelayDockerError("license relay port publication has drifted")
    if host.get("ReadonlyRootfs") is not True:
        raise RelayDockerError("license relay root filesystem is writable")
    if host.get("CapAdd") not in (None, []) or host.get("CapDrop") != ["ALL"]:
        raise RelayDockerError("license relay capability policy has drifted")
    if not _host_authority_isolated(host):
        raise RelayDockerError("license relay host authority has drifted")


def _host_authority_isolated(host: dict) -> bool:
    return (
        host.get("Privileged") is False
        and host.get("PidMode") in (None, "")
        and host.get("IpcMode") in (None, "", "private")
        and host.get("UsernsMode") in (None, "", "private")
        and host.get("Devices") in (None, [])
        and host.get("DeviceRequests") in (None, [])
    )


def _ports_are_private(config: dict, host: dict) -> bool:
    bindings = host.get("PortBindings")
    if bindings not in (None, {}):
        return False
    if host.get("PublishAllPorts") is not False:
        return False
    exposed = config.get("ExposedPorts")
    return exposed in (None, {})


def _security_options_match(raw: object) -> bool:
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], str):
        return False
    return raw[0] in {"no-new-privileges", "no-new-privileges:true"}


def _limits_match(host: dict) -> bool:
    return (
        host.get("PidsLimit") == 64
        and host.get("Memory") == 64 * 1024 * 1024
        and host.get("NanoCpus") == 500_000_000
        and host.get("Ulimits") == [{"Name": "nofile", "Hard": 256, "Soft": 256}]
    )


def _validate_relay_env(raw: object, expected: set[str]) -> None:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise RelayDockerError("Docker returned invalid relay environment state")
    actual = {item for item in raw if item.partition("=")[0] in _RELAY_ENV_NAMES}
    if actual != expected:
        raise RelayDockerError("license relay environment differs from host authority")


def _inspect_object(run: Runner, kind: str, name: str) -> dict:
    result = run([kind, "inspect", name], DOCKER_TIMEOUT)
    if result.returncode != 0:
        detail = result.stderr.strip() or "Docker inspect failed"
        raise RelayDockerError(f"cannot inspect {kind} {name}: {detail}")
    try:
        decoded = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RelayDockerError(f"Docker returned invalid state for {kind} {name}") from exc
    if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
        raise RelayDockerError(f"Docker returned invalid state for {kind} {name}")
    return decoded[0]


def _validate_network(
    run: Runner,
    name: str,
    internal: bool,
    role_label: str,
    session_id: str,
    issuance_labels: tuple[str, ...],
) -> None:
    labels = _inspect_labels(run, "network", name)
    expected = {role_label, f"{SESSION_LABEL}={session_id}", *issuance_labels}
    if labels != expected:
        raise RelayDockerError(f"license network {name} labels differ from host issuance")
    observed = _inspect_value(run, "network", name, "{{.Internal}}").lower()
    if observed != str(internal).lower():
        raise RelayDockerError(f"license network {name} routing policy has drifted")
    options_output = _inspect_value(run, "network", name, "{{json .Options}}")
    try:
        options = json.loads(options_output)
    except json.JSONDecodeError as exc:
        raise RelayDockerError(f"Docker returned invalid options for network {name}") from exc
    expected_options = {GATEWAY_MODE_OPTION: GATEWAY_MODE_ISOLATED} if internal else {}
    if options != expected_options:
        raise RelayDockerError(f"license network {name} gateway policy has drifted")


def _validate_network_endpoints(
    run: Runner,
    resources: RelayResources,
    profile: RelayProfile,
    session_container: str | None,
    issuance_labels: tuple[str, ...],
) -> None:
    """Reject extra endpoints and require relay aliases on the private network."""
    private = _network_endpoint_names(run, resources.private_network)
    outbound = _network_endpoint_names(run, resources.outbound_network)
    if outbound != {resources.relay_container}:
        raise RelayDockerError("license outbound network has unauthorized endpoints")
    if resources.relay_container not in private:
        raise RelayDockerError("license relay is absent from its private network")
    clients = private - {resources.relay_container}
    if session_container is not None:
        if clients != {session_container}:
            raise RelayDockerError("license private network has unauthorized endpoints")
    else:
        for client in clients:
            labels = _inspect_labels(run, "container", client)
            expected = {"booley.role=interactive", *issuance_labels}
            if not expected.issubset(labels):
                raise RelayDockerError("license private network has unauthorized endpoints")
    networks = _inspect_object(run, "container", resources.relay_container).get(
        "NetworkSettings", {}
    )
    attached = networks.get("Networks", {}) if isinstance(networks, dict) else {}
    private_state = attached.get(resources.private_network) if isinstance(attached, dict) else None
    aliases = private_state.get("Aliases") if isinstance(private_state, dict) else None
    if not isinstance(aliases, list) or not {
        RELAY_ALIAS,
        profile.server_hostid,
    }.issubset(set(aliases)):
        raise RelayDockerError("license relay private aliases have drifted")


def _network_endpoint_names(run: Runner, network: str) -> set[str]:
    state = _inspect_object(run, "network", network)
    containers = state.get("Containers")
    if not isinstance(containers, dict):
        raise RelayDockerError(f"Docker returned invalid endpoints for network {network}")
    names: set[str] = set()
    for endpoint in containers.values():
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("Name"), str):
            raise RelayDockerError(f"Docker returned invalid endpoints for network {network}")
        names.add(endpoint["Name"])
    return names


def _inspect_labels(run: Runner, kind: str, name: str) -> set[str]:
    output = _inspect_value(
        run,
        kind,
        name,
        "{{json .Labels}}" if kind == "network" else "{{json .Config.Labels}}",
    )
    try:
        labels = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RelayDockerError(f"Docker returned invalid labels for {kind} {name}") from exc
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
    ):
        raise RelayDockerError(f"Docker returned invalid labels for {kind} {name}")
    return {f"{key}={value}" for key, value in labels.items()}


def _inspect_networks(run: Runner, container: str) -> set[str]:
    output = _inspect_value(
        run,
        "container",
        container,
        "{{json .NetworkSettings.Networks}}",
    )
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RelayDockerError(f"Docker returned invalid network state for {container}") from exc
    if not isinstance(decoded, dict):
        raise RelayDockerError(f"Docker returned invalid network state for {container}")
    return set(decoded)


def _inspect_value(run: Runner, kind: str, name: str, template: str) -> str:
    result = run([kind, "inspect", name, "--format", template], DOCKER_TIMEOUT)
    if result.returncode != 0:
        detail = result.stderr.strip() or "Docker inspect failed"
        raise RelayDockerError(f"cannot inspect {kind} {name}: {detail}")
    return result.stdout.strip()


def _rollback(run: Runner, created: list[tuple[str, str]]) -> tuple[str, ...]:
    residual: list[str] = []
    for kind, name in reversed(created):
        args = ["container", "rm", "-f", name] if kind == "container" else ["network", "rm", name]
        try:
            result = run(args, DOCKER_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            residual.append(f"{kind}:{name}")
            continue
        if result.returncode != 0 and not _is_missing(result.stderr):
            residual.append(f"{kind}:{name}")
    return tuple(residual)


def _is_missing(detail: str) -> bool:
    lowered = detail.lower()
    return "no such" in lowered or "not found" in lowered


def _valid_port(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _append_labels(argv: list[str], labels: tuple[str, ...], final: str) -> list[str]:
    for label in labels:
        argv += ["--label", label]
    argv.append(final)
    return argv
