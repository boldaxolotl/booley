"""Host issuance and full validation of immutable Sandbox specs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from booley.config.flow_enablement import FlowConfigError, flow_enabled
from booley.config.host_config import HostConfigError, load_host_policy
from booley.core.booley_dir_links import is_booley_dir_entry
from booley.core.booley_dir_pin import BooleyDirPinError, require_pinned_entry
from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_int,
    require_opt_str,
    require_str,
)
from booley.core.differences import format_differences
from booley.core.egress_modes import declared_egress
from booley.core.private_store import PrivateStore
from booley.core.project_identity import project_label_id
from booley.core.user_paths import config_dir
from booley.eda.provisioning import authority
from booley.eda.provisioning import session_requirements as eda_requirements
from booley.eda.provisioning.policies.vivado import CONTAINER_TARGET
from booley.runtime.compiler_cache import COMPILER_CACHE_ROOT_ENV, ISSUED_COMPILER_CACHE_ROOT
from booley.runtime.devcontainer import (
    BOOLEY_DIR_TARGET,
    EGRESS_NETWORK,
    UNPROXIED_NETWORK,
    devcontainer_path,
    render_devcontainer_json,
)
from booley.runtime.filesystem_utils import (
    open_with_sharing_retry,
    read_file_bytes,
    replace_file,
    unlink_file,
)
from booley.runtime.incontainer_git_identity import (
    GitIdentity,
    GitIdentityError,
    git_identity_environment,
    load_git_identity,
)
from booley.runtime.platform_paths import docker_mount_path, host_path_from_docker_mount
from booley.runtime.timefmt import LOCAL_TIMEZONE_ENV

_IS_WINDOWS = os.name == "nt"
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
_FIXED_REGISTRAR = "python -m booley.harness.incontainer_register"
_LEGACY_FIXED_REGISTRAR = "python -m booley.runtime.incontainer_register"
_ACCEPTED_FIXED_REGISTRARS = frozenset({_FIXED_REGISTRAR, _LEGACY_FIXED_REGISTRAR})
_FIXED_GIT_IDENTITY = "python -m booley.runtime.incontainer_git_identity_cleanup"
_ACCEPTED_FIXED_START_TAILS = frozenset(
    {_FIXED_GIT_IDENTITY}
    | {f"{_FIXED_GIT_IDENTITY} && {registrar}" for registrar in _ACCEPTED_FIXED_REGISTRARS}
)
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
_FIXED_RUN_ARGS = (
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
)
# Host ``[sandbox] egress = "unproxied"``: the routed network takes the slot.
_UNPROXIED_RUN_ARGS = tuple(
    UNPROXIED_NETWORK if item == EGRESS_NETWORK else item for item in _FIXED_RUN_ARGS
)
_AGENT_APPS = frozenset({"claude", "codex", "none"})
# Proxy environment: required and fixed under host egress "proxy", absent unproxied.
_PROXY_REMOTE_ENV = {
    "HTTP_PROXY": "http://booley-proxy:8080",
    "HTTPS_PROXY": "http://booley-proxy:8080",
    "http_proxy": "http://booley-proxy:8080",
    "https_proxy": "http://booley-proxy:8080",
    "NO_PROXY": "localhost,127.0.0.1",
}
_REQUIRED_REMOTE_ENV = frozenset(
    {
        "BOOLEY_MCP_MODE",
        "BOOLEY_DIR",
        COMPILER_CACHE_ROOT_ENV,
        "BOOLEY_AGENT_APP",
    }
)
_ALLOWED_REMOTE_ENV = (
    _REQUIRED_REMOTE_ENV
    | set(_PROXY_REMOTE_ENV)
    | set(git_identity_environment(GitIdentity("Dev", "dev@localhost")))
    | {
        "ANTHROPIC_API_KEY",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_CREDENTIAL_SCHEMA",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        LOCAL_TIMEZONE_ENV,
    }
)
_FIXED_REMOTE_ENV = {
    "BOOLEY_MCP_MODE": "interactive",
    "BOOLEY_DIR": BOOLEY_DIR_TARGET,
    COMPILER_CACHE_ROOT_ENV: ISSUED_COMPILER_CACHE_ROOT,
}


#: Issued container environment that names the pinned Sandbox Image for deep Doctor.
SANDBOX_IMAGE_ID_ENV = "BOOLEY_SANDBOX_IMAGE_ID"


class RuntimeSpecError(RuntimeError):
    """A Sandbox spec is missing, drifted, or outside issued policy."""


class IssuanceUnavailableError(RuntimeSpecError):
    """The host cannot authenticate a predecessor for managed refresh."""


class HostPolicyError(RuntimeSpecError):
    """The host config.toml carrying the Sandbox Policy is invalid."""

    def __init__(self, error: HostConfigError) -> None:
        super().__init__(str(error))
        self.path = error.path


class EgressDriftError(RuntimeSpecError):
    """A Sandbox spec was issued for the other host ``[sandbox] egress`` mode."""


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
    project_data_layout: str = "legacy"


@dataclass(frozen=True, slots=True)
class SessionSpecInputs:
    """Value-only host inputs supplied to a pure Runtime spec builder."""

    project_data_source: Path
    trusted_eda_mounts: tuple[tuple[str, str], ...]
    fixed_container_environment: tuple[tuple[str, str], ...]
    installation_name: str | None
    license_profile_name: str | None
    egress: str = "proxy"


@dataclass(frozen=True, slots=True)
class PreparedSessionSpec:
    """Side-effect-free, pinned and sealed prospective Sandbox specification."""

    spec: dict[str, Any]
    digest: str
    inputs: SessionSpecInputs
    prospective_issuance: Issuance | None = None


SpecBuilder = Callable[[SessionSpecInputs], dict[str, Any]]


def _issuance_mapping(issuance: Issuance | None) -> dict[str, object]:
    if issuance is None:
        return {field.name: None for field in fields(Issuance)}
    return asdict(issuance)


def _authentication_differences(
    issuance: Issuance,
    project: Path,
    spec: dict[str, Any],
    spec_path: Path,
) -> str | None:
    recorded = {
        "project_root": issuance.project_root,
        "file_sha256": issuance.file_sha256,
        "spec_sha256": issuance.spec_sha256,
    }
    actual = {
        "project_root": str(project),
        "file_sha256": _file_sha256(spec_path),
        "spec_sha256": _spec_digest(spec),
    }
    return format_differences(recorded, actual) if recorded != actual else None


def issuance_from_document(raw: object) -> Issuance:
    """Decode one complete persisted issuance using the stamp schema."""
    try:
        values = require_dict(raw, field="host-issued spec stamp")
    except BoundaryError as exc:
        raise RuntimeSpecError(str(exc)) from exc
    expected = {field.name for field in fields(Issuance)}
    if set(values) not in (expected, expected - {"project_data_layout"}):
        raise RuntimeSpecError("host-issued spec stamp has unexpected or missing fields")
    try:
        issuance = Issuance(
            version=require_int(values.get("version"), field="issuance version"),
            project_root=require_str(values, "project_root"),
            spec_sha256=require_str(values, "spec_sha256"),
            image=require_str(values, "image"),
            image_id=require_str(values, "image_id"),
            keeper_image=require_str(values, "keeper_image"),
            policy_revision=require_int(
                values.get("policy_revision"), field="issuance policy_revision"
            ),
            installation=require_opt_str(values, "installation"),
            license_profile=require_opt_str(values, "license_profile"),
            wrapper_sha256=require_opt_str(values, "wrapper_sha256"),
            relay_image_id=require_opt_str(values, "relay_image_id"),
            validator_sha256=require_str(values, "validator_sha256"),
            file_sha256=require_opt_str(values, "file_sha256"),
            project_data_source=require_opt_str(values, "project_data_source"),
            project_data_layout=(
                require_str(values, "project_data_layout")
                if "project_data_layout" in values
                else "legacy"
            ),
        )
    except BoundaryError as exc:
        raise RuntimeSpecError(f"host-issued spec stamp is invalid: {exc}") from exc
    _validate_issuance_fields(issuance)
    return issuance


def _validate_issuance_fields(issuance: Issuance) -> None:
    digests = (issuance.spec_sha256, issuance.validator_sha256, issuance.file_sha256)
    if issuance.version != STAMP_VERSION:
        raise RuntimeSpecError("host-issued spec stamp has an unsupported version")
    if (
        not Path(issuance.project_root).is_absolute()
        or issuance.policy_revision < 0
        or any(value is None or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in digests)
        or not issuance.image_id.startswith("sha256:")
        or re.fullmatch(r"booley-issued-[0-9a-f]{64}:session", issuance.keeper_image) is None
        or (
            issuance.wrapper_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", issuance.wrapper_sha256) is None
        )
        or (
            issuance.relay_image_id is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", issuance.relay_image_id) is None
        )
        or issuance.project_data_source is None
        or issuance.project_data_layout not in {"legacy", "canonical-alias"}
    ):
        raise RuntimeSpecError("host-issued spec stamp contains invalid field types or values")


def stamp_path(project_root: Path) -> Path:
    """Private host path keyed by exact canonical Project identity."""
    return stamp_path_for_identity(str(project_root.resolve()))


def stamp_path_for_identity(project_root: str) -> Path:
    """Private host path for an already-canonical persisted Project identity."""
    identity = hashlib.sha256(project_root.encode()).hexdigest()
    return _stamp_store().root / f"{identity}.json"


def invalidate_project(project_root: str) -> None:
    """Remove the host-issued stamp for an already-canonical Project identity."""
    unlink_file(stamp_path_for_identity(project_root), missing_ok=True)
    unlink_file(_legacy_stamp_path(project_root), missing_ok=True)


def _stamp_store() -> PrivateStore:
    return PrivateStore(
        config_dir() / "runtime" / "session-specs",
        config_dir().parent,
        "host-issued Sandbox spec",
        RuntimeSpecError,
    )


def validate_auth_reseed_storage(project_root: Path) -> None:
    """Admit Project-owned and conditional issuance storage without mutation."""
    from booley.config.eda import EdaConfigError
    from booley.eda.provisioning import authority
    from booley.eda.provisioning.configuration import load_eda_config

    authorized_project_data_source(project_root)
    store = _stamp_store()
    # Validate each existing prefix even if the final stamp directory is absent.
    for root in (store.root.parent, store.root):
        PrivateStore(
            root, store.anchor, store.subject, store.error_type
        ).validate_existing_directory()
    try:
        enabled = flow_enabled("fpga", project_root)
        eda_requirements.inspect_build_requirements(project_root, vivado_enabled=enabled)
        if enabled and "vivado" in load_eda_config(project_root):
            authority.validate_existing_issuance_storage()
    except (
        FlowConfigError,
        EdaConfigError,
        eda_requirements.SessionRequirementsError,
        authority.AuthorityError,
    ) as exc:
        raise RuntimeSpecError(str(exc)) from exc


def _legacy_stamp_path(project_root: str) -> Path:
    identity = hashlib.sha256(project_root.encode()).hexdigest()
    return config_dir() / "eda" / "session-specs" / f"{identity}.json"


def _stamp_read_path(project_root: Path) -> Path:
    preferred = stamp_path(project_root)
    if preferred.exists() or preferred.is_symlink():
        return preferred
    legacy = _legacy_stamp_path(str(project_root.resolve()))
    return legacy if legacy.exists() or legacy.is_symlink() else preferred


def recovery_stamp_path(project_root: Path) -> Path:
    """Return the current or legacy stamp path that recovery must snapshot."""
    return _stamp_read_path(project_root.resolve(strict=True))


def load_issued_snapshot(project_root: Path) -> Issuance:
    """Load structurally valid prior issuance without consulting current authority.

    Session refresh uses this recovery view before replacing an existing
    container.  The current grant may legitimately have changed — healing that
    drift is the point of refresh — while the sealed prior licence decision and
    Project identity still have to be trusted before Docker mutation.
    """
    project = project_root.resolve(strict=True)
    issuance = _load_stamp(_stamp_read_path(project))
    if issuance.project_root != str(project):
        raise RuntimeSpecError("host-issued spec stamp belongs to a different Project")
    if issuance.keeper_image != keeper_image(project):
        raise RuntimeSpecError("host-issued spec image keeper differs from this Project")
    return issuance


def load_recovery_snapshot(project_root: Path, spec: dict[str, Any], spec_path: Path) -> Issuance:
    """Authenticate the exact prior issuance without consulting current grants."""
    project = project_root.resolve(strict=True)
    issuance = _load_stamp(_stamp_read_path(project))
    if differences := _authentication_differences(issuance, project, spec, spec_path):
        raise RuntimeSpecError(
            "prior Sandbox spec differs from its issuance stamp: " + differences
        )
    expected_keeper = keeper_image(project)
    if issuance.keeper_image != expected_keeper:
        raise RuntimeSpecError("prior Sandbox keeper belongs to a different Project")
    try:
        retained_id = _resolve_image_id(expected_keeper)
    except RuntimeSpecError as exc:
        raise RuntimeSpecError("prior Sandbox Image keeper is missing") from exc
    if retained_id != issuance.image_id:
        raise RuntimeSpecError("prior Sandbox Image keeper points at different bytes")
    return issuance


def keeper_image(project_root: Path) -> str:
    """Private Docker tag retaining the image bytes for one Project issuance."""
    return keeper_image_for_identity(str(project_root.resolve()))


def keeper_image_for_identity(project_root: str) -> str:
    """Retain bytes for an already canonical stored Project identity."""
    identity = hashlib.sha256(project_root.encode()).hexdigest()
    return f"booley-issued-{identity}:session"


def initialize_command(executable: str = "booley") -> list[str]:
    """Fixed host-side validator/topology preparer used before VS Code create."""
    return [
        executable,
        "session",
        "prepare",
        "--project",
        "${localWorkspaceFolder}",
    ]


def _legacy_initialize_command() -> list[str]:
    command = initialize_command()
    command[3] = "--project-root"
    return command


def pin_image(spec: dict[str, Any], *, expected_image_id: str | None = None) -> str:
    """Replace a generated image reference with its immutable local image ID."""
    if expected_image_id is None:
        image_id = _resolve_image_id(_require_string(spec, "image"))
    else:
        image_id = _resolve_image_id(expected_image_id)
        if image_id != expected_image_id:
            raise RuntimeSpecError(
                "reconciled Sandbox Image ID no longer resolves to the same artifact"
            )
    spec["image"] = image_id
    spec.setdefault("containerEnv", {})[SANDBOX_IMAGE_ID_ENV] = image_id
    return image_id


@contextlib.contextmanager
def _issuance_requirements(
    project_root: Path,
    spec: dict[str, Any],
    prior: Issuance | None = None,
    *,
    include_relay_identity: bool,
) -> Iterator[eda_requirements.SessionEdaRequirements]:
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
    prior_eda = license_marker or (
        prior is not None and (prior.installation is not None or prior.license_profile is not None)
    )
    with eda_requirements.lease_requirements(
        project_root,
        vivado_enabled=flow_enabled("fpga", project_root),
        prior_eda=prior_eda,
    ) as leased:
        yield eda_requirements.with_relay_identity(leased) if include_relay_identity else leased


def seal(project_root: Path, spec: dict[str, Any]) -> str:
    """Add host-derived networks and issuance labels to a generated spec.

    Docker 29 accepts repeated ``--network`` flags for user-defined networks,
    which lets VS Code create a licensed runtime atomically on both its egress
    network (the restricted egress network under host ``[sandbox] egress =
    "proxy"``, the routed unproxied network otherwise) and its pre-created
    private license network.
    """
    project = project_root.resolve(strict=True)
    egress = _host_egress_mode()
    project_data_path = authorized_project_data_source(project)
    _pin_initialize_command(project, spec)
    _pin_project_data_mount(
        spec,
        docker_mount_path(project_data_path),
        project,
        project_data_path,
    )
    _pin_devcontainer_mount(spec, project)
    try:
        with _issuance_requirements(project, spec, include_relay_identity=False) as requirements:
            return _seal_with_requirements(project, spec, project_data_path, requirements, egress)
    except (
        FlowConfigError,
        authority.AuthorityError,
        eda_requirements.SessionRequirementsError,
    ) as exc:
        raise _runtime_authority_error(spec, exc) from exc


def _seal_with_requirements(
    project: Path,
    spec: dict[str, Any],
    project_data_path: Path,
    requirements: eda_requirements.SessionEdaRequirements,
    egress: str,
) -> str:
    _pin_git_identity(spec, project_data_path)
    run_args = spec.get("runArgs")
    if not isinstance(run_args, list) or any(not isinstance(item, str) for item in run_args):
        raise RuntimeSpecError("devcontainer.json runArgs must be a string list")
    if any(_is_issuance_label(item) for item in run_args):
        raise RuntimeSpecError("generated Sandbox spec is already sealed")
    if requirements.private_network is not None:
        run_args += ["--network", requirements.private_network]
    digest = _spec_digest(spec)
    provisional = _issuance(
        project,
        spec,
        digest,
        requirements,
        file_sha256=None,
        project_data_source=str(project_data_path),
    )
    for label in labels(provisional):
        run_args += ["--label", label]
    _validate_generated_spec(project, spec, requirements, provisional, egress)
    return digest


def requested_license(
    project_root: Path,
    *,
    expected_name: str | None = "",
) -> authority.LicenseProfile | None:
    """Return the host-selected License Profile, when the runtime requests one.

    ``expected_name=None`` is the validated no-licence runtime path: it avoids
    opening the EDA authority store. Omitting the argument retains discovery
    for ``booley init``, before a Sandbox issuance exists.
    """
    try:
        return eda_requirements.requested_license(
            project_root,
            vivado_enabled=flow_enabled("fpga", project_root),
            expected_name=expected_name,
        )
    except (FlowConfigError, eda_requirements.SessionRequirementsError) as exc:
        raise RuntimeSpecError(str(exc)) from exc


def preview(
    project_root: Path,
    build_spec: SpecBuilder,
    *,
    expected_image_id: str | None = None,
) -> PreparedSessionSpec:
    """Build, pin, and seal a Runtime spec without persistent side effects."""
    project = project_root.resolve(strict=True)
    try:
        requirements = eda_requirements.inspect_build_requirements(
            project,
            vivado_enabled=flow_enabled("fpga", project),
        )
        return _prepare_spec(
            project,
            build_spec,
            requirements,
            expected_image_id=expected_image_id,
        )
    except (FlowConfigError, eda_requirements.SessionRequirementsError) as exc:
        raise RuntimeSpecError(str(exc)) from exc


def inspect_prepared(project_root: Path, prepared: PreparedSessionSpec) -> Issuance:
    """Authenticate an issued spec and require exact prepared document content."""
    project = project_root.resolve(strict=True)
    path = devcontainer_path(project)
    if not path.is_file():
        raise RuntimeSpecError("prepared Sandbox specification is missing")
    try:
        actual = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeSpecError("prepared Sandbox specification cannot be read") from exc
    if prepared.digest != _spec_digest(prepared.spec):
        raise RuntimeSpecError("prepared Sandbox specification digest is inconsistent")
    if actual != render_devcontainer_json(prepared.spec):
        raise RuntimeSpecError("devcontainer.json differs from the prepared specification")
    return validate(project, prepared.spec, path)


def _prepare_spec(
    project: Path,
    build_spec: SpecBuilder,
    requirements: eda_requirements.SessionEdaRequirements,
    *,
    expected_image_id: str | None,
) -> PreparedSessionSpec:
    inputs = _session_spec_inputs(project, requirements)
    project_data_path = inputs.project_data_source
    spec = build_spec(inputs)
    if not isinstance(spec, dict):
        raise RuntimeSpecError("Sandbox spec builder must return a dictionary")
    pin_image(spec, expected_image_id=expected_image_id)
    _pin_initialize_command(project, spec)
    _pin_project_data_mount(
        spec,
        docker_mount_path(project_data_path),
        project,
        project_data_path,
    )
    _pin_devcontainer_mount(spec, project)
    digest = _seal_with_requirements(project, spec, project_data_path, requirements, inputs.egress)
    # The Booley Directory layout is derived from the sealed spec (and the pinned
    # image's alias capability), so it belongs to the prospective issuance, not
    # to the host inputs: issue_prepared re-derives the issuance and compares
    # it, while a fresh authority snapshot can never observe a derived layout.
    prospective = _prospective_issuance(
        project,
        spec,
        digest,
        requirements,
        project_data_path,
    )
    return PreparedSessionSpec(spec, digest, inputs, prospective)


def _prospective_issuance(
    project: Path,
    spec: dict[str, Any],
    digest: str,
    requirements: eda_requirements.SessionEdaRequirements,
    project_data_path: Path,
) -> Issuance:
    return _issuance(
        project,
        spec,
        digest,
        requirements,
        file_sha256=None,
        project_data_source=str(project_data_path),
    )


def _session_spec_inputs(
    project: Path,
    requirements: eda_requirements.SessionEdaRequirements,
) -> SessionSpecInputs:
    installation = requirements.installation
    license_environment = (
        {"XILINXD_LICENSE_FILE": requirements.license_environment}
        if requirements.license_environment is not None
        else {}
    )
    return SessionSpecInputs(
        project_data_source=authorized_project_data_source(project),
        trusted_eda_mounts=(
            ((installation.source, CONTAINER_TARGET),) if installation is not None else ()
        ),
        fixed_container_environment=tuple(
            sorted(
                {
                    **license_environment,
                    COMPILER_CACHE_ROOT_ENV: ISSUED_COMPILER_CACHE_ROOT,
                    **_project_identity_environment(authorized_project_data_source(project)),
                }.items()
            )
        ),
        installation_name=installation.name if installation else None,
        license_profile_name=(
            requirements.license_profile.name if requirements.license_profile else None
        ),
        egress=_host_egress_mode(),
    )


def _host_egress_mode() -> str:
    """Return host ``[sandbox] egress``; an invalid host config refuses issuance."""
    try:
        return load_host_policy().egress
    except HostConfigError as exc:
        raise HostPolicyError(exc) from exc


def _revalidate_prepared(
    project: Path,
    prepared: PreparedSessionSpec,
    leased: eda_requirements.SessionEdaRequirements,
    current_builder: Callable[[], SpecBuilder] | None,
) -> None:
    if current_builder is not None:
        current = _prepare_spec(
            project, current_builder(), leased, expected_image_id=prepared.spec["image"]
        )
        if current != prepared:
            raise RuntimeSpecError("Sandbox inputs changed after specification preparation")
    current_inputs = _session_spec_inputs(project, leased)
    if prepared.inputs != current_inputs:
        raise RuntimeSpecError(
            "Sandbox authority changed after its specification was prepared: "
            + format_differences(asdict(prepared.inputs), asdict(current_inputs))
        )
    prospective = _prospective_issuance(
        project,
        prepared.spec,
        prepared.digest,
        leased,
        prepared.inputs.project_data_source,
    )
    if prepared.prospective_issuance != prospective:
        raise RuntimeSpecError(
            "Sandbox policy changed after its specification was prepared: "
            + format_differences(
                _issuance_mapping(prepared.prospective_issuance),
                _issuance_mapping(prospective),
            )
        )


def issue_prepared(
    project_root: Path,
    prepared: PreparedSessionSpec,
    *,
    force_dependencies: bool = False,
    current_builder: Callable[[], SpecBuilder] | None = None,
    reconcile_dashboard: bool = True,
) -> Issuance:
    """Persist one exact preview after revalidating its authority inputs."""
    project = project_root.resolve(strict=True)
    if prepared.digest != _spec_digest(prepared.spec):
        raise RuntimeSpecError("prepared Sandbox specification digest is inconsistent")
    try:
        with eda_requirements.lease_requirements(
            project,
            vivado_enabled=flow_enabled("fpga", project),
        ) as leased:
            _revalidate_prepared(project, prepared, leased, current_builder)
            requirements = eda_requirements.prepare_runtime_dependencies(
                leased,
                force_relay=force_dependencies,
            )
            return _persist_prepared(
                project,
                prepared,
                requirements=requirements,
                reconcile_dashboard=reconcile_dashboard,
            )
    except (FlowConfigError, eda_requirements.SessionRequirementsError) as exc:
        raise RuntimeSpecError(str(exc)) from exc


def issue(project_root: Path, spec: dict[str, Any], spec_path: Path) -> Issuance:
    """Validate trusted inputs and atomically issue the exact generated spec."""
    project = project_root.resolve(strict=True)
    project_data_source = str(authorized_project_data_source(project))
    try:
        with _issuance_requirements(project, spec, include_relay_identity=True) as requirements:
            return _issue_document_with_requirements(
                project,
                spec,
                spec_path,
                requirements,
                project_data_source,
            )
    except (
        FlowConfigError,
        authority.AuthorityError,
        eda_requirements.SessionRequirementsError,
    ) as exc:
        raise _runtime_authority_error(spec, exc) from exc


def _persist_prepared(
    project_root: Path,
    prepared: PreparedSessionSpec,
    *,
    requirements: eda_requirements.SessionEdaRequirements,
    reconcile_dashboard: bool = True,
) -> Issuance:
    """Persist and issue one prepared spec, restoring its predecessor on failure."""
    from booley.runtime import dashboard_tasks, devcontainer

    project = project_root.resolve(strict=True)
    path = devcontainer.devcontainer_path(project)
    if path.is_symlink():
        # Checked before the snapshot so rollback never replaces or unlinks the link.
        raise RuntimeSpecError(f"refusing to write through symlinked {path}")
    previous = path.read_bytes() if path.is_file() else None
    previous_mode = stat.S_IMODE(path.stat().st_mode) if previous is not None else 0o644
    stamp_paths = (stamp_path_for_identity(str(project)), _legacy_stamp_path(str(project)))
    stamp_snapshots = tuple(_file_snapshot(item, default_mode=0o600) for item in stamp_paths)
    previous_issuance = None
    previous_stamp_path = _stamp_read_path(project)
    if previous_stamp_path.is_file():
        with contextlib.suppress(RuntimeSpecError):
            previous_issuance = _load_stamp(previous_stamp_path)
    previous_keeper_id = (
        previous_issuance.image_id
        if previous_issuance is not None
        else _current_keeper_id(project)
    )
    task_transaction = None
    try:
        written = devcontainer.write_devcontainer(project, prepared.spec)
        if reconcile_dashboard:
            task_transaction = dashboard_tasks.reconcile(
                project, prepared.inputs.project_data_source
            )
        return _issue_document_with_requirements(
            project,
            prepared.spec,
            written,
            requirements,
            str(authorized_project_data_source(project)),
        )
    except Exception:
        if task_transaction is not None:
            task_transaction.rollback()
        _restore_file(path, previous, previous_mode)
        for stamp_file, (content, mode) in zip(stamp_paths, stamp_snapshots, strict=True):
            _restore_file(stamp_file, content, mode)
        _restore_keeper(project, previous_keeper_id)
        raise


def _file_snapshot(path: Path, *, default_mode: int) -> tuple[bytes | None, int]:
    if not path.is_file():
        return None, default_mode
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore_file(path: Path, content: bytes | None, mode: int) -> None:
    if content is None:
        unlink_file(path, missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _current_keeper_id(project: Path) -> str | None:
    from booley.runtime import interactive_docker as docker

    try:
        return docker.image_id_strict(keeper_image(project))
    except RuntimeError as exc:
        raise RuntimeSpecError(
            "cannot snapshot the prior Sandbox Image keeper before issuance"
        ) from exc


def _restore_keeper(project: Path, image_id: str | None) -> None:
    from booley.runtime import interactive_docker as docker

    keeper = keeper_image(project)
    try:
        if image_id is None:
            docker.remove_image_tag(keeper)
            return
        docker.tag_image(image_id, keeper)
    except RuntimeError as exc:
        raise RuntimeSpecError(
            "failed issuance also prevented restoration of the prior Sandbox Image keeper"
        ) from exc
    if _resolve_image_id(keeper) != image_id:
        raise RuntimeSpecError(
            "failed issuance restored the prior stamp but not its Sandbox Image keeper"
        )


def _issue_document_with_requirements(
    project: Path,
    spec: dict[str, Any],
    spec_path: Path,
    requirements: eda_requirements.SessionEdaRequirements,
    project_data_source: str,
) -> Issuance:
    digest = _spec_digest(spec)
    issuance = _issuance(
        project,
        spec,
        digest,
        requirements,
        file_sha256=_file_sha256(spec_path),
        project_data_source=project_data_source,
    )
    _validate_generated_spec(project, spec, requirements, issuance, _host_egress_mode())
    _validate_bind_sources(spec["mounts"])
    image = _require_string(spec, "image")
    image_id = _resolve_image_id(image)
    if image != image_id:
        raise RuntimeSpecError(
            "Sandbox spec image is mutable; regenerate it through `booley init`"
        )
    if requirements.installation is not None:
        _validate_image_contract(image_id)
    _retain_issued_image(issuance)
    _write_stamp(stamp_path(project), issuance)
    unlink_file(_legacy_stamp_path(str(project)), missing_ok=True)
    return issuance


def _validate_image_identity(project: Path, spec: dict[str, Any], stamp: Issuance) -> None:
    expected = {"image": stamp.image, "image_id": stamp.image_id}
    actual = {"image": spec.get("image"), "image_id": _resolve_image_id(stamp.image)}
    if expected != actual:
        raise RuntimeSpecError(
            "Sandbox Image tag/digest has drifted since issuance: "
            + format_differences(expected, actual)
        )
    expected_keeper = keeper_image(project)
    if stamp.keeper_image != expected_keeper:
        raise RuntimeSpecError("Sandbox Image keeper differs from this Project")
    try:
        retained_id = _resolve_image_id(stamp.keeper_image)
    except RuntimeSpecError as exc:
        raise RuntimeSpecError("issued Sandbox Image keeper is missing") from exc
    if retained_id != stamp.image_id:
        raise RuntimeSpecError("issued Sandbox Image keeper points at different bytes")


def _validate_grant_identity(
    stamp: Issuance, requirements: eda_requirements.SessionEdaRequirements
) -> None:
    expected = {
        "installation": requirements.installation.name if requirements.installation else None,
        "license_profile": (
            requirements.license_profile.name if requirements.license_profile else None
        ),
    }
    recorded = {
        "installation": stamp.installation,
        "license_profile": stamp.license_profile,
    }
    if recorded != expected:
        raise RuntimeSpecError(
            "Project grant differs from the issued Sandbox spec: "
            + format_differences(recorded, expected)
        )
    if stamp.policy_revision != requirements.policy_revision:
        raise RuntimeSpecError("Sandbox EDA policy revision has drifted")
    if stamp.relay_image_id != requirements.relay_image_id:
        raise RuntimeSpecError("FlexNet relay image has drifted since spec issuance")
    if stamp.wrapper_sha256 != requirements.wrapper_sha256:
        raise RuntimeSpecError("Booley Vivado wrapper has changed since spec issuance")


def validate(project_root: Path, spec: dict[str, Any], spec_path: Path) -> Issuance:
    """Validate every authority-bearing field against the current host issuance."""
    project = project_root.resolve(strict=True)
    egress = _host_egress_mode()
    stamp = authenticate(project, spec, spec_path)
    try:
        with _issuance_requirements(
            project, spec, stamp, include_relay_identity=True
        ) as requirements:
            _validate_generated_spec(project, spec, requirements, stamp, egress)
            _validate_bind_sources(spec["mounts"])
            _validate_image_identity(project, spec, stamp)
            _validate_grant_identity(stamp, requirements)
            return stamp
    except (
        FlowConfigError,
        authority.AuthorityError,
        eda_requirements.SessionRequirementsError,
    ) as exc:
        raise _runtime_authority_error(spec, exc) from exc


def authenticate(project_root: Path, spec: dict[str, Any], spec_path: Path) -> Issuance:
    """Authenticate immutable host issuance fields without trusting bind sources."""
    project = project_root.resolve(strict=True)
    stamp = _load_stamp(_stamp_read_path(project))
    if differences := _authentication_differences(stamp, project, spec, spec_path):
        raise RuntimeSpecError(
            "devcontainer.json differs from its host-issued specification: " + differences
        )
    _validate_initialize_command(project, spec.get("initializeCommand"))
    validator = _initialize_executable(spec)
    if stamp.validator_sha256 != _file_sha256(validator):
        raise RuntimeSpecError("host Booley validator has changed since spec issuance")
    return stamp


def labels(issuance: Issuance) -> tuple[str, ...]:
    """Return exact resource labels for lifecycle, revoke, Doctor, and reaping."""
    values = {
        "booley.project-id": project_label_id(issuance.project_root),
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
    requirements: eda_requirements.SessionEdaRequirements,
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
        policy_revision=requirements.policy_revision,
        installation=(requirements.installation.name if requirements.installation else None),
        license_profile=(
            requirements.license_profile.name if requirements.license_profile else None
        ),
        wrapper_sha256=requirements.wrapper_sha256,
        relay_image_id=requirements.relay_image_id if file_sha256 is not None else None,
        validator_sha256=_file_sha256(_initialize_executable(spec)),
        file_sha256=file_sha256,
        project_data_source=project_data_source,
        project_data_layout=_spec_project_data_layout(spec),
    )


def expected_vivado_mount(installation: authority.Installation) -> str:
    """Fixed read-only bind for the built-in Vivado policy."""
    return f"source={installation.source},target={CONTAINER_TARGET},type=bind,readonly"


def expected_devcontainer_mount(project_root: Path) -> str:
    """Final nested read-only bind protecting future runtime creation."""
    source = docker_mount_path(project_root.resolve() / ".devcontainer")
    return f"source={source},target={DEVCONTAINER_TARGET},type=bind,readonly"


def _validate_spec_identity(spec: dict[str, Any]) -> str:
    expected_location = {"workspaceFolder": "/work", "remoteUser": "agent"}
    actual_location = {
        "workspaceFolder": spec.get("workspaceFolder"),
        "remoteUser": spec.get("remoteUser"),
    }
    if actual_location != expected_location:
        raise RuntimeSpecError(
            "devcontainer.json workspace/user policy has drifted: "
            + format_differences(expected_location, actual_location)
        )
    remote = spec.get("remoteEnv")
    app = remote.get("BOOLEY_AGENT_APP") if isinstance(remote, dict) else None
    if app not in _AGENT_APPS:
        raise RuntimeSpecError(
            "devcontainer.json Sandbox identity has drifted: "
            + format_differences({"app": sorted(_AGENT_APPS)}, {"app": app})
        )
    expected_name = f"Booley Interactive ({app})"
    if spec.get("name") != expected_name:
        raise RuntimeSpecError(
            "devcontainer.json Sandbox identity has drifted: "
            + format_differences({"name": expected_name}, {"name": spec.get("name")})
        )
    return app


def _validate_generated_spec(
    project: Path,
    spec: dict[str, Any],
    requirements: eda_requirements.SessionEdaRequirements,
    issuance: Issuance,
    egress: str,
) -> None:
    unknown = set(spec) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown or any(key in spec for key in _ESCAPE_KEYS - {"workspaceFolder"}):
        raise RuntimeSpecError("devcontainer.json contains an unsupported escape surface")
    _validate_egress_mode(spec, egress)
    app = _validate_spec_identity(spec)
    expected_workspace = "source=${localWorkspaceFolder},target=/work,type=bind"
    if spec.get("workspaceMount") != expected_workspace:
        raise RuntimeSpecError("devcontainer.json Project workspace mount has drifted")
    _validate_initialize_command(project, spec.get("initializeCommand"))
    if spec.get("shutdownAction") != "none":
        raise RuntimeSpecError("devcontainer.json shutdown policy has drifted")
    if spec.get("updateRemoteUserUID") is not False:
        raise RuntimeSpecError("devcontainer.json immutable user policy has drifted")
    _validate_lifecycle(spec)
    _validate_issued_mounts(project, spec, requirements, issuance, app)
    assert issuance.project_data_source is not None
    _validate_git_identity_environment(spec, Path(issuance.project_data_source))
    _validate_environment(spec, requirements.license_environment, egress)
    _validate_run_args(
        spec.get("runArgs"),
        expected_labels=labels(issuance),
        private_network=requirements.private_network,
        egress=egress,
    )


def _validate_issued_mounts(
    project: Path,
    spec: dict[str, Any],
    requirements: eda_requirements.SessionEdaRequirements,
    issuance: Issuance,
    app: str,
) -> None:
    """Check every mount against the host issuance and its EDA authorization."""
    mounts = spec.get("mounts")
    if not isinstance(mounts, list) or any(not isinstance(item, str) for item in mounts):
        raise RuntimeSpecError("devcontainer.json mounts must be a list of strings")
    project_data_workspace_target = _require_project_data_mount(
        mounts, issuance.project_data_source, project, layout=issuance.project_data_layout
    )
    if issuance.project_data_layout == "canonical-alias" and not _project_data_alias_capable(
        issuance.image_id
    ):
        raise RuntimeSpecError(
            "issued image no longer proves the canonical Booley Directory alias"
        )
    _validate_credential_policy(spec, project, Path(issuance.project_data_source))
    _validate_state_volume(mounts, app, project, spec["remoteEnv"]["BOOLEY_PRIMARY_AUTH"])
    _require_exact_readonly_mount(mounts, expected_devcontainer_mount(project), last=True)
    vivado_mounts = [item for item in mounts if _mount_target(item) == CONTAINER_TARGET]
    if requirements.installation_mount is not None:
        _require_exact_readonly_mount(mounts, requirements.installation_mount)
    elif vivado_mounts:
        raise RuntimeSpecError("devcontainer.json exposes Vivado without host authorization")
    _validate_overlaps(mounts)
    _validate_mount_surfaces(mounts, project_data_workspace_target)


def _validate_egress_mode(spec: dict[str, Any], egress: str) -> None:
    """Refuse a spec issued for the other host egress mode before any shape check."""
    container = spec.get("containerEnv")
    actual = declared_egress(
        container.get("BOOLEY_EGRESS") if isinstance(container, dict) else None
    )
    if actual != egress:
        raise EgressDriftError(
            f"Sandbox spec egress mode '{actual}' differs from host [sandbox] egress = '{egress}'"
        )


def _validate_run_args(
    raw: object,
    *,
    expected_labels: tuple[str, ...],
    private_network: str | None,
    egress: str,
) -> None:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise RuntimeSpecError("devcontainer.json runArgs must be a string list")
    forbidden = {
        "--privileged",
        "--pid=host",
        "--network=host",
        "--ipc=host",
        "--userns=host",
    }
    if any(item in forbidden for item in raw):
        raise RuntimeSpecError("devcontainer.json requests forbidden host authority")
    fixed = _FIXED_RUN_ARGS if egress == "proxy" else _UNPROXIED_RUN_ARGS
    cursor = len(fixed)
    if tuple(raw[:cursor]) != fixed:
        raise RuntimeSpecError(
            "devcontainer.json run arguments differ from fixed runtime hardening: "
            + format_differences(
                {"fixed_hardening": fixed},
                {"fixed_hardening": tuple(raw[:cursor])},
            )
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
            raise RuntimeSpecError("licensed Sandbox private network has drifted")
        cursor += 2
    expected_tail = [value for label in expected_labels for value in ("--label", label)]
    if raw[cursor:] != expected_tail:
        raise RuntimeSpecError(
            "devcontainer.json issuance labels differ from host authority: "
            + _issuance_label_differences(expected_labels, raw[cursor:])
        )


def _issuance_label_differences(expected_labels: tuple[str, ...], tail: list[str]) -> str:
    expected = dict(label.split("=", 1) for label in expected_labels)
    actual = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for flag, item in zip(tail[::2], tail[1::2], strict=False)
        if flag == "--label" and "=" in item and item.split("=", 1)[0] in expected
    }
    return format_differences(expected, actual)


def _project_identity_environment(project_data: Path) -> dict[str, str]:
    try:
        return git_identity_environment(load_git_identity(project_data))
    except GitIdentityError as exc:
        raise RuntimeSpecError(f"invalid [agent.git] identity: {exc}") from exc


def _pin_git_identity(spec: dict[str, Any], project_data: Path) -> None:
    environment = _project_identity_environment(project_data)
    for section in ("containerEnv", "remoteEnv"):
        spec[section] = {**require_dict(spec.get(section), field=section), **environment}


def _validate_git_identity_environment(spec: dict[str, Any], project_data: Path) -> None:
    expected = _project_identity_environment(project_data)
    container, remote = _environment_sections(spec)
    for section in (container, remote):
        if any(section.get(key) != value for key, value in expected.items()):
            raise RuntimeSpecError("[agent.git] Sandbox identity has drifted")


def _environment_sections(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    container = spec.get("containerEnv", {})
    remote = spec.get("remoteEnv", {})
    if not isinstance(container, dict) or not isinstance(remote, dict):
        raise RuntimeSpecError("devcontainer.json environment sections must be objects")
    if "BOOLEY_HOST_MCP_URL" in remote or "BOOLEY_HOST_MCP_URL" in container:
        raise RuntimeSpecError("Host MCP environment is forbidden")
    return container, remote


def _validate_remote_environment(remote: dict[str, Any], egress: str) -> None:
    proxy_env = _PROXY_REMOTE_ENV if egress == "proxy" else {}
    required = _REQUIRED_REMOTE_ENV | set(proxy_env)
    allowed_remote = _ALLOWED_REMOTE_ENV - (set(_PROXY_REMOTE_ENV) - set(proxy_env))
    fixed_remote = {**proxy_env, **_FIXED_REMOTE_ENV}
    if not required.issubset(remote):
        expected_presence = dict.fromkeys(sorted(required), "present")
        actual_presence = {key: "present" for key in sorted(required) if key in remote}
        raise RuntimeSpecError(
            "devcontainer.json is missing fixed Sandbox environment: "
            + format_differences(expected_presence, actual_presence)
        )
    if any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in remote.items()
    ):
        raise RuntimeSpecError("devcontainer.json environment must contain strings only")
    if set(remote) - allowed_remote:
        raise RuntimeSpecError(
            "devcontainer.json contains unsupported Sandbox environment: "
            + format_differences(
                {"unsupported_environment_keys": []},
                {"unsupported_environment_keys": sorted(set(remote) - allowed_remote)},
            )
        )
    if any(remote.get(key) != value for key, value in fixed_remote.items()):
        raise RuntimeSpecError(
            "devcontainer.json fixed Sandbox environment has drifted: "
            + format_differences(
                fixed_remote,
                {key: remote.get(key) for key in fixed_remote},
            )
        )
    app = remote.get("BOOLEY_AGENT_APP")
    from booley.runtime import auth_token

    policy = remote.get("BOOLEY_PRIMARY_AUTH", "auto")
    try:
        allowed = auth_token.allowed_credential_keys(str(app), policy)
    except ValueError as exc:
        raise RuntimeSpecError("invalid Sandbox credential policy; refresh the Sandbox") from exc
    for key in auth_token.CREDENTIAL_ENV_KEYS:
        if key in remote and (key not in allowed or remote[key] != f"${{localEnv:{key}}}"):
            raise RuntimeSpecError("devcontainer.json credential environment has drifted")


def _validate_credential_policy(spec: dict[str, Any], project: Path, project_data: Path) -> None:
    from booley.config.agent import project_credential_policy
    from booley.runtime import auth_token
    from booley.runtime import devcontainer as dc

    _, policy = project_credential_policy(project, booley_dir=project_data)
    remote = spec.get("remoteEnv", {})
    if (
        remote.get("BOOLEY_PRIMARY_AUTH") != policy
        or remote.get("BOOLEY_CREDENTIAL_SCHEMA") != auth_token.CREDENTIAL_SCHEMA
    ):
        raise RuntimeSpecError(
            "Sandbox credential policy/schema has drifted; refresh and recreate the Sandbox"
        )
    app = remote.get("BOOLEY_AGENT_APP")
    allowed = auth_token.allowed_credential_keys(str(app), policy)
    forbidden_targets = {
        target for provider, target in dc._APP_CREDS_SEED_TARGET.items() if provider != app
    }
    forbidden_targets.update(
        target for provider, target in dc._APP_TOKEN_SEED_TARGET.items() if provider != app
    )
    if policy == "api_key":
        forbidden_targets.update(dc._APP_CREDS_SEED_TARGET.values())
    credential = auth_token.CREDENTIALS.get(app)
    if credential is not None and credential.env_var not in allowed:
        forbidden_targets.add(dc._APP_TOKEN_SEED_TARGET[app])
    _validate_credential_seed_sources(spec, str(app), policy)
    hooks = " ".join(str(spec.get(key, "")) for key in ("postCreateCommand", "postStartCommand"))
    for target in forbidden_targets:
        if target in hooks or any(_mount_target(raw) == target for raw in spec.get("mounts", [])):
            raise RuntimeSpecError(
                "Sandbox credential seed violates Project auth policy; refresh the Sandbox"
            )


def _validate_credential_seed_sources(spec: dict[str, Any], app: str, policy: str) -> None:
    from booley.runtime import auth_token
    from booley.runtime import devcontainer as dc

    if policy == "auto":
        return
    for raw in spec.get("mounts", []):
        fields = _mount_fields(raw)
        target = fields.get("target")
        source = Path(fields.get("source", ""))
        if target == dc._APP_CREDS_SEED_TARGET.get(app) and not auth_token.subscription_available(
            app, policy, source
        ):
            raise RuntimeSpecError(
                "Sandbox login seed is not a recognized subscription credential"
            )
        if target == dc._CLAUDE_CONFIG_SEED_TARGET:
            try:
                auth_token.inspect_config_seed(source, policy)
            except ValueError as exc:
                raise RuntimeSpecError(str(exc)) from exc


def _validate_environment(
    spec: dict[str, Any], license_environment: str | None, egress: str
) -> None:
    container, remote = _environment_sections(spec)
    # Checked before the exact key set so a Sandbox issued before the shared
    # compiler cache gets the actionable refresh hint, not a generic key diff.
    issued_roots = (container.get(COMPILER_CACHE_ROOT_ENV), remote.get(COMPILER_CACHE_ROOT_ENV))
    if issued_roots != (ISSUED_COMPILER_CACHE_ROOT, ISSUED_COMPILER_CACHE_ROOT):
        raise RuntimeSpecError("Compiler-cache root differs from fixed Sandbox policy")
    if container.get(SANDBOX_IMAGE_ID_ENV) != spec.get("image"):
        raise RuntimeSpecError("Sandbox Image identity environment differs from the pinned image")
    allowed_container = {
        COMPILER_CACHE_ROOT_ENV,
        SANDBOX_IMAGE_ID_ENV,
        *git_identity_environment(GitIdentity("Dev", "dev@localhost")),
    }
    if license_environment is not None:
        allowed_container.add("XILINXD_LICENSE_FILE")
    if egress == "unproxied":
        # Its value was already checked by _validate_egress_mode.
        allowed_container.add("BOOLEY_EGRESS")
    if set(container) != allowed_container:
        raise RuntimeSpecError(
            "devcontainer.json contains unsupported container environment: "
            + format_differences(
                {"container_environment_keys": sorted(allowed_container)},
                {"container_environment_keys": sorted(container)},
            )
        )
    _validate_remote_environment(remote, egress)
    expected = license_environment
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
        if not fragments or fragments[-1] not in _ACCEPTED_FIXED_START_TAILS:
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
    """Return the sole host-authorized Booley Directory for issuance."""
    project = project_root.resolve(strict=True)
    configured = os.environ.get("BOOLEY_DIR")
    if configured:
        candidate = Path(configured).expanduser()
    else:
        _reject_project_authored_mount_override(project)
        candidate = project / ".booley"
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
            "Project-authored [project].dir cannot authorize a Sandbox host mount; "
            "set BOOLEY_DIR in the trusted host environment"
        )


def _validate_project_data_source(
    project: Path,
    candidate: Path,
) -> Path:
    if not candidate.is_absolute():
        raise RuntimeSpecError("BOOLEY_DIR must be an absolute host path")
    if any(char == "," or ord(char) < 32 for char in str(candidate)):
        raise RuntimeSpecError("Booley Directory path contains unsafe Docker mount grammar")
    # Only the default entry ``<project>/.booley`` itself may be a link; a link
    # above it or any other linked spelling (e.g. a symlinked BOOLEY_DIR) is refused.
    is_link = candidate.is_symlink()
    linked = is_link and is_booley_dir_entry(candidate, project / ".booley")
    if (is_link and not linked) or any(p.is_symlink() for p in candidate.parents):
        raise RuntimeSpecError(
            "Booley Directory mount source or its ancestry must not be a symlink"
        )
    try:
        source = candidate.resolve(strict=True)
        info = source.stat()
    except (OSError, RuntimeError) as exc:  # Python < 3.13 reports a link loop as RuntimeError
        raise RuntimeSpecError(
            f"Booley Directory mount source is unavailable: {candidate}: {exc}"
        ) from exc
    if not source.is_dir():
        raise RuntimeSpecError(f"Booley Directory mount source is not a directory: {source}")
    home = Path.home().resolve()
    broad = source == Path(source.anchor) or source == home or source.parent == Path(source.anchor)
    local = source == project / ".booley"
    overlaps = (
        source == project or source in project.parents or (project in source.parents and not local)
    )
    source_label = "symlink target" if linked else "mount source"
    if broad or overlaps:
        raise RuntimeSpecError(
            f"Booley Directory {source_label} is too broad or overlaps the Project: {source}"
        )
    if os.name != "nt" and info.st_uid != os.getuid():
        raise RuntimeSpecError(
            f"Booley Directory mount source is not owned by the host user: {source}"
        )
    # The marker must be a regular file: a link below the entry is never followed.
    marker = source / "booley.toml"
    if linked and (marker.is_symlink() or not marker.is_file()):
        raise RuntimeSpecError(
            f"Booley Directory symlink target is not a Booley Directory (no booley.toml): {source}"
        )
    if linked:
        _require_host_pin(project)
    # Keep the entry spelling for a link so the mount pin and the /work/.booley
    # shadow match the real-directory layout exactly.
    return candidate if linked else source


def _require_host_pin(project: Path) -> None:
    """Refuse a linked entry whose target the host never pinned (or that moved)."""
    try:
        require_pinned_entry(project)
    except BooleyDirPinError as exc:
        raise RuntimeSpecError(str(exc)) from exc


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
        raise RuntimeSpecError("Booley Directory target must have one exact writable bind")
    index = indexes[0]
    allowed = {
        "source=${localWorkspaceFolder}/.booley,target=/booley-project,type=bind",
        f"source={expected_source},target=/booley-project,type=bind",
    }
    if mounts[index] not in allowed:
        raise RuntimeSpecError("Booley Directory mount source was not authorized by the host")
    mounts[index] = f"source={expected_source},target=/booley-project,type=bind"
    shadow_target = _project_data_shadow_target(project, project_data)
    if shadow_target is None:
        if _project_data_alias_capable(_resolve_image_id(str(spec.get("image", "")))):
            raise RuntimeSpecError(
                "selected image alias hides the external Booley Directory source; an ordinary /booley-project directory is required"
            )
        return
    if any(_mount_target(raw) == shadow_target for raw in mounts):
        raise RuntimeSpecError("Booley Directory workspace view is already mounted")
    shadow_source = expected_source
    mounts.insert(index + 1, f"source={shadow_source},target={shadow_target},type=bind")
    if _project_data_alias_capable(_resolve_image_id(str(spec.get("image", "")))):
        mounts[:] = [
            raw
            for raw in mounts
            if _mount_target(raw) != "/booley-project"
            and not str(_mount_target(raw)).startswith("/booley-project/")
        ]


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
    if spec.get("initializeCommand") not in (initialize_command(), _legacy_initialize_command()):
        raise RuntimeSpecError("devcontainer.json has no fixed host validation command")
    executable = _find_trusted_validator(project)
    if executable is None:
        raise RuntimeSpecError(
            "cannot resolve the trusted host Booley executable; restore the "
            "canonical installed Booley launcher or add its scripts directory to PATH"
        )
    spec["initializeCommand"] = initialize_command(str(executable))


def _validate_initialize_command(project: Path, raw: object) -> None:
    if (
        not isinstance(raw, list)
        or len(raw) != 5
        or raw[1:] not in (initialize_command()[1:], _legacy_initialize_command()[1:])
    ):
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
    """Return the invoked ``booley`` launcher, restoring its Windows suffix."""
    invoked = Path(sys.argv[0])
    name = invoked.name.casefold()
    if not invoked.is_absolute() or name not in {"booley", "booley.exe"}:
        return None
    if _IS_WINDOWS and name == "booley" and not invoked.exists():
        return invoked.with_suffix(".exe")
    return invoked


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
    *,
    layout: str = "legacy",
) -> str | None:
    if not source:
        raise RuntimeSpecError("host issuance lacks an authorized Booley Directory mount source")
    project = Path(source)
    canonical = _validate_project_data_bind_source(project_root, project)
    if canonical != project:
        raise RuntimeSpecError("issued Booley Directory mount source is no longer canonical")
    expected_source = docker_mount_path(project)
    expected = f"source={expected_source},target=/booley-project,type=bind"
    matches = [item for item in mounts if _mount_target(item) == "/booley-project"]
    if layout == "canonical-alias":
        if project != project_root / ".booley":
            raise RuntimeSpecError(
                "canonical alias layout requires checkout-local Booley Directory"
            )
        if any(
            _mount_target(raw) == "/booley-project"
            or str(_mount_target(raw)).startswith("/booley-project/")
            for raw in mounts
        ):
            raise RuntimeSpecError("canonical alias layout forbids alias-subtree mounts")
    elif layout != "legacy" or matches != [expected]:
        raise RuntimeSpecError("Booley Directory target must have one exact host-authorized bind")
    shadow_target = _project_data_shadow_target(project_root, project)
    if shadow_target is None:
        return None
    expected_shadow = f"source={expected_source},target={shadow_target},type=bind"
    shadows = [item for item in mounts if _mount_target(item) == shadow_target]
    if shadows != [expected_shadow] or (
        layout != "canonical-alias" and mounts.index(expected_shadow) < mounts.index(expected)
    ):
        raise RuntimeSpecError(
            "Booley Directory workspace view must be pinned by the exact host-authorized bind"
        )
    return shadow_target


def _validate_state_volume(
    mounts: list[str], app: object, project: Path, policy: str = "auto"
) -> None:
    from booley.runtime import devcontainer as dc

    expected = dc.state_volume_mount(str(app), dc.canonical_project_id(project), policy)
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
    return _mount_fields(raw).get("target", "")


def _mount_fields(raw: str) -> dict[str, str]:
    """Parse Booley's unquoted lowercase Docker mount grammar without ambiguity."""
    fields = {}
    for field in raw.split(","):
        key, separator, value = field.partition("=")
        if '"' in field or key != key.lower():
            raise RuntimeSpecError("mount fields must use canonical lowercase unquoted grammar")
        if not separator:
            continue
        if key == "src":
            key = "source"
        if key in {"target", "dst", "destination"}:
            key = "target"
            value = posixpath.normpath("/" + value.lstrip("/")) if value.startswith("/") else value
        if key in fields:
            raise RuntimeSpecError("mount has conflicting or repeated fields")
        fields[key] = value
    if fields.get("type", "") != fields.get("type", "").lower():
        raise RuntimeSpecError("mount type must use canonical lowercase grammar")
    return fields


def _runtime_authority_error(spec: dict[str, Any], exc: Exception) -> RuntimeSpecError:
    """Add sealed bind context when Vivado authority fails before mount validation."""
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    mounts = spec.get("mounts")
    if isinstance(cause, authority.InstallationValidationError) and isinstance(mounts, list):
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
                f"{sources[0]}: {cause}"
            )
    return RuntimeSpecError(str(exc))


def _resolve_image_id(image: str) -> str:
    from booley.runtime import interactive_docker as docker

    value = docker.image_id(image)
    if not value:
        raise RuntimeSpecError(f"cannot resolve Sandbox Image to an immutable ID: {image}")
    return value


def _retain_issued_image(issuance: Issuance) -> None:
    """Make the issuance survive container deletion and Docker image pruning.

    The keeper is project-scoped and intentionally mutable: reissuing a project
    moves this one private tag to the newly approved immutable ID. Runtime specs
    and stamps continue to use the ID itself as their authority boundary.
    """
    from booley.runtime import interactive_docker as docker

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
        raise RuntimeSpecError("issued Sandbox Image keeper could not be verified")


def _validate_image_contract(image_id: str) -> None:
    """Inspect the fixed compatibility contract without executing image code."""
    from booley.runtime import interactive_docker as docker

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
        raise RuntimeSpecError("cannot create inert container for Sandbox Image inspection")
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
                        "Sandbox Image does not satisfy the built-in "
                        f"Vivado compatibility contract ({source} is unavailable)"
                    )
            try:
                eda_requirements.validate_image_observations(root)
            except eda_requirements.SessionRequirementsError as exc:
                raise RuntimeSpecError(str(exc)) from exc
    finally:
        removed = docker._run_docker(["container", "rm", "-f", container], timeout=30)
        if removed.returncode != 0:
            raise RuntimeSpecError(
                f"inert Sandbox Image inspection container could not be removed: {container}"
            )


def _require_string(spec: dict[str, Any], key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeSpecError(f"devcontainer.json {key!r} must be a non-empty string")
    return value


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(read_file_bytes(path)).hexdigest()
    except OSError as exc:
        raise RuntimeSpecError(f"cannot hash generated Sandbox spec: {exc}") from exc


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
        descriptor = open_with_sharing_retry(lambda: os.open(path, flags), path)
        try:
            info = os.fstat(descriptor)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                raw = json.load(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        stamp = issuance_from_document(raw)
    except FileNotFoundError as exc:
        raise IssuanceUnavailableError(
            "Sandbox issuance was withdrawn (EDA grant change, invalidation or Project forget)"
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeSpecError) as exc:
        raise IssuanceUnavailableError(f"host-issued spec stamp is corrupt: {exc}") from exc
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeSpecError(f"host-issued spec stamp has insecure ownership/mode: {path}")
    if os.name != "nt":
        parent_info = parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
            raise RuntimeSpecError(
                f"host-issued spec directory has insecure ownership/mode: {parent}"
            )
    return stamp


def _write_stamp(path: Path, issuance: Issuance) -> None:
    store = _stamp_store()
    if path.parent == store.root:
        store.ensure_directory()
    else:
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
        replace_file(temp_path, path)
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


def _spec_project_data_layout(spec: dict[str, Any]) -> str:
    mounts = spec.get("mounts", [])
    return (
        "legacy"
        if any(_mount_target(raw) == "/booley-project" for raw in mounts)
        else "canonical-alias"
    )


def _project_data_alias_capable(image_id: str) -> bool:
    from booley.runtime.project_image import DockerImageError, project_data_alias_capable

    if not isinstance(image_id, str):
        raise RuntimeSpecError("Sandbox image identity is unavailable for Booley Directory proof")
    try:
        return project_data_alias_capable(image_id)
    except DockerImageError as exc:
        raise RuntimeSpecError(str(exc)) from exc
