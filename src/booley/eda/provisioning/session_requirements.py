"""Resolve EDA facts required by one host-issued Session Runtime."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from booley.eda.config import PROVISIONING_HOST, EdaConfig, EdaConfigError, load_eda_config
from booley.eda.provisioning import authority
from booley.eda.provisioning.licensing.flexnet_docker import (
    RelayDockerError,
    resolve_relay_image_id,
    resources_for_session,
)
from booley.eda.provisioning.policies.vivado import (
    CONTAINER_TARGET,
    POLICY_REVISION,
    wrapper_sha256,
)


class SessionRequirementsError(RuntimeError):
    """EDA requirements are absent, drifted, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class ExpectedEdaIdentity:
    """Persisted EDA identity fields relevant to current-authority validation."""

    installation: str | None
    license_profile: str | None


@dataclass(frozen=True, slots=True)
class SessionEdaRequirements:
    """Immutable EDA facts that Runtime maps into its own specification."""

    installation: authority.Installation | None
    license_profile: authority.LicenseProfile | None
    installation_mount: str | None
    private_network: str | None
    license_environment: str | None
    policy_revision: int
    wrapper_sha256: str | None
    relay_image_id: str | None


@dataclass(frozen=True, slots=True)
class SessionBuildRequirements:
    """Detached value inputs allowed to cross into Runtime spec construction."""

    trusted_mounts: tuple[tuple[str, str], ...]
    container_environment: tuple[tuple[str, str], ...]
    installation_name: str | None
    license_profile_name: str | None


def build_requirements(
    project_root: Path,
    *,
    vivado_enabled: bool,
) -> SessionBuildRequirements:
    """Resolve value-only EDA inputs for a prospective Runtime specification."""
    try:
        _, installation = requested_host_installation(
            project_root,
            vivado_enabled=vivado_enabled,
        )
        profile = requested_license(project_root, vivado_enabled=vivado_enabled)
    except EdaConfigError as exc:
        raise SessionRequirementsError(str(exc)) from exc
    return SessionBuildRequirements(
        trusted_mounts=(
            ((installation.source, CONTAINER_TARGET),) if installation is not None else ()
        ),
        container_environment=(
            (("XILINXD_LICENSE_FILE", f"{profile.lmgrd_port}@booley-license-xilinx"),)
            if profile is not None
            else ()
        ),
        installation_name=installation.name if installation is not None else None,
        license_profile_name=profile.name if profile is not None else None,
    )


def requested_host_installation(
    project_root: Path,
    *,
    vivado_enabled: bool,
) -> tuple[EdaConfig | None, authority.Installation | None]:
    """Resolve the Grant-selected installation for an active host Vivado Flow."""
    try:
        config = load_eda_config(project_root).get("vivado")
    except EdaConfigError as exc:
        raise SessionRequirementsError(str(exc)) from exc
    if not _host_vivado_requested(config, vivado_enabled):
        return config, None
    try:
        return config, authority.resolve_installation(project_root)
    except authority.AuthorityError as exc:
        raise SessionRequirementsError(str(exc)) from exc


def requested_license(
    project_root: Path,
    *,
    vivado_enabled: bool = True,
    expected_name: str | None = "",
) -> authority.LicenseProfile | None:
    """Return the host-selected License Profile when an active Flow requests it."""
    if expected_name is None:
        return None
    project = project_root.resolve(strict=True)
    if expected_name:
        profile = _optional_license(project)
        if profile is None or profile.name != expected_name:
            raise SessionRequirementsError("Project licence grant differs from the issued runtime")
        return profile
    try:
        config = load_eda_config(project).get("vivado")
    except EdaConfigError as exc:
        raise SessionRequirementsError(str(exc)) from exc
    if config is None or not vivado_enabled:
        return None
    return _optional_license(project)


def resolve_for_session(
    project_root: Path,
    *,
    vivado_enabled: bool,
    license_marker: bool,
    expected: ExpectedEdaIdentity | None = None,
    include_relay_identity: bool,
) -> AbstractContextManager[SessionEdaRequirements]:
    """Lease one coherent EDA authority snapshot for Runtime issuance."""
    try:
        config = load_eda_config(project_root).get("vivado")
    except EdaConfigError as exc:
        raise SessionRequirementsError(str(exc)) from exc
    host_provisioning = _host_vivado_requested(config, vivado_enabled)
    eda_requested = host_provisioning or (config is not None and vivado_enabled)
    prior_eda = expected is not None and (
        expected.installation is not None or expected.license_profile is not None
    )
    if not eda_requested and not license_marker and not prior_eda:
        return nullcontext(_requirements(project_root, None, None, include_relay_identity=False))
    return _leased_requirements(
        project_root,
        host_provisioning=host_provisioning,
        include_relay_identity=include_relay_identity,
    )


def _leased_requirements(
    project_root: Path,
    *,
    host_provisioning: bool,
    include_relay_identity: bool,
) -> AbstractContextManager[SessionEdaRequirements]:
    return _authority_lease(
        project_root,
        host_provisioning=host_provisioning,
        include_relay_identity=include_relay_identity,
    )


@contextmanager
def _authority_lease(
    project_root: Path,
    *,
    host_provisioning: bool,
    include_relay_identity: bool,
) -> Iterator[SessionEdaRequirements]:
    try:
        with authority.resolve_for_issuance(project_root, host_provisioning) as (
            installation,
            profile,
        ):
            yield _requirements(
                project_root,
                installation,
                profile,
                include_relay_identity=include_relay_identity,
            )
    except authority.AuthorityError as exc:
        raise SessionRequirementsError(str(exc)) from exc


def _requirements(
    project_root: Path,
    installation: authority.Installation | None,
    profile: authority.LicenseProfile | None,
    *,
    include_relay_identity: bool,
) -> SessionEdaRequirements:
    relay_image = _relay_image_id() if profile is not None and include_relay_identity else None
    return SessionEdaRequirements(
        installation=installation,
        license_profile=profile,
        installation_mount=(
            f"source={installation.source},target={CONTAINER_TARGET},type=bind,readonly"
            if installation is not None
            else None
        ),
        private_network=(
            resources_for_session(str(project_root.resolve())).private_network
            if profile is not None
            else None
        ),
        license_environment=(
            f"{profile.lmgrd_port}@booley-license-xilinx" if profile is not None else None
        ),
        policy_revision=POLICY_REVISION,
        wrapper_sha256=wrapper_sha256() if installation is not None else None,
        relay_image_id=relay_image,
    )


def validate_image_observations(root: Path) -> None:
    """Validate Runtime-extracted files against the built-in Vivado contract."""
    if _file_sha256(root / "vivado-wrapper") != wrapper_sha256():
        raise SessionRequirementsError("Runtime Image contains the wrong Vivado wrapper digest")
    for name in ("libudev.so.1", "libpixman-1.so.0"):
        try:
            prefix = (root / name).read_bytes()[:4]
        except OSError as exc:
            raise SessionRequirementsError(f"cannot inspect Runtime Image library {name}") from exc
        if prefix != b"\x7fELF":
            raise SessionRequirementsError(f"Runtime Image contains an invalid {name}")
    try:
        locale_archive = (root / "locale-archive").read_bytes()
    except OSError as exc:
        raise SessionRequirementsError("cannot inspect Session Runtime locale archive") from exc
    if b"en_US" not in locale_archive:
        raise SessionRequirementsError("Runtime Image lacks the required en_US.UTF-8 locale")


def _host_vivado_requested(config: EdaConfig | None, vivado_enabled: bool) -> bool:
    return bool(config is not None and config.provisioning == PROVISIONING_HOST and vivado_enabled)


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
        raise SessionRequirementsError(str(exc)) from exc


def _relay_image_id() -> str:
    try:
        return resolve_relay_image_id()
    except RelayDockerError as exc:
        raise SessionRequirementsError(
            f"cannot resolve immutable FlexNet relay image: {exc}"
        ) from exc


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SessionRequirementsError(f"cannot inspect Runtime Image file: {exc}") from exc
