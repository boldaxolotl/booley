"""Doctor's developer probe starts no Booley MCP server and offers no tools (#2437)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.config import agent as agent_config
from booley.core.models import AgentResult
from booley.harness import developer_probe, doctor
from booley.runtime import _claude_backend, _codex_backend
from booley.runtime import agent as agent_mod
from booley.runtime import agent_config as backend_config
from booley.runtime.booley_dir import reset_cache


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A container-like process with a Project root, a private HOME and no Codex models cache."""
    for name in ("BOOLEY_PRIMARY_AUTH", "BOOLEY_PRIMARY_PROVIDER", "BOOLEY_AGENT_APP"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "project" / ".booley"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    monkeypatch.setattr(_codex_backend, "_inside_container", lambda: True)
    monkeypatch.setattr(_codex_backend, "_NESTED_HOMES", {})
    monkeypatch.setattr(doctor, "_agent_app_installed", lambda _provider: True)
    monkeypatch.setattr(developer_probe, "configure_agent_call", lambda params: params)
    agent_config.set_agent_settings(None)
    reset_cache()
    yield tmp_path / "project"
    agent_config.set_agent_settings(None)
    reset_cache()
    backend_config.set_backend_config(None)


def _project(root: Path, provider: str) -> None:
    (root / ".booley").mkdir(parents=True)
    (root / ".booley" / "booley.toml").write_text(
        f'[agent]\nprovider = "{provider}"\n', encoding="utf-8"
    )


@pytest.mark.parametrize("flavor", ["claude", "codex-native", "codex-gateway"])
def test_probe_call_has_no_booley_mcp_server_or_tools(sandbox, monkeypatch, flavor):
    _project(sandbox, "claude" if flavor == "claude" else "codex")
    if flavor == "codex-gateway":
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
        monkeypatch.setenv("BOOLEY_GATEWAY_URL", "https://gateway.example/v1")
        monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC_GATEWAY_TOKEN")
    seen = {}

    async def fake_call_agent(params):
        seen["params"] = params
        seen["cwd_existed"] = Path(params.cwd).is_dir()
        if flavor == "claude":
            seen["options"] = _claude_backend._build_sdk_options(params)
            return AgentResult(output="OK")

        async def capture(*cmd, env, **_kwargs):
            config = Path(env["CODEX_HOME"]) / "config.toml"
            seen["cmd"] = cmd
            seen["config_exists"] = config.exists()
            seen["config"] = config.read_text(encoding="utf-8") if config.exists() else ""
            catalog = Path(env["CODEX_HOME"]) / "model-catalog.json"
            seen["catalog"] = catalog.read_text(encoding="utf-8") if catalog.exists() else ""
            raise OSError("captured")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        auth_mode = agent_config.get_agent_settings().auth
        with pytest.raises(OSError, match="captured"):
            await _codex_backend._codex_spawn(["codex", "exec", "-"], params, auth_mode)
        return AgentResult(output="OK")

    monkeypatch.setattr(agent_mod, "call_agent", fake_call_agent)
    developer_probe.measure_developer_rss(
        sandbox, getrusage=lambda _who: SimpleNamespace(ru_maxrss=1)
    )

    params = seen["params"]
    assert params.nested_mcp_tools is None
    assert seen["cwd_existed"]
    assert sandbox not in Path(params.cwd).parents and not Path(params.cwd).exists()
    assert not list(sandbox.rglob(".text-only-codex"))
    if flavor == "claude":
        options = seen["options"]
        assert (options.mcp_servers, options.tools) == ({}, [])
        assert options.strict_mcp_config is True
        return
    # Native mode writes no config at all; gateway mode writes the provider only.
    assert seen["config_exists"] is (flavor == "codex-gateway")
    assert "mcp_servers" not in seen["config"]
    assert "features.shell_tool=false" in seen["cmd"]
    assert any(str(part).startswith("model_catalog_json=") for part in seen["cmd"])
    assert '"shell_type": "disabled"' in seen["catalog"]
    assert ("booley_gateway" in seen["config"]) is (flavor == "codex-gateway")


def test_probe_scratch_failure_is_a_probe_error(sandbox, monkeypatch):
    """The probe's fail-soft contract covers a temp directory that cannot be created."""

    def refuse(_prefix):
        raise OSError("read-only temp")

    monkeypatch.setattr(developer_probe, "owned_tempdir", refuse)
    with pytest.raises(developer_probe.ProbeError, match="read-only temp"):
        developer_probe.measure_developer_rss(sandbox)
