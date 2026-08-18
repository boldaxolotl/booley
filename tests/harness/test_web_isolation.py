"""Provider-hosted web MCP tools must not bypass the Session Runtime boundary."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import booley
from booley.harness import web_isolation
from booley.harness.models import AgentCallParams
from booley.runtime import _claude_backend, _codex_backend, mcp_config


def _docker_data() -> Path:
    return Path(booley.__file__).parent / "data" / "docker"


def test_shipped_managed_policies_disable_provider_web_tools() -> None:
    data = _docker_data()
    codex = tomllib.loads((data / "codex-requirements.toml").read_text(encoding="utf-8"))
    claude = json.loads((data / "claude-managed-settings.json").read_text(encoding="utf-8"))

    assert codex["allowed_web_search_modes"] == []
    assert set(claude["permissions"]["deny"]) >= web_isolation.CLAUDE_WEB_CAPABILITIES


def test_sandbox_image_installs_both_managed_policies() -> None:
    dockerfile = (_docker_data() / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (_docker_data().parents[3] / ".dockerignore").read_text(encoding="utf-8")

    assert "codex-requirements.toml /etc/codex/requirements.toml" in dockerfile
    assert "claude-managed-settings.json /etc/claude-code/managed-settings.json" in dockerfile
    assert "!src/booley/data/docker/codex-requirements.toml" in dockerignore
    assert "!src/booley/data/docker/claude-managed-settings.json" in dockerignore


def test_policy_probe_accepts_complete_policy(tmp_path: Path) -> None:
    codex_path = tmp_path / web_isolation.CODEX_POLICY
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text("allowed_web_search_modes = []\n", encoding="utf-8")
    claude_path = tmp_path / web_isolation.CLAUDE_POLICY
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text(
        json.dumps({"permissions": {"deny": ["WebFetch", "WebSearch"]}}),
        encoding="utf-8",
    )

    assert web_isolation.policy_error(tmp_path) is None


def test_policy_probe_rejects_each_missing_boundary(tmp_path: Path) -> None:
    assert "Codex web policy unreadable" in (web_isolation.policy_error(tmp_path) or "")

    codex_path = tmp_path / web_isolation.CODEX_POLICY
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text('allowed_web_search_modes = ["live"]\n', encoding="utf-8")
    assert "not forced disabled" in (web_isolation.policy_error(tmp_path) or "")

    codex_path.write_text("allowed_web_search_modes = []\n", encoding="utf-8")
    assert "Claude web policy unreadable" in (web_isolation.policy_error(tmp_path) or "")


def test_claude_backend_always_denies_provider_web_tools(tmp_path: Path) -> None:
    params = AgentCallParams(
        prompt="p",
        model="m",
        cwd=tmp_path,
        disallowed_agent_capabilities=["Bash"],
    )

    options = _claude_backend._build_sdk_options(params, None)

    assert options.disallowed_agent_capabilities == ["Bash", "WebFetch", "WebSearch"]


def test_codex_backend_forces_web_search_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_codex_backend.shutil, "which", lambda _name: "/usr/bin/codex")

    cmd, schema = _codex_backend._codex_build_cmd("m", tmp_path, None, None, None)

    assert schema is None
    assert cmd[cmd.index("-c") : cmd.index("-c") + 2] == ["-c", 'web_search="disabled"']


def test_generated_codex_mcp_config_disables_web_search() -> None:
    parsed = tomllib.loads(mcp_config.generate_codex_config())

    assert parsed["web_search"] == "disabled"
