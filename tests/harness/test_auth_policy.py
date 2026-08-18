"""Tests for the ``[agent] auth`` policy: parsing, resolution, and enforcement.

The knob pins what bills. ``auto`` leaves the agent CLI's own precedence alone;
``subscription`` makes Booley scrub the API-key env vars from every agent
environment it controls (the Claude CLI's own prescribed remedy: "Unset
ANTHROPIC_API_KEY to use your claude.ai account instead"); ``api_key`` fails
loud in the backend health check when the key is absent, because the CLI would
otherwise silently fall back to — and bill — the subscription.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.config import agent as bc
from booley.core.models import AgentCallParams
from booley.runtime import auth_token

_TOKEN = "sk-ant-oat01-abcdef123456"


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """No auth-relevant env leaking in; HOME and config dir in a tmp tree."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "userhome"))
    (tmp_path / "userhome").mkdir()
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_PROJECT_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class TestParseAuth:
    def test_absent_is_none(self):
        assert bc._parse_auth({}) is None

    @pytest.mark.parametrize("mode", ["auto", "subscription", "api_key"])
    def test_valid_values(self, mode):
        assert bc._parse_auth({"auth": mode}) == mode

    def test_retired_primary_auth_alias_is_ignored(self):
        assert bc._parse_auth({"primary_auth": "subscription"}) is None

    def test_invalid_value_raises(self):
        # A typo must not silently bill a credential the project never chose.
        with pytest.raises(bc.BackendConfigError, match="subscripton"):
            bc._parse_auth({"auth": "subscripton"})


class TestResolveAuthPolicy:
    def test_defaults_to_auto(self, clean_env):
        assert bc.resolve_auth_policy() == "auto"

    def test_env_handoff_wins(self, clean_env, monkeypatch):
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")
        assert bc.resolve_auth_policy() == "subscription"

    def test_invalid_env_degrades_to_auto(self, clean_env, monkeypatch):
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "junk")
        assert bc.resolve_auth_policy() == "auto"

    def test_reads_project_toml(self, clean_env, monkeypatch):
        project = clean_env / "proj"
        project.mkdir()
        (project / "booley.toml").write_text('[agent]\nauth = "subscription"\n')
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
        assert bc.resolve_auth_policy() == "subscription"

    def test_invalid_toml_value_degrades_not_raises(self, clean_env, monkeypatch):
        # This helper feeds reporting/forwarding paths — it must never raise;
        # the loud BackendConfigError belongs to the config-load path.
        project = clean_env / "proj"
        project.mkdir()
        (project / "booley.toml").write_text('[agent]\nauth = "junk"\n')
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
        assert bc.resolve_auth_policy() == "auto"


class TestClaudeSubscriptionScrub:
    """options.env can only merge OVER the inherited env, so "unset" is an
    empty value — which the CLI (a JS process) treats as absent."""

    @staticmethod
    def _options(auth_mode):
        from booley.runtime._claude_backend import _build_sdk_options

        params = AgentCallParams(prompt="p", model="claude-sonnet-4-6", cwd=".")
        return _build_sdk_options(params, None, auth_mode=auth_mode)

    def test_subscription_blanks_the_api_key(self, clean_env):
        assert self._options("subscription").env == {"ANTHROPIC_API_KEY": ""}

    @pytest.mark.parametrize("mode", ["auto", "api_key"])
    def test_other_modes_do_not_touch_env(self, mode, clean_env):
        # getattr: the test stub of ClaudeAgentOptions may lack the attribute
        # entirely — either way, nothing must be scrubbed outside subscription.
        assert not getattr(self._options(mode), "env", None)


class TestClaudeStoredTokenInjection:
    """SDK runs exclude "user" settings (setting_sources), so the registrar's
    settings.json env route never reaches them — _build_sdk_options must hand
    the rotation-free `booley auth` credential to the CLI via options.env, or
    ticket agents fall back to the seeded (host-rotated, stale) creds file and
    crash at launch with "OAuth session expired"."""

    @staticmethod
    def _options(auth_mode):
        from booley.runtime._claude_backend import _build_sdk_options

        params = AgentCallParams(prompt="p", model="claude-sonnet-4-6", cwd=".")
        return _build_sdk_options(params, None, auth_mode=auth_mode)

    def test_auto_injects_the_stored_token(self, clean_env):
        auth_token.store_token(_TOKEN)
        assert self._options("auto").env == {auth_token.ENV_VAR: _TOKEN}

    def test_subscription_injects_alongside_the_api_key_scrub(self, clean_env):
        auth_token.store_token(_TOKEN)
        assert self._options("subscription").env == {
            "ANTHROPIC_API_KEY": "",
            auth_token.ENV_VAR: _TOKEN,
        }

    def test_api_key_mode_never_injects(self, clean_env):
        auth_token.store_token(_TOKEN)
        assert not getattr(self._options("api_key"), "env", None)

    def test_ambient_env_token_wins_over_stored(self, clean_env, monkeypatch):
        # An exported value already reaches the CLI process env — injecting the
        # stored one would silently override the user's explicit choice.
        auth_token.store_token(_TOKEN)
        monkeypatch.setenv(auth_token.ENV_VAR, "sk-ant-oat01-ambient")
        assert not getattr(self._options("auto"), "env", None)

    def test_container_token_seed_is_injected(self, clean_env, monkeypatch):
        # In-container there is no ~/.config/booley store; the spec bind-mounts
        # the host's stored credential at a home sidecar instead.
        home = clean_env / "userhome"
        (home / auth_token.TOKEN_SEED_BASENAME[auth_token.APP_CLAUDE]).write_text(f"{_TOKEN}\n")
        assert self._options("auto").env == {auth_token.ENV_VAR: _TOKEN}


class TestClaudeHealthCheck:
    def test_api_key_mode_without_key_fails_loud(self, clean_env):
        from booley.runtime._claude_backend import ClaudeSDKBackend

        warning = ClaudeSDKBackend(auth_mode="api_key").health_check()

        assert warning is not None and "ANTHROPIC_API_KEY" in warning

    def test_subscription_mode_without_any_subscription_cred_fails_loud(self, clean_env):
        from booley.runtime._claude_backend import ClaudeSDKBackend

        warning = ClaudeSDKBackend(auth_mode="subscription").health_check()

        assert warning is not None and "subscription" in warning

    def test_api_key_mode_with_key_passes_the_auth_gate(self, clean_env, monkeypatch):
        from booley.runtime._claude_backend import ClaudeSDKBackend

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        warning = ClaudeSDKBackend(auth_mode="api_key").health_check()

        # The CLI-shim probe may still warn; the auth gate itself must not.
        assert warning is None or "auth" not in warning


class TestCodexHealthCheck:
    @staticmethod
    def _backend(monkeypatch, auth_mode):
        from booley.runtime import _codex_backend

        monkeypatch.setattr(_codex_backend.shutil, "which", lambda _: "/usr/bin/codex")
        return _codex_backend.CodexBackend(auth_mode=auth_mode)

    def test_api_key_mode_without_key_fails_loud(self, clean_env, monkeypatch):
        warning = self._backend(monkeypatch, "api_key").health_check()

        assert warning is not None and "OPENAI_API_KEY" in warning

    def test_subscription_mode_without_login_fails_loud(self, clean_env, monkeypatch):
        warning = self._backend(monkeypatch, "subscription").health_check()

        assert warning is not None and "codex login" in warning

    def test_auto_mode_is_silent(self, clean_env, monkeypatch):
        assert self._backend(monkeypatch, "auto").health_check() is None


class TestCodexSpawnScrub:
    @pytest.mark.asyncio
    async def test_subscription_pops_the_api_key(self, clean_env, monkeypatch):
        import asyncio

        from booley.runtime import _codex_backend

        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-x")
        captured: dict = {}

        async def fake_exec(*cmd, **kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        params = AgentCallParams(prompt="p", model="gpt-5.5", cwd=".")

        await _codex_backend._codex_spawn(["codex"], params, "subscription")
        assert captured["env"] is not None and "OPENAI_API_KEY" not in captured["env"]

        await _codex_backend._codex_spawn(["codex"], params, "auto")
        # auto on the host inherits the environment untouched
        assert captured["env"] is None


class TestSandboxEnvFileScrub:
    @staticmethod
    def _env_file_body(monkeypatch) -> str | None:
        from booley.harness import sandbox

        cmd: list[str] = []
        runner = sandbox.DockerRunner.__new__(sandbox.DockerRunner)
        runner._append_api_key_env_file(cmd)
        if "--env-file" not in cmd:
            return None
        return Path(cmd[cmd.index("--env-file") + 1]).read_text(encoding="utf-8")

    def test_subscription_drops_api_keys_keeps_oauth_token(self, clean_env, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-x")
        auth_token.store_token(_TOKEN)
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")

        body = self._env_file_body(monkeypatch)

        assert body is not None
        assert "ANTHROPIC_API_KEY" not in body
        assert "OPENAI_API_KEY" not in body
        assert f"CLAUDE_CODE_OAUTH_TOKEN={_TOKEN}" in body

    def test_auto_forwards_the_api_key(self, clean_env, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        body = self._env_file_body(monkeypatch)

        assert body is not None and "ANTHROPIC_API_KEY=sk-ant-api03-x" in body

    def test_subscription_skips_the_stored_codex_key(self, clean_env, monkeypatch):
        # Codex's stored credential IS an API key — under subscription it must
        # not be injected either.
        auth_token.store_token("sk-proj-stored", auth_token.APP_CODEX)
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")

        body = self._env_file_body(monkeypatch)

        assert body is None or "OPENAI_API_KEY" not in body


class TestEffectiveCredentialPolicy:
    def test_subscription_policy_ignores_the_api_key(self, clean_env, monkeypatch):
        creds = Path.home() / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        eff = auth_token.effective_credential(auth_token.APP_CLAUDE, policy="subscription")

        assert eff.mode == auth_token.MODE_SUBSCRIPTION

    def test_subscription_policy_still_honors_the_oauth_token(self, clean_env, monkeypatch):
        # The one-year token bills the subscription; the policy keeps it.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        auth_token.store_token(_TOKEN)

        eff = auth_token.effective_credential(auth_token.APP_CLAUDE, policy="subscription")

        assert eff.mode == auth_token.MODE_OAUTH_TOKEN

    def test_api_key_policy_without_key_is_none(self, clean_env):
        creds = Path.home() / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text("{}", encoding="utf-8")

        assert auth_token.effective_credential(auth_token.APP_CLAUDE, policy="api_key") is None

    def test_api_key_policy_accepts_codex_stored_key(self, clean_env):
        auth_token.store_token("sk-proj-stored", auth_token.APP_CODEX)

        eff = auth_token.effective_credential(auth_token.APP_CODEX, policy="api_key")

        assert eff is not None and eff.mode == auth_token.MODE_API_KEY

    def test_subscription_policy_scrubs_codex_key_too(self, clean_env, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-x")

        assert auth_token.effective_credential(auth_token.APP_CODEX, policy="subscription") is None


class TestDoctorPolicyAwareness:
    @staticmethod
    def _run_oauth_check(monkeypatch, policy):
        from booley.harness import doctor

        monkeypatch.setattr(doctor, "_detect_claude_code", lambda: True)
        monkeypatch.setattr(doctor, "_detect_codex", lambda: False)
        sink: dict[str, list[str]] = {"pass": [], "note": [], "warn": [], "skip": []}
        doctor._check_oauth_token(
            "claude",
            sink["pass"].append,
            lambda msg, fix=None: sink["warn"].append(msg),
            sink["skip"].append,
            policy=policy,
            _note=sink["note"].append,
        )
        return sink

    def test_subscription_policy_accepts_rotation_risk_as_a_note(self, clean_env, monkeypatch):
        # Under auth = "subscription" the key never reaches the agents, so it
        # cannot count as the rotation-free credential.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        sink = self._run_oauth_check(monkeypatch, "subscription")

        assert sink["pass"] == []
        assert sink["warn"] == []
        assert any("revoke" in m and "accepted" in m for m in sink["note"])

    def test_auto_policy_accepts_the_api_key(self, clean_env, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        sink = self._run_oauth_check(monkeypatch, "auto")

        assert sink["warn"] == []
        assert any("API key" in m for m in sink["pass"])

    def test_configured_policy_reads_booley_toml(self, clean_env, tmp_path):
        from booley.harness import doctor

        audit = doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=tmp_path / ".booley_project",
            booley_toml={"agent": {"auth": "subscription"}},
            configs_toml={},
            first_target="",
        )

        assert doctor._configured_auth_policy(audit) == "subscription"

    def test_unconfigured_policy_is_auto(self, clean_env):
        from booley.harness import doctor

        assert doctor._configured_auth_policy(None) == "auto"
