"""Shipped managed agent policies: web-tool isolation, no CLI self-update, no risky T3 Code tools."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import booley
from booley.core.models import AgentCallParams
from booley.harness import web_isolation
from booley.runtime import _claude_backend, _codex_backend, mcp_config


def _docker_data() -> Path:
    return Path(booley.__file__).parent / "data" / "docker"


def test_shipped_managed_policies_disable_provider_web_tools() -> None:
    data = _docker_data()
    codex = tomllib.loads((data / "codex-requirements.toml").read_text(encoding="utf-8"))
    claude = json.loads((data / "claude-managed-settings.json").read_text(encoding="utf-8"))

    assert codex["allowed_web_search_modes"] == []
    assert set(claude["permissions"]["deny"]) >= web_isolation.CLAUDE_WEB_CAPABILITIES


def test_shipped_claude_policy_denies_riskier_t3code_tools() -> None:
    # The maintainer's decision on #4782: a Claude thread started by T3 Code cannot schedule
    # tasks, link machines, transfer threads, manage T3 Code projects, use devices or ask for secrets.
    claude = json.loads(
        (_docker_data() / "claude-managed-settings.json").read_text(encoding="utf-8")
    )
    prefix = "mcp__t3-code__"

    denied = {
        rule.removeprefix(prefix)
        for rule in claude["permissions"]["deny"]
        if rule.startswith(prefix)
    }

    assert denied == {
        "schedule_task",
        "delete_scheduled_task",
        "list_scheduled_tasks",
        "run_scheduled_task_now",
        "t3_environment_link",
        "t3_environment_links",
        "t3_environment_unlink",
        "t3_thread_transfer",
        "t3_project_create",
        "t3_project_delete",
        "t3_project_clone",
        "device_open",
        "device_close",
        "device_list",
        "device_screenshot",
        "request_secret",
    }


def test_shipped_managed_policies_disable_agent_cli_self_update() -> None:
    # The CLIs live in a root-owned npm prefix, so any self-update must fail.
    data = _docker_data()
    codex = tomllib.loads((data / "codex-requirements.toml").read_text(encoding="utf-8"))
    claude = json.loads((data / "claude-managed-settings.json").read_text(encoding="utf-8"))

    assert claude.get("env", {}).get("DISABLE_UPDATES") == "1"
    assert codex.get("check_for_update_on_startup") is False


def test_shipped_t3code_policy_turns_off_remote_features_and_limits_drivers() -> None:
    policy = json.loads((_docker_data() / "t3code-policy.json").read_text(encoding="utf-8"))

    assert policy["limits"] == {
        "maxRuntimeMode": "auto",
        "connect": False,
        "telemetry": False,
        "updates": {"mode": "off"},
        "providers": {"allowedDrivers": ["claudeAgent", "codex"]},
    }


def test_sandbox_image_installs_both_managed_policies() -> None:
    dockerfile = (_docker_data() / "Dockerfile.base").read_text(encoding="utf-8")
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

    options = _claude_backend._build_sdk_options(params)

    assert options.disallowed_tools == ["Bash", "WebFetch", "WebSearch"]


def test_codex_backend_forces_web_search_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_codex_backend.shutil, "which", lambda _name: "/usr/bin/codex")

    cmd, schema = _codex_backend._codex_build_cmd("m", tmp_path, None, None, None)

    assert schema is None
    assert cmd[cmd.index("-c") : cmd.index("-c") + 2] == ["-c", 'web_search="disabled"']


def test_generated_codex_mcp_config_disables_web_search() -> None:
    parsed = tomllib.loads(mcp_config.generate_codex_config())

    assert parsed["web_search"] == "disabled"
