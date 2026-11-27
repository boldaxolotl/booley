"""Tests for the ``[agent] auth`` policy: parsing, resolution, and enforcement.

The knob pins what bills; a missing value is ``subscription`` and ``auto`` is retired.
``subscription`` makes Booley scrub the API-key env vars from every agent
environment it controls (the Claude CLI's own prescribed remedy: "Unset
ANTHROPIC_API_KEY to use your claude.ai account instead"); ``api_key`` fails
loud in the backend health check when the key is absent, because the CLI would
otherwise silently fall back to — and bill — the subscription.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

from booley.config import agent as bc
from booley.core.models import AgentCallParams
from booley.runtime import auth_token
from booley.runtime._claude_backend import ClaudeSDKBackend
from tests.claude_cli_fake import record_sdk_options
from tests.harness.agent_auth_support import credential_findings, install_agent_clis, pin_venue
from tests.path_tool_fake import PathToolFake

_TOKEN = "sk-ant-oat01-abcdef123456"
_TRAFFIC_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
_TRAFFIC = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
_CLAUDE_LOGIN = '{"claudeAiOauth":{"accessToken":"SYNTHETIC"}}'
_BLANK = {"ANTHROPIC_API_KEY": ""}


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """No auth-relevant env leaking in; HOME and config dir in a tmp tree."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "userhome"))
    (tmp_path / "userhome").mkdir()
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_AGENT_CLIS",
        "BOOLEY_AGENT_APP",
        "BOOLEY_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _fresh_settings():
    bc.set_agent_settings(None)
    yield
    bc.set_agent_settings(None)


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        ({}, None),
        ({"auth": "subscription"}, "subscription"),
        ({"auth": "api_key"}, "api_key"),
        ({"primary_auth": "subscription"}, None),  # the retired alias is ignored
        (
            {"auth": {"claude": "subscription", "codex": "gateway"}},
            {"claude": "subscription", "codex": "gateway"},
        ),
    ],
)
def test_agent_section_auth(agent, expected):
    assert bc.parse_agent_section(agent).auth == expected


def test_misspelled_auth_is_rejected():
    # A typo must not silently bill a credential the project never chose.
    with pytest.raises(bc.BackendConfigError, match="subscripton"):
        bc.parse_agent_section({"auth": "subscripton"})


_BEARER_ENV = {"ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_BEARER"}
_VISITOR = auth_token.VISITOR_AUTH
_STORED = {auth_token.ENV_VAR: _TOKEN}


@pytest.mark.parametrize(
    ("mode", "env", "setup", "expected"),
    [
        (_VISITOR, {}, "", _TRAFFIC_ENV),
        (_VISITOR, {_TRAFFIC: "0"}, "", {_TRAFFIC: "0"}),
        (
            "subscription",
            _BEARER_ENV,
            "login",
            {**_TRAFFIC_ENV, **_BLANK, "ANTHROPIC_AUTH_TOKEN": ""},
        ),
        ("api_key", {"ANTHROPIC_API_KEY": "sk-ant-api03-x"}, "stored", _TRAFFIC_ENV),
        (_VISITOR, _BEARER_ENV, "stored", {**_TRAFFIC_ENV, **_STORED}),
        ("subscription", {}, "stored", {**_TRAFFIC_ENV, **_BLANK, **_STORED}),
        (_VISITOR, {auth_token.ENV_VAR: "sk-ant-oat01-ambient"}, "stored", _TRAFFIC_ENV),
        (_VISITOR, {}, "seed", {**_TRAFFIC_ENV, **_STORED}),
    ],
    ids=[
        "visitor-traffic-off",
        "visitor-ambient-traffic",
        "subscription-login",
        "api-key-no-token",
        "visitor-stored",
        "subscription-stored",
        "visitor-ambient-token",
        "visitor-seed",
    ],
)
def test_claude_child_env_follows_the_policy(clean_env, monkeypatch, mode, env, setup, expected):
    """The env the Claude CLI receives: ``options.env`` only merges OVER the inherited env,
    so "unset" is an empty value, and the rotation-free ``booley auth`` credential is handed
    over here because SDK runs exclude "user" settings."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if setup == "stored":
        auth_token.store_token(_TOKEN)
    if setup == "seed":
        # In-container there is no ~/.config/booley store; the spec bind-mounts the host's
        # stored credential at a home sidecar instead.
        (Path.home() / auth_token.TOKEN_SEED_BASENAME[auth_token.APP_CLAUDE]).write_text(_TOKEN)
    if setup == "login":
        creds = auth_token.subscription_creds_path("claude")
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(_CLAUDE_LOGIN, encoding="utf-8")
    seen = record_sdk_options(monkeypatch)

    anyio.run(
        ClaudeSDKBackend(auth_mode=mode).call,
        AgentCallParams(prompt="p", model="claude-test", cwd=clean_env),
    )

    assert seen[0].env == expected


@pytest.mark.parametrize(
    ("backend", "mode", "env", "fragment"),
    [
        ("claude", auth_token.VISITOR_AUTH, {}, None),
        ("claude", "api_key", {}, "ANTHROPIC_API_KEY"),
        ("claude", "subscription", {}, "subscription"),
        ("claude", "api_key", {"ANTHROPIC_API_KEY": "sk-ant-api03-x"}, None),
        ("codex", "api_key", {}, "OPENAI_API_KEY"),
        ("codex", "subscription", {}, "codex login"),
        ("codex", auth_token.VISITOR_AUTH, {}, None),
    ],
)
def test_health_check_fails_loud_without_the_policy_credential(
    clean_env, monkeypatch, backend, mode, env, fragment
):
    from booley.runtime import _codex_backend

    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if backend == "codex":
        PathToolFake.install(clean_env / "bin", "codex", monkeypatch)
        checked = _codex_backend.CodexBackend(auth_mode=mode)
    else:
        checked = ClaudeSDKBackend(auth_mode=mode)

    warning = checked.health_check()

    if fragment is None:
        assert warning is None
    else:
        assert warning is not None and fragment in warning
    # The health check must not leak its probing into the process environment.
    assert "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK" not in os.environ
    assert _TRAFFIC not in os.environ


def test_shell_env_file_under_subscription_carries_only_the_oauth_token(clean_env, monkeypatch):
    from booley.harness.sandbox import DockerRunner, DockerSandboxConfig

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-x")
    auth_token.store_token(_TOKEN)
    # Codex's stored credential IS an API key; under subscription it is not injected either.
    auth_token.store_token("sk-proj-stored", auth_token.APP_CODEX)
    root = clean_env / "project"
    (root / ".booley").mkdir(parents=True)
    (root / ".booley" / "booley.toml").write_text(
        '[agent]\nclis = ["claude", "codex"]\nauth = "subscription"\n', encoding="utf-8"
    )
    runner = DockerRunner(DockerSandboxConfig(image="test-image", needs_network=False), root)

    argv = runner.ephemeral_argv(["true"], tty=False)

    try:
        body = Path(argv[argv.index("--env-file") + 1]).read_text(encoding="utf-8")
    finally:
        runner.cleanup_ephemeral()
    assert body == f"CLAUDE_CODE_OAUTH_TOKEN={_TOKEN}\n"


@pytest.mark.parametrize(
    ("inside", "policy", "env", "expected_note", "expected_pass"),
    [
        pytest.param(False, "subscription", {}, True, False, id="subscription"),
        pytest.param(
            True,
            auth_token.VISITOR_AUTH,
            {"BOOLEY_PRIMARY_AUTH": "auto"},
            False,
            True,
            id="visitor",
        ),
    ],
)
def test_doctor_grades_an_exported_key_under_the_policy(
    clean_env, monkeypatch, inside, policy, env, expected_note, expected_pass
):
    """Under subscription the key never reaches the agents, so it cannot count as the
    rotation-free credential (an accepted NOTE, no WARN); the visitor mode accepts it."""
    from booley.runtime import runtime_context

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
    pin_venue(monkeypatch, inside=inside)
    # Pin the venue: a Sandbox without a seed sidecar reports a different NOTE.
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: inside)
    install_agent_clis(Path.home())

    findings = credential_findings(
        clean_env / "project", {"agent": {"provider": "claude", "auth": policy}}
    )

    by_severity = {
        name: [f.message for f in findings if f.severity.value == name]
        for name in ("pass", "note", "warn", "fail")
    }
    assert by_severity["warn"] == []
    assert any("revoke" in m and "accepted" in m for m in by_severity["note"]) == expected_note
    assert any("API key" in m for m in by_severity["pass"]) == expected_pass


def test_unconfigured_policy_is_subscription(clean_env, monkeypatch):
    from booley.harness import doctor

    pin_venue(monkeypatch, inside=False)

    # private: no Doctor entry point passes a missing Project; tests/runtime/test_agent_app_auto.py
    # also pins this fallback.
    assert doctor._agent_audit(None, None).policy("claude") == "subscription"
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "api_key")
    assert doctor._agent_audit(None, None).policy("claude") == "api_key"


def _give_credentials(app, key, stored, login, monkeypatch):
    """Set up the exported key, stored token and native login a matrix case names."""
    if key:
        monkeypatch.setenv(auth_token.API_KEY_ENV[app], "SYNTHETIC_API_KEY")
    if stored:
        auth_token.store_token("SYNTHETIC_STORED", app)
    if login:
        path = auth_token.subscription_creds_path(app)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            {"claudeAiOauth": {"accessToken": "SYNTHETIC_LOGIN"}}
            if app == "claude"
            else {"tokens": {"access_token": "SYNTHETIC_LOGIN"}, "OPENAI_API_KEY": None}
        )
        path.write_text(json.dumps(payload))


@pytest.mark.parametrize("app", ["claude", "codex"])
@pytest.mark.parametrize("policy", ["subscription", "api_key"])
@pytest.mark.parametrize(
    "key,stored,login", [(bool(n & 4), bool(n & 2), bool(n & 1)) for n in range(8)]
)
def test_issued_policy_matrix(clean_env, monkeypatch, app, policy, key, stored, login):
    """Issued env and seed mounts agree with the independent billing table."""
    from booley.harness import init_cmd
    from booley.harness.setup.common import InitContext
    from booley.runtime.session_issuance import SessionSpecInputs

    root = clean_env / "project"
    project_data = root / ".booley"
    project_data.mkdir(parents=True)
    (root / ".devcontainer").mkdir()
    (project_data / "booley.toml").write_text(f'[agent]\nprovider="{app}"\nauth="{policy}"\n')
    _give_credentials(app, key, stored, login, monkeypatch)
    ctx = InitContext(project_root=root)
    # private: booley init writes this spec through Docker and VS Code; _sandbox_spec_sources is
    # the spec it issues.
    sources = init_cmd._sandbox_spec_sources(ctx, init_cmd._NANGATE_PDK_NOT_REQUESTED, app, None)
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
        (app == "claude" and policy == "subscription") or (app == "codex" and policy == "api_key")
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
    token_allowed = (app == "claude" and policy == "subscription") or (
        app == "codex" and policy == "api_key"
    )
    assert (auth_token.TOKEN_SEED_BASENAME[app] in mounts) == (stored and token_allowed)


@pytest.mark.parametrize("app", ["claude", "codex"])
@pytest.mark.parametrize("policy", ["subscription", "api_key"])
@pytest.mark.parametrize(
    "key,stored,login", [(bool(n & 4), bool(n & 2), bool(n & 1)) for n in range(8)]
)
def test_reporting_policy_matrix(clean_env, monkeypatch, capsys, app, policy, key, stored, login):
    """Status exit/billing and Doctor grading share the actual selected credential."""
    import argparse

    from booley.harness import auth_cmd

    root = clean_env / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text(f'[agent]\nprovider="{app}"\nauth="{policy}"\n')
    _give_credentials(app, key, stored, login, monkeypatch)
    expected = (
        "api_key" if policy != "subscription" and (key or (app == "codex" and stored)) else None
    )
    if expected is None and policy != "api_key":
        expected = (
            "oauth_token" if app == "claude" and stored else "subscription" if login else None
        )
    code = auth_cmd.run_auth(argparse.Namespace(status=True, app=app), root)
    output = capsys.readouterr().out
    install_agent_clis(Path.home())
    findings = credential_findings(root, {"agent": {"provider": app, "auth": policy}})
    assert not [f for f in findings if f.check_id == "agent.credential-policy"], policy
    sink = {
        name: [f.message for f in findings if f.severity.value == name]
        for name in ("pass", "warn", "skip", "fail")
    }
    assert code == (0 if expected else 1)
    assert f"auth policy '{policy}'" in output
    assert "SYNTHETIC" not in output + str(sink)
    assert bool(sink["fail"]) == (expected is None)
    assert any(" auth:" in message for message in sink["pass"]) == (expected is not None)
    if expected == "api_key":
        assert "bills per token" in output
    elif expected == "oauth_token":
        assert "bills against the subscription" in output
    elif expected == "subscription":
        assert "refreshing subscription login" in output


_PROJECT_NOTE = "credentials come from the environment this command inherited"


@pytest.mark.parametrize(
    ("app", "in_project", "broken", "notes", "code"),
    [
        pytest.param(
            None,
            True,
            False,
            [
                f"codex: auth policy 'subscription'; {_PROJECT_NOTE}",
                f"claude: auth policy 'api_key'; {_PROJECT_NOTE}",
            ],
            0,
            id="every-cli",
        ),
        pytest.param(
            "codex",
            True,
            False,
            [f"codex: auth policy 'subscription'; {_PROJECT_NOTE}"],
            1,
            id="filter-codex",
        ),
        pytest.param(
            "claude",
            True,
            False,
            [f"claude: auth policy 'api_key'; {_PROJECT_NOTE}"],
            0,
            id="filter-claude",
        ),
        pytest.param(
            None,
            True,
            True,
            [
                f"codex: auth policy 'subscription'; {_PROJECT_NOTE}",
                f"claude: auth policy 'api_key'; {_PROJECT_NOTE}",
            ],
            0,
            id="one-cli-broken",
        ),
        pytest.param(
            None,
            False,
            False,
            [f"auth policy 'subscription'; {_PROJECT_NOTE}"],
            1,
            id="outside-project",
        ),
    ],
)
def test_auth_status_reports_each_project_cli_under_its_policy(
    clean_env, monkeypatch, capsys, app, in_project, broken, notes, code
):
    """In a Project `auth --status` reports every allowed CLI, default first, each under
    its own policy; `--cli` filters; one CLI's unplannable credential hides no other."""
    import argparse

    from booley.harness import auth_cmd
    from booley.presentation import colors

    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    root = clean_env / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text(
        '[agent]\nclis = ["claude", "codex"]\ndefault = "codex"\n'
        '[agent.auth]\nclaude = "api_key"\ncodex = "subscription"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_API_KEY")
    if broken:
        login = auth_token.subscription_creds_path("codex")
        login.parent.mkdir(parents=True, exist_ok=True)
        login.write_text('{"tokens":{"access_token":"SYNTHETIC"}}', encoding="utf-8")
        (Path.home() / ".claude.json").write_text(
            '{"primaryApiKey": "SYNTHETIC"}', encoding="utf-8"
        )

    result = auth_cmd.run_auth(
        argparse.Namespace(status=True, app=app), root if in_project else None
    )

    captured = capsys.readouterr()
    lines = (captured.out + captured.err).splitlines()
    assert [line.split("  ", 2)[-1] for line in lines if "auth policy '" in line] == notes
    assert result == code
    if app == "codex":
        assert "claude:" not in captured.out + captured.err
    if broken:
        assert any(
            line.split("  ", 2)[-1].startswith("claude: Claude config seed contains ambiguous")
            for line in lines
        )


@pytest.mark.parametrize(
    "toml",
    ['[agent]\nauth = "api_key"\n', '[agent.auth]\ncodex = "api_key"\n'],
    ids=["string-auth", "per-cli-auth"],
)
def test_auth_status_requested_cli_is_allowed_when_project_declares_none(
    clean_env, monkeypatch, capsys, toml
):
    """With no ``[agent]`` CLI declared, ``--cli codex`` is the allowed set, as at issuance."""
    import argparse

    from booley.harness import auth_cmd

    root = clean_env / "project"
    data = root / ".booley"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text(toml)
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_API_KEY")

    code = auth_cmd.run_auth(argparse.Namespace(status=True, app="codex"), root)

    output = capsys.readouterr().out
    assert code == 0
    assert "auth policy 'api_key'" in output


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
    from booley.runtime import _claude_backend, incontainer_setup

    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", policy)
    pin_venue(monkeypatch, inside=False)
    _give_credentials(app, key, stored, login, monkeypatch)
    container = clean_env / "container-home"
    container.mkdir()
    mode = auth_token.VISITOR_AUTH if policy == "auto" else policy  # "auto" spells visitor
    token_allowed = (
        mode == auth_token.VISITOR_AUTH
        or (app == "claude" and policy == "subscription")
        or (app == "codex" and policy == "api_key")
    )
    if stored and token_allowed:
        (container / auth_token.TOKEN_SEED_BASENAME[app]).write_text("SYNTHETIC_STORED")
    incontainer_setup.apply_stored_credential(app, container)
    if app == "claude":
        settings = container / ".claude" / "settings.json"
        persisted = json.loads(settings.read_text()).get("env", {}) if settings.exists() else {}
        # private: the matrix includes unusable credentials, where ClaudeSDKBackend.call refuses
        # before it builds the env.
        child = {**os.environ, **_claude_backend._claude_env_overrides(mode)}
        assert bool(persisted.get("CLAUDE_CODE_OAUTH_TOKEN")) == (stored and token_allowed)
    else:
        child = await _captured_codex_child(clean_env, monkeypatch, mode, key, stored, login)
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
    usable = (
        (key or stored) if policy == "api_key" else login if policy == "subscription" else True
    )
    params = AgentCallParams(prompt="p", model="gpt-5.5", cwd=str(root))
    # private: the shared fake codex runs only as a Sandbox with a nested home; this matrix pins
    # the host spawn.
    spawn_codex = _codex_backend._codex_spawn
    if not usable:
        with pytest.raises(ValueError, match="no usable credential"):
            await spawn_codex(["codex"], params, policy)
        return None
    await spawn_codex(["codex"], params, policy)
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


def _project_with_agent_section(root: Path, agent_toml: str) -> Path:
    project = root / "project"
    (project / ".booley").mkdir(parents=True)
    (project / ".booley" / "booley.toml").write_bytes(f"[agent]\n{agent_toml}".encode())
    return project


@pytest.mark.parametrize(
    ("agent_toml", "named"),
    [
        ('default = "claude"\nauth = "auto"\n', "auth = 'auto'"),
        ('default = "codex"\n[agent.auth]\ncodex = "auto"\n', "[agent.auth] codex = 'auto'"),
    ],
    ids=["string", "table"],
)
def test_retired_auto_auth_is_rejected_at_load(clean_env, agent_toml, named):
    project = _project_with_agent_section(clean_env, agent_toml)

    with pytest.raises(bc.BackendConfigError) as caught:
        bc.load_agent_settings(project)

    message = str(caught.value)
    assert named in message
    assert "is retired" in message
    assert 'set "subscription" or "api_key"' in message
    assert "booley init" in message


@pytest.mark.parametrize("inside", [False, True], ids=["host", "container"])
@pytest.mark.parametrize("auth_line", ["", 'auth = "visitor"\n'], ids=["missing", "visitor"])
def test_missing_or_visitor_auth_outside_a_hand_written_sandbox_bills_the_subscription(
    clean_env, monkeypatch, inside, auth_line
):
    """The host, a CI runner and a hand-made devcontainer have no env hand-off."""
    pin_venue(monkeypatch, inside=inside)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-exported-api-key")
    project = _project_with_agent_section(clean_env, f'default = "claude"\n{auth_line}')

    assert bc.load_agent_settings(project).auth_for("claude") == "subscription"


def test_auto_is_not_a_credential_policy():
    assert not auth_token.defers_to_cli("auto")
    assert auth_token.defers_to_cli(auth_token.VISITOR_AUTH)
    with pytest.raises(ValueError, match="invalid credential policy"):
        auth_token.allowed_credential_keys(auth_token.APP_CLAUDE, "auto")
    with pytest.raises(ValueError, match="retired"):
        bc.decode_cli_policies("claude=auto")
    # The retired value stays readable for the two readers that repair or report it.
    assert bc.parse_agent_section({"auth": "auto"}, keep_retired_auth=True).auth == "auto"
    assert bc.decode_cli_policies("claude=auto", legacy_auto=True) == {"claude": "auto"}
    # The visitor mode is written as ``auto`` on the wire older readers expect.
    assert bc.wire_auth(auth_token.VISITOR_AUTH) == "auto"
    assert bc.wire_auth("subscription") == "subscription"
    assert bc.encode_cli_policies({"claude": "visitor", "codex": "api_key"}) == (
        "claude=auto,codex=api_key"
    )


def test_auth_status_in_a_handwritten_sandbox_accepts_the_visitor_key(
    clean_env, monkeypatch, capsys
):
    """No Project, ``BOOLEY_PRIMARY_AUTH=auto`` and only an API key: the visitor mode runs on it."""
    import argparse

    from booley.harness import auth_cmd

    pin_venue(monkeypatch, inside=True)
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

    code = auth_cmd.run_auth(argparse.Namespace(status=True, app=None), None)

    assert code == 0
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out
