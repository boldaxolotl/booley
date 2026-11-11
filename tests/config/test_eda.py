"""Declarative EDA parsing preserves each configuration reader's contract."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
from tests.subprocess_budget import run_bounded_child

from booley.config import eda, project_config, settings


@pytest.fixture
def document(tmp_path, monkeypatch):
    path = tmp_path / "booley.toml"
    monkeypatch.setattr(eda, "resolve_booley_dir", lambda _root: tmp_path)
    monkeypatch.setattr(project_config, "resolve_booley_dir", lambda: tmp_path)
    monkeypatch.setattr(project_config, "_CONFIG_CACHE", None)
    monkeypatch.setattr(settings, "resolve_booley_toml", lambda _root: path)
    return path


@pytest.mark.parametrize(
    "raw,match",
    [
        (False, "table"),
        ([], "table"),
        ({"vivado": []}, "table"),
        ({"vivado": {"provisioning": True}}, "image.*host"),
        ({"vivado": {"environment": {}}}, "unknown key"),
    ],
)
def test_invalid_values_are_rejected(raw, match):
    with pytest.raises(eda.EdaConfigError, match=match):
        eda.parse_eda_config(raw)


@pytest.mark.parametrize("value", [[], {}])
def test_unhashable_provisioning_preserves_existing_exception(value):
    with pytest.raises(TypeError):
        eda.parse_eda_config({"vivado": {"provisioning": value}})


def test_eda_loader_absent_and_malformed_documents(document):
    assert eda.load_eda_config(document.parent) == {}
    document.write_text("[broken")
    with pytest.raises(eda.EdaConfigError, match="cannot read"):
        eda.load_eda_config(document.parent)


def test_readers_preserve_different_malformed_document_behavior(document, caplog):
    document.write_text("[broken")
    assert settings._load_booley_toml(document.parent) == {}
    assert "Failed to load" in caplog.text
    with pytest.raises(tomllib.TOMLDecodeError):
        project_config._load_config()


@pytest.mark.parametrize("reader", ["eda", "settings", "project"])
def test_migration_diagnostic_precedes_invalid_eda(document, reader):
    document.write_text('[flows.sim]\nbackend = "none"\n[eda.unknown]\n')
    with pytest.raises(eda.EdaConfigError, match="enabled = false"):
        if reader == "eda":
            eda.load_eda_config(document.parent)
        elif reader == "settings":
            settings._load_booley_toml(document.parent)
        else:
            project_config._load_config()


def test_project_config_parses_only_on_first_access_and_caches(document, monkeypatch):
    document.write_text('[project]\nname = "first"\n[eda.vivado]\nprovisioning = "host"\n')
    calls = []
    parse = eda.parse_eda_config

    def observe(raw):
        calls.append(raw)
        return parse(raw)

    monkeypatch.setattr(eda, "parse_eda_config", observe)
    assert calls == []
    assert project_config._get("PROJECT_NAME") == "first"
    document.write_text('[project]\nname = "second"\n')
    assert project_config._get("PROJECT_NAME") == "first"
    assert len(calls) == 1


def test_config_imports_do_not_open_project_documents():
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import booley  # Version attribution reads distribution/source metadata.
def no_open(*args, **kwargs):
    raise AssertionError("configuration import opened a file")
Path.open = no_open
from booley.config import settings, project_config
from booley.config import eda
assert project_config._CONFIG_CACHE is None
"""
    result = run_bounded_child([sys.executable, "-c", script, str(source)], timeout=20)
    assert result.returncode == 0, result.stderr


def test_host_policy_is_only_applied_by_eda_loaders(document, monkeypatch):
    from booley import eda as public_eda
    from booley.eda.provisioning import configuration

    document.write_text('[eda.vivado]\nprovisioning = "host"\n')
    # Patch only the policy module's platform object, not Python's global platform.
    from types import SimpleNamespace

    monkeypatch.setattr(configuration, "sys", SimpleNamespace(platform="win32"))
    assert eda.load_eda_config(document.parent) == {"vivado": eda.EdaConfig("vivado", "host")}
    assert settings._load_booley_toml(document.parent)["eda"]["vivado"]["provisioning"] == "host"
    assert project_config._get("PROJECT_NAME") == "rtl_project"
    for loader in (configuration.load_eda_config, public_eda.load_eda_config):
        with pytest.raises(eda.EdaConfigError, match="unsupported on Windows"):
            loader(document.parent)
