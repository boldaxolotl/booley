"""Tests for agent_backend.py — protocol conformance, BackendConfig, config loading."""

from __future__ import annotations

import json
import os
import textwrap
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from booley.config.agent import _PROVIDER_TIER_MODELS
from booley.config.settings import (
    _DEFAULT_TIER_MODELS,
    MODEL_MAP,
    STEP_TIERS,
    BackendConfig,
    BackendConfigError,
    get_backend_config,
    load_models_config,
    set_backend_config,
)
from booley.runtime.agent_backend import (
    AgentBackend,
    ClaudeSDKBackend,
    CodexBackend,
    _codex_build_prompt,
    _codex_parse_events,
    _codex_sandbox_mode,
    _codex_write_transcript,
    _is_transient_error,
    _transcript_path_for_attempt,
)


def _capturing_successful_query():
    from claude_agent_sdk import ResultMessage

    captured_options = []

    async def fake_query(*, prompt, options):
        captured_options.append(options)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="test-session",
            total_cost_usd=0,
            usage={},
            result="done",
            structured_output=None,
        )

    return captured_options, fake_query


# ===========================================================================
# Protocol conformance
# ===========================================================================


class TestProtocolConformance:
    def test_claude_sdk_is_backend(self):
        backend = ClaudeSDKBackend()
        assert isinstance(backend, AgentBackend)

    def test_codex_is_backend(self):
        backend = CodexBackend()
        assert isinstance(backend, AgentBackend)

    def test_claude_sdk_has_name(self):
        assert ClaudeSDKBackend().name == "Claude SDK"

    def test_codex_has_name(self):
        assert CodexBackend().name == "Codex"


class TestClaudeSdkLaunchContract:
    @pytest.mark.asyncio
    async def test_call_delegates_cli_selection_to_sdk(self, monkeypatch, tmp_path):
        from booley.core.models import AgentCallParams
        from booley.runtime import _claude_backend as cb

        captured_options, fake_query = _capturing_successful_query()
        monkeypatch.setattr(cb, "query", fake_query)

        result = await ClaudeSDKBackend().call(
            AgentCallParams(prompt="p", model="sonnet", cwd=tmp_path)
        )

        assert result.output == "done"
        assert len(captured_options) == 1
        assert getattr(captured_options[0], "cli_path", None) is None

    @pytest.mark.asyncio
    async def test_backend_swaps_keep_options_independent_and_parent_env_unchanged(
        self, monkeypatch, tmp_path
    ):
        from booley.core.models import AgentCallParams
        from booley.runtime import _claude_backend as cb

        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)
        captured_options, fake_query = _capturing_successful_query()
        monkeypatch.setattr(cb, "query", fake_query)
        params = AgentCallParams(prompt="p", model="sonnet", cwd=tmp_path)

        await ClaudeSDKBackend(auth_mode="subscription").call(params)
        await ClaudeSDKBackend(auth_mode="api_key").call(params)

        assert len(captured_options) == 2
        subscription, api_key = captured_options
        assert subscription is not api_key
        assert getattr(subscription, "cli_path", None) is None
        assert getattr(api_key, "cli_path", None) is None
        assert subscription.env["ANTHROPIC_API_KEY"] == ""
        assert "ANTHROPIC_API_KEY" not in api_key.env
        assert "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK" not in os.environ
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in os.environ


# ===========================================================================
# BackendConfig
# ===========================================================================


class TestBackendConfig:
    def setup_method(self):
        self.mock_active = MagicMock()
        self.mock_active.name = "MockActive"

    def test_model_for_tier_known(self):
        cfg = BackendConfig(
            active_backend=self.mock_active,
            tier_models={"heavy": "opus", "standard": "sonnet", "light": "haiku"},
        )
        assert cfg.model_for_tier("heavy") == "opus"
        assert cfg.model_for_tier("standard") == "sonnet"
        assert cfg.model_for_tier("light") == "haiku"

    def test_model_for_tier_unknown_falls_back_to_standard(self):
        cfg = BackendConfig(
            active_backend=self.mock_active,
            tier_models={"heavy": "opus", "standard": "sonnet", "light": "haiku"},
        )
        assert cfg.model_for_tier("nonexistent") == "sonnet"

    def test_backend_for_tier_active(self):
        cfg = BackendConfig(
            active_backend=self.mock_active,
            tier_models=dict(_DEFAULT_TIER_MODELS),
        )
        assert cfg.backend_for_tier("heavy") is self.mock_active
        assert cfg.backend_for_tier("standard") is self.mock_active
        assert cfg.backend_for_tier("light") is self.mock_active


# ===========================================================================
# STEP_TIERS
# ===========================================================================


class TestStepTiers:
    def test_all_tiers_valid(self):
        valid_tiers = {"heavy", "standard", "light"}
        for step, tier in STEP_TIERS.items():
            assert tier in valid_tiers, f"STEP_TIERS[{step!r}] = {tier!r} invalid"

    def test_has_required_tiers(self):
        tiers_used = set(STEP_TIERS.values())
        assert "heavy" in tiers_used
        assert "light" in tiers_used

    def test_recovery_is_light(self):
        assert STEP_TIERS["recovery"] == "light"

    def test_developer_is_heavy(self):
        assert STEP_TIERS["developer"] == "heavy"


# ===========================================================================
# load_models_config (reads [agent] from harness config)
# ===========================================================================


class TestLoadModelsConfig:
    def setup_method(self):
        # Save and reset global state before each test
        self._saved_model_map = dict(MODEL_MAP)
        set_backend_config(None)

    def teardown_method(self):
        set_backend_config(None)
        # Restore MODEL_MAP to avoid poisoning other tests
        MODEL_MAP.clear()
        MODEL_MAP.update(self._saved_model_map)

    def test_missing_file_uses_defaults(self, tmp_path):
        """No toml → default (claude) provider, never a silent codex."""
        load_models_config(tmp_path)
        cfg = get_backend_config()
        assert cfg is not None
        assert isinstance(cfg.active_backend, ClaudeSDKBackend)
        assert cfg.provider == "claude"
        assert cfg.tier_models == _PROVIDER_TIER_MODELS["claude"]

    def test_explicit_models_override_defaults(self, tmp_path):
        """[models] section overrides the hardcoded codex tier models."""
        toml_dir = tmp_path / ".booley" / "project"
        toml_dir.mkdir(parents=True)
        (toml_dir / "booley.toml").write_text(
            textwrap.dedent("""\
            [agent]
            auth = "subscription"

            [models]
            heavy = "gpt-5.5-turbo"
            standard = "gpt-5.4-mini"
            light = "gpt-5.4-mini"
        """)
        )
        load_models_config(tmp_path)
        cfg = get_backend_config()
        assert cfg.tier_models["heavy"] == "gpt-5.5-turbo"
        assert cfg.tier_models["light"] == "gpt-5.4-mini"

    def test_claude_provider_opt_in(self, tmp_path):
        """[agent] provider = 'claude' flips the active backend to Claude."""
        from booley.runtime.agent_backend import ClaudeSDKBackend

        toml_dir = tmp_path / ".booley" / "project"
        toml_dir.mkdir(parents=True)
        (toml_dir / "booley.toml").write_text(
            textwrap.dedent("""\
            [agent]
            provider = "claude"
            auth = "subscription"
        """)
        )
        load_models_config(tmp_path)
        cfg = get_backend_config()
        assert isinstance(cfg.active_backend, ClaudeSDKBackend)
        assert cfg.provider == "claude"
        assert cfg.tier_models == _PROVIDER_TIER_MODELS["claude"]

    def test_retired_primary_alias_is_ignored(self, tmp_path):
        from booley.runtime.agent_backend import ClaudeSDKBackend

        toml_dir = tmp_path / ".booley" / "project"
        toml_dir.mkdir(parents=True)
        (toml_dir / "booley.toml").write_text(
            textwrap.dedent("""\
            [agent]
            primary = "codex"
            secondary = false
        """)
        )
        load_models_config(tmp_path)
        cfg = get_backend_config()
        assert isinstance(cfg.active_backend, ClaudeSDKBackend)
        assert cfg.provider == "claude"

    def test_invalid_provider_raises(self, tmp_path):
        """An invalid provider is a hard error — never a silent fallback."""
        toml_dir = tmp_path / ".booley" / "project"
        toml_dir.mkdir(parents=True)
        (toml_dir / "booley.toml").write_text(
            textwrap.dedent("""\
            [agent]
            provider = "gpt4all"
        """)
        )
        with pytest.raises(BackendConfigError, match="gpt4all"):
            load_models_config(tmp_path)

    def test_malformed_toml_uses_defaults(self, tmp_path):
        """Unparseable toml can't declare a provider → default (claude)."""
        toml_dir = tmp_path / ".booley" / "project"
        toml_dir.mkdir(parents=True)
        (toml_dir / "pipeline.toml").write_text("this is not valid toml {{{")
        load_models_config(tmp_path)
        cfg = get_backend_config()
        assert cfg is not None
        assert cfg.provider == "claude"
        assert cfg.tier_models == _PROVIDER_TIER_MODELS["claude"]

    def test_default_provider_is_claude(self, tmp_path):
        """With no provider set, the default is claude (never a silent codex)."""
        toml_dir = tmp_path / ".booley" / "project"
        toml_dir.mkdir(parents=True)
        (toml_dir / "booley.toml").write_text(
            textwrap.dedent("""\
            [agent]
            auth = "subscription"
        """)
        )
        load_models_config(tmp_path)
        cfg = get_backend_config()
        assert isinstance(cfg.active_backend, ClaudeSDKBackend)
        assert cfg.provider == "claude"

    def test_auth_from_toml(self, tmp_path):
        """The single auth field is loaded from toml."""
        toml_dir = tmp_path / ".booley" / "project"
        toml_dir.mkdir(parents=True)
        (toml_dir / "booley.toml").write_text(
            textwrap.dedent("""\
            [agent]
            auth = "subscription"
        """)
        )
        load_models_config(tmp_path)
        cfg = get_backend_config()
        assert cfg.auth == "subscription"

    def test_retired_primary_auth_alias_is_ignored(self, tmp_path):
        toml_dir = tmp_path / ".booley" / "project"
        toml_dir.mkdir(parents=True)
        (toml_dir / "pipeline.toml").write_text(
            textwrap.dedent("""\
            [agent]
            primary_auth = "api_key"
        """)
        )
        load_models_config(tmp_path)
        cfg = get_backend_config()
        assert cfg.auth == "auto"

    def test_lazy_default_honors_provider_env(self, monkeypatch):
        """get_backend_config() reads BOOLEY_PRIMARY_PROVIDER for nested agents."""
        from booley.runtime.agent_backend import ClaudeSDKBackend

        monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "claude")
        set_backend_config(None)
        cfg = get_backend_config()
        assert isinstance(cfg.active_backend, ClaudeSDKBackend)
        assert cfg.provider == "claude"

    def test_provider_handoff_preserves_project_auth(self, tmp_path, monkeypatch):
        (tmp_path / "booley.toml").write_text(
            '[agent]\nprovider = "claude"\nauth = "subscription"\n', encoding="utf-8"
        )
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))
        monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "codex")
        monkeypatch.delenv("BOOLEY_PRIMARY_AUTH", raising=False)
        set_backend_config(None)

        assert get_backend_config().auth == "subscription"

    def test_codex_resume_command_and_thread_id(self, tmp_path, monkeypatch):
        from booley.runtime._codex_backend import _codex_build_cmd, _codex_thread_id

        monkeypatch.setattr("booley.runtime._codex_backend.shutil.which", lambda _name: "codex")
        monkeypatch.setattr("booley.runtime._codex_backend._inside_container", lambda: False)
        cmd, schema = _codex_build_cmd("gpt-test", tmp_path, [], None, None, session_id="thread-7")

        assert schema is None
        assert cmd[:3] == ["codex", "exec", "resume"]
        assert cmd[-2:] == ["thread-7", "-"]
        events = [{"type": "thread.started", "thread_id": "thread-7"}]
        assert _codex_thread_id(events) == "thread-7"

    def test_lazy_default_is_claude_without_env(self, monkeypatch):
        """On the host with no env/toml signal, default to claude — never codex."""
        from booley.runtime.agent_backend import ClaudeSDKBackend

        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        set_backend_config(None)
        cfg = get_backend_config()
        assert isinstance(cfg.active_backend, ClaudeSDKBackend)
        assert cfg.provider == "claude"

    def test_lazy_resolves_provider_from_project_toml(self, tmp_path, monkeypatch):
        """No env hand-off → resolve provider from BOOLEY_PROJECT_DIR/booley.toml.

        This is the fix for specialist subprocesses (e.g. the reviewer) that
        used to silently default to codex when the developer's env
        propagation was dropped. The container mounts booley.toml flat under
        BOOLEY_PROJECT_DIR.
        """
        from booley.runtime.agent_backend import CodexBackend

        (tmp_path / "booley.toml").write_text(
            textwrap.dedent("""\
            [agent]
            provider = "codex"
            auth = "subscription"
        """)
        )
        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))
        set_backend_config(None)
        cfg = get_backend_config()
        assert isinstance(cfg.active_backend, CodexBackend)
        assert cfg.provider == "codex"
        assert cfg.auth == "subscription"

    def test_lazy_raises_in_container_without_provider(self, tmp_path, monkeypatch):
        """Inside a container with no env, no toml, no app signal → fail loud."""
        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.delenv("BOOLEY_AGENT_APP", raising=False)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))  # no booley.toml
        monkeypatch.setattr(
            "booley.config.agent._inside_container",
            lambda: True,
        )
        set_backend_config(None)
        with pytest.raises(BackendConfigError, match="unresolved inside container"):
            get_backend_config()

    def test_container_error_explains_how_to_fix(self, tmp_path, monkeypatch):
        """The fail-loud message tells a standalone user how to pin a provider (QA-9)."""
        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.delenv("BOOLEY_AGENT_APP", raising=False)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "booley.config.agent._inside_container",
            lambda: True,
        )
        set_backend_config(None)
        with pytest.raises(BackendConfigError) as exc:
            get_backend_config()
        msg = str(exc.value)
        assert "BOOLEY_PRIMARY_PROVIDER=claude" in msg
        assert "[agent] provider" in msg

    def test_lazy_falls_back_to_agent_app_in_container(self, tmp_path, monkeypatch):
        """QA-9: a bare specialist run with no hand-off resolves via BOOLEY_AGENT_APP.

        The devcontainer always exports BOOLEY_AGENT_APP (claude/codex/none),
        so a specialist launched directly from a terminal inside the container
        — with no developer to set BOOLEY_PRIMARY_PROVIDER and no
        [agent] provider in booley.toml — resolves instead of hard-failing.
        """
        from booley.runtime.agent_backend import CodexBackend

        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))  # no booley.toml
        monkeypatch.setenv("BOOLEY_AGENT_APP", "codex")
        monkeypatch.setattr(
            "booley.config.agent._inside_container",
            lambda: True,
        )
        set_backend_config(None)
        cfg = get_backend_config()
        assert isinstance(cfg.active_backend, CodexBackend)
        assert cfg.provider == "codex"

    def test_agent_app_none_does_not_resolve(self, tmp_path, monkeypatch):
        """BOOLEY_AGENT_APP='none' is not a provider → still fail loud in container."""
        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))
        monkeypatch.setenv("BOOLEY_AGENT_APP", "none")
        monkeypatch.setattr(
            "booley.config.agent._inside_container",
            lambda: True,
        )
        set_backend_config(None)
        with pytest.raises(BackendConfigError, match="unresolved inside container"):
            get_backend_config()

    def test_explicit_provider_env_wins_over_agent_app(self, monkeypatch):
        """BOOLEY_PRIMARY_PROVIDER outranks the BOOLEY_AGENT_APP fallback."""
        from booley.runtime.agent_backend import ClaudeSDKBackend

        monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "claude")
        monkeypatch.setenv("BOOLEY_AGENT_APP", "codex")
        set_backend_config(None)
        cfg = get_backend_config()
        assert isinstance(cfg.active_backend, ClaudeSDKBackend)
        assert cfg.provider == "claude"

    def test_booley_toml_preferred_over_pipeline_toml(self, tmp_path):
        """booley.toml takes precedence when both exist."""
        toml_dir = tmp_path / ".booley" / "project"
        toml_dir.mkdir(parents=True)
        (toml_dir / "pipeline.toml").write_text(
            textwrap.dedent("""\
            [agent]
            auth = "api_key"
        """)
        )
        (toml_dir / "booley.toml").write_text(
            textwrap.dedent("""\
            [agent]
            auth = "subscription"
        """)
        )
        load_models_config(tmp_path)
        cfg = get_backend_config()
        assert cfg.auth == "subscription"


# ===========================================================================
# Transient error detection
# ===========================================================================


class TestTransientErrors:
    def test_connection_error(self):
        assert _is_transient_error(ConnectionError("reset"))

    def test_os_error(self):
        assert _is_transient_error(OSError("network down"))

    def test_500_in_message(self):
        assert _is_transient_error(Exception("500 internal server error"))

    def test_non_transient(self):
        assert not _is_transient_error(ValueError("bad input"))


# ===========================================================================
# Transcript path
# ===========================================================================


class TestTranscriptPath:
    def test_none_passthrough(self):
        assert _transcript_path_for_attempt(None, 1) is None
        assert _transcript_path_for_attempt(None, 3) is None

    def test_attempt_1_unchanged(self):
        p = Path("/logs/02-planning.jsonl")
        assert _transcript_path_for_attempt(p, 1) == p

    def test_attempt_2_gets_suffix(self):
        p = Path("/logs/02-planning.jsonl")
        result = _transcript_path_for_attempt(p, 2)
        assert result == Path("/logs/02-planning-retry2.jsonl")


# ===========================================================================
# Codex backend — health check
# ===========================================================================


class TestCodexHealthCheck:
    def test_healthy_when_on_path(self):
        backend = CodexBackend()
        with patch("booley.runtime._codex_backend.shutil.which", return_value="/usr/bin/codex"):
            assert backend.health_check() is None

    def test_unhealthy_when_missing(self):
        backend = CodexBackend()
        with patch("booley.runtime._codex_backend.shutil.which", return_value=None):
            result = backend.health_check()
            assert result is not None
            assert "not found" in result


# ===========================================================================
# Codex backend — prompt building
# ===========================================================================


class TestCodexPromptBuilding:
    def test_basic_prompt(self):
        result = _codex_build_prompt("Do the thing", None)
        assert result == "Do the thing"

    def test_with_system_prompt(self):
        result = _codex_build_prompt("Do it", "You are a reviewer")
        assert "You are a reviewer" in result
        assert "---" in result
        assert "Do it" in result


# ===========================================================================
# Codex backend — event parsing
# ===========================================================================


class TestCodexEventParsing:
    def test_parse_agent_message(self):
        events_jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "hello world"},
            }
        )
        text, _in_tok, _out_tok, _cached_tok, err, _events = _codex_parse_events(events_jsonl)
        assert text == "hello world"
        assert err is None

    def test_live_file_change_keeps_paths(self):
        from booley.runtime import _codex_backend as cb

        events = []
        cb._dispatch_stdout_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [
                        {"kind": "update", "path": "rtl/top.sv"},
                        {"kind": "add", "path": "tb/top_tb.sv"},
                    ],
                },
            },
            events.append,
            None,
        )

        assert events == [{"type": "file_change", "paths": ["rtl/top.sv", "tb/top_tb.sv"]}]

    def test_booley_mcp_events_pause_and_resume_developer_budget(self):
        from booley.runtime import _codex_backend as cb

        budget = MagicMock()
        item = {
            "id": "call-1",
            "type": "mcp_tool_call",
            "server": "booley",
            "tool": "simulate",
        }
        cb._dispatch_stdout_event({"type": "item.started", "item": item}, None, None, budget)
        cb._dispatch_stdout_event({"type": "item.completed", "item": item}, None, None, budget)

        budget.pause.assert_called_once_with("codex-mcp:call-1", "waiting for simulate")
        budget.resume.assert_called_once_with("codex-mcp:call-1")

    def test_non_booley_mcp_does_not_pause_developer_budget(self):
        from booley.runtime import _codex_backend as cb

        budget = MagicMock()
        cb._dispatch_stdout_event(
            {
                "type": "item.started",
                "item": {
                    "id": "call-1",
                    "type": "mcp_tool_call",
                    "server": "other",
                    "tool": "lookup",
                },
            },
            None,
            None,
            budget,
        )
        budget.pause.assert_not_called()

    def test_parse_usage(self):
        events_jsonl = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 500, "output_tokens": 200},
            }
        )
        _, in_tok, out_tok, _, _, _ = _codex_parse_events(events_jsonl)
        assert in_tok == 500
        assert out_tok == 200

    def test_parse_cached_tokens(self):
        events_jsonl = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 5000, "output_tokens": 200, "cached_input_tokens": 4000},
            }
        )
        _, in_tok, out_tok, cached_tok, _, _ = _codex_parse_events(events_jsonl)
        assert in_tok == 5000
        assert out_tok == 200
        assert cached_tok == 4000

    def test_parse_error(self):
        events_jsonl = json.dumps({"type": "error", "message": "rate limit exceeded"})
        _, _, _, _, err, _ = _codex_parse_events(events_jsonl)
        assert err == "rate limit exceeded"

    def test_parse_turn_failed(self):
        events_jsonl = json.dumps({"type": "turn.failed", "error": {"message": "crash"}})
        _, _, _, _, err, _ = _codex_parse_events(events_jsonl)
        assert err == "crash"

    def test_parse_ignores_invalid_json(self):
        raw = 'not json\n{"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}'
        _, in_tok, _out_tok, _, _, events = _codex_parse_events(raw)
        assert len(events) == 1
        assert in_tok == 1


# ===========================================================================
# Codex backend — transcript writing
# ===========================================================================


class TestCodexTranscript:
    def test_write_transcript_noop_when_path_none(self):
        _codex_write_transcript([{"type": "test"}], None)

    def test_write_transcript_noop_when_empty(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _codex_write_transcript([], path)
        assert not path.exists()

    def test_write_transcript_creates_file(self, tmp_path):
        path = tmp_path / "sub" / "transcript.jsonl"
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ]
        _codex_write_transcript(events, path)
        assert path.exists()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        entry = json.loads(lines[0])
        assert entry["type"] == "item.completed"


# ===========================================================================
# Codex backend — sandbox mode mapping
# ===========================================================================


class TestCodexSandboxMode:
    # Write-capable tiers use danger-full-access as workaround for
    # codex exec ignoring --full-auto / -s workspace-write (#18113).

    def test_none_tools_is_danger_full_access(self):
        assert _codex_sandbox_mode(None) == "danger-full-access"

    def test_full_tools_is_danger_full_access(self):
        assert (
            _codex_sandbox_mode(["Read", "Glob", "Grep", "Write", "Edit", "Bash"])
            == "danger-full-access"
        )

    def test_edit_tools_is_danger_full_access(self):
        assert (
            _codex_sandbox_mode(["Read", "Glob", "Grep", "Edit", "Bash"]) == "danger-full-access"
        )

    def test_read_only_tools_is_read_only(self):
        assert _codex_sandbox_mode(["Read", "Glob", "Grep"]) == "read-only"

    def test_bash_only_is_danger_full_access(self):
        assert _codex_sandbox_mode(["Bash"]) == "danger-full-access"

    def test_write_without_bash_is_danger_full_access(self):
        assert _codex_sandbox_mode(["Read", "Write"]) == "danger-full-access"

    def test_empty_list_is_read_only(self):
        assert _codex_sandbox_mode([]) == "read-only"


# ===========================================================================
# Codex backend — multi-message accumulation
# ===========================================================================


class TestCodexMultiMessage:
    def test_multiple_agent_messages_accumulated(self):
        lines = "\n".join(
            [
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}
                ),
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "second"}}
                ),
            ]
        )
        text, _, _, _, _, _ = _codex_parse_events(lines)
        assert "first" in text
        assert "second" in text

    def test_single_message_no_extra_separators(self):
        lines = json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "only"}}
        )
        text, _, _, _, _, _ = _codex_parse_events(lines)
        assert text == "only"

    def test_empty_messages_skipped(self):
        lines = "\n".join(
            [
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": ""}}
                ),
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "real"}}
                ),
            ]
        )
        text, _, _, _, _, _ = _codex_parse_events(lines)
        assert text == "real"


# ===========================================================================
# _build_sdk_options — session resume mapping
# ===========================================================================


class TestBuildSdkOptionsResume:
    def test_resume_fields_mapped(self, tmp_path):
        from booley.harness.models import AgentCallParams
        from booley.runtime._claude_backend import _build_sdk_options

        params = AgentCallParams(
            prompt="p",
            model="sonnet",
            cwd=str(tmp_path),
            session_id="abc-123",
            resume_session=True,
        )
        options = _build_sdk_options(params)
        assert options.resume == "abc-123"
        assert options.continue_conversation is True

    def test_defaults_produce_no_resume_fields(self, tmp_path):
        from booley.harness.models import AgentCallParams
        from booley.runtime._claude_backend import _build_sdk_options

        params = AgentCallParams(
            prompt="p",
            model="sonnet",
            cwd=str(tmp_path),
        )
        options = _build_sdk_options(params)
        assert getattr(options, "resume", None) is None
        assert getattr(options, "continue_conversation", False) is False


# ===========================================================================
# _build_sdk_options — Booley MCP wiring (native ClaudeSDKBackend)
# ===========================================================================


class TestBuildSdkOptionsMcp:
    def _options(self, tmp_path, **kwargs):
        from booley.harness.models import AgentCallParams
        from booley.runtime._claude_backend import _build_sdk_options

        params = AgentCallParams(
            prompt="p",
            model="sonnet",
            cwd=str(tmp_path),
            **kwargs,
        )
        return _build_sdk_options(params)

    def test_booley_mcp_server_always_wired(self, tmp_path):
        options = self._options(tmp_path)
        assert "booley" in options.mcp_servers
        server = options.mcp_servers["booley"]
        assert server["command"] == "python"
        assert server["args"] == ["-m", "booley.mcp.server"]
        assert options.strict_mcp_config is True

    def test_developer_call_has_no_nested_filter(self, tmp_path):
        # nested_mcp_tools is None -> full MCP, no nested allowlist env
        options = self._options(tmp_path, nested_mcp_tools=None)
        env = options.mcp_servers["booley"]["env"]
        assert "BOOLEY_NESTED_AGENT" not in env
        assert "BOOLEY_NESTED_MCP_TOOLS" not in env

    def test_nested_allowlist_sets_recursion_safe_env(self, tmp_path):
        options = self._options(tmp_path, nested_mcp_tools=["sim", "bwave_open"])
        env = options.mcp_servers["booley"]["env"]
        assert env["BOOLEY_NESTED_AGENT"] == "1"
        assert env["BOOLEY_NESTED_MCP_TOOLS"] == "sim,bwave_open"

    def test_empty_nested_allowlist_blocks_all_mcp(self, tmp_path):
        # [] -> nested call with zero MCP tools visible (still recursion-safe)
        options = self._options(tmp_path, nested_mcp_tools=[])
        env = options.mcp_servers["booley"]["env"]
        assert env["BOOLEY_NESTED_AGENT"] == "1"
        assert env["BOOLEY_NESTED_MCP_TOOLS"] == ""


# ===========================================================================
# Idle/heartbeat stream timeout (QA-10)
# ===========================================================================


class TestIdleStreamTimeout:
    """The stream timeout is per-event-gap, not total wall-clock.

    A throttled-but-progressing stream (heavy 7-day rate-limit pressure) must
    keep resetting the deadline and never be killed; only a genuine stall
    (no event at all for the idle window) times out.
    """

    @staticmethod
    def _run_stream(backend, *, items, gap, initial_delay, timeout):
        import anyio

        from booley.runtime import _claude_backend as cb

        async def _fake_query(*, prompt, options):
            if initial_delay:
                await anyio.sleep(initial_delay)
            for it in items:
                yield it
                await anyio.sleep(gap)

        counters = cb._UsageCounters()
        state = cb._StreamState()

        async def _drive():
            with patch.object(cb, "query", _fake_query):
                await backend._process_stream(
                    "p",
                    object(),
                    timeout,
                    counters,
                    None,
                    None,
                    state,
                )

        anyio.run(_drive)
        return counters, state

    def test_slow_but_progressing_stream_is_not_killed(self):
        # Total elapsed (~0.48s) far exceeds the timeout (0.3s), but every
        # inter-event gap (0.06s) is well under it. A fixed wall-clock timeout
        # would kill this; the idle/heartbeat timeout must not.
        backend = ClaudeSDKBackend()
        sentinels = [object() for _ in range(8)]
        counters, _state = self._run_stream(
            backend,
            items=sentinels,
            gap=0.06,
            initial_delay=0.0,
            timeout=0.3,
        )
        # Completed the whole stream without raising TimeoutError.
        assert counters is not None

    def test_genuine_stall_times_out(self):
        # No event for longer than the idle window -> TimeoutError.
        backend = ClaudeSDKBackend()
        with pytest.raises(TimeoutError):
            self._run_stream(
                backend,
                items=[object()],
                gap=0.0,
                initial_delay=0.4,
                timeout=0.15,
            )


class TestClaudeDeveloperBudgetEvents:
    def test_booley_tool_use_pauses_until_matching_result(self):
        from claude_agent_sdk import AssistantMessage, UserMessage

        from booley.runtime import _claude_backend as cb

        budget = MagicMock()
        started = MagicMock(
            spec=AssistantMessage,
            content=[SimpleNamespace(id="tool-1", name="mcp__booley__asic_synthesize", input={})],
        )
        completed = MagicMock(spec=UserMessage, content=[SimpleNamespace(tool_use_id="tool-1")])

        cb._update_budget_for_claude_message(started, budget)
        cb._update_budget_for_claude_message(completed, budget)

        budget.pause.assert_called_once_with("claude-mcp:tool-1", "waiting for asic_synthesize")
        budget.resume.assert_called_once_with("claude-mcp:tool-1")

    def test_builtin_tool_does_not_pause(self):
        from claude_agent_sdk import AssistantMessage

        from booley.runtime import _claude_backend as cb

        budget = MagicMock()
        message = MagicMock(
            spec=AssistantMessage,
            content=[SimpleNamespace(id="tool-1", name="Bash", input={})],
        )
        cb._update_budget_for_claude_message(message, budget)
        budget.pause.assert_not_called()


class TestCallOnceTimeoutResultSalvage:
    """A ResultMessage captured before an SDK teardown stall must not be lost."""

    @staticmethod
    def _options():
        from types import SimpleNamespace

        return SimpleNamespace(model="claude-opus-4-8", system_prompt=None)

    def test_post_result_idle_timeout_is_swallowed(self):
        import anyio

        from booley.runtime import _claude_backend as cb

        backend = ClaudeSDKBackend()
        counters = cb._UsageCounters()
        counters.final_text = "partial findings so far"

        async def _fake_process(self, prompt, options, timeout_seconds, c, tf, oe, st):
            c.got_result = True  # result already landed before the stall
            raise TimeoutError

        with (
            patch.object(
                cb.ClaudeSDKBackend, "_prepare_call", return_value=(None, counters, deque())
            ),
            patch.object(cb.ClaudeSDKBackend, "_process_stream", _fake_process),
        ):
            result = anyio.run(
                lambda: backend._call_once(
                    "p",
                    self._options(),
                    5,
                    transcript_path=None,
                    output_format=None,
                )
            )
        assert result.output == "partial findings so far"

    def test_pre_result_idle_timeout_propagates(self):
        import anyio

        from booley.runtime import _claude_backend as cb

        backend = ClaudeSDKBackend()
        counters = cb._UsageCounters()

        async def _fake_process(self, prompt, options, timeout_seconds, c, tf, oe, st):
            raise TimeoutError  # no result captured

        with (
            patch.object(
                cb.ClaudeSDKBackend, "_prepare_call", return_value=(None, counters, deque())
            ),
            patch.object(cb.ClaudeSDKBackend, "_process_stream", _fake_process),
            pytest.raises(TimeoutError),
        ):
            anyio.run(
                lambda: backend._call_once(
                    "p",
                    self._options(),
                    5,
                    transcript_path=None,
                    output_format=None,
                )
            )


class TestToolCallCapture:
    """_UsageCounters captures named sub-agent MCP-tool-call inputs."""

    @staticmethod
    def _msg(blocks):
        from types import SimpleNamespace

        return SimpleNamespace(content=blocks, usage=None)

    @staticmethod
    def _tool_block(name, inp):
        from types import SimpleNamespace

        return SimpleNamespace(name=name, input=inp)

    def test_captures_only_named_tools(self):
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters(frozenset({"ReportFindings"}))
        c.capture_mcp_tool_uses(
            self._msg(
                [
                    self._tool_block("ReportFindings", {"findings": [{"file": "a.sv"}]}),
                    self._tool_block("Read", {"file_path": "a.sv"}),  # not captured
                ]
            )
        )
        assert c.captured_agent_capability_calls == {
            "ReportFindings": [{"findings": [{"file": "a.sv"}]}],
        }

    def test_multiple_calls_appended_in_order(self):
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters(frozenset({"ReportFindings"}))
        c.capture_mcp_tool_uses(self._msg([self._tool_block("ReportFindings", {"n": 1})]))
        c.capture_mcp_tool_uses(self._msg([self._tool_block("ReportFindings", {"n": 2})]))
        assert c.captured_agent_capability_calls["ReportFindings"] == [{"n": 1}, {"n": 2}]

    def test_no_capture_names_is_noop(self):
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters()  # default: capture nothing
        c.capture_mcp_tool_uses(self._msg([self._tool_block("ReportFindings", {"n": 1})]))
        assert c.captured_agent_capability_calls == {}

    def test_non_dict_input_skipped(self):
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters(frozenset({"ReportFindings"}))
        c.capture_mcp_tool_uses(self._msg([self._tool_block("ReportFindings", None)]))
        assert c.captured_agent_capability_calls == {}


class TestClaudeFileChangeEvents:
    @staticmethod
    def _block(**fields):
        from types import SimpleNamespace

        return SimpleNamespace(**fields)

    def test_successful_edit_emits_path_after_tool_result(self):
        from types import SimpleNamespace

        from booley.runtime import _claude_backend as cb

        state = cb._StreamState()
        assistant = SimpleNamespace(
            content=[
                self._block(
                    id="edit-1",
                    name="Edit",
                    input={"file_path": "/work/rtl/top.sv"},
                )
            ]
        )
        cb._capture_pending_file_edits(assistant, state)

        events = []
        result = SimpleNamespace(content=[self._block(tool_use_id="edit-1", is_error=False)])
        cb._dispatch_completed_file_edits(events.append, result, state)

        assert events == [{"type": "file_change", "paths": ["/work/rtl/top.sv"]}]
        assert state.pending_file_edits == {}

    def test_failed_edit_does_not_emit(self):
        from types import SimpleNamespace

        from booley.runtime import _claude_backend as cb

        state = cb._StreamState(pending_file_edits={"edit-1": "rtl/top.sv"})
        result = SimpleNamespace(content=[self._block(tool_use_id="edit-1", is_error=True)])
        events = []

        cb._dispatch_completed_file_edits(events.append, result, state)

        assert events == []
        assert state.pending_file_edits == {}


class TestLiveUsageDeltas:
    """Streaming usage is emitted as output-token deltas the Console accumulates.

    Input is deliberately excluded from the delta: with prompt caching the
    cumulative input is dominated by re-reads of the same context and grows
    with turn count, so it tracks conversation length, not work done. The
    Console shows the *current* prompt size (``context_tokens``) instead.
    """

    @staticmethod
    def _msg(usage):
        from types import SimpleNamespace

        return SimpleNamespace(content=[], usage=usage)

    def test_first_delta_is_the_full_running_total(self):
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters()
        c.update_from_assistant(self._msg({"input_tokens": 1000, "output_tokens": 200}))
        out_tokens, cost = c.usage_delta("claude-opus-4-8")
        assert out_tokens == 200
        assert cost > 0

    def test_subsequent_deltas_exclude_what_was_already_emitted(self):
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters()
        c.update_from_assistant(self._msg({"input_tokens": 1000, "output_tokens": 200}))
        c.usage_delta("claude-opus-4-8")
        c.update_from_assistant(self._msg({"input_tokens": 500, "output_tokens": 100}))
        out_tokens, _cost = c.usage_delta("claude-opus-4-8")
        assert out_tokens == 100

    def test_no_new_usage_yields_a_zero_delta(self):
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters()
        c.update_from_assistant(self._msg({"input_tokens": 10, "output_tokens": 5}))
        c.usage_delta("claude-opus-4-8")
        assert c.usage_delta("claude-opus-4-8") == (0, 0.0)

    def test_result_cost_supersedes_the_estimate(self):
        """The billed figure replaces the price-table estimate, dip and all."""
        from types import SimpleNamespace

        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters()
        c.update_from_assistant(self._msg({"input_tokens": 100_000, "output_tokens": 5_000}))
        _tok, estimated = c.usage_delta("claude-opus-4-8")
        c.apply_result(
            SimpleNamespace(
                result="done",
                structured_output=None,
                total_cost_usd=0.01,  # far below the estimate
                usage=None,
            )
        )
        _tok2, correction = c.usage_delta("claude-opus-4-8")
        assert correction < 0
        assert estimated + correction == pytest.approx(0.01)

    def test_unknown_model_costs_nothing_and_does_not_raise(self):
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters()
        c.update_from_assistant(self._msg({"input_tokens": 100, "output_tokens": 10}))
        assert c.usage_delta("some-unlisted-model") == (10, 0.0)

    def test_context_tokens_track_the_latest_prompt_not_the_sum(self):
        """context_tokens is the current window fill, so it must not accumulate."""
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters()
        c.update_from_assistant(
            self._msg(
                {
                    "input_tokens": 4_000,
                    "cache_read_input_tokens": 86_000,
                    "output_tokens": 200,
                }
            )
        )
        assert c.context_tokens == 90_000
        c.update_from_assistant(
            self._msg(
                {
                    "input_tokens": 2_000,
                    "cache_read_input_tokens": 140_000,
                    "output_tokens": 300,
                }
            )
        )
        assert c.context_tokens == 142_000  # replaced, not 90k + 142k
        assert c.total_input == 232_000  # cumulative total still accrues

    def test_cache_writes_are_tracked_and_billed_at_their_own_rate(self):
        """A cache write costs 1.25x input, not 1x as the old estimate assumed."""
        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters()
        c.update_from_assistant(
            self._msg({"input_tokens": 0, "cache_creation_input_tokens": 100_000})
        )
        assert c.total_cache_create == 100_000
        # opus-4-8 input is $5/M, so a cache write is $6.25/M.
        assert c.estimated_cost("claude-opus-4-8") == pytest.approx(100_000 * 6.25 / 1e6)


class TestDispatchUsage:
    """_dispatch_usage only fires when there is something new to report."""

    @staticmethod
    def _counters_with(tokens):
        from types import SimpleNamespace

        from booley.runtime import _claude_backend as cb

        c = cb._UsageCounters()
        c.update_from_assistant(SimpleNamespace(content=[], usage={"output_tokens": tokens}))
        return c

    def test_emits_usage_event(self):
        from booley.runtime import _claude_backend as cb

        events = []
        cb._dispatch_usage(events.append, self._counters_with(42), "claude-opus-4-8")
        assert events == [
            {
                "type": "usage",
                "output_tokens": 42,
                "cost_usd": pytest.approx(42 * 25e-6),
                "context_tokens": 0,
                "context_limit": 1_000_000,
            }
        ]

    def test_silent_when_nothing_accrued(self):
        from booley.runtime import _claude_backend as cb

        events = []
        cb._dispatch_usage(events.append, cb._UsageCounters(), "claude-opus-4-8")
        assert events == []

    def test_no_on_event_is_a_noop(self):
        from booley.runtime import _claude_backend as cb

        cb._dispatch_usage(None, self._counters_with(42), "claude-opus-4-8")

    def test_callback_failure_never_escapes(self):
        """A broken display must not abort a paid stream."""
        from booley.runtime import _claude_backend as cb

        def _boom(_event):
            raise RuntimeError("console died")

        cb._dispatch_usage(_boom, self._counters_with(42), "claude-opus-4-8")
