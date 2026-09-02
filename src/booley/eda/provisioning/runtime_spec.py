"""Host issuance and full validation of immutable Session Runtime specs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
import tomllib
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict
from booley.harness.devcontainer import EGRESS_NETWORK
from booley.runtime.auth_token import config_dir
from booley.runtime.platform_paths import docker_mount_path, host_path_from_docker_mount
from booley.runtime.timefmt import LOCAL_TIMEZONE_ENV

from ..config import PROVISIONING_HOST, EdaConfig, load_eda_config
from . import authority
from .policies.vivado import CONTAINER_TARGET, POLICY_REVISION, wrapper_sha256

STAMP_VERSION = 4
DEVCONTAINER_TARGET = "/work/.devcontainer"
_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "name",
        "image",
        "workspaceMount",
        "workspaceFolder",
        "remoteUser",
        "runArgs",
        "mounts",
        "remoteEnv",
        "containerEnv",
        "shutdownAction",
        "updateRemoteUserUID",
        "postCreateCommand",
        "postStartCommand",
        "postAttachCommand",
        "initializeCommand",
        "customizations",
    }
)
_ESCAPE_KEYS = frozenset({"dockerComposeFile", "service", "runServices", "workspaceFolder"})
_FIXED_REGISTRAR = "python -m booley.runtime.incontainer_register"
_FIXED_ATTACH = "python -m booley.runtime.incontainer_live_preview && python -m booley.runtime.incontainer_vaporview"
_FIXED_SEED_FRAGMENTS = frozenset(
    {
        "cp -n /home/agent/.claude-config-seed.json /home/agent/.claude.json 2>/dev/null || true",
        "(cp /home/agent/.claude-creds-seed.json "
        "/home/agent/.claude/.credentials.json && chmod 600 "
        "/home/agent/.claude/.credentials.json) 2>/dev/null || true",
        "(cp /home/agent/.codex-auth-seed.json /home/agent/.codex/auth.json && "
        "chmod 600 /home/agent/.codex/auth.json) 2>/dev/null || true",
    }
)


class RuntimeSpecError(RuntimeError):
    """A Session Runtime spec is missing, drifted, or outside issued policy."""


@dataclass(frozen=True)
class Issuance:
    """Host-observed inputs covered by one exact spec digest."""

    version: int
    project_root: str
    spec_sha256: str
    image: str
    image_id: str
    keeper_image: str
    policy_revision: int
    installation: str | None
    license_profile: str | None
    wrapper_sha256: str | None
    relay_image_id: str | None
    validator_sha256: str
    file_sha256: str | None = None
    project_data_source: str | None = None


def stamp_path(project_root: Path) -> Path:
    """Private host path keyed by exact canonical Project identity."""
    return stamp_path_for_identity(str(project_root.resolve()))


def stamp_path_for_identity(project_root: str) -> Path:
    """Private host path for an already-canonical persisted Project identity."""
    identity = hashlib.sha256(project_root.encode()).hexdigest()
    return config_dir() / "eda" / "session-specs" / f"{identity}.json"


def load_issued_snapshot(project_root: Path) -> Issuance:
    """Load structurally valid prior issuance without consulting current authority.

    Session refresh uses this recovery view before replacing an existing
    container.  The current grant may legitimately have changed — healing that
    drift is the point of refresh — while the sealed prior licence decision and
    Project identity still have to be trusted before Docker mutation.
    """
    project = project_root.resolve(strict=True)
    issuance = _load_stamp(stamp_path(project))
    if issuance.project_root != str(project):
        raise RuntimeSpecError("host-issued spec stamp belongs to a different Project")
    if issuance.keeper_image != keeper_image(project):
        raise RuntimeSpecError("host-issued spec image keeper differs from this Project")
    return issuance


def load_recovery_snapshot(project_root: Path, spec: dict[str, Any], spec_path: Path) -> Issuance:
    """Authenticate the exact prior issuance without consulting current grants."""
    project = project_root.resolve(strict=True)
    issuance = _load_stamp(stamp_path(project))
    if (
        issuance.project_root != str(project)
        or issuance.file_sha256 != _file_sha256(spec_path)
        or issuance.spec_sha256 != _spec_digest(spec)
    ):
        raise RuntimeSpecError("prior Session Runtime spec differs from its issuance stamp")
    expected_keeper = keeper_image(project)
    if issuance.keeper_image != expected_keeper:
        raise RuntimeSpecError("prior Session Runtime keeper belongs to a different Project")
    try:
        retained_id = _resolve_image_id(expected_keeper)
    except RuntimeSpecError as exc:
        raise RuntimeSpecError("prior Session Runtime image keeper is missing") from exc
    if retained_id != issuance.image_id:
        raise RuntimeSpecError("prior Session Runtime image keeper points at different bytes")
    return issuance


def keeper_image(project_root: Path) -> str:
    """Private Docker tag retaining the image bytes for one Project issuance."""
    identity = hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()
    return f"booley-issued-{identity}:session"


def initialize_command(executable: str = "booley") -> list[str]:
    """Fixed host-side validator/topology preparer used before VS Code create."""
    return [
        executable,
        "session",
        "prepare",
        "--project-root",
        "${localWorkspaceFolder}",
    ]


def pin_image(spec: dict[str, Any], *, expected_image_id: str | None = None) -> str:
    """Replace a generated image reference with its immutable local image ID."""
    if expected_image_id is None:
        image_id = _resolve_image_id(_require_string(spec, "image"))
    else:
        image_id = _resolve_image_id(expected_image_id)
        if image_id != expected_image_id:
            raise RuntimeSpecError(
                "reconciled Session Image ID no longer resolves to the same artifact"
            )
    spec["image"] = image_id
    return image_id


def _vivado_requested(project_root: Path, config: EdaConfig | None) -> bool:
    """Whether the active FPGA Flow requests a configured Vivado toolchain.

    An explicit ``[flows.fpga].enabled = false`` disables the consumer of this
    authority. Resolution is fail-closed: malformed or unreadable Flow
    configuration defaults to enabled.
    """
    if config is None:
        return False
    from booley.flows.execution import flow_enabled

    return flow_enabled("fpga", project_root)


def _host_vivado_requested(project_root: Path, config: EdaConfig | None) -> bool:
    """Whether the active FPGA Flow requests a host Vivado installation."""
    return bool(
        config is not None
        and config.provisioning == PROVISIONING_HOST
        and _vivado_requested(project_root, config)
    )


def _issuance_authority(
    project_root: Path,
    config: EdaConfig | None,
    spec: dict[str, Any],
    host_provisioning: bool,
    prior: Issuance | None = None,
) -> AbstractContextManager[tuple[authority.Installation | None, authority.LicenseProfile | None]]:
    """Resolve EDA authority only when the runtime has an active consumer.

    A Project without an active Vivado Flow requests neither a host install nor
    a licence.  Its runtime remains fully host-issued, but validating that
    issuance must not create or open the separate writable EDA authority store.
    """
    try:
        container_env = require_dict(spec.get("containerEnv", {}), field="containerEnv")
    except BoundaryError as exc:
        raise RuntimeSpecError(f"devcontainer.json {exc}") from exc
    license_marker = "XILINXD_LICENSE_FILE" in container_env
    eda_requested = host_provisioning or _vivado_requested(project_root, config)
    prior_eda = prior is not None and (
        prior.installation is not None or prior.license_profile is not None
    )
    if not eda_requested and not license_marker and not prior_eda:
        return nullcontext((None, None))
    return authority.resolve_for_issuance(
        project_root,
        host_provisioning,
    )


def seal(project_root: Path, spec: dict[str, Any]) -> str:
    """Add host-derived networks and issuance labels to a generated spec.

    Docker 29 accepts repeated ``--network`` flags for user-defined networks,
    which lets VS Code create a licensed runtime atomically on both the normal
    restricted egress network and its pre-created private license network.
    """
    project = project_root.resolve(strict=True)
    project_data_path = authorized_project_data_source(project)
    _pin_initialize_command(project, spec)
    _pin_project_data_mount(
        spec,
        docker_mount_path(project_data_path),
        project,
        project_data_path,
    )
    _pin_devcontainer_mount(spec, project)
    config = load_eda_config(project).get("vivado")
    host_provisioning = _host_vivado_requested(project, config)
    try:
        with _issuance_authority(project, config, spec, host_provisioning) as (
            installation,
            profile,
        ):
            run_args = spec.get("runArgs")
            if not isinstance(run_args, list) or any(
                not isinstance(item, str) for item in run_args
            ):
                raise RuntimeSpecError("devcontainer.json runArgs must be a string list")
            if any(_is_issuance_label(item) for item in run_args):
                raise RuntimeSpecError("generated Session Runtime spec is already sealed")
            if profile is not None:
                from .licensing.flexnet_docker import resources_for_session

                run_args += [
                    "--network",
                    resources_for_session(str(project)).private_network,
                ]
            digest = _spec_digest(spec)
            provisional = _issuance(
                project,
                spec,
                digest,
                installation,
                profile,
                file_sha256=None,
                project_data_source=str(project_data_path),
            )
            for label in labels(provisional):
                run_args += ["--label", label]
            _validate_generated_spec(project, spec, installation, profile, provisional)
            return digest
    except authority.AuthorityError as exc:
        raise _runtime_authority_error(spec, host_provisioning, exc) from exc


def requested_host_installation(
    project_root: Path,
) -> tuple[EdaConfig | None, authority.Installation | None]:
    """Resolve the grant-selected installation for a host-provisioned Project."""
    config = load_eda_config(project_root).get("vivado")
    if not _host_vivado_requested(project_root, config):
        return config, None
    return config, authority.resolve_installation(project_root)


def requested_license(
    project_root: Path,
    *,
    expected_name: str | None = "",
) -> authority.LicenseProfile | None:
    """Return the host-selected License Profile, when the runtime requests one.

    ``expected_name=None`` is the validated no-licence runtime path: it avoids
    opening the EDA authority store. Omitting the argument retains discovery
    for ``booley init``, before a runtime issuance exists.
    """
    if expected_name is None:
        return None
    project = project_root.resolve(strict=True)
    if expected_name:
        profile = _optional_license(project)
        if profile is None or profile.name != expected_name:
            raise RuntimeSpecError("Project licence grant differs from the issued runtime")
        return profile
    config = load_eda_config(project).get("vivado")
    if not _vivado_requested(project, config):
        return None
    return _optional_license(project)


def issue(project_root: Path, spec: dict[str, Any], spec_path: Path) -> Issuance:
    """Validate trusted inputs and atomically issue the exact generated spec."""
    project = project_root.resolve(strict=True)
    project_data_source = str(authorized_project_data_source(project))
    config = load_eda_config(project).get("vivado")
    host_provisioning = _host_vivado_requested(project, config)
    try:
        with _issuance_authority(project, config, spec, host_provisioning) as (
            installation,
            profile,
        ):
            digest = _spec_digest(spec)
            issuance = _issuance(
                project,
                spec,
                digest,
                installation,
                profile,
                file_sha256=_file_sha256(spec_path),
                project_data_source=project_data_source,
            )
            _validate_generated_spec(project, spec, installation, profile, issuance)
            _validate_bind_sources(spec["mounts"])
            image = _require_string(spec, "image")
            image_id = _resolve_image_id(image)
            if image != image_id:
                raise RuntimeSpecError(
                    "Session Runtime spec image is mutable; regenerate it through `booley init`"
                )
            if installation is not None:
                _validate_image_contract(image_id)
            _retain_issued_image(issuance)
            _write_stamp(stamp_path(project), issuance)
            return issuance
    except authority.AuthorityError as exc:
        raise _runtime_authority_error(spec, host_provisioning, exc) from exc


def validate(project_root: Path, spec: dict[str, Any], spec_path: Path) -> Issuance:
    """Validate every authority-bearing field against the current host issuance."""
    project = project_root.resolve(strict=True)
    stamp = authenticate(project, spec, spec_path)
    config = load_eda_config(project).get("vivado")
    host_provisioning = _host_vivado_requested(project, config)
    try:
        with _issuance_authority(project, config, spec, host_provisioning, stamp) as (
            installation,
            profile,
        ):
            _validate_generated_spec(project, spec, installation, profile, stamp)
            _validate_bind_sources(spec["mounts"])
            if stamp.image != spec.get("image") or stamp.image_id != _resolve_image_id(
                stamp.image
            ):
                raise RuntimeSpecError(
                    "Session Runtime image tag/digest has drifted since issuance"
                )
            expected_keeper = keeper_image(project)
            if stamp.keeper_image != expected_keeper:
                raise RuntimeSpecError("Session Runtime image keeper differs from this Project")
            try:
                retained_id = _resolve_image_id(stamp.keeper_image)
            except RuntimeSpecError as exc:
                raise RuntimeSpecError("issued Session Runtime image keeper is missing") from exc
            if retained_id != stamp.image_id:
                raise RuntimeSpecError(
                    "issued Session Runtime image keeper points at different bytes"
                )
            expected_installation = installation.name if installation else None
            expected_profile = profile.name if profile else None
            if (
                stamp.installation != expected_installation
                or stamp.license_profile != expected_profile
            ):
                raise RuntimeSpecError(
                    "Project grant differs from the issued Session Runtime spec"
                )
            if stamp.policy_revision != POLICY_REVISION:
                raise RuntimeSpecError("Session Runtime EDA policy revision has drifted")
            expected_relay_image = _relay_image_id(profile) if profile else None
            if stamp.relay_image_id != expected_relay_image:
                raise RuntimeSpecError("FlexNet relay image has drifted since spec issuance")
            if installation and stamp.wrapper_sha256 != wrapper_sha256():
                raise RuntimeSpecError("Booley Vivado wrapper has changed since spec issuance")
            return stamp
    except authority.AuthorityError as exc:
        raise _runtime_authority_error(spec, host_provisioning, exc) from exc


def authenticate(project_root: Path, spec: dict[str, Any], spec_path: Path) -> Issuance:
    """Authenticate immutable host issuance fields without trusting bind sources."""
    project = project_root.resolve(strict=True)
    stamp = _load_stamp(stamp_path(project))
    if (
        stamp.project_root != str(project)
        or stamp.file_sha256 != _file_sha256(spec_path)
        or stamp.spec_sha256 != _spec_digest(spec)
    ):
        raise RuntimeSpecError("devcontainer.json differs from its host-issued specification")
    _validate_initialize_command(project, spec.get("initializeCommand"))
    validator = _initialize_executable(spec)
    if stamp.validator_sha256 != _file_sha256(validator):
        raise RuntimeSpecError("host Booley validator has changed since spec issuance")
    return stamp


def labels(issuance: Issuance) -> tuple[str, ...]:
    """Return exact resource labels for lifecycle, revoke, Doctor, and reaping."""
    values = {
        "booley.project-id": hashlib.sha256(issuance.project_root.encode()).hexdigest(),
        "booley.spec-digest": issuance.spec_sha256,
        "booley.eda-policy": str(issuance.policy_revision),
        "booley.eda-installation": issuance.installation or "none",
        "booley.license-profile": issuance.license_profile or "none",
    }
    return tuple(f"{key}={value}" for key, value in values.items())


_ISSUANCE_LABEL_PREFIXES = tuple(
    f"{key}="
    for key in (
        "booley.project-id",
        "booley.spec-digest",
        "booley.eda-policy",
        "booley.eda-installation",
        "booley.license-profile",
    )
)


def _is_issuance_label(value: str) -> bool:
    return value.startswith(_ISSUANCE_LABEL_PREFIXES)


def _spec_digest(spec: dict[str, Any]) -> str:
    """Digest the sealed spec excluding only its self-describing label pairs."""
    normalized = json.loads(json.dumps(spec))
    raw = normalized.get("runArgs")
    if not isinstance(raw, list):
        raise RuntimeSpecError("devcontainer.json runArgs must be a list")
    cleaned: list[object] = []
    index = 0
    while index < len(raw):
        if (
            raw[index] == "--label"
            and index + 1 < len(raw)
            and isinstance(raw[index + 1], str)
            and _is_issuance_label(raw[index + 1])
        ):
            index += 2
            continue
        cleaned.append(raw[index])
        index += 1
    normalized["runArgs"] = cleaned
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _issuance(
    project: Path,
    spec: dict[str, Any],
    digest: str,
    installation: authority.Installation | None,
    profile: authority.LicenseProfile | None,
    *,
    file_sha256: str | None,
    project_data_source: str | None = None,
) -> Issuance:
    image = _require_string(spec, "image")
    return Issuance(
        version=STAMP_VERSION,
        project_root=str(project),
        spec_sha256=digest,
        image=image,
        image_id=_resolve_image_id(image),
        keeper_image=keeper_image(project),
        policy_revision=POLICY_REVISION,
        installation=installation.name if installation else None,
        license_profile=profile.name if profile else None,
        wrapper_sha256=wrapper_sha256() if installation else None,
        relay_image_id=_relay_image_id(profile) if profile and file_sha256 is not None else None,
        validator_sha256=_file_sha256(_initialize_executable(spec)),
        file_sha256=file_sha256,
        project_data_source=project_data_source,
    )


def expected_vivado_mount(installation: authority.Installation) -> str:
    """Fixed read-only bind for the built-in Vivado policy."""
    return f"source={installation.source},target={CONTAINER_TARGET},type=bind,readonly"


def expected_devcontainer_mount(project_root: Path) -> str:
    """Final nested read-only bind protecting future runtime creation."""
    source = docker_mount_path(project_root.resolve() / ".devcontainer")
    return f"source={source},target={DEVCONTAINER_TARGET},type=bind,readonly"


def _validate_generated_spec(
    project: Path,
    spec: dict[str, Any],
    installation: authority.Installation | None,
    profile: authority.LicenseProfile | None,
    issuance: Issuance,
) -> None:
    unknown = set(spec) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown or any(key in spec for key in _ESCAPE_KEYS - {"workspaceFolder"}):
        raise RuntimeSpecError("devcontainer.json contains an unsupported escape surface")
    if spec.get("workspaceFolder") != "/work" or spec.get("remoteUser") != "agent":
        raise RuntimeSpecError("devcontainer.json workspace/user policy has drifted")
    app = (
        spec.get("remoteEnv", {}).get("BOOLEY_AGENT_APP")
        if isinstance(spec.get("remoteEnv"), dict)
        else None
    )
    if app not in {"claude", "codex", "none"} or spec.get("name") != (
        f"Booley Interactive ({app})"
    ):
        raise RuntimeSpecError("devcontainer.json Session Runtime identity has drifted")
    expected_workspace = "source=${localWorkspaceFolder},target=/work,type=bind"
    if spec.get("workspaceMount") != expected_workspace:
        raise RuntimeSpecError("devcontainer.json Project workspace mount has drifted")
    _validate_initialize_command(project, spec.get("initializeCommand"))
    if spec.get("shutdownAction") != "none":
        raise RuntimeSpecError("devcontainer.json shutdown policy has drifted")
    if spec.get("updateRemoteUserUID") is not False:
        raise RuntimeSpecError("devcontainer.json immutable user policy has drifted")
    _validate_lifecycle(spec)
    private_network = None
    if profile is not None:
        from .licensing.flexnet_docker import resources_for_session

        private_network = resources_for_session(str(project)).private_network
    mounts = spec.get("mounts")
    if not isinstance(mounts, list) or any(not isinstance(item, str) for item in mounts):
        raise RuntimeSpecError("devcontainer.json mounts must be a list of strings")
    project_data_workspace_target = _require_project_data_mount(
        mounts, issuance.project_data_source, project
    )
    _validate_state_volume(mounts, app, project)
    _require_exact_readonly_mount(mounts, expected_devcontainer_mount(project), last=True)
    vivado_mounts = [item for item in mounts if _mount_target(item) == CONTAINER_TARGET]
    if installation:
        _require_exact_readonly_mount(mounts, expected_vivado_mount(installation))
    elif vivado_mounts:
        raise RuntimeSpecError("devcontainer.json exposes Vivado without host authorization")
    _validate_overlaps(mounts)
    _validate_mount_surfaces(mounts, project_data_workspace_target)
    _validate_environment(spec, profile)
    _validate_run_args(
        spec.get("runArgs"),
        expected_labels=labels(issuance),
        private_network=private_network,
    )


def _validate_run_args(
    raw: object,
    *,
    expected_labels: tuple[str, ...],
    private_network: str | None,
) -> None:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise RuntimeSpecError("devcontainer.json runArgs must be a string list")
    fixed = [
        "--init",
        "--network",
        EGRESS_NETWORK,
        "--label",
        "booley.role=interactive",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "4096",
    ]
    forbidden = {
        "--privileged",
        "--pid=host",
        "--network=host",
        "--ipc=host",
        "--userns=host",
    }
    if any(item in forbidden for item in raw):
        raise RuntimeSpecError("devcontainer.json requests forbidden host authority")
    cursor = len(fixed)
    if raw[:cursor] != fixed:
        raise RuntimeSpecError(
            "devcontainer.json run arguments differ from fixed runtime hardening"
        )
    if raw[cursor : cursor + 1] == ["--memory"]:
        if (
            cursor + 1 >= len(raw)
            or re.fullmatch(r"[1-9][0-9]*(?:[kKmMgG])?", raw[cursor + 1]) is None
        ):
            raise RuntimeSpecError("devcontainer.json memory limit is invalid")
        cursor += 2
    if private_network is not None:
        if raw[cursor : cursor + 2] != ["--network", private_network]:
            raise RuntimeSpecError("licensed Session Runtime private network has drifted")
        cursor += 2
    expected_tail = [value for label in expected_labels for value in ("--label", label)]
    if raw[cursor:] != expected_tail:
        raise RuntimeSpecError("devcontainer.json issuance labels differ from host authority")


def _validate_environment(spec: dict[str, Any], profile: authority.LicenseProfile | None) -> None:
    container = spec.get("containerEnv", {})
    remote = spec.get("remoteEnv", {})
    if not isinstance(container, dict) or not isinstance(remote, dict):
        raise RuntimeSpecError("devcontainer.json environment sections must be objects")
    if "BOOLEY_HOST_MCP_URL" in remote or "BOOLEY_HOST_MCP_URL" in container:
        raise RuntimeSpecError("Host MCP environment is forbidden")
    allowed_container = {"XILINXD_LICENSE_FILE"} if profile is not None else set()
    if set(container) != allowed_container:
        raise RuntimeSpecError("devcontainer.json contains unsupported container environment")
    required_remote = {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "NO_PROXY",
        "BOOLEY_MCP_MODE",
        "BOOLEY_PROJECT_DIR",
        "BOOLEY_AGENT_APP",
    }
    if not required_remote.issubset(remote):
        raise RuntimeSpecError("devcontainer.json is missing fixed Session Runtime environment")
    if any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in remote.items()
    ):
        raise RuntimeSpecError("devcontainer.json environment must contain strings only")
    allowed_remote = required_remote | {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        LOCAL_TIMEZONE_ENV,
    }
    if set(remote) - allowed_remote:
        raise RuntimeSpecError(
            "devcontainer.json contains unsupported Session Runtime environment"
        )
    fixed_values = {
        "HTTP_PROXY": "http://booley-proxy:8080",
        "HTTPS_PROXY": "http://booley-proxy:8080",
        "http_proxy": "http://booley-proxy:8080",
        "https_proxy": "http://booley-proxy:8080",
        "NO_PROXY": "localhost,127.0.0.1",
        "BOOLEY_MCP_MODE": "interactive",
        "BOOLEY_PROJECT_DIR": "/booley-project",
    }
    if any(remote.get(key) != value for key, value in fixed_values.items()):
        raise RuntimeSpecError("devcontainer.json fixed Session Runtime environment has drifted")
    app = remote.get("BOOLEY_AGENT_APP")
    credential_key = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "codex": "OPENAI_API_KEY"}.get(app)
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"):
        if key in remote and (key != credential_key or remote[key] != f"${{localEnv:{key}}}"):
            raise RuntimeSpecError("devcontainer.json credential environment has drifted")
    expected = None if profile is None else f"{profile.lmgrd_port}@booley-license-xilinx"
    actual = container.get("XILINXD_LICENSE_FILE")
    if actual != expected:
        raise RuntimeSpecError("XILINXD_LICENSE_FILE differs from the host License Profile")


def _validate_lifecycle(spec: dict[str, Any]) -> None:
    if spec.get("postAttachCommand") != _FIXED_ATTACH:
        raise RuntimeSpecError(
            "devcontainer.json postAttachCommand differs from fixed Booley policy"
        )
    for key in ("postCreateCommand", "postStartCommand"):
        raw = spec.get(key)
        if not isinstance(raw, str):
            raise RuntimeSpecError(f"devcontainer.json {key} differs from fixed Booley policy")
        fragments = raw.split("; ")
        if not fragments or fragments[-1] != _FIXED_REGISTRAR:
            raise RuntimeSpecError(f"devcontainer.json {key} differs from fixed Booley policy")
        if len(fragments) != len(set(fragments)) or any(
            fragment not in _FIXED_SEED_FRAGMENTS for fragment in fragments[:-1]
        ):
            raise RuntimeSpecError(f"devcontainer.json {key} differs from fixed Booley policy")


def _validate_overlaps(mounts: list[str]) -> None:
    protected = {DEVCONTAINER_TARGET, CONTAINER_TARGET}
    for raw in mounts:
        target = _mount_target(raw)
        if not target:
            continue
        for root in protected:
            if target.startswith(root.rstrip("/") + "/"):
                raise RuntimeSpecError(f"mount overlaps protected target: {target}")


def _validate_mount_surfaces(mounts: list[str], project_data_workspace_target: str | None) -> None:
    """Reject broad authority mounts and writable host binds outside fixed state."""
    writable_targets = {"/booley-project"}
    if project_data_workspace_target is not None:
        writable_targets.add(project_data_workspace_target)
    forbidden = {"/", "/var/run/docker.sock", "/run/docker.sock", "/root", "/home"}
    for raw in mounts:
        fields = _mount_fields(raw)
        target = fields.get("target", "")
        kind = fields.get("type")
        if target in forbidden or target.startswith("/root/"):
            raise RuntimeSpecError(f"devcontainer.json exposes forbidden host authority: {target}")
        if kind == "bind" and target not in writable_targets and not raw.endswith(",readonly"):
            raise RuntimeSpecError(f"host bind must be read-only: {target}")
        if "readonly=false" in raw:
            raise RuntimeSpecError(f"host bind explicitly disables read-only policy: {target}")


def _validate_bind_sources(mounts: list[str]) -> None:
    """Reject an issued spec that Docker cannot instantiate on this host."""
    for raw in mounts:
        fields = _mount_fields(raw)
        if fields.get("type") != "bind":
            continue
        source = fields.get("source", "")
        target = fields.get("target", "")
        if not source:
            raise RuntimeSpecError(f"generated bind source for {target} is missing: {source!r}")
        if not target:
            raise RuntimeSpecError(f"generated bind target for {source} is missing: {target!r}")
        host_path = host_path_from_docker_mount(source)
        if host_path is None:
            raise RuntimeSpecError(
                f"generated bind source for {target} is unavailable: {source}: "
                "Docker path has no native host mapping"
            )
        try:
            host_path.stat()
        except FileNotFoundError:
            raise RuntimeSpecError(
                f"generated bind source for {target} is missing: {source}"
            ) from None
        except OSError as exc:
            raise RuntimeSpecError(
                f"generated bind source for {target} is unavailable: {source}: {exc}"
            ) from exc


def authorized_project_data_source(project_root: Path) -> Path:
    """Return the sole host-authorized Project-data directory for issuance."""
    project = project_root.resolve(strict=True)
    configured = os.environ.get("BOOLEY_PROJECT_DIR")
    if configured:
        candidate = Path(configured).expanduser()
    else:
        _reject_project_authored_mount_override(project)
        candidate = project / ".booley_project"
    return _validate_project_data_bind_source(project, candidate)


def _validate_project_data_bind_source(project: Path, candidate: Path) -> Path:
    try:
        return _validate_project_data_source(project, candidate)
    except RuntimeSpecError as exc:
        source = docker_mount_path(candidate)
        raise RuntimeSpecError(
            f"generated bind source for /booley-project could not be validated: {source}: {exc}"
        ) from exc


def _reject_project_authored_mount_override(project: Path) -> None:
    config = project / "booley.toml"
    if not config.is_file():
        return
    try:
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeSpecError(f"cannot inspect Project mount configuration: {exc}") from exc
    section = raw.get("project")
    if isinstance(section, dict) and section.get("dir"):
        raise RuntimeSpecError(
            "Project-authored [project].dir cannot authorize a Session Runtime host mount; "
            "set BOOLEY_PROJECT_DIR in the trusted host environment"
        )


def _validate_project_data_source(
    project: Path,
    candidate: Path,
) -> Path:
    if not candidate.is_absolute():
        raise RuntimeSpecError("BOOLEY_PROJECT_DIR must be an absolute host path")
    if any(char == "," or ord(char) < 32 for char in str(candidate)):
        raise RuntimeSpecError("Project-data path contains unsafe Docker mount grammar")
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise RuntimeSpecError("Project-data mount source or its ancestry must not be a symlink")
    try:
        source = candidate.resolve(strict=True)
        info = source.stat()
    except OSError as exc:
        raise RuntimeSpecError(
            f"Project-data mount source is unavailable: {candidate}: {exc}"
        ) from exc
    if not source.is_dir():
        raise RuntimeSpecError(f"Project-data mount source is not a directory: {source}")
    home = Path.home().resolve()
    broad = source == Path(source.anchor) or source == home or source.parent == Path(source.anchor)
    local = source == project / ".booley_project"
    overlaps = (
        source == project or source in project.parents or (project in source.parents and not local)
    )
    if broad or overlaps:
        raise RuntimeSpecError(
            f"Project-data mount source is too broad or overlaps the Project: {source}"
        )
    if os.name != "nt" and info.st_uid != os.getuid():
        raise RuntimeSpecError(
            f"Project-data mount source is not owned by the host user: {source}"
        )
    return source


def _pin_project_data_mount(
    spec: dict[str, Any], expected_source: str, project: Path, project_data: Path
) -> None:
    mounts = spec.get("mounts")
    if not isinstance(mounts, list) or any(not isinstance(item, str) for item in mounts):
        raise RuntimeSpecError("devcontainer.json mounts must be a list of strings")
    indexes = [
        index for index, raw in enumerate(mounts) if _mount_target(raw) == "/booley-project"
    ]
    if len(indexes) != 1:
        raise RuntimeSpecError("Project-data target must have one exact writable bind")
    index = indexes[0]
    allowed = {
        "source=${localWorkspaceFolder}/.booley_project,target=/booley-project,type=bind",
        f"source={expected_source},target=/booley-project,type=bind",
    }
    if mounts[index] not in allowed:
        raise RuntimeSpecError("Project-data mount source was not authorized by the host")
    mounts[index] = f"source={expected_source},target=/booley-project,type=bind"
    shadow_target = _project_data_shadow_target(project, project_data)
    if shadow_target is None:
        return
    if any(_mount_target(raw) == shadow_target for raw in mounts):
        raise RuntimeSpecError("Project-data workspace view is already mounted")
    shadow_source = expected_source
    mounts.insert(index + 1, f"source={shadow_source},target={shadow_target},type=bind")


def _pin_devcontainer_mount(spec: dict[str, Any], project: Path) -> None:
    """Canonicalize the protected definition bind to Docker's host path form."""
    mounts = spec.get("mounts")
    if not isinstance(mounts, list) or any(not isinstance(item, str) for item in mounts):
        raise RuntimeSpecError("devcontainer.json mounts must be a list of strings")
    indexes = [
        index for index, raw in enumerate(mounts) if _mount_target(raw) == DEVCONTAINER_TARGET
    ]
    if len(indexes) != 1:
        raise RuntimeSpecError(
            f"trusted target {DEVCONTAINER_TARGET} must have one exact read-only bind"
        )
    index = indexes[0]
    source = project.resolve() / ".devcontainer"
    canonical = expected_devcontainer_mount(project)
    allowed = {
        f"source={source},target={DEVCONTAINER_TARGET},type=bind,readonly",
        canonical,
    }
    if mounts[index] not in allowed:
        raise RuntimeSpecError(
            f"trusted target {DEVCONTAINER_TARGET} must have one exact read-only bind"
        )
    mounts[index] = canonical


def _project_data_shadow_target(project: Path, source: Path) -> str | None:
    try:
        relative = source.relative_to(project)
    except ValueError:
        return None
    return f"/work/{relative.as_posix()}"


def _pin_initialize_command(project: Path, spec: dict[str, Any]) -> None:
    if spec.get("initializeCommand") != initialize_command():
        raise RuntimeSpecError("devcontainer.json has no fixed host validation command")
    executable = _find_trusted_validator(project)
    if executable is None:
        raise RuntimeSpecError(
            "cannot resolve the trusted host Booley executable; reinstall Booley with "
            "pipx or add its Python scripts directory to PATH"
        )
    spec["initializeCommand"] = initialize_command(str(executable))


def _validate_initialize_command(project: Path, raw: object) -> None:
    if not isinstance(raw, list) or len(raw) != 5 or raw[1:] != initialize_command()[1:]:
        raise RuntimeSpecError("devcontainer.json has no fixed host validation command")
    executable = Path(raw[0]) if isinstance(raw[0], str) else Path()
    if not _trusted_validator(executable, project):
        raise RuntimeSpecError("host Booley executable is not a trusted absolute path")


def _initialize_executable(spec: dict[str, Any]) -> Path:
    raw = spec.get("initializeCommand")
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], str):
        raise RuntimeSpecError("devcontainer.json has no fixed host validation command")
    return Path(raw[0])


def _validator_script_directories() -> tuple[Path, ...]:
    """Supported interpreter-owned script directories outside ``PATH``."""
    directories = [Path(sysconfig.get_path("scripts"))]
    try:
        user_scheme = sysconfig.get_preferred_scheme("user")
        directories.append(Path(sysconfig.get_path("scripts", scheme=user_scheme)))
    except (KeyError, TypeError, ValueError):
        pass
    return tuple(dict.fromkeys(directory.resolve() for directory in directories))


def _invoked_validator_candidate() -> Path | None:
    """Return the absolute ``booley`` entry point used for this process."""
    invoked = Path(sys.argv[0])
    if invoked.is_absolute() and invoked.name.casefold() in {"booley", "booley.exe"}:
        return invoked
    return None


def _path_validator_candidates() -> tuple[Path, ...]:
    """Return every ``booley`` location represented by ``PATH`` in order."""
    names = ("booley.exe", "booley") if os.name == "nt" else ("booley",)
    return tuple(
        dict.fromkeys(
            Path(directory) / name
            for directory in os.environ.get("PATH", "").split(os.pathsep)
            if directory
            for name in names
        )
    )


def _is_executable_file(candidate: Path) -> bool:
    """Return whether a candidate exists and could win command resolution."""
    try:
        return candidate.is_file() and os.access(candidate, os.X_OK)
    except OSError:
        return False


def _find_trusted_validator(project: Path) -> Path | None:
    """Select the first trusted installed ``booley``, including off-PATH installs."""
    invoked = _invoked_validator_candidate()
    if invoked is not None:
        return _resolve_trusted_validator(invoked, project)
    path_candidates = _path_validator_candidates()
    for candidate in path_candidates:
        canonical = _resolve_trusted_validator(candidate, project)
        if canonical is not None:
            return canonical
    shell_candidate = shutil.which("booley")
    if shell_candidate:
        canonical = _resolve_trusted_validator(Path(shell_candidate), project)
        if canonical is not None:
            return canonical
    if shell_candidate or any(_is_executable_file(candidate) for candidate in path_candidates):
        return None
    for directory in _validator_script_directories():
        candidate = directory / ("booley.exe" if os.name == "nt" else "booley")
        canonical = _resolve_trusted_validator(candidate, project)
        if canonical is not None:
            return canonical
    return None


def _resolve_trusted_validator(executable: Path, project: Path) -> Path | None:
    """Canonicalize a discovered launcher, then validate the executable it names."""
    try:
        canonical = executable.resolve(strict=True)
    except OSError:
        return None
    return canonical if _trusted_validator(canonical, project) else None


def _trusted_validator(executable: Path, project: Path) -> bool:
    """Require a canonical executable under a host-owned installation prefix."""
    try:
        canonical = executable.resolve(strict=True)
    except OSError:
        return False
    if (
        not executable.is_absolute()
        or canonical != executable
        or not canonical.is_file()
        or not os.access(canonical, os.X_OK)
        or project == canonical
        or project in canonical.parents
    ):
        return False
    prefix_anchors = _validator_prefix_anchors()
    anchor = next(
        (
            trusted_anchor
            for prefix, trusted_anchor in prefix_anchors.items()
            if canonical.parent == prefix and project not in prefix.parents
        ),
        None,
    )
    if anchor is None:
        return False
    if os.name == "nt":
        return True
    return _secure_validator_ancestry(canonical, anchor)


def _validator_prefix_anchors() -> dict[Path, Path]:
    home = Path.home().resolve()
    environment = Path(sys.prefix).resolve()
    environment_anchor = (
        home if environment == home or home in environment.parents else environment
    )
    anchors = {
        (home / ".local" / "bin").resolve(): home,
        Path("/usr/local/bin").resolve(): Path("/usr").resolve(),
        Path("/usr/bin").resolve(): Path("/usr").resolve(),
        Path("/bin").resolve(): Path("/usr").resolve(),
        (environment / ("Scripts" if os.name == "nt" else "bin")).resolve(): environment_anchor,
    }
    for directory in _validator_script_directories():
        if directory == home or home in directory.parents:
            anchors[directory] = home
    return anchors


def _secure_validator_ancestry(executable: Path, anchor: Path) -> bool:
    """Reject validator replacement by an untrusted owner or writable ancestry."""
    current_uid = os.geteuid()
    path = executable
    while True:
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            return False
        mode = stat.S_IMODE(info.st_mode)
        if info.st_uid not in {0, current_uid} or mode & stat.S_IWOTH:
            return False
        if mode & stat.S_IWGRP and not _private_primary_group(info.st_gid, current_uid):
            return False
        if path == anchor:
            return True
        if anchor not in path.parents:
            return False
        path = path.parent


def _private_primary_group(group_id: int, user_id: int) -> bool:
    """Return whether *group_id* is the current user's exclusive primary group."""
    import grp
    import pwd

    try:
        account = pwd.getpwuid(user_id)
        group = grp.getgrgid(group_id)
        primary_users = {entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == group_id}
    except (KeyError, OSError):
        return False
    return (
        group_id == account.pw_gid
        and primary_users == {account.pw_name}
        and set(group.gr_mem).issubset({account.pw_name})
    )


def _require_project_data_mount(
    mounts: list[str],
    source: str | None,
    project_root: Path,
) -> str | None:
    if not source:
        raise RuntimeSpecError("host issuance lacks an authorized Project-data mount source")
    project = Path(source)
    canonical = _validate_project_data_bind_source(project_root, project)
    if canonical != project:
        raise RuntimeSpecError("issued Project-data mount source is no longer canonical")
    expected_source = docker_mount_path(project)
    expected = f"source={expected_source},target=/booley-project,type=bind"
    matches = [item for item in mounts if _mount_target(item) == "/booley-project"]
    if matches != [expected]:
        raise RuntimeSpecError("Project-data target must have one exact host-authorized bind")
    shadow_target = _project_data_shadow_target(project_root, project)
    if shadow_target is None:
        return None
    expected_shadow = f"source={expected_source},target={shadow_target},type=bind"
    shadows = [item for item in mounts if _mount_target(item) == shadow_target]
    if shadows != [expected_shadow] or mounts.index(expected_shadow) < mounts.index(expected):
        raise RuntimeSpecError(
            "Project-data workspace view must be pinned by the exact host-authorized bind"
        )
    return shadow_target


def _validate_state_volume(mounts: list[str], app: object, project: Path) -> None:
    from booley.harness import devcontainer as dc

    expected = dc.state_volume_mount(str(app), dc.canonical_project_id(project))
    targets = {f"{dc.AGENT_HOME}/.claude", f"{dc.AGENT_HOME}/.codex"}
    actual = [raw for raw in mounts if _mount_target(raw) in targets]
    if actual != ([] if expected is None else [expected]):
        raise RuntimeSpecError("persistent agent state is not scoped to the canonical Project")


def _require_exact_readonly_mount(mounts: list[str], expected: str, *, last: bool = False) -> None:
    target = _mount_target(expected)
    matches = [item for item in mounts if _mount_target(item) == target]
    if matches != [expected] or "readonly=false" in expected:
        raise RuntimeSpecError(f"trusted target {target} must have one exact read-only bind")
    if last and mounts[-1] != expected:
        raise RuntimeSpecError(f"trusted target {target} must be the final mount")


def _mount_target(raw: str) -> str:
    for field in raw.split(","):
        key, separator, value = field.partition("=")
        if separator and key == "target":
            return value
    return ""


def _mount_fields(raw: str) -> dict[str, str]:
    """Parse Booley's generated Docker mount grammar."""
    return dict(field.split("=", 1) for field in raw.split(",") if "=" in field)


def _runtime_authority_error(
    spec: dict[str, Any], host_provisioning: bool, exc: authority.AuthorityError
) -> RuntimeSpecError:
    """Add sealed bind context when Vivado authority fails before mount validation."""
    mounts = spec.get("mounts")
    if (
        host_provisioning
        and isinstance(exc, authority.InstallationValidationError)
        and isinstance(mounts, list)
    ):
        sources = [
            fields.get("source", "")
            for raw in mounts
            if isinstance(raw, str)
            and (fields := _mount_fields(raw)).get("type") == "bind"
            and fields.get("target") == CONTAINER_TARGET
        ]
        if len(sources) == 1 and sources[0]:
            return RuntimeSpecError(
                f"generated bind source for {CONTAINER_TARGET} could not be validated: "
                f"{sources[0]}: {exc}"
            )
    return RuntimeSpecError(str(exc))


def _optional_license(project: Path) -> authority.LicenseProfile | None:
    try:
        return authority.resolve_license(project)
    except authority.AuthorityError as exc:
        if (
            "no exact" in str(exc)
            or "authority directory is missing" in str(exc)
            or "authority registry is missing" in str(exc)
        ):
            return None
        raise RuntimeSpecError(str(exc)) from exc


def _resolve_image_id(image: str) -> str:
    from booley.harness import interactive_docker as docker

    value = docker.image_id(image)
    if not value:
        raise RuntimeSpecError(f"cannot resolve Session Runtime image to an immutable ID: {image}")
    return value


def _retain_issued_image(issuance: Issuance) -> None:
    """Make the issuance survive container deletion and Docker image pruning.

    The keeper is project-scoped and intentionally mutable: reissuing a project
    moves this one private tag to the newly approved immutable ID. Runtime specs
    and stamps continue to use the ID itself as their authority boundary.
    """
    from booley.harness import interactive_docker as docker

    try:
        retained_id = _resolve_image_id(issuance.keeper_image)
    except RuntimeSpecError:
        retained_id = None
    if retained_id == issuance.image_id:
        return
    try:
        docker.tag_image(issuance.image_id, issuance.keeper_image)
    except RuntimeError as exc:
        raise RuntimeSpecError(str(exc)) from exc
    if _resolve_image_id(issuance.keeper_image) != issuance.image_id:
        raise RuntimeSpecError("issued Session Runtime image keeper could not be verified")


def _relay_image_id(profile: authority.LicenseProfile) -> str:
    """Resolve the licensed topology image exactly while authority is locked."""
    del profile
    from .licensing.flexnet_docker import RelayDockerError, resolve_relay_image_id

    try:
        return resolve_relay_image_id()
    except RelayDockerError as exc:
        raise RuntimeSpecError(f"cannot resolve immutable FlexNet relay image: {exc}") from exc


def _validate_image_contract(image_id: str) -> None:
    """Inspect the fixed compatibility contract without executing image code."""
    from booley.harness import interactive_docker as docker

    created = docker._run_docker(
        [
            "container",
            "create",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "/bin/false",
            image_id,
        ],
        timeout=60,
    )
    container = created.stdout.strip()
    if created.returncode != 0 or not container:
        raise RuntimeSpecError("cannot create inert container for Session image inspection")
    try:
        with tempfile.TemporaryDirectory(prefix="booley-image-contract-") as raw:
            root = Path(raw)
            sources = {
                "/usr/local/bin/vivado": root / "vivado-wrapper",
                "/usr/lib/x86_64-linux-gnu/libudev.so.1": root / "libudev.so.1",
                "/usr/lib/x86_64-linux-gnu/libpixman-1.so.0": root / "libpixman-1.so.0",
                "/usr/lib/locale/locale-archive": root / "locale-archive",
            }
            for source, destination in sources.items():
                copied = docker._run_docker(
                    ["container", "cp", "-L", f"{container}:{source}", str(destination)],
                    timeout=60,
                )
                if copied.returncode != 0:
                    raise RuntimeSpecError(
                        "Session Runtime image does not satisfy the built-in "
                        f"Vivado compatibility contract ({source} is unavailable)"
                    )
            _validate_extracted_image_contract(root)
    finally:
        removed = docker._run_docker(["container", "rm", "-f", container], timeout=30)
        if removed.returncode != 0:
            raise RuntimeSpecError(
                f"inert Session image inspection container could not be removed: {container}"
            )


def _validate_extracted_image_contract(root: Path) -> None:
    """Validate bytes copied from an inert, never-started candidate container."""
    if _file_sha256(root / "vivado-wrapper") != wrapper_sha256():
        raise RuntimeSpecError("Session Runtime image contains the wrong Vivado wrapper digest")
    for name in ("libudev.so.1", "libpixman-1.so.0"):
        try:
            prefix = (root / name).read_bytes()[:4]
        except OSError as exc:
            raise RuntimeSpecError(f"cannot inspect Session Runtime image library {name}") from exc
        if prefix != b"\x7fELF":
            raise RuntimeSpecError(f"Session Runtime image contains an invalid {name}")
    try:
        locale_archive = (root / "locale-archive").read_bytes()
    except OSError as exc:
        raise RuntimeSpecError("cannot inspect Session Runtime locale archive") from exc
    if b"en_US" not in locale_archive:
        raise RuntimeSpecError("Session Runtime image lacks the required en_US.UTF-8 locale")


def _require_string(spec: dict[str, Any], key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeSpecError(f"devcontainer.json {key!r} must be a non-empty string")
    return value


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeSpecError(f"cannot hash generated Session Runtime spec: {exc}") from exc


def _load_stamp(path: Path) -> Issuance:
    parent = path.parent
    if parent.is_symlink():
        raise RuntimeSpecError(f"host-issued spec directory must not be a symlink: {parent}")
    if path.is_symlink():
        raise RuntimeSpecError(f"host-issued spec stamp must not be a symlink: {path}")
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                raw = json.load(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        stamp = Issuance(**raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeSpecError(f"host-issued spec stamp is missing or corrupt: {exc}") from exc
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeSpecError(f"host-issued spec stamp has insecure ownership/mode: {path}")
    if os.name != "nt":
        parent_info = parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
            raise RuntimeSpecError(
                f"host-issued spec directory has insecure ownership/mode: {parent}"
            )
    if stamp.version != STAMP_VERSION:
        raise RuntimeSpecError("host-issued spec stamp has an unsupported version")
    if (
        not isinstance(stamp.version, int)
        or isinstance(stamp.version, bool)
        or not isinstance(stamp.project_root, str)
        or not Path(stamp.project_root).is_absolute()
        or not isinstance(stamp.spec_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", stamp.spec_sha256) is None
        or not isinstance(stamp.file_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", stamp.file_sha256) is None
        or not isinstance(stamp.project_data_source, str)
        or not stamp.project_data_source
        or not isinstance(stamp.image, str)
        or not stamp.image
        or not isinstance(stamp.image_id, str)
        or not stamp.image_id.startswith("sha256:")
        or not isinstance(stamp.keeper_image, str)
        or re.fullmatch(r"booley-issued-[0-9a-f]{64}:session", stamp.keeper_image) is None
        or not isinstance(stamp.policy_revision, int)
        or isinstance(stamp.policy_revision, bool)
        or (stamp.installation is not None and not isinstance(stamp.installation, str))
        or (stamp.license_profile is not None and not isinstance(stamp.license_profile, str))
        or (stamp.wrapper_sha256 is not None and not isinstance(stamp.wrapper_sha256, str))
        or (
            isinstance(stamp.wrapper_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", stamp.wrapper_sha256) is None
        )
        or not isinstance(stamp.validator_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", stamp.validator_sha256) is None
        or (stamp.relay_image_id is not None and not isinstance(stamp.relay_image_id, str))
        or (
            isinstance(stamp.relay_image_id, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", stamp.relay_image_id) is None
        )
    ):
        raise RuntimeSpecError("host-issued spec stamp contains invalid field types or values")
    return stamp


def _write_stamp(path: Path, issuance: Issuance) -> None:
    authority.ensure_state_dir()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise RuntimeSpecError(f"host-issued spec directory must not be a symlink: {path.parent}")
    if os.name != "nt":
        parent_info = path.parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
            raise RuntimeSpecError(
                f"host-issued spec directory has insecure ownership/mode: {path.parent}"
            )
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(issuance), indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        # Windows does not permit opening a directory as a regular file
        # handle. The file itself was flushed above; the extra directory
        # durability barrier is available only on POSIX hosts.
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temp_path.unlink(missing_ok=True)
