"""Typed project EDA provisioning configuration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.eda.config import (
    EdaConfig,
    EdaConfigError,
    load_eda_config,
    parse_eda_config,
    retired_config_error,
)
from booley.runtime.project_dir import reset_cache


def test_absent_config_is_empty() -> None:
    assert parse_eda_config(None) == {}


def test_image_is_default() -> None:
    assert parse_eda_config({"vivado": {}}) == {"vivado": EdaConfig("vivado")}


def test_host_selects_provisioning_without_an_installation_name() -> None:
    assert parse_eda_config({"vivado": {"provisioning": "host"}}) == {
        "vivado": EdaConfig("vivado", "host")
    }


@pytest.mark.parametrize(
    "raw,match",
    [
        ({"quartus": {}}, "unsupported EDA kind"),
        ({"vivado": {"mount": "/opt"}}, "unknown key"),
        ({"vivado": {"provisioning": "remote"}}, "image.*host"),
        (
            {"vivado": {"provisioning": "host", "installation": "vivado_2025_2"}},
            "installation is retired",
        ),
    ],
)
def test_rejects_invalid_authority_surface(raw: object, match: str) -> None:
    with pytest.raises(EdaConfigError, match=match):
        parse_eda_config(raw)


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ({"flows": {"venue": "host"}}, "[flows].venue"),
        ({"flows": {"fpga": {"venue": "host"}}}, "[flows.fpga].venue"),
        (
            {"flows": {"sim": {"host_setup_commands": ["module load x"]}}},
            "host_setup_commands",
        ),
        ({"sandbox": {"passthrough_env": ["LM_LICENSE_FILE"]}}, "passthrough_env"),
    ],
)
def test_retired_host_surfaces_have_hard_migration_errors(raw: dict, fragment: str) -> None:
    assert fragment in (retired_config_error(raw) or "")


def test_load_eda_config_uses_external_project_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "rtl"
    data = tmp_path / "private-project-data"
    project.mkdir()
    data.mkdir()
    (data / "booley.toml").write_text(
        '[eda.vivado]\nprovisioning = "host"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    reset_cache()
    try:
        assert load_eda_config(project) == {"vivado": EdaConfig("vivado", "host")}
    finally:
        reset_cache()
