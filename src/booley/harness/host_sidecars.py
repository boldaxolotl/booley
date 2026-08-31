"""Desired-state reconciliation for global Interactive Mode Docker resources."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from booley.config.host_config import InteractiveHostPolicy
from booley.core.boundary import (
    BoundaryError,
    as_str,
    require_bool,
    require_dict,
    require_str,
)
from booley.harness import interactive_docker as legacy
from booley.harness.image_lifecycle import Intent

IMAGE_SCHEMA = "1"
POLICY_SCHEMA = 1
LABEL_SIDECAR_SCHEMA = "io.booley.sidecar.schema"
LABEL_SIDECAR_KIND = "io.booley.sidecar.kind"
LABEL_SOURCE_FINGERPRINT = "io.booley.sidecar.source-fingerprint"
LABEL_BOOLEY_VERSION = "io.booley.sidecar.booley-version"
LABEL_POLICY_FINGERPRINT = "io.booley.sidecar.policy-fingerprint"
ROLE_LABEL = "booley.role"
SESSION_ROLE = "interactive"


class SidecarState(StrEnum):
    """One global Docker resource's reconciliation state."""

    CURRENT = "current"
    PENDING = "pending"
    CHANGED = "changed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SidecarFinding:
    """Typed readiness fact for one sidecar resource."""

    resource: str
    state: SidecarState
    detail: str


@dataclass(frozen=True, slots=True)
class SidecarResult:
    """Ordered findings for sidecar images, network, and containers."""

    findings: tuple[SidecarFinding, ...]

    @property
    def ready(self) -> bool:
        return all(
            finding.state in {SidecarState.CURRENT, SidecarState.CHANGED}
            for finding in self.findings
        )


@dataclass(frozen=True, slots=True)
class _ImageSpec:
    resource: str
    reference: str
    kind: str
    dockerfile: Path
    context: Path
    sources: tuple[Path, ...]

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path in (self.dockerfile, *self.sources):
            try:
                body = path.read_bytes()
            except OSError as exc:
                raise SidecarError(f"cannot fingerprint sidecar input {path}: {exc}") from exc
            digest.update(path.name.encode())
            digest.update(b"\0")
            digest.update(body)
            digest.update(b"\0")
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class _ContainerState:
    exists: bool
    image_id: str = ""
    running: bool = False
    labels: dict[str, str] | None = None
    networks: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _ContainerSpec:
    resource: str
    name: str
    image: str
    role: str
    run_args: tuple[str, ...]
    required_network: str | None = None


@dataclass(frozen=True, slots=True)
class _NetworkState:
    exists: bool
    current: bool = False
    attached_names: tuple[str, ...] = ()


class SidecarError(RuntimeError):
    """Docker state could not be proven safe or reconciled."""


class _DockerPort(Protocol):
    def run(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]: ...


class _DockerCli:
    def run(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        try:
            return legacy._run_docker(args, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SidecarError(f"Docker command failed: {exc}") from exc


def policy_fingerprint(policy: InteractiveHostPolicy) -> str:
    """Return the canonical identity stamped on both singleton containers."""
    document = {
        "egress_allowlist": list(policy.egress_allowlist),
        "idle_timeout_seconds": policy.idle_timeout_seconds,
        "max_sessions": policy.max_sessions,
        "schema": POLICY_SCHEMA,
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def reconcile_sidecars(
    policy: InteractiveHostPolicy,
    intent: Intent,
    *,
    booley_root: Path | None = None,
) -> SidecarResult:
    """Inspect or converge all global Docker objects in dependency order."""
    docker = _docker_adapter()
    try:
        specs = _image_specs(booley_root)
    except SidecarError as exc:
        return SidecarResult((SidecarFinding("sidecar-images", SidecarState.ERROR, str(exc)),))
    findings: list[SidecarFinding] = []
    for spec in specs:
        finding = _reconcile_image(spec, intent, docker)
        findings.append(finding)
        if finding.state is SidecarState.ERROR:
            return SidecarResult(tuple(findings))
    network = _reconcile_network(intent, docker)
    findings.append(network)
    if network.state is SidecarState.ERROR:
        return SidecarResult(tuple(findings))
    fingerprint = policy_fingerprint(policy)
    proxy_spec = _ContainerSpec(
        "proxy",
        legacy.PROXY_CONTAINER,
        legacy.PROXY_IMAGE,
        "egress-proxy",
        tuple(_proxy_run_args(policy, fingerprint)),
        legacy.EGRESS_NETWORK,
    )
    proxy = _reconcile_container(
        proxy_spec,
        fingerprint,
        intent,
        docker,
    )
    findings.append(proxy)
    if proxy.state is SidecarState.ERROR:
        return SidecarResult(tuple(findings))
    findings.append(
        _reconcile_container(
            _ContainerSpec(
                "reaper",
                legacy.REAPER_CONTAINER,
                legacy.REAPER_IMAGE,
                "reaper",
                tuple(_reaper_run_args(policy, fingerprint)),
            ),
            fingerprint,
            intent,
            docker,
        )
    )
    return SidecarResult(tuple(findings))


def _image_specs(booley_root: Path | None) -> tuple[_ImageSpec, _ImageSpec]:
    root = booley_root or _installed_root()
    package = root / "src" / "booley" if (root / "src" / "booley").is_dir() else root
    data = package / "data" / "docker"
    context = package / "docker"
    return (
        _ImageSpec(
            "proxy-image",
            legacy.PROXY_IMAGE,
            "egress-proxy",
            data / "Dockerfile.egress-proxy",
            context,
            (context / "egress_proxy.py", context / "proxy_entry.py"),
        ),
        _ImageSpec(
            "reaper-image",
            legacy.REAPER_IMAGE,
            "reaper",
            data / "Dockerfile.reaper",
            context,
            (context / "reaper.py",),
        ),
    )


def _installed_root() -> Path:
    from booley.runtime.paths import docker_data_dir

    data = docker_data_dir()
    source_root = data.parents[3]
    return source_root if (source_root / "src" / "booley").is_dir() else data.parent.parent


def _image_labels(spec: _ImageSpec) -> dict[str, str]:
    from booley import __version__

    return {
        LABEL_SIDECAR_SCHEMA: IMAGE_SCHEMA,
        LABEL_SIDECAR_KIND: spec.kind,
        LABEL_SOURCE_FINGERPRINT: spec.fingerprint,
        LABEL_BOOLEY_VERSION: __version__,
    }


def _reconcile_image(spec: _ImageSpec, intent: Intent, docker: _DockerPort) -> SidecarFinding:
    try:
        current = _inspect_image(spec.reference, docker)
        _verify_image_ownership(spec, current)
        expected = _image_labels(spec)
        is_current = current is not None and all(
            current[1].get(key) == value for key, value in expected.items()
        )
        needs_build = not is_current or intent is Intent.REFRESH
        if not needs_build:
            return SidecarFinding(
                spec.resource, SidecarState.CURRENT, f"{spec.reference} is current"
            )
        detail = f"{spec.reference} is missing or stale"
        if intent is Intent.CHECK:
            return SidecarFinding(spec.resource, SidecarState.PENDING, detail)
        _build_image(spec, expected, docker)
        verified = _inspect_image(spec.reference, docker)
        if verified is None or any(
            verified[1].get(key) != value for key, value in expected.items()
        ):
            raise SidecarError(
                f"{spec.reference} build completed without expected provenance labels"
            )
        return SidecarFinding(spec.resource, SidecarState.CHANGED, f"reconciled {spec.reference}")
    except SidecarError as exc:
        return SidecarFinding(spec.resource, SidecarState.ERROR, str(exc))


def _verify_image_ownership(
    spec: _ImageSpec,
    current: tuple[str, dict[str, str]] | None,
) -> None:
    """Reject a fixed-name image unless all Booley provenance fields are present."""
    if current is None:
        return
    labels = current[1]
    required = {
        LABEL_SIDECAR_SCHEMA,
        LABEL_SIDECAR_KIND,
        LABEL_SOURCE_FINGERPRINT,
        LABEL_BOOLEY_VERSION,
    }
    missing = sorted(required - labels.keys())
    if missing or labels.get(LABEL_SIDECAR_KIND) != spec.kind:
        detail = f"missing {', '.join(missing)}" if missing else "sidecar kind does not match"
        raise SidecarError(
            f"foreign image collision: {spec.reference} lacks expected Booley ownership "
            f"provenance ({detail}); it was not modified"
        )


def _build_image(spec: _ImageSpec, labels: dict[str, str], docker: _DockerPort) -> None:
    args = ["build", "-t", spec.reference, "-f", str(spec.dockerfile)]
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    args.append(str(spec.context))
    result = docker.run(args, timeout=600)
    if result.returncode:
        raise SidecarError(f"failed to build {spec.reference}: {_failure_detail(result)}")


def _inspect_image(reference: str, docker: _DockerPort) -> tuple[str, dict[str, str]] | None:
    result = docker.run(["image", "inspect", reference, "--format", "{{json .}}"], timeout=15)
    if result.returncode:
        if _is_missing(result):
            return None
        raise SidecarError(f"cannot inspect image {reference}: {_failure_detail(result)}")
    document = _json_object(result.stdout, f"image {reference}")
    try:
        image_id = require_str(document, "Id")
        config = require_dict(document.get("Config"), field=f"image {reference}.Config")
    except BoundaryError as exc:
        raise SidecarError(f"Docker returned incomplete inspection for image {reference}") from exc

    return image_id, _string_labels(config.get("Labels"), f"image {reference}")


def _reconcile_network(intent: Intent, docker: _DockerPort) -> SidecarFinding:
    try:
        state = _inspect_network(docker)
        if state.current:
            return SidecarFinding(
                "network", SidecarState.CURRENT, f"{legacy.EGRESS_NETWORK} is current"
            )
        detail = "missing" if not state.exists else "has stale routing policy"
        if intent is Intent.CHECK:
            return SidecarFinding(
                "network", SidecarState.PENDING, f"{legacy.EGRESS_NETWORK} {detail}"
            )
        if state.exists:
            _replace_stale_network(state, docker)
        else:
            _create_network(docker)
        return SidecarFinding(
            "network", SidecarState.CHANGED, f"reconciled {legacy.EGRESS_NETWORK}"
        )
    except SidecarError as exc:
        return SidecarFinding("network", SidecarState.ERROR, str(exc))


def _create_network(docker: _DockerPort) -> None:
    result = docker.run(
        [
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--opt",
            f"{legacy.GATEWAY_MODE_OPTION}={legacy.GATEWAY_MODE_ISOLATED}",
            legacy.EGRESS_NETWORK,
        ]
    )
    if result.returncode:
        raise SidecarError(f"failed to create {legacy.EGRESS_NETWORK}: {_failure_detail(result)}")


def _replace_stale_network(state: _NetworkState, docker: _DockerPort) -> None:
    active = _active_session_names(docker)
    if active:
        raise SidecarError(
            f"cannot replace stale {legacy.EGRESS_NETWORK} while active Booley Sessions "
            f"exist: {', '.join(active)}; shut them down and retry `booley bootstrap`"
        )
    foreign = tuple(name for name in state.attached_names if name != legacy.PROXY_CONTAINER)
    if foreign:
        raise SidecarError(
            f"cannot replace stale {legacy.EGRESS_NETWORK}; attached foreign containers were "
            f"not modified: {', '.join(foreign)}"
        )
    proxy = _inspect_container(legacy.PROXY_CONTAINER, docker)
    _verify_container_ownership(legacy.PROXY_CONTAINER, "egress-proxy", proxy)
    if proxy.exists:
        removed_proxy = docker.run(["rm", "-f", legacy.PROXY_CONTAINER])
        if removed_proxy.returncode:
            raise SidecarError(
                f"failed to remove stale {legacy.PROXY_CONTAINER}: {_failure_detail(removed_proxy)}"
            )
    removed_network = docker.run(["network", "rm", legacy.EGRESS_NETWORK])
    if removed_network.returncode:
        raise SidecarError(
            f"failed to replace {legacy.EGRESS_NETWORK}: {_failure_detail(removed_network)}"
        )
    _create_network(docker)


def _inspect_network(docker: _DockerPort) -> _NetworkState:
    result = docker.run(
        ["network", "inspect", legacy.EGRESS_NETWORK, "--format", "{{json .}}"], timeout=15
    )
    if result.returncode:
        if _is_missing(result):
            return _NetworkState(False)
        raise SidecarError(
            f"cannot inspect network {legacy.EGRESS_NETWORK}: {_failure_detail(result)}"
        )
    document = _json_object(result.stdout, f"network {legacy.EGRESS_NETWORK}")
    try:
        internal = _required_bool(document, "Internal")
        options = require_dict(
            document.get("Options"), field=f"network {legacy.EGRESS_NETWORK}.Options"
        )
        containers = require_dict(
            document.get("Containers"),
            field=f"network {legacy.EGRESS_NETWORK}.Containers",
        )
        attached_names = _network_container_names(containers)
    except BoundaryError as exc:
        raise SidecarError(
            f"Docker returned incomplete inspection for network {legacy.EGRESS_NETWORK}"
        ) from exc
    current = (
        internal
        and as_str(options.get(legacy.GATEWAY_MODE_OPTION)) == legacy.GATEWAY_MODE_ISOLATED
    )
    return _NetworkState(True, current, attached_names)


def _network_container_names(containers: dict[Any, Any]) -> tuple[str, ...]:
    names = []
    for container_id, raw in containers.items():
        if as_str(container_id) is None:
            raise BoundaryError("network container IDs must be strings")
        entry = require_dict(raw, field=f"network container {container_id}")
        names.append(require_str(entry, "Name"))
    return tuple(sorted(names))


def _reconcile_container(
    spec: _ContainerSpec,
    fingerprint: str,
    intent: Intent,
    docker: _DockerPort,
) -> SidecarFinding:
    try:
        state = _inspect_container(spec.name, docker)
        image_state = _inspect_image(spec.image, docker)
        if image_state is None:
            raise SidecarError(f"cannot reconcile {spec.name}: image {spec.image} is missing")
        _verify_container_ownership(spec.name, spec.role, state)
        current = _container_matches(state, image_state[0], fingerprint)
        network_missing = (
            spec.required_network is not None and spec.required_network not in state.networks
        )
        if current and state.running and not network_missing:
            return SidecarFinding(spec.resource, SidecarState.CURRENT, f"{spec.name} is current")
        if intent is Intent.CHECK:
            return SidecarFinding(
                spec.resource,
                SidecarState.PENDING,
                f"{spec.name} is missing, stopped, or stale",
            )
        _apply_container(spec.name, state, current, list(spec.run_args), docker)
        _ensure_container_network(spec.name, spec.required_network, docker)
        return SidecarFinding(spec.resource, SidecarState.CHANGED, f"reconciled {spec.name}")
    except SidecarError as exc:
        return SidecarFinding(spec.resource, SidecarState.ERROR, str(exc))


def _verify_container_ownership(name: str, role: str, state: _ContainerState) -> None:
    if not state.exists:
        return
    actual_role = (state.labels or {}).get(ROLE_LABEL)
    if actual_role == role:
        return
    shown = actual_role if actual_role is not None else "missing"
    raise SidecarError(
        f"foreign name collision: container {name} has {ROLE_LABEL}={shown!r}, "
        f"expected {role!r}; it was not modified"
    )


def _container_matches(state: _ContainerState, image_id: str, fingerprint: str) -> bool:
    return (
        state.exists
        and state.image_id == image_id
        and (state.labels or {}).get(LABEL_POLICY_FINGERPRINT) == fingerprint
    )


def _apply_container(
    name: str,
    state: _ContainerState,
    current: bool,
    run_args: list[str],
    docker: _DockerPort,
) -> None:
    if not state.exists:
        _run_container(name, run_args, docker)
        return
    if not current:
        _replace_stale_container(name, run_args, docker)
        return
    if not state.running:
        started = docker.run(["start", name])
        if started.returncode:
            raise SidecarError(f"failed to start {name}: {_failure_detail(started)}")


def _replace_stale_container(name: str, run_args: list[str], docker: _DockerPort) -> None:
    active = _active_session_names(docker)
    if active:
        joined = ", ".join(active)
        raise SidecarError(
            f"cannot replace stale {name} while active Booley Sessions exist: {joined}; "
            "shut them down with `booley session down` and retry `booley bootstrap`"
        )
    removed = docker.run(["rm", "-f", name])
    if removed.returncode:
        raise SidecarError(f"failed to replace {name}: {_failure_detail(removed)}")
    _run_container(name, run_args, docker)


def _ensure_container_network(
    name: str, required_network: str | None, docker: _DockerPort
) -> None:
    if required_network is None or required_network in _inspect_container(name, docker).networks:
        return
    connected = docker.run(["network", "connect", required_network, name])
    if connected.returncode and "already exists" not in connected.stderr.lower():
        raise SidecarError(
            f"failed to connect {name} to {required_network}: {_failure_detail(connected)}"
        )


def _inspect_container(name: str, docker: _DockerPort) -> _ContainerState:
    result = docker.run(["container", "inspect", name, "--format", "{{json .}}"], timeout=15)
    if result.returncode:
        if _is_missing(result):
            return _ContainerState(False)
        raise SidecarError(f"cannot inspect container {name}: {_failure_detail(result)}")
    document = _json_object(result.stdout, f"container {name}")
    try:
        config = require_dict(document.get("Config"), field=f"container {name}.Config")
        state = require_dict(document.get("State"), field=f"container {name}.State")
        networks = require_dict(
            document.get("NetworkSettings"), field=f"container {name}.NetworkSettings"
        )
        image_id = require_str(document, "Image")
        running = _required_bool(state, "Running")
        attached = require_dict(
            networks.get("Networks"), field=f"container {name}.NetworkSettings.Networks"
        )
        attached_names = _string_keys(attached, f"container {name} networks")
    except BoundaryError as exc:
        raise SidecarError(f"Docker returned incomplete inspection for container {name}") from exc
    return _ContainerState(
        True,
        image_id,
        running,
        _string_labels(config.get("Labels"), f"container {name}"),
        frozenset(attached_names),
    )


def _active_session_names(docker: _DockerPort) -> tuple[str, ...]:
    result = docker.run(
        [
            "ps",
            "--filter",
            f"label={ROLE_LABEL}={SESSION_ROLE}",
            "--format",
            "{{.ID}}\t{{.Names}}",
        ],
        timeout=30,
    )
    if result.returncode:
        raise SidecarError(
            f"cannot enumerate active Booley Sessions safely: {_failure_detail(result)}"
        )
    names: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not all(field.strip() for field in fields):
            raise SidecarError("Docker returned an incomplete active Session listing")
        container_id, name = (field.strip() for field in fields)
        state = _inspect_container(container_id, docker)
        if (
            not state.exists
            or not state.running
            or (state.labels or {}).get(ROLE_LABEL) != SESSION_ROLE
        ):
            raise SidecarError(f"cannot prove active Session ownership for {name}")
        names.append(name)
    return tuple(sorted(names))


def _proxy_run_args(policy: InteractiveHostPolicy, fingerprint: str) -> list[str]:
    args = _base_run_args(legacy.PROXY_CONTAINER, "egress-proxy", fingerprint)
    args += ["-e", f"PROXY_PORT={legacy.PROXY_PORT}"]
    if policy.egress_allowlist:
        args += ["-e", f"PROXY_ALLOWLIST={json.dumps(list(policy.egress_allowlist))}"]
    args.append(legacy.PROXY_IMAGE)
    return args


def _reaper_run_args(policy: InteractiveHostPolicy, fingerprint: str) -> list[str]:
    args = _base_run_args(legacy.REAPER_CONTAINER, "reaper", fingerprint)
    args += [
        "-v",
        f"{legacy.DOCKER_SOCK}:{legacy.DOCKER_SOCK}",
        "-e",
        f"BOOLEY_IDLE_TIMEOUT_SECONDS={policy.idle_timeout_seconds}",
        "-e",
        f"BOOLEY_MAX_SESSIONS={policy.max_sessions}",
        legacy.REAPER_IMAGE,
    ]
    return args


def _base_run_args(name: str, role: str, fingerprint: str) -> list[str]:
    return [
        "run",
        "-d",
        "--name",
        name,
        "--restart",
        "unless-stopped",
        "--label",
        f"{ROLE_LABEL}={role}",
        "--label",
        f"{LABEL_POLICY_FINGERPRINT}={fingerprint}",
    ]


def _run_container(name: str, args: list[str], docker: _DockerPort) -> None:
    result = docker.run(args)
    if result.returncode:
        raise SidecarError(f"failed to start {name}: {_failure_detail(result)}")


def _json_object(raw: str, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return require_dict(value, field=description)
    except (json.JSONDecodeError, BoundaryError) as exc:
        raise SidecarError(f"Docker returned invalid {description} JSON") from exc


def _string_labels(value: object, description: str) -> dict[str, str]:
    if value is None:
        return {}
    try:
        labels = require_dict(value, field=f"labels for {description}")
        result = {}
        for key, item in labels.items():
            string_key = as_str(key)
            string_item = as_str(item)
            if string_key is None or string_item is None:
                raise BoundaryError("label names and values must be strings")
            result[string_key] = string_item
        return result
    except BoundaryError as exc:
        raise SidecarError(f"Docker returned invalid labels for {description}") from exc


def _required_bool(value: dict[Any, Any], key: str) -> bool:
    """Apply the shared boolean guard while requiring the external field."""
    if key not in value:
        raise BoundaryError(f"{key} is required")
    return require_bool(value, key)


def _string_keys(value: dict[Any, Any], description: str) -> tuple[str, ...]:
    keys = tuple(as_str(key) for key in value)
    if any(key is None for key in keys):
        raise BoundaryError(f"{description} must use string keys")
    return tuple(key for key in keys if key is not None)


def _is_missing(result: subprocess.CompletedProcess[str]) -> bool:
    detail = (result.stderr or result.stdout).lower()
    return "no such" in detail or "not found" in detail


def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout).strip()
    return detail or f"Docker exited {result.returncode} without diagnostic output"


def _docker_adapter() -> _DockerPort:
    return _DockerCli()
