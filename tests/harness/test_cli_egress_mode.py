"""Sandbox egress mode (`BOOLEY_EGRESS`) at the CLI entry point (#2132)."""

from __future__ import annotations

import os
import sys

import pytest

from booley.harness import booley as cli
from booley.runtime import mcp_config
from booley.runtime.booley_dir import reset_cache

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


@pytest.fixture
def sandbox_cli(tmp_path, monkeypatch):
    """A Project inside a Sandbox with a clean proxy and egress environment."""
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (project / ".booley").mkdir()
    monkeypatch.chdir(project)
    for var in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_EGRESS", "NO_PROXY", *_PROXY_VARS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setattr(cli, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    reset_cache()
    seen: list[dict[str, dict[str, str | None]]] = []

    def record(_args, _root) -> int:
        """Record what a spawned agent and a nested MCP server would inherit."""
        nested = mcp_config._codex_server_env(None, None)
        seen.append(
            {
                "process": {var: os.environ.get(var) for var in _PROXY_VARS},
                "nested_mcp": {var: nested.get(var) for var in _PROXY_VARS},
            }
        )
        return 0

    for command in ("targets", "doctor"):
        monkeypatch.setitem(cli._EARLY_COMMANDS, command, record)
    return seen


def test_unproxied_command_and_agents_inherit_no_proxy(sandbox_cli, monkeypatch):
    monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
    monkeypatch.setattr(sys, "argv", ["booley", "targets"])
    assert cli.main() == 0
    assert sandbox_cli == [
        {
            "process": dict.fromkeys(_PROXY_VARS),
            "nested_mcp": dict.fromkeys(_PROXY_VARS),
        }
    ]


def test_invalid_mode_refuses_command(sandbox_cli, monkeypatch, capsys):
    monkeypatch.setenv("BOOLEY_EGRESS", "bogus")
    monkeypatch.setattr(sys, "argv", ["booley", "targets"])
    assert cli.main() == 2
    error = capsys.readouterr().err
    assert error.startswith("ERROR:")
    assert "`proxy`" in error and "`unproxied`" in error
    assert sandbox_cli == []


def test_invalid_mode_still_runs_doctor(sandbox_cli, monkeypatch):
    monkeypatch.setenv("BOOLEY_EGRESS", "bogus")
    monkeypatch.setattr(sys, "argv", ["booley", "doctor"])
    assert cli.main() == 0
    assert sandbox_cli[0]["process"] == dict.fromkeys(_PROXY_VARS)
