"""Tests for developer agent launch plumbing.

ADR 0028: the developer launches as a NATIVE in-container agent session
on the configured backend — no Docker spawn, no runtime mounts, no path
remapping. The BOOLEY_* env contract is exported into ``os.environ`` so the
agent CLI and its stdio MCP server inherit it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from booley.config import settings as harness_config
from booley.harness import developer
from booley.harness.models import AgentCallParams


@contextlib.contextmanager
def _env_guard():
    """Snapshot/restore os.environ around code that exports env permanently."""
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


class _DummyBackend:
    """Native backend stub capturing the call and the env DURING the call."""

    name = "Dummy"

    def __init__(self) -> None:
        self.captured: dict = {}

    async def call(self, params, **kwargs):
        self.captured["params"] = params
        self.captured["kwargs"] = kwargs
        # Snapshot env mid-call: the agent CLI + stdio MCP server inherit it.
        self.captured["env"] = {k: v for k, v in os.environ.items() if k.startswith("BOOLEY_")}
        return "ok"


class _DummyConfig:
    """Minimal backend config for exercising launch env construction."""

    auth = "subscription"
    provider = "claude"

    def __init__(self) -> None:
        self.active_backend = _DummyBackend()

    def model_for_tier(self, _tier: str) -> str:
        return "dummy-model"

    def model_for_role(self, _role: str, _tier: str) -> str:
        return "dummy-model"

    def effort_for_tier(self, _tier: str) -> str:
        return "medium"


def test_launch_developer_agent_native_env_and_params(tmp_path, monkeypatch):
    cfg = _DummyConfig()
    monkeypatch.setattr(harness_config, "get_backend_config", lambda: cfg)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)

    logs_dir = tmp_path / "logs"
    state_path = logs_dir / ".runtime" / "booley_state.json"
    (tmp_path / ".booley_project").mkdir()

    def _on_event(_ev):
        pass

    with _env_guard():
        result = asyncio.run(
            developer._launch_developer_agent(
                "prompt",
                system_prompt="system",
                cwd=tmp_path,
                slug="my-ticket",
                ticket_type="feature",
                execution_id="execution-7",
                state_path=state_path,
                logs_dir=logs_dir,
                mcp_tools=["lint", "sim"],
                project_root=tmp_path,
                on_event=_on_event,
            )
        )

    assert result == "ok"
    captured = cfg.active_backend.captured

    # Native call: (params, on_event) only — no env/runtime_mounts/mcp_tools.
    assert captured["kwargs"] == {"on_event": _on_event}

    # Env exported into os.environ during the call — REAL Runner paths.
    env = captured["env"]
    assert env["BOOLEY_SLUG"] == "my-ticket"
    assert env["BOOLEY_WORKTREE"] == str(tmp_path)
    assert env["BOOLEY_PAIRED_PROJECT_REPOSITORY"] == ""
    assert env["BOOLEY_TICKET_TYPE"] == "feature"
    assert env["BOOLEY_TICKET_FILE"] == str(logs_dir / "ticket.md")
    assert env["BOOLEY_LOGS_DIR"] == str(logs_dir)
    assert env["BOOLEY_RUNTIME_DIR"] == str(logs_dir / ".runtime")
    assert env["BOOLEY_STATE_FILE"] == str(state_path)
    assert env["BOOLEY_AGENT_ROLE"] == "ticket"
    assert env["BOOLEY_EXECUTION_ID"] == "execution-7"
    assert env["BOOLEY_MCP_TOOLS"] == "lint,sim"
    assert env["BOOLEY_PRIMARY_PROVIDER"] == "claude"
    assert env["BOOLEY_PRIMARY_AUTH"] == "subscription"
    assert env["BOOLEY_PROJECT_DIR"] == str(tmp_path / ".booley_project")
    assert env["BOOLEY_CONTROL_PROJECT_ROOT"] == str(tmp_path)
    # Nested markers must never leak onto the developer's server.
    assert "BOOLEY_NESTED_AGENT" not in env

    params = captured["params"]
    assert params.prompt == "prompt"
    assert params.system_prompt == "system"
    assert params.label == "developer"
    # Resolved from the live backend config (so a [models.roles] developer pin
    # applies), not from the MODEL_MAP module global.
    assert params.model == cfg.model_for_role("developer", "heavy")
    assert params.reasoning_effort == "medium"
    assert params.nested_mcp_tools is None
    assert params.developer_mcp_tools == ["lint", "sim"]


def test_launch_developer_agent_env_dir_override(tmp_path, monkeypatch):
    """BOOLEY_PROJECT_DIR from the devcontainer env wins over discovery."""
    cfg = _DummyConfig()
    monkeypatch.setattr(harness_config, "get_backend_config", lambda: cfg)

    project_dir = tmp_path / "mounted-project"
    project_dir.mkdir()

    with _env_guard():
        os.environ["BOOLEY_PROJECT_DIR"] = str(project_dir)
        asyncio.run(
            developer._launch_developer_agent(
                "prompt",
                system_prompt="system",
                cwd=tmp_path,
                slug="my-ticket",
                state_path=tmp_path / "booley_state.json",
                logs_dir=tmp_path / "logs",
                project_root=tmp_path,
            )
        )

    env = cfg.active_backend.captured["env"]
    assert env["BOOLEY_PROJECT_DIR"] == str(project_dir)
    # No mcp_tools passed -> no explicit MCP allowlist exported.
    assert "BOOLEY_MCP_TOOLS" not in env
    assert cfg.active_backend.captured["params"].developer_mcp_tools is None


def test_launch_restores_control_plane_project_dir_after_ticket_local_call(tmp_path, monkeypatch):
    cfg = _DummyConfig()
    monkeypatch.setattr(harness_config, "get_backend_config", lambda: cfg)

    control_project_dir = tmp_path / "control-project"
    control_project_dir.mkdir()
    checkout = tmp_path / "ticket-checkout"
    ticket_project_dir = checkout / ".booley_project"
    ticket_project_dir.mkdir(parents=True)

    with _env_guard():
        os.environ["BOOLEY_PROJECT_DIR"] = str(control_project_dir)
        os.environ["BOOLEY_CONTROL_PROJECT_ROOT"] = str(tmp_path / "outer-project")
        asyncio.run(
            developer._launch_developer_agent(
                "prompt",
                system_prompt="system",
                cwd=checkout,
                slug="my-ticket",
                state_path=tmp_path / "booley_state.json",
                logs_dir=control_project_dir / "tickets" / "logs" / "my-ticket",
                project_root=tmp_path,
            )
        )

        assert os.environ["BOOLEY_PROJECT_DIR"] == str(control_project_dir)
        assert os.environ["BOOLEY_CONTROL_PROJECT_ROOT"] == str(tmp_path / "outer-project")

    assert cfg.active_backend.captured["env"]["BOOLEY_PROJECT_DIR"] == str(ticket_project_dir)
    assert cfg.active_backend.captured["env"]["BOOLEY_CONTROL_PROJECT_ROOT"] == str(tmp_path)


def test_launch_restores_every_overridden_environment_key(tmp_path, monkeypatch):
    cfg = _DummyConfig()
    monkeypatch.setattr(harness_config, "get_backend_config", lambda: cfg)
    monkeypatch.setenv("BOOLEY_SLUG", "outer-session")
    monkeypatch.delenv("BOOLEY_AGENT_ROLE", raising=False)

    asyncio.run(
        developer._launch_developer_agent(
            "prompt",
            system_prompt="system",
            cwd=tmp_path,
            slug="ticket-session",
            state_path=tmp_path / "booley_state.json",
            logs_dir=tmp_path / "logs",
        )
    )

    assert os.environ["BOOLEY_SLUG"] == "outer-session"
    assert "BOOLEY_AGENT_ROLE" not in os.environ


def test_launch_passes_developer_budget_to_backend(tmp_path, monkeypatch):
    cfg = _DummyConfig()
    monkeypatch.setattr(harness_config, "get_backend_config", lambda: cfg)
    budget = object()

    with _env_guard():
        asyncio.run(
            developer._launch_developer_agent(
                "prompt",
                system_prompt="system",
                cwd=tmp_path,
                slug="my-ticket",
                state_path=tmp_path / "booley_state.json",
                logs_dir=tmp_path / "logs",
                developer_budget=budget,
            )
        )

    assert cfg.active_backend.captured["kwargs"]["developer_budget"] is budget


def test_developer_codex_home_config(tmp_path, monkeypatch):
    """Codex developer HOME bakes BOOLEY_* env + allowlist, no nested markers."""
    from booley.runtime import _codex_backend as cb

    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("BOOLEY_SLUG", "my-ticket")
    monkeypatch.setenv("BOOLEY_CONTROL_PROJECT_ROOT", "/project/control-root")
    monkeypatch.setenv("BOOLEY_WORKSPACE_SLUG", "my-project")
    monkeypatch.setenv("BOOLEY_AGENT_ROLE", "ticket")
    monkeypatch.setenv("BOOLEY_STATE_FILE", "/x/booley_state.json")
    # Stale nested markers in the Runner env must NOT reach the config.
    monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
    monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "lint")

    home = cb._ensure_developer_codex_home(
        "test-orch-launch-unit",
        ["lint", "sim"],
    )

    config = (Path(home) / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'web_search = "disabled"' in config
    assert 'BOOLEY_MCP_TOOLS = "lint,sim"' in config
    assert 'BOOLEY_SLUG = "my-ticket"' in config
    assert 'BOOLEY_CONTROL_PROJECT_ROOT = "/project/control-root"' in config
    assert 'BOOLEY_WORKSPACE_SLUG = "my-project"' in config
    assert 'BOOLEY_AGENT_ROLE = "ticket"' in config
    assert 'BOOLEY_STATE_FILE = "/x/booley_state.json"' in config
    # Developer Agent server skips TOP_LEVEL reconciliation but is NOT a
    # nested (specialist) agent server.
    assert 'BOOLEY_MCP_NESTED = "1"' in config
    assert "BOOLEY_NESTED_AGENT" not in config
    assert "BOOLEY_NESTED_MCP_TOOLS" not in config
    # Auth is copied so the subscription login survives the HOME redirect.
    assert (Path(home) / ".codex" / "auth.json").exists()


def test_codex_homes_are_isolated_by_ticket(tmp_path, monkeypatch):
    """Concurrent ticket runners must never share mutable Codex config."""
    from booley.runtime import _codex_backend as cb

    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    cb._NESTED_HOMES.clear()

    monkeypatch.setenv("BOOLEY_SLUG", "ticket-a")
    monkeypatch.setenv("BOOLEY_STATE_FILE", "/state/a.json")
    developer_a = Path(cb._ensure_developer_codex_home("developer", ["sim"]))
    nested_a = Path(cb._ensure_nested_codex_home("debugger", ["sim"]))

    monkeypatch.setenv("BOOLEY_SLUG", "ticket-b")
    monkeypatch.setenv("BOOLEY_STATE_FILE", "/state/b.json")
    developer_b = Path(cb._ensure_developer_codex_home("developer", ["sim"]))
    nested_b = Path(cb._ensure_nested_codex_home("debugger", ["sim"]))

    assert developer_a != developer_b
    assert nested_a != nested_b
    assert 'BOOLEY_SLUG = "ticket-a"' in (developer_a / ".codex/config.toml").read_text()
    assert 'BOOLEY_SLUG = "ticket-b"' in (developer_b / ".codex/config.toml").read_text()
    assert 'BOOLEY_STATE_FILE = "/state/a.json"' in (nested_a / ".codex/config.toml").read_text()
    assert 'BOOLEY_STATE_FILE = "/state/b.json"' in (nested_b / ".codex/config.toml").read_text()


def test_codex_spawn_routes_developer_home(tmp_path, monkeypatch):
    """developer_mcp_tools selects the developer HOME, not the nested one."""
    from booley.runtime import _codex_backend as cb

    calls: dict = {}

    def _orch_home(label, mcp_tools):
        calls["orch"] = (label, mcp_tools)
        return "/tmp/fake-orch-home"

    def _nested_home(label, mcp_tools):
        calls["nested"] = (label, mcp_tools)
        return "/tmp/fake-nested-home"

    async def _fake_exec(*_cmd, **kwargs):
        calls["env"] = kwargs["env"]
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(cb, "_inside_container", lambda: True)
    monkeypatch.setattr(cb, "_ensure_developer_codex_home", _orch_home)
    monkeypatch.setattr(cb, "_ensure_nested_codex_home", _nested_home)
    monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", _fake_exec)

    params = AgentCallParams(
        prompt="p",
        model="m",
        cwd=tmp_path,
        label="developer",
        developer_mcp_tools=["lint"],
    )
    asyncio.run(cb._codex_spawn(["codex"], params))

    assert calls["orch"] == ("developer", ["lint"])
    assert "nested" not in calls
    assert calls["env"]["HOME"] == "/tmp/fake-orch-home"

    # Without the marker, in-container spawns stay on the nested path.
    calls.clear()
    params2 = AgentCallParams(prompt="p", model="m", cwd=tmp_path, label="reviewer")
    asyncio.run(cb._codex_spawn(["codex"], params2))
    assert calls["nested"] == ("reviewer", None)
    assert "orch" not in calls
    assert calls["env"]["HOME"] == "/tmp/fake-nested-home"


def test_developer_prompt_snapshot_is_run_indexed(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    feedback_dir = logs_dir / "human-logs" / "oracle_feedback"
    feedback_dir.mkdir(parents=True)
    (feedback_dir / "attempt_1_feedback.md").write_text("golden failed", encoding="utf-8")
    ctx = SimpleNamespace(
        logs_dir=logs_dir,
        slug="my-ticket",
        ticket_type="feature",
        work_dir=tmp_path / "work",
    )

    monkeypatch.setattr(harness_config, "get_backend_config", _DummyConfig)
    monkeypatch.setattr(developer, "_detect_backend_key", lambda: "codex")
    monkeypatch.setenv("BOOLEY_ORACLE_FEEDBACK", "1")
    monkeypatch.setenv("BOOLEY_ORACLE_FEEDBACK_ATTEMPT", "2")
    monkeypatch.setenv("BOOLEY_ORACLE_FEEDBACK_MAX_ATTEMPTS", "5")

    developer._write_developer_prompt_snapshot(
        ctx,
        run_index=2,
        transcript_path=logs_dir / "developer" / "run_002.jsonl",
        crash_transcript=logs_dir / "developer" / "run_001.jsonl",
        system_prompt="system",
        user_prompt="user",
        mcp_tool_names=["lint", "sim"],
    )

    payload = json.loads(
        (logs_dir / "developer" / "run_002.prompt.json").read_text(encoding="utf-8")
    )
    assert payload["system_prompt"] == "system"
    assert payload["user_prompt"] == "user"
    assert payload["metadata"]["run_index"] == 2
    assert payload["metadata"]["oracle_feedback_attempt"] == "2"
    assert payload["metadata"]["oracle_feedback_path"].endswith("attempt_1_feedback.md")
