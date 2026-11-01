"""Resolve EDA facts required by one host-issued Sandbox."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from booley.config.eda import PROVISIONING_HOST, EdaConfig, EdaConfigError
from booley.eda.provisioning import authority
from booley.eda.provisioning.configuration import load_eda_config
from booley.eda.provisioning.licensing.flexnet_docker import (
    RelayDockerError,
    ensure_relay_image,
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


def inspect_build_requirements(
    project_root: Path,
    *,
    vivado_enabled: bool,
) -> SessionEdaRequirements:
    """Resolve prospective Runtime inputs without creating authority state."""
    project = project_root.resolve(strict=True)
    config = _load_vivado_config(project)
    if config is None or not vivado_enabled:
        return _requirements(project, None, None)
    try:
        installation, profile = authority.resolve_for_inspection(
            project,
            config.provisioning == PROVISIONING_HOST,
        )
    except authority.AuthorityError as exc:
        raise SessionRequirementsError(str(exc)) from exc
    return _requirements(project, installation, profile)


@contextmanager
def lease_requirements(
    project_root: Path,
    *,
    vivado_enabled: bool,
    prior_eda: bool = False,
) -> Iterator[SessionEdaRequirements]:
    """Hold one coherent EDA authority snapshot through Runtime persistence.

    The authority store opens only for an enabled Vivado Flow or when the caller
    reports that the prior issuance named EDA identity; otherwise the requirements
    are empty and the store is left alone.
    """
    project = project_root.resolve(strict=True)
    config = _load_vivado_config(project)
    active = config is not None and vivado_enabled
    if not active and not prior_eda:
        yield _requirements(project, None, None)
        return
    host_provisioning = (
        config is not None and vivado_enabled and config.provisioning == PROVISIONING_HOST
    )
    try:
        with authority.resolve_for_issuance(project, host_provisioning) as (
            installation,
            profile,
        ):
            yield _requirements(project, installation, profile)
    except authority.AuthorityError as exc:
        raise SessionRequirementsError(str(exc)) from exc


def _load_vivado_config(project: Path) -> EdaConfig | None:
    try:
        return load_eda_config(project).get("vivado")
    except EdaConfigError as exc:
        raise SessionRequirementsError(str(exc)) from exc


def with_relay_identity(requirements: SessionEdaRequirements) -> SessionEdaRequirements:
    """Add the immutable relay image ID to licensed requirements; never builds or pulls."""
    if requirements.license_profile is None:
        return requirements
    return replace(requirements, relay_image_id=_relay_image_id())


def prepare_runtime_dependencies(
    requirements: SessionEdaRequirements,
    *,
    force_relay: bool = False,
) -> SessionEdaRequirements:
    """Prepare mutable dependencies after a caller validates one leased value."""
    try:
        if requirements.license_profile is not None:
            ensure_relay_image(force=force_relay)
    except RelayDockerError as exc:
        raise SessionRequirementsError(str(exc)) from exc
    return with_relay_identity(requirements)


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
            raise SessionRequirementsError("Project licence grant differs from the issued Sandbox")
        return profile
    if _load_vivado_config(project) is None or not vivado_enabled:
        return None
    return _optional_license(project)


def _requirements(
    project_root: Path,
    installation: authority.Installation | None,
    profile: authority.LicenseProfile | None,
) -> SessionEdaRequirements:
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
        relay_image_id=None,
    )


def validate_image_observations(root: Path) -> None:
    """Validate Runtime-extracted files against the built-in Vivado contract."""
    if _file_sha256(root / "vivado-wrapper") != wrapper_sha256():
        raise SessionRequirementsError("Sandbox Image contains the wrong Vivado wrapper digest")
    for name in ("libudev.so.1", "libpixman-1.so.0"):
        try:
            prefix = (root / name).read_bytes()[:4]
        except OSError as exc:
            raise SessionRequirementsError(f"cannot inspect Sandbox Image library {name}") from exc
        if prefix != b"\x7fELF":
            raise SessionRequirementsError(f"Sandbox Image contains an invalid {name}")
    try:
        locale_archive = (root / "locale-archive").read_bytes()
    except OSError as exc:
        raise SessionRequirementsError("cannot inspect Sandbox locale archive") from exc
    if b"en_US" not in locale_archive:
        raise SessionRequirementsError("Sandbox Image lacks the required en_US.UTF-8 locale")


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
        raise SessionRequirementsError(f"cannot inspect Sandbox Image file: {exc}") from exc
