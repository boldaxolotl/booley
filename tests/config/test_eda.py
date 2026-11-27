"""Declarative EDA parsing preserves each configuration reader's contract."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
from tests.subprocess_budget import run_bounded_child

from booley.config import eda, project_config, settings
from booley.core.booley_dir import reset_cache


@pytest.fixture
def document(tmp_path, monkeypatch):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    monkeypatch.chdir(tmp_path)
    # private: no public function empties the memoized Project configuration
    monkeypatch.setattr(project_config, "_CONFIG_CACHE", None)
    reset_cache()
    yield booley_dir / "booley.toml"
    reset_cache()


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
    assert eda.load_eda_config(document.parents[1]) == {}
    document.write_text("[broken")
    with pytest.raises(eda.EdaConfigError, match="cannot read"):
        eda.load_eda_config(document.parents[1])


def test_readers_preserve_different_malformed_document_behavior(document, caplog):
    document.write_text("[broken")
    assert settings.load_submodule_config(document.parents[1]) == settings.SubmoduleConfig()
    assert "Failed to load" in caplog.text
    with pytest.raises(tomllib.TOMLDecodeError):
        project_config.project_name()


@pytest.mark.parametrize(
    "reader",
    [
        eda.load_eda_config,
        settings.load_submodule_config,
        lambda _root: project_config.project_name(),
    ],
    ids=["eda", "settings", "project"],
)
@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[flows.elab]\n[eda.unknown]\n", "elab-only"),
        ("[eda.unknown]\n", "unsupported EDA kind"),
    ],
    ids=["retired-key-first", "invalid-eda"],
)
def test_migration_diagnostic_precedes_invalid_eda(document, reader, text, match):
    document.write_text(text)
    with pytest.raises(eda.EdaConfigError, match=match):
        reader(document.parents[1])


def test_project_config_is_read_once_and_cached(document):
    document.write_text('[project]\nname = "first"\n')
    assert project_config.project_name() == "first"
    document.write_text('[project]\nname = "second"\n')
    assert project_config.project_name() == "first"


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
    result = run_bounded_child([sys.executable, "-c", script, str(source)])
    assert result.returncode == 0, result.stderr


def test_host_policy_is_only_applied_by_eda_loaders(document, monkeypatch):
    from booley import eda as public_eda
    from booley.eda.provisioning import configuration

    document.write_text('[eda.vivado]\nprovisioning = "host"\n')
    # Patch only the policy module's platform object, not Python's global platform.
    from types import SimpleNamespace

    monkeypatch.setattr(configuration, "sys", SimpleNamespace(platform="win32"))
    assert eda.load_eda_config(document.parents[1]) == {"vivado": eda.EdaConfig("vivado", "host")}
    assert settings.load_submodule_config(document.parents[1]) == settings.SubmoduleConfig()
    assert project_config.project_name() == "rtl_project"
    for loader in (configuration.load_eda_config, public_eda.load_eda_config):
        with pytest.raises(eda.EdaConfigError, match="unsupported on Windows") as raised:
            loader(document.parents[1])
        assert raised.value.path == document


@pytest.mark.parametrize(
    ("text", "line", "match"),
    [
        (b"[a]\n[a]\n", 2, "cannot read"),
        (b"x = [1,\n", 2 if sys.version_info >= (3, 14) else None, "cannot read"),
        (b"[flows.elab]\n", None, "retired"),
        pytest.param(
            b'[eda.vivado]\ninstallation = "x"\n',
            None,
            r"unknown key\(s\): installation",
            id="installation",
        ),
    ],
)
def test_refused_document_reports_its_path_and_parser_line(document, text, line, match):
    document.write_bytes(text)
    with pytest.raises(eda.EdaConfigError, match=match) as raised:
        eda.load_eda_config(document.parents[1])
    assert raised.value.path == document
    assert raised.value.line == line


def test_toml_error_line_reads_only_the_trailing_location_suffix():
    class Old(tomllib.TOMLDecodeError):
        lineno = None  # type: ignore[assignment]

    quoted = Old("Cannot declare ('at line 9, column 9',) twice (at line 4, column 2)")
    assert eda.toml_error_line(quoted) == 4
    assert eda.toml_error_line(Old("Unclosed array (at end of document)")) is None
