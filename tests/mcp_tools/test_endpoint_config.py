"""Project visibility settings use capability names rather than MCP terminology."""

from pathlib import Path

import pytest

from booley.audit.project_schema import audit_known_tables
from booley.harness.setup import readiness
from booley.mcp.endpoint_config import get_endpoint_config
from booley.mcp.endpoint_config import get_endpoint_config as developer_config
from booley.mcp.endpoint_validation import _load_endpoint_config as endpoint_config
from booley.mcp.registry import discover_mcp_tools


@pytest.mark.parametrize("loader", [get_endpoint_config, developer_config, endpoint_config])
def test_specialist_visibility_is_shared_across_execution_modes(tmp_path, monkeypatch, loader):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text(
        "[specialists.coverage_analyst]\nenabled = false\n[flows.lint]\nenabled = false\n",
        encoding="utf-8",
    )

    specialists, flows = loader(tmp_path)
    endpoints = discover_mcp_tools(specialist_config=specialists, flow_config=flows)
    names = {endpoint.name for endpoint in endpoints}

    assert "coverage_analyst" not in names
    assert "lint" not in names
    assert {"sim", "reviewer"} <= names


@pytest.mark.parametrize("table", ["mcp_tools", "tools"])
@pytest.mark.parametrize("loader", [get_endpoint_config, developer_config, endpoint_config])
def test_retired_visibility_tables_fail_with_migration_guidance(
    tmp_path, monkeypatch, loader, table
):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / ".booley"
    project.mkdir()
    config = project / "booley.toml"
    config.write_text(f"[{table}.reviewer]\nenabled = false\n", encoding="utf-8")

    for read in (lambda: loader(tmp_path),):
        with pytest.raises(ValueError, match=r"\[specialists\.\*\]"):
            read()


def test_project_audit_rejects_retired_mcp_table():
    audit = audit_known_tables({"mcp_tools": {"reviewer": {"enabled": False}}})

    assert not audit.is_valid
    assert "[specialists.*]" in audit.findings[0].fix


def test_specialist_settings_cannot_disable_protocol_utilities(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text(
        "[specialists.submit_run_report]\nenabled = false\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"submit_run_report.*not a discovered Specialist"):
        get_endpoint_config(tmp_path)


def test_readiness_does_not_accept_retired_endpoint_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / ".booley"
    project.mkdir()
    config = "[project]\nname = 'fixture'\n" + "".join(
        f"[flows.{name}]\nenabled = false\n" for name in ("sim", "lint", "synth")
    )
    (project / "booley.toml").write_text(config + "[mcp_tools.reviewer]\nenabled = false\n")

    loaded = readiness.load_project(tmp_path)

    assert loaded.project is None
    assert any("[mcp_tools] is retired" in finding.message for finding in loaded.report.findings)


@pytest.mark.parametrize(
    "config",
    [
        "specialists = false\n",
        "[specialists]\nreviewer = false\n",
        '[specialists.reviewer]\nenabled = "false"\n',
        "[specialists.reviewer]\nenabled = 0\n",
        "[specialists.reveiwer]\nenabled = false\n",
    ],
)
@pytest.mark.parametrize("loader", [get_endpoint_config, developer_config, endpoint_config])
def test_invalid_specialist_settings_fail_closed(tmp_path, monkeypatch, config, loader):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text(config)

    with pytest.raises(ValueError, match="specialists"):
        loader(tmp_path)


@pytest.mark.parametrize("loader", [get_endpoint_config, developer_config, endpoint_config])
def test_malformed_toml_fails_closed_in_every_endpoint_loader(tmp_path, monkeypatch, loader):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / ".booley"
    project.mkdir()
    config = project / "booley.toml"
    config.write_text("[specialists.reviewer\nenabled = false\n")

    for read in (lambda: loader(tmp_path),):
        with pytest.raises(ValueError, match=r"booley\.toml"):
            read()


def test_misspelled_specialist_name_is_rejected():
    with pytest.raises(ValueError, match=r"reveiwer.*not a discovered Specialist"):
        discover_mcp_tools(specialist_config={"reveiwer": {"enabled": False}})


def test_readiness_rejects_misspelled_specialist_name(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / ".booley"
    project.mkdir()
    config = "[project]\nname = 'fixture'\n" + "".join(
        f"[flows.{name}]\nenabled = false\n" for name in ("sim", "lint", "synth")
    )
    (project / "booley.toml").write_text(config + "[specialists.reveiwer]\nenabled = false\n")

    loaded = readiness.load_project(tmp_path)

    assert loaded.project is None
    assert any("reveiwer" in item.message for item in loaded.report.findings)


def test_cli_renders_retirement_error_without_traceback(tmp_path, monkeypatch, capsys):
    from booley.harness import booley as cli

    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text("[mcp_tools.reviewer]\nenabled = false\n")
    args = cli._build_parser().parse_args(["specialist"])
    monkeypatch.setattr(cli, "_parse_cli", lambda: args)
    monkeypatch.setattr(cli, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(cli, "_reject_source_project_command", lambda *_args: None)
    monkeypatch.setattr(cli, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", lambda: False)

    assert cli.main() == 2
    output = capsys.readouterr().err
    assert "[specialists.*]" in output
    assert "Traceback" not in output


def test_retired_table_cannot_be_masked_by_new_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text(
        "[mcp_tools.reviewer]\nenabled = false\n[specialists.reviewer]\nenabled = true\n"
    )

    with pytest.raises(ValueError, match="is retired"):
        get_endpoint_config(tmp_path)


def test_mcp_entry_point_renders_catalog_configuration_error(tmp_path, monkeypatch, capsys):
    import sys

    from booley.mcp import server

    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text("[mcp_tools.reviewer]\nenabled = false\n")

    async def start_catalog():
        server._discover_booley_mcp_tools()

    monkeypatch.setattr(server, "_get_endpoint_config", lambda: get_endpoint_config(tmp_path))
    monkeypatch.setattr(server, "_main", start_catalog)
    monkeypatch.setattr(server.runtime_context, "container_only_error", lambda _command: None)
    monkeypatch.setattr(server.runtime_context, "ensure_proxy_env", lambda: False)
    monkeypatch.setattr(sys, "argv", ["booley-mcp"])

    with pytest.raises(SystemExit) as excinfo:
        server.main()

    assert excinfo.value.code == 2
    output = capsys.readouterr().err
    assert "[specialists.*]" in output
    assert "Traceback" not in output


def test_endpoint_validation_reads_the_resolved_booley_directory(tmp_path):
    from booley.mcp.endpoint_validation import validate_custom_endpoints_and_criteria

    project = tmp_path / "project-data"
    project.mkdir()
    (tmp_path / "booley.toml").write_text('[project]\ndir = "project-data"\n')
    (project / "booley.toml").write_text("[specialists.reveiwer]\nenabled = false\n")
    with pytest.raises(ValueError, match="reveiwer"):
        validate_custom_endpoints_and_criteria(tmp_path)


@pytest.mark.parametrize("names", [(), ("local",)])
def test_supplied_specialist_inventory_avoids_discovery(tmp_path, monkeypatch, names):
    from booley.mcp import registry
    from booley.mcp.endpoint_config import read_endpoint_config

    def unexpected(**kwargs):
        pytest.fail("supplied inventory must avoid discovery")

    monkeypatch.setattr(registry, "discover_mcp_tools", unexpected)
    path = tmp_path / "booley.toml"
    assert read_endpoint_config(path, specialist_names=names) == ({}, {})
    path.write_text("[specialists.local]\nenabled = false\n")
    if names:
        assert read_endpoint_config(path, specialist_names=names)[0] == {
            "local": {"enabled": False}
        }
    else:
        with pytest.raises(ValueError, match="local"):
            read_endpoint_config(path, specialist_names=names)


@pytest.mark.parametrize("content", ["[tools]\n", "[specialists]\nlocal = false\n", "[broken"])
def test_supplied_inventory_preserves_invalid_config_policy(tmp_path, content):
    from booley.mcp.endpoint_config import EndpointConfigError, read_endpoint_config

    path = tmp_path / "booley.toml"
    path.write_text(content)
    with pytest.raises(EndpointConfigError):
        read_endpoint_config(path, specialist_names=("local",))


def test_supplied_inventory_preserves_unreadable_config_policy(tmp_path):
    from booley.mcp.endpoint_config import EndpointConfigError, read_endpoint_config

    with pytest.raises(EndpointConfigError, match="Cannot read"):
        read_endpoint_config(tmp_path, specialist_names=())
