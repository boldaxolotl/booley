"""Text-only model boundary: no model-visible execution capabilities."""

import json
import os
import tomllib
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


def test_text_only_auth_links_real_login(tmp_path):
    """AC1: the text-only home links the real login instead of copying it."""
    from tests.conftest import require_symlinks

    require_symlinks(tmp_path)
    original = tmp_path / "original"
    original.mkdir()
    (original / "models_cache.json").write_text('{"models":[{"slug":"chosen"}]}')
    native = original / "auth.json"
    native.write_text('{"tokens":{"access_token":"SYNTHETIC"}}')
    root = tmp_path / "invocation"
    root.mkdir()
    params = AgentCallParams(prompt="Explain", model="chosen", cwd=root, text_only=True)
    _, child = prepare_codex_text_only(
        ["codex", "exec", "-"], params, {"CODEX_HOME": str(original)}
    )
    auth = Path(child["CODEX_HOME"]) / "auth.json"
    assert auth.is_symlink()
    assert auth.resolve() == native.resolve()
    assert [p for p in root.rglob("auth.json") if not p.is_symlink()] == []


def test_text_only_missing_exact_model_fails_closed(tmp_path):
    (tmp_path / "models_cache.json").write_text('{"models": []}')
    params = AgentCallParams(prompt="Analyze", model="missing", cwd=tmp_path, text_only=True)
    with pytest.raises(ValueError, match="exact model"):
        prepare_codex_text_only(["codex", "exec", "-"], params, {"CODEX_HOME": str(tmp_path)})


def test_text_only_codex_may_expose_one_scoped_nested_mcp_tool(tmp_path):
    cache = tmp_path / "original"
    cache.mkdir()
    (cache / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "chosen", "context_window": 200000}]})
    )
    root = tmp_path / "invocation"
    root.mkdir()
    params = AgentCallParams(
        prompt="Analyze",
        model="chosen",
        cwd=root,
        text_only=True,
        nested_mcp_tools=["coverage_evidence"],
        nested_mcp_env={"BOOLEY_COVERAGE_CAMPAIGN": "/reports/coverage.json"},
    )

    _, env = prepare_codex_text_only(["codex", "exec", "-"], params, {"CODEX_HOME": str(cache)})

    config = (Path(env["CODEX_HOME"]) / "config.toml").read_text()
    assert 'BOOLEY_NESTED_MCP_TOOLS = "coverage_evidence"' in config
    assert 'BOOLEY_COVERAGE_CAMPAIGN = "/reports/coverage.json"' in config
    assert "mcp_servers.booley" in config


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


@pytest.mark.parametrize("preference", [None, True, False])
@pytest.mark.parametrize("toml11", [False, True])
def test_text_only_regeneration_preserves_destination_preference(
    tmp_path, preference, toml11, caplog
):
    original = tmp_path / "original"
    original.mkdir()
    (original / "models_cache.json").write_text('{"models":[{"slug":"chosen"}]}')
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    params = AgentCallParams(
        prompt="Analyze", model="chosen", cwd=invocation, text_only=True, nested_mcp_tools=["lint"]
    )
    _, env = prepare_codex_text_only(["codex", "exec", "-"], params, {"CODEX_HOME": str(original)})
    config = Path(env["CODEX_HOME"]) / "config.toml"
    assert tomllib.loads(config.read_text())["suppress_unstable_features_warning"] is True
    if preference is not None or toml11:
        choice = (
            ""
            if preference is None
            else f"suppress_unstable_features_warning={str(preference).lower()}\n"
        )
        config.write_text(choice + ("features={\n mcp_2026_07_28=true,\n}\n" if toml11 else ""))
    prepare_codex_text_only(["codex", "exec", "-"], params, {"CODEX_HOME": str(original)})
    parsed = tomllib.loads(config.read_text())
    assert "malformed" not in caplog.text
    assert parsed["suppress_unstable_features_warning"] is (
        True if preference is None else preference
    )
    assert parsed["mcp_servers"]["booley"]["env"]["BOOLEY_NESTED_MCP_TOOLS"] == "lint"
    params.nested_mcp_tools = None
    before = config.read_bytes()
    prepare_codex_text_only(["codex", "exec", "-"], params, {"CODEX_HOME": str(original)})
    assert config.read_bytes() == before


def test_text_only_reused_output_clears_disallowed_or_disappeared_auth(tmp_path):
    from tests.conftest import require_symlinks

    require_symlinks(tmp_path)
    original = tmp_path / "original"
    original.mkdir()
    (original / "models_cache.json").write_text('{"models":[{"slug":"chosen"}]}')
    native = original / "auth.json"
    native.write_text('{"tokens":{"access_token":"SYNTHETIC"}}')
    root = tmp_path / "invocation"
    root.mkdir()
    params = AgentCallParams(prompt="Explain", model="chosen", cwd=root, text_only=True)
    command = ["codex", "exec", "-"]
    environment = {"CODEX_HOME": str(original)}
    _, child = prepare_codex_text_only(command, params, environment, auth_mode="subscription")
    copied = Path(child["CODEX_HOME"]) / "auth.json"
    assert copied.exists()
    prepare_codex_text_only(command, params, environment, auth_mode="api_key")
    assert not os.path.lexists(copied)
    prepare_codex_text_only(command, params, environment, auth_mode="subscription")
    native.unlink()
    prepare_codex_text_only(command, params, environment, auth_mode="subscription")
    assert not os.path.lexists(copied)


def test_text_only_gateway_seeds_pinned_model_catalog_entry(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
    cache = original / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gw-model",
                        "base_instructions": "NATIVE",
                        "apply_patch_tool_type": "freeform",
                        "shell_type": "shell_command",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "invocation"
    root.mkdir()
    environment = {
        "CODEX_HOME": str(original),
        "BOOLEY_GATEWAY_URL": "https://gateway.example/v1",
        "BOOLEY_GATEWAY_TOKEN": "SYNTHETIC_GATEWAY_TOKEN",
    }
    params = AgentCallParams(
        prompt="Analyze",
        model="gw-model",
        cwd=root,
        text_only=True,
        reasoning_effort="low",
        nested_mcp_tools=["coverage_evidence"],
    )

    def prepare():
        _, env = prepare_codex_text_only(
            ["codex", "exec", "-"], params, environment, auth_mode="gateway"
        )
        return Path(env["CODEX_HOME"])

    private = prepare()
    models = json.loads((private / "model-catalog.json").read_text(encoding="utf-8"))["models"]
    (entry,) = models
    assert entry["slug"] == "gw-model"
    assert entry["base_instructions"] not in ("", "NATIVE")
    assert entry["shell_type"] == "disabled"
    assert entry["apply_patch_tool_type"] is None
    assert entry["experimental_supported_tools"] == []
    assert entry["default_reasoning_level"] == "low"
    config = tomllib.loads((private / "config.toml").read_text(encoding="utf-8"))
    assert config["model_provider"] == "booley_gateway"
    provider = config["model_providers"]["booley_gateway"]
    assert provider["base_url"] == "https://gateway.example/v1"
    assert provider["auth"]["cwd"] == str(private)
    assert not (private / "auth.json").exists()
    assert not any(
        "SYNTHETIC_GATEWAY_TOKEN" in path.read_text(encoding="utf-8", errors="replace")
        for path in private.rglob("*")
        if path.is_file()
    )

    # The seeded entry does not depend on the Codex models cache.
    cache.unlink()
    assert prepare() == private
    assert json.loads((private / "model-catalog.json").read_text(encoding="utf-8")) == {
        "models": models
    }

    params.nested_mcp_tools = None
    with pytest.raises(ValueError, match="nested MCP tool allowlist"):
        prepare()
