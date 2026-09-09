"""Text-only model boundary: no model-visible execution capabilities."""

import json
from pathlib import Path

import pytest

from booley.core.models import AgentCallParams
from booley.runtime.text_only_agent import prepare_codex_text_only


def test_text_only_catalog_removes_model_supplied_tools_and_ambient_servers(tmp_path):
    cache = tmp_path / "original"
    cache.mkdir()
    (cache / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "chosen",
                        "shell_type": "shell_command",
                        "apply_patch_tool_type": "freeform",
                        "experimental_supported_tools": ["clock"],
                        "tool_mode": "code_mode",
                        "node_repl_disabled": False,
                        "supports_search_tool": True,
                        "context_window": 200000,
                    }
                ]
            }
        )
    )
    root = tmp_path / "invocation"
    root.mkdir()
    params = AgentCallParams(prompt="Analyze", model="chosen", cwd=root, text_only=True)
    command, env = prepare_codex_text_only(
        ["codex", "exec", "-"], params, {"CODEX_HOME": str(cache)}
    )
    catalog_path = Path(env["CODEX_HOME"]) / "model-catalog.json"
    model = json.loads(catalog_path.read_text())["models"][0]
    assert model["apply_patch_tool_type"] is None
    assert model["shell_type"] == "disabled"
    assert model["experimental_supported_tools"] == []
    assert model["context_window"] == 200000
    assert "features.shell_tool=false" in command
    assert "agents.enabled=false" in command
    assert "features.multi_agent_v2=false" in command
    assert Path(env["CODEX_HOME"]) != cache
    assert not (Path(env["CODEX_HOME"]) / "config.toml").exists()


def test_text_only_missing_exact_model_fails_closed(tmp_path):
    (tmp_path / "models_cache.json").write_text('{"models": []}')
    params = AgentCallParams(prompt="Analyze", model="missing", cwd=tmp_path, text_only=True)
    with pytest.raises(ValueError, match="exact model"):
        prepare_codex_text_only(["codex", "exec", "-"], params, {"CODEX_HOME": str(tmp_path)})


@pytest.mark.asyncio
async def test_claude_text_only_model_receives_no_tools_mcp_or_project_settings(
    tmp_path, monkeypatch
):
    from booley.runtime import _claude_backend
    from booley.runtime.agent_backend import ClaudeSDKBackend
    from tests.harness.test_agent_backend import _capturing_successful_query

    options, query = _capturing_successful_query()
    monkeypatch.setattr(_claude_backend, "query", query)
    await ClaudeSDKBackend().call(
        AgentCallParams(prompt="Explain", model="sonnet", cwd=tmp_path, text_only=True)
    )
    assert options[0].tools == []
    assert options[0].mcp_servers == {}
    assert options[0].setting_sources == []
