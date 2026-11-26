"""Failure-boundary tests for value-only Session EDA requirements."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests import eda_grant_support
from tests.relay_docker_fake import RelayDocker

from booley.eda.provisioning import authority
from booley.eda.provisioning import session_requirements as requirements
from booley.eda.provisioning.policies.vivado import wrapper_path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / ".booley").mkdir()
    (root / ".booley" / "booley.toml").write_text(
        '[eda.vivado]\nprovisioning = "image"\n', encoding="utf-8"
    )
    return root


def _grant_license(project: Path) -> authority.LicenseProfile:
    profile = eda_grant_support.register_license()
    eda_grant_support.add_grant(project, license_profile=profile.name)
    return profile


def test_requested_license_treats_absent_authority_as_unlicensed(project: Path) -> None:
    assert requirements.requested_license(project) is None  # no authority directory
    authority.ensure_state_dir()
    assert requirements.requested_license(project) is None  # no registry
    profile = eda_grant_support.register_license()
    assert requirements.requested_license(project) is None  # no grant for this Project

    eda_grant_support.add_grant(project, license_profile=profile.name)

    assert requirements.requested_license(project) == profile


def test_corrupt_authority_fails_requirements(project: Path) -> None:
    _grant_license(project)
    authority.state_path().write_text("{}", encoding="utf-8")

    with pytest.raises(requirements.SessionRequirementsError, match="invalid top-level schema"):
        requirements.requested_license(project)
    with (
        pytest.raises(requirements.SessionRequirementsError, match="invalid top-level schema"),
        requirements.lease_requirements(project, vivado_enabled=True),
    ):
        pass


def test_requested_license_translates_config_failure(project: Path) -> None:
    (project / ".booley" / "booley.toml").write_text("[eda.quartus]\n", encoding="utf-8")

    with pytest.raises(requirements.SessionRequirementsError, match="unsupported EDA kind"):
        requirements.requested_license(project, vivado_enabled=True)


def test_lease_opens_authority_for_prior_eda_when_flow_disabled(project: Path) -> None:
    profile = _grant_license(project)

    with requirements.lease_requirements(project, vivado_enabled=False) as idle:
        assert idle.license_profile is None
    with requirements.lease_requirements(project, vivado_enabled=False, prior_eda=True) as prior:
        assert prior.license_profile == profile
        assert prior.license_environment == "27000@booley-license-xilinx"


def test_relay_identity_translates_docker_failure(project: Path) -> None:
    _grant_license(project)
    needs_relay = requirements.inspect_build_requirements(project, vivado_enabled=True)
    docker = RelayDocker(fail_prefix=("image", "inspect"))

    with pytest.raises(
        requirements.SessionRequirementsError, match="cannot resolve immutable FlexNet relay image"
    ):
        requirements.with_relay_identity(needs_relay, runner=docker)


def test_missing_runtime_wrapper_is_reported(tmp_path: Path) -> None:
    with pytest.raises(requirements.SessionRequirementsError, match="cannot inspect"):
        requirements.validate_image_observations(tmp_path)


def test_missing_runtime_locale_archive_is_reported(tmp_path: Path) -> None:
    (tmp_path / "vivado-wrapper").write_bytes(wrapper_path().read_bytes())
    (tmp_path / "libudev.so.1").write_bytes(b"\x7fELFfixture")
    (tmp_path / "libpixman-1.so.0").write_bytes(b"\x7fELFfixture")

    with pytest.raises(requirements.SessionRequirementsError, match="locale archive"):
        requirements.validate_image_observations(tmp_path)
