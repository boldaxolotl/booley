"""Failure-boundary tests for value-only Session EDA requirements."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.eda.config import EdaConfig, EdaConfigError
from booley.eda.provisioning import authority
from booley.eda.provisioning import session_requirements as requirements
from booley.eda.provisioning.licensing.flexnet_docker import RelayDockerError
from booley.eda.provisioning.policies.vivado import wrapper_path


def test_build_requirements_returns_the_leased_value_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = requirements.SessionBuildRequirements((), (), None, None)

    @contextmanager
    def leased(*_args, **_kwargs):
        yield SimpleNamespace(build=expected)

    monkeypatch.setattr(requirements, "lease_build_requirements", leased)

    assert requirements.build_requirements(tmp_path, vivado_enabled=True) is expected


def test_build_lease_translates_authority_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        requirements,
        "load_eda_config",
        lambda _project: {"vivado": EdaConfig("vivado", "host")},
    )

    @contextmanager
    def unavailable(*_args, **_kwargs):
        raise authority.AuthorityError("grant unavailable")
        yield None, None  # pragma: no cover

    monkeypatch.setattr(authority, "resolve_for_issuance", unavailable)

    with (
        pytest.raises(requirements.SessionRequirementsError, match="grant unavailable"),
        requirements.lease_build_requirements(project, vivado_enabled=True),
    ):
        pass


def test_requested_host_installation_translates_config_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requirements,
        "load_eda_config",
        lambda _project: (_ for _ in ()).throw(EdaConfigError("invalid EDA config")),
    )

    with pytest.raises(requirements.SessionRequirementsError, match="invalid EDA config"):
        requirements.requested_host_installation(tmp_path, vivado_enabled=True)


def test_requested_host_installation_translates_authority_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = EdaConfig("vivado", "host")
    monkeypatch.setattr(requirements, "load_eda_config", lambda _project: {"vivado": config})
    monkeypatch.setattr(
        authority,
        "resolve_installation",
        lambda _project: (_ for _ in ()).throw(authority.AuthorityError("grant missing")),
    )

    with pytest.raises(requirements.SessionRequirementsError, match="grant missing"):
        requirements.requested_host_installation(tmp_path, vivado_enabled=True)


def test_requested_license_translates_config_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        requirements,
        "load_eda_config",
        lambda _project: (_ for _ in ()).throw(EdaConfigError("invalid EDA config")),
    )

    with pytest.raises(requirements.SessionRequirementsError, match="invalid EDA config"):
        requirements.requested_license(project, vivado_enabled=True)


def test_requested_license_returns_optional_active_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    profile = SimpleNamespace(name="site")
    monkeypatch.setattr(
        requirements,
        "load_eda_config",
        lambda _project: {"vivado": EdaConfig("vivado")},
    )
    monkeypatch.setattr(requirements, "_optional_license", lambda _project: profile)

    assert requirements.requested_license(project, vivado_enabled=True) is profile


def test_session_resolution_translates_config_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requirements,
        "load_eda_config",
        lambda _project: (_ for _ in ()).throw(EdaConfigError("invalid EDA config")),
    )

    with pytest.raises(requirements.SessionRequirementsError, match="invalid EDA config"):
        requirements.resolve_for_session(
            tmp_path,
            vivado_enabled=True,
            license_marker=False,
            include_relay_identity=False,
        )


@pytest.mark.parametrize(
    "message",
    [
        "no exact licence grant",
        "authority directory is missing",
        "authority registry is missing",
    ],
)
def test_optional_license_treats_absent_authority_as_unlicensed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    monkeypatch.setattr(
        authority,
        "resolve_license",
        lambda _project: (_ for _ in ()).throw(authority.AuthorityError(message)),
    )

    assert requirements._optional_license(tmp_path) is None


def test_optional_license_translates_unexpected_authority_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        authority,
        "resolve_license",
        lambda _project: (_ for _ in ()).throw(authority.AuthorityError("registry corrupt")),
    )

    with pytest.raises(requirements.SessionRequirementsError, match="registry corrupt"):
        requirements._optional_license(tmp_path)


def test_relay_identity_translates_docker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requirements,
        "resolve_relay_image_id",
        lambda: (_ for _ in ()).throw(RelayDockerError("daemon unavailable")),
    )

    with pytest.raises(requirements.SessionRequirementsError, match="daemon unavailable"):
        requirements._relay_image_id()


def test_missing_runtime_wrapper_is_reported(tmp_path: Path) -> None:
    with pytest.raises(requirements.SessionRequirementsError, match="cannot inspect"):
        requirements.validate_image_observations(tmp_path)


def test_missing_runtime_locale_archive_is_reported(tmp_path: Path) -> None:
    (tmp_path / "vivado-wrapper").write_bytes(wrapper_path().read_bytes())
    (tmp_path / "libudev.so.1").write_bytes(b"\x7fELFfixture")
    (tmp_path / "libpixman-1.so.0").write_bytes(b"\x7fELFfixture")

    with pytest.raises(requirements.SessionRequirementsError, match="locale archive"):
        requirements.validate_image_observations(tmp_path)
