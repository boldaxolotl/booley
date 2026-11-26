"""Gateway auth policy (#2266): validation and the BOOLEY_PRIMARY_AUTH hand-off."""

from __future__ import annotations

import pytest
from tests.harness.agent_auth_support import pin_venue

from booley import config
from booley.config import agent as agent_config

_URL = "https://gateway.example/v1"
_TOKEN = "SYNTHETIC_GATEWAY_TOKEN"


@pytest.fixture
def gateway_container(monkeypatch):
    """A Sandbox-like process: gateway hand-off honored, no ambient auth state."""
    for name in (
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_GATEWAY_URL",
        "BOOLEY_GATEWAY_TOKEN",
        "BOOLEY_GATEWAY_TOKEN_COMMAND",
        "BOOLEY_DIR",
        "BOOLEY_AGENT_CLIS",
        "BOOLEY_PRIMARY_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    pin_venue(monkeypatch, inside=True)
    agent_config.set_agent_settings(None)
    yield monkeypatch
    agent_config.set_agent_settings(None)


def test_gateway_policy_accepts_complete_https_config():
    settings = config.gateway.gateway_settings_from_env(
        {"BOOLEY_GATEWAY_URL": f" {_URL} ", "BOOLEY_GATEWAY_TOKEN": f" {_TOKEN}\n"}
    )
    assert settings == config.gateway.GatewaySettings(url=_URL, token=_TOKEN)
    assert _TOKEN not in repr(settings)


def test_gateway_policy_accepts_token_command_alone():
    settings = config.gateway.gateway_settings_from_env(
        {
            "BOOLEY_GATEWAY_URL": _URL,
            "BOOLEY_GATEWAY_TOKEN_COMMAND": " gateway-login --print-token \n",
        }
    )

    assert settings.token is None
    assert settings.token_command == "gateway-login --print-token"
    assert settings.token_source == "BOOLEY_GATEWAY_TOKEN_COMMAND"
    assert "gateway-login" not in repr(settings)


_COMMAND = "BOOLEY_GATEWAY_TOKEN_COMMAND"
_WITH_URL = {"BOOLEY_GATEWAY_URL": _URL}


@pytest.mark.parametrize(
    ("environ", "match"),
    [
        (
            {**_WITH_URL, "BOOLEY_GATEWAY_TOKEN": _TOKEN, _COMMAND: "gateway-login"},
            "exactly one of BOOLEY_GATEWAY_TOKEN and",
        ),
        (_WITH_URL, "BOOLEY_GATEWAY_TOKEN or BOOLEY_GATEWAY_TOKEN_COMMAND"),
        ({**_WITH_URL, _COMMAND: "'SYNTHETIC_UNTERMINATED"}, _COMMAND),
        ({"BOOLEY_GATEWAY_TOKEN": _TOKEN}, "needs BOOLEY_GATEWAY_URL"),
        (
            {"BOOLEY_GATEWAY_URL": "http://gateway.example/v1", "BOOLEY_GATEWAY_TOKEN": _TOKEN},
            "BOOLEY_GATEWAY_URL must be an https://",
        ),
        ({**_WITH_URL, "BOOLEY_GATEWAY_TOKEN": "  "}, "needs BOOLEY_GATEWAY_TOKEN"),
        *(
            ({"BOOLEY_GATEWAY_URL": url, "BOOLEY_GATEWAY_TOKEN": _TOKEN}, None)
            for url in (
                "https://user:SYNTHETIC_PASSWORD@gateway.example/v1",
                "https:///v1",
                "https://gateway.example/v1 extra",
                "https://gateway.example/v1?api_key=SYNTHETIC_QUERY",
            )
        ),
    ],
)
def test_gateway_policy_rejects_invalid_config_without_echoing_secrets(environ, match):
    with pytest.raises(ValueError, match=match) as caught:
        config.gateway.gateway_settings_from_env(environ)
    assert "SYNTHETIC" not in str(caught.value)


@pytest.mark.parametrize(
    "toml",
    [
        b'[agent]\nprovider = "codex"\nauth = "subscription"\n',
        b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n',
    ],
)
def test_gateway_handoff_reaches_loaded_agent_settings(gateway_container, tmp_path, toml):
    """The MCP server's toml-only loader must see the env-only policy, with no CLI record."""
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    (project / ".booley" / "booley.toml").write_bytes(toml)
    gateway_container.setenv("BOOLEY_PRIMARY_AUTH", "gateway")

    settings = agent_config.load_agent_settings(project)

    assert settings.auth_for("codex") == "gateway"
    assert agent_config.gateway_handoff_active()
    assert agent_config.sandbox_cli_policy("claude") == "gateway"
    assert not agent_config.issued_codex_gateway()


def test_gateway_handoff_with_surrounding_whitespace_is_one_policy_everywhere(gateway_container):
    gateway_container.setenv("BOOLEY_PRIMARY_AUTH", "gateway \n")
    gateway_container.setenv("BOOLEY_PRIMARY_PROVIDER", "codex")

    assert agent_config.env_auth_handoff() == "gateway"
    assert agent_config.get_agent_settings().auth_for("codex") == "gateway"


def test_cli_record_accepts_codex_gateway_and_refuses_claude_gateway():
    assert agent_config.decode_cli_policies("claude=subscription,codex=gateway") == {
        "claude": "subscription",
        "codex": "gateway",
    }
    with pytest.raises(ValueError, match="malformed Agent CLI record entry"):
        agent_config.decode_cli_policies("claude=gateway,codex=subscription")


def test_issued_record_rules_per_cli_and_hides_nothing_without_codex_gateway(gateway_container):
    gateway_container.setenv("BOOLEY_PRIMARY_AUTH", "subscription")
    gateway_container.setenv("BOOLEY_AGENT_CLIS", "claude=subscription,codex=gateway")

    assert agent_config.sandbox_cli_policy("claude") == "subscription"
    assert agent_config.sandbox_cli_policy("codex") == "gateway"
    assert agent_config.gateway_handoff_active()  # whichever CLI is the default
    assert agent_config.issued_codex_gateway()

    gateway_container.setenv("BOOLEY_AGENT_CLIS", "claude=subscription")
    assert agent_config.sandbox_cli_policy("codex") is None
    assert not agent_config.gateway_handoff_active()
    assert not agent_config.issued_codex_gateway()


def test_record_policies_apply_per_cli_over_the_handoff(gateway_container):
    """With a record, ``_auth_policies`` applies it per CLI; the hand-off does not widen it."""
    gateway_container.setenv("BOOLEY_PRIMARY_AUTH", "subscription")
    gateway_container.setenv("BOOLEY_AGENT_CLIS", "claude=subscription,codex=gateway")
    data = {"agent": {"clis": ["claude", "codex"], "default": "claude"}}

    settings = agent_config.settings_from_data(data, lazy=True)

    assert dict(settings.auth) == {"claude": "subscription", "codex": "gateway"}
    # The toml-only loader (not lazy) still routes Codex, whichever CLI is the default.
    assert agent_config.settings_from_data(data, lazy=False).auth_for("codex") == "gateway"


def test_gateway_policy_is_not_honored_outside_a_sandbox(gateway_container, tmp_path):
    pin_venue(gateway_container, inside=False)
    gateway_container.setenv("BOOLEY_AGENT_CLIS", "claude=subscription,codex=gateway")
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    (project / ".booley" / "booley.toml").write_bytes(
        b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n'
    )

    assert agent_config.load_agent_settings(project).auth_for("codex") == "subscription"
    assert not agent_config.issued_codex_gateway()
    assert not agent_config.gateway_handoff_active()
