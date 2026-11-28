"""Desired-state reconciliation for global Sandbox Docker resources."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from booley.config.host_config import SandboxHostPolicy
from booley.core import docker_cli
from booley.core.boundary import (
    BoundaryError,
    as_str,
    require_bool,
    require_dict,
    require_list,
    require_str,
    require_str_value,
)
from booley.core.differences import format_differences
from booley.core.egress_modes import EGRESS_MODES
from booley.runtime import host_docker as legacy
from booley.runtime.devcontainer import PROXY_PORT
from booley.runtime.docker_capacity import (
    BuildEstimateClass,
    DockerBuildPlan,
    DockerBuildRequest,
    DockerCacheEvidence,
    ensure_docker_build_capacity,
)
from booley.runtime.image_lifecycle import Intent
from booley.runtime.platform_paths import host_path_from_docker_mount

IMAGE_SCHEMA = "1"
POLICY_SCHEMA = 1
LABEL_SIDECAR_SCHEMA = "io.booley.sidecar.schema"
LABEL_SIDECAR_KIND = "io.booley.sidecar.kind"
LABEL_SOURCE_FINGERPRINT = "io.booley.sidecar.source-fingerprint"
LABEL_BOOLEY_VERSION = "io.booley.sidecar.booley-version"
LABEL_POLICY_FINGERPRINT = "io.booley.sidecar.policy-fingerprint"
SESSION_ROLE = "interactive"
_DEVCONTAINER_FOLDER_LABEL = "devcontainer.local_folder"
_WORKSPACE_MOUNT_TARGET = "/work"
# Host ``[sandbox] egress`` modes; under unproxied no Sandbox uses the egress proxy.
_PROXY, _UNPROXIED = EGRESS_MODES
_UNPROXIED_REASON = f"host egress = {_UNPROXIED}"
# Container states plain ``docker ps`` lists, so ``_active_sessions`` names them.
_ACTIVE_STATES = frozenset({"running", "restarting", "paused"})
# Proxy-mode resources that converge to absent under unproxied egress, in row order.
_RETIRED_EGRESS = (("network", legacy.EGRESS_NETWORK), ("proxy", legacy.PROXY_CONTAINER))


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
    remedy: str | None = None


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
    project_root: str | None = None


@dataclass(frozen=True, slots=True)
class _ActiveSession:
    name: str
    project_root: str
    vscode: bool = False


@dataclass(frozen=True, slots=True)
class _ContainerSpec:
    resource: str
    name: str
    image: str
    role: str
    run_args: tuple[str, ...]
    fingerprint: str
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


def policy_fingerprint(policy: SandboxHostPolicy, docker_socket: str = legacy.DOCKER_SOCK) -> str:
    """Return the canonical identity stamped on a singleton container.

    ``docker_socket`` is the host socket the reaper mounts. Only a source other than the
    default ``/var/run/docker.sock`` changes the identity, so an installed reaper on a
    default-socket host stays current.
    """
    document: dict[str, object] = {
        "egress_allowlist": list(policy.egress_allowlist),
        "idle_timeout_seconds": policy.idle_timeout_seconds,
        "max_sessions": policy.max_sessions,
        "schema": POLICY_SCHEMA,
    }
    if docker_socket != legacy.DOCKER_SOCK:
        document["docker_socket"] = docker_socket
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def reconcile_sidecars(
    policy: SandboxHostPolicy,
    intent: Intent,
    *,
    booley_root: Path | None = None,
    capacity_plan: DockerBuildPlan | None = None,
) -> SidecarResult:
    """Inspect or converge all global Docker objects in dependency order."""
    docker = _docker_adapter()
    specs = _required_image_specs(booley_root, policy.egress)
    if policy.egress == _UNPROXIED:
        return _reconcile_unproxied(policy, intent, specs, docker, capacity_plan)
    findings = list(_reconcile_images(specs, intent, docker, capacity_plan))
    if findings and findings[-1].state is SidecarState.ERROR:
        return SidecarResult(tuple(findings))
    network = _reconcile_network(intent, docker)
    findings.append(network)
    if network.state is SidecarState.ERROR:
        return SidecarResult(tuple(findings))
    findings.extend(_reconcile_containers(policy, intent, docker))
    return SidecarResult(tuple(findings))


def inspect_sidecars(
    policy: SandboxHostPolicy | None, *, booley_root: Path | None = None
) -> SidecarResult:
    """Inspect every independent sidecar resource for Bootstrap CLI only."""
    docker = _docker_adapter()
    if policy is not None and policy.egress == _UNPROXIED:
        return _inspect_unproxied(policy, booley_root, docker)
    findings = _inspect_images(booley_root, _PROXY, docker)
    findings.append(_inspect_bootstrap_network(docker))
    if policy is None:
        findings.extend(
            SidecarFinding(
                resource,
                SidecarState.ERROR,
                "cannot inspect desired container state without valid host policy",
                remedy="repair host config",
            )
            for resource in ("proxy", "reaper")
        )
    else:
        findings.append(_inspect_bootstrap_container(_proxy_spec(policy), docker))
        findings.append(_reaper_finding(policy, Intent.CHECK, docker, inspect=True))
    return SidecarResult(tuple(findings))


def _inspect_images(
    booley_root: Path | None, egress: str, docker: _DockerPort
) -> list[SidecarFinding]:
    """Inspect the required sidecar images, or report why their recipes are unusable."""
    try:
        specs = _required_image_specs(booley_root, egress)
    except (SidecarError, OSError) as exc:
        return [SidecarFinding("sidecar-images", SidecarState.ERROR, str(exc))]
    return [_reconcile_image(spec, Intent.CHECK, docker) for spec in specs]


def _reconcile_unproxied(
    policy: SandboxHostPolicy,
    intent: Intent,
    specs: tuple[_ImageSpec, ...],
    docker: _DockerPort,
    capacity_plan: DockerBuildPlan | None,
) -> SidecarResult:
    """Converge the reaper and retire the proxy-mode egress resources.

    The reaper does not depend on the proxy or its network, so a retirement
    error never stops reaper reconciliation; a reaper-image error still stops
    before any retirement mutation.
    """
    findings = [_proxy_image_not_required()]
    findings.extend(_reconcile_images(specs, intent, docker, capacity_plan))
    if findings[-1].state is SidecarState.ERROR:
        return SidecarResult(tuple(findings))
    findings.extend(_retire_egress(intent, docker, inspect=False))
    findings.append(_reaper_finding(policy, intent, docker, inspect=False))
    return SidecarResult(tuple(findings))


def _inspect_unproxied(
    policy: SandboxHostPolicy, booley_root: Path | None, docker: _DockerPort
) -> SidecarResult:
    """Inspect every unproxied-mode sidecar row for check-only Bootstrap."""
    findings = _inspect_images(booley_root, _UNPROXIED, docker)
    if findings[0].resource != "sidecar-images":
        findings.insert(0, _proxy_image_not_required())
    findings.extend(_retire_egress(Intent.CHECK, docker, inspect=True))
    findings.append(_reaper_finding(policy, Intent.CHECK, docker, inspect=True))
    return SidecarResult(tuple(findings))


def _proxy_image_not_required() -> SidecarFinding:
    return SidecarFinding(
        "proxy-image",
        SidecarState.CURRENT,
        f"{legacy.PROXY_IMAGE} is not required ({_UNPROXIED_REASON})",
    )


def _retire_egress(
    intent: Intent, docker: _DockerPort, *, inspect: bool
) -> tuple[SidecarFinding, ...]:
    """Converge the unused egress network and proxy to absent under unproxied egress.

    The readiness check (``CHECK`` without ``inspect``: init, auth reseed,
    Doctor) accepts a leftover without enumerating Sandboxes, so reissuing one
    Project never waits for the others. Check-only Bootstrap (``inspect``)
    reports what Bootstrap would do; mutating intents remove the leftover only
    when no Booley Sandbox runs, no refresh predecessor is parked, and no
    foreign container is attached.
    """
    try:
        proxy = _inspect_container(legacy.PROXY_CONTAINER, docker)
        _verify_container_ownership(legacy.PROXY_CONTAINER, "egress-proxy", proxy)
        network = _inspect_network(docker)
        present = {"network": network.exists, "proxy": proxy.exists}
        if not any(present.values()):
            return _egress_rows(present, SidecarState.CURRENT, "")
        if intent is Intent.CHECK and not inspect:
            return _egress_rows(
                present,
                SidecarState.CURRENT,
                f"is present but unused under {_UNPROXIED_REASON}; "
                "`booley bootstrap` removes it once no Sandbox runs",
            )
        _require_no_egress_blockers(network, docker)
        if intent is Intent.CHECK:
            return _egress_rows(
                present,
                SidecarState.PENDING,
                f"is present although {_UNPROXIED_REASON}; `booley bootstrap` removes it",
            )
    except SidecarError as exc:
        return tuple(
            SidecarFinding(row, SidecarState.ERROR, str(exc)) for row, _ in _RETIRED_EGRESS
        )
    return _remove_egress(present, docker)


def _egress_rows(
    present: dict[str, bool], state: SidecarState, detail: str
) -> tuple[SidecarFinding, ...]:
    """Report each retired resource: absent rows are current, present rows take ``state``."""
    return tuple(
        SidecarFinding(row, state, f"{name} {detail}")
        if present[row]
        else SidecarFinding(
            row, SidecarState.CURRENT, f"{name} is absent (unused: {_UNPROXIED_REASON})"
        )
        for row, name in _RETIRED_EGRESS
    )


def _require_no_egress_blockers(network: _NetworkState, docker: _DockerPort) -> None:
    """Refuse retirement while a Sandbox or foreign container could still need the network."""
    active = _active_sessions(docker)
    parked = _parked_refresh_predecessors(docker)
    if active or parked:
        summaries = (_active_session_summary(active), _parked_predecessor_summary(parked))
        described = ", ".join(summary for summary in summaries if summary)
        raise SidecarError(
            f"cannot remove unused {legacy.PROXY_CONTAINER} and {legacy.EGRESS_NETWORK} "
            f"({_UNPROXIED_REASON}) while Booley Sandboxes exist: {described}; stop or remove each "
            "listed Sandbox and retry `booley bootstrap`"
        )
    foreign = tuple(name for name in network.attached_names if name != legacy.PROXY_CONTAINER)
    if foreign:
        raise SidecarError(
            f"cannot remove unused {legacy.EGRESS_NETWORK} ({_UNPROXIED_REASON}); detach foreign "
            f"containers after checking ownership: {', '.join(foreign)}"
        )


def _remove_egress(present: dict[str, bool], docker: _DockerPort) -> tuple[SidecarFinding, ...]:
    """Remove the proxy first: Docker refuses to remove a network with a running endpoint."""
    try:
        if present["proxy"]:
            _remove_unused(["rm", "-f", legacy.PROXY_CONTAINER], legacy.PROXY_CONTAINER, docker)
    except SidecarError as exc:
        return tuple(
            SidecarFinding(row, SidecarState.ERROR, str(exc)) for row, _ in _RETIRED_EGRESS
        )
    removed = _egress_rows(
        present, SidecarState.CHANGED, f"was removed (unused: {_UNPROXIED_REASON})"
    )
    try:
        if present["network"]:
            _remove_unused(["network", "rm", legacy.EGRESS_NETWORK], legacy.EGRESS_NETWORK, docker)
    except SidecarError as exc:
        proxy_row = next(finding for finding in removed if finding.resource == "proxy")
        return SidecarFinding("network", SidecarState.ERROR, str(exc)), proxy_row
    return removed


def _remove_unused(args: list[str], name: str, docker: _DockerPort) -> None:
    result = docker.run(args)
    if result.returncode:
        raise SidecarError(f"failed to remove unused {name}: {_failure_detail(result)}")


def _proxy_spec(policy: SandboxHostPolicy) -> _ContainerSpec:
    fingerprint = policy_fingerprint(policy)
    return _ContainerSpec(
        "proxy",
        legacy.PROXY_CONTAINER,
        legacy.PROXY_IMAGE,
        "egress-proxy",
        tuple(_proxy_run_args(policy, fingerprint)),
        fingerprint,
        legacy.EGRESS_NETWORK,
    )


def _reaper_spec(policy: SandboxHostPolicy, docker: _DockerPort) -> _ContainerSpec:
    source = _reaper_socket_source(docker)
    fingerprint = policy_fingerprint(policy, docker_socket=source)
    return _ContainerSpec(
        "reaper",
        legacy.REAPER_CONTAINER,
        legacy.REAPER_IMAGE,
        "reaper",
        tuple(_reaper_run_args(policy, fingerprint, source)),
        fingerprint,
    )


def _runner(docker: _DockerPort) -> docker_cli.Runner:
    return lambda args, timeout: docker.run(args, timeout=timeout)


def _inspect(kind: str, name: str, docker: _DockerPort) -> dict[str, Any] | None:
    """Return one object's inspection document, or ``None`` when Docker has no such object."""
    try:
        return docker_cli.inspect(_runner(docker), kind, name, timeout=15)
    except docker_cli.InspectError as exc:
        result = exc.result
        if result is None:
            raise SidecarError(f"Docker returned invalid {kind} {name} JSON") from exc
        if _is_missing(result):
            return None
        raise SidecarError(f"cannot inspect {kind} {name}: {_failure_detail(result)}") from exc


def _docker_text(docker: _DockerPort, args: list[str], description: str) -> str:
    """Run one read-only Docker query and return its stripped output."""
    result = docker.run(args, timeout=15)
    if result.returncode:
        raise SidecarError(f"cannot read {description}: {_failure_detail(result)}")
    return result.stdout.strip()


def _docker_endpoint(docker: _DockerPort) -> str:
    """Return the endpoint of the active Docker context."""
    try:
        return docker_cli.inspect_value(
            _runner(docker),
            "context",
            None,
            timeout=15,
            template="{{json .Endpoints.docker.Host}}",
            require=require_str_value,
        )
    except docker_cli.InspectError as exc:
        detail = str(exc) if exc.result is None else _failure_detail(exc.result)
        raise SidecarError(f"cannot read the active Docker endpoint: {detail}") from exc


def _reaper_socket_source(docker: _DockerPort) -> str:
    """Return the host path the reaper mounts as the Docker socket of Booley's daemon."""
    endpoint = _docker_endpoint(docker)
    version = _docker_text(
        docker,
        ["version", "--format", "{{.Client.Os}}\t{{.Server.Platform.Name}}"],
        "the Docker engine",
    )
    client_os, _, engine = version.partition("\t")
    scheme, _, path = endpoint.partition("://")
    if engine.startswith("Docker Desktop"):
        return legacy.DOCKER_SOCK
    if scheme == "unix" and path and client_os == "linux":
        return path
    if scheme in {"unix", "npipe"} and client_os != "linux":
        # Every local engine on macOS and Windows runs in a VM that exposes the default socket.
        return legacy.DOCKER_SOCK
    raise SidecarError(
        f"cannot mount the Docker socket of {endpoint} into {legacy.REAPER_CONTAINER}: "
        "the reaper needs a local unix:// socket of the daemon Booley uses; select a local "
        "Docker endpoint with DOCKER_HOST, DOCKER_CONTEXT or `docker context use`, "
        "then retry `booley bootstrap`"
    )


def _verify_reaper_daemon(docker: _DockerPort) -> None:
    """Fail unless the reaper reaches the same Docker daemon as Booley's CLI."""
    expected = _docker_text(docker, ["info", "--format", "{{.ID}}"], "the Docker daemon ID")
    try:
        result = docker.run(
            [
                "exec",
                legacy.REAPER_CONTAINER,
                "docker",
                "--host",
                f"unix://{legacy.DOCKER_SOCK}",
                "info",
                "--format",
                "{{.ID}}",
            ],
            timeout=15,
        )
    except SidecarError as exc:
        result = subprocess.CompletedProcess([], 1, "", str(exc))
    seen = result.stdout.strip()
    if not result.returncode and seen == expected:
        return
    reached = (
        f"another Docker daemon ({seen})"
        if not result.returncode
        else f"no Docker daemon ({_failure_detail(result)})"
    )
    raise SidecarError(
        f"{legacy.REAPER_CONTAINER} reaches {reached}, not the daemon Booley uses "
        f"({expected}); make the active Docker endpoint a local unix:// socket of the "
        "daemon Booley uses, then retry `booley bootstrap`"
    )


def _reaper_finding(
    policy: SandboxHostPolicy, intent: Intent, docker: _DockerPort, *, inspect: bool
) -> SidecarFinding:
    """Inspect or converge the reaper, then prove it reaches Booley's Docker daemon."""
    try:
        spec = _reaper_spec(policy, docker)
        if inspect:
            return _inspect_bootstrap_container(spec, docker)
        finding = _reconcile_container(spec, intent, docker)
        if intent is not Intent.CHECK and finding.state in {
            SidecarState.CURRENT,
            SidecarState.CHANGED,
        }:
            _verify_reaper_daemon(docker)
        return finding
    except SidecarError as exc:
        return SidecarFinding("reaper", SidecarState.ERROR, str(exc))


def _inspect_bootstrap_container(spec: _ContainerSpec, docker: _DockerPort) -> SidecarFinding:
    try:
        state = _inspect_container(spec.name, docker)
        _verify_container_ownership(spec.name, spec.role, state)
        image = _inspect_image(spec.image, docker)
        current = image is not None and _container_matches(state, image[0], spec.fingerprint)
        if state.exists and not current:
            _require_idle_sessions(spec.name, docker)
        if image is None:
            return SidecarFinding(
                spec.resource,
                SidecarState.PENDING,
                f"{spec.name} needs missing image {spec.image}",
            )
        network_ready = spec.required_network is None or spec.required_network in state.networks
        if current and state.running and network_ready:
            return SidecarFinding(spec.resource, SidecarState.CURRENT, f"{spec.name} is current")
        return SidecarFinding(
            spec.resource, SidecarState.PENDING, _container_pending_detail(spec, state, image[0])
        )
    except SidecarError as exc:
        return SidecarFinding(spec.resource, SidecarState.ERROR, str(exc))


def _require_idle_sessions(resource: str, docker: _DockerPort) -> None:
    active = _active_sessions(docker)
    if active:
        raise SidecarError(
            f"cannot replace stale {resource} while active Booley Sessions exist: "
            f"{_active_session_summary(active)}; shut down each listed Project "
            "and retry `booley bootstrap`"
        )


def _inspect_bootstrap_network(docker: _DockerPort) -> SidecarFinding:
    try:
        state = _inspect_network(docker)
        if state.current:
            return SidecarFinding(
                "network", SidecarState.CURRENT, f"{legacy.EGRESS_NETWORK} is current"
            )
        if state.exists:
            _require_idle_sessions(legacy.EGRESS_NETWORK, docker)
            foreign = tuple(
                name for name in state.attached_names if name != legacy.PROXY_CONTAINER
            )
            if foreign:
                raise SidecarError(
                    f"cannot replace stale {legacy.EGRESS_NETWORK}; detach foreign containers "
                    f"after checking ownership: {', '.join(foreign)}"
                )
            proxy = _inspect_container(legacy.PROXY_CONTAINER, docker)
            _verify_container_ownership(legacy.PROXY_CONTAINER, "egress-proxy", proxy)
        detail = "has stale routing policy" if state.exists else "is missing"
        return SidecarFinding("network", SidecarState.PENDING, f"{legacy.EGRESS_NETWORK} {detail}")
    except SidecarError as exc:
        return SidecarFinding("network", SidecarState.ERROR, str(exc))


def _reconcile_containers(
    policy: SandboxHostPolicy, intent: Intent, docker: _DockerPort
) -> tuple[SidecarFinding, ...]:
    """Reconcile the proxy/reaper container suffix after images and network."""
    proxy = _reconcile_container(_proxy_spec(policy), intent, docker)
    if proxy.state is SidecarState.ERROR:
        return (proxy,)
    return proxy, _reaper_finding(policy, intent, docker, inspect=False)


def _reconcile_images(
    specs: tuple[_ImageSpec, ...],
    intent: Intent,
    docker: _DockerPort,
    capacity_plan: DockerBuildPlan | None,
) -> tuple[SidecarFinding, ...]:
    """Reconcile the ordered sidecar image prefix, stopping on its first error."""
    findings = []
    for spec in specs:
        finding = _reconcile_image(spec, intent, docker, capacity_plan=capacity_plan)
        findings.append(finding)
        if finding.state is SidecarState.ERROR:
            break
    return tuple(findings)


def plan_image_builds(
    intent: Intent,
    *,
    egress: str = _PROXY,
    booley_root: Path | None = None,
    docker: _DockerPort | None = None,
) -> tuple[DockerBuildRequest, ...]:
    """Inspect the sidecar images ``egress`` requires and return their builds without mutation."""
    resolved_docker = docker or _docker_adapter()
    requests = []
    for spec in _required_image_specs(booley_root, egress):
        current = _inspect_image(spec.reference, resolved_docker)
        _verify_image_ownership(spec, current)
        labels = _image_labels(spec)
        is_current = current is not None and all(
            current[1].get(key) == value for key, value in labels.items()
        )
        if not is_current or intent is Intent.REFRESH:
            requests.append(
                DockerBuildRequest(
                    spec.reference,
                    spec.reference,
                    BuildEstimateClass.THIN_OVERLAY,
                    DockerCacheEvidence(spec.reference, tuple(labels.items())),
                )
            )
    return tuple(requests)


def _required_image_specs(booley_root: Path | None, egress: str) -> tuple[_ImageSpec, ...]:
    """Return the sidecar images the host egress mode uses; unproxied needs no proxy."""
    specs = _image_specs(booley_root)
    if egress != _UNPROXIED:
        return specs
    return tuple(spec for spec in specs if spec.resource != "proxy-image")


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


def _reconcile_image(
    spec: _ImageSpec,
    intent: Intent,
    docker: _DockerPort,
    *,
    capacity_plan: DockerBuildPlan | None = None,
) -> SidecarFinding:
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
        if current is None:
            detail = f"{spec.reference} is missing"
        elif not is_current:
            actual = {key: current[1].get(key) for key in expected}
            detail = f"{spec.reference} is stale: {format_differences(expected, actual)}"
        else:
            detail = f"{spec.reference} refresh requested"
        if intent is Intent.CHECK:
            return SidecarFinding(spec.resource, SidecarState.PENDING, detail)
        tail = capacity_plan
        if tail is not None:
            tail = tail.tail_from_output(spec.reference)
        _build_image(spec, expected, docker, capacity_plan=tail)
        verified = _inspect_image(spec.reference, docker)
        if verified is None:
            raise SidecarError(f"{spec.reference} build completed but the image is missing")
        actual = {key: verified[1].get(key) for key in expected}
        if actual != expected:
            raise SidecarError(
                f"{spec.reference} build completed without expected provenance labels: "
                + format_differences(expected, actual)
            )
        return SidecarFinding(spec.resource, SidecarState.CHANGED, f"reconciled {spec.reference}")
    except (OSError, SidecarError, ValueError) as exc:
        return SidecarFinding(spec.resource, SidecarState.ERROR, str(exc))


def _verify_image_ownership(spec: _ImageSpec, current: tuple[str, dict[str, str]] | None) -> None:
    """Reject a fixed-name image unless all Booley provenance fields are present."""
    if current is None:
        return
    labels = current[1]
    expected = _image_labels(spec)
    actual = {key: labels[key] for key in expected if key in labels}
    if set(actual) != set(expected) or labels.get(LABEL_SIDECAR_KIND) != spec.kind:
        raise SidecarError(
            f"foreign image collision: {spec.reference} lacks expected Booley ownership "
            f"provenance ({format_differences(expected, actual)}); it was not modified"
        )


def _build_image(
    spec: _ImageSpec,
    labels: dict[str, str],
    docker: _DockerPort,
    *,
    capacity_plan: DockerBuildPlan | None = None,
) -> None:
    args = ["build", "-t", spec.reference, "-f", str(spec.dockerfile)]
    for key, value in labels.items():
        args += ["--label", f"{key}={value}"]
    args.append(str(spec.context))
    request = (
        capacity_plan.current
        if capacity_plan is not None
        else DockerBuildRequest(
            spec.reference,
            spec.reference,
            BuildEstimateClass.THIN_OVERLAY,
            DockerCacheEvidence(spec.reference, tuple(labels.items())),
        )
    )
    ensure_docker_build_capacity(
        ("docker", "build"),
        image=spec.reference,
        current_request=request,
        remaining_plan=capacity_plan or DockerBuildPlan((request,)),
    )
    result = docker.run(args, timeout=600)
    if result.returncode:
        raise SidecarError(f"failed to build {spec.reference}: {_failure_detail(result)}")


def _inspect_image(reference: str, docker: _DockerPort) -> tuple[str, dict[str, str]] | None:
    document = _inspect("image", reference, docker)
    if document is None:
        return None
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
    active = _active_sessions(docker)
    if active:
        raise SidecarError(
            f"cannot replace stale {legacy.EGRESS_NETWORK} while active Booley Sessions "
            f"exist: {_active_session_summary(active)}; shut down each listed "
            "Project and retry `booley bootstrap`"
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
    document = _inspect("network", legacy.EGRESS_NETWORK, docker)
    if document is None:
        return _NetworkState(False)
    try:
        internal = _required_bool(document, "Internal")
        options = require_dict(
            document.get("Options"), field=f"network {legacy.EGRESS_NETWORK}.Options"
        )
        containers = require_dict(
            document.get("Containers"), field=f"network {legacy.EGRESS_NETWORK}.Containers"
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
        entry = require_dict(raw, field=f"network container {container_id}")
        names.append(require_str(entry, "Name"))
    return tuple(sorted(names))


def _reconcile_container(
    spec: _ContainerSpec, intent: Intent, docker: _DockerPort
) -> SidecarFinding:
    try:
        state = _inspect_container(spec.name, docker)
        image_state = _inspect_image(spec.image, docker)
        if image_state is None:
            raise SidecarError(f"cannot reconcile {spec.name}: image {spec.image} is missing")
        _verify_container_ownership(spec.name, spec.role, state)
        current = _container_matches(state, image_state[0], spec.fingerprint)
        network_missing = (
            spec.required_network is not None and spec.required_network not in state.networks
        )
        if current and state.running and not network_missing:
            return SidecarFinding(spec.resource, SidecarState.CURRENT, f"{spec.name} is current")
        if intent is Intent.CHECK:
            return SidecarFinding(
                spec.resource,
                SidecarState.PENDING,
                _container_pending_detail(spec, state, image_state[0]),
            )
        _apply_container(spec.name, state, current, list(spec.run_args), docker)
        _ensure_container_network(spec.name, spec.required_network, docker)
        return SidecarFinding(spec.resource, SidecarState.CHANGED, f"reconciled {spec.name}")
    except SidecarError as exc:
        return SidecarFinding(spec.resource, SidecarState.ERROR, str(exc))


def _verify_container_ownership(name: str, role: str, state: _ContainerState) -> None:
    if not state.exists:
        return
    actual_role = (state.labels or {}).get(docker_cli.ROLE_LABEL)
    if actual_role == role:
        return
    shown = actual_role if actual_role is not None else "missing"
    raise SidecarError(
        f"foreign name collision: container {name} has {docker_cli.ROLE_LABEL}={shown!r}, "
        f"expected {role!r}; it was not modified"
    )


def _container_matches(state: _ContainerState, image_id: str, fingerprint: str) -> bool:
    return (
        state.exists
        and state.image_id == image_id
        and (state.labels or {}).get(LABEL_POLICY_FINGERPRINT) == fingerprint
    )


def _container_pending_detail(
    spec: _ContainerSpec, state: _ContainerState, image_identity: str
) -> str:
    expected = {
        "exists": True,
        "image_identity": image_identity,
        "source_fingerprint": spec.fingerprint,
        "running": True,
        "required_network_present": True,
    }
    actual = {
        "exists": state.exists,
        "image_identity": state.image_id if state.exists else None,
        "source_fingerprint": (state.labels or {}).get(LABEL_POLICY_FINGERPRINT),
        "running": state.running,
        "required_network_present": spec.required_network is None
        or spec.required_network in state.networks,
    }
    return f"{spec.name} is missing, stopped, or stale: " + format_differences(expected, actual)


def _apply_container(
    name: str, state: _ContainerState, current: bool, run_args: list[str], docker: _DockerPort
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
    active = _active_sessions(docker)
    if active:
        raise SidecarError(
            f"cannot replace stale {name} while active Booley Sessions exist: "
            f"{_active_session_summary(active)}; shut down each listed Project "
            "and retry `booley bootstrap`"
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
    document = _inspect("container", name, docker)
    if document is None:
        return _ContainerState(False)
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
        labels = _string_labels(config.get("Labels"), f"container {name}")
        project_root = _project_root_from_inspection(document, labels, name)
    except BoundaryError as exc:
        raise SidecarError(f"Docker returned incomplete inspection for container {name}") from exc
    return _ContainerState(True, image_id, running, labels, frozenset(attached), project_root)


def _project_root_from_inspection(
    document: dict[str, Any], labels: dict[str, str], name: str
) -> str | None:
    """Return the host Project mounted by a Sandbox container."""
    if folder := labels.get(_DEVCONTAINER_FOLDER_LABEL):
        return folder
    mounts = require_list(document.get("Mounts"), field=f"container {name}.Mounts")
    for index, raw in enumerate(mounts):
        mount = require_dict(raw, field=f"container {name}.Mounts[{index}]")
        kind = require_str(mount, "Type")
        destination = require_str(mount, "Destination")
        if kind == "bind" and destination == _WORKSPACE_MOUNT_TARGET:
            source = require_str(mount, "Source")
            host_path = host_path_from_docker_mount(source)
            return str(host_path) if host_path is not None else None
    return None


def _active_session_summary(sessions: tuple[_ActiveSession, ...]) -> str:
    """Describe active Sessions with a copyable Project-scoped shutdown command."""
    descriptions = []
    for session in sessions:
        if session.vscode:
            # `session down` always refuses an editor-owned Sandbox; name its owner's remedy.
            remedy = "close its VS Code window or stop it with Dev Containers"
        else:
            command = _shell_command(
                ["booley", "session", "down", "--project", session.project_root]
            )
            remedy = f"run `{command}`"
        descriptions.append(f"{session.name} (Project {session.project_root}; {remedy})")
    return ", ".join(descriptions)


def _parked_predecessor_summary(parked: tuple[_ActiveSession, ...]) -> str:
    """Describe parked rebuild predecessors, leading with the non-destructive rename."""
    descriptions = []
    for sandbox in parked:
        original = sandbox.name.removesuffix(legacy.REFRESH_BACKUP_SUFFIX)
        rename = _shell_command(["docker", "rename", sandbox.name, original])
        descriptions.append(
            f"{sandbox.name} (Project {sandbox.project_root}; parked by an interrupted "
            "`booley session up --rebuild` or an older refresh: rename it back with "
            f"`{rename}` when that name is free, or remove it after inspection)"
        )
    return ", ".join(descriptions)


def _shell_command(argv: list[str]) -> str:
    """Quote ``argv`` as one copyable command for the host shell."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _active_sessions(docker: _DockerPort) -> tuple[_ActiveSession, ...]:
    result = docker.run(
        [
            "ps",
            "--filter",
            f"label={docker_cli.ROLE_LABEL}={SESSION_ROLE}",
            "--format",
            "{{.ID}}\t{{.Names}}",
        ],
        timeout=30,
    )
    if result.returncode:
        raise SidecarError(
            f"cannot enumerate active Booley Sessions safely: {_failure_detail(result)}"
        )
    sessions: list[_ActiveSession] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not all(field.strip() for field in fields):
            raise SidecarError("Docker returned an incomplete active Session listing")
        container_id, name = (field.strip() for field in fields)
        state = _inspect_container(container_id, docker)
        if (
            not state.exists
            or not state.running
            or (state.labels or {}).get(docker_cli.ROLE_LABEL) != SESSION_ROLE
        ):
            raise SidecarError(f"cannot prove active Session ownership for {name}")
        if state.project_root is None:
            raise SidecarError(f"cannot identify owning Project for active Session {name}")
        vscode = _DEVCONTAINER_FOLDER_LABEL in (state.labels or {})
        sessions.append(_ActiveSession(name, state.project_root, vscode))
    return tuple(sorted(sessions, key=lambda session: session.name))


def _parked_refresh_predecessors(docker: _DockerPort) -> tuple[_ActiveSession, ...]:
    """Return stopped Sandboxes parked by `session up --rebuild`; restore needs their network."""
    result = docker.run(
        [
            "ps",
            "-a",
            "--filter",
            f"label={docker_cli.ROLE_LABEL}={SESSION_ROLE}",
            "--format",
            "{{.ID}}\t{{.Names}}\t{{.State}}",
        ],
        timeout=30,
    )
    if result.returncode:
        raise SidecarError(
            f"cannot enumerate parked Booley Sandboxes safely: {_failure_detail(result)}"
        )
    parked: list[_ActiveSession] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not all(field.strip() for field in fields):
            raise SidecarError("Docker returned an incomplete Sandbox listing")
        container_id, name, status = (field.strip() for field in fields)
        # Plain ``docker ps`` already lists these states as active Sandboxes.
        if not name.endswith(legacy.REFRESH_BACKUP_SUFFIX) or status in _ACTIVE_STATES:
            continue
        state = _inspect_container(container_id, docker)
        if not state.exists or (state.labels or {}).get(docker_cli.ROLE_LABEL) != SESSION_ROLE:
            raise SidecarError(f"cannot prove parked Sandbox ownership for {name}")
        if state.project_root is None:
            raise SidecarError(f"cannot identify owning Project for parked Sandbox {name}")
        parked.append(_ActiveSession(name, state.project_root))
    return tuple(sorted(parked, key=lambda session: session.name))


def _proxy_run_args(policy: SandboxHostPolicy, fingerprint: str) -> list[str]:
    args = _base_run_args(legacy.PROXY_CONTAINER, "egress-proxy", fingerprint)
    args += ["-e", f"PROXY_PORT={PROXY_PORT}"]
    if policy.egress_allowlist:
        args += ["-e", f"PROXY_ALLOWLIST={json.dumps(list(policy.egress_allowlist))}"]
    args.append(legacy.PROXY_IMAGE)
    return args


def _reaper_run_args(policy: SandboxHostPolicy, fingerprint: str, source: str) -> list[str]:
    args = _base_run_args(legacy.REAPER_CONTAINER, "reaper", fingerprint)
    args += [
        "-v",
        f"{source}:{legacy.DOCKER_SOCK}",
        "-e",
        f"BOOLEY_IDLE_TIMEOUT_SECONDS={policy.idle_timeout_seconds}",
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
        f"{docker_cli.ROLE_LABEL}={role}",
        "--label",
        f"{LABEL_POLICY_FINGERPRINT}={fingerprint}",
    ]


def _run_container(name: str, args: list[str], docker: _DockerPort) -> None:
    result = docker.run(args)
    if result.returncode:
        raise SidecarError(f"failed to start {name}: {_failure_detail(result)}")


def _string_labels(value: object, description: str) -> dict[str, str]:
    if value is None:
        return {}
    try:
        return docker_cli.labels(value, allow_empty=True)
    except BoundaryError as exc:
        raise SidecarError(f"Docker returned invalid labels for {description}") from exc


def _required_bool(value: dict[Any, Any], key: str) -> bool:
    """Apply the shared boolean guard while requiring the external field."""
    if key not in value:
        raise BoundaryError(f"{key} is required")
    return require_bool(value, key)


def _is_missing(result: subprocess.CompletedProcess[str]) -> bool:
    detail = docker_cli.failure_detail(result).lower()
    return "no such" in detail or "not found" in detail


def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    fallback = f"Docker exited {result.returncode} without diagnostic output"
    return docker_cli.failure_detail(result, fallback)


def _docker_adapter() -> _DockerPort:
    return _DockerCli()
