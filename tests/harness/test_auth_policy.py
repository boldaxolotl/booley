"""Tests for the ``[agent] auth`` policy: parsing, resolution, and enforcement.

The knob pins what bills. ``auto`` leaves the agent CLI's own precedence alone;
``subscription`` makes Booley scrub the API-key env vars from every agent
environment it controls (the Claude CLI's own prescribed remedy: "Unset
ANTHROPIC_API_KEY to use your claude.ai account instead"); ``api_key`` fails
loud in the backend health check when the key is absent, because the CLI would
otherwise silently fall back to — and bill — the subscription.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.config import agent as bc
from booley.core.models import AgentCallParams
from booley.runtime import auth_token

_TOKEN = "sk-ant-oat01-abcdef123456"
_TRAFFIC_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


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
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_DIR",
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
        monkeypatch.setenv("BOOLEY_DIR", str(project))
        assert bc.resolve_auth_policy() == "subscription"

    def test_invalid_toml_value_degrades_not_raises(self, clean_env, monkeypatch):
        # This helper feeds reporting/forwarding paths — it must never raise;
        # the loud BackendConfigError belongs to the config-load path.
        project = clean_env / "proj"
        project.mkdir()
        (project / "booley.toml").write_text('[agent]\nauth = "junk"\n')
        monkeypatch.setenv("BOOLEY_DIR", str(project))
        assert bc.resolve_auth_policy() == "auto"


class TestClaudeSubscriptionScrub:
    """options.env can only merge OVER the inherited env, so "unset" is an
    empty value — which the CLI (a JS process) treats as absent."""

    @staticmethod
    def _options(auth_mode):
        from booley.runtime._claude_backend import _build_sdk_options

        params = AgentCallParams(prompt="p", model="claude-sonnet-4-6", cwd=".")
        return _build_sdk_options(params, auth_mode=auth_mode)

    def test_nonessential_traffic_is_disabled_in_child_env(self, clean_env):
        assert self._options("auto").env == {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

    def test_ambient_nonessential_traffic_setting_is_preserved(self, clean_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "0")
        assert self._options("auto").env == {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0"}

    def test_subscription_blanks_the_api_key(self, clean_env):
        assert self._options("subscription").env == {
            **_TRAFFIC_ENV,
            "ANTHROPIC_API_KEY": "",
        }

    @pytest.mark.parametrize("mode", ["auto", "api_key"])
    def test_other_modes_do_not_touch_auth_env(self, mode, clean_env):
        assert self._options(mode).env == _TRAFFIC_ENV


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
        return _build_sdk_options(params, auth_mode=auth_mode)

    def test_auto_injects_the_stored_token(self, clean_env):
        auth_token.store_token(_TOKEN)
        assert self._options("auto").env == {
            **_TRAFFIC_ENV,
            auth_token.ENV_VAR: _TOKEN,
        }

    def test_subscription_injects_alongside_the_api_key_scrub(self, clean_env):
        auth_token.store_token(_TOKEN)
        assert self._options("subscription").env == {
            **_TRAFFIC_ENV,
            "ANTHROPIC_API_KEY": "",
            auth_token.ENV_VAR: _TOKEN,
        }

    def test_api_key_mode_never_injects(self, clean_env):
        auth_token.store_token(_TOKEN)
        assert self._options("api_key").env == _TRAFFIC_ENV

    def test_ambient_env_token_wins_over_stored(self, clean_env, monkeypatch):
        # An exported value already reaches the CLI process env — injecting the
        # stored one would silently override the user's explicit choice.
        auth_token.store_token(_TOKEN)
        monkeypatch.setenv(auth_token.ENV_VAR, "sk-ant-oat01-ambient")
        assert self._options("auto").env == _TRAFFIC_ENV

    def test_container_token_seed_is_injected(self, clean_env, monkeypatch):
        # In-container there is no ~/.config/booley store; the spec bind-mounts
        # the host's stored credential at a home sidecar instead.
        home = clean_env / "userhome"
        (home / auth_token.TOKEN_SEED_BASENAME[auth_token.APP_CLAUDE]).write_text(f"{_TOKEN}\n")
        assert self._options("auto").env == {
            **_TRAFFIC_ENV,
            auth_token.ENV_VAR: _TOKEN,
        }


class TestClaudeHealthCheck:
    def test_auto_mode_is_auth_only_and_does_not_mutate_process_env(self, clean_env, monkeypatch):
        from booley.runtime._claude_backend import ClaudeSDKBackend

        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)

        assert ClaudeSDKBackend(auth_mode="auto").health_check() is None
        assert "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK" not in os.environ
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in os.environ

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
        login = auth_token.subscription_creds_path("codex")
        login.parent.mkdir(parents=True)
        login.write_text('{"tokens":{"access_token":"SYNTHETIC"}}')
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
        assert captured["env"]["OPENAI_API_KEY"] == "sk-proj-x"


class TestSandboxEnvFileScrub:
    @staticmethod
    def _env_file_body(monkeypatch) -> str | None:
        from booley.harness import sandbox

        cmd: list[str] = []
        runner = sandbox.DockerRunner.__new__(sandbox.DockerRunner)
        runner.worktree = Path.home() / "project"
        (runner.worktree / ".booley").mkdir(parents=True, exist_ok=True)
        policy = os.environ.get("BOOLEY_PRIMARY_AUTH", "auto")
        (runner.worktree / ".booley" / "booley.toml").write_text(f'[agent]\nauth="{policy}"\n')
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
        creds.write_text('{"claudeAiOauth":{"accessToken":"SYNTHETIC"}}', encoding="utf-8")
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
        creds.write_text('{"claudeAiOauth":{"accessToken":"SYNTHETIC"}}', encoding="utf-8")

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

        monkeypatch.setattr(
            doctor.host_environment,
            "inspect_agent_installation",
            lambda provider: doctor.host_environment.AgentInstallation(provider == "claude", None),
        )
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
            booley_dir=tmp_path / ".booley",
            booley_toml={"agent": {"auth": "subscription"}},
            configs_toml={},
            first_target="",
        )

        assert doctor._configured_auth_policy(audit) == "subscription"

    def test_unconfigured_policy_is_auto(self, clean_env):
        from booley.harness import doctor

        assert doctor._configured_auth_policy(None) == "auto"


@pytest.mark.parametrize("app", ["claude", "codex"])
@pytest.mark.parametrize("policy", ["auto", "subscription", "api_key"])
@pytest.mark.parametrize(
    "key,stored,login", [(bool(n & 4), bool(n & 2), bool(n & 1)) for n in range(8)]
)
def test_issued_policy_matrix(clean_env, monkeypatch, app, policy, key, stored, login):
    """Issued env and seed mounts agree with the independent billing table."""
    import json

    from booley.harness import init_cmd
    from booley.harness.setup.common import InitContext
    from booley.runtime.session_issuance import SessionSpecInputs

    root = clean_env / "project"
    project_data = root / ".booley"
    project_data.mkdir(parents=True)
    (root / ".devcontainer").mkdir()
    (project_data / "booley.toml").write_text(f'[agent]\nprovider="{app}"\nauth="{policy}"\n')
    if key:
        monkeypatch.setenv(auth_token.API_KEY_ENV[app], "SYNTHETIC_API_KEY")
    if stored:
        auth_token.store_token("SYNTHETIC_STORED", app)
    if login:
        path = auth_token.subscription_creds_path(app)
        path.parent.mkdir(parents=True)
        payload = (
            {"claudeAiOauth": {"accessToken": "SYNTHETIC_LOGIN"}}
            if app == "claude"
            else {"tokens": {"access_token": "SYNTHETIC_LOGIN"}, "OPENAI_API_KEY": None}
        )
        path.write_text(json.dumps(payload))
    ctx = InitContext(project_root=root)
    sources = init_cmd._interactive_spec_sources(
        ctx, init_cmd._NANGATE_PDK_NOT_REQUESTED, app, None
    )
    inputs = SessionSpecInputs(
        project_data_source=project_data,
        trusted_eda_mounts=(),
        fixed_container_environment=(),
        installation_name=None,
        license_profile_name=None,
    )
    spec = sources.build(inputs)
    expected_keys = set()
    if key and policy != "subscription":
        expected_keys.add(auth_token.API_KEY_ENV[app])
    if stored and (
        policy == "auto"
        or (app == "claude" and policy == "subscription")
        or (app == "codex" and policy == "api_key")
    ):
        expected_keys.add(auth_token.CREDENTIALS[app].env_var)
    credential_keys = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"}
    assert set(spec["remoteEnv"]) & credential_keys == expected_keys
    assert spec["remoteEnv"]["BOOLEY_PRIMARY_AUTH"] == policy
    assert "SYNTHETIC" not in json.dumps(spec)
    mounts = "\n".join(spec["mounts"])
    assert (".claude-creds-seed.json" in mounts or ".codex-auth-seed.json" in mounts) == (
        login and policy != "api_key"
    )
    token_allowed = (
        policy == "auto"
        or (app == "claude" and policy == "subscription")
        or (app == "codex" and policy == "api_key")
    )
    assert (auth_token.TOKEN_SEED_BASENAME[app] in mounts) == (stored and token_allowed)


@pytest.mark.parametrize("app", ["claude", "codex"])
@pytest.mark.parametrize("policy", ["auto", "subscription", "api_key"])
@pytest.mark.parametrize(
    "key,stored,login", [(bool(n & 4), bool(n & 2), bool(n & 1)) for n in range(8)]
)
def test_reporting_policy_matrix(clean_env, monkeypatch, capsys, app, policy, key, stored, login):
    """Status exit/billing and Doctor grading share the actual selected credential."""
    import argparse
    import json

    from booley.harness import auth_cmd, doctor

    root = clean_env / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text(f'[agent]\nprovider="{app}"\nauth="{policy}"\n')
    if key:
        monkeypatch.setenv(auth_token.API_KEY_ENV[app], "SYNTHETIC_API_KEY")
    if stored:
        auth_token.store_token("SYNTHETIC_STORED", app)
    if login:
        path = auth_token.subscription_creds_path(app)
        path.parent.mkdir(parents=True)
        payload = (
            {"claudeAiOauth": {"accessToken": "SYNTHETIC_LOGIN"}}
            if app == "claude"
            else {"tokens": {"access_token": "SYNTHETIC_LOGIN"}, "OPENAI_API_KEY": None}
        )
        path.write_text(json.dumps(payload))
    expected = (
        "api_key" if policy != "subscription" and (key or (app == "codex" and stored)) else None
    )
    if expected is None and policy != "api_key":
        expected = (
            "oauth_token" if app == "claude" and stored else "subscription" if login else None
        )
    code = auth_cmd.run_auth(argparse.Namespace(status=True, app=app), root)
    output = capsys.readouterr().out
    monkeypatch.setattr(doctor, "_agent_app_installed", lambda _: True)
    audit = doctor.ProjectAudit(
        project_root=root,
        booley_dir=data,
        booley_toml={"agent": {"provider": app, "auth": policy}},
        configs_toml={},
        first_target="",
    )
    reporter = doctor._Reporter.create()
    doctor._run_agent_credential_checks(audit, reporter)
    findings = reporter.findings or []
    sink = {
        name: [f.message for f in findings if f.severity.value == name]
        for name in ("pass", "warn", "skip", "fail")
    }
    assert code == (0 if expected else 1)
    assert f"auth policy '{policy}'" in output
    assert "SYNTHETIC" not in output + str(sink)
    assert bool(sink["fail"]) == (expected is None and policy != "auto")
    assert any(" auth:" in message for message in sink["pass"]) == (expected is not None)
    if expected == "api_key":
        assert "bills per token" in output
    elif expected == "oauth_token":
        assert "bills against the subscription" in output
    elif expected == "subscription":
        assert "refreshing subscription login" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("app", ["claude", "codex"])
@pytest.mark.parametrize("policy", ["auto", "subscription", "api_key"])
@pytest.mark.parametrize(
    "key,stored,login", [(bool(n & 4), bool(n & 2), bool(n & 1)) for n in range(8)]
)
async def test_registrar_and_child_policy_matrix(
    clean_env, monkeypatch, app, policy, key, stored, login
):
    """Registrar results and captured native child env cannot bypass pinned billing."""
    import json

    from booley.runtime import _claude_backend, incontainer_setup

    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", policy)
    if key:
        monkeypatch.setenv(auth_token.API_KEY_ENV[app], "SYNTHETIC_API_KEY")
    if stored:
        auth_token.store_token("SYNTHETIC_STORED", app)
    if login:
        native = auth_token.subscription_creds_path(app)
        native.parent.mkdir(parents=True)
        native.write_text(
            json.dumps(
                {"claudeAiOauth": {"accessToken": "SYNTHETIC_LOGIN"}}
                if app == "claude"
                else {"tokens": {"access_token": "SYNTHETIC_LOGIN"}, "OPENAI_API_KEY": None}
            )
        )
    container = clean_env / "container-home"
    container.mkdir()
    token_allowed = (
        policy == "auto"
        or (app == "claude" and policy == "subscription")
        or (app == "codex" and policy == "api_key")
    )
    if stored and token_allowed:
        (container / auth_token.TOKEN_SEED_BASENAME[app]).write_text("SYNTHETIC_STORED")
    incontainer_setup.apply_stored_credential(app, container)
    if app == "claude":
        settings = container / ".claude" / "settings.json"
        persisted = json.loads(settings.read_text()).get("env", {}) if settings.exists() else {}
        child = {**os.environ, **_claude_backend._claude_env_overrides(policy)}
        assert bool(persisted.get("CLAUDE_CODE_OAUTH_TOKEN")) == (stored and token_allowed)
    else:
        child = await _captured_codex_child(clean_env, monkeypatch, policy, key, stored, login)
        if child is None:
            return
        auth = container / ".codex" / "auth.json"
        assert auth.exists() == (token_allowed and (stored or key))
    assert bool(child.get(auth_token.API_KEY_ENV[app])) == (
        policy != "subscription" and (key or (app == "codex" and stored))
    )
    if app == "claude":
        assert bool(child.get("CLAUDE_CODE_OAUTH_TOKEN")) == (stored and token_allowed)


async def _captured_codex_child(root, monkeypatch, policy, key, stored, login):
    import asyncio

    from booley.runtime import _codex_backend

    captured = {}

    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(_codex_backend, "_inside_container", lambda: False)
    usable = (
        (key or stored) if policy == "api_key" else login if policy == "subscription" else True
    )
    params = AgentCallParams(prompt="p", model="gpt-5.5", cwd=str(root))
    if not usable:
        with pytest.raises(ValueError, match="no usable credential"):
            await _codex_backend._codex_spawn(["codex"], params, policy)
        return None
    await _codex_backend._codex_spawn(["codex"], params, policy)
    return captured["env"] or dict(os.environ)


@pytest.mark.parametrize("policy", ["subscription", "api_key"])
@pytest.mark.parametrize(
    "payload",
    [
        '{"primaryApiKey":"SYNTHETIC_SECRET"}',
        '{"apiKeyHelper":"SYNTHETIC_SECRET"}',
        '{"customApiKeyResponses":{"rejected":["SYNTHETIC_SECRET"]}}',
        "not-json",
    ],
)
def test_explicit_policy_refuses_ambiguous_claude_seed_without_mutation(
    clean_env, policy, payload
):
    config = Path.home() / ".claude.json"
    config.write_text(payload)
    with pytest.raises(ValueError, match="config seed") as caught:
        auth_token.credential_plan("claude", policy)
    assert "SYNTHETIC_SECRET" not in str(caught.value)
    assert config.read_text() == payload


def test_explicit_api_policy_keeps_ordinary_claude_config(clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_KEY")
    config = Path.home() / ".claude.json"
    config.write_text('{"hasCompletedOnboarding":true,"cachedStatsigGates":{"ordinary":true}}')
    plan = auth_token.credential_plan("claude", "api_key")
    assert plan.config_seed == config
    assert plan.subscription_seed is None
    assert plan.token_seed is None


def test_malformed_or_unreadable_codex_login_never_establishes_subscription(
    clean_env, monkeypatch
):
    native = auth_token.subscription_creds_path("codex")
    native.parent.mkdir()
    native.write_text("not-json")
    assert auth_token.effective_credential("codex", "subscription") is None
    native.write_text('{"tokens":{"access_token":"SYNTHETIC"}}')
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == native:
            raise PermissionError("synthetic read refusal")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert auth_token.effective_credential("codex", "subscription") is None
