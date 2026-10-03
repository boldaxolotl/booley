"""Project visibility settings use capability names rather than MCP terminology."""

from pathlib import Path

import pytest

from booley.audit.project_schema import audit_known_tables
from booley.harness.developer import _load_endpoint_config as developer_config
from booley.harness.ticket_preflight import _load_endpoint_config as preflight_config
from booley.mcp.endpoint_config import get_endpoint_config
from booley.mcp.registry import discover_mcp_tools
from booley.ticket_board.validation import _read_endpoint_config as ticket_config


@pytest.mark.parametrize("loader", [get_endpoint_config, developer_config, preflight_config])
def test_specialist_visibility_is_shared_across_execution_modes(tmp_path, monkeypatch, loader):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    project = tmp_path / ".booley_project"
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
    assert {"sim", "reviewer", "submit_run_report"} <= names
    assert ticket_config(project / "booley.toml") == (specialists, flows)


@pytest.mark.parametrize("table", ["mcp_tools", "tools"])
@pytest.mark.parametrize("loader", [get_endpoint_config, developer_config, preflight_config])
def test_retired_visibility_tables_fail_with_migration_guidance(
    tmp_path, monkeypatch, loader, table
):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    project = tmp_path / ".booley_project"
    project.mkdir()
    config = project / "booley.toml"
    config.write_text(f"[{table}.reviewer]\nenabled = false\n", encoding="utf-8")

    for read in (lambda: loader(tmp_path), lambda: ticket_config(config)):
        with pytest.raises(ValueError, match=r"\[specialists\.\*\]"):
            read()


def test_project_audit_rejects_retired_mcp_table():
    audit = audit_known_tables({"mcp_tools": {"reviewer": {"enabled": False}}})

    assert not audit.is_valid
    assert "[specialists.*]" in audit.findings[0].fix


def test_specialist_settings_cannot_disable_protocol_utilities(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    project = tmp_path / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text(
        "[specialists.submit_run_report]\nenabled = false\n", encoding="utf-8"
    )
    specialists, flows = get_endpoint_config(tmp_path)
    endpoints = discover_mcp_tools(specialist_config=specialists, flow_config=flows)

    assert "submit_run_report" in {endpoint.name for endpoint in endpoints}
