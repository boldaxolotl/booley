"""Typed project EDA provisioning configuration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.eda.config import (
    EdaConfig,
    EdaConfigError,
    load_eda_config,
    parse_eda_config,
    validate_host_provisioning_platform,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, {}),
        ({"vivado": {}}, {"vivado": EdaConfig("vivado")}),
        ({"vivado": {"provisioning": "host"}}, {"vivado": EdaConfig("vivado", "host")}),
    ],
)
def test_parse_selects_provisioning(raw: object, expected: dict[str, EdaConfig]) -> None:
    assert parse_eda_config(raw) == expected


@pytest.mark.parametrize(
    "provisioning,match", [("host", r"unsupported on Windows.*eda\.vivado"), ("image", None)]
)
def test_windows_accepts_only_image_provisioning(provisioning: str, match: str | None) -> None:
    configs = parse_eda_config({"vivado": {"provisioning": provisioning}})

    if match is None:
        validate_host_provisioning_platform(configs, platform_name="win32")
    else:
        with pytest.raises(EdaConfigError, match=match):
            validate_host_provisioning_platform(configs, platform_name="win32")


@pytest.mark.parametrize(
    "raw,match",
    [
        ({"quartus": {}}, "unsupported EDA kind"),
        pytest.param(
            {"vivado": {"provisioning": "host", "installation": "vivado_2025_2"}},
            r"unknown key\(s\): installation",
            id="installation",
        ),
    ],
)
def test_rejects_invalid_authority_surface(raw: object, match: str) -> None:
    with pytest.raises(EdaConfigError, match=match):
        parse_eda_config(raw)


def test_load_eda_config_uses_external_project_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "rtl"
    data = tmp_path / "private-project-data"
    project.mkdir()
    data.mkdir()
    (data / "booley.toml").write_text('[eda.vivado]\nprovisioning = "host"\n', encoding="utf-8")
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    monkeypatch.setattr("booley.eda.provisioning.configuration.sys.platform", "linux")

    assert load_eda_config(project) == {"vivado": EdaConfig("vivado", "host")}
